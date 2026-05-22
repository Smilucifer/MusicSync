"""Test merge module: propose_merge + execute_merge."""
import json
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

from db import init_db, connect, upsert_song, upsert_platform_link, now_iso
from merge import propose_merge, execute_merge


def _temp_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(p)
    return p


# --- propose_merge tests ---

def test_propose_merge_recommends_longer_name():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="phoenix|lol", name="Phoenix",
                                  artist="英雄联盟", album="A", match_source="l3_name_artist")
            sid_tgt = upsert_song(conn, canonical_key="凤凰|英雄联盟", name="凤凰 (Phoenix)",
                                  artist="英雄联盟", album="B", match_source="manual")
            preview = propose_merge(conn, sid_src, sid_tgt)
        assert preview["recommended"]["name"] == "凤凰 (Phoenix)"
        assert preview["recommended"]["artist"] == "英雄联盟"
    finally:
        os.unlink(db)


def test_propose_merge_keeps_target_album():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="x|y", name="X",
                                  artist="Y", album="Longer Album Name", match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="x|y", name="X",
                                  artist="Y", album="Short", match_source="manual")
            preview = propose_merge(conn, sid_src, sid_tgt)
        assert preview["recommended"]["album"] == "Short"  # target album kept
    finally:
        os.unlink(db)


def test_propose_merge_warning_on_platform_overlap():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_src, platform="netease",
                                 platform_track_id="ne1", liked=1)
            upsert_platform_link(conn, song_id=sid_tgt, platform="netease",
                                 platform_track_id="ne2", liked=1)
            upsert_platform_link(conn, song_id=sid_src, platform="qq",
                                 platform_track_id="qq1", liked=1)
            preview = propose_merge(conn, sid_src, sid_tgt)
        assert len(preview["warnings"]) > 0
        assert any("UNIQUE" in w or "冲突" in w for w in preview["warnings"])
    finally:
        os.unlink(db)


# --- execute_merge tests ---

def test_execute_merge_atomic_success():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="src|x", name="Src",
                                  artist="x", album="A", match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="tgt|x", name="Tgt",
                                  artist="x", album="B", match_source="manual")
            upsert_platform_link(conn, song_id=sid_src, platform="netease",
                                 platform_track_id="ne1", liked=1)
            upsert_platform_link(conn, song_id=sid_tgt, platform="qq",
                                 platform_track_id="qq1", liked=1)

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="Merged", artist="x", album="B")

        with connect(db) as conn:
            # Target has both links
            links = conn.execute(
                "SELECT * FROM platform_links WHERE song_id=?", (sid_tgt,)
            ).fetchall()
            plats = {l["platform"] for l in links}
            assert plats == {"netease", "qq"}

            # Source soft-deleted
            src = conn.execute("SELECT * FROM songs WHERE id=?", (sid_src,)).fetchone()
            assert src["deleted_at"] is not None

            # merge_log has one row
            log = conn.execute("SELECT * FROM merge_log").fetchall()
            assert len(log) == 1
            assert log[0]["source_song_id"] == sid_src
            assert log[0]["target_song_id"] == sid_tgt
    finally:
        os.unlink(db)


def test_execute_merge_writes_audit_payload():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album="Alb", match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="M", artist="x", album=None)

        with connect(db) as conn:
            log = conn.execute("SELECT * FROM merge_log").fetchone()
            payload = json.loads(log["source_payload"])
            assert payload["id"] == sid_src
            assert payload["name"] == "A"
            assert payload["artist"] == "x"
            assert payload["album"] == "Alb"
            assert payload["match_source"] == "unmatched"
    finally:
        os.unlink(db)


def test_execute_merge_writes_links_snapshot():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_src, platform="netease",
                                 platform_track_id="ne1", liked=1)
            upsert_platform_link(conn, song_id=sid_src, platform="qq",
                                 platform_track_id="qq1", liked=1)

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="M", artist="x", album=None)

        with connect(db) as conn:
            log = conn.execute("SELECT * FROM merge_log").fetchone()
            links = json.loads(log["source_links"])
            assert len(links) == 2
            plats = {l["platform"] for l in links}
            assert plats == {"netease", "qq"}
    finally:
        os.unlink(db)


