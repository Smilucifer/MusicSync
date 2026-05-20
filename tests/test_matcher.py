"""Test 4-layer matching engine: L0 canonicalize + L1/L2/L3 + safety nets."""
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
from matcher import (
    clean_name, clean_artist, canonical_key,
    pick_primary_song, l0_canonicalize,
    match_l1, match_l2, match_l3,
    find_match_in_candidates, check_link_conflict,
)


def _temp_db() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


# --- L0 主版本规则 ---

def test_primary_rule_1_prefers_manual():
    """两 song 同 canonical_key，一个 manual → 选 manual。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_manual = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                     album="A", match_source="manual")
            sid_other = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                    album="B", match_source="migrated")
            songs = conn.execute(
                "SELECT * FROM songs WHERE canonical_key='x|y' ORDER BY id"
            ).fetchall()
            chosen = pick_primary_song(conn, songs)
        assert chosen["id"] == sid_manual
        assert sid_other  # silence flake8
    finally:
        os.unlink(db)


def test_primary_rule_2_prefers_synced_link():
    """无 manual；一个 song 有 liked=1+synced_at NOT NULL → 选它。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_a = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="A", match_source="migrated")
            sid_b = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="B", match_source="migrated")
            upsert_platform_link(conn, song_id=sid_b, platform="netease",
                                 platform_track_id="10", liked=1, synced_at=now_iso())
            songs = conn.execute(
                "SELECT * FROM songs WHERE canonical_key='x|y' ORDER BY id"
            ).fetchall()
            chosen = pick_primary_song(conn, songs)
        assert chosen["id"] == sid_b
        assert sid_a
    finally:
        os.unlink(db)


def test_primary_rule_3_prefers_oldest_created_at():
    """无 manual、无 synced_at → 选 created_at 最早。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_a = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="A", match_source="migrated")
            sid_b = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="B", match_source="migrated")
            conn.execute("UPDATE songs SET created_at='2020-01-01' WHERE id=?", (sid_b,))
            conn.execute("UPDATE songs SET created_at='2026-01-01' WHERE id=?", (sid_a,))
            conn.commit()
            songs = conn.execute(
                "SELECT * FROM songs WHERE canonical_key='x|y' ORDER BY created_at, id"
            ).fetchall()
            chosen = pick_primary_song(conn, songs)
        assert chosen["id"] == sid_b
    finally:
        os.unlink(db)


def test_primary_rule_4_tie_breaks_on_id():
    """created_at 相同 → 选 song.id 更小。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_a = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="A", match_source="migrated")
            sid_b = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                                album="B", match_source="migrated")
            ts = now_iso()
            conn.execute("UPDATE songs SET created_at=? WHERE id IN (?, ?)",
                         (ts, sid_a, sid_b))
            conn.commit()
            songs = conn.execute(
                "SELECT * FROM songs WHERE canonical_key='x|y' ORDER BY created_at, id"
            ).fetchall()
            chosen = pick_primary_song(conn, songs)
        assert chosen["id"] == sid_a
    finally:
        os.unlink(db)


# --- L0 canonicalize ---

def test_l0_existing_link_updates_in_place():
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid = upsert_song(conn, canonical_key="x|y", name="X", artist="Y",
                              album="A", match_source="manual")
            upsert_platform_link(conn, song_id=sid, platform="qq",
                                 platform_track_id="100", platform_name="old",
                                 liked=1)
            new_song_id, link_id = l0_canonicalize(
                conn, platform="qq",
                track={"id": "100", "name": "newname", "artist": "Y", "album": "A"},
            )
        assert new_song_id == sid
        with connect(db) as conn:
            link = conn.execute("SELECT * FROM platform_links WHERE id=?", (link_id,)).fetchone()
        assert link["platform_name"] == "newname"
    finally:
        os.unlink(db)


def test_l0_new_track_creates_song():
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid, _ = l0_canonicalize(
                conn, platform="netease",
                track={"id": "999", "name": "Fresh", "artist": "Nobody", "album": "Solo"},
            )
        with connect(db) as conn:
            song = conn.execute("SELECT * FROM songs WHERE id=?", (sid,)).fetchone()
        assert song["match_source"] == "l0_canonical"
        assert song["name"] == "Fresh"
    finally:
        os.unlink(db)


