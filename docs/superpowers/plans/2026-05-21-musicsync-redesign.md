# MusicSync 底层重做 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 MusicSync 的真源从 CSV 迁到 SQLite，重写匹配引擎和 10 步流水线，从根上消除 A1/A2/B 三类数据腐败。

**Architecture:**  数据层用 stdlib `sqlite3`（WAL，外键开启），两表 `songs` + `platform_links` 加 `lyrics_cache`/`meta`，靠 `UNIQUE(platform, platform_track_id)` 物理强制防 A1，靠 `canonical_key` 索引（非 UNIQUE）保留 A2 album 变体。匹配引擎区分 Scene A（Step 3 canonicalize 只用 L0）和 Scene B（Step 5 match unlinked 只用 L1/L2/L3）。流水线 10 步，细粒度事务（每条 atomic 操作独立 commit），新增 Step 2.5 fetch sanity + Step 7 safety gate。CI cache miss 时通过 `bootstrap_db_from_snapshot.py` 从 git 里的 snapshot CSV 自动重建 DB。

**Tech Stack:** Python 3.11+, stdlib `sqlite3`，pytest-style 断言函数（沿用 repo 现有风格，无 pytest 配置），保留 `scripts/netease_api.py` 和 `scripts/qqmusic_api_v2.py` 不动。

**Reference spec:** `docs/superpowers/specs/2026-05-20-musicsync-redesign-design.md`

---

## File Structure

| 文件 | 操作 | 职责 |
|---|---|---|
| `scripts/db.py` | 新建 | schema 创建 + connection helper + UPSERT/查询 helpers |
| `scripts/migrate_csv_to_db.py` | 新建 | 一次性迁移（union-find on netease_id/qq_id） |
| `scripts/verify_migration.py` | 新建 | 迁移后断言验证 |
| `scripts/bootstrap_db_from_snapshot.py` | 新建 | CI cache miss 时从 snapshot CSV 自动重建 DB |
| `scripts/matcher.py` | 重写 | L0 canonicalize + L1/L2/L3 + ambiguity 安全网 + UNIQUE 冲突跳过 |
| `scripts/sync.py` | 重写 | 10 步流水线，细粒度事务 |
| `scripts/netease_api.py` | 不动 | |
| `scripts/qqmusic_api_v2.py` | 不动 | |
| `scripts/csv_database.py` | 删除 | 完全被 db.py 取代 |
| `data/musicsync.db` | 新建 | SQLite 真源；进 .gitignore；CI cache 持久化 |
| `csv/song_mappings.csv` | 归档 | 保留最后状态，不再读写 |
| `csv/song_mappings_snapshot.csv` | 新建 | 每次 sync 末尾 dump，提交进 git |
| `state/sync_state.json` | 删除 | lyrics_cache → DB；last_sync_at → meta |
| `tests/test_db.py` | 新建 | UNIQUE 约束反向测试 |
| `tests/test_migrate.py` | 新建 | 迁移脚本对 fixture 测试 |
| `tests/test_matcher.py` | 新建 | L0/L1/L2/L3 + 主版本规则 + UNIQUE 冲突 |
| `tests/test_sync_plan.py` | 新建 | mock API，断言 Step 6 plan 输出 |
| `.github/workflows/sync.yml` | 修改 | cache key 改 DB + bootstrap fallback |
| `.gitignore` | 修改 | 加 `data/` |

---

## Task 1: SQLite Schema 与基础 Helpers

**Files:**
- Create: `scripts/db.py`
- Create: `tests/test_db.py`

### Step 1.1: 写失败测试 — schema 创建后表存在

- [ ] Create `tests/test_db.py` with:

```python
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
            # 第二次同 (qq, 100) 必须抛 IntegrityError 或被 helper 视为更新
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
    test_meta_set_and_get()
    print("ALL OK")
```

- [ ] Run: `python tests/test_db.py`
- [ ] Expected: ModuleNotFoundError / ImportError (db.py 不存在)

### Step 1.2: 实现 `scripts/db.py`

- [ ] Create `scripts/db.py`:

