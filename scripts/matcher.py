"""Song matching engine: L1 (ISRC) + L2 (cleaned name + artist containment)."""
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


def match_l1(netease_track: dict, qq_track: dict) -> bool:
    """L1: ISRC exact match."""
    ne_isrc = str(netease_track.get("isrc", "")).strip().upper()
    qq_isrc = str(qq_track.get("isrc", "")).strip().upper()
    if ne_isrc and qq_isrc and ne_isrc == qq_isrc:
        return True
    return False


def match_l2(netease_track: dict, qq_track: dict) -> bool:
    """L2: Cleaned name exact match + artist containment check."""
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
    is "L1", "L2", or "" (no match).
    """
    for qq_track in qq_search_results:
        if match_l1(netease_track, qq_track):
            return qq_track, "L1"
        if match_l2(netease_track, qq_track):
            return qq_track, "L2"

    return None, ""
