"""NetEase Cloud Music weapi encryption.

The weapi protocol: first AES with preset key, then AES with random key,
then RSA-encrypt the random key. Reference: Binaryify/NeteaseCloudMusicApi.
"""
import base64
import json
import os
import hashlib
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

# Fixed constants
_AES_IV = b"0102030405060708"
_PRESET_KEY = b"0CoJUm6Qyw8W8jud"

# NetEase weapi RSA public key (well-known constant from web player)
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
    return os.urandom(16)


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    """AES-128-CBC encrypt with PKCS7 padding."""
    cipher = AES.new(key, AES.MODE_CBC, _AES_IV)
    return cipher.encrypt(pad(data, AES.block_size))


def weapi_encrypt(data: dict) -> dict[str, str]:
    """Encrypt params into weapi form fields.

    Layer 1: AES encrypt JSON with preset key → base64 string
    Layer 2: AES encrypt the base64 string with random key → base64 string
    Then RSA-encrypt the reversed random key for encSecKey.

    Returns {"params": "<base64>", "encSecKey": "<hex>"}.
    """
    payload = json.dumps(data, separators=(",", ":")).encode("utf-8")

    # Layer 1: AES with preset key → base64
    first = _aes_encrypt(payload, _PRESET_KEY)
    first_b64 = base64.b64encode(first).decode("ascii")

    # Layer 2: AES encrypt the base64 string with random key → base64
    secret_key = _random_key()
    second = _aes_encrypt(first_b64.encode("utf-8"), secret_key)

    params = base64.b64encode(second).decode("ascii")

    # RSA encrypt the reversed secret key
    enc_sec_key = _RSA_CIPHER.encrypt(secret_key[::-1])
    enc_sec_key = enc_sec_key.hex()

    return {"params": params, "encSecKey": enc_sec_key}


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()
