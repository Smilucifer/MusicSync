# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Bidirectional sync of "我喜欢" (Liked Songs) between NetEase Cloud Music and QQ Music. Runs locally for development and on GitHub Actions on a cron schedule. The hard part is **identity** — the two platforms have different IDs for the same song — so the project maintains a CSV "song identity database" mapping `netease_id ↔ qq_id` and uses a layered matching engine to populate it.

## Common commands

The CI environment is **Linux**, but local development is on **Windows / PowerShell**. Use POSIX shell only when invoking the bash scripts.

```powershell
# Setup
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env  # then fill in real credentials

# Start the NetEase HTTP API service (required before sync.py runs).
# Needs Node.js 18+. Listens on http://localhost:3000.
bash scripts/start_netease_api.sh

# Dry-run preview (default mode — no writes anywhere)
$env:DRY_RUN="true"; python scripts/sync.py

# Real execution (writes to platforms AND saves CSV)
$env:DRY_RUN="false"; python scripts/sync.py

# Test reverse sync with a small batch (avoids NetEase rate limits)
$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts/sync.py

# Force a fresh full sync ignoring cached state
$env:FORCE_FULL_SYNC="true"; python scripts/sync.py
```

Tests are plain pytest-style assertion functions (no pytest config in repo). Run individually:

```powershell
python tests/test_netease_api_http.py   # requires the Node.js service running
python scripts/test_isrc.py             # probes whether QQ API exposes ISRC
```

There is **no lint/format/build configuration** — don't add one without asking.

## Architecture

### Pipeline (`scripts/sync.py`)

Single entry point, 8 sequential steps. Read `sync.py:136` to see the flow:

1. **Auth pre-check** — both platforms must authenticate or the run aborts.
2. **Load state** — `state/sync_state.json` (lyrics cache, prev track snapshots, unmatched attempts). On CI, restored from Actions Cache.
3. **Fetch** — pull current liked-songs lists from both platforms; upsert any new tracks into the CSV.
4. **Diff** — compute which NetEase tracks need processing (not yet `synced=1` or still need a match).
5. **Match** — layered: `manual` (CSV override) → `L1` ISRC → `L3` name+artist pre-filter → `L2` lyrics+duration verification → fallback to QQ search API.
6. **Execute (Ne→QQ)** — `manual` and L2 auto-add to QQ; pure `name_artist` is **dry-run only** (logged, never written).
7. **Save CSV** — only when `DRY_RUN=false` (matches are committed once verified).
8. **Reverse sync (QQ→Ne)** — for each `qq_only` row, search NetEase, match, add. Heavily rate-limited (see below).
9. **Cleanup** — tracks unliked on one side get removed from the other; uses `prev_*_ids` snapshot saved before Step 6 overwrites state.

### CSV is the source of truth

`csv/song_mappings.csv` — one row per song, double-ID schema (`netease_id`, `qq_id`). Either ID can be empty. Columns and helpers live in `scripts/csv_database.py`.

Key invariants enforced by `csv_database.py`:
- **QQ-only rows** are keyed by synthetic `qq_{qq_id}` in the in-memory dict; `promote_qq_row` rewrites the key to a real `netease_id` once reverse sync succeeds.
- **`_qq_index`** is an in-memory `qq_id → key` index built at load time; do not persist or iterate over it as if it were a row.
- **`synced` is independent of `match_source`** — `synced=1` means the track was actually added to the other platform, regardless of how it was matched.
- **`match_source ∈ VALID_SOURCES`** (`csv_database.py:17`). `manual` is the only source that auto-executes despite weak matching.

### Matching engine (`scripts/matcher.py`)

- **L1 (ISRC)** — exact match. **In practice unavailable** because `qqmusic-api-python` v0.6.0 doesn't expose ISRC; kept for future and for NetEase-side use.
- **L2 (lyrics+duration)** — character-level `difflib.SequenceMatcher` on `normalize_lyrics()` output, plus duration within 3s. `normalize_lyrics` strips LRC tags, credit lines (`作词:`, `Lyrics by`, etc.), and "title - artist" header lines. Recent fixes here cluster around lyrics noise — read `matcher.py` before changing similarity logic.
- **L3 (name+artist)** — cleaned name equality + artist substring. Used as a **pre-filter** before fetching lyrics for L2, and as a dry-run-only fallback.

When tweaking matching: lyrics fetches are expensive and rate-limited — preserve the L3-pre-filter-then-L2 order. The state file caches lyrics (`LYRICS_CACHE_MAX = 1000`) and explicitly evicts hex-encoded QRC entries.

### Two API clients

- `scripts/netease_api.py` — talks **HTTP to `NeteaseCloudMusicApiEnhanced`** (a separate Node.js service started by `start_netease_api.sh`). The migration away from the Windows-only `pymusiclibrary` is what makes CI possible. Auth is via `MUSIC_U` cookie (preferred) or phone+MD5 password fallback.
- `scripts/qqmusic_api_v2.py` — wraps **`qqmusic-api-python`** (async). Auth uses `QQMUSIC_KEY` + `QQMUSIC_UIN` + `QQMUSIC_EUIN` cookies extracted from a logged-in browser. There is no working refresh-token flow in this codebase despite what `.env.example` shows.

### State persistence on CI

GitHub Actions has no writable repo — state survives via **Actions Cache v4**, not git push. `.github/workflows/sync.yml` saves `state/sync_state.json` always, but only saves `csv/song_mappings.csv` when `dry_run != 'true'`. This mirrors the local `DRY_RUN` semantics in `sync.py`.

### Rate limits to respect

NetEase search is the chokepoint. Short-window 405 responses → cookie marked → IP banned for 7-10 days. The reverse-sync loop sleeps 30s before starting and 5s between searches. CI uses `REVERSE_BATCH=25`. **Don't reduce these sleeps without a strong reason.**

## Conventions specific to this repo

- **`DRY_RUN` defaults to `true`** everywhere — including the scheduled CI runs. To make scheduled runs actually execute, the workflow file would need to change. Don't assume a manual `Run workflow` will write anything unless the user toggled `dry_run=false`.
- `scripts/sync.py` reconfigures stdout to UTF-8 on Windows (`sys.stdout.reconfigure`) — necessary for printing CJK track names on the default `cp936` console.
- Helper/diagnostic scripts (`bulk_search_qq.py`, `resolve_unmatched.py`, `verify_*.py`, `test_isrc.py`) are one-shot tools, not part of the pipeline. Read before editing — many are stale or platform-specific experiments.
- The `=0.0.4` file in the repo root is an accidental pip output redirect; ignore it.

## When making changes

- **Test locally before pushing** (per user preference) — at minimum a dry-run end-to-end. CI failures from rate-limit issues take days to clear.
- **Never trigger CI without an explicit user request.**
- Treat `csv/song_mappings.csv` as production data — back up before scripted edits, prefer adding helpers to `csv_database.py` over ad-hoc rewrites.
