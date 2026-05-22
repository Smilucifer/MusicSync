"""Merge two songs into one — atomic transaction with audit log.

Public API:
  propose_merge(conn, source_id, target_id) → preview dict
  execute_merge(conn, source_id, target_id, name, artist, album, note=None)

Does NOT import Flask. Does NOT call external APIs. Pure DB operations.
"""
import json
import sqlite3

from db import get_links_for_song, now_iso
from matcher import canonical_key


# match_source priority: higher index = higher priority
_MATCH_SOURCE_PRIORITY = {
    "migrated": 0,
    "unmatched": 1,
    "l3_name_artist": 2,
    "l2_lyrics": 3,
    "l1_isrc": 4,
    "l0_canonical": 5,
    "manual_merged": 6,
    "manual": 7,
}


def _pick_better(a_val: str, a_src: str, b_val: str, b_src: str) -> str:
    """Pick the better field value based on match_source priority, then length."""
    pa = _MATCH_SOURCE_PRIORITY.get(a_src, -1)
    pb = _MATCH_SOURCE_PRIORITY.get(b_src, -1)
    if pa != pb:
        return a_val if pa > pb else b_val
    # Same priority — longer string wins (usually has more info)
    return a_val if len(a_val.strip()) >= len(b_val.strip()) else b_val


def _get_song_row(conn: sqlite3.Connection, song_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM songs WHERE id=? AND deleted_at IS NULL", (song_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"Song {song_id} not found or deleted")
    return row


def propose_merge(
    conn: sqlite3.Connection,
    source_song_id: int,
    target_song_id: int,
) -> dict:
    """Return preview dict without writing DB.

    Returns:
        {
            "source": {id, name, artist, album, plats: [...]},
            "target": {id, name, artist, album, plats: [...]},
            "recommended": {name, artist, album},
            "warnings": [...]
        }
    """
    if source_song_id == target_song_id:
        raise ValueError("source and target are the same song")

    src = _get_song_row(conn, source_song_id)
    tgt = _get_song_row(conn, target_song_id)

    src_links = get_links_for_song(conn, source_song_id)
    tgt_links = get_links_for_song(conn, target_song_id)
    src_plats = [l["platform"] for l in src_links if l["liked"] == 1]
    tgt_plats = [l["platform"] for l in tgt_links if l["liked"] == 1]

    # Recommended fields
    rec_name = _pick_better(src["name"], src["match_source"],
                            tgt["name"], tgt["match_source"])
    rec_artist = _pick_better(src["artist"], src["match_source"],
                              tgt["artist"], tgt["match_source"])
    rec_album = tgt["album"] or src["album"]  # always prefer target album

    # Warnings
    warnings = []
    overlap = set(src_plats) & set(tgt_plats)
    if overlap:
        warnings.append(
            f"无法合并:两首歌在 {', '.join(overlap)} 平台都有 link，会触发 UNIQUE 冲突"
        )
    if src["match_source"] == "manual":
        warnings.append("source 是手工绑定的，确认要合并吗?")
    src_synced = [l for l in src_links if l["synced_at"]]
    if src_synced:
        warnings.append("source 的 link 已被 sync 推送过，合并后 audit_log 仍可回退")

    return {
        "source": {
            "id": src["id"], "name": src["name"],
            "artist": src["artist"], "album": src["album"],
            "plats": src_plats,
        },
        "target": {
            "id": tgt["id"], "name": tgt["name"],
            "artist": tgt["artist"], "album": tgt["album"],
            "plats": tgt_plats,
        },
        "recommended": {
            "name": rec_name, "artist": rec_artist, "album": rec_album,
        },
        "warnings": warnings,
    }


def execute_merge(
    conn: sqlite3.Connection,
    *,
    source_song_id: int,
    target_song_id: int,
    name: str,
    artist: str,
    album: str | None,
    note: str | None = None,
) -> None:
    """Atomic merge in a single transaction.

    Steps:
      1. Read source song + links; validate.
      2. INSERT merge_log with source_payload + source_links snapshots.
      3. UPDATE platform_links SET song_id=target WHERE song_id=source.
      4. UPDATE target song: new fields + match_source='manual_merged' + recompute canonical_key.
      5. UPDATE source song: SET deleted_at=now.
    """
    if source_song_id == target_song_id:
        raise ValueError("source and target are the same song")

    src = _get_song_row(conn, source_song_id)
    tgt = _get_song_row(conn, target_song_id)
    ts = now_iso()

    # Read source links for audit snapshot
    src_links = get_links_for_song(conn, source_song_id)
    src_links_dicts = [dict(l) for l in src_links]

    # Check platform overlap (would violate UNIQUE)
    tgt_links = get_links_for_song(conn, target_song_id)
    tgt_plats = {l["platform"] for l in tgt_links}
    for l in src_links:
        if l["platform"] in tgt_plats:
            raise ValueError(
                f"Platform overlap: both songs have '{l['platform']}' link — "
                "would violate UNIQUE(platform, platform_track_id)"
            )

    # Build source payload snapshot
    source_payload = json.dumps({
        "id": src["id"],
        "canonical_key": src["canonical_key"],
        "name": src["name"],
        "artist": src["artist"],
        "album": src["album"],
        "match_source": src["match_source"],
        "original_match_source": src["original_match_source"],
        "created_at": src["created_at"],
        "updated_at": src["updated_at"],
        "deleted_at": src["deleted_at"],
    }, ensure_ascii=False)

    # Step 2: INSERT merge_log
    conn.execute(
        "INSERT INTO merge_log (source_song_id, target_song_id, source_payload, "
        "source_links, merged_at, note) VALUES (?, ?, ?, ?, ?, ?)",
        (source_song_id, target_song_id, source_payload,
         json.dumps(src_links_dicts, ensure_ascii=False), ts, note),
    )

    # Step 3: Transfer source links to target
    conn.execute(
        "UPDATE platform_links SET song_id=?, updated_at=? WHERE song_id=?",
        (target_song_id, ts, source_song_id),
    )

    # Step 4: Update target song
    new_ck = canonical_key(name, artist)
    conn.execute(
        "UPDATE songs SET name=?, artist=?, album=?, canonical_key=?, "
        "match_source='manual_merged', updated_at=? WHERE id=?",
        (name, artist, album, new_ck, ts, target_song_id),
    )

    # Step 5: Soft-delete source
    conn.execute(
        "UPDATE songs SET deleted_at=? WHERE id=?",
        (ts, source_song_id),
    )

    conn.commit()
