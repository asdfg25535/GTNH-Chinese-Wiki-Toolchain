"""将 BetterQuesting DefaultQuests 和中文语言文件导出为 Wiki JSON 数据页。"""

import argparse
import base64
import json
import os
from pathlib import Path
import re
import struct
import sys
import textwrap

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from databaseDump.bounded_json import DEFAULT_MAX_BYTES, validate_limit
from databaseDump.data_version import title_part

TYPE_SUFFIX = re.compile(r"^(.*):(\d+)$")
CONFIG_KEYS = {"MINECRAFT_DIR", "MOD_REMOTE_NAME", "MOD_VERSION"}


def load_config(env_file, *, keys=CONFIG_KEYS, required=None):
    """仅读取所需配置；环境变量优先，不对 Windows 路径做反斜杠转义。"""
    required = keys if required is None else required
    config = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.removeprefix("export ").partition("=")
            key, value = key.strip(), value.strip()
            if sep and key in keys:
                if value.startswith(("'", '"')):
                    quote = value[0]
                    end = value.find(quote, 1)
                    remainder = value[end + 1:].strip() if end >= 0 else ""
                    if end < 0 or (remainder and not remainder.startswith("#")):
                        raise ValueError(f"{env_file}: {key} 的引号格式无效")
                    value = value[1:end]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                config[key] = value
    config.update({key: os.environ[key] for key in keys if key in os.environ})
    for key in required:
        if not config.get(key, "").strip():
            raise ValueError(f"请在 {env_file} 或环境变量中设置 {key}")
        config[key] = config[key].strip()
    return config


def read_lang(path):
    translations = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            # 同名键沿用语言文件中最后一次定义；译文中的等号和空格不丢失。
            translations[key.strip()] = value
    return translations


def quest_id(high, low):
    """BQ 的两个有符号 long 按大端拼接，再编码为不带填充的 URL-safe Base64。"""
    if any(type(value) is not int or not -(1 << 63) <= value < (1 << 63)
           for value in (high, low)):
        raise ValueError("任务 ID 必须由两个有符号 64 位整数构成")
    return base64.urlsafe_b64encode(struct.pack(">qq", high, low)).decode("ascii").rstrip("=")


def normalize_nbt(value, tag_type=None):
    """去掉 NBT 类型后缀，列表转数组；ID 和其他 long 使用无损字符串。"""
    if tag_type == 9 and isinstance(value, dict):
        entries = []
        for key, child in value.items():
            match = TYPE_SUFFIX.fullmatch(key)
            if not match or not match[1].isdecimal():
                raise ValueError(f"无效的 NBT 列表索引: {key}")
            entries.append((int(match[1]), normalize_nbt(child, int(match[2]))))
        entries.sort(key=lambda entry: entry[0])
        if len({index for index, _ in entries}) != len(entries):
            raise ValueError("NBT 列表包含重复索引")
        return [child for _, child in entries]
    if isinstance(value, list):
        return [normalize_nbt(child) for child in value]
    if not isinstance(value, dict):
        return str(value) if tag_type == 4 else value
    result = {}
    for key, child in value.items():
        match = TYPE_SUFFIX.fullmatch(key)
        name, kind = (match[1], int(match[2])) if match else (key, None)
        if name.endswith("IDHigh") and kind == 4:
            prefix = name[:-4]
            low = prefix + "Low:4"
            if low not in value:
                raise ValueError(f"缺失 ID 低位: {low}")
            name, converted = prefix, quest_id(child, value[low])
        elif name.endswith("IDLow") and kind == 4:
            if name[:-3] + "High:4" not in value:
                raise ValueError(f"缺失 ID 高位: {name}")
            continue
        else:
            converted = normalize_nbt(child, kind)
        if name in result:
            raise ValueError(f"去除 NBT 类型后缀后字段冲突: {name}")
        result[name] = converted
    return result


def read_record(path):
    try:
        record = normalize_nbt(json.loads(path.read_text(encoding="utf-8-sig")))
        if not isinstance(record, dict):
            raise ValueError("记录必须是 JSON 对象")
        return record
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc


