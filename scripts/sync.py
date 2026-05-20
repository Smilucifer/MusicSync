"""MusicSync main entry — 10-step pipeline over SQLite.

Pipeline:
  1. Auth check
  2. Fetch both sides
  2.5 Fetch sanity check (skip if first-run or FORCE_FULL_SYNC)
  3. Canonicalize (L0 — UPSERT songs + platform_links)
  4. Diff vs DB
  5. Match unlinked (L1→L2→L3 with UNIQUE conflict skip)
  6. Plan
  7. Safety gate
  8. Execute
  9. Snapshot dump + update meta.last_sync_at
"""
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from auth_manager import AuthManager
from db import (
    init_db, connect, get_db_path,
    upsert_song, upsert_platform_link, set_link_liked,
    get_links_for_platform, get_links_for_song,
    get_meta, set_meta, now_iso,
    get_lyrics, put_lyrics,
    update_match_source,
)
from matcher import (
    l0_canonicalize, find_match_in_candidates, check_link_conflict,
    canonical_key, clean_name, clean_artist,
)

load_dotenv()


DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
FORCE_FULL_SYNC = os.getenv("FORCE_FULL_SYNC", "false").lower() == "true"
REVERSE_BATCH = int(os.getenv("REVERSE_BATCH", "25"))
UNLIKE_ABORT_THRESHOLD = int(os.getenv("UNLIKE_ABORT_THRESHOLD", "10"))
FETCH_DROP_THRESHOLD = float(os.getenv("FETCH_DROP_THRESHOLD", "0.15"))

# NetEase rate limit: 5s between searches; 30s before first search
NETEASE_SEARCH_WARMUP = 30
NETEASE_SEARCH_INTERVAL = 5


def fetch_sanity_check(
    conn: sqlite3.Connection,
    platform: str,
    fetched: list[dict],
    *,
    force_full_sync: bool = False,
) -> Optional[str]:
    """Return abort reason string if fetch suggests data loss, else None."""
    if force_full_sync:
        return None
    if get_meta(conn, "last_sync_at") is None:
        return None
    db_count = conn.execute(
        "SELECT COUNT(*) AS c FROM platform_links WHERE platform=? AND liked=1",
        (platform,),
    ).fetchone()["c"]
    if db_count == 0:
        return None
    drop = (db_count - len(fetched)) / db_count
    if drop > FETCH_DROP_THRESHOLD:
        return (f"fetch_sanity: {platform} fetch={len(fetched)} db_liked={db_count} "
                f"drop={drop:.2%} > threshold {FETCH_DROP_THRESHOLD:.0%}")
    return None


def canonicalize_fetch(conn: sqlite3.Connection, platform: str,
                       tracks: list[dict]) -> set[str]:
    """Step 3: UPSERT songs + platform_links. Returns set of platform_track_ids seen."""
    seen: set[str] = set()
    for t in tracks:
        tid = str(t.get("id", ""))
        if not tid:
            continue
        l0_canonicalize(conn, platform=platform, track=t)
        seen.add(tid)
    return seen


def diff_vs_db(
    conn: sqlite3.Connection,
    ne_seen: set[str],
    qq_seen: set[str],
) -> dict:
    """Return three lists keyed by category:
       single_side_songs: list of (song_id, present_platform, other_platform)
       user_unliked_links: list of (link_id, platform, platform_track_id)
       healthy_pairs: list of song_id (informational)
    """
    single_side: list[tuple] = []
    unliked: list[tuple] = []
    healthy: list[int] = []

    cur = conn.execute("SELECT id FROM songs ORDER BY id")
    for srow in cur.fetchall():
        sid = srow["id"]
        links = get_links_for_song(conn, sid)
        link_by_plat = {ll["platform"]: ll for ll in links if ll["liked"] == 1}
        if "netease" in link_by_plat and "qq" in link_by_plat:
            ne_link = link_by_plat["netease"]
            qq_link = link_by_plat["qq"]
            ne_present = ne_link["platform_track_id"] in ne_seen
            qq_present = qq_link["platform_track_id"] in qq_seen
            if ne_present and qq_present:
                healthy.append(sid)
            elif ne_present and not qq_present:
                unliked.append((qq_link["id"], "qq", qq_link["platform_track_id"]))
                # 反向：用户 unlike QQ → 也要 unlike Ne
                unliked.append((ne_link["id"], "netease", ne_link["platform_track_id"]))
            elif qq_present and not ne_present:
                unliked.append((ne_link["id"], "netease", ne_link["platform_track_id"]))
                unliked.append((qq_link["id"], "qq", qq_link["platform_track_id"]))
            else:
                # 都不在了：用户两侧 unlike
                unliked.append((ne_link["id"], "netease", ne_link["platform_track_id"]))
                unliked.append((qq_link["id"], "qq", qq_link["platform_track_id"]))
        elif "netease" in link_by_plat:
            single_side.append((sid, "netease", "qq"))
        elif "qq" in link_by_plat:
            single_side.append((sid, "qq", "netease"))
    return {"single_side": single_side, "unliked": unliked, "healthy": healthy}


