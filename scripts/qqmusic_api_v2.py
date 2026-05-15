"""QQ Music API wrapper using qqmusic-api-python library."""
import asyncio
import os
from typing import Optional

from qqmusic_api import Client, Credential


class QQMusicAPI:
    """QQ Music liked-tracks operations via qqmusic-api-python."""

    def __init__(self):
        self.musickey = os.getenv("QQMUSIC_KEY", "")
        self.musicid = int(os.getenv("QQMUSIC_UIN", "0"))
        self.euin = os.getenv("QQMUSIC_EUIN", "")
        self._ready = bool(self.musickey and self.musicid and self.euin)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def uid(self) -> Optional[int]:
        return self.musicid if self._ready else None

    # --- Sync wrappers ---

    def get_all_liked_tracks(self) -> list[dict]:
        if not self._ready:
            return []
        return asyncio.run(self._get_all_liked_async())

    def search(self, keyword: str, limit: int = 10) -> list[dict]:
        if not self._ready:
            return []
        return asyncio.run(self._search_async(keyword, limit))

    def add_to_liked(self, track_id: str) -> bool:
        if not self._ready:
            return False
        return asyncio.run(self._add_to_liked_async(int(track_id)))

    def remove_from_liked(self, track_id: str) -> bool:
        if not self._ready:
            return False
        return asyncio.run(self._del_from_liked_async(int(track_id)))

    # --- Async implementations ---

    async def _get_all_liked_async(self) -> list[dict]:
        all_songs = []
        page = 1
        async with Client(credential=self._credential()) as client:
            while True:
                result = await client.user.get_fav_song(
                    euin=self.euin, page=page, num=100,
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
                        "duration": song.interval if hasattr(song, "interval") else 0,
                    })

                if not result.hasmore:
                    break
                page += 1
        return all_songs

    async def _search_async(self, keyword: str, limit: int = 10) -> list[dict]:
        async with Client(credential=self._credential()) as client:
            result = await client.search.search_by_type(keyword=keyword, num=limit)
            if not hasattr(result, "song") or not result.song:
                return []
            return [
                {
                    "id": str(song.id),
                    "name": song.name,
                    "artist": song.singer[0].name if song.singer else "",
                    "album": song.album.name if song.album else "",
                    "mid": song.mid,
                    "duration": song.interval if hasattr(song, "interval") else 0,
                }
                for song in result.song
            ]

    async def _add_to_liked_async(self, song_id: int) -> bool:
        async with Client(credential=self._credential()) as client:
            return await client.songlist.add_songs(
                dirid=201,
                song_info=[(song_id, 1)],
            )

    async def _del_from_liked_async(self, song_id: int) -> bool:
        async with Client(credential=self._credential()) as client:
            return await client.songlist.del_songs(
                dirid=201,
                song_info=[(song_id, 1)],
            )

    async def _get_lyric_async(self, song_id: int) -> tuple[str, str]:
        """Fetch lyrics for a track. Returns (original, translated) plain text."""
        import re

        def strip_lrc(raw: str) -> str:
            if not raw:
                return ""
            lines = raw.splitlines()
            clean = [re.sub(r"\[\d+:\d+\.\d+\]", "", line).strip() for line in lines]
            return "\n".join(line for line in clean if line)

        async with Client(credential=self._credential()) as client:
            try:
                result = await client.lyric.get_lyric(song_id, trans=True)
                # Always try decryption — some tracks have encrypted lyrics
                # even when crypt field doesn't indicate it
                crypt_val = getattr(result, "crypt", 0)
                if crypt_val == 1:
                    result = result.decrypt()
                original = strip_lrc(result.lyric if hasattr(result, "lyric") else "")
                translated = strip_lrc(result.trans if hasattr(result, "trans") else "")
                # Debug: log crypt value for first few tracks
                return original, translated
            except Exception:
                return "", ""

    def get_lyric(self, track_id: str) -> tuple[str, str]:
        """Sync wrapper for lyrics fetching. Returns (original, translated)."""
        if not self._ready:
            return "", ""
        return asyncio.run(self._get_lyric_async(int(track_id)))

    def _credential(self) -> Credential:
        return Credential(musicid=self.musicid, musickey=self.musickey)

    def close(self):
        pass