def localize(record, kind, translations, missing):
    id_field = "questID" if kind == "quest" else "questLineID"
    try:
        identifier = record[id_field]
        properties = record["properties"]["betterquesting"]
        for field in ("name", "desc"):
            original = properties[field]
            if not isinstance(original, str):
                raise ValueError(f"{id_field}={identifier}: {field} 不是文本")
            key = f"betterquesting.{kind}.{identifier}.{field}"
            if key not in translations:
                missing.append(key)
            original_field = "originalName" if field == "name" else "originalDesc"
            properties[original_field] = original
            properties[field] = translations.get(key, original).replace("%n", "\n")
        record["properties"]["betterquesting"] = dict(sorted(properties.items()))
    except (KeyError, TypeError) as exc:
        raise ValueError(f"任务记录缺少 ID 或 properties.betterquesting.name/desc: {record.get(id_field)}") from exc


def read_order(path):
    order = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        identifier, sep, _ = line.partition(":")
        identifier = identifier.strip().rstrip("=")
        if not sep or not re.fullmatch(r"[A-Za-z0-9_-]{22}", identifier):
            raise ValueError(f"{path}:{number}: 无效的任务线顺序记录")
        if identifier in order:
            raise ValueError(f"{path}:{number}: 重复的任务线 ID {identifier}")
        order.append(identifier)
    return order


def build_records(source, translations):
    missing, quests, lines = [], {}, {}
    warnings = []
    for path in sorted((source / "Quests").rglob("*.json")):
        record = read_record(path)
        localize(record, "quest", translations, missing)
        identifier = record["questID"]
        if identifier in quests:
            raise ValueError(f"重复的任务 ID {identifier}: {path}")
        quests[identifier] = record
    for path in sorted((source / "QuestLines").glob("*/QuestLine.json")):
        record = read_record(path)
        localize(record, "questline", translations, missing)
        identifier = record["questLineID"]
        if identifier in lines:
            raise ValueError(f"重复的任务线 ID {identifier}: {path}")
        placements = [read_record(p) for p in sorted(path.parent.glob("*.json")) if p != path]
        seen = set()
        for placement in placements:
            quest = placement.get("questID")
            if not isinstance(quest, str):
                raise ValueError(f"任务线 {identifier} 的布局记录缺少 questID")
            if quest not in quests:
                warnings.append(f"任务线 {identifier} 引用了不存在的任务: {quest}")
            if quest in seen:
                raise ValueError(f"任务线 {identifier} 包含重复任务: {quest}")
            seen.add(quest)
        record["quests"] = sorted(placements, key=lambda placement: placement["questID"])
        lines[identifier] = record
    if not quests or not lines:
        raise ValueError(f"{source}: 未找到 Quests/**/*.json 或 QuestLines/*/QuestLine.json")
    order = read_order(source / "QuestLinesOrder.txt")
    if set(order) != set(lines):
        raise ValueError("QuestLinesOrder.txt 与 QuestLines 中的任务线 ID 不一致")
    for record in quests.values():
        for prerequisite in record.get("preRequisites", []):
            if prerequisite.get("questID") not in quests:
                warnings.append(f"任务 {record['questID']} 的前置任务不存在: {prerequisite}")
    ordered_lines = []
    for index, identifier in enumerate(order):
        lines[identifier]["order"] = index
        ordered_lines.append(lines[identifier])
    return ordered_lines, [quests[key] for key in sorted(quests)], missing, warnings


def pretty_array_pages(records, field, max_bytes=DEFAULT_MAX_BYTES):
    """两空格缩进的 JSON 分页；缩进、分隔符和末尾换行均计入 UTF-8 字节数。"""
    validate_limit(max_bytes)
    prefix = ('{\n  ' + json.dumps(field, ensure_ascii=False) + ': [\n').encode("utf-8")
    suffix = b"\n  ]\n}\n"
    separator = b",\n"
    shell_size = len(prefix) + len(suffix)
    if shell_size > max_bytes:
        raise ValueError(f"JSON 外层结构超过分页上限: {shell_size} > {max_bytes}")
    batch, size = [], shell_size
    for record in records:
        text = json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False)
        data = textwrap.indent(text, "    ").encode("utf-8")
        if shell_size + len(data) > max_bytes:
            raise ValueError(f"单条 {field} 记录含缩进后超过分页上限: {shell_size + len(data)} > {max_bytes}")
        cost = len(data) + (len(separator) if batch else 0)
        if batch and size + cost > max_bytes:
            yield prefix + separator.join(batch) + suffix
            batch, size = [], shell_size
        size += len(data) + (len(separator) if batch else 0)
        batch.append(data)
    if batch:
        yield prefix + separator.join(batch) + suffix