def _fetch_lyrics_cached(conn, api, platform: str, tid: str) -> tuple[str, str]:
    cached = get_lyrics(conn, platform, tid)
    if cached is not None:
        return cached
    orig, trans = api.get_lyric(tid)
    put_lyrics(conn, platform, tid, orig, trans)
    return orig, trans


def match_unlinked(
    conn: sqlite3.Connection,
    ne_api,
    qq_api,
    single_side: list[tuple],
    *,
    reverse_batch: int = 0,
) -> list[dict]:
    """Step 5: for each one-sided song, search the other platform and try L1→L2→L3.

    Returns a list of dicts describing each attempt: {song_id, status, target_platform,
    matched_track_id, level} where status in (matched, unmatched, conflict_skipped).

    On match success: creates the missing platform_link with liked=0/synced_at=NULL
    (Step 8 will flip liked=1 + set synced_at after API ADD succeeds).
    """
    results = []
    ne_search_count = 0
    qq_search_count = 0
    netease_warmed = False

    for sid, present, target in single_side:
        if target == "netease" and reverse_batch and ne_search_count >= reverse_batch:
            results.append({"song_id": sid, "status": "deferred",
                            "target_platform": target})
            continue
        present_link = next(
            (l for l in get_links_for_song(conn, sid)
             if l["platform"] == present and l["liked"] == 1), None,
        )
        if present_link is None:
            continue

        # Build source dict with lyrics
        src_api = ne_api if present == "netease" else qq_api
        src_orig, src_trans = _fetch_lyrics_cached(
            conn, src_api, present, present_link["platform_track_id"],
        )
        source = {
            "id": present_link["platform_track_id"],
            "name": present_link["platform_name"] or "",
            "artist": present_link["platform_artist"] or "",
            "album": present_link["platform_album"] or "",
            "_lyrics": (src_orig, src_trans),
            "duration": 0,  # 我们 DB 不存 duration；现 fetch 也未传 — L2 在缺 duration 时跳过
        }

        # Search target
        target_api = ne_api if target == "netease" else qq_api
        keyword = f"{source['name']} {source['artist']}".strip()
        if target == "netease" and not netease_warmed:
            time.sleep(NETEASE_SEARCH_WARMUP)
            netease_warmed = True
        try:
            candidates = target_api.search(keyword, limit=10)
        except Exception as e:
            print(f"  search failed for sid={sid}: {e}")
            candidates = []
        if target == "netease":
            ne_search_count += 1
            time.sleep(NETEASE_SEARCH_INTERVAL)
        else:
            qq_search_count += 1

        # Enrich candidates with lyrics for L2
        enriched: list[dict] = []
        for c in candidates:
            cid = str(c.get("id", ""))
            c_orig, c_trans = _fetch_lyrics_cached(conn, target_api, target, cid)
            enriched.append({**c, "_lyrics": (c_orig, c_trans), "id": cid})

        match, level = find_match_in_candidates(source, enriched)
        if match is None:
            results.append({"song_id": sid, "status": "unmatched",
                            "target_platform": target})
            update_match_source(conn, sid, "unmatched")
            continue

        # Conflict pre-check
        conflict = check_link_conflict(
            conn, platform=target, platform_track_id=str(match["id"]),
            current_song_id=sid,
        )
        if conflict is not None:
            print(f"  WARN: {target}:{match['id']} already linked to song {conflict}, "
                  f"skipping candidate for song {sid}")
            results.append({"song_id": sid, "status": "conflict_skipped",
                            "target_platform": target, "matched_track_id": str(match["id"])})
            update_match_source(conn, sid, "unmatched")
            continue

        # Create the pending platform_link (liked=0 until Step 8 ADD succeeds)
        upsert_platform_link(
            conn, song_id=sid, platform=target,
            platform_track_id=str(match["id"]),
            platform_name=match.get("name", ""),
            platform_artist=match.get("artist", ""),
            platform_album=match.get("album", ""),
            liked=0, synced_at=None,
        )
        update_match_source(conn, sid, {"L1": "l1_isrc", "L2": "l2_lyrics",
                                        "L3": "l3_name_artist"}[level])
        results.append({"song_id": sid, "status": "matched",
                        "target_platform": target, "matched_track_id": str(match["id"]),
                        "level": level})
    return results


