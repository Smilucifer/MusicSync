"""Test sync.py pipeline plan construction with mocked APIs."""
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

from db import init_db, connect, upsert_song, upsert_platform_link, set_meta, now_iso
import sync


class FakeAPI:
    def __init__(self, tracks: list[dict], search_results: dict | None = None):
        self._tracks = tracks
        self._search_results = search_results or {}
        self.adds: list[str] = []
        self.removes: list[str] = []

    def get_all_liked_tracks(self) -> list[dict]:
        return self._tracks

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        return self._search_results.get(keyword, [])

    def add_to_liked(self, track_id: str) -> bool:
        self.adds.append(track_id)
        return True

    def remove_from_liked(self, track_id: str) -> bool:
        self.removes.append(track_id)
        return True

    def get_lyric(self, track_id: str) -> tuple[str, str]:
        return ("", "")


def _temp_db_with_one_pair() -> str:
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    init_db(p)
    with connect(p) as conn:
        sid = upsert_song(conn, canonical_key="hello|world", name="Hello",
                          artist="World", album="A", match_source="manual")
        upsert_platform_link(conn, song_id=sid, platform="netease",
                             platform_track_id="ne1", liked=1, synced_at=now_iso())
        upsert_platform_link(conn, song_id=sid, platform="qq",
                             platform_track_id="qq1", liked=1, synced_at=now_iso())
        set_meta(conn, "last_sync_at", "2026-05-20T00:00:00+00:00")
    return p


def test_plan_steady_state_yields_nothing():
    db = _temp_db_with_one_pair()
    try:
        ne = FakeAPI([{"id": "ne1", "name": "Hello", "artist": "World", "album": "A"}])
        qq = FakeAPI([{"id": "qq1", "name": "Hello", "artist": "World", "album": "A"}])
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True)
        assert result["plan"]["add_ne"] == []
        assert result["plan"]["add_qq"] == []
        assert result["plan"]["unlike_ne"] == []
        assert result["plan"]["unlike_qq"] == []
    finally:
        os.unlink(db)


def test_plan_unlike_detected_when_track_missing():
    db = _temp_db_with_one_pair()
    try:
        ne = FakeAPI([{"id": "ne1", "name": "Hello", "artist": "World", "album": "A"}])
        qq = FakeAPI([])  # 用户在 QQ 端 unlike
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True,
                                   force_full_sync=True)
        # 用户在 QQ 端 unlike → 应反向 unlike ne1
        assert "ne1" in result["plan"]["unlike_ne"]
    finally:
        os.unlink(db)


def test_fetch_sanity_check_aborts_on_large_drop():
    """fetch 数比 DB 少 >FETCH_DROP_THRESHOLD → abort。"""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db)
        with connect(db) as conn:
            for i in range(20):
                sid = upsert_song(conn, canonical_key=f"s{i}|x", name=f"S{i}",
                                  artist="x", album=None, match_source="manual")
                upsert_platform_link(conn, song_id=sid, platform="qq",
                                     platform_track_id=f"q{i}", liked=1)
            set_meta(conn, "last_sync_at", "2026-05-20T00:00:00+00:00")

        ne = FakeAPI([])
        # 20 → 10 = 50% 下降，> 15% 阈值
        qq = FakeAPI([{"id": f"q{i}", "name": f"S{i}", "artist": "x", "album": "A"}
                      for i in range(10)])
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True)
        assert result.get("aborted") is True
        assert "fetch_sanity" in result.get("abort_reason", "")
    finally:
        os.unlink(db)


def test_fetch_sanity_skipped_on_first_run():
    """meta.last_sync_at 为空 → Step 2.5 跳过 sanity 检查。"""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db)
        with connect(db) as conn:
            for i in range(20):
                sid = upsert_song(conn, canonical_key=f"s{i}|x", name=f"S{i}",
                                  artist="x", album=None, match_source="manual")
                upsert_platform_link(conn, song_id=sid, platform="qq",
                                     platform_track_id=f"q{i}", liked=1)
            # 注意：不写 last_sync_at
        ne = FakeAPI([])
        qq = FakeAPI([{"id": "q0", "name": "S0", "artist": "x", "album": "A"}])
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True)
        assert result.get("aborted") is not True
    finally:
        os.unlink(db)


def test_unlike_safety_gate_skips_cleanup_when_over_threshold():
    """unlike_ne + unlike_qq > UNLIKE_ABORT_THRESHOLD → 跳过 cleanup 但 ADD 继续。"""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        init_db(db)
        with connect(db) as conn:
            for i in range(15):
                sid = upsert_song(conn, canonical_key=f"s{i}|x", name=f"S{i}",
                                  artist="x", album=None, match_source="manual")
                upsert_platform_link(conn, song_id=sid, platform="netease",
                                     platform_track_id=f"n{i}", liked=1)
                upsert_platform_link(conn, song_id=sid, platform="qq",
                                     platform_track_id=f"q{i}", liked=1)
            set_meta(conn, "last_sync_at", "2026-05-20T00:00:00+00:00")
        # fetch 全空 → 15 个 unlike_ne + 15 个 unlike_qq = 30，远超阈值 10
        # 但因 fetch sanity 优先 abort，需要绕过 sanity：设大量歌但 fetch 比 DB 少不到 15%
        # 改用 FORCE_FULL_SYNC=True 跳过 sanity 但保留 safety gate
        ne = FakeAPI([])
        qq = FakeAPI([])
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True,
                                   force_full_sync=True)
        # cleanup 被 safety gate 跳过：unlike list 在 plan 中存在但 executed 数为 0
        assert len(result["plan"]["unlike_ne"]) + len(result["plan"]["unlike_qq"]) > 10
        assert result.get("cleanup_skipped") is True
    finally:
        os.unlink(db)


