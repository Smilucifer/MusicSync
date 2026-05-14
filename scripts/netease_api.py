"""NetEase Cloud Music API wrapper using NeteaseCloudMusicApi (Node.js) service."""
import time
from typing import Optional

import requests


class NetEaseAPI:
    """NetEase Cloud Music liked-tracks operations via HTTP API."""

    def __init__(self, base_url="http://localhost:3000"):
        self.base_url = base_url
        self.uid: Optional[int] = None
        self._ready: bool = False
        self._session = requests.Session()
        self._timeout = 15

    def auth_with_cookie(self, music_u: str) -> bool:
        """Authenticate using a browser-exported MUSIC_U cookie."""
        self._session.cookies.set("MUSIC_U", music_u)
        try:
            response = self._session.get(
                f"{self.base_url}/login/status", timeout=self._timeout
            )
            data = response.json()
            # Enhanced API wraps login/status response in {"data": {...}}
            login_data = data.get("data", data)
            if login_data.get("code") == 200:
                profile = login_data.get("profile", {})
                self.uid = profile.get("userId")
                if self.uid:
                    self._ready = True
                    return True
        except Exception as e:
            print(f"Auth failed: {e}")
        return False

    def login(self, phone: str, password_md5: str) -> bool:
        """Login via cellphone + MD5 password (may trigger captcha)."""
        try:
            response = self._session.get(
                f"{self.base_url}/login/cellphone",
                params={
                    "phone": phone,
                    "md5_password": password_md5,
                    "countrycode": "86",
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
            login_data = data.get("data", data)
            if login_data.get("code") == 200:
                profile = login_data.get("profile") or login_data.get("account", {})
                self.uid = profile.get("userId") or profile.get("id")
                if self.uid:
                    self._ready = True
                    return True
        except Exception as e:
            print(f"Login failed: {e}")
        return False

    def _rate_limit(self):
        time.sleep(0.5)

    def _request(self, path: str, method: str = "GET", **params) -> dict:
        try:
            url = f"{self.base_url}{path}"
            if method.upper() == "POST":
                response = self._session.post(
                    url, data=params, timeout=self._timeout,
                )
            else:
                response = self._session.get(
                    url, params=params, timeout=self._timeout,
                )
            response.raise_for_status()
            self._rate_limit()
            data = response.json()
            # Enhanced API wraps some responses in {"data": {...}}
            return data.get("data", data)
        except Exception as e:
            print(f"Request failed: {e}")
            return {}

    def get_liked_track_ids(self) -> list[int]:
        if not self.uid:
            return []

        result = self._request("/likelist", uid=str(self.uid))
        if result.get("code") != 200:
            return []

        return result.get("ids", [])

    def get_track_details_batch(self, track_ids: list[int]) -> list[dict]:
        if not track_ids:
            return []

        ids_str = ",".join(str(tid) for tid in track_ids)
        result = self._request("/song/detail", ids=ids_str)

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
                "isrc": song.get("no", ""),
            }
            for song in songs
        ]

    def get_all_liked_tracks(self) -> list[dict]:
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
        result = self._request(
            "/search",
            method="POST",
            keywords=keyword,
            type="1",
            limit=str(limit),
        )
        if result.get("code") != 200:
            return []

        songs = result.get("result", {}).get("songs", [])
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
        result = self._request("/like", id=track_id, like="true")
        return result.get("code") == 200

    def remove_from_liked(self, track_id: str) -> bool:
        result = self._request("/like", id=track_id, like="false")
        return result.get("code") == 200

    def close(self):
        self._session.close()