def build_plan(conn: sqlite3.Connection, unliked: list[tuple]) -> dict:
    """Step 6: convert match results + unliked diff into action lists."""
    add_ne: list[str] = []
    add_qq: list[str] = []
    unlike_ne: list[str] = []
    unlike_qq: list[str] = []

    cur = conn.execute(
        "SELECT id, song_id, platform, platform_track_id FROM platform_links "
        "WHERE liked=0 AND synced_at IS NULL"
    )
    for r in cur.fetchall():
        if r["platform"] == "netease":
            add_ne.append(r["platform_track_id"])
        else:
            add_qq.append(r["platform_track_id"])

    for link_id, platform, tid in unliked:
        if platform == "netease":
            unlike_ne.append(tid)
        else:
            unlike_qq.append(tid)
    return {"add_ne": add_ne, "add_qq": add_qq,
            "unlike_ne": unlike_ne, "unlike_qq": unlike_qq}


def execute_plan(
    conn: sqlite3.Connection,
    ne_api, qq_api,
    plan: dict,
    unliked: list[tuple],
    *,
    cleanup_skipped: bool,
) -> dict:
    """Step 8: call APIs, commit per-action."""
    counts = {"add_ne_ok": 0, "add_ne_fail": 0,
              "add_qq_ok": 0, "add_qq_fail": 0,
              "unlike_ne_ok": 0, "unlike_ne_fail": 0,
              "unlike_qq_ok": 0, "unlike_qq_fail": 0}

    for tid in plan["add_ne"]:
        try:
            ok = ne_api.add_to_liked(tid)
        except Exception as e:
            print(f"  add_ne {tid} error: {e}")
            ok = False
        if ok:
            ts = now_iso()
            conn.execute(
                "UPDATE platform_links SET liked=1, synced_at=?, updated_at=? "
                "WHERE platform='netease' AND platform_track_id=?",
                (ts, ts, tid),
            )
            conn.commit()
            counts["add_ne_ok"] += 1
        else:
            counts["add_ne_fail"] += 1

    for tid in plan["add_qq"]:
        try:
            ok = qq_api.add_to_liked(tid)
        except Exception as e:
            print(f"  add_qq {tid} error: {e}")
            ok = False
        if ok:
            ts = now_iso()
            conn.execute(
                "UPDATE platform_links SET liked=1, synced_at=?, updated_at=? "
                "WHERE platform='qq' AND platform_track_id=?",
                (ts, ts, tid),
            )
            conn.commit()
            counts["add_qq_ok"] += 1
        else:
            counts["add_qq_fail"] += 1

    if not cleanup_skipped:
        for link_id, platform, tid in unliked:
            api = ne_api if platform == "netease" else qq_api
            try:
                ok = api.remove_from_liked(tid)
            except Exception as e:
                print(f"  unlike {platform}:{tid} error: {e}")
                ok = False
            if ok:
                conn.execute(
                    "UPDATE platform_links SET liked=0, updated_at=? WHERE id=?",
                    (now_iso(), link_id),
                )
                conn.commit()
                counts[f"unlike_{platform if platform == 'qq' else 'ne'}_ok"] += 1
            else:
                counts[f"unlike_{platform if platform == 'qq' else 'ne'}_fail"] += 1
    return counts


