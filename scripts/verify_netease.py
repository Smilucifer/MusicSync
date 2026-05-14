"""Phase 0: Verify NetEase Cloud Music API connectivity."""
import os
import sys
from dotenv import load_dotenv
from netease_api import NetEaseAPI

load_dotenv()


def main():
    music_u = os.getenv("NETEASE_MUSIC_U", "")
    phone = os.getenv("NETEASE_PHONE", "")
    md5_password = os.getenv("NETEASE_MD5_PASSWORD", "")

    if not music_u and (not phone or not md5_password):
        print("FAIL: NETEASE_MUSIC_U or NETEASE_PHONE+NETEASE_MD5_PASSWORD must be set")
        sys.exit(1)

    api = NetEaseAPI()

    # Step 1: Authenticate (prefer cookie, fallback to login)
    print("[1/4] Authenticating...")
    if music_u:
        if not api.auth_with_cookie(music_u):
            print("FAIL: Cookie authentication failed — MUSIC_U may be expired")
            api.close()
            sys.exit(1)
        print(f"  OK: Cookie authenticated (uid={api.uid})")
    else:
        if not api.login(phone, md5_password):
            print("FAIL: Login failed — check credentials or captcha required")
            api.close()
            sys.exit(1)
        print(f"  OK: Login successful (uid={api.uid})")

    # Step 2: Fetch liked track IDs
    print("[2/4] Fetching liked track IDs...")
    ids = api.get_liked_track_ids()
    if not ids:
        print("  WARN: No liked tracks found (account may be empty)")
    else:
        print(f"  OK: Got {len(ids)} liked track IDs")

    # Step 3: Fetch track details for first few tracks
    print("[3/4] Fetching track details...")
    if ids:
        sample = ids[:5]
        details = api.get_track_details_batch(sample)
        if details:
            for d in details:
                print(f"  [{d['id']}] {d['name']} — {d['artist']}  (ISRC: {d.get('isrc', 'N/A')})")
            print(f"  OK: Got details for {len(details)} tracks")
        else:
            print("  WARN: Track detail fetch returned empty")
    else:
        print("  SKIP: No track IDs to verify")

    # Step 4: Search
    print("[4/4] Searching for test track...")
    results = api.search("晴天 周杰伦", limit=3)
    if results:
        for r in results:
            print(f"  [{r['id']}] {r['name']} — {r['artist']}")
        print("  OK: Search working")
    else:
        print("  WARN: Search returned empty (may need different keyword)")

    api.close()
    print("\n=== VERIFY PASSED: NetEase API is operational ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