def test_match_unlinked_and_execute_creates_add_action():
    """Step 5→6→8 happy path: single-side song matches via L3, ADD fires, FakeAPI records it."""
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fd2, snap = tempfile.mkstemp(suffix=".csv")
    os.close(fd2)
    try:
        init_db(db)
        with connect(db) as conn:
            sid = upsert_song(conn, canonical_key="solo|artist",
                              name="Solo", artist="Artist", album=None,
                              match_source="manual")
            upsert_platform_link(conn, song_id=sid, platform="netease",
                                 platform_track_id="ne1",
                                 platform_name="Solo", platform_artist="Artist",
                                 platform_album="", liked=1, synced_at=now_iso())
            set_meta(conn, "last_sync_at", "2026-05-20T00:00:00+00:00")

        ne = FakeAPI([{"id": "ne1", "name": "Solo", "artist": "Artist", "album": ""}])
        qq = FakeAPI(
            tracks=[],
            search_results={
                "Solo Artist": [{"id": "qq_new", "name": "Solo", "artist": "Artist", "album": ""}],
            },
        )
        result = sync.run_pipeline(
            ne, qq, db_path=db, dry_run=False,
            force_full_sync=True,
            snapshot_path=snap,
        )
        assert "qq_new" in qq.adds, f"expected qq_new in qq.adds, got {qq.adds}"
        assert result.get("cleanup_skipped") is True
    finally:
        os.unlink(db)
        if os.path.exists(snap):
            os.unlink(snap)


def test_netease_3_strike_aborts_match_unlinked():
    """3 consecutive NetEase HTTPError in match_unlinked → abort the loop, don't keep hammering."""
    import requests as _requests

    class FlakyNeAPI(FakeAPI):
        def __init__(self):
            super().__init__(tracks=[])
            self.search_calls = 0

        def search(self, keyword, limit=10):
            self.search_calls += 1
            resp = _requests.Response()
            resp.status_code = 405
            raise _requests.HTTPError("405", response=resp)

    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    fd2, snap = tempfile.mkstemp(suffix=".csv")
    os.close(fd2)
    try:
        init_db(db)
        with connect(db) as conn:
            # 5 QQ-only songs → 5 NetEase search attempts queued
            for i in range(5):
                sid = upsert_song(conn, canonical_key=f"s{i}|x", name=f"S{i}",
                                  artist="x", album=None, match_source="manual")
                upsert_platform_link(conn, song_id=sid, platform="qq",
                                     platform_track_id=f"q{i}",
                                     platform_name=f"S{i}", platform_artist="x",
                                     platform_album="", liked=1, synced_at=now_iso())
            set_meta(conn, "last_sync_at", "2026-05-20T00:00:00+00:00")

        ne = FlakyNeAPI()
        qq = FakeAPI([{"id": f"q{i}", "name": f"S{i}", "artist": "x", "album": ""}
                      for i in range(5)])
        # Patch warmup + interval so the test doesn't sleep 30+s
        orig_warmup = sync.NETEASE_SEARCH_WARMUP
        orig_interval = sync.NETEASE_SEARCH_INTERVAL
        sync.NETEASE_SEARCH_WARMUP = 0
        sync.NETEASE_SEARCH_INTERVAL = 0
        try:
            result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True,
                                       force_full_sync=True, snapshot_path=snap)
        finally:
            sync.NETEASE_SEARCH_WARMUP = orig_warmup
            sync.NETEASE_SEARCH_INTERVAL = orig_interval

        # Should stop searching at strike 3, not all 5
        assert ne.search_calls == 3, f"expected 3 strikes then abort, got {ne.search_calls}"
        assert any(r.get("status") == "aborted_rate_limit"
                   for r in result.get("match_results", []))
    finally:
        os.unlink(db)
        if os.path.exists(snap):
            os.unlink(snap)


if __name__ == "__main__":
    test_plan_steady_state_yields_nothing()
    test_plan_unlike_detected_when_track_missing()
    test_fetch_sanity_check_aborts_on_large_drop()
    test_fetch_sanity_skipped_on_first_run()
    test_unlike_safety_gate_skips_cleanup_when_over_threshold()
    test_match_unlinked_and_execute_creates_add_action()  # NEW
    test_netease_3_strike_aborts_match_unlinked()
    print("ALL OK")
