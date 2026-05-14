"""Authentication manager for NetEase and QQ Music."""
import os
import httpx
from dataclasses import dataclass
from typing import Optional

from netease_api import NetEaseAPI


@dataclass
class AuthResult:
    success: bool
    error: str = ""
    musickey: Optional[str] = None
    uin: Optional[str] = None


class AuthManager:
    """Manages authentication for both music platforms."""

    def __init__(self):
        self.qq_refresh_token = os.getenv("QQMUSIC_REFRESH_TOKEN", "")
        self.qq_uin = os.getenv("QQMUSIC_UIN", "")
        self._netease: Optional[NetEaseAPI] = None
        self._qq_musickey: Optional[str] = None

    # --- NetEase ---

    def netease_login(self) -> AuthResult:
        """Login to NetEase via cellphone + MD5 password."""
        phone = os.getenv("NETEASE_PHONE", "")
        md5_pw = os.getenv("NETEASE_MD5_PASSWORD", "")
        if not phone or not md5_pw:
            return AuthResult(False, "NETEASE_PHONE or NETEASE_MD5_PASSWORD not set")

        api = NetEaseAPI()
        if not api.login(phone, md5_pw):
            api.close()
            return AuthResult(False, "NetEase login failed")

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

    def qqmusic_refresh(self) -> AuthResult:
        """Refresh QQ Music musickey via refresh_token."""
        if not self.qq_refresh_token or not self.qq_uin:
            return AuthResult(False, "QQMUSIC_REFRESH_TOKEN or QQMUSIC_UIN not set")

        try:
            resp = httpx.post(
                "https://y.qq.com/oauth2/refresh_token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self.qq_refresh_token,
                    "uin": self.qq_uin,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                return AuthResult(
                    False, f"QQ token refresh HTTP {resp.status_code}"
                )

            data = resp.json()
            musickey = data.get("musickey") or data.get("access_token")
            if not musickey:
                return AuthResult(
                    False,
                    f"No musickey in response",
                )

            self._qq_musickey = musickey
            return AuthResult(True, musickey=musickey, uin=self.qq_uin)
        except Exception as e:
            return AuthResult(False, f"QQ token refresh exception: {e}")

    def get_qq_credentials(self):
        """Get (musickey, uin), refreshing if needed."""
        if not self._qq_musickey:
            result = self.qqmusic_refresh()
            if not result.success:
                return None, None
        return self._qq_musickey, self.qq_uin

    # --- Pre-check ---

    def pre_check(self) -> list[str]:
        """Run auth pre-check before sync. Returns list of error messages."""
        errors = []

        ne_result = self.netease_login()
        if not ne_result.success:
            errors.append(f"NetEase: {ne_result.error}")

        qq_result = self.qqmusic_refresh()
        if not qq_result.success:
            errors.append(f"QQ Music: {qq_result.error}")

        return errors

    def close(self):
        if self._netease:
            self._netease.close()
