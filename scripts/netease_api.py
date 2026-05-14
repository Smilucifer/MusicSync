"""NetEase Cloud Music API wrapper using MusicLibrary (pymusiclibrary)."""
import time
import random
from typing import Optional

from MusicLibrary.neteaseCloudMusicApi import NeteaseCloudMusicApi


class NetEaseAPI:
    """NetEase Cloud Music liked-tracks operations via MusicLibrary binding."""

    def __init__(self):
        self.api = NeteaseCloudMusicApi()
        self.uid: Optional[int] = None
        self._ready: bool = False

    def auth_with_cookie(self, music_u: str) -> bool:
        """Authenticate using a browser-exported MUSIC_U cookie."""
        self.api.set_cookie({"MUSIC_U": music_u})
        result = self.api.request("/login/status")
        body = result.body if isinstance(result.body, dict) else {}

        # Response structure: {"data": {"code": 200, "profile": {...}}}
        inner = body.get("data", body)
        if inner.get("code") == 200:
            profile = inner.get("profile", {})
            self.uid = profile.get("userId") or inner.get("account", {}).get("id")
            if self.uid:
                self._ready = True
                return True

        return False

    def login(self, phone: str, password_md5: str) -> bool:
        """Login via cellphone + MD5 password (may trigger captcha)."""
        result = self.api.request(
            "/login/cellphone",
            phone=phone,
            md5_password=password_md5,
            countrycode="86",
        )
        body = result.body if isinstance(result.body, dict) else {}
        if result.status != 200 or body.get("code") != 200:
            return False

        profile = body.get("profile") or body.get("account", {})
        self.uid = profile.get("userId") or profile.get("id")
        self._ready = True
        return True

    def _rate_limit(self):
        time.sleep(0.5)

    def _request(self, path: str, **params) -> dict:
        result = self.api.request(path, **params)
        self._rate_limit()
        return result.body if isinstance(result.body, dict) else {}

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
        self.api.destroy()
