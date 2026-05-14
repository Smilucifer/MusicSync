"""Authentication manager for NetEase and QQ Music."""
import os
from dataclasses import dataclass
from typing import Optional

from netease_api import NetEaseAPI
from qqmusic_api_v2 import QQMusicAPI


@dataclass
class AuthResult:
    success: bool
    error: str = ""


class AuthManager:
    """Manages authentication for both music platforms."""

    def __init__(self):
        self._netease: Optional[NetEaseAPI] = None
        self._qq: Optional[QQMusicAPI] = None

    # --- NetEase ---

    def netease_login(self) -> AuthResult:
        """Authenticate NetEase via MUSIC_U cookie or phone login."""
        music_u = os.getenv("NETEASE_MUSIC_U", "")
        phone = os.getenv("NETEASE_PHONE", "")
        md5_pw = os.getenv("NETEASE_MD5_PASSWORD", "")

        if not music_u and (not phone or not md5_pw):
            return AuthResult(False, "NETEASE_MUSIC_U or NETEASE_PHONE+NETEASE_MD5_PASSWORD not set")

        api = NetEaseAPI()

        if music_u:
            if not api.auth_with_cookie(music_u):
                api.close()
                return AuthResult(False, "NetEase cookie auth failed — MUSIC_U may be expired")
        else:
            if not api.login(phone, md5_pw):
                api.close()
                return AuthResult(False, "NetEase login failed (captcha may be required)")

        self._netease = api
        return AuthResult(True)

    def get_netease_api(self) -> Optional[NetEaseAPI]:
        """Get authenticated NetEase API instance, logging in if needed."""
        if not self._netease:
            result = self.netease_login()
            if not result.success:
                return None
        return self._netease

    # --- QQ Music ---

    def qqmusic_login(self) -> AuthResult:
        """Create QQ Music API instance using QQMUSIC_KEY + QQMUSIC_UIN + QQMUSIC_EUIN."""
        api = QQMusicAPI()
        if not api.ready:
            return AuthResult(False, "QQMUSIC_KEY, QQMUSIC_UIN, or QQMUSIC_EUIN not set")

        self._qq = api
        return AuthResult(True)

    def get_qq_api(self) -> Optional[QQMusicAPI]:
        """Get QQ Music API instance, logging in if needed."""
        if not self._qq:
            result = self.qqmusic_login()
            if not result.success:
                return None
        return self._qq

    # --- Pre-check ---

    def pre_check(self) -> list[str]:
        """Run auth pre-check before sync. Returns list of error messages."""
        errors = []

        ne_result = self.netease_login()
        if not ne_result.success:
            errors.append(f"NetEase: {ne_result.error}")

        qq_result = self.qqmusic_login()
        if not qq_result.success:
            errors.append(f"QQ Music: {qq_result.error}")

        return errors

    def close(self):
        if self._netease:
            self._netease.close()