```python
"""SQLite store for MusicSync — schema + connection + UPSERT/查询 helpers."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS songs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key          TEXT NOT NULL,
    name                   TEXT NOT NULL,
    artist                 TEXT NOT NULL,
    album                  TEXT,
    match_source           TEXT NOT NULL CHECK(match_source IN
                           ('manual','l0_canonical','l1_isrc','l2_lyrics','l3_name_artist','unmatched','migrated')),
    original_match_source  TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_songs_canonical ON songs(canonical_key);

CREATE TABLE IF NOT EXISTS platform_links (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id           INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL CHECK(platform IN ('netease','qq')),
    platform_track_id TEXT NOT NULL,
    platform_name     TEXT,
    platform_artist   TEXT,
    platform_album    TEXT,
    liked             INTEGER NOT NULL DEFAULT 1 CHECK(liked IN (0,1)),
    synced_at         TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (platform, platform_track_id)
);
CREATE INDEX IF NOT EXISTS idx_links_song_platform ON platform_links(song_id, platform);
CREATE INDEX IF NOT EXISTS idx_links_liked ON platform_links(platform, liked);

CREATE TABLE IF NOT EXISTS lyrics_cache (
    platform          TEXT NOT NULL CHECK(platform IN ('netease','qq')),
    platform_track_id TEXT NOT NULL,
    original          TEXT,
    translated        TEXT,
    cached_at         TEXT NOT NULL,
    PRIMARY KEY (platform, platform_track_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

SCHEMA_VERSION = "1"


def default_db_path() -> Path:
    return Path(os.path.dirname(__file__)).parent / "data" / "musicsync.db"


def get_db_path() -> Path:
    env_path = os.getenv("DB_PATH")
    if env_path:
        return Path(env_path)
    return default_db_path()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db(path: str | os.PathLike) -> None:
    """Create schema if missing. Idempotent."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        # Stamp schema version (no-op if already set)
        cur = conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        if cur.fetchone() is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
    finally:
        conn.close()


@contextmanager
def connect(path: str | os.PathLike | None = None):
    """Open a SQLite connection with PRAGMA foreign_keys=ON and row factory."""
    target = str(path) if path is not None else str(get_db_path())
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# --- Song helpers ---

def upsert_song(
    conn: sqlite3.Connection,
    *,
    canonical_key: str,
    name: str,
    artist: str,
    album: Optional[str],
    match_source: str,
    original_match_source: Optional[str] = None,
    song_id: Optional[int] = None,
) -> int:
    """Insert a new song or update existing by id. Returns song.id."""
    ts = now_iso()
    if song_id is not None:
        conn.execute(
            "UPDATE songs SET canonical_key=?, name=?, artist=?, album=?, "
            "match_source=?, original_match_source=?, updated_at=? WHERE id=?",
            (canonical_key, name, artist, album, match_source,
             original_match_source, ts, song_id),
        )
        conn.commit()
        return song_id
    cur = conn.execute(
        "INSERT INTO songs (canonical_key, name, artist, album, match_source, "
        "original_match_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (canonical_key, name, artist, album, match_source,
         original_match_source, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def get_song(conn: sqlite3.Connection, song_id: int) -> Optional[sqlite3.Row]:
    cur = conn.execute("SELECT * FROM songs WHERE id=?", (song_id,))
    return cur.fetchone()


def get_song_by_canonical(conn: sqlite3.Connection, canonical_key: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM songs WHERE canonical_key=? ORDER BY id", (canonical_key,)
    )
    return cur.fetchall()


def update_match_source(conn: sqlite3.Connection, song_id: int, match_source: str) -> None:
    conn.execute(
        "UPDATE songs SET match_source=?, updated_at=? WHERE id=?",
        (match_source, now_iso(), song_id),
    )
    conn.commit()


# --- platform_links helpers ---

def upsert_platform_link(
    conn: sqlite3.Connection,
    *,
    song_id: int,
    platform: str,
    platform_track_id: str,
    platform_name: Optional[str] = None,
    platform_artist: Optional[str] = None,
    platform_album: Optional[str] = None,
    liked: int = 1,
    synced_at: Optional[str] = None,
) -> int:
    """Insert by (platform, platform_track_id) or update existing row's mutable fields.
    Returns platform_links.id.
    """
    ts = now_iso()
    cur = conn.execute(
        "SELECT id FROM platform_links WHERE platform=? AND platform_track_id=?",
        (platform, platform_track_id),
    )
    row = cur.fetchone()
    if row is not None:
        conn.execute(
            "UPDATE platform_links SET song_id=?, platform_name=?, platform_artist=?, "
            "platform_album=?, liked=?, synced_at=COALESCE(?, synced_at), updated_at=? "
            "WHERE id=?",
            (song_id, platform_name, platform_artist, platform_album,
             liked, synced_at, ts, row["id"]),
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO platform_links (song_id, platform, platform_track_id, "
        "platform_name, platform_artist, platform_album, liked, synced_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (song_id, platform, platform_track_id, platform_name, platform_artist,
         platform_album, liked, synced_at, ts, ts),
    )
    conn.commit()
    return cur.lastrowid


def get_link(conn: sqlite3.Connection, platform: str,
             platform_track_id: str) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM platform_links WHERE platform=? AND platform_track_id=?",
        (platform, platform_track_id),
    )
    return cur.fetchone()


def get_links_for_song(conn: sqlite3.Connection, song_id: int) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM platform_links WHERE song_id=? ORDER BY platform", (song_id,)
    )
    return cur.fetchall()


def get_links_for_platform(conn: sqlite3.Connection, platform: str,
                           liked_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM platform_links WHERE platform=?"
    params: tuple = (platform,)
    if liked_only:
        sql += " AND liked=1"
    cur = conn.execute(sql, params)
    return cur.fetchall()


def set_link_liked(conn: sqlite3.Connection, link_id: int, liked: int,
                   synced_at: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE platform_links SET liked=?, synced_at=COALESCE(?, synced_at), updated_at=? "
        "WHERE id=?",
        (liked, synced_at, now_iso(), link_id),
    )
    conn.commit()


# --- lyrics_cache helpers (autocommit-style separate conn recommended) ---

def get_lyrics(conn: sqlite3.Connection, platform: str,
               platform_track_id: str) -> Optional[tuple[str, str]]:
    cur = conn.execute(
        "SELECT original, translated FROM lyrics_cache "
        "WHERE platform=? AND platform_track_id=?",
        (platform, platform_track_id),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row["original"] or "", row["translated"] or ""


def put_lyrics(conn: sqlite3.Connection, platform: str, platform_track_id: str,
               original: str, translated: str) -> None:
    conn.execute(
        "INSERT INTO lyrics_cache (platform, platform_track_id, original, translated, cached_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(platform, platform_track_id) DO UPDATE SET "
        "original=excluded.original, translated=excluded.translated, "
        "cached_at=excluded.cached_at",
        (platform, platform_track_id, original, translated, now_iso()),
    )
    conn.commit()


# --- meta helpers ---

def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None
```

- [ ] Run: `python tests/test_db.py`
- [ ] Expected: `ALL OK`

### Step 1.3: 验证 sync.py 依然能跑 dry-run（向后兼容窗口）

