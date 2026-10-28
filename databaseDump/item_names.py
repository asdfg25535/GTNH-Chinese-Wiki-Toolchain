"""本地生成名称索引、重名报告和中文图片候选报告。"""

import argparse
import csv
import hashlib
import json
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from bounded_json import DEFAULT_MAX_BYTES, encode_json, validate_limit, write_bounded
from routing_hash import bucket_id, choose_bucket_count
from item_buckets import write_item_buckets
from name_recipes import collect_handlers


def image_name_config():
    """解析当前首选清单路径，供预处理和图片发布共同校验。"""
    configured = os.environ.get("IMAGE_NAME_CONFIG")
    return Path(configured) if configured else Path(__file__).parent / "config/preferred-items.json"


def file_hash(path):
    path = Path(path)
    if not path.exists():
        return None
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def image_title(name):
    """先沿用 Dumper 的文件名清理，再合并 MediaWiki 等价标题。"""
    name = re.sub(r'[/\\:*"<>|?]', "", name)
    name = name.translate(str.maketrans("[]{}#", "-----"))
    name = " ".join(unicodedata.normalize("NFC", name).replace("_", " ").split())
    if not name:
        return None
    first = name[0].upper()
    return (first if len(first) == 1 else name[0]) + name[1:] + ".png"


def canonical_file(title):
    value = " ".join(unicodedata.normalize("NFC", title).replace("_", " ").split())
    return value[:1].upper() + value[1:]


def collect_names(items):
    names = defaultdict(set)
    candidates = defaultdict(dict)
    for item in items:
        key = item.get("key")
        name = item.get("translatedName")
        if not key or not isinstance(name, str) or not name.strip():
            continue
        names[name].add(key)
        icon = item.get("iconName") or item.get("icon")
        title = image_title(name)
        if not icon or not title:
            continue
        row = {"key": key, "name": name, "icon": icon, "mod": item.get("modName") or item.get("mod", "")}
        old = candidates[title].get(key)
        if old and old["icon"] != icon:
            raise ValueError(f"同一物品 key 对应不同图片: {key}")
        candidates[title][key] = row
    return ({name: sorted(keys) for name, keys in sorted(names.items())},
            {title: sorted(rows.values(), key=lambda row: row["key"])
             for title, rows in sorted(candidates.items())})


def load_choices(path):
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text("utf-8"))
    if data.get("schemaVersion") != 2 or not isinstance(data.get("choices"), dict):
        raise ValueError(f"无效的图片名称配置: {path}")
    for title, choice in data["choices"].items():
        if (not isinstance(choice, dict) or "key" not in choice
                or choice["key"] is not None and not isinstance(choice["key"], str)):
            raise ValueError(f"无效的图片名称选择: {title}")
    return data["choices"]


