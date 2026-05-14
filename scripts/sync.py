"""MusicSync main entry point — orchestrates the 7-step sync pipeline."""
import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from auth_manager import AuthManager
from qqmusic_api import QQMusicAPI
from matcher import match_track, match_l1, match_l2

load_dotenv()

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FORCE_FULL_SYNC = os.getenv("FORCE_FULL_SYNC", "false").lower() == "true"


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
    }


def is_first_run(state: dict) -> bool:
    return not state.get("last_sync")


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

    musickey, uin = auth.get_qq_credentials()
    if not musickey:
        print("  FAIL: QQ Music credentials not available")
        auth.close()
        sys.exit(1)

    qq_api = QQMusicAPI()
    qq_api.set_auth(musickey, uin)

    # --- Step 1: Load State ---
    print("\n[Step 1] Loading sync state...")
    state = load_state()
    if not state or FORCE_FULL_SYNC:
        print("  Fresh state (first run or force_full_sync)")
        state = init_fresh_state()
    else:
        print(f"  Last sync: {state.get('last_sync', 'unknown')}")
        ne_count = len(state.get("netease", {}).get("tracks", []))
        qq_count = len(state.get("qqmusic", {}).get("tracks", []))
        print(f"  NetEase tracks in state: {ne_count}")
        print(f"  QQ Music tracks in state: {qq_count}")

    is_first = is_first_run(state)

    # --- Step 2: Fetch ---
    print("\n[Step 2] Fetching liked tracks...")

    ne_tracks = ne_api.get_all_liked_tracks()
    print(f"  NetEase: {len(ne_tracks)} liked tracks")

    qq_tracks = qq_api.get_all_liked_tracks()
    print(f"  QQ Music: {len(qq_tracks)} liked tracks")

    # --- Step 3: Diff ---
    print("\n[Step 3] Computing diff...")

    # Tracks already synced (have qq_match_id in state)
    already_synced_ids = {
        t["id"] for t in state["netease"]["tracks"]
        if t.get("qq_match_id")
    }

    # New tracks on NetEase (not in state or no match yet)
    new_ne_tracks = [
        t for t in ne_tracks
        if t["id"] not in already_synced_ids
    ]

    print(f"  New/unsynced NetEase tracks: {len(new_ne_tracks)}")
    print(f"  Already synced: {len(already_synced_ids)}")

    if not new_ne_tracks:
        print("  Nothing to sync.")
        now = datetime.now(timezone.utc).isoformat()
        state["last_sync"] = now
        state["netease"]["last_fetch"] = now
        state["netease"]["tracks"] = ne_tracks
        state["qqmusic"]["last_fetch"] = now
        state["qqmusic"]["tracks"] = qq_tracks
        save_state(state)
        write_summary("## MusicSync Complete\nNo new tracks to sync.")
        qq_api.close()
        auth.close()
        sys.exit(0)

    # --- Step 4: Match ---
    print("\n[Step 4] Matching tracks...")

    matched_l1 = []
    matched_l2 = []
    unmatched_new = []

    for ne_track in new_ne_tracks:
        qq_match = None
        level = ""

        # Check if already in QQ Music liked tracks
        for qq_t in qq_tracks:
            if match_l1(ne_track, qq_t):
                qq_match = qq_t
                level = "L1"
                break
            if match_l2(ne_track, qq_t):
                qq_match = qq_t
                level = "L2"
                break

        if qq_match:
            if level == "L1":
                matched_l1.append((ne_track, qq_match))
            else:
                matched_l2.append((ne_track, qq_match))
            continue

        # Search QQ Music
        result = qq_api.search_track(ne_track["name"], ne_track["artist"])
        if not result:
            unmatched_new.append(ne_track)
            continue

        matched_result, matched_level = match_track(ne_track, [result])
        if matched_level:
            if matched_level == "L1":
                matched_l1.append((ne_track, matched_result))
            else:
                matched_l2.append((ne_track, matched_result))
        else:
            unmatched_new.append(ne_track)

    print(f"  L1 matches (auto-execute): {len(matched_l1)}")
    print(f"  L2 matches (dry-run preview): {len(matched_l2)}")
    print(f"  Unmatched: {len(unmatched_new)}")

    # --- Step 5: Execute ---
    print(f"\n[Step 5] {'[DRY-RUN] ' if DRY_RUN else ''}Executing sync operations...")

    executed_l1 = 0
    failed_l1 = []

    for ne_track, qq_track in matched_l1:
        songmid = qq_track.get("mid", "")
        track_name = f"{ne_track['name']} - {ne_track['artist']}"

        if DRY_RUN:
            print(f"  [DRY-RUN] Would add: {track_name} → QQ {qq_track['id']}")
            continue

        success = qq_api.add_to_favorites(songmid)
        if success:
            print(f"  OK: {track_name}")
            executed_l1 += 1
        else:
            print(f"  FAIL: {track_name}")
            failed_l1.append(ne_track)

    # L2: dry-run only in MVP
    if matched_l2:
        print(f"\n  --- L2 matches (DRY-RUN ONLY) ---")
        for ne_track, qq_track in matched_l2:
            print(f"  [L2] {ne_track['name']} - {ne_track['artist']} → QQ {qq_track.get('name', '?')}")

    # Unmatched
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

    # --- Step 6: Save State ---
    print("\n[Step 6] Saving state...")
    now = datetime.now(timezone.utc).isoformat()
    state["last_sync"] = now
    state["netease"]["last_fetch"] = now
    state["netease"]["tracks"] = ne_tracks
    state["qqmusic"]["last_fetch"] = now
    state["qqmusic"]["tracks"] = qq_tracks

    for ne_track, qq_track in matched_l1:
        for t in state["netease"]["tracks"]:
            if t["id"] == ne_track["id"]:
                t["qq_match_id"] = qq_track.get("id", "")
                t["qq_match_confidence"] = "L1"

    save_state(state)

    # --- Summary ---
    summary = f"""## MusicSync {'DRY-RUN' if DRY_RUN else 'Complete'}
**Time:** {now}
**NetEase liked:** {len(ne_tracks)} tracks
**QQ Music liked:** {len(qq_tracks)} tracks
**L1 executed:** {executed_l1}
**L1 failed:** {len(failed_l1)}
**L2 (dry-run only):** {len(matched_l2)}
**Unmatched:** {len(unmatched_new)}
**Dead unmatched:** {dead_count}
"""
    write_summary(summary)

    if DRY_RUN and is_first:
        print("\n*** FIRST RUN — DRY-RUN COMPLETE ***")
        print("Review the preview above. To execute, re-run with dry_run=false")

    qq_api.close()
    auth.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