def test_l0_canonical_hit_attaches_to_primary():
    """新 track 命中已存在 canonical_key (manual song) → 挂到那个 song 下，不新建。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_manual = upsert_song(conn, canonical_key="fresh|nobody", name="Fresh",
                                     artist="Nobody", album="Solo", match_source="manual")
            new_song_id, _ = l0_canonicalize(
                conn, platform="netease",
                track={"id": "888", "name": "Fresh", "artist": "Nobody", "album": "Solo"},
            )
        assert new_song_id == sid_manual
    finally:
        os.unlink(db)


def test_l0_canonical_hit_picks_primary_among_multiple():
    """新 track 命中 ≥2 个 canonical_key 的 song → 走 pick_primary_song 选主版本。"""
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_other = upsert_song(conn, canonical_key="fresh|nobody", name="Fresh",
                                    artist="Nobody", album="Album1", match_source="migrated")
            sid_manual = upsert_song(conn, canonical_key="fresh|nobody", name="Fresh",
                                     artist="Nobody", album="Album2", match_source="manual")
            new_song_id, _ = l0_canonicalize(
                conn, platform="netease",
                track={"id": "777", "name": "Fresh", "artist": "Nobody", "album": "Album3"},
            )
        assert new_song_id == sid_manual  # manual takes priority over migrated
        assert sid_other  # silence flake8
    finally:
        os.unlink(db)


# --- L2 短歌词 duration 收紧 ---

def test_l2_long_lyrics_uses_15s_tolerance():
    ne = {"duration": 100, "_lyrics": ("a\n" * 200, "")}
    qq = {"duration": 113, "_lyrics": ("a\n" * 200, "")}
    assert match_l2(ne, qq) is True


def test_l2_short_lyrics_keeps_5s_tolerance():
    short = "唯一一句"
    ne = {"duration": 100, "_lyrics": (short, "")}
    qq = {"duration": 110, "_lyrics": (short, "")}
    assert match_l2(ne, qq) is False
    qq2 = {"duration": 104, "_lyrics": (short, "")}
    assert match_l2(ne, qq2) is True


# --- L3 ambiguity 安全网 ---

def test_l3_with_single_candidate_matches():
    ne = {"name": "Eclipse", "artist": "Aimer"}
    candidates = [
        {"id": "c1", "name": "Eclipse", "artist": "Aimer", "album": "Plenty"},
        {"id": "c2", "name": "DEEP", "artist": "Aimer", "album": "Sun Dance"},
    ]
    matched, level = find_match_in_candidates(ne, candidates, allow_l3=True)
    assert level == "L3"
    assert matched["id"] == "c1"


def test_l3_with_multiple_same_canonical_skips():
    """≥2 候选同 canonical_key → 跳过。"""
    ne = {"name": "Tell me", "artist": "milet"}
    candidates = [
        {"id": "a", "name": "Tell me", "artist": "milet", "album": "Prover"},
        {"id": "b", "name": "Tell me", "artist": "milet", "album": "eyes"},
    ]
    matched, level = find_match_in_candidates(ne, candidates, allow_l3=True)
    assert matched is None
    assert level == ""


# --- Step 5 UNIQUE 冲突预检 ---

def test_check_link_conflict_returns_other_song_id():
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_a = upsert_song(conn, canonical_key="a|x", name="A", artist="x",
                                album=None, match_source="manual")
            sid_b = upsert_song(conn, canonical_key="b|x", name="B", artist="x",
                                album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_b, platform="qq",
                                 platform_track_id="999", liked=1)
            conflict = check_link_conflict(conn, platform="qq",
                                           platform_track_id="999",
                                           current_song_id=sid_a)
        assert conflict == sid_b
    finally:
        os.unlink(db)


def test_check_link_conflict_returns_none_when_own():
    db = _temp_db()
    try:
        init_db(db)
        with connect(db) as conn:
            sid_a = upsert_song(conn, canonical_key="a|x", name="A", artist="x",
                                album=None, match_source="manual")
            upsert_platform_link(conn, song_id=sid_a, platform="qq",
                                 platform_track_id="999", liked=1)
            conflict = check_link_conflict(conn, platform="qq",
                                           platform_track_id="999",
                                           current_song_id=sid_a)
        assert conflict is None
    finally:
        os.unlink(db)


# --- L1/L2 dispatch through find_match_in_candidates ---

def test_find_dispatches_to_l1():
    src = {"isrc": "USRC1", "name": "X", "artist": "Y"}
    candidates = [
        {"id": "wrong", "isrc": "OTHER", "name": "Z", "artist": "Y"},
        {"id": "right", "isrc": "usrc1", "name": "Z", "artist": "Y"},
    ]
    matched, level = find_match_in_candidates(src, candidates)
    assert level == "L1"
    assert matched["id"] == "right"


def test_find_dispatches_to_l2():
    src = {"name": "X", "artist": "Y", "duration": 200,
           "_lyrics": ("a\n" * 200, "")}
    candidates = [
        {"id": "wrong", "name": "Z", "artist": "Y", "duration": 999,
         "_lyrics": ("zzz\n" * 200, "")},
        {"id": "right", "name": "Z", "artist": "Y", "duration": 210,
         "_lyrics": ("a\n" * 200, "")},
    ]
    matched, level = find_match_in_candidates(src, candidates)
    assert level == "L2"
    assert matched["id"] == "right"


# --- L1 / L3 / artist containment 保持 ---

def test_l1_exact_isrc_match():
    assert match_l1({"isrc": "USRC11600001"}, {"isrc": "usrc11600001"}) is True
    assert match_l1({"isrc": ""}, {"isrc": "usrc1"}) is False
    assert match_l1({"isrc": "a"}, {"isrc": "b"}) is False


if __name__ == "__main__":
    test_primary_rule_1_prefers_manual()
    test_primary_rule_2_prefers_synced_link()
    test_primary_rule_3_prefers_oldest_created_at()
    test_primary_rule_4_tie_breaks_on_id()
    test_l0_existing_link_updates_in_place()
    test_l0_new_track_creates_song()
    test_l0_canonical_hit_attaches_to_primary()
    test_l0_canonical_hit_picks_primary_among_multiple()
    test_l2_long_lyrics_uses_15s_tolerance()
    test_l2_short_lyrics_keeps_5s_tolerance()
    test_l3_with_single_candidate_matches()
    test_l3_with_multiple_same_canonical_skips()
    test_check_link_conflict_returns_other_song_id()
    test_check_link_conflict_returns_none_when_own()
    test_find_dispatches_to_l1()
    test_find_dispatches_to_l2()
    test_l1_exact_isrc_match()
    print("ALL OK")
