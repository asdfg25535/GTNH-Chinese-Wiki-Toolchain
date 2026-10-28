"""按最终物品 key 分桶完整物品记录，Items 只保留当前分桶分页。"""

from collections import defaultdict
from pathlib import Path

from bounded_json import DEFAULT_MAX_BYTES, encode_json, validate_limit, write_array_pages
from routing_hash import HASH_ALGORITHM, bucket_id, choose_bucket_count


def write_item_buckets(items, output, max_bytes=DEFAULT_MAX_BYTES):
    validate_limit(max_bytes)
    output = Path(output)
    items = list(items)
    bucket_count = choose_bucket_count(sum(len(encode_json(item)) + 1 for item in items), max_bytes)
    groups = defaultdict(list)
    for item in items:
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("物品分桶要求每条记录具有最终 key")
        groups[bucket_id(key, bucket_count)].append(item)
    written = set()
    for bucket, records in sorted(groups.items()):
        metadata = {"hashAlgorithm": HASH_ALGORITHM, "bucketCount": bucket_count}
        written.update(write_array_pages(records, metadata, "items", output / bucket, max_bytes))
    # 仅清理本生成器的数字文件，包括迁移前的根目录顺序分页。
    old_pages = list(output.glob("[0-9]*.json"))
    for directory in output.iterdir() if output.exists() else ():
        if directory.is_dir() and directory.name.isdecimal():
            old_pages.extend(directory.glob("[0-9]*.json"))
    for path in old_pages:
        if path.stem.isdecimal() and path not in written:
            path.unlink()
    return len(written)
