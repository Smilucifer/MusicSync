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
                crypt_val = getattr(result, "crypt", 0)
                raw_lyric = result.lyric if hasattr(result, "lyric") else ""
                raw_trans = result.trans if hasattr(result, "trans") else ""

                # Always try to decrypt if lyrics look like hex (encrypted)
                need_decrypt = crypt_val == 1
                if not need_decrypt and raw_lyric:
                    stripped = raw_lyric.replace("\n", "").replace(" ", "")
                    if stripped and all(c in "0123456789abcdefABCDEF" for c in stripped) and len(stripped) > 20:
                        need_decrypt = True

                if need_decrypt:
                    try:
                        decrypted = result.decrypt()
                        original = strip_lrc(decrypted.lyric if hasattr(decrypted, "lyric") else "")
                        translated = strip_lrc(decrypted.trans if hasattr(decrypted, "trans") else "")
                    except Exception:
                        # Standard decrypt failed — try qrc=True parameter
                        try:
                            qrc_result = await client.lyric.get_lyric(song_id, qrc=True, trans=True)
                            qrc_crypt = getattr(qrc_result, "crypt", 0)
                            if qrc_crypt == 1:
                                qrc_decrypted = qrc_result.decrypt()
                                original = strip_lrc(qrc_decrypted.lyric if hasattr(qrc_decrypted, "lyric") else "")
                                translated = strip_lrc(qrc_decrypted.trans if hasattr(qrc_decrypted, "trans") else "")
                            else:
                                original = strip_lrc(qrc_result.lyric if hasattr(qrc_result, "lyric") else "")
                                translated = strip_lrc(qrc_result.trans if hasattr(qrc_result, "trans") else "")
                        except Exception:
                            # All decryption attempts failed — return empty
                            # (L3 name+artist will be used as fallback)
                            original = ""
                            translated = ""
                else:
                    original = strip_lrc(raw_lyric)
                    translated = strip_lrc(raw_trans)

                # Debug: show crypt status for encrypted tracks
                if need_decrypt:
                    print(f"  [QQ] song={song_id} crypt={crypt_val} raw={raw_lyric[:50]!r} → orig={original[:50]!r}")
                return original, translated
            except Exception as e:
                print(f"  [QQ] song={song_id} error: {e}")
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