def test_execute_merge_recomputes_canonical_key():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="old|x", name="Old",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="tgt|x", name="Tgt",
                                  artist="x", album=None, match_source="manual")

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="New Name", artist="New Artist", album=None)

        with connect(db) as conn:
            tgt = conn.execute("SELECT * FROM songs WHERE id=?", (sid_tgt,)).fetchone()
            from matcher import canonical_key
            assert tgt["canonical_key"] == canonical_key("New Name", "New Artist")
    finally:
        os.unlink(db)


def test_execute_merge_sets_match_source_to_manual_merged():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="M", artist="x", album=None)

        with connect(db) as conn:
            tgt = conn.execute("SELECT * FROM songs WHERE id=?", (sid_tgt,)).fetchone()
            assert tgt["match_source"] == "manual_merged"
    finally:
        os.unlink(db)


def test_execute_merge_preserves_target_original_match_source():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual",
                                  original_match_source="l2_lyrics")

        with connect(db) as conn:
            execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                          name="M", artist="x", album=None)

        with connect(db) as conn:
            tgt = conn.execute("SELECT * FROM songs WHERE id=?", (sid_tgt,)).fetchone()
            assert tgt["original_match_source"] == "l2_lyrics"
    finally:
        os.unlink(db)


def test_execute_merge_rejects_self_merge():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid = upsert_song(conn, canonical_key="a|x", name="A",
                              artist="x", album=None, match_source="manual")
        with connect(db) as conn:
            try:
                execute_merge(conn, source_song_id=sid, target_song_id=sid,
                              name="M", artist="x", album=None)
                assert False, "expected ValueError"
            except ValueError as e:
                assert "same" in str(e).lower() or "source" in str(e).lower()
    finally:
        os.unlink(db)


def test_execute_merge_rejects_platform_overlap():
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_src, platform="netease",
                                 platform_track_id="ne1", liked=1)
            upsert_platform_link(conn, song_id=sid_tgt, platform="netease",
                                 platform_track_id="ne2", liked=1)

        with connect(db) as conn:
            try:
                execute_merge(conn, source_song_id=sid_src, target_song_id=sid_tgt,
                              name="M", artist="x", album=None)
                assert False, "expected ValueError for platform overlap"
            except ValueError as e:
                assert "overlap" in str(e).lower() or "UNIQUE" in str(e)
    finally:
        os.unlink(db)


def test_execute_merge_rolls_back_on_error():
    """If execute_merge fails mid-way, the source song and links must be unchanged."""
    db = _temp_db()
    try:
        with connect(db) as conn:
            sid_src = upsert_song(conn, canonical_key="a|x", name="A",
                                  artist="x", album=None, match_source="unmatched")
            sid_tgt = upsert_song(conn, canonical_key="b|x", name="B",
                                  artist="x", album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_src, platform="netease",
                                 platform_track_id="ne1", liked=1)

        # Monkey-patch to simulate failure after step 2
        import merge as _merge
        orig_execute = _merge.execute_merge

        def failing_execute(conn, **kwargs):
            raise sqlite3.OperationalError("simulated disk full")

        _merge.execute_merge = failing_execute
        try:
            with connect(db) as conn:
                try:
                    _merge.execute_merge(conn, source_song_id=sid_src,
                                         target_song_id=sid_tgt,
                                         name="M", artist="x", album=None)
                except sqlite3.OperationalError:
                    pass
        finally:
            _merge.execute_merge = orig_execute

        # Verify nothing changed
        with connect(db) as conn:
            src = conn.execute("SELECT * FROM songs WHERE id=?", (sid_src,)).fetchone()
            assert src["deleted_at"] is None, "source should not be soft-deleted"
            links = conn.execute(
                "SELECT * FROM platform_links WHERE song_id=?", (sid_src,)
            ).fetchall()
            assert len(links) == 1, "source links should be intact"
            log = conn.execute("SELECT * FROM merge_log").fetchall()
            assert len(log) == 0, "merge_log should be empty after rollback"
    finally:
        os.unlink(db)


if __name__ == "__main__":
    test_propose_merge_recommends_longer_name()
    test_propose_merge_keeps_target_album()
    test_propose_merge_warning_on_platform_overlap()
    test_execute_merge_atomic_success()
    test_execute_merge_writes_audit_payload()
    test_execute_merge_writes_links_snapshot()
    test_execute_merge_recomputes_canonical_key()
    test_execute_merge_sets_match_source_to_manual_merged()
    test_execute_merge_preserves_target_original_match_source()
    test_execute_merge_rejects_self_merge()
    test_execute_merge_rejects_platform_overlap()
    test_execute_merge_rolls_back_on_error()
    print("ALL OK")
