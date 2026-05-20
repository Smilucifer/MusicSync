"""Bootstrap data/musicsync.db from csv/song_mappings_snapshot.csv on CI cache miss.

Snapshot format (one line per cross-platform link pair, see sync.py snapshot dumper):
  song_id,canonical_key,name,artist,album,
    ne_track_id,ne_liked,ne_synced_at,
    qq_track_id,qq_liked,qq_synced_at,
    match_source

Bootstrap is intentionally simpler than full migration: no union-find,
no original_match_source (snapshot doesn't carry it).
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from db import init_db, connect, now_iso


def bootstrap(snapshot_path: str, db_path: str) -> dict:
    init_db(db_path)
    summary = {"songs": 0, "links": 0}
    with open(snapshot_path, "r", encoding="utf-8-sig", newline="") as f, \
            connect(db_path) as conn:
        for r in csv.DictReader(f):
            song_id = int(r["song_id"])
            ts = now_iso()
            conn.execute(
                "INSERT OR REPLACE INTO songs "
                "(id, canonical_key, name, artist, album, match_source, original_match_source, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (song_id, r["canonical_key"], r["name"], r["artist"],
                 r.get("album") or None, r.get("match_source") or "migrated", ts, ts),
            )
            summary["songs"] += 1
            for platform, id_col, liked_col, synced_col, name_col, artist_col in (
                ("netease", "ne_track_id", "ne_liked", "ne_synced_at", "name", "artist"),
                ("qq", "qq_track_id", "qq_liked", "qq_synced_at", "name", "artist"),
            ):
                tid = (r.get(id_col) or "").strip()
                if not tid:
                    continue
                liked_raw = (r.get(liked_col) or "1").strip()
                liked = 1 if liked_raw in ("1", "true", "True") else 0
                synced_at = (r.get(synced_col) or "").strip() or None
                conn.execute(
                    "INSERT OR REPLACE INTO platform_links "
                    "(song_id, platform, platform_track_id, platform_name, platform_artist, "
                    "platform_album, liked, synced_at, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (song_id, platform, tid, r.get(name_col) or "", r.get(artist_col) or "",
                     r.get("album") or None, liked, synced_at, ts, ts),
                )
                summary["links"] += 1
        conn.commit()
    return summary


def main(argv: list[str]) -> int:
    snapshot = argv[1] if len(argv) > 1 else str(ROOT / "csv" / "song_mappings_snapshot.csv")
    db_path = argv[2] if len(argv) > 2 else str(ROOT / "data" / "musicsync.db")
    if not Path(snapshot).exists():
        print(f"FAIL: snapshot {snapshot} not found — first-time deploy must run migrate_csv_to_db.py locally first")
        return 1
    summary = bootstrap(snapshot, db_path)
    print(f"Bootstrapped from snapshot: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
