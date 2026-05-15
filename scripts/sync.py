"""MusicSync main entry point — orchestrates the 7-step sync pipeline."""
import os
import sys
import json
import time
import random
from datetime import datetime, timezone

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

from auth_manager import AuthManager
from matcher import match_track, match_l1, match_l2, match_l3
from csv_database import (
    load_mappings, lookup_by_ne, lookup_by_qq,
    get_synced_ne_ids, get_pending_manual_ids, needs_match,
    upsert_ne_track, upsert_qq_track,
    record_match, record_unmatched, mark_synced,
    promote_qq_row, get_qq_only_tracks,
    save_mappings, get_csv_path,
)

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FORCE_FULL_SYNC = os.getenv("FORCE_FULL_SYNC", "false").lower() == "true"
REVERSE_BATCH = int(os.getenv("REVERSE_BATCH", "0"))  # per-run limit, 0=unlimited
LYRICS_CACHE_MAX = 1000


def load_state() -> dict:
    """Load sync state from local file (Actions Cache handles CI persistence)."""
    local_path = os.path.join(
        os.path.dirname(__file__), "..", "state", "sync_state.json"
    )
    if os.path.exists(local_path):
        with open(local_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save sync state to local file."""
    state_path = os.path.join(os.path.dirname(__file__), "..", "state")
    os.makedirs(state_path, exist_ok=True)
    state_file = os.path.join(state_path, "sync_state.json")
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def init_fresh_state() -> dict:
    return {
        "last_sync": None,
        "direction": "netease_to_qq",
        "netease": {"last_fetch": None, "tracks": []},
        "qqmusic": {"last_fetch": None, "tracks": []},
        "unmatched": {},
        "tombstones": [],
        "lyrics_cache": {},
    }


def is_first_run(state: dict) -> bool:
    return not state.get("last_sync")


def get_cached_lyrics(state: dict, track_id: str) -> tuple[str, str] | None:
    """Get lyrics from cache, or None if not cached. Returns (original, translated)."""
    cache = state.get("lyrics_cache", {})
    if track_id in cache:
        entry = cache[track_id]
        # Handle legacy string format
        if isinstance(entry, str):
            return entry, ""
        return tuple(entry) if isinstance(entry, list) else entry
    return None


def put_cached_lyrics(state: dict, track_id: str, lyrics: tuple[str, str]):
    """Cache lyrics, evicting oldest if over limit. Skips if both empty or encrypted."""
    original, translated = lyrics
    if not original and not translated:
        return
    # Don't cache encrypted lyrics (hex strings from QRC)
    if original and all(c in '0123456789abcdefABCDEF' for c in original.replace('\n', '').replace(' ', '')):
        return
    cache = state.setdefault("lyrics_cache", {})
    if track_id in cache:
        return
    if len(cache) >= LYRICS_CACHE_MAX:
        oldest_key = next(iter(cache))
        del cache[oldest_key]
    cache[track_id] = list(lyrics)


def fetch_lyrics_with_cache(state: dict, api, track_id: str) -> tuple[str, str]:
    """Fetch lyrics, using cache when available. Returns (original, translated)."""
    cached = get_cached_lyrics(state, track_id)
    if cached is not None:
        return cached
    lyrics = api.get_lyric(track_id)
    put_cached_lyrics(state, track_id, lyrics)
    return lyrics


def write_summary(summary: str):
    """Write to GitHub Actions step summary and stdout."""
    summary_file = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(summary + "\n")
    print(summary)


def main():
    print("=" * 60)
    print(f"MusicSync — {datetime.now(timezone.utc).isoformat()}")
    print(f"DRY_RUN={DRY_RUN}  FORCE_FULL_SYNC={FORCE_FULL_SYNC}")
    print("=" * 60)

    # --- Step 0: Auth Pre-check ---
    print("\n[Step 0] Auth pre-check...")
    auth = AuthManager()
    errors = auth.pre_check()
    if errors:
        for e in errors:
            print(f"  FAIL: {e}")
        write_summary(f"## MusicSync Failed\n**Auth failure:** {'; '.join(errors)}")
        auth.close()
        sys.exit(1)
    print("  OK: Both platforms authenticated")

    ne_api = auth.get_netease_api()
    if not ne_api:
        print("  FAIL: NetEase API not available after login")
        auth.close()
        sys.exit(1)

    qq_api = auth.get_qq_api()
    if not qq_api:
        print("  FAIL: QQ Music API not available after login")
        auth.close()
        sys.exit(1)

    # --- Step 1: Load State ---
    print("\n[Step 1] Loading sync state...")
    state = load_state()
    if not state or FORCE_FULL_SYNC:
        print("  Fresh state (first run or force_full_sync)")
        state = init_fresh_state()

    # Ensure lyrics_cache exists in older state files
    state.setdefault("lyrics_cache", {})

    # Remove encrypted lyrics from cache (hex strings from QRC)
    cache = state["lyrics_cache"]
    encrypted_keys = [
        k for k, v in cache.items()
        if isinstance(v, (list, tuple)) and v[0]
        and all(c in '0123456789abcdefABCDEF' for c in v[0].replace('\n', '').replace(' ', ''))
    ]
    if encrypted_keys:
        for k in encrypted_keys:
            del cache[k]
        print(f"  Cleared {len(encrypted_keys)} encrypted lyrics from cache")

    mappings = load_mappings()
    row_count = sum(1 for k in mappings if k and not k.startswith("_"))
    print(f"  Last sync: {state.get('last_sync', 'unknown')}")
    print(f"  CSV rows: {row_count} (NetEase + QQ union)")
    print(f"  Lyrics cache: {len(state['lyrics_cache'])} entries")

    is_first = is_first_run(state)

    # --- Step 2: Fetch & Update CSV ---
    print("\n[Step 2] Fetching liked tracks...")

    ne_tracks = ne_api.get_all_liked_tracks()
    print(f"  NetEase: {len(ne_tracks)} liked tracks")

    qq_tracks = qq_api.get_all_liked_tracks()
    print(f"  QQ Music: {len(qq_tracks)} liked tracks")

    # Update CSV with any new tracks from either platform
    ne_new_csv = 0
    for t in ne_tracks:
        ne_id = str(t["id"])
        if ne_id not in mappings:
            upsert_ne_track(mappings, t)
            ne_new_csv += 1

    qq_new_csv = 0
    for t in qq_tracks:
        qq_id = str(t["id"])
        if lookup_by_qq(mappings, qq_id) is None:
            upsert_qq_track(mappings, t)
            qq_new_csv += 1

    if ne_new_csv or qq_new_csv:
        print(f"  CSV new: +{ne_new_csv} NetEase, +{qq_new_csv} QQ-only")

    # --- Step 3: Diff ---
    print("\n[Step 3] Computing diff...")

    already_synced_ids = get_synced_ne_ids(mappings)
    pending_manual_ids = get_pending_manual_ids(mappings)

    # Tracks that need processing: brand new + pending (matched but not yet executed)
    needs_processing_ids = set()
    for t in ne_tracks:
        ne_id = str(t["id"])
        if ne_id not in already_synced_ids:
            needs_processing_ids.add(ne_id)

    # Also include tracks needing first-time match
    unmatched_ids = {
        ne_id for ne_id, row in mappings.items()
        if ne_id and not ne_id.startswith("_")
        and needs_match(mappings, ne_id)
    }
    needs_processing_ids |= unmatched_ids

    new_ne_tracks = [t for t in ne_tracks if str(t["id"]) in needs_processing_ids]

    print(f"  Tracks needing processing: {len(new_ne_tracks)}")
    print(f"  Already synced (in CSV): {len(already_synced_ids)}")
    print(f"  Pending (matched, not executed): {len(pending_manual_ids)}")

    if not new_ne_tracks:
        print("  Nothing to sync.")
        now = datetime.now(timezone.utc).isoformat()
        state["last_sync"] = now
        state["netease"]["last_fetch"] = now
        state["qqmusic"]["last_fetch"] = now
        save_state(state)
        if not DRY_RUN:
            save_mappings(mappings)
        write_summary("## MusicSync Complete\nNo new tracks to sync.")
        auth.close()
        sys.exit(0)

    # --- Step 4: Match ---
    print("\n[Step 4] Matching tracks...")

    qq_track_by_id = {t["id"]: t for t in qq_tracks}

    matched_l1 = []     # (ne_track, qq_match, level) — manual + isrc → auto-execute
    matched_l2 = []     # (ne_track, qq_match, level) — lyrics+duration → auto-execute
    matched_l3 = []     # (ne_track, qq_match) — name_artist → dry-run only
    unmatched_new = []
    lyrics_fetched = 0

    for ne_track in new_ne_tracks:
        ne_id = str(ne_track["id"])
        qq_match = None
        level = ""

        # 1) Check CSV for manual mapping (highest priority)
        csv_row = lookup_by_ne(mappings, ne_id)
        if csv_row and csv_row.get("match_source") == "manual":
            csv_qq_id = csv_row.get("qq_id", "")
            if csv_qq_id:
                qq_match = qq_track_by_id.get(csv_qq_id)
                if qq_match:
                    level = "manual"
                else:
                    qq_match = {"id": csv_qq_id}
                    level = "manual"

        # 2) L1 (ISRC) against existing QQ liked tracks
        if not qq_match:
            for qq_t in qq_tracks:
                if match_l1(ne_track, qq_t):
                    qq_match = qq_t
                    level = "isrc"
                    break

        if qq_match:
            matched_l1.append((ne_track, qq_match, level))
            record_match(mappings, ne_id, qq_match, level)
            continue

        # 3) Name+Artist pre-filter against existing QQ liked tracks
        name_artist_candidates = []
        for qq_t in qq_tracks:
            if match_l3(ne_track, qq_t):
                name_artist_candidates.append(qq_t)

        # 4) Search QQ Music if no local candidates
        if not name_artist_candidates:
            results = qq_api.search(f"{ne_track['name']} {ne_track['artist']}", limit=3)
            if results:
                for result in results:
                    if match_l3(ne_track, result):
                        name_artist_candidates.append(result)

        # 5) Verify candidates with lyrics + duration
        if name_artist_candidates:
            ne_lyrics = fetch_lyrics_with_cache(state, ne_api, ne_id)
            ne_dur = ne_track.get("duration", 0)
            lyrics_fetched += 1

            best_match = None
            best_level = None
            for qq_t in name_artist_candidates:
                # Attach lyrics to candidate for match_l2
                qq_lyrics = fetch_lyrics_with_cache(state, qq_api, str(qq_t["id"]))
                qq_t["_lyrics"] = qq_lyrics
                lyrics_fetched += 1
                ne_track["_lyrics"] = ne_lyrics
                ne_track["duration"] = ne_dur

                if match_l2(ne_track, qq_t):
                    best_match = qq_t
                    best_level = "lyrics_duration"
                    break

            if best_match:
                matched_l2.append((ne_track, best_match, best_level))
                record_match(mappings, ne_id, best_match, "name_artist")
            else:
                # L3: name+artist only, dry-run — log why L2 failed
                if name_artist_candidates:
                    qq_t = name_artist_candidates[0]
                    qq_lyrics = fetch_lyrics_with_cache(state, qq_api, str(qq_t["id"]))
                    from matcher import lyrics_similarity, duration_match, normalize_lyrics
                    ne_orig, ne_trans = ne_lyrics
                    qq_orig, qq_trans = qq_lyrics
                    ne_o = normalize_lyrics(ne_orig)
                    qq_o = normalize_lyrics(qq_orig)
                    ne_t = normalize_lyrics(ne_trans)
                    qq_t_norm = normalize_lyrics(qq_trans)
                    sim_oo = lyrics_similarity(ne_o, qq_o)
                    sim_tt = lyrics_similarity(ne_t, qq_t_norm)
                    sim_ot = lyrics_similarity(ne_o, qq_t_norm)
                    sim_to = lyrics_similarity(ne_t, qq_o)
                    dur_ok = duration_match(ne_dur, qq_t.get("duration", 0))
                    print(f"  [L3] {ne_track['name']} - dur_ok={dur_ok} sim_oo={sim_oo:.3f} sim_tt={sim_tt:.3f} sim_ot={sim_ot:.3f} sim_to={sim_to:.3f}")
                    print(f"    ne_orig({len(ne_o)}): {ne_o[:60]!r}")
                    print(f"    qq_orig({len(qq_o)}): {qq_o[:60]!r}")
                    print(f"    ne_trans({len(ne_t)}): {ne_t[:60]!r}")
                    print(f"    qq_trans({len(qq_t_norm)}): {qq_t_norm[:60]!r}")
                matched_l3.append((ne_track, name_artist_candidates[0]))
                record_match(mappings, ne_id, name_artist_candidates[0], "name_artist")
        else:
            unmatched_new.append(ne_track)
            record_unmatched(mappings, ne_id)

    manual_count = sum(1 for m in matched_l1 if m[2] == "manual")
    isrc_count = sum(1 for m in matched_l1 if m[2] == "isrc")
    print(f"  Manual (from CSV): {manual_count}")
    print(f"  ISRC (auto-execute): {isrc_count}")
    print(f"  Lyrics+Duration (auto-execute): {len(matched_l2)}")
    print(f"  Name+Artist only (dry-run): {len(matched_l3)}")
    print(f"  Unmatched: {len(unmatched_new)}")
    print(f"  Lyrics API calls: {lyrics_fetched}")

    # --- Step 5: Execute ---
    print(f"\n[Step 5] {'[DRY-RUN] ' if DRY_RUN else ''}Executing sync operations...")

    executed_l1 = 0
    failed_l1 = []

    # L1: manual + ISRC → auto-execute
    for ne_track, qq_track, _level in matched_l1:
        track_name = f"{ne_track['name']} - {ne_track['artist']}"
        ne_id = str(ne_track["id"])

        if DRY_RUN:
            print(f"  [DRY-RUN] Would add ({_level}): {track_name} → QQ {qq_track['id']}")
            continue

        success = qq_api.add_to_liked(qq_track["id"])
        if success:
            print(f"  OK ({_level}): {track_name}")
            mark_synced(mappings, ne_id)
            executed_l1 += 1
        else:
            print(f"  FAIL ({_level}): {track_name}")
            failed_l1.append(ne_track)

    # L2: lyrics+duration → auto-execute
    executed_l2 = 0
    failed_l2 = []
    for ne_track, qq_track, _level in matched_l2:
        track_name = f"{ne_track['name']} - {ne_track['artist']}"
        ne_id = str(ne_track["id"])

        if DRY_RUN:
            print(f"  [DRY-RUN] Would add ({_level}): {track_name} → QQ {qq_track['id']}")
            continue

        success = qq_api.add_to_liked(qq_track["id"])
        if success:
            print(f"  OK ({_level}): {track_name}")
            mark_synced(mappings, ne_id)
            executed_l2 += 1
        else:
            print(f"  FAIL ({_level}): {track_name}")
            failed_l2.append(ne_track)

    # L3: name+artist only → dry-run
    if matched_l3:
        print(f"\n  --- Name+Artist only matches (DRY-RUN ONLY) ---")
        for ne_track, qq_track in matched_l3:
            print(f"  [name_artist] {ne_track['name']} - {ne_track['artist']} → QQ {qq_track.get('name', '?')}")

    # Unmatched — track attempts in state
    if unmatched_new:
        print(f"\n  --- Unmatched tracks ---")
        for ne_track in unmatched_new:
            print(f"  [UNMATCHED] {ne_track['name']} - {ne_track['artist']}")
            key = f"netease:{ne_track['id']}"
            if key in state["unmatched"]:
                state["unmatched"][key]["attempts"] += 1
                state["unmatched"][key]["last_attempt"] = datetime.now(timezone.utc).isoformat()
            else:
                state["unmatched"][key] = {
                    "name": ne_track["name"],
                    "artist": ne_track["artist"],
                    "attempts": 1,
                    "last_attempt": datetime.now(timezone.utc).isoformat(),
                    "failure_reason": "not_found",
                    "dead": False,
                }

    # Mark dead unmatched
    dead_count = 0
    for key, entry in state["unmatched"].items():
        if entry["attempts"] >= 3 and not entry["dead"]:
            entry["dead"] = True
            dead_count += 1
    if dead_count:
        print(f"  Marked {dead_count} unmatched tracks as dead (3+ failures)")

    # --- Step 6: Save CSV ---
    print("\n[Step 6] Saving CSV...")
    now = datetime.now(timezone.utc).isoformat()

    # Save previous track IDs BEFORE overwriting (needed for Step 8 diff)
    prev_ne_ids = {t["id"] for t in state.get("netease", {}).get("tracks", [])}
    prev_qq_ids = {t["id"] for t in state.get("qqmusic", {}).get("tracks", [])}

    # Write CSV only when not DRY_RUN (matches are confirmed)
    if not DRY_RUN:
        save_mappings(mappings)
        print(f"  CSV saved: {get_csv_path()}")
    else:
        print(f"  CSV NOT saved (DRY_RUN — run with DRY_RUN=false to persist)")

    # --- Step 7: Reverse sync (QQ → NetEase) ---
    print("\n[Step 7] Reverse sync: QQ → NetEase...")
    # Longer cooldown — NetEase search API aggressively rate-limits
    time.sleep(30)

    qq_only = get_qq_only_tracks(mappings)
    rev_executed = 0
    rev_failed = []
    rev_skipped = 0
    rev_matched_count = 0

    # Build set of already-liked NetEase IDs to skip redundant adds
    ne_liked_ids = {str(t["id"]) for t in ne_tracks}

    print(f"  QQ-only tracks in CSV: {len(qq_only)}")

    if not qq_only:
        print("  No QQ-only tracks to process.")
    else:
        # Shuffle to avoid blocking on impossible matches every run
        random.shuffle(qq_only)

        if REVERSE_BATCH > 0 and len(qq_only) > REVERSE_BATCH:
            print(f"  Limiting to {REVERSE_BATCH} tracks (REVERSE_BATCH set)")
            qq_only = qq_only[:REVERSE_BATCH]

        rev_matched = []   # (qq_key, ne_track, level)
        rev_unmatched = []

        for qq_key, qq_row in qq_only:
            qq_name = qq_row.get("qq_name", qq_row.get("name", ""))
            qq_artist = qq_row.get("qq_artist", qq_row.get("artist", ""))
            qq_id = qq_row.get("qq_id", "")

            # Build a qq_track dict for matcher
            qq_track = {"id": qq_id, "name": qq_name, "artist": qq_artist}

            ne_match = None
            level = ""

            # 1) Try L1 against existing NetEase liked tracks
            for ne_t in ne_tracks:
                if match_l1(ne_t, qq_track):
                    ne_match = ne_t
                    level = "isrc"
                    break

            # 2) Search NetEase if no match in liked tracks
            if not ne_match:
                time.sleep(5)  # rate-limit: avoid 405 errors
                try:
                    results = ne_api.search(f"{qq_name} {qq_artist}", limit=3)
                    if results:
                        # Try L1 first
                        for r in results:
                            if match_l1(qq_track, r):
                                ne_match = r
                                level = "isrc"
                                break
                        # Then try lyrics+duration
                        if not ne_match:
                            qq_lyrics = fetch_lyrics_with_cache(state, qq_api, qq_id)
                            qq_track["_lyrics"] = qq_lyrics
                            for r in results:
                                ne_lyrics = fetch_lyrics_with_cache(state, ne_api, str(r["id"]))
                                r["_lyrics"] = ne_lyrics
                                if match_l2(qq_track, r):
                                    ne_match = r
                                    level = "lyrics_duration"
                                    break
                        # Finally try name+artist
                        if not ne_match:
                            best, best_lv = match_track(qq_track, results)
                            if best_lv:
                                ne_match = best
                                level = "name_artist"
                except Exception:
                    pass

            if ne_match and level:
                rev_matched.append((qq_key, ne_match, level))
            else:
                rev_unmatched.append((qq_key, qq_track))

        isrc_count = sum(1 for m in rev_matched if m[2] == "isrc")
        ld_count = sum(1 for m in rev_matched if m[2] == "lyrics_duration")
        na_count = sum(1 for m in rev_matched if m[2] == "name_artist")
        rev_matched_count = len(rev_matched)
        print(f"  ISRC matches: {isrc_count}")
        print(f"  Lyrics+Duration matches: {ld_count}")
        print(f"  Name+Artist matches: {na_count}")
        print(f"  No match: {len(rev_unmatched)}")

        # Execute reverse sync
        for qq_key, ne_match, _level in rev_matched:
            track_name = f"{ne_match['name']} - {ne_match['artist']}"

            if DRY_RUN:
                print(f"  [DRY-RUN] Would add ({_level}): {track_name} → NetEase {ne_match['id']}")
                continue

            # Skip if already liked on NetEase
            if str(ne_match["id"]) in ne_liked_ids:
                print(f"  SKIP already liked ({_level}): {track_name}")
                promote_qq_row(mappings, qq_key, ne_match, _level)
                mark_synced(mappings, str(ne_match["id"]))
                rev_skipped += 1
                continue

            success = ne_api.add_to_liked(ne_match["id"])
            if success:
                print(f"  OK ({_level}): {track_name}")
                promote_qq_row(mappings, qq_key, ne_match, _level)
                mark_synced(mappings, str(ne_match["id"]))
                rev_executed += 1
            else:
                print(f"  FAIL ({_level}): {track_name}")
                rev_failed.append(ne_match)

        if rev_unmatched:
            print(f"\n  --- Could not match on NetEase ---")
            for qq_key, qq_track in rev_unmatched:
                print(f"  [SKIP] {qq_track['name']} - {qq_track['artist']}")

        if not DRY_RUN and (rev_executed or rev_skipped):
            save_mappings(mappings)

    # --- Step 8: Cleanup removed tracks ---
    print("\n[Step 8] Cleanup: removing unliked tracks from the other platform...")

    # Current track IDs (prev_ne_ids/prev_qq_ids saved before Step 6 overwrote state)
    cur_ne_ids = {str(t["id"]) for t in ne_tracks}
    cur_qq_ids = {str(t["id"]) for t in qq_tracks}

    print(f"  Baseline (prev): NetEase {len(prev_ne_ids)} / QQ {len(prev_qq_ids)}")
    print(f"  Current:         NetEase {len(cur_ne_ids)} / QQ {len(cur_qq_ids)}")

    # Find removed tracks
    removed_ne_ids = prev_ne_ids - cur_ne_ids
    removed_qq_ids = prev_qq_ids - cur_qq_ids

    print(f"  NetEase unliked: {len(removed_ne_ids)} tracks")
    print(f"  QQ Music unliked: {len(removed_qq_ids)} tracks")

    # Remove from QQ Music (tracks removed from NetEase)
    ne_removed_executed = 0
    ne_removed_failed = []
    for ne_id in removed_ne_ids:
        csv_row = lookup_by_ne(mappings, ne_id)
        if csv_row and csv_row.get("qq_id"):
            qq_id = csv_row["qq_id"]
            track_name = f"{csv_row.get('name', ne_id)} - {csv_row.get('artist', '')}"

            if DRY_RUN:
                print(f"  [DRY-RUN] Would remove: {track_name} → QQ {qq_id}")
                continue

            success = qq_api.remove_from_liked(qq_id)
            if success:
                print(f"  OK removed: {track_name}")
                ne_removed_executed += 1
            else:
                print(f"  FAIL remove: {track_name}")
                ne_removed_failed.append(ne_id)

    # Remove from NetEase (tracks removed from QQ Music)
    qq_removed_executed = 0
    qq_removed_failed = []
    for qq_id in removed_qq_ids:
        ne_id = lookup_by_qq(mappings, qq_id)
        if ne_id:
            # lookup_by_qq returns netease_id string; get track name from mappings
            ne_row = lookup_by_ne(mappings, ne_id)
            track_name = f"{ne_row.get('name', qq_id) if ne_row else qq_id} - {ne_row.get('artist', '') if ne_row else ''}"

            if DRY_RUN:
                print(f"  [DRY-RUN] Would remove: {track_name} → NetEase {ne_id}")
                continue

            success = ne_api.remove_from_liked(ne_id)
            if success:
                print(f"  OK removed: {track_name}")
                qq_removed_executed += 1
            else:
                print(f"  FAIL remove: {track_name}")
                qq_removed_failed.append(qq_id)

    print(f"  NetEase→QQ removed: {ne_removed_executed} executed / {len(ne_removed_failed)} failed")
    print(f"  QQ→NetEase removed: {qq_removed_executed} executed / {len(qq_removed_failed)} failed")

    # --- Save state (AFTER Step 8 so crash won't lose unliked detection) ---
    now = datetime.now(timezone.utc).isoformat()
    state["last_sync"] = now
    state["netease"]["last_fetch"] = now
    state["qqmusic"]["last_fetch"] = now
    state["netease"]["tracks"] = [{"id": t["id"]} for t in ne_tracks]
    state["qqmusic"]["tracks"] = [{"id": t["id"]} for t in qq_tracks]
    save_state(state)
    print(f"\n  State saved: {now}")
    print(f"  Lyrics cache: {len(state['lyrics_cache'])} entries")

    # --- Summary ---
    csv_path = os.path.join(os.path.dirname(__file__), "..", "csv", "song_mappings.csv")
    summary = f"""## MusicSync {'DRY-RUN' if DRY_RUN else 'Complete'}
**Time:** {now}
**NetEase liked:** {len(ne_tracks)} tracks
**QQ Music liked:** {len(qq_tracks)} tracks
**Forward (Ne→QQ):** {executed_l1 + executed_l2} executed ({isrc_count} ISRC + {len(matched_l2)} lyrics) / {len(failed_l1) + len(failed_l2)} failed / {len(matched_l3)} dry-run / {len(unmatched_new)} unmatched
**Reverse (QQ→Ne):** {rev_executed} executed / {rev_skipped} already liked / {len(rev_failed)} failed / {rev_matched_count} matched
**Unliked cleanup:** {ne_removed_executed} Ne→QQ / {qq_removed_executed} QQ→Ne
**Dead unmatched:** {dead_count}

**CSV database:** `{csv_path}` — edit `match_source` to `manual` and fill `qq_id` to map unmatched tracks.
"""
    write_summary(summary)

    if DRY_RUN and is_first:
        print("\n*** FIRST RUN — DRY-RUN COMPLETE ***")
        print("Review the preview above. To execute, re-run with dry_run=false")

    auth.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
