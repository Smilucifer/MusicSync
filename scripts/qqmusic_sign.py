"""QQ Music API signing algorithms.

Two signing methods:
1. zzb sign (from GoMusic) — used for u.y.qq.com API endpoints
2. Simple MD5 sign — used for legacy c.y.qq.com favorites endpoints
"""
import hashlib
import re


# --- zzb sign (GoMusic algorithm for u.y.qq.com API) ---

_K1 = {
    "0": 0, "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7,
    "8": 8, "9": 9, "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15,
}
_L1 = [212, 45, 80, 68, 195, 163, 163, 203, 157, 220, 254, 91, 204, 79, 104, 6]
_B64_TABLE = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="


def zzb_sign(param: str) -> str:
    """Generate QQ Music API signature using the zzb algorithm (GoMusic port)."""
    md5_hex = hashlib.md5(param.encode("utf-8")).hexdigest().upper()

    # Extract characters by fixed indices
    t1_indices = [21, 4, 9, 26, 16, 20, 27, 30]
    t3_indices = [18, 11, 3, 2, 1, 7, 6, 25]
    t1 = "".join(md5_hex[i] for i in t1_indices)
    t3 = "".join(md5_hex[i] for i in t3_indices)

    # Build ls2: XOR each byte's value with the fixed key
    ls2 = []
    for i in range(16):
        x1 = _K1[md5_hex[i * 2]]
        x2 = _K1[md5_hex[i * 2 + 1]]
        x3 = (x1 * 16 ^ x2) ^ _L1[i]
        ls2.append(x3)

    # Base64-like encode into ls3
    ls3 = []
    for i in range(6):
        if i == 5:
            # Last iteration: only one remaining byte
            ls3.append(_B64_TABLE[ls2[-1] >> 2])
            ls3.append(_B64_TABLE[(ls2[-1] & 3) << 4])
        else:
            x4 = ls2[i * 3] >> 2
            x5 = (ls2[i * 3 + 1] >> 4) ^ ((ls2[i * 3] & 3) << 4)
            x6 = (ls2[i * 3 + 2] >> 6) ^ ((ls2[i * 3 + 1] & 15) << 2)
            x7 = 63 & ls2[i * 3 + 2]
            ls3.append(_B64_TABLE[x4] + _B64_TABLE[x5] + _B64_TABLE[x6] + _B64_TABLE[x7])

    t2 = "".join(ls3)
    # Remove \, /, + characters
    t2 = re.sub(r"[\\/+]", "", t2)

    return "zzb" + (t1 + t2 + t3).lower()


# --- Simple MD5 sign (for c.y.qq.com legacy API) ---

# This key may change; extract from QQ Music web player
_QQMUSIC_COMMON_KEY = "0b1d3d5f7f9a2c4e6b8d0f1a3c5e7f9b"


def simple_sign(params: dict[str, str], key: str = None) -> str:
    """Generate simple MD5 sign for c.y.qq.com endpoints.

    1. Sort params by key
    2. Concatenate as key=value pairs with &
    3. Append &key={app_key}
    4. MD5 hash, uppercase
    """
    if key is None:
        key = _QQMUSIC_COMMON_KEY
    sorted_keys = sorted(params.keys())
    raw = "&".join(f"{k}={params[k]}" for k in sorted_keys if params[k] is not None)
    raw += f"&key={key}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


# --- g_tk generator ---

def generate_g_tk(musickey: str) -> int:
    """Generate g_tk CSRF token from QQ Music musickey cookie."""
    hash_val = 5381
    for ch in musickey:
        hash_val += (hash_val << 5) + ord(ch)
    return hash_val & 0x7FFFFFFF
