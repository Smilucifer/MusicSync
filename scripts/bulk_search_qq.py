"""Bulk search QQ Music IDs for unmatched tracks in the template file."""
import asyncio
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from qqmusic_api import Client, Credential


async def search(keyword: str, limit: int = 3):
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


async def main():
    template_path = os.path.join(
        os.path.dirname(__file__), "..", "state", "manual_mappings_template.json"
    )
    if not os.path.exists(template_path):
        print("No template file found. Run sync.py first to generate it.")
        return

    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)

    if not template:
        print("Template is empty.")
        return

    print(f"Searching QQ Music for {len(template)} unmatched tracks...\n")

    # Try multiple keyword strategies for each track
    for ne_id, info in template.items():
        name = info["_name"]
        artist = info["_artist"]

        # Remove parenthetical notes in the name for cleaner search
        import re
        clean = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
        clean = re.sub(r"[【\[]([^】\]]*[】\]])", "", clean).strip()

        # Strategy 1: clean name + artist
        kw1 = f"{clean} {artist}"
        # Strategy 2: just clean name (有些歌名太长)
        kw2 = clean.split("—")[0].strip() if "—" in clean else clean

        results = []
        for kw in [kw1, kw2]:
            if not results:
                results = await search(kw, limit=3)

        print(f"{ne_id}  {name} — {artist}")
        if results:
            for r in results:
                print(f"  → {r['id']}  {r['name']} — {r['artist']}")
        else:
            print(f"  → NOT FOUND")
        print()


if __name__ == "__main__":
    asyncio.run(main())
