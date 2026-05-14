"""Phase 0: Verify QQ Music auth + liked tracks fetch via refresh_token."""
import os
import sys
import json
import httpx
from dotenv import load_dotenv
from qqmusic_sign import generate_g_tk, simple_sign

load_dotenv()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://y.qq.com/",
}


def refresh_musickey(refresh_token: str, uin: str) -> str | None:
    """Exchange refresh_token for a musickey via QQ Music auth API."""
    # QQ Music OAuth token refresh — endpoint determined from community docs
    url = "https://y.qq.com/oauth2/refresh_token"
    try:
        resp = httpx.post(
            url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "uin": uin,
            },
            headers=HEADERS,
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  FAIL: Token refresh HTTP {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        musickey = data.get("musickey") or data.get("access_token")
        if not musickey:
            print(f"  FAIL: No musickey in response: {json.dumps(data)[:500]}")
            return None
        return musickey
    except Exception as e:
        print(f"  FAIL: Token refresh exception: {e}")
        return None


def fetch_liked_tracks(musickey: str, uin: str) -> list[dict]:
    """Fetch the first page of QQ Music liked tracks."""
    g_tk = generate_g_tk(musickey)
    params = {
        "format": "json",
        "inCharset": "utf-8",
        "outCharset": "utf-8",
        "platform": "yqq.json",
        "g_tk": str(g_tk),
        "uin": uin,
        "loginUin": uin,
        "hostUin": "0",
        "dirid": "201",
        "p": "0",
        "num": "10",
    }
    params["sign"] = simple_sign(params)

    headers = {**HEADERS, "Cookie": f"uin={uin}; qqmusic_key={musickey};"}
    resp = httpx.get(
        "https://c.y.qq.com/fav/fcgi-bin/fav_get_favlist",
        params=params,
        headers=headers,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  FAIL: Fav list HTTP {resp.status_code}: {resp.text[:200]}")
        return []

    body = resp.json()
    if body.get("code") != 0:
        print(f"  FAIL: Fav list code={body.get('code')}: {json.dumps(body)[:300]}")
        return []

    return body.get("data", {}).get("songlist", [])


def main():
    refresh_token = os.getenv("QQMUSIC_REFRESH_TOKEN")
    uin = os.getenv("QQMUSIC_UIN")
    if not refresh_token or not uin:
        print("FAIL: QQMUSIC_REFRESH_TOKEN or QQMUSIC_UIN not set in .env")
        sys.exit(1)

    # Step 1: Refresh musickey
    print("[1/2] Refreshing musickey from refresh_token...")
    musickey = refresh_musickey(refresh_token, uin)
    if not musickey:
        sys.exit(1)
    print(f"  OK: Got musickey (len={len(musickey)})")

    # Step 2: Fetch liked tracks
    print("[2/2] Fetching liked tracks...")
    songs = fetch_liked_tracks(musickey, uin)
    print(f"  OK: Got {len(songs)} liked tracks")
    for s in songs[:5]:
        name = s.get("songname", "?")
        artist = (s.get("singer", [{}])[0].get("name", "?") if s.get("singer") else "?")
        print(f"  [{s.get('songid', '?')}] {name} — {artist}")

    print("\n=== VERIFY PASSED: QQ Music auth + liked tracks fetch works ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
