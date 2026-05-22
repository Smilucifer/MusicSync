# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Bidirectional sync of "我喜欢" (Liked Songs) between NetEase Cloud Music and QQ Music. Runs locally for development and on GitHub Actions on a cron schedule. The hard part is **identity** — the two platforms have different IDs for the same song — so the project keeps a SQLite "song identity database" mapping `netease_id ↔ qq_id` and uses a layered matching engine (L0/L1/L2/L3) to populate it.

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

# Real execution (writes to platforms AND updates DB + snapshot)
$env:DRY_RUN="false"; python scripts/sync.py

# Test reverse sync with a small batch (avoids NetEase rate limits)
$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts/sync.py

# Force a fresh full sync ignoring sanity check + cleanup gate
$env:FORCE_FULL_SYNC="true"; python scripts/sync.py
```

Tests are plain assertion functions (no pytest config in repo). Run individually:

```powershell
python tests/test_db.py
python tests/test_matcher.py
python tests/test_migrate.py
python tests/test_sync_plan.py
python tests/test_netease_api_http.py   # requires the Node.js service running
```

There is **no lint/format/build configuration** — don't add one without asking.

## Architecture

### Pipeline (`scripts/sync.py`)

`run_pipeline()` is the single entry. 10 sequential steps:

1. **Auth pre-check** — both platforms must authenticate or the run aborts.
2. **Fetch** — pull current liked-songs lists from both platforms.
2.5. **Sanity check** — abort if `(db_liked - fetched) / db_liked > FETCH_DROP_THRESHOLD` (default 15%). Skipped on first run or with `FORCE_FULL_SYNC`.
3. **Canonicalize (L0)** — UPSERT every fetched track into `songs` + `platform_links`. Uses `canonical_key` to attach to existing primary song; otherwise creates a new song.
4. **Diff vs DB** — categorize every song into healthy / single-side / user-unliked.
5. **Match unlinked** — for each one-sided song, search the other platform; try L1→L2→L3; pre-check `UNIQUE(platform, platform_track_id)` conflict before inserting the pending link.
6. **Plan** — collect ADD_NE / ADD_QQ / UNLIKE_NE / UNLIKE_QQ action lists.
7. **Safety gate** — if `unlike_count > UNLIKE_ABORT_THRESHOLD` or `FORCE_FULL_SYNC`, skip cleanup (ADDs still execute).
8. **Execute** — call APIs; per-action commit; flip `liked` + set `synced_at` only after API success.
9. **Snapshot + meta** — write `csv/song_mappings_snapshot.csv` (the "git-trackable" view) and update `meta.last_sync_at`.

### SQLite is the source of truth

`data/musicsync.db` (gitignored). Schema in `scripts/db.py:14`:

- `songs(id, canonical_key, name NOT NULL, artist NOT NULL, album, match_source, original_match_source, created_at, updated_at)` — `canonical_key` indexed but **not UNIQUE** (preserves album variants).
- `platform_links(id, song_id, platform, platform_track_id, platform_name, platform_artist, platform_album, liked, synced_at, created_at, updated_at, UNIQUE(platform, platform_track_id))` — UNIQUE prevents the same external track from being claimed by two songs.
- `lyrics_cache` — keyed by `(platform, platform_track_id)`; replaces the old in-memory `state.json` cache.
- `meta(key, value)` — currently holds `last_sync_at`.

CI persists the DB via Actions Cache v4. The `csv/song_mappings_snapshot.csv` written at Step 9 is committed by the workflow when `dry_run=false` and is the bootstrap source if the cache is cold (see `scripts/bootstrap_db_from_snapshot.py`).

### Matching engine (`scripts/matcher.py`)

Two distinct usage scenes:

- **Scene A (Step 3, `l0_canonicalize`)** — we hold a freshly fetched `(platform, track_id)` and want to attach it to a song. Pure local DB query: existing link → reuse; else `canonical_key` lookup → attach to primary version (manual > synced > oldest > smallest id); else create.
- **Scene B (Step 5, `find_match_in_candidates`)** — a song has a one-sided link; we want to find the matching track on the other platform via search API. Runs L1 (ISRC, currently unavailable on QQ) → L2 (lyrics+duration) → L3 (name+artist), with a multi-canonical guard (≥2 candidates with same `canonical_key` → skip rather than guess).

L2 uses character-level `difflib.SequenceMatcher` after `normalize_lyrics()` (strips LRC tags + credit lines + "title - artist" headers). Duration tolerance: 5s for short lyrics (<100 chars), 15s for longer. Lyrics fetches go through `lyrics_cache` to amortize rate-limit cost.

### Two API clients

- `scripts/netease_api.py` — talks **HTTP to `NeteaseCloudMusicApiEnhanced`** (a separate Node.js service started by `start_netease_api.sh`). Auth: `MUSIC_U` cookie preferred, phone+MD5 password fallback.
- `scripts/qqmusic_api_v2.py` — wraps **`qqmusic-api-python`** (async). Auth: `QQMUSIC_KEY` + `QQMUSIC_UIN` + `QQMUSIC_EUIN` cookies extracted from a logged-in browser. No working refresh-token flow.

### State persistence on CI

GitHub Actions has no writable repo — DB survives via **Actions Cache v4**. `.github/workflows/sync.yml` saves `data/musicsync.db` only when `workflow_dispatch && dry_run == 'false'`. Snapshot CSV commit is gated the same way. Scheduled cron runs are read-only by default.

### Rate limits to respect

NetEase search is the chokepoint. Short-window 405 responses → cookie marked → IP banned for 7-10 days. The reverse-sync loop sleeps 30s before the first search and 5s between subsequent searches. Three consecutive HTTPErrors against NetEase trigger a hard abort within `match_unlinked` to avoid extending a ban. CI uses `REVERSE_BATCH=25`. **Don't reduce these sleeps without a strong reason.**

## Conventions specific to this repo

- **`DRY_RUN` defaults to `true`** everywhere — including the scheduled CI runs. To make scheduled runs actually execute, the workflow file would need to change. Don't assume a manual `Run workflow` writes anything unless `dry_run=false` was selected.
- `scripts/sync.py` reconfigures stdout to UTF-8 on Windows (`sys.stdout.reconfigure`) — necessary for printing CJK track names on the default `cp936` console.
- `match_unlinked` returns `{song_id, status}` only. `status` ∈ `{matched, unmatched, conflict_skipped, deferred, aborted_rate_limit}`.
- Tests use temp DBs (`tempfile.mkstemp`); never touch `data/musicsync.db`.

## When making changes

- **Test locally before pushing** (per user preference) — at minimum a dry-run end-to-end. CI failures from rate-limit issues take days to clear.
- **Never trigger CI without an explicit user request.**
- Treat `data/musicsync.db` as production data. Use migration scripts or test DBs; don't hand-edit.
- Track-dict fields from the platform APIs may carry `None` for `name`/`artist`/`album`; normalize with `or ""` before passing to DB helpers (`songs.name/artist` are NOT NULL).