def filter_image_candidates(items):
    """按已确认的显示偏好缩减候选，保留仍需选择的全部身份。"""
    candidates = list(items)
    reasons = []

    def prefer(matches, reason):
        nonlocal candidates
        if matches and len(matches) < len(candidates):
            candidates = matches
            reasons.append(reason)

    def tooltip(item):
        return re.sub(r'§.', '', item.get('tooltips', ''))

    # 先选择仍在使用的物品，避免已移除的高优先级模组物品占用中文别名。
    prefer([item for item in candidates
            if not re.search(r'弃用|移除|\bdeprecated\b', tooltip(item), re.IGNORECASE)], 'current-item')

    # 与线上 Database 的名称选择顺序一致；metadata 与 NBT 使用下方的显示偏好。
    for prefix, reason in (('minecraft:', 'minecraft'), ('fluid.', 'fluid'),
                           ('gregtech:', 'gregtech'), ('IC2:', 'ic2')):
        matches = [item for item in candidates if item['key'].startswith(prefix)]
        if matches:
            prefer(matches, reason)
            break

    # fluid.* 是预处理后的流体身份；其显示 NBT 不是普通物品变体。
    prefer([item for item in candidates
            if item['key'].startswith('fluid.')
            and item.get('registeredBy', '').lower() == 'gregtech'], 'gregtech-fluid-owner')

    # 电池箱的槽位数量由 tooltip 给出，与 metadata 的大小无对应关系。
    if all(item.get('rawKey', item['key']).startswith('gregtech:gt.blockmachines:')
           for item in candidates):
        if all(item.get('translatedName', '').endswith('电池箱') for item in candidates):
            prefer([item for item in candidates
                    if re.search(r'(?:^|<br\s*/?>|\n)\s*1个槽位\s*(?:$|<br\s*/?>|\n)', tooltip(item))],
                   'one-slot-battery-buffer')

    by_raw = defaultdict(list)
    for item in candidates:
        by_raw[item.get('rawKey', item['key'])].append(item)
    reduced = []
    for group in by_raw.values():
        plain = [item for item in group if not item.get('nbt')]
        if not plain:
            plain = [item for item in group if str(item.get('nbt', '')).strip() == '{}']
        reduced.extend(plain or group)
    prefer(reduced, 'no-nbt')

    def registry_damage(item):
        raw = item.get('rawKey', item['key'])
        parts = raw.rsplit(':', 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0], int(parts[1])
        return raw, 0

    # GTBlockOre / GTOreAdapter：普通石头是 ores2 的底岩索引 0。
    # 8000 为天然状态位，16000 为小型矿石位；这里仅判断底岩。
    ore_items = [item for item in candidates
                 if re.fullmatch(r'gregtech:gt\.blockores[2-7]', registry_damage(item)[0])]
    if len(ore_items) == len(candidates):
        prefer([item for item in candidates
                if registry_damage(item)[0] == 'gregtech:gt.blockores2'
                and registry_damage(item)[1] % 8000 < 1000], 'stone-ore')

    by_registry = defaultdict(list)
    for item in candidates:
        by_registry[registry_damage(item)[0]].append(item)
    reduced = []
    for group in by_registry.values():
        zero = [item for item in group if registry_damage(item)[1] == 0]
        reduced.extend(zero or group)
    prefer(reduced, 'zero-damage')
    # 同一 rawKey 的 NBT 变体允许任取一个展示代表；完整 key 排序保持可重复。
    # 不跨注册物品或 metadata 兜底，也不改变配方的物品身份。
    if len(candidates) > 1 and len({item.get('rawKey', item['key']) for item in candidates}) == 1:
        prefer([min(candidates, key=lambda item: item['key'])], 'nbt-key-order')
    return candidates, reasons


def default_image_candidate(items):
    """默认规则仅接受唯一候选；仍有多个身份时交由首选清单选择。"""
    candidates, reasons = filter_image_candidates(items)
    return (candidates[0]['key'] if len(candidates) == 1 else None), reasons


def generate_preferred_config(items, path, config, icon_map):
    """保留人工选择，为规则未解决的名称按 Dumper 图标遍历顺序生成独立清单。"""
    path, config = Path(path), Path(config)
    if path.resolve() in {config.resolve(), (Path(__file__).parent / "config/preferred-items.json").resolve()}:
        raise ValueError("生成清单不能覆盖首选配置")
    choices = dict(load_choices(config))
    icon_order = {}
    for index, row in enumerate(json.loads(Path(icon_map).read_text('utf-8'))['shortKey']):
        icon_order.setdefault(row['iconName'], index)

    items = list(items)
    _, groups = collect_names(items)
    by_key = {item['key']: item for item in items}
    for title, rows in groups.items():
        if title in choices:
            continue
        candidates, _ = filter_image_candidates([by_key[row['key']] for row in rows])
        if len(candidates) < 2:
            continue
        # 流体等预处理身份可能改写 key；iconName 仍指向同一张原始图标。
        def order(item):
            icon = item.get('iconName') or item.get('icon')
            if icon not in icon_order:
                raise ValueError(f"Dumper 图标映射缺少候选: {title}: {item['key']} ({icon})")
            return icon_order[icon], item['key']

        chosen = min(candidates, key=order)
        choices[title] = {'key': chosen['key']}
    result = {'schemaVersion': 2, 'choices': dict(sorted(choices.items()))}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    return result


