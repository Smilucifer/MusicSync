"""Test if QQ Music get_detail() returns ISRC in extras field."""
import asyncio
import os
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv()

from qqmusic_api import Client, Credential


async def main():
    musickey = os.getenv("QQMUSIC_KEY", "")
    uin_str = os.getenv("QQMUSIC_UIN", "")
    cred = Credential(musicid=int(uin_str), musickey=musickey)

    # Sample QQ song IDs to test (popular ones more likely to have ISRC)
    test_mids = [
        "001UjFd14MQEjF",  # popular Chinese song
        "002Nkxnf2LQ0vH",  # another popular song
    ]

    async with Client(credential=cred) as client:
        # First try get_detail with a song ID from liked tracks
        result = await client.user.get_fav_song(euin=os.getenv("QQMUSIC_EUIN", ""), page=1, num=3)
        songs = result.songs if hasattr(result, "songs") else []

        for song in songs:
            mid = song.mid
            print(f"\n--- Testing: {song.name} (id={song.id}, mid={mid}) ---")

            detail = await client.song.get_detail(mid)
            track = detail.track if hasattr(detail, "track") else None

            if hasattr(detail, "extras"):
                print(f"extras dict: {detail.extras}")
            else:
                print("No extras field")

            if track:
                # Check all available fields on the track model
                fields = ["id", "mid", "name", "type", "title", "subtitle", "label",
                          "isonly", "language", "genre", "status", "time_public",
                          "bpm", "ov", "sa", "es", "interval"]
                for f in fields:
                    val = getattr(track, f, None)
                    if val is not None and val != "":
                        print(f"  track.{f}: {val}")

                # Check vs, vi, vf lists
                for list_field in ["vs", "vi", "vf"]:
                    val = getattr(track, list_field, None)
                    if val:
                        print(f"  track.{list_field}: {val}")

            # Also check raw response (if accessible)
            if hasattr(detail, "model_dump"):
                import json
                dumped = detail.model_dump()
                # Search for ISRC-like patterns in the dumped data
                dump_str = json.dumps(dumped, ensure_ascii=False, default=str)
                isrc_matches = []
                for field_name in dumped.keys():
                    val = str(dumped[field_name])
                    if any(code in val.upper() for code in ["ISRC", "CN", "US", "JP", "KR", "FR"]) and len(val) < 100:
                        isrc_matches.append(f"  {field_name}: {val}")
                if isrc_matches:
                    print("  Potential ISRC candidates:")
                    for m in isrc_matches:
                        print(m)

                # Look specifically in extras
                extras = dumped.get("extras", {})
                if extras:
                    print(f"  extras keys: {list(extras.keys())}")
                    for k, v in extras.items():
                        print(f"    extras['{k}']: {v}")

            break  # Just test one for now


if __name__ == "__main__":
    asyncio.run(main())
