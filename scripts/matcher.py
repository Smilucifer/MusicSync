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
    """Strip LRC timestamps and blank lines, return plain text."""
    if not raw:
        return ""
    lines = raw.splitlines()
    clean = []
    for line in lines:
        line = re.sub(r"\[\d+:\d+\.\d+\]", "", line).strip()
        if line:
            clean.append(line)
    return "\n".join(clean)


def lyrics_similarity(text_a: str, text_b: str) -> float:
    """Compare two normalized lyrics texts. Returns 0.0~1.0."""
    if not text_a or not text_b:
        return 0.0
    lines_a = set(text_a.splitlines())
    lines_b = set(text_b.splitlines())
    if not lines_a or not lines_b:
        return 0.0
    intersection = lines_a & lines_b
    union = lines_a | lines_b
    return len(intersection) / len(union) if union else 0.0


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

    Both tracks must have lyrics and duration for this to match.
    """
    ne_lyrics = normalize_lyrics(netease_track.get("_lyrics", ""))
    qq_lyrics = normalize_lyrics(qq_track.get("_lyrics", ""))
    ne_dur = netease_track.get("duration", 0)
    qq_dur = qq_track.get("duration", 0)

    if not ne_lyrics or not qq_lyrics:
        return False
    if not duration_match(ne_dur, qq_dur):
        return False
    if lyrics_similarity(ne_lyrics, qq_lyrics) >= 0.6:
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