def generate_config(items, path, config):
    """生成本地候选报告；公共首选文件只读，不写回默认推断。"""
    path, config = Path(path), Path(config)
    if path.resolve() in {config.resolve(), (Path(__file__).parent / "config/preferred-items.json").resolve()}:
        raise ValueError("候选报告不能覆盖首选配置")
    items = list(items)
    choices = load_choices(config)
    _, groups = collect_names(items)
    by_key = {item['key']: item for item in items}
    candidates = {}
    for title, rows in groups.items():
        selected, reasons = default_image_candidate([by_key[row['key']] for row in rows])
        candidates[title] = {"candidates": rows, "defaultKey": selected, "defaultReasons": reasons}
        if title in choices:
            candidates[title]["choice"] = choices[title]
    result = {"candidates": candidates, "configSha256": file_hash(config)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return result


def resolve_images(groups, choices, physical_icons=(), items=()):
    by_key = {item["key"]: item for item in items}
    redirects = {}
    unresolved = {}
    invalid = {}
    all_icons = {canonical_file(row["icon"]) for rows in groups.values() for row in rows}
    all_icons.update(canonical_file(icon) for icon in physical_icons)
    for title, rows in groups.items():
        if title in choices:
            selected = choices[title].get("key")
            matches = [row for row in rows if row["key"] == selected]
            if selected is None:
                continue
            if len(matches) != 1:
                invalid[title] = {"key": selected, "candidates": rows}
                continue
            chosen = matches[0]
        else:
            selected, _ = default_image_candidate([by_key.get(row['key'], row) for row in rows])
            if selected is None:
                unresolved[title] = rows
                continue
            chosen = next(row for row in rows if row['key'] == selected)
        target = canonical_file(chosen["icon"])
        if title in all_icons:
            if title != target:
                invalid[title] = {"reason": "中文别名与实际图片文件标题冲突", "key": chosen["key"]}
            continue
        redirects["File:" + title] = "File:" + chosen["icon"]
    for title in choices.keys() - groups.keys():
        invalid[title] = {"reason": "配置中的名称已不存在", "key": choices[title].get("key")}
    return redirects, unresolved, invalid


def _name_pages(mapping, metadata, max_bytes):
    """按字段成员分页；同名记录跨页时，读取方合并数组和 queryKeys 映射。"""
    names = {}
    shell_size = len(encode_json({**metadata, "names": {}}))
    size = shell_size
    for name, record in mapping.items():
        atoms = [(field, value) for field, values in record.items()
                 for value in (values.items() if isinstance(values, dict) else values)]
        for field, value in atoms or [("keys", None)]:
            while True:
                current = names.get(name, {"keys": []})
                cost = 0 if name in names else len(encode_json({name: current})) - 2 + bool(names)
                if value is None:
                    pass
                elif field == "queryKeys":
                    pair = {value[0]: value[1]}
                    cost += (len(encode_json(pair)) - 2 + bool(current[field]) if field in current
                             else len(encode_json({field: pair})) - 2 + bool(current))
                else:
                    cost += (len(encode_json(value)) + bool(current[field]) if field in current
                             else len(encode_json({field: [value]})) - 2 + bool(current))
                if size + cost <= max_bytes:
                    break
                if not names:
                    raise ValueError(f"单条名称字段含 metadata 后超过分页上限: {size + cost} > {max_bytes}")
                yield encode_json({**metadata, "names": names})
                names, size = {}, shell_size
            current = names.setdefault(name, {"keys": []})
            if field == "queryKeys":
                current.setdefault(field, {})[value[0]] = value[1]
            elif value is not None:
                current.setdefault(field, []).append(value)
            size += cost
    if names:
        yield encode_json({**metadata, "names": names})


def write_name_index(names, output, max_bytes=DEFAULT_MAX_BYTES, *, query_keys=None, raw_keys=None, handlers=None):
    validate_limit(max_bytes)
    output = Path(output)
    raw_keys, query_keys, handlers = raw_keys or {}, query_keys or {}, handlers or {}
    records = {}
    for name, values in sorted(names.items()):
        keys = sorted(set(values))
        record = {"keys": keys}
        raw = sorted({raw_keys.get(key, key) for key in keys})
        if raw != keys:
            record["rawKeys"] = raw
        semantic = {key: query_keys[key] for key in keys if query_keys.get(key)}
        if semantic:
            record["queryKeys"] = semantic
        for field in ("sourceHandlers", "usageHandlers"):
            paths = handlers.get(field, {}).get(name)
            if paths:
                record[field] = sorted(paths, key=lambda entry: entry[0] if isinstance(entry, list) else entry)
        records[name] = record
    bucket_count = choose_bucket_count(sum(len(encode_json({name: record})) - 1
                                          for name, record in records.items()), max_bytes)
    metadata = {"bucketCount": bucket_count}
    grouped = defaultdict(dict)
    for name, record in records.items():
        grouped[bucket_id(name, bucket_count)][name] = record
    written = set()
    for bucket, mapping in sorted(grouped.items()):
        for part, content in enumerate(_name_pages(mapping, metadata, max_bytes)):
            path = output / bucket / f"{part}.json"
            write_bounded(path, content, max_bytes)
            written.add(path)
    # 只删除本生成器目录下旧的数字分块，防止缩减数据后残留旧索引。
    for path in output.glob("[0-9]*/[0-9]*.json"):
        if path.parent.name.isdecimal() and path.stem.isdecimal() and path not in written:
            path.unlink()
    return len(written)


def write_item_name_index(items, version_dir, output=None):
    """基于最终 Items、OreDictionary、Recipes 生成名称查询所需的全部字段。"""
    items = list(items)
    names, _ = collect_names(items)
    raw_keys = {item["key"]: item["rawKey"] for item in items if item.get("rawKey")}
    query_keys = {item["key"]: item["queryKey"] for item in items if item.get("queryKey")}
    handlers = collect_handlers(names, raw_keys, query_keys, version_dir)
    return write_name_index(names, output or Path(version_dir) / "ItemNames",
                            raw_keys=raw_keys, query_keys=query_keys, handlers=handlers)


def build_artifacts(items, version_dir, state_dir, source_dir, config, icon_source=None, *, write_items=True):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    items = list(items)
    names, groups = collect_names(items)
    physical_icons = (path.name for path in (Path(source_dir) / "icons").glob("*.png"))
    redirects, unresolved, invalid = resolve_images(groups, load_choices(config), physical_icons, items)
    count = write_item_name_index(items, version_dir)
    item_dir = Path(version_dir) / "Items"
    item_file_count = (write_item_buckets(items, item_dir) if write_items else
                       sum(path.stem.isdecimal() for path in item_dir.rglob("*.tabx")))
    with (state_dir / "image_redirect.resolved.csv").open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(sorted(redirects.items()))
    icon_source = Path(icon_source) if icon_source else Path(source_dir) / "itemicon.json"
    report = {"schemaVersion": 1, "nameCount": len(names), "indexFileCount": count,
              "itemFileCount": item_file_count,
              "collisions": {title: rows for title, rows in groups.items() if len(rows) > 1},
              "unresolved": unresolved, "invalidSelections": invalid,
              "redirectCount": len(redirects), "config": str(Path(config).resolve()),
              "configSha256": file_hash(config),
              "redirectsSha256": file_hash(state_dir / "image_redirect.resolved.csv"),
              "sourceIconsPath": str(icon_source.resolve()), "sourceIconsSha256": file_hash(icon_source),
              "sourceItemsSha256": file_hash(Path(source_dir) / "advitempanel.json")}
    (state_dir / "image-name-resolution.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    print(f"  [Item names] {len(names):,} names; {len(unresolved):,} unresolved image names; {len(invalid):,} invalid choices")
    return report


def read_items(directory):
    paths = sorted(Path(directory).rglob("*.json"))
    if paths:
        for path in paths:
            yield from json.loads(path.read_text("utf-8"))["items"]
    else:
        for path in sorted(Path(directory).rglob("*.tabx")):
            data = json.loads(path.read_text("utf-8"))
            fields = data["schema"]["fields"]
            for row in data["data"]:
                yield {field["name"]: (json.loads(value) if field.get("jsonEncoded") and value is not None else value)
                       for field, value in zip(fields, row)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["generate-config", "generate-preferred"])
    parser.add_argument("--items-dir", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--icon-map", type=Path, help="同次导出的 itemicon.json，保留 Dumper 遍历顺序")
    args = parser.parse_args()
    if args.command == 'generate-preferred' and not args.icon_map:
        parser.error('generate-preferred 需要 --icon-map 指定同次导出的 itemicon.json')
    if not args.items_dir or not args.config:
        import split_all
        split_all.configure_source()
        args.items_dir = args.items_dir or Path(split_all.VERSION) / "Items"
        args.config = args.config or image_name_config()
    items = list(read_items(args.items_dir))
    if not items:
        raise ValueError("没有预处理后的物品数据，请先执行 split_all.py items")
    if args.command == 'generate-preferred':
        args.output = args.output or Path('preferred-items.generated.json')
        result = generate_preferred_config(items, args.output, args.config, args.icon_map)
        added = len(result['choices']) - len(load_choices(args.config))
        print(f"Generated {added} fallback choices; {len(result['choices'])} total: {args.output}")
    else:
        args.output = args.output or Path('image-name-candidates.json')
        result = generate_config(items, args.output, args.config)
        print(f"Generated {len(result['candidates'])} image-name candidates: {args.output}")


if __name__ == "__main__":
    main()
