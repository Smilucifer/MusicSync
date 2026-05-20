"""Test SQLite schema creation and basic helpers."""
import os
import sqlite3
import sys
import io
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from db import (
    init_db, connect,
    upsert_song, upsert_platform_link,
    get_song_by_canonical, get_link, get_links_for_platform,
    set_meta, get_meta,
)


def _temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_init_creates_all_tables():
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            names = [r[0] for r in cur.fetchall()]
        assert "songs" in names
        assert "platform_links" in names
        assert "lyrics_cache" in names
        assert "meta" in names
    finally:
        os.unlink(path)


def test_foreign_keys_enabled():
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            cur = conn.execute("PRAGMA foreign_keys")
            assert cur.fetchone()[0] == 1
    finally:
        os.unlink(path)


def test_wal_mode_enabled():
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            cur = conn.execute("PRAGMA journal_mode")
            assert cur.fetchone()[0].lower() == "wal"
    finally:
        os.unlink(path)


def test_unique_platform_track_id_enforced():
    """A1 structural defense: UNIQUE(platform, platform_track_id) physically blocks dupes."""
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            sid = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                              album="A", match_source="manual")
            upsert_platform_link(conn, song_id=sid, platform="qq",
                                 platform_track_id="100", platform_name="X",
                                 platform_artist="Y", platform_album="A",
                                 liked=1, synced_at=None)
            sid2 = upsert_song(conn, canonical_key="x|y", name="X2", artist="Y",
                               album="B", match_source="manual")
            try:
                conn.execute(
                    "INSERT INTO platform_links "
                    "(song_id, platform, platform_track_id, liked, created_at, updated_at) "
                    "VALUES (?, 'qq', '100', 1, '2026-01-01', '2026-01-01')",
                    (sid2,),
                )
                conn.commit()
                assert False, "expected IntegrityError on duplicate (platform, platform_track_id)"
            except sqlite3.IntegrityError:
                pass
    finally:
        os.unlink(path)


def test_canonical_key_index_allows_duplicates():
    """canonical_key 仅索引，不 UNIQUE — A2 album 变体可保留为多条 song。"""
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            sid1 = upsert_song(conn, canonical_key="tell me|milet", name="Tell me",
                               artist="milet", album="Prover", match_source="l1_isrc")
            sid2 = upsert_song(conn, canonical_key="tell me|milet", name="Tell me",
                               artist="milet", album="eyes", match_source="l1_isrc")
        assert sid1 != sid2
    finally:
        os.unlink(path)


def test_upsert_platform_link_updates_on_conflict():
    """同 (platform, platform_track_id) 二次 upsert 应更新而非抛错。"""
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            sid = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                              album="A", match_source="manual")
            upsert_platform_link(conn, song_id=sid, platform="qq",
                                 platform_track_id="100", platform_name="oldname",
                                 platform_artist="Y", platform_album="A",
                                 liked=1, synced_at=None)
            upsert_platform_link(conn, song_id=sid, platform="qq",
                                 platform_track_id="100", platform_name="newname",
                                 platform_artist="Y", platform_album="A",
                                 liked=0, synced_at="2026-05-21T00:00:00+00:00")
            link = get_link(conn, "qq", "100")
        assert link["platform_name"] == "newname"
        assert link["liked"] == 0
        assert link["synced_at"] == "2026-05-21T00:00:00+00:00"
    finally:
        os.unlink(path)


def test_upsert_platform_link_preserves_synced_at_when_none():
    """COALESCE guarantee: upserting with synced_at=None must NOT overwrite an existing value."""
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            sid = upsert_song(conn, canonical_key="a|b", name="A", artist="B",
                              album="C", match_source="manual")
            # First upsert: set a real synced_at
            upsert_platform_link(conn, song_id=sid, platform="netease",
                                 platform_track_id="999", platform_name="A",
                                 platform_artist="B", platform_album="C",
                                 liked=1, synced_at="2026-05-21T00:00:00+00:00")
            # Second upsert: routine fetch — synced_at is None, must not wipe the stored value
            upsert_platform_link(conn, song_id=sid, platform="netease",
                                 platform_track_id="999", platform_name="A (new name)",
                                 platform_artist="B", platform_album="C",
                                 liked=1, synced_at=None)
            link = get_link(conn, "netease", "999")
        assert link["synced_at"] == "2026-05-21T00:00:00+00:00", (
            f"synced_at was wiped; got {link['synced_at']!r}"
        )
        # Confirm the non-synced_at fields were still updated
        assert link["platform_name"] == "A (new name)"
    finally:
        os.unlink(path)


def test_meta_set_and_get():
    path = _temp_db()
    try:
        init_db(path)
        with connect(path) as conn:
            set_meta(conn, "last_sync_at", "2026-05-21T01:00:00+00:00")
            assert get_meta(conn, "last_sync_at") == "2026-05-21T01:00:00+00:00"
            assert get_meta(conn, "missing") is None
    finally:
        os.unlink(path)


if __name__ == "__main__":
    test_init_creates_all_tables()
    test_foreign_keys_enabled()
    test_wal_mode_enabled()
    test_unique_platform_track_id_enforced()
    test_canonical_key_index_allows_duplicates()
    test_upsert_platform_link_updates_on_conflict()
    test_upsert_platform_link_preserves_synced_at_when_none()
    test_meta_set_and_get()
    print("ALL OK")
