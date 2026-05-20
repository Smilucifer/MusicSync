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