def export_quests(source, lang, destination, *, max_bytes=DEFAULT_MAX_BYTES,
                  strict_translations=False, dry_run=False):
    lines, quests, missing, warnings = build_records(source, read_lang(lang))
    if missing and strict_translations:
        raise ValueError(f"缺少 {len(missing)} 项汉化，例如 {missing[0]}")
    # 两类页面全部完成解析、引用校验和字节限制检查后再写入。
    pages = {
        "QuestLines": list(pretty_array_pages(lines, "questLines", max_bytes)),
        "Quests": list(pretty_array_pages(quests, "quests", max_bytes)),
    }
    destination = destination.resolve()
    stale = []
    for category, content in pages.items():
        folder = destination / category
        if not folder.resolve().is_relative_to(destination):
            raise ValueError(f"输出目录越出目标版本目录: {folder}")
        for path in folder.glob("*.json"):
            if path.stem.isdecimal():
                if not path.resolve().is_relative_to(destination):
                    raise ValueError(f"生成页越出目标版本目录: {path}")
                if path.name not in {f"{index}.json" for index in range(len(content))}:
                    stale.append(path)
    if not dry_run:
        for category, content in pages.items():
            folder = destination / category
            folder.mkdir(parents=True, exist_ok=True)
            for index, data in enumerate(content):
                path = folder / f"{index}.json"
                # 内容未改变时保留 mtime，避免工具链重复安排上传。
                if not path.exists() or path.read_bytes() != data:
                    path.write_bytes(data)
        for path in stale:
            path.unlink()
    return {"questLines": len(lines), "quests": len(quests), "missing": missing, "warnings": warnings,
            "pages": {category: len(content) for category, content in pages.items()},
            "maxPageBytes": max(len(data) for content in pages.values() for data in content)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                        help="每个 JSON 页的 UTF-8 字节上限（默认 1800000，最大 2000000）")
    parser.add_argument("--strict-translations", action="store_true", help="缺少译文时报错，不写入文件")
    parser.add_argument("--dry-run", action="store_true", help="解析、校验并统计，不写入文件")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env_file)
        minecraft = Path(config["MINECRAFT_DIR"]).expanduser()
        if not minecraft.is_absolute():
            minecraft = args.env_file.resolve().parent / minecraft
        destination = (PROJECT_ROOT / "wikitext/Data/Versions"
                       / title_part(config["MOD_REMOTE_NAME"]) / title_part(config["MOD_VERSION"]))
        result = export_quests(
            minecraft / "config/betterquesting/DefaultQuests",
            minecraft / "config/txloader/forceload/betterquesting/lang/zh_CN.lang",
            destination, max_bytes=args.max_bytes,
            strict_translations=args.strict_translations, dry_run=args.dry_run,
        )
    except (OSError, ValueError) as exc:
        parser.exit(1, f"错误: {exc}\n")
    print(f"{'校验完成' if args.dry_run else '生成完成'}: {destination}")
    print(f"任务线 {result['questLines']}，任务 {result['quests']}；"
          f"QuestLines {result['pages']['QuestLines']} 页，Quests {result['pages']['Quests']} 页；"
          f"最大页面 {result['maxPageBytes']} 字节")
    if result["missing"]:
        print(f"警告: {len(result['missing'])} 项译文缺失，保留原文：", file=sys.stderr)
        for key in result["missing"]:
            print(f"  {key}", file=sys.stderr)
    else:
        print("标题和描述均已匹配中文语言文件。")
    for warning in result["warnings"]:
        print(f"警告（已保留源引用）: {warning}", file=sys.stderr)


if __name__ == "__main__":
    main()