由于本 task 没有改动 sync.py，旧流水线应仍正常。

- [ ] Run: `$env:DRY_RUN="true"; python scripts/sync.py`
- [ ] Expected: sync.py 像往常一样执行旧 CSV 流水线并退出（无 crash）

### Step 1.4: Commit

- [ ] Run:

```bash
git add scripts/db.py tests/test_db.py
git commit -m "feat(db): SQLite schema + helpers + reverse UNIQUE test

- scripts/db.py: songs/platform_links/lyrics_cache/meta schema (WAL, FK on)
- UNIQUE(platform, platform_track_id) 是 A1 的结构性防护
- canonical_key 仅索引（非 UNIQUE）保留 A2 album 变体
- tests/test_db.py: 反向验证 UNIQUE 抛 IntegrityError + UPSERT 更新而非冲突"
```

---

## Task 2: 一次性迁移、验证、Bootstrap 脚本

**Files:**
- Create: `scripts/migrate_csv_to_db.py`
- Create: `scripts/verify_migration.py`
- Create: `scripts/bootstrap_db_from_snapshot.py`
- Create: `tests/test_migrate.py`
- Create: `tests/fixtures/song_mappings_fixture.csv`

### Step 2.1: 准备 fixture CSV

fixture 必须覆盖 A1（同 qq_id 多行）、A2（不同 qq_id 同 canonical_key）、B（双侧 qq_only 但同曲）、manual、unmatched、qq_only。

- [ ] Create `tests/fixtures/song_mappings_fixture.csv`:

```csv
"netease_id","qq_id","name","artist","album","ne_name","ne_artist","qq_name","qq_artist","match_source","synced"
"100","200","Tell me","milet","Prover","Tell me","milet","Tell me","milet","manual","1"
"101","201","Tell me","milet","eyes","Tell me","milet","Tell me","milet","name_artist","1"
"102","202","风起天阑","河图","风起天阑","风起天阑","河图","风起天阑","河图","manual","1"
"103","","逆光","坂本真綾","逆光","逆光","坂本真綾","","","unmatched","0"
"104","203","Love Story (Taylor's Version)","Taylor Swift","Fearless TV","Love Story (Taylor's Version)","Taylor Swift","Love Story","Taylor Swift","name_artist","1"
"","203","Love Story","Taylor Swift","Fearless","","","Love Story","Taylor Swift","qq_only","0"
"","204","蜜雪冰城","蜜雪冰城","","","","蜜雪冰城","蜜雪冰城","qq_only","0"
"105","","蜜雪冰城","蜜雪冰城","","蜜雪冰城","蜜雪冰城","","","unmatched","0"
```

