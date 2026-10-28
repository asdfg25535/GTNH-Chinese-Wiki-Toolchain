"""与 GTNH-Dumper Utils.hashNBT 相同的 UTF-8、SHA-256 折叠和 Base62 算法。"""

import hashlib

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
HASH_LENGTH = 15
HASH_ALGORITHM = "sha256-xor128-base62-15"


def choose_bucket_count(byte_size, target_bytes):
    """按紧凑 JSON 数据量选取 1、2、4、8…桶，使平均数据量不超过目标字节数。"""
    pages = max(1, (byte_size + target_bytes - 1) // target_bytes)
    return 1 << (pages - 1).bit_length()


def short_hash(text: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    folded = bytes(digest[i] ^ digest[i + 16] for i in range(16))
    value = int.from_bytes(folded, "big")
    result = []
    for _ in range(HASH_LENGTH):
        value, digit = divmod(value, 62)
        result.append(BASE62[digit])
    return "".join(reversed(result))


def bucket_id(text: str, bucket_count: int) -> str:
    """对相同的 15 位哈希取模；逐位取模也能在 Lua 中避开浮点精度问题。"""
    if type(bucket_count) is not int or bucket_count <= 0:
        raise ValueError("bucket_count 必须为正整数")
    value = 0
    for digit in short_hash(text):
        value = (value * 62 + BASE62.index(digit)) % bucket_count
    return f"{value:02d}"
