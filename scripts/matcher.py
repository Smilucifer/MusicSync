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
    n = re.sub(r"（[^）]*）", "", n)
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
    if ck == "|":
        # No usable name+artist — skip canonical merge, create isolated song
        candidates = []
    else:
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
    a_orig, a_trans = a.get("_lyrics") or ("", "")
    b_orig, b_trans = b.get("_lyrics") or ("", "")
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
    row = conn.execute(
        "SELECT song_id FROM platform_links WHERE platform=? AND platform_track_id=?",
        (platform, platform_track_id),
    ).fetchone()
    if row is None:
        return None
    if row["song_id"] == current_song_id:
        return None
    return row["song_id"]
