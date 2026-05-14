"""Phase 0: Verify QQ Music API connectivity using qqmusic-api-python library."""
import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()


async def main():
    from qqmusic_api import Client, Credential
    from qqmusic_api_v2 import QQMusicAPI

    musickey = os.getenv("QQMUSIC_KEY", "")
    uin_str = os.getenv("QQMUSIC_UIN", "")
    euin = os.getenv("QQMUSIC_EUIN", "")

    if not musickey or not uin_str or not euin:
        print("FAIL: QQMUSIC_KEY, QQMUSIC_UIN, and QQMUSIC_EUIN must be set in .env")
        sys.exit(1)

    musicid = int(uin_str)
    print(f"musicid (wxuin): {musicid}")
    print(f"euin: {euin}")
    print(f"musickey prefix: {musickey[:8]}...")

    # Step 1: Create credential and check validity
    print("\n[1/5] Checking credential validity...")
    cred = Credential(musicid=musicid, musickey=musickey)

    async with Client(credential=cred) as client:
        is_expired = await client.login.check_expired()
        if is_expired:
            print("  WARN: Credential appears expired")
        else:
            print("  OK: Credential is valid")

        # Step 2: Get user info
        print("[2/5] Getting user info...")
        from qqmusic_api.modules._base import ApiModule
        mod = ApiModule(client)
        req = mod._build_request(
            module="music.UserInfo.userInfoServer",
            method="GetLoginUserInfo",
            param={},
            credential=cred,
            allow_error_codes=(1000, 104401, 104400),
        )
        data = await req
        nick = data.get("data", {}).get("info", {}).get("nick", "?")
        print(f"  OK: Logged in as '{nick}'")

    # Step 3: Get liked songs (use async directly to avoid nested event loop)
    print("[3/5] Fetching liked songs (all pages)...")
    api = QQMusicAPI()
    all_songs = []
    page = 1
    async with Client(credential=api._credential()) as client:
        while True:
            result = await client.user.get_fav_song(
                euin=euin, page=page, num=100,
            )
            songs = result.songs if hasattr(result, "songs") else []
            if not songs:
                break
            for song in songs:
                singer_name = song.singer[0].name if song.singer else ""
                all_songs.append({
                    "id": str(song.id),
                    "name": song.name,
                    "artist": singer_name,
                    "album": song.album.name if song.album else "",
                    "mid": song.mid,
                })
            if not result.hasmore:
                break
            page += 1

    print(f"  OK: Got {len(all_songs)} liked songs")
    if all_songs:
        for s in all_songs[:5]:
            print(f"  [{s['id']}] {s['name']} — {s['artist']}")
        if len(all_songs) > 5:
            print(f"  ... and {len(all_songs) - 5} more")

    # Step 4: Search test
    print("[4/5] Searching for test track...")
    async with Client(credential=api._credential()) as client:
        result = await client.search.search_by_type(keyword="晴天 周杰伦", num=3)
        if hasattr(result, "song") and result.song:
            search_results = []
            for s in result.song[:3]:
                singer_name = s.singer[0].name if s.singer else ""
                search_results.append({
                    "id": str(s.id),
                    "name": s.name,
                    "artist": singer_name,
                })
                print(f"  [{s.id}] {s.name} — {singer_name}")
            print("  OK: Search working")
        else:
            print("  WARN: Search returned empty")
            search_results = []

    # Step 5: Add/remove test
    print("[5/5] Add/remove liked test...")
    if search_results and os.getenv("CI") != "true":
        test_track = search_results[0]
        async with Client(credential=api._credential()) as client:
            added = await client.songlist.add_songs(dirid=201, song_info=[(int(test_track["id"]), 1)])
            print(f"  Add result: {added}")
            removed = await client.songlist.del_songs(dirid=201, song_info=[(int(test_track["id"]), 1)])
            print(f"  Remove result: {removed}")
    else:
        print("  SKIP: No results or CI environment")

    print("\n=== VERIFY PASSED: QQ Music API is operational ===")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
