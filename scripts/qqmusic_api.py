"""QQ Music API wrapper with signing algorithms."""
import time
import httpx
from typing import Optional

from qqmusic_sign import simple_sign, zzb_sign, generate_g_tk

HEADERS_TEMPLATE = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://y.qq.com/",
}


class QQMusicAPI:
    """QQ Music favorites operations via HTTP + signing."""

    def __init__(self):
        self.client = httpx.Client(timeout=30)
        self.musickey: str = ""
        self.uin: str = ""

    def set_auth(self, musickey: str, uin: str):
        """Set authentication credentials after token refresh."""
        self.musickey = musickey
        self.uin = uin

    @property
    def g_tk(self) -> int:
        return generate_g_tk(self.musickey)

    def _cookie_header(self) -> str:
        return f"uin={self.uin}; qqmusic_key={self.musickey};"

    def _rate_limit(self):
        time.sleep(1)  # 1s between QQ Music API calls

    # --- Favorites API (c.y.qq.com — simple MD5 sign) ---

    def get_liked_tracks(self, offset: int = 0, limit: int = 100) -> list[dict]:
        """Get liked/favorite tracks with pagination."""
        params = {
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "g_tk": str(self.g_tk),
            "uin": self.uin,
            "loginUin": self.uin,
            "hostUin": "0",
            "dirid": "201",
            "p": str(offset),
            "num": str(limit),
        }
        params["sign"] = simple_sign(params)

        headers = {**HEADERS_TEMPLATE, "Cookie": self._cookie_header()}
        resp = self.client.get(
            "https://c.y.qq.com/fav/fcgi-bin/fav_get_favlist",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        self._rate_limit()

        body = resp.json()
        if body.get("code") != 0:
            return []

        songs = body.get("data", {}).get("songlist", [])
        return [
            {
                "id": str(s.get("songid", "")),
                "mid": s.get("songmid", ""),
                "name": s.get("songname", ""),
                "artist": (s.get("singer", [{}])[0].get("name", "")
                           if s.get("singer") else ""),
                "album": s.get("albumname", ""),
                "isrc": "",
            }
            for s in songs
        ]

    def get_all_liked_tracks(self) -> list[dict]:
        """Fetch all liked tracks with pagination."""
        all_tracks = []
        offset = 0
        page_size = 100
        while True:
            page = self.get_liked_tracks(offset=offset, limit=page_size)
            if not page:
                break
            all_tracks.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return all_tracks

    def add_to_favorites(self, songmid: str) -> bool:
        """Add a track to QQ Music favorites."""
        params = {
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "g_tk": str(self.g_tk),
            "uin": self.uin,
            "loginUin": self.uin,
        }
        params["sign"] = simple_sign(params)

        headers = {**HEADERS_TEMPLATE, "Cookie": self._cookie_header()}
        resp = self.client.post(
            "https://c.y.qq.com/fav/fcgi-bin/fav_add_song",
            params=params,
            data={"songmid": songmid, "dirid": "201"},
            headers=headers,
        )
        self._rate_limit()
        return resp.json().get("code") == 0

    def remove_from_favorites(self, songmid: str) -> bool:
        """Remove a track from QQ Music favorites."""
        params = {
            "format": "json",
            "inCharset": "utf-8",
            "outCharset": "utf-8",
            "platform": "yqq.json",
            "g_tk": str(self.g_tk),
            "uin": self.uin,
            "loginUin": self.uin,
        }
        params["sign"] = simple_sign(params)

        headers = {**HEADERS_TEMPLATE, "Cookie": self._cookie_header()}
        resp = self.client.post(
            "https://c.y.qq.com/fav/fcgi-bin/fav_del_song",
            params=params,
            data={"songmid": songmid, "dirid": "201"},
            headers=headers,
        )
        self._rate_limit()
        return resp.json().get("code") == 0

    # --- Search API (u.y.qq.com — zzb sign) ---

    def search_track(self, name: str, artist: str) -> Optional[dict]:
        """Search QQ Music for a track. Returns best match or None."""
        import json

        query_data = {
            "method": "DoSearchForQQMusicDesktop",
            "module": "music.search.SearchCgiService",
            "param": json.dumps({
                "searchid": "",
                "search_type": 0,
                "query": f"{name} {artist}",
                "num_per_page": 5,
                "page_num": 1,
                "grp": 1,
            }),
        }
        body_str = json.dumps(query_data)
        sign = zzb_sign(body_str)

        headers = {**HEADERS_TEMPLATE, "Cookie": self._cookie_header()}
        resp = self.client.post(
            f"https://u.y.qq.com/cgi-bin/musics.fcg?sign={sign}",
            data=body_str,
            headers=headers,
        )
        resp.raise_for_status()
        self._rate_limit()

        body = resp.json()
        songs = (
            body.get("data", {})
            .get("body", {})
            .get("song", {})
            .get("list", [])
        )
        if not songs:
            return None

        s = songs[0]
        return {
            "id": str(s.get("songid", "")),
            "mid": s.get("songmid", ""),
            "name": s.get("name", "") or s.get("songname", ""),
            "artist": (s.get("singer", [{}])[0].get("name", "")
                       if s.get("singer") else ""),
            "album": s.get("albumname", ""),
            "isrc": "",
        }

    def close(self):
        self.client.close()
