"""Song matching engine: L1 (ISRC) + L2 (lyrics+duration) + L3 (name+artist)."""
import re
from typing import Optional


def clean_name(name: str) -> str:
    """Normalize track name for comparison.

    Removes parentheticals, normalizes fullwidth/halfwidth punctuation,
    collapses whitespace.
    """
    if not name:
        return ""
    name = str(name)
    name = re.sub(r"\([^)]*\)", "", name)
    name = re.sub(r"\[[^\]]*\]", "", name)
    name = re.sub(r"（[^）]*）", "", name)
    name = re.sub(r"【[^】]*】", "", name)
    name = name.replace(" - ", " ").replace(" – ", " ")
    name = name.replace("／", "/").replace("：", ":")
    name = re.sub(r"\s+", " ", name).strip()
    return name.lower()


def clean_artist(artist: str) -> str:
    """Normalize artist name: lowercase, strip whitespace."""
    if not artist:
        return ""
    return str(artist).strip().lower()


def normalize_lyrics(raw: str) -> str:
    """Strip LRC tags, metadata, and blank lines, return plain lyrics text."""
    if not raw:
        return ""
    lines = raw.splitlines()
    clean = []
    for line in lines:
        # Strip all LRC tags: [00:00.00], [ti:...], [ar:...], [al:...], [by:...], etc.
        line = re.sub(r"\[[^\]]*\]", "", line).strip()
        # Strip metadata credit lines (Chinese and English)
        if re.match(r"^(作词|作曲|编曲|词|曲|唱|词曲|制作人|出品|演唱|混音|母带|录音|Lyrics\s+by|Composed\s+by|Programming|All\s+Instrument)\b", line, re.IGNORECASE):
            continue
        # Strip title-artist lines like "Eclipse - Aimer" or "チカっとチカ千花っ♡ - 小原好美"
        # Pattern: something + " - " + something, under 80 chars
        if re.match(r"^.{1,70}\s+[-–—]\s+.{1,30}$", line):
            continue
        if line:
            clean.append(line)
    return "\n".join(clean)


def lyrics_similarity(text_a: str, text_b: str) -> float:
    """Compare two normalized lyrics texts using sequence matching.
    Returns 0.0~1.0. Uses character-level comparison for robustness
    against formatting differences (whitespace, punctuation, line breaks)."""
    if not text_a or not text_b:
        return 0.0
    import difflib
    # Collapse to continuous strings for comparison
    a = re.sub(r"\s+", "", text_a)
    b = re.sub(r"\s+", "", text_b)
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def duration_match(dur_a: int, dur_b: int, tolerance: int = 3) -> bool:
    """Compare durations in seconds. Returns True if within tolerance."""
    if dur_a <= 0 or dur_b <= 0:
        return False
    return abs(dur_a - dur_b) <= tolerance


def match_l1(netease_track: dict, qq_track: dict) -> bool:
    """L1: ISRC exact match."""
    ne_isrc = str(netease_track.get("isrc", "")).strip().upper()
    qq_isrc = str(qq_track.get("isrc", "")).strip().upper()
    if ne_isrc and qq_isrc and ne_isrc == qq_isrc:
        return True
    return False


def match_l2(netease_track: dict, qq_track: dict) -> bool:
    """L2: Lyrics similarity >= 0.6 AND duration within 3s.

    Compares original lyrics AND translated lyrics.
    Match succeeds if either comparison passes.
    _lyrics format: (original_text, translated_text)
    """
    ne_orig, ne_trans = netease_track.get("_lyrics", ("", ""))
    qq_orig, qq_trans = qq_track.get("_lyrics", ("", ""))

    ne_dur = netease_track.get("duration", 0)
    qq_dur = qq_track.get("duration", 0)

    if not duration_match(ne_dur, qq_dur):
        return False

    # Check original lyrics similarity
    ne_orig_norm = normalize_lyrics(ne_orig)
    qq_orig_norm = normalize_lyrics(qq_orig)
    if ne_orig_norm and qq_orig_norm:
        if lyrics_similarity(ne_orig_norm, qq_orig_norm) >= 0.6:
            return True

    # Check translated lyrics similarity
    ne_trans_norm = normalize_lyrics(ne_trans)
    qq_trans_norm = normalize_lyrics(qq_trans)
    if ne_trans_norm and qq_trans_norm:
        if lyrics_similarity(ne_trans_norm, qq_trans_norm) >= 0.6:
            return True

    # Cross-check: original vs translated (in case one platform has original, other has translation)
    if ne_orig_norm and qq_trans_norm:
        if lyrics_similarity(ne_orig_norm, qq_trans_norm) >= 0.6:
            return True
    if ne_trans_norm and qq_orig_norm:
        if lyrics_similarity(ne_trans_norm, qq_orig_norm) >= 0.6:
            return True

    return False


def match_l3(netease_track: dict, qq_track: dict) -> bool:
    """L3: Cleaned name exact match + artist containment (low confidence)."""
    ne_name = clean_name(netease_track.get("name", ""))
    qq_name = clean_name(qq_track.get("name", ""))

    if not ne_name or not qq_name:
        return False
    if ne_name != qq_name:
        return False

    ne_artist = clean_artist(netease_track.get("artist", ""))
    qq_artist = clean_artist(qq_track.get("artist", ""))

    if not ne_artist or not qq_artist:
        return False

    if ne_artist in qq_artist or qq_artist in ne_artist:
        return True

    return False


def match_track(
    netease_track: dict,
    qq_search_results: list[dict],
) -> tuple[Optional[dict], str]:
    """Match a NetEase track against QQ Music search results.

    Returns (matched_qq_track, confidence_level) where confidence_level
    is "L1", "L2", "L3", or "" (no match).
    """
    for qq_track in qq_search_results:
        if match_l1(netease_track, qq_track):
            return qq_track, "L1"
        if match_l2(netease_track, qq_track):
            return qq_track, "L2"
        if match_l3(netease_track, qq_track):
            return qq_track, "L3"

    return None, ""
