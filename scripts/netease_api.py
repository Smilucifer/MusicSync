"""NetEase Cloud Music API wrapper using direct HTTP + weapi encryption."""
import json
import time
import httpx
from typing import Optional

from weapi import weapi_encrypt, md5

BASE_URL = "https://music.163.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}


class NetEaseAPI:
    """NetEase Cloud Music liked-tracks operations via direct HTTP."""

    def __init__(self):
        self.client = httpx.Client(timeout=30)
        self.uid: Optional[int] = None
        self._cookie: str = ""

    def login(self, phone: str, password_md5: str) -> bool:
        """Login via cellphone + MD5 password. Returns True on success."""
        data = weapi_encrypt({
            "phone": phone,
            "password": password_md5,
            "rememberLogin": "true",
        })
        resp = self.client.post(
            f"{BASE_URL}/weapi/login/cellphone",
            data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            return False

        body = resp.json()
        if body.get("code") != 200:
            return False

        # Store cookies from Set-Cookie header
        cookies = resp.headers.get("set-cookie", "")
        self._cookie = self._parse_cookies(cookies)

        # Store user ID
        profile = body.get("profile") or body.get("account", {})
        self.uid = profile.get("userId") or profile.get("id")

        # Set cookie header for subsequent requests
        if self._cookie:
            self.client.headers.update({"Cookie": self._cookie})
        return True

    def _parse_cookies(self, set_cookie_header: str) -> str:
        """Extract key=value pairs from Set-Cookie headers."""
        if not set_cookie_header:
            return ""
        pairs = []
        for part in set_cookie_header.split(","):
            part = part.strip()
            for chunk in part.split(";"):
                chunk = chunk.strip()
                if "=" in chunk and not chunk.lower().startswith(("path", "domain", "expires", "max-age", "secure", "httponly", "samesite")):
                    pairs.append(chunk)
        return "; ".join(pairs)

    def _weapi_post(self, path: str, data: dict) -> dict:
        """Make a weapi-encrypted POST request."""
        encrypted = weapi_encrypt(data)
        resp = self.client.post(
            f"{BASE_URL}{path}",
            data=encrypted,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        return resp.json()

    def _rate_limit(self):
        time.sleep(0.5)

    def get_liked_track_ids(self) -> list[int]:
        """Get all liked track IDs for the logged-in user."""
        if not self.uid:
            return []

        result = self._weapi_post("/weapi/song/like/list", {"uid": str(self.uid)})
        self._rate_limit()

        if result.get("code") != 200:
            return []

        return result.get("ids", [])

    def get_track_details_batch(self, track_ids: list[int]) -> list[dict]:
        """Get track details for a batch of up to ~100 track IDs."""
        if not track_ids:
            return []

        # The API expects c as a JSON string of [{id: xxx}, ...]
        c = json.dumps([{"id": tid} for tid in track_ids])
        result = self._weapi_post("/weapi/v3/song/detail", {"c": c})
        self._rate_limit()

        if result.get("code") != 200:
            return []

        songs = result.get("songs", [])
        return [
            {
                "id": str(song.get("id", "")),
                "name": song.get("name", ""),
                "artist": (song.get("ar", [{}])[0].get("name", "")
                           if song.get("ar") else ""),
                "album": song.get("al", {}).get("name", ""),
                "isrc": song.get("no", ""),  # ISRC-like field; may be empty
            }
            for song in songs
        ]

    def get_all_liked_tracks(self) -> list[dict]:
        """Fetch all liked tracks with full metadata."""
        ids = self.get_liked_track_ids()
        if not ids:
            return []

        all_songs = []
        batch_size = 100
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            details = self.get_track_details_batch(batch)
            all_songs.extend(details)

        return all_songs

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        """Search for tracks by keyword."""
        data = weapi_encrypt({
            "s": keyword,
            "type": "1",  # 1 = single track
            "limit": str(limit),
            "offset": "0",
        })
        resp = self.client.post(
            f"{BASE_URL}/weapi/search/get",
            data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        self._rate_limit()
        if resp.status_code != 200:
            return []

        body = resp.json()
        if body.get("code") != 200:
            return []

        songs = body.get("result", {}).get("songs", [])
        return [
            {
                "id": str(song.get("id", "")),
                "name": song.get("name", ""),
                "artist": (song.get("ar", [{}])[0].get("name", "")
                           if song.get("ar") else ""),
                "album": song.get("al", {}).get("name", ""),
                "isrc": song.get("no", ""),
            }
            for song in songs
        ]

    def add_to_liked(self, track_id: str) -> bool:
        """Like/favorite a track by ID. (Phase 2: for bidirectional sync)"""
        data = weapi_encrypt({
            "trackId": track_id,
            "like": "true",
        })
        resp = self.client.post(
            f"{BASE_URL}/weapi/radio/like",
            data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        self._rate_limit()
        return resp.status_code == 200 and resp.json().get("code") == 200

    def remove_from_liked(self, track_id: str) -> bool:
        """Unfavorite a track. (Phase 2: tombstone-gated)"""
        data = weapi_encrypt({
            "trackId": track_id,
            "like": "false",
        })
        resp = self.client.post(
            f"{BASE_URL}/weapi/radio/like",
            data=data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        self._rate_limit()
        return resp.status_code == 200 and resp.json().get("code") == 200

    def close(self):
        self.client.close()
