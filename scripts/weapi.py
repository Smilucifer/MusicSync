"""NetEase Cloud Music weapi encryption implementation.

The weapi protocol encrypts JSON params with AES-128-CBC and RSA-encrypts the key.
Reference: Binaryify/NeteaseCloudMusicApi (community-documented protocol).
"""
import base64
import json
import os
import hashlib
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

# Fixed AES IV for weapi
_AES_IV = b"0102030405060708"

# NetEase weapi RSA public key (extracted from the web player, well-known constant)
_RSA_PUB_KEY = RSA.construct((
    int(
        "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725"
        "152b3ab17a876aea8a5aa76d2e417629ec4ee341f56135fccf695280104e0312"
        "ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b42"
        "4d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7",
        16,
    ),
    int("010001", 16),
))
_RSA_CIPHER = PKCS1_v1_5.new(_RSA_PUB_KEY)


def _random_key() -> bytes:
    """Generate a random 16-byte AES key using os.urandom."""
    return os.urandom(16)


def weapi_encrypt(data: dict) -> dict[str, str]:
    """Encrypt a dict of params into weapi form fields.

    Returns {"params": "<base64>", "encSecKey": "<hex>"} ready for POST.
    """
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")
    # Pad to 16-byte boundary (PKCS7-style via zero-padding is what NetEase expects)
    pad_len = 16 - len(payload) % 16
    payload += bytes([pad_len]) * pad_len

    key = _random_key()
    cipher = AES.new(key, AES.MODE_CBC, _AES_IV)
    encrypted = cipher.encrypt(payload)

    params = base64.b64encode(encrypted).decode("ascii")

    # encSecKey: reverse the key, hex-encode, RSA encrypt, then hex the result
    reversed_key = key[::-1]
    enc_sec_key = _RSA_CIPHER.encrypt(reversed_key)
    enc_sec_key = enc_sec_key.hex()

    return {"params": params, "encSecKey": enc_sec_key}


def md5(text: str) -> str:
    """MD5 hash (used for password in NetEase login)."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()
