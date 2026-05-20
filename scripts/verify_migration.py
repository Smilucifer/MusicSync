"""Post-migration assertions over data/musicsync.db.

Run after migrate_csv_to_db.py. Aborts with non-zero exit on any inconsistency.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from db import connect, get_db_path


def main(argv: list[str]) -> int:
    csv_path = Path(argv[1]) if len(argv) > 1 else (ROOT / "csv" / "song_mappings.csv")
    db_path = Path(argv[2]) if len(argv) > 2 else get_db_path()

    if not csv_path.exists():
        print(f"FAIL: csv not found at {csv_path}")
        return 1
    if not db_path.exists():
        print(f"FAIL: db not found at {db_path}")
        return 1

    errors: list[str] = []
    manual_pairs: list[tuple[str, str]] = []
    pairs: list[tuple[str, str]] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            ne = (r.get("netease_id") or "").strip()
            qq = (r.get("qq_id") or "").strip()
            pairs.append((ne, qq))
            if r.get("match_source") == "manual":
                manual_pairs.append((ne, qq))

    with connect(str(db_path)) as conn:
        # 1. 所有原 CSV (ne, qq) 对都映射到 db 里的某个 song
        for ne, qq in pairs:
            if not ne and not qq:
                continue
            if ne:
                row = conn.execute(
                    "SELECT song_id FROM platform_links WHERE platform='netease' AND platform_track_id=?",
                    (ne,),
                ).fetchone()
                if row is None:
                    errors.append(f"missing netease link for ne={ne}")
                    continue
                ne_song = row["song_id"]
            else:
                ne_song = None
            if qq:
                row = conn.execute(
                    "SELECT song_id FROM platform_links WHERE platform='qq' AND platform_track_id=?",
                    (qq,),
                ).fetchone()
                if row is None:
                    errors.append(f"missing qq link for qq={qq}")
                    continue
                qq_song = row["song_id"]
            else:
                qq_song = None
            if ne_song is not None and qq_song is not None and ne_song != qq_song:
                errors.append(f"({ne},{qq}) split across songs {ne_song}≠{qq_song}")

        # 2. 所有 manual CSV 行的 song 在 db 中标记为 manual
        for ne, qq in manual_pairs:
            target = ne or qq
            plat = "netease" if ne else "qq"
            row = conn.execute(
                "SELECT s.match_source FROM songs s JOIN platform_links pl ON pl.song_id=s.id "
                "WHERE pl.platform=? AND pl.platform_track_id=?",
                (plat, target),
            ).fetchone()
            if row is None or row["match_source"] != "manual":
                errors.append(f"manual lost for ({ne},{qq})")

        # 3. A1 = 0
        dup = conn.execute(
            "SELECT platform, platform_track_id, COUNT(*) AS c FROM platform_links "
            "GROUP BY platform, platform_track_id HAVING c>1"
        ).fetchall()
        if dup:
            errors.append(f"duplicate links: {[dict(r) for r in dup]}")

    if errors:
        print("VERIFY FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 2
    print(f"VERIFY OK: {len(pairs)} CSV rows accounted for; {len(manual_pairs)} manual preserved.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