def dump_snapshot(conn: sqlite3.Connection, path: Path) -> int:
    """Step 9: write csv/song_mappings_snapshot.csv. Returns row count."""
    import csv
    rows = []
    cur = conn.execute(
        "SELECT s.id AS song_id, s.canonical_key, s.name, s.artist, s.album, s.match_source "
        "FROM songs s ORDER BY s.id"
    )
    for s in cur.fetchall():
        links = {ll["platform"]: ll for ll in get_links_for_song(conn, s["id"])}
        ne = links.get("netease")
        qq = links.get("qq")
        rows.append({
            "song_id": s["song_id"], "canonical_key": s["canonical_key"],
            "name": s["name"], "artist": s["artist"], "album": s["album"] or "",
            "ne_track_id": ne["platform_track_id"] if ne else "",
            "ne_liked": ne["liked"] if ne else "",
            "ne_synced_at": (ne["synced_at"] or "") if ne else "",
            "qq_track_id": qq["platform_track_id"] if qq else "",
            "qq_liked": qq["liked"] if qq else "",
            "qq_synced_at": (qq["synced_at"] or "") if qq else "",
            "match_source": s["match_source"],
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "song_id", "canonical_key", "name", "artist", "album",
            "ne_track_id", "ne_liked", "ne_synced_at",
            "qq_track_id", "qq_liked", "qq_synced_at", "match_source",
        ], quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def run_pipeline(
    ne_api, qq_api,
    *,
    db_path: str | os.PathLike | None = None,
    dry_run: bool = True,
    reverse_batch: int = 0,
    force_full_sync: bool = False,
) -> dict:
    """Single entry — runnable from tests with fake APIs."""
    # Use local variable — don't mutate module global (avoids test state leakage)
    _force_full_sync = force_full_sync or FORCE_FULL_SYNC

    path = str(db_path) if db_path else str(get_db_path())
    init_db(path)
    summary: dict = {"dry_run": dry_run, "db_path": path}

    with connect(path) as conn:
        # Step 2: fetch
        ne_tracks = ne_api.get_all_liked_tracks()
        qq_tracks = qq_api.get_all_liked_tracks()
        print(f"Fetched NE={len(ne_tracks)} QQ={len(qq_tracks)}")

        # Step 2.5: sanity
        for plat, fetched in (("netease", ne_tracks), ("qq", qq_tracks)):
            reason = fetch_sanity_check(conn, plat, fetched, force_full_sync=_force_full_sync)
            if reason:
                print(f"ABORT: {reason}")
                summary["aborted"] = True
                summary["abort_reason"] = reason
                return summary

        # Step 3: canonicalize
        ne_seen = canonicalize_fetch(conn, "netease", ne_tracks)
        qq_seen = canonicalize_fetch(conn, "qq", qq_tracks)

        # Step 4: diff
        diff = diff_vs_db(conn, ne_seen, qq_seen)

        # Step 5: match unlinked
        match_results = match_unlinked(
            conn, ne_api, qq_api, diff["single_side"], reverse_batch=reverse_batch,
        )
        summary["match_results"] = match_results

        # Step 6: plan
        plan = build_plan(conn, diff["unliked"])
        summary["plan"] = plan
        print(f"Plan: ADD_NE={len(plan['add_ne'])} ADD_QQ={len(plan['add_qq'])} "
              f"UNLIKE_NE={len(plan['unlike_ne'])} UNLIKE_QQ={len(plan['unlike_qq'])}")

        # Compute cleanup_skipped early so dry-run with force_full_sync still reports it
        unlike_count = len(plan["unlike_ne"]) + len(plan["unlike_qq"])
        if _force_full_sync:
            summary["cleanup_skipped"] = True
        elif unlike_count > UNLIKE_ABORT_THRESHOLD:
            summary["cleanup_skipped"] = True

        if dry_run:
            print("DRY_RUN — stopping before Step 7/8.")
            return summary

        # Step 7: safety gate
        cleanup_skipped = summary.get("cleanup_skipped", False)
        if cleanup_skipped:
            if _force_full_sync:
                print(f"FORCE_FULL_SYNC — cleanup skipped entirely")
            else:
                print(f"WARN: unlike count {unlike_count} > {UNLIKE_ABORT_THRESHOLD} — "
                      "cleanup skipped (ADDs still executed)")

        # Step 8: execute
        summary["counts"] = execute_plan(
            conn, ne_api, qq_api, plan, diff["unliked"],
            cleanup_skipped=cleanup_skipped,
        )

        # Step 9: snapshot + meta
        snapshot_path = ROOT / "csv" / "song_mappings_snapshot.csv"
        n = dump_snapshot(conn, snapshot_path)
        print(f"Snapshot: {n} rows → {snapshot_path}")
        set_meta(conn, "last_sync_at", now_iso())

    return summary


def main() -> int:
    auth = AuthManager()
    ne_api = auth.get_netease_api()
    qq_api = auth.get_qq_api()
    if ne_api is None or qq_api is None:
        print("Auth failed — abort.")
        return 1
    if not getattr(ne_api, "_ready", True) or not getattr(qq_api, "_ready", True):
        print("One side not ready — abort.")
        return 1

    print(f"=== MusicSync (DRY_RUN={DRY_RUN}, FORCE_FULL_SYNC={FORCE_FULL_SYNC}, "
          f"REVERSE_BATCH={REVERSE_BATCH}) ===")
    summary = run_pipeline(
        ne_api, qq_api,
        dry_run=DRY_RUN,
        reverse_batch=REVERSE_BATCH,
        force_full_sync=FORCE_FULL_SYNC,
    )
    if summary.get("aborted"):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
