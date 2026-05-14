"""Helper to search QQ Music for a track and print its ID."""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from qqmusic_api import Client, Credential


async def search(keyword: str, limit: int = 5):
    musickey = os.getenv("QQMUSIC_KEY", "")
    uin_str = os.getenv("QQMUSIC_UIN", "")

    if not musickey or not uin_str:
        print("ERROR: QQMUSIC_KEY or QQMUSIC_UIN not set in .env")
        return []

    cred = Credential(musicid=int(uin_str), musickey=musickey)
    async with Client(credential=cred) as client:
        result = await client.search.search_by_type(keyword=keyword, num=limit)
        if not hasattr(result, "song") or not result.song:
            return []
        results = []
        for song in result.song:
            singer = song.singer[0].name if song.singer else ""
            results.append({
                "id": str(song.id),
                "name": song.name,
                "artist": singer,
                "album": song.album.name if song.album else "",
            })
        return results


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/search_qq.py <keyword>")
        print("  e.g. python scripts/search_qq.py '晴天 周杰伦'")
        sys.exit(1)

    keyword = sys.argv[1]
    results = asyncio.run(search(keyword))
    if not results:
        print("No results found.")
    else:
        for r in results:
            print(f"  {r['id']}  {r['name']} - {r['artist']}  [{r['album']}]")


if __name__ == "__main__":
    main()
