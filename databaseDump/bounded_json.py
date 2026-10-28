"""UTF-8 JSON 分页：按最终字节计数，每条记录只序列化一次。"""

import json
from pathlib import Path

HARD_MAX_BYTES = 2_000_000
DEFAULT_MAX_BYTES = 1_800_000


def validate_limit(max_bytes):
    if type(max_bytes) is not int or not 0 < max_bytes <= HARD_MAX_BYTES:
        raise ValueError(f"JSON 分页上限必须是 1..{HARD_MAX_BYTES} 字节的整数")
    return max_bytes


def encode_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def array_shell(metadata, field):
    # 数组字段放在最后，前缀包含左方括号，后缀固定为 ]}。
    shell = encode_json({**{key: value for key, value in metadata.items() if key != field}, field: []})
    return shell[:-2], b"]}"


def array_pages(records, metadata, field, max_bytes=DEFAULT_MAX_BYTES, *, emit_empty=False):
    validate_limit(max_bytes)
    prefix, suffix = array_shell(metadata, field)
    shell_size = len(prefix) + len(suffix)
    if shell_size > max_bytes:
        raise ValueError(f"JSON metadata 超过分页上限: {shell_size} > {max_bytes}")
    batch, size = [], shell_size
    for record in records:
        data = encode_json(record)
        if shell_size + len(data) > max_bytes:
            raise ValueError(f"单条 {field} 记录含 metadata 后超过分页上限: {shell_size + len(data)} > {max_bytes}")
        cost = len(data) + bool(batch)
        if batch and size + cost > max_bytes:
            yield prefix + b",".join(batch) + suffix
            batch, size = [], shell_size
        size += len(data) + bool(batch)
        batch.append(data)
    if batch or emit_empty:
        yield prefix + b",".join(batch) + suffix


def write_bounded(path, content, max_bytes=DEFAULT_MAX_BYTES):
    """最终序列化字节检查位于写入之前，包含 metadata、标点与 UTF-8 编码开销。"""
    validate_limit(max_bytes)
    if len(content) > max_bytes:
        raise ValueError(f"JSON 文件超过分页上限: {len(content)} > {max_bytes}: {path}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def write_array_pages(records, metadata, field, target, max_bytes=DEFAULT_MAX_BYTES):
    written = []
    for part, content in enumerate(array_pages(records, metadata, field, max_bytes)):
        path = Path(target) / f"{part}.json"
        write_bounded(path, content, max_bytes)
        written.append(path)
    return written


def map_array_pages(mapping, metadata, field, max_bytes=DEFAULT_MAX_BYTES):
    """对象值为数组；长数组跨页，单页中每个对象键最多出现一次。"""
    validate_limit(max_bytes)
    shell = encode_json({**{key: value for key, value in metadata.items() if key != field}, field: {}})
    prefix, suffix = shell[:-2], b"}}"
    # 内层分页结果有一对额外花括号，嵌入对象时剥除。
    entry_limit = max_bytes - len(shell) + 2
    if entry_limit <= 2:
        raise ValueError("名称索引 metadata 已用尽分页上限")
    batch, names, size = [], set(), len(shell)
    for name, values in mapping.items():
        for fragment in array_pages(values, {}, name, entry_limit, emit_empty=True):
            entry = fragment[1:-1]
            if batch and (name in names or size + len(entry) + 1 > max_bytes):
                yield prefix + b",".join(batch) + suffix
                batch, names, size = [], set(), len(shell)
            size += len(entry) + bool(batch)
            batch.append(entry)
            names.add(name)
    if batch:
        yield prefix + b",".join(batch) + suffix
