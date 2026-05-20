"""Test one-shot CSV→SQLite migration."""
import os
import sqlite3
import sys
import io
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from migrate_csv_to_db import migrate_csv

FIXTURE = ROOT / "tests" / "fixtures" / "song_mappings_fixture.csv"


def _temp_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_a2_distinct_album_versions_kept_separate():
    """milet/Tell me 在 Prover 和 eyes 是两个 song，因不同 qq_id。"""
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        songs = _rows(db, "SELECT * FROM songs WHERE artist='milet' AND name='Tell me'")
        assert len(songs) == 2, f"expected 2 milet/Tell me songs, got {len(songs)}"
        albums = sorted([s["album"] for s in songs])
        assert albums == ["Prover", "eyes"]
    finally:
        os.unlink(db)


def test_a1_same_qq_id_merged_into_one_song():
    """qq_id=203 出现在 row 104 和 row qq_only — 必须合并为 1 个 song。"""
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        links = _rows(db, "SELECT * FROM platform_links WHERE platform='qq' AND platform_track_id='203'")
        assert len(links) == 1, f"expected 1 link for qq=203, got {len(links)}"
        song_id = links[0]["song_id"]
        ne_links = _rows(db, "SELECT * FROM platform_links WHERE song_id=? AND platform='netease'", (song_id,))
        assert len(ne_links) == 1
        assert ne_links[0]["platform_track_id"] == "104"
    finally:
        os.unlink(db)


def test_b_pure_unmatched_pair_stays_separate():
    """row 105 (ne_only) 与 row qq=204 (qq_only) 不共享 id，按设计不合并。"""
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        ne_link = _rows(db, "SELECT * FROM platform_links WHERE platform='netease' AND platform_track_id='105'")
        qq_link = _rows(db, "SELECT * FROM platform_links WHERE platform='qq' AND platform_track_id='204'")
        assert len(ne_link) == 1
        assert len(qq_link) == 1
        assert ne_link[0]["song_id"] != qq_link[0]["song_id"]
    finally:
        os.unlink(db)


def test_manual_preserved():
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        manual = _rows(db, "SELECT * FROM songs WHERE match_source='manual'")
        assert len(manual) >= 2
        for s in manual:
            assert s["original_match_source"] == "manual"
    finally:
        os.unlink(db)


def test_non_manual_records_original_match_source():
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        migrated = _rows(db, "SELECT * FROM songs WHERE match_source='migrated'")
        assert len(migrated) >= 1
        for s in migrated:
            assert s["original_match_source"] in (
                "name_artist", "unmatched", "qq_only", "isrc"
            )
    finally:
        os.unlink(db)


def test_no_duplicate_platform_track_id():
    """A1 数 = 0 — 即所有 (platform, platform_track_id) UNIQUE。"""
    db = _temp_db()
    try:
        migrate_csv(str(FIXTURE), db)
        rows = _rows(
            db,
            "SELECT platform, platform_track_id, COUNT(*) AS c FROM platform_links "
            "GROUP BY platform, platform_track_id HAVING c>1",
        )
        assert rows == [], f"duplicate links: {rows}"
    finally:
        os.unlink(db)


if __name__ == "__main__":
    test_a2_distinct_album_versions_kept_separate()
    test_a1_same_qq_id_merged_into_one_song()
    test_b_pure_unmatched_pair_stays_separate()
    test_manual_preserved()
    test_non_manual_records_original_match_source()
    test_no_duplicate_platform_track_id()
    print("ALL OK")
