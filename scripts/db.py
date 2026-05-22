"""SQLite store for MusicSync — schema + connection + UPSERT/查询 helpers."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA_V2 = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS songs (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key          TEXT NOT NULL,
    name                   TEXT NOT NULL,
    artist                 TEXT NOT NULL,
    album                  TEXT,
    match_source           TEXT NOT NULL CHECK(match_source IN
                           ('manual','l0_canonical','l1_isrc','l2_lyrics',
                            'l3_name_artist','unmatched','migrated','manual_merged')),
    original_match_source  TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    deleted_at             TEXT
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

CREATE TABLE IF NOT EXISTS merge_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_song_id  INTEGER NOT NULL,
    target_song_id  INTEGER NOT NULL,
    source_payload  TEXT NOT NULL,
    source_links    TEXT NOT NULL,
    merged_at       TEXT NOT NULL,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_merge_log_target ON merge_log(target_song_id);
"""

# V1 schema kept for migration reference
SCHEMA_V1 = """
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

SCHEMA = SCHEMA_V2  # current version
SCHEMA_VERSION = "2"


def default_db_path() -> Path:
    return Path(os.path.dirname(__file__)).parent / "data" / "musicsync.db"


def get_db_path() -> Path:
    env_path = os.getenv("DB_PATH")
    if env_path:
        return Path(env_path)
    return default_db_path()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """v1 → v2: songs gains deleted_at + manual_merged CHECK; new merge_log table."""
    conn.execute("PRAGMA foreign_keys = OFF")
    # Rebuild songs table with new CHECK constraint + deleted_at column
    conn.execute("CREATE TABLE songs_backup AS SELECT * FROM songs")
    conn.execute("DROP TABLE songs")
    conn.execute("""CREATE TABLE songs (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        canonical_key          TEXT NOT NULL,
        name                   TEXT NOT NULL,
        artist                 TEXT NOT NULL,
        album                  TEXT,
        match_source           TEXT NOT NULL CHECK(match_source IN
                               ('manual','l0_canonical','l1_isrc','l2_lyrics',
                                'l3_name_artist','unmatched','migrated','manual_merged')),
        original_match_source  TEXT,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL,
        deleted_at             TEXT
    )""")
    conn.execute("INSERT INTO songs SELECT *, NULL FROM songs_backup")
    conn.execute("DROP TABLE songs_backup")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_songs_canonical ON songs(canonical_key)")
    # Add merge_log table
    conn.execute("""CREATE TABLE IF NOT EXISTS merge_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_song_id  INTEGER NOT NULL,
        target_song_id  INTEGER NOT NULL,
        source_payload  TEXT NOT NULL,
        source_links    TEXT NOT NULL,
        merged_at       TEXT NOT NULL,
        note            TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_merge_log_target ON merge_log(target_song_id)")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("UPDATE meta SET value='2' WHERE key='schema_version'")


def init_db(path: str | os.PathLike) -> None:
    """Create schema if missing. Migrate if needed. Idempotent."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    try:
        # Check if meta table exists (fresh DB won't have it)
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
        )
        has_meta = cur.fetchone() is not None

        if not has_meta:
            # Fresh DB — create v2 schema + set version
            conn.executescript(SCHEMA_V2)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('schema_version', '2')"
            )
            conn.commit()
        else:
            cur = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            )
            row = cur.fetchone()
            ver = row[0] if row else None

            if ver == "1":
                # Migrate v1 → v2
                migrate_v1_to_v2(conn)
                conn.commit()
            elif ver == "2":
                # Already current — ensure all tables exist (idempotent)
                conn.executescript(SCHEMA_V2)
                conn.commit()
            else:
                raise RuntimeError(f"Unknown schema_version: {ver}")
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


def get_song_by_canonical(conn: sqlite3.Connection, canonical_key: str) -> list[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM songs WHERE canonical_key=? AND deleted_at IS NULL ORDER BY id",
        (canonical_key,),
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


# --- Cleanup helpers ---

def cleanup_orphan_songs(conn: sqlite3.Connection) -> list[int]:
    """Soft-delete songs that have no liked=1 links on any platform.

    Returns list of soft-deleted song IDs.
    """
    cur = conn.execute("""
        SELECT s.id FROM songs s
        WHERE s.deleted_at IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM platform_links WHERE song_id=s.id AND liked=1
        )
    """)
    ids = [r["id"] for r in cur.fetchall()]
    if not ids:
        return []
    ts = now_iso()
    conn.executemany(
        "UPDATE songs SET deleted_at=?, updated_at=? WHERE id=?",
        [(ts, ts, sid) for sid in ids],
    )
    conn.commit()
    return ids


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