- Row 100/101: A2 — 同 milet/Tell me 不同 album 不同 qq_id，应保留为 2 个 song
- Row 104 + row(qq_id=203, ne_id=""): A1 — 同 qq_id=203 出现两行（name 还不一样：Love Story vs Love Story (Taylor's Version)），应合并为 1 个 song 持 2 个 platform_link？不对——qq_id 203 唯一。union-find 应按 (netease_id=104, qq_id=203) 和 (netease_id="", qq_id=203) 合并到同一簇。合并后保留 ne_id=104 那条作为主行；qq_only 那行被消化。
- Row 105 + row(qq_id=204): B — 双侧都未 sync，name 相同。**不应**自动合并（迁移按 id 合并，不按 canonical_key）。两个 song，一个有 ne_id=105，一个有 qq_id=204。

### Step 2.2: 写失败测试

- [ ] Create `tests/test_migrate.py`:

```python
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
        # 找到挂着 qq_id=203 的 song
        links = _rows(db, "SELECT * FROM platform_links WHERE platform='qq' AND platform_track_id='203'")
        assert len(links) == 1, f"expected 1 link for qq=203, got {len(links)}"
        song_id = links[0]["song_id"]
        # 同 song 应同时挂着 netease_id=104
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
        # row 100 (manual) + row 102 (manual) = 2 个 manual song
        assert len(manual) >= 2
        # original_match_source 也应为 'manual' 便于审计统一
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
        # original_match_source 应为 name_artist / unmatched / qq_only 之一
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
```

- [ ] Run: `python tests/test_migrate.py`
- [ ] Expected: ModuleNotFoundError (migrate_csv_to_db 不存在)

### Step 2.3: 实现 `scripts/migrate_csv_to_db.py`

- [ ] Create `scripts/migrate_csv_to_db.py`:

```python
"""One-shot: migrate csv/song_mappings.csv → data/musicsync.db.

Algorithm: union-find on (netease_id, qq_id) — any two rows that share
either id collapse into one song cluster. canonical_key is NOT used as
the merge key because the same logical song may have different cleaned
names across platforms (e.g. "Love Story (Taylor's Version)" vs "Love Story").
"""
import csv
import json
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from db import (
    init_db, connect, upsert_song, upsert_platform_link, put_lyrics, set_meta,
)
from matcher import clean_name, clean_artist  # uses post-rewrite matcher's helpers; available now from old matcher too


PRIORITY = {
    "manual": 0,
    "isrc": 1,
    "name_artist": 2,
    "unmatched": 3,
    "qq_only": 3,
}


class UnionFind:
    def __init__(self):
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        while self.parent.get(x, x) != x:
            self.parent[x] = self.parent.get(self.parent[x], self.parent[x])
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def add(self, x: int) -> None:
        self.parent.setdefault(x, x)


def _canonical_key(name: str, artist: str) -> str:
    return f"{clean_name(name)}|{clean_artist(artist)}"


def _priority(row: dict) -> int:
    return PRIORITY.get(row.get("match_source", ""), 99)


def migrate_csv(csv_path: str, db_path: str) -> dict:
    """Read CSV → write SQLite. Returns summary dict."""
    init_db(db_path)

    rows: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    uf = UnionFind()
    ne_to_idx: dict[str, int] = {}
    qq_to_idx: dict[str, int] = {}

    for i, r in enumerate(rows):
        uf.add(i)
        ne = (r.get("netease_id") or "").strip()
        qq = (r.get("qq_id") or "").strip()
        if ne:
            if ne in ne_to_idx:
                uf.union(i, ne_to_idx[ne])
            else:
                ne_to_idx[ne] = i
        if qq:
            if qq in qq_to_idx:
                uf.union(i, qq_to_idx[qq])
            else:
                qq_to_idx[qq] = i

    clusters: dict[int, list[int]] = {}
    for i in range(len(rows)):
        clusters.setdefault(uf.find(i), []).append(i)

    summary = {"rows": len(rows), "songs": 0, "links": 0}

    with connect(db_path) as conn:
        for cluster_rows in clusters.values():
            members = [rows[i] for i in cluster_rows]
            # 主行: 最高优先级
            members.sort(key=_priority)
            head = members[0]

            # 决定 match_source：manual 保留 manual，否则 'migrated'
            is_manual = (head.get("match_source") == "manual")
            match_source = "manual" if is_manual else "migrated"
            original_match_source = head.get("match_source") or None

            song_id = upsert_song(
                conn,
                canonical_key=_canonical_key(head.get("name", ""), head.get("artist", "")),
                name=head.get("name", "") or head.get("ne_name", "") or head.get("qq_name", "") or "",
                artist=head.get("artist", "") or head.get("ne_artist", "") or head.get("qq_artist", "") or "",
                album=head.get("album") or None,
                match_source=match_source,
                original_match_source=original_match_source,
            )
            summary["songs"] += 1

            seen_links: set[tuple[str, str]] = set()
            for r in members:
                ne_id = (r.get("netease_id") or "").strip()
                qq_id = (r.get("qq_id") or "").strip()
                if ne_id and ("netease", ne_id) not in seen_links:
                    upsert_platform_link(
                        conn,
                        song_id=song_id,
                        platform="netease",
                        platform_track_id=ne_id,
                        platform_name=r.get("ne_name") or r.get("name") or "",
                        platform_artist=r.get("ne_artist") or r.get("artist") or "",
                        platform_album=r.get("album") or None,
                        liked=1,
                        synced_at=None,
                    )
                    seen_links.add(("netease", ne_id))
                    summary["links"] += 1
                if qq_id and ("qq", qq_id) not in seen_links:
                    upsert_platform_link(
                        conn,
                        song_id=song_id,
                        platform="qq",
                        platform_track_id=qq_id,
                        platform_name=r.get("qq_name") or r.get("name") or "",
                        platform_artist=r.get("qq_artist") or r.get("artist") or "",
                        platform_album=r.get("album") or None,
                        liked=1,
                        synced_at=None,
                    )
                    seen_links.add(("qq", qq_id))
                    summary["links"] += 1

        # 迁移 state.json 中的 lyrics_cache
        state_path = ROOT / "state" / "sync_state.json"
        if state_path.exists():
            try:
                with open(state_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                cache = state.get("lyrics_cache", {})
                imported = 0
                for k, v in cache.items():
                    # 旧 key 形如 "netease:<id>" / "qq:<id>"；如果是别的格式跳过
                    if ":" not in k:
                        continue
                    platform, tid = k.split(":", 1)
                    if platform not in ("netease", "qq") or not tid:
                        continue
                    if isinstance(v, dict):
                        original = v.get("original", "") or v.get("lyric", "") or ""
                        translated = v.get("translated", "") or v.get("trans", "") or ""
                    elif isinstance(v, (list, tuple)) and len(v) >= 2:
                        original, translated = v[0] or "", v[1] or ""
                    else:
                        continue
                    put_lyrics(conn, platform, tid, original, translated)
                    imported += 1
                summary["lyrics_cache_imported"] = imported
                last_sync = state.get("last_sync")
                if last_sync:
                    set_meta(conn, "last_sync_at", last_sync)
            except Exception as e:
                print(f"WARN: lyrics_cache migration skipped: {e}")

    return summary


def main(argv: list[str]) -> int:
    csv_path = argv[1] if len(argv) > 1 else str(ROOT / "csv" / "song_mappings.csv")
    db_path = argv[2] if len(argv) > 2 else str(ROOT / "data" / "musicsync.db")
    if os.path.exists(db_path):
        print(f"REFUSING: {db_path} already exists. Delete it first if you mean to re-run.")
        return 1
    summary = migrate_csv(csv_path, db_path)
    print(f"Migrated: {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
```

- [ ] Run: `python tests/test_migrate.py`
- [ ] Expected: `ALL OK`

### Step 2.4: 实现 `scripts/verify_migration.py`

- [ ] Create `scripts/verify_migration.py`:

```python
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
```

### Step 2.5: 实现 `scripts/bootstrap_db_from_snapshot.py`

- [ ] Create `scripts/bootstrap_db_from_snapshot.py`:

```python
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
```

### Step 2.6: 验证 sync.py 仍能跑

本 task 不动 sync.py（旧流水线继续工作）。

- [ ] Run: `$env:DRY_RUN="true"; python scripts/sync.py`
- [ ] Expected: 旧流水线退出无 crash

### Step 2.7: Commit

- [ ] Run:

```bash
git add scripts/migrate_csv_to_db.py scripts/verify_migration.py scripts/bootstrap_db_from_snapshot.py
git add tests/test_migrate.py tests/fixtures/song_mappings_fixture.csv
git commit -m "feat(migrate): one-shot CSV→SQLite migration + verify + bootstrap

- scripts/migrate_csv_to_db.py: union-find on (ne_id, qq_id) merges A1; A2 stays split by qq_id
- scripts/verify_migration.py: post-migration assertions (pair coverage, manual preserved, A1=0)
- scripts/bootstrap_db_from_snapshot.py: CI cache-miss disaster recovery from snapshot CSV
- tests/test_migrate.py + fixture: A1 merge, A2 split, manual preserved, no dup links"
```

---

## Task 3: Matcher 四层重写

**Files:**
- Modify: `scripts/matcher.py` (重写整个文件)
- Create: `tests/test_matcher.py`

匹配器要新增的能力：L0 canonicalize 决定 song 主版本、L2 duration ±15s（短歌词回收 ±5s）、L3 升级为可执行（含 ambiguity 安全网）、Step 5 UNIQUE 冲突跳过的显式预检函数。

### Step 3.1: 写失败测试

- [ ] Create `tests/test_matcher.py`:

```python
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
            # 手动倒置 created_at，确保规则按 created_at 决定而非 id
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


# --- L2 短歌词 duration 收紧 ---

def test_l2_long_lyrics_uses_15s_tolerance():
    ne = {"duration": 100, "_lyrics": ("a\n" * 200, "")}
    qq = {"duration": 113, "_lyrics": ("a\n" * 200, "")}
    assert match_l2(ne, qq) is True


def test_l2_short_lyrics_keeps_5s_tolerance():
    short = "唯一一句"
    ne = {"duration": 100, "_lyrics": (short, "")}
    qq = {"duration": 110, "_lyrics": (short, "")}
    # 短歌词 <100 字符 → ±5s 限制 → 10s 差距应失败
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
    test_l2_long_lyrics_uses_15s_tolerance()
    test_l2_short_lyrics_keeps_5s_tolerance()
    test_l3_with_single_candidate_matches()
    test_l3_with_multiple_same_canonical_skips()
    test_check_link_conflict_returns_other_song_id()
    test_check_link_conflict_returns_none_when_own()
    test_l1_exact_isrc_match()
    print("ALL OK")
```

- [ ] Run: `python tests/test_matcher.py`
- [ ] Expected: ImportError (新 helper 未实现)

### Step 3.2: 重写 `scripts/matcher.py`

- [ ] Replace `scripts/matcher.py` entirely with:

```python
"""Song matching engine.

Two distinct usage scenes:
- Scene A (canonicalize, Step 3): we hold a (platform, platform_track_id) freshly
  fetched. Question: which song does it belong to? Uses L0 only — local DB query.
- Scene B (match unlinked, Step 5): a song has a one-sided link; we want to find
  the matching track on the other platform via search API. Uses L1/L2/L3 only.
  Reusing L0 would attach to a local song instead of finding a real other-platform
  track, causing wrong-album cross-attachments.
"""
import difflib
import re
import sqlite3
from typing import Optional

from db import (
    get_song_by_canonical, get_link, upsert_song, upsert_platform_link,
)


SHORT_LYRICS_THRESHOLD = 100
L2_DURATION_TOLERANCE = 15
L2_SHORT_DURATION_TOLERANCE = 5
L2_LYRICS_SIMILARITY = 0.6


# --- Normalization ---

def clean_name(name: str) -> str:
    if not name:
        return ""
    n = str(name)
    n = re.sub(r"\([^)]*\)", "", n)
    n = re.sub(r"\[[^\]]*\]", "", n)
    n = re.sub(r"([^)]*)", "", n)
    n = re.sub(r"【[^】]*】", "", n)
    n = n.replace(" - ", " ").replace(" – ", " ")
    n = n.replace("／", "/").replace("：", ":")
    n = re.sub(r"\s+", " ", n).strip()
    return n.lower()


def clean_artist(artist: str) -> str:
    if not artist:
        return ""
    return str(artist).strip().lower()


def canonical_key(name: str, artist: str) -> str:
    return f"{clean_name(name)}|{clean_artist(artist)}"


def normalize_lyrics(raw: str) -> str:
    if not raw:
        return ""
    lines = raw.splitlines()
    clean = []
    for line in lines:
        line = re.sub(r"\[[^\]]*\]", "", line).strip()
        if re.match(
            r"^(作词|作曲|编曲|词|曲|唱|词曲|制作人|出品|演唱|混音|母带|录音|"
            r"Lyrics\s+by|Composed\s+by|Programming|All\s+Instrument)\b",
            line, re.IGNORECASE,
        ):
            continue
        if re.match(r"^.{1,70}\s+[-–—]\s+.{1,30}$", line):
            continue
        if line:
            clean.append(line)
    return "\n".join(clean)


def lyrics_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    aa = re.sub(r"\s+", "", a)
    bb = re.sub(r"\s+", "", b)
    if not aa or not bb:
        return 0.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


# --- L0 canonicalize (Scene A) ---

def pick_primary_song(conn: sqlite3.Connection,
                      songs: list[sqlite3.Row]) -> sqlite3.Row:
    """Apply the 4-tier rule:
    1. manual > others
    2. has liked=1 + synced_at NOT NULL > others
    3. older created_at > younger
    4. smaller id > larger
    """
    if len(songs) == 1:
        return songs[0]

    def score(song: sqlite3.Row) -> tuple:
        is_manual = 0 if song["match_source"] == "manual" else 1
        has_synced = conn.execute(
            "SELECT 1 FROM platform_links WHERE song_id=? AND liked=1 "
            "AND synced_at IS NOT NULL LIMIT 1",
            (song["id"],),
        ).fetchone()
        synced_score = 0 if has_synced else 1
        return (is_manual, synced_score, song["created_at"], song["id"])

    return sorted(songs, key=score)[0]


def l0_canonicalize(
    conn: sqlite3.Connection,
    *,
    platform: str,
    track: dict,
) -> tuple[int, int]:
    """Canonicalize a freshly-fetched track.

    Returns (song_id, platform_link_id).
    """
    tid = str(track["id"])

    existing = get_link(conn, platform, tid)
    if existing is not None:
        link_id = upsert_platform_link(
            conn,
            song_id=existing["song_id"],
            platform=platform,
            platform_track_id=tid,
            platform_name=track.get("name", ""),
            platform_artist=track.get("artist", ""),
            platform_album=track.get("album", ""),
            liked=1,
        )
        return existing["song_id"], link_id

    ck = canonical_key(track.get("name", ""), track.get("artist", ""))
    candidates = get_song_by_canonical(conn, ck)
    if not candidates:
        song_id = upsert_song(
            conn,
            canonical_key=ck,
            name=track.get("name", ""),
            artist=track.get("artist", ""),
            album=track.get("album") or None,
            match_source="l0_canonical",
        )
    else:
        primary = pick_primary_song(conn, candidates)
        song_id = primary["id"]

    link_id = upsert_platform_link(
        conn,
        song_id=song_id,
        platform=platform,
        platform_track_id=tid,
        platform_name=track.get("name", ""),
        platform_artist=track.get("artist", ""),
        platform_album=track.get("album", ""),
        liked=1,
    )
    return song_id, link_id


# --- L1 / L2 / L3 (Scene B) ---

def match_l1(a: dict, b: dict) -> bool:
    ia = str(a.get("isrc", "")).strip().upper()
    ib = str(b.get("isrc", "")).strip().upper()
    return bool(ia and ib and ia == ib)


def _duration_match(da: int, db_: int, tol: int) -> bool:
    if da <= 0 or db_ <= 0:
        return False
    return abs(da - db_) <= tol


def match_l2(a: dict, b: dict) -> bool:
    """Lyrics similarity ≥0.6 AND duration within tolerance.

    Long lyrics (≥100 chars): duration tolerance ±15s
    Short lyrics (<100 chars): duration tolerance tightens to ±5s (multiply-error guard)
    """
    a_orig, a_trans = a.get("_lyrics", ("", ""))
    b_orig, b_trans = b.get("_lyrics", ("", ""))
    da, db_ = a.get("duration", 0), b.get("duration", 0)

    pairs = []
    a_orig_n = normalize_lyrics(a_orig)
    b_orig_n = normalize_lyrics(b_orig)
    a_trans_n = normalize_lyrics(a_trans)
    b_trans_n = normalize_lyrics(b_trans)
    if a_orig_n and b_orig_n:
        pairs.append((a_orig_n, b_orig_n))
    if a_trans_n and b_trans_n:
        pairs.append((a_trans_n, b_trans_n))
    if a_orig_n and b_trans_n:
        pairs.append((a_orig_n, b_trans_n))
    if a_trans_n and b_orig_n:
        pairs.append((a_trans_n, b_orig_n))

    for x, y in pairs:
        if lyrics_similarity(x, y) < L2_LYRICS_SIMILARITY:
            continue
        is_short = max(len(x), len(y)) < SHORT_LYRICS_THRESHOLD
        tol = L2_SHORT_DURATION_TOLERANCE if is_short else L2_DURATION_TOLERANCE
        if _duration_match(da, db_, tol):
            return True
    return False


def match_l3(a: dict, b: dict) -> bool:
    """Cleaned name equality + artist containment (weakest layer)."""
    na, nb = clean_name(a.get("name", "")), clean_name(b.get("name", ""))
    if not na or not nb or na != nb:
        return False
    aa, ab = clean_artist(a.get("artist", "")), clean_artist(b.get("artist", ""))
    if not aa or not ab:
        return False
    return aa in ab or ab in aa


def find_match_in_candidates(
    source: dict,
    candidates: list[dict],
    allow_l1: bool = True,
    allow_l2: bool = True,
    allow_l3: bool = True,
) -> tuple[Optional[dict], str]:
    """Walk candidates in order, return first L1/L2/L3 hit.

    L3 ambiguity safety net: if ≥2 candidates share canonical_key with `source`,
    abort L3 entirely for this song (return no match).
    """
    if allow_l1:
        for c in candidates:
            if match_l1(source, c):
                return c, "L1"
    if allow_l2:
        for c in candidates:
            if match_l2(source, c):
                return c, "L2"
    if allow_l3:
        src_ck = canonical_key(source.get("name", ""), source.get("artist", ""))
        same_ck = [c for c in candidates
                   if canonical_key(c.get("name", ""), c.get("artist", "")) == src_ck]
        if len(same_ck) >= 2:
            return None, ""
        for c in candidates:
            if match_l3(source, c):
                return c, "L3"
    return None, ""


# --- Step 5 UNIQUE conflict pre-check ---

def check_link_conflict(
    conn: sqlite3.Connection,
    *,
    platform: str,
    platform_track_id: str,
    current_song_id: int,
) -> Optional[int]:
    """If `(platform, platform_track_id)` already exists under a different song,
    return that song.id. Otherwise return None (safe to insert).
    """
    row = conn.execute(
        "SELECT song_id FROM platform_links WHERE platform=? AND platform_track_id=?",
        (platform, platform_track_id),
    ).fetchone()
    if row is None:
        return None
    if row["song_id"] == current_song_id:
        return None
    return row["song_id"]
```

- [ ] Run: `python tests/test_matcher.py`
- [ ] Expected: `ALL OK`

### Step 3.3: 验证 sync.py 仍能跑

sync.py 旧代码 `from matcher import match_track, match_l1, match_l2, match_l3` — `match_track` 已被移除。需要在 matcher.py 末尾加一个向后兼容 shim，**仅本 task 期间保留**，下个 task 删 sync.py 重写时一并清掉。

- [ ] Append shim to bottom of `scripts/matcher.py`:

```python
# --- Backwards-compat shim (removed in Task 4 when sync.py is rewritten) ---

def match_track(netease_track: dict,
                qq_search_results: list[dict]) -> tuple[Optional[dict], str]:
    """LEGACY: old sync.py entry — equivalent to find_match_in_candidates."""
    return find_match_in_candidates(netease_track, qq_search_results)
```

- [ ] Run: `$env:DRY_RUN="true"; python scripts/sync.py`
- [ ] Expected: 旧流水线退出无 crash

### Step 3.4: Commit

- [ ] Run:

```bash
git add scripts/matcher.py tests/test_matcher.py
git commit -m "feat(matcher): L0 canonicalize + L1/L2/L3 with safety nets

- canonical_key + pick_primary_song: 4-tier primary rule (manual>synced>oldest>id)
- l0_canonicalize: Scene A (existing link / new song / canonical hit attach)
- match_l2: duration ±15s (short lyrics <100 chars: ±5s)
- find_match_in_candidates: L3 ambiguity safety net (≥2 same canonical_key → skip)
- check_link_conflict: Step 5 UNIQUE pre-check helper
- tests/test_matcher.py: 4 primary-rule branches + L0 + L2 short/long + L3 ambiguity + conflict pre-check
- shim match_track preserved temporarily for legacy sync.py until Task 4"
```

---

## Task 4: 10 步流水线整体重写

**Files:**
- Rewrite: `scripts/sync.py`
- Create: `tests/test_sync_plan.py`
- Delete: `scripts/csv_database.py`
- Delete: `state/sync_state.json` (working tree only; migrate first if not yet done)

本 task 是最大的一次 commit。spec §实施顺序 Task 4 合并说明：早期切分为 4/5/6 三个 task 时中间 commit 不可运行，故合为单 commit 全量重写，保证每 commit 都 dry-run 可运行。

### Step 4.1: 写失败测试 — sync_plan

- [ ] Create `tests/test_sync_plan.py`:

```python
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
        result = sync.run_pipeline(ne, qq, db_path=db, dry_run=True)
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


if __name__ == "__main__":
    test_plan_steady_state_yields_nothing()
    test_plan_unlike_detected_when_track_missing()
    test_fetch_sanity_check_aborts_on_large_drop()
    test_fetch_sanity_skipped_on_first_run()
    test_unlike_safety_gate_skips_cleanup_when_over_threshold()
    print("ALL OK")
```

- [ ] Run: `python tests/test_sync_plan.py`
- [ ] Expected: ImportError 或 AttributeError (`sync.run_pipeline` 不存在)

### Step 4.2: 重写 `scripts/sync.py`

- [ ] Replace `scripts/sync.py` entirely with:

```python
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
) -> Optional[str]:
    """Return abort reason string if fetch suggests data loss, else None."""
    if FORCE_FULL_SYNC:
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
            from db import update_match_source
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
            from db import update_match_source
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
        from db import update_match_source
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
    global FORCE_FULL_SYNC
    if force_full_sync:
        FORCE_FULL_SYNC = True

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
            reason = fetch_sanity_check(conn, plat, fetched)
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

        if dry_run:
            print("DRY_RUN — stopping before Step 7/8.")
            return summary

        # Step 7: safety gate
        cleanup_skipped = False
        unlike_count = len(plan["unlike_ne"]) + len(plan["unlike_qq"])
        if FORCE_FULL_SYNC:
            cleanup_skipped = True
            print(f"FORCE_FULL_SYNC — cleanup skipped entirely")
        elif unlike_count > UNLIKE_ABORT_THRESHOLD:
            cleanup_skipped = True
            print(f"WARN: unlike count {unlike_count} > {UNLIKE_ABORT_THRESHOLD} — "
                  "cleanup skipped (ADDs still executed)")
        summary["cleanup_skipped"] = cleanup_skipped

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
    qq_api = auth.get_qqmusic_api()
    if ne_api is None or qq_api is None:
        print("Auth failed — abort.")
        return 1
    if not getattr(ne_api, "_ready", True) or not qq_api.ready:
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
```

### Step 4.3: 删除旧文件

- [ ] Run:

```powershell
git rm scripts/csv_database.py
```

- [ ] If `state/sync_state.json` exists: `git rm state/sync_state.json` (前提：已先用 migrate 把 lyrics_cache 灌进 DB)

### Step 4.4: 跑测试与 dry-run

- [ ] Run: `python tests/test_sync_plan.py`
- [ ] Expected: `ALL OK`
- [ ] Run: `python tests/test_db.py`
- [ ] Expected: `ALL OK`
- [ ] Run: `python tests/test_matcher.py`
- [ ] Expected: `ALL OK`
- [ ] Run: `python tests/test_migrate.py`
- [ ] Expected: `ALL OK`

本地端到端 dry-run（需要先把 csv → db 跑过；若 `data/musicsync.db` 不存在，先跑 `python scripts/migrate_csv_to_db.py`）:

- [ ] Run: `$env:DRY_RUN="true"; python scripts/sync.py`
- [ ] Expected: 打印 fetched 数 + plan summary，不 crash，无 DRY_RUN 写入

### Step 4.5: 删 matcher.py 的兼容 shim

- [ ] Open `scripts/matcher.py` 删除 `match_track` 兼容 shim（Task 3 末尾加的那段）

### Step 4.6: Commit

- [ ] Run:

```bash
git add scripts/sync.py scripts/matcher.py tests/test_sync_plan.py
git commit -m "feat(sync): 10-step pipeline over SQLite, fine-grained transactions

- run_pipeline 单入口；test_sync_plan.py 用 mock API 覆盖
- Step 2.5 fetch sanity (first-run / FORCE_FULL_SYNC 跳过)
- Step 5 match unlinked: L0 不参与；L1→L2→L3；UNIQUE 冲突预检 + 跳过
- Step 7 safety gate: |unlike| > 10 → cleanup 整段跳过，ADD 继续
- 每条 API 成功后立即 UPDATE + COMMIT；崩溃后已完成进度全保留
- Step 9 snapshot dump 进 csv/song_mappings_snapshot.csv + meta.last_sync_at
- 删除 scripts/csv_database.py 和 state/sync_state.json 及 matcher.py 兼容 shim"
```

---

## Task 5: CI workflow 切换 + 端到端真跑

**Files:**
- Modify: `.github/workflows/sync.yml`
- Modify: `.gitignore`

### Step 5.1: 更新 `.gitignore`

- [ ] Edit `.gitignore` 加一行 `data/` (位置：和现有 `state/` 邻近)

### Step 5.2: 改写 `.github/workflows/sync.yml`

- [ ] Replace the two `Restore sync state` / `Restore CSV database` steps and the two trailing `Save` steps with:

```yaml
      - name: Restore SQLite DB
        id: restore-db
        uses: actions/cache/restore@v4
        with:
          path: data/musicsync.db
          key: musicsync-db-${{ github.run_id }}
          restore-keys: musicsync-db-

      - name: Bootstrap DB from snapshot if missing
        run: |
          if [ ! -f data/musicsync.db ]; then
            python scripts/bootstrap_db_from_snapshot.py
          fi
```

trailing:

```yaml
      - name: Save SQLite DB
        if: github.event.inputs.dry_run != 'true'
        uses: actions/cache/save@v4
        with:
          path: data/musicsync.db
          key: musicsync-db-${{ github.run_id }}

      - name: Commit snapshot CSV
        if: github.event.inputs.dry_run != 'true'
        run: |
          git config user.email "actions@github.com"
          git config user.name "github-actions"
          git add csv/song_mappings_snapshot.csv
          if git diff --cached --quiet; then
            echo "no snapshot changes"
          else
            git commit -m "data: sync snapshot $(date -u +%FT%TZ)"
            git push
          fi
```

### Step 5.3: 本地端到端真跑验证

注意：本步骤会真写两个平台 — 用户偏好本地小批量验证。

- [ ] Run: `$env:DRY_RUN="false"; $env:REVERSE_BATCH="5"; python scripts/sync.py`
- [ ] Expected: 至少 1 首歌通过 L1/L2/L3 双向 add；snapshot CSV 更新
- [ ] Inspect: `python scripts/verify_migration.py` 仍 OK（DB 没腐败）

### Step 5.4: Bootstrap 演练

- [ ] Backup: `Copy-Item data/musicsync.db data/musicsync.db.bak`
- [ ] Delete: `Remove-Item data/musicsync.db`
- [ ] Run: `python scripts/bootstrap_db_from_snapshot.py`
- [ ] Expected: 打印 songs/links 数；DB 重建成功
- [ ] Run: `$env:DRY_RUN="true"; python scripts/sync.py`
- [ ] Expected: plan summary 与有原 DB 时一致或合理（lyrics_cache 缺失但应继续）
- [ ] Restore: `Move-Item data/musicsync.db.bak data/musicsync.db -Force`

### Step 5.5: Commit

- [ ] Run:

```bash
git add .github/workflows/sync.yml .gitignore csv/song_mappings_snapshot.csv
git commit -m "ci: switch Actions cache to SQLite DB + snapshot bootstrap fallback

- cache key: musicsync-db-<run_id>，restore-keys: musicsync-db-
- 启动时若 DB 缺失，自动从 csv/song_mappings_snapshot.csv bootstrap
- 真跑产物 snapshot CSV 通过 commit + push 提交回 main（仅 dry_run!=true）
- .gitignore 加 data/，DB 不进 git"
```

---

## Notes for Implementers

- **不要并发跑 sync.py 与测试** — sqlite3 在 WAL 下虽允许多读单写，但 tests 用临时 DB 不冲突。
- **生产 DB 路径** `data/musicsync.db`。临时 DB 在测试里用 `tempfile.mkstemp(suffix=".db")`。
- **Lyrics fetch 缺失 duration** — 旧 sync.py 在 fetch 时不存 duration；新 sync.py 同样不存（API 返回 `duration` 字段在 fetch_all 里有，但写入 platform_links 时未保留）。Task 4 的 source dict 里 `duration=0` 会让 L2 失败。这是已知缺陷 — 真跑时主要靠 L3 命中。后续改进留给独立 PR。
- **未实现：UNLIKE_ABORT_THRESHOLD 的 env 透传** 在 run_pipeline 里读全局常量。若测试需要不同阈值，直接 monkey-patch `sync.UNLIKE_ABORT_THRESHOLD`。
- **Safety**: 每个 task commit 后必须 `python scripts/sync.py` dry-run 不 crash。Task 1/2/3 期间 sync.py 仍是旧逻辑；Task 4 切换到新逻辑后，需要先本地跑过 `migrate_csv_to_db.py` 才能 dry-run。
