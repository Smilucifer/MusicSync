"""Post-migration sanity check over data/musicsync.db.

Lightweight: link count vs. CSV id count + A1 UNIQUE invariant.
Detailed invariants (manual preserved, pair-mapping, A2 album splits) live
in tests/test_migrate.py. Run after migrate_csv_to_db.py.
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
    if not csv_path.exists() or not db_path.exists():
        print(f"FAIL: csv={csv_path.exists()} db={db_path.exists()}")
        return 1

    csv_ne, csv_qq = 0, 0
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("netease_id") or "").strip(): csv_ne += 1
            if (r.get("qq_id") or "").strip(): csv_qq += 1

    with connect(str(db_path)) as conn:
        db_ne = conn.execute("SELECT COUNT(*) c FROM platform_links WHERE platform='netease'").fetchone()["c"]
        db_qq = conn.execute("SELECT COUNT(*) c FROM platform_links WHERE platform='qq'").fetchone()["c"]
        dup = conn.execute(
            "SELECT COUNT(*) c FROM (SELECT 1 FROM platform_links "
            "GROUP BY platform, platform_track_id HAVING COUNT(*)>1)"
        ).fetchone()["c"]

    if dup:
        print(f"FAIL: {dup} duplicate (platform, track_id) groups")
        return 2
    if db_ne != csv_ne or db_qq != csv_qq:
        print(f"FAIL: ne csv={csv_ne} db={db_ne}; qq csv={csv_qq} db={db_qq}")
        return 2
    print(f"VERIFY OK: ne={db_ne} qq={db_qq}, no dup")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
