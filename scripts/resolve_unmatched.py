"""Search QQ Music for unmatched tracks and auto-resolve high-confidence matches."""
import asyncio
import os
import re
import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(__file__))
from csv_database import load_mappings, save_mappings

from dotenv import load_dotenv
load_dotenv()

from qqmusic_api import Client, Credential


async def search(keyword: str, limit: int = 5):
    musickey = os.getenv("QQMUSIC_KEY", "")
    uin_str = os.getenv("QQMUSIC_UIN", "")
    if not musickey or not uin_str:
        return []
    cred = Credential(musicid=int(uin_str), musickey=musickey)
    async with Client(credential=cred) as client:
        result = await client.search.search_by_type(keyword=keyword, num=limit)
        if not hasattr(result, "song") or not result.song:
            return []
        return [
            {
                "id": str(song.id),
                "name": song.name,
                "artist": song.singer[0].name if song.singer else "",
            }
            for song in result.song
        ]


def clean_name(name: str) -> str:
    """Remove parenthetical notes and normalize for comparison."""
    s = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
    s = re.sub(r"[【\[][^】\]]*[】\]]", "", s).strip()
    s = re.sub(r"（Cover[^）]*）", "", s)
    s = re.sub(r"\(Cover[^)]*\)", "", s)
    return s


def fuzzy_match(track_name: str, track_artist: str, results: list):
    """Try to find the best match among search results."""
    clean_ne_name = clean_name(track_name).lower().strip()
    clean_ne_artist = clean_name(track_artist).lower().strip() if track_artist else ""

    for r in results:
        r_name = r["name"].lower().strip()
        r_artist = r["artist"].lower().strip() if r["artist"] else ""

        # Exact match on both
        if r_name == clean_ne_name and r_artist == clean_ne_artist:
            return r, "exact"

        # Name contains each other + artist matches
        if (clean_ne_name in r_name or r_name in clean_ne_name) and r_artist == clean_ne_artist:
            return r, "name_contains"

        # Name exact match, artist different (cover/remix)
        if r_name == clean_ne_name:
            return r, "name_exact"

    # fallback: first result if name similarity > threshold
    if results:
        r = results[0]
        r_name = clean_name(r["name"]).lower().strip()
        # Check word overlap
        ne_words = set(clean_ne_name.split())
        r_words = set(r_name.split())
        if ne_words and r_words:
            overlap = len(ne_words & r_words) / len(ne_words)
            if overlap >= 0.5:
                return r, f"partial_{overlap:.0%}"

    return None, None


async def main():
    mappings = load_mappings()
    unmatched = [
        (k, v) for k, v in mappings.items()
        if not k.startswith("_") and v.get("match_source") == "unmatched"
    ]
    print(f"Unmatched tracks: {len(unmatched)}\n")

    found = 0
    still_unmatched = 0

    for ne_id, row in unmatched:
        name = row["ne_name"]
        artist = row["ne_artist"]

        # Build search keywords
        clean = clean_name(name)
        kws = [f"{clean} {artist}"]
        if "—" in clean:
            kws.append(clean.split("—")[0].strip())
        if "cover" in name.lower() or "翻自" in name:
            kws.append(clean)

        results = []
        for kw in kws:
            if not results:
                results = await search(kw, limit=5)

        match, level = fuzzy_match(name, artist, results)

        if match and level:
            row["qq_id"] = match["id"]
            row["qq_name"] = match["name"]
            row["qq_artist"] = match["artist"]
            row["match_source"] = "manual"
            print(f"OK [{level}] {name} — {artist}")
            print(f"   → QQ {match['id']}  {match['name']} — {match['artist']}")
            found += 1
        else:
            print(f"?? {name} — {artist}")
            if results:
                for r in results[:3]:
                    print(f"   ? {r['id']}  {r['name']} — {r['artist']}")
            else:
                print(f"   → NO RESULTS")
            still_unmatched += 1

    print(f"\n---")
    print(f"Resolved: {found}  Still unmatched: {still_unmatched}")
    save_mappings(mappings)
    print("CSV saved.")


if __name__ == "__main__":
    asyncio.run(main())
