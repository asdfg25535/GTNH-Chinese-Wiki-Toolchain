"""
split_all.py
Unified splitter for Items / Fluids / OreDictionary / Recipes / Research.

Usage:
    python split_all.py                    # all tasks
    python split_all.py items              # Items only
    python split_all.py fluids             # Fluids only
    python split_all.py oredict            # OreDictionary only (requires generated Items)
    python split_all.py recipes            # Recipes only
    python split_all.py tc4research        # Thaumcraft research only
    python split_all.py items fluids       # multiple
    python split_all.py --debug            # debug mode (limit output)
"""

import json
import os
import sys
from collections import Counter, defaultdict
from itertools import chain
from pathlib import Path
from data_version import addon_mods, local_mod_name, source_directory, version_module_title, version_tag
from item_names import build_artifacts, image_name_config, read_items, write_item_name_index
from nbt_identity import nbt_identity
from bounded_json import HARD_MAX_BYTES, DEFAULT_MAX_BYTES, array_pages, encode_json, write_bounded
from routing_hash import HASH_ALGORITHM, bucket_id, choose_bucket_count

# Ensure real-time progress output
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None


def _load_dotenv():
    """加载项目根目录的 .env 文件到 os.environ（不覆盖已有环境变量）。"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    search = [script_dir]
    parent = os.path.dirname(script_dir)
    search.append(parent)
    search.append(os.path.dirname(parent))
    for d in search:
        env_path = os.path.join(d, ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as _f:
                for _line in _f:
                    _line = _line.strip()
                    if not _line or _line.startswith("#"):
                        continue
                    _k, _sep, _v = _line.partition("=")
                    _k, _v = _k.strip(), _v.strip()
                    if _k and _v:
                        os.environ.setdefault(_k, _v)
            return


_load_dotenv()

# 项目根目录（.env 所在，向上两级）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUMPS_DIR = os.environ.get("DUMPS_DIR", "")
# 模组导出可从完整 dumps 中补充图标/流体等关联数据。
REFERENCE_DUMPS_DIR = DUMPS_DIR
if DUMPS_DIR and local_mod_name():
    DUMPS_DIR = str(source_directory(DUMPS_DIR, local_mod_name(), check_exists=False))
AUX_DIR = os.path.dirname(os.path.abspath(__file__))


def reference_path(name: str) -> str:
    local = os.path.join(DUMPS_DIR, name)
    return local if os.path.isfile(local) else os.path.join(REFERENCE_DUMPS_DIR, name)


def write_item_names(items, write_items=True):
    return build_artifacts(items, VERSION, AUX_DIR, DUMPS_DIR, image_name_config(),
                           reference_path("itemicon.json"), write_items=write_items)


def clear_generated_pages(directory, suffixes, keep=()):
    """仅清理当前输出目录内数字命名的生成页，保留 manifest 等辅助文件。"""
    root = Path(directory).resolve()
    keep = {Path(path).resolve() for path in keep}
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes or not path.stem.isdecimal():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"生成页路径越出输出目录: {path}")
        if resolved not in keep:
            path.unlink()

# ============================================================
# Common utilities
# ============================================================

MAX_FILE_SIZE = 1.8 * 1024 * 1024   # 旧分页估算目标；最终发布文件硬上限为 2,000,000 字节。
INDEX_OVERHEAD = 30                  # "index": "99/99999"

BEE_TYPES_BY_RAW_KEY = {
    "Forestry:beeDroneGE": "DRONE",
    "Forestry:beePrincessGE": "PRINCESS",
    "Forestry:beeQueenGE": "QUEEN",
    "Forestry:beeLarvaeGE": "LARVAE",
}


def wrapper_head(key: str) -> str:
    return '{\n  "' + key + '": [\n'


def wrapper_tail() -> str:
    return '\n  ]\n}'


def wrapper_overhead(key: str) -> int:
    return len(wrapper_head(key).encode("utf-8")) + len(wrapper_tail().encode("utf-8"))


def serialize_item(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, indent=2)


def _bee_query_key(identity) -> str | None:
    """Return the stable species/type key exported by the dumper."""
    if not isinstance(identity, dict):
        return None
    species_uid = identity.get("speciesUid")
    bee_type = identity.get("type")
    if not isinstance(species_uid, str) or not species_uid:
        return None
    if not isinstance(bee_type, str) or bee_type not in set(BEE_TYPES_BY_RAW_KEY.values()):
        return None
    return f"bee:{bee_type}:{species_uid}"


def _add_bee_query_keys(items: list[dict]) -> None:
    """Add queryKey without changing the exact NBT-derived item key."""
    added = 0
    incomplete = []
    for item in items:
        identity = item.get("beeIdentity")
        if identity is None:
            continue
        query_key = _bee_query_key(identity)
        raw_key = item.get("rawKey") or item.get("key")
        expected_type = BEE_TYPES_BY_RAW_KEY.get(raw_key)
        if not query_key or (expected_type and identity.get("type") != expected_type):
            incomplete.append({
                "key": item.get("key"),
                "rawKey": raw_key,
                "beeIdentity": identity,
                "issue": "invalid_or_mismatched_bee_identity",
            })
            continue
        item["queryKey"] = query_key
        added += 1
    print(f"  [Bee identity] Added queryKey to {added:,} item(s)")
    if incomplete:
        print(f"  WARNING: {len(incomplete):,} item(s) have incomplete or mismatched bee identity")
        path = os.path.join(AUX_DIR, "bee_item_identity_diagnostics.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(incomplete, f, ensure_ascii=False, indent=2)
        print(f"  [Bee identity] Diagnostics written to {path}")


def split_array(items: list, key: str, debug: bool) -> list:
    """Split items list by MAX_FILE_SIZE. Returns list of (start, end) index ranges."""
    print(f"  Pre-serializing {len(items):,} entries...")
    serialized = []
    for i, item in enumerate(items):
        serialized.append(serialize_item(item))
        if (i + 1) % 50000 == 0:
            print(f"    Serialized: {i + 1:,}/{len(items):,}")

    overhead = wrapper_overhead(key) if key else len('[\n\n]\n'.encode("utf-8"))
    indent = 4 if key else 2
    batches = []          # list[(start, end)]
    batch_start = 0
    current_size = overhead

    for i, item_str in enumerate(serialized):
        byte_len = len(item_str.encode("utf-8"))
        lines = item_str.count("\n") + 1
        indent_extra = lines * indent
        separator = 2 if i > batch_start else 0
        total = byte_len + indent_extra + separator + INDEX_OVERHEAD

        if current_size + total > MAX_FILE_SIZE and i > batch_start:
            batches.append((batch_start, i))
            if debug and len(batches) >= 5:
                print(f"    [DEBUG] Collected 5 batches, stopping early.")
                batch_start = -1
                break
            batch_start = i
            current_size = overhead + byte_len + indent_extra + INDEX_OVERHEAD
        else:
            current_size += total

        if (i + 1) % 50000 == 0:
            print(f"    Batching: {i + 1:,}/{len(items):,}, batch {len(batches) + 1}")

    if batch_start >= 0 and batch_start < len(items):
        batches.append((batch_start, len(items)))

    return batches


def write_version_json(path, payload):
    """保留现有分页与 index；完整 UTF-8 内容超限时在写入前报错。"""
    content = encode_json(payload)
    write_bounded(path, content, HARD_MAX_BYTES)
    return len(content)


def write_batches(items: list, batches: list, key: str, output_dir: str):
    """Write existing batches and indexes after checking the final compact JSON size."""
    os.makedirs(output_dir, exist_ok=True)
    total = 0

    for file_idx, (start, end) in enumerate(batches):
        entries = []
        for pos, global_i in enumerate(range(start, end)):
            entry = dict(items[global_i])
            entry["index"] = f"{file_idx}/{pos}"
            entries.append(entry)

        payload = {key: entries} if key else entries
        path = os.path.join(output_dir, f"{file_idx}.json")
        size = write_version_json(path, payload)
        count = end - start
        print(f"  -> {file_idx}.json: {count:,} entries, {size / 1024:.1f} KB (OK)")
        total += count

    # 重跑后页数可能减少，只保留本次生成的数字分页。
    for old_path in Path(output_dir).glob("[0-9]*.json"):
        if old_path.stem.isdecimal() and int(old_path.stem) >= len(batches):
            old_path.unlink()
    print(f"  Done: {total:,} entries -> {len(batches)} file(s)\n")


# ============================================================
# Tooltip cleanup
# ============================================================

def _clean_tooltips(items: list):
    """移除所有 tooltip 字段中的组件占位符（§z数字§Z）及 §!shift / §!ctrl 标记。"""
    import re
    removed = 0
    for item in items:
        for field in ("tooltips", "tooltipsShift", "tooltipsCtrl", "tooltipsShiftAndCtrl"):
            tips = item.get(field, "")
            if not tips:
                continue
            old = tips
            tips = re.sub(r"<br>§z\d+§Z", "", tips)
            tips = re.sub(r"<br>§!shift", "", tips)
            tips = re.sub(r"<br>§!ctrl", "", tips)
            if tips != old:
                item[field] = tips
                removed += 1
    # 去重：优先级 空 < Shift < Ctrl < ShiftAndCtrl，后面的等于前面就删
    dedup = 0
    for item in items:
        fields = [
            ("tooltips", item.get("tooltips", "")),
            ("tooltipsShift", item.get("tooltipsShift", "")),
            ("tooltipsCtrl", item.get("tooltipsCtrl", "")),
            ("tooltipsShiftAndCtrl", item.get("tooltipsShiftAndCtrl", "")),
        ]
        for i in range(len(fields) - 1):
            fi_name, fi_val = fields[i]
            if not fi_val:
                continue
            for j in range(i + 1, len(fields)):
                fj_name, fj_val = fields[j]
                if fj_val and fj_val == fi_val:
                    item.pop(fj_name, None)
                    fields[j] = (fj_name, "")
                    dedup += 1
    if dedup:
        print(f"  Deduped tooltips: {dedup} duplicate fields removed")

    if removed:
        print(f"  Cleaned tooltips: {removed} items")


def _load_nbt_regress_cache():
    """加载 NBT 退行缓存。返回 (regress_set, keep_set)。"""
    cache_path = os.path.join(AUX_DIR, "nbt_regress_cache.json")
    if not os.path.exists(cache_path):
        return set(), set()
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data.get("regress", [])), set(data.get("keep", []))
    except Exception:
        return set(), set()


def _save_nbt_regress_cache(regress_set, keep_set):
    """保存 NBT 退行缓存。"""
    cache_path = os.path.join(AUX_DIR, "nbt_regress_cache.json")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump({"regress": sorted(regress_set), "keep": sorted(keep_set)}, f, ensure_ascii=False, indent=2)


def _fix_wisp_aspects(items: list):
    """修正天域之华物品的 aspect 字段：NBT 中的真实要素合并到 aspect。
    所有天域之华 item 的 aspect 写死为 Auram:2，但 NBT 中记录了实际要素。"""
    import re
    CUSTOM_MAP = {
        "custom1": "Aequalitas", "custom2": "Vesania", "custom3": "Primordium",
        "custom4": "Astrum", "custom5": "Gloria",
        "caelum": "Caelum", "tabernus": "Tabernus",
    }
    fixed = 0
    for item in items:
        k = item.get("key", "")
        if not k.startswith("Thaumcraft:ItemWispEssence"):
            continue
        nbt = item.get("nbt", "")
        m = re.search(r'key:"(\w+)"', nbt)
        if not m:
            continue
        nbt_aspect = m.group(1)
        # 映射 custom1~5 等特殊名
        real_aspect = CUSTOM_MAP.get(nbt_aspect, nbt_aspect.capitalize())
        asp = item.get("aspect")
        if not isinstance(asp, dict):
            asp = {}
        if real_aspect not in asp:
            asp[real_aspect] = 2
            item["aspect"] = asp
            fixed += 1
    if fixed:
        print(f"  [WispFix] Merged NBT aspect into {fixed} ItemWispEssence items")


# IC2 充电工具前缀：:1 是没电，:26/:27 是满电。复制 :1 为裸 key。
_CHARGEABLE_TOOL_PREFIXES = {
    "IC2:", "GraviSuite:", "AdvancedSolarPanel:", "EMT:",
}

def _fix_ic2_chargeable_tools(items: list):
    """充电工具：:1=没电，:26/:27=满电。
    复制 :1 变体为去 meta 的裸 key 版本，插入在原条目旁边。"""
    inserted = 0
    existing_keys = {item.get("key") for item in items}
    i = 0
    while i < len(items):
        item = items[i]
        rk = item.get("key", "")  # 此时尚未 rename，key 即原始含 meta 的 key
        if not rk.endswith(":1"):
            i += 1
            continue
        base_key = rk[:-2]  # 去掉 :1
        if base_key in existing_keys:
            i += 1
            continue
        # 检查是否在充电工具前缀列表中
        is_chargeable = False
        for prefix in _CHARGEABLE_TOOL_PREFIXES:
            if base_key.startswith(prefix):
                is_chargeable = True
                break
        if not is_chargeable:
            i += 1
            continue
        # 向前后找 :26 或 :27 变体
        has_charged = False
        for j in (i - 1, i + 1):
            if 0 <= j < len(items):
                nk = items[j].get("key", "")
                if nk == base_key + ":26" or nk == base_key + ":27":
                    has_charged = True
                    break
        if has_charged:
            # 复制 :1 条目，去掉 meta
            copy_item = dict(item)
            copy_item["key"] = base_key
            copy_item["shortKey"] = base_key
            items.insert(i + 1, copy_item)
            existing_keys.add(base_key)
            inserted += 1
            i += 2  # 跳过插入的条目
        else:
            i += 1
    if inserted:
        print(f"  [IC2Tools] Inserted {inserted} bare-key copies for IC2 chargeable tools")


def _fix_aspectrecipeindex_keys(items: list):
    """修正 aspectrecipeindex:aspect 物品的 key：用 originalName 替换无意义的 NBT hash。
    aspectrecipeindex:aspect_85gjJ462zA0lgsJ → aspectrecipeindex:aspect_Terra"""
    fixed = 0
    for item in items:
        if item.get("rawKey") == "aspectrecipeindex:aspect":
            oname = item.get("originalName", "")
            if oname:
                new_key = f"aspectrecipeindex:aspect_{oname}"
                if item.get("key") != new_key:
                    item["key"] = new_key
                    fixed += 1
    if fixed:
        print(f"  [AspectRecipeIndex] Fixed {fixed} keys (NBT hash → originalName)")


def _regress_nbt_only_items(items: list):
    """退行机制：对于 rawKey 唯一的 item（只有一个 NBT 变体），
    如果其 key（带 NBT 后缀）从未在 recipe 中出现过，则退行 key → rawKey。

    按全表统计 rawKey；配方补充的变体不要求与创造列表连续排列。
    使用 nbt_regress_cache.json 缓存已确认的结果，避免重复扫描 recipe。
    """
    recipes_dir = os.path.join(DUMPS_DIR, "recipes")

    # Step 0: 空 NBT (nbt: "{}") 对应的固定 hash 后缀 _gLuB0gegBqh1aRe，直接退行
    EMPTY_NBT_SUFFIX = "_gLuB0gegBqh1aRe"
    empty_nbt_regressed = 0
    existing_keys = {item.get("key") for item in items}
    for item in items:
        k = item.get("key", "")
        rk = item.get("rawKey", "")
        # 已有裸 key 时保留空 NBT 变体，避免两条记录共用 key 却指向不同图片。
        if k and rk and k.endswith(EMPTY_NBT_SUFFIX) and k != rk and rk not in existing_keys:
            item["key"] = rk
            empty_nbt_regressed += 1
    if empty_nbt_regressed:
        print(f"  [NBT regress] Stripped empty-NBT suffix ({EMPTY_NBT_SUFFIX}) from {empty_nbt_regressed} items")

    # Step 1: 找出所有 rawKey 唯一的 NBT 单例，采集待检查的 key
    nbt_keys_to_check = {}   # key → index in items
    raw_key_counts = Counter(item.get("rawKey", "") for item in items)
    for i, item in enumerate(items):
        rk = item.get("rawKey", "")
        k = item.get("key", "")
        if raw_key_counts[rk] == 1 and k and rk and k != rk:
            nbt_keys_to_check[k] = i

    if not nbt_keys_to_check:
        print("  [NBT regress] No NBT-singleton items to check.")
        return

    # Step 2: 加载缓存，分流
    cache_regress, cache_keep = _load_nbt_regress_cache()
    cache_hits = 0
    regressed_from_cache = 0
    keys_to_scan = {}  # key → index (not in cache)

    for k, idx in nbt_keys_to_check.items():
        if k in cache_regress:
            # 缓存确认：recipe 中未出现 → 直接退行
            items[idx]["key"] = items[idx]["rawKey"]
            regressed_from_cache += 1
            cache_hits += 1
        elif k in cache_keep:
            # 缓存确认：recipe 中出现过 → 跳过
            cache_hits += 1
        else:
            keys_to_scan[k] = idx

    if regressed_from_cache:
        print(f"  [NBT regress] {regressed_from_cache} keys regressed from cache")
    if cache_hits:
        print(f"  [NBT regress] {cache_hits} keys resolved from cache, {len(keys_to_scan)} new keys need scanning")

    if not keys_to_scan:
        return

    if not os.path.isdir(recipes_dir):
        print("  [NBT regress] recipes 目录不存在，跳过剩余 {len(keys_to_scan)} 个 key 的扫描")
        return

    # Step 3: 扫描 recipe 文件，纯字符串匹配（仅对新 key）
    print(f"  [NBT regress] Scanning recipes for {len(keys_to_scan)} new keys...")
    unmatched = set(keys_to_scan.keys())
    recipe_files = []
    for root, dirs, files in os.walk(recipes_dir):
        dirs[:] = [name for name in dirs if name.casefold() != "filtered"]
        for fn in files:
            if fn.endswith(".json"):
                recipe_files.append(os.path.join(root, fn))
    total_rf = len(recipe_files)
    for ri, rpath in enumerate(recipe_files):
        if not unmatched:
            break
        with open(rpath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        unmatched = {k for k in unmatched if k not in content}
        if (ri + 1) % 50 == 0:
            print(f"    {ri + 1:,}/{total_rf:,} files, {len(unmatched)} keys remaining")

    # 更新缓存：未匹配到的 → regress，匹配到的 → keep
    new_regress = unmatched
    new_keep = set(keys_to_scan.keys()) - unmatched
    cache_regress.update(new_regress)
    cache_keep.update(new_keep)
    _save_nbt_regress_cache(cache_regress, cache_keep)
    print(f"  [NBT regress] Cache updated: +{len(new_regress)} regress, +{len(new_keep)} keep")

    # Step 4: 对未匹配到的 key，退行 key → rawKey
    regressed = 0
    for k in new_regress:
        idx = keys_to_scan[k]
        item = items[idx]
        if item.get("key") == k and item.get("rawKey"):
            item["key"] = item["rawKey"]
            regressed += 1

    total_regressed = regressed_from_cache + regressed
    print(f"  [NBT regress] Regressed {regressed} items from scan ({total_regressed} total: key → rawKey, NBT suffix stripped).")


# ============================================================
# Custom item injection
# ============================================================

def _inject_custom_items(items: list):
    """在 item 列表末尾强行注入自定义条目（如编程电路各配置变体）。"""
    n = len(items)

    # 编程电路 (gregtech:gt.integrated_circuit) — 配置 1~24
    for i in range(1, 25):
        items.append({
            "shortKey": f"gregtech:gt.integrated_circuit:{i}",
            "key": f"gregtech:gt.integrated_circuit:{i}",
            "originalName": "Programmed Circuit",
            "translatedName": f"编程电路{i}",
            "tooltips": f"编程电路<br>配置== {i}<br>右键以重新配置<br>需要螺丝刀或编程工具",
            "mod": "GregTech",
            "iconName": f"icon_gregtech_gt.integrated_circuit_{i}.png",
            "size": 16,
            "frameCount": 1,
        })

    print(f"  Injected {len(items) - n} custom items ({n} -> {len(items)})")


# ============================================================
# Tasks
# ============================================================

VERSION_TAG = version_tag()
VERSION = os.path.join(_PROJECT_ROOT, "wikitext", "Data", "Versions", VERSION_TAG)


def _load_itemicon_map():
    """加载 itemicon.json 并构建 shortKey -> {iconName, size, frameCount} 的查找表。"""
    path = reference_path("itemicon.json")
    print(f"  Loading icon data: {path} ({os.path.getsize(path) / 1024 / 1024:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    icon_map = {}
    skipped = 0
    for entry in data["shortKey"]:
        sk = entry.get("shortKey")
        if not sk:
            skipped += 1
            continue
        icon_map[sk] = {
            "iconName": entry.get("iconName", ""),
            "size": entry.get("size", 0),
            "frameCount": entry.get("frameCount", 1),
        }
    del data
    print(f"  Icon map: {len(icon_map):,} entries ({skipped} skipped without shortKey)")
    return icon_map


def _merge_item_icons(items: list, icon_map: dict):
    """将 icon 数据合并到每个 item 上。对每个 shortKey 生成一组候选 key
    （原 key + 逐层剥离 _ 后缀后的 base），依次尝试直接匹配和前缀扩展匹配。"""
    missing = []
    retry_strip = 0
    retry_prefix = 0
    # 保留原有“首个前缀匹配”的顺序，避免每个缺失变体都全扫图标表。
    prefix_icons = {}
    for icon_key, icon_value in icon_map.items():
        for offset, character in enumerate(icon_key):
            if character == "_":
                prefix_icons.setdefault(icon_key[:offset], icon_value)
    for i, item in enumerate(items):
        sk = item.get("shortKey", "")
        icon = icon_map.get(sk)

        if icon is not None:
            item.update(icon)
            continue

        # 构建候选列表：原始 key + 逐层剥离 _ 后缀
        candidates = [sk]
        base = sk
        while "_" in base:
            base, _, _ = base.rpartition("_")
            if base:
                candidates.append(base)

        # 对每个候选依次尝试直接匹配和前缀扩展
        found = False
        for cand in candidates:
            # 直接匹配
            icon = icon_map.get(cand)
            if icon is not None:
                if cand != sk:
                    retry_strip += 1
                found = True
                break
            # 前缀扩展：icon 中可能以 cand_ 开头
            icon = prefix_icons.get(cand)
            if icon is not None:
                retry_prefix += 1
                found = True
            if found:
                break

        if not found:
            missing.append((i, sk))
            if len(missing) <= 5:
                print(f"  ERROR: item[{i}] shortKey='{sk}' not found in itemicon.json!")
        else:
            item.update(icon)

    if retry_strip:
        print(f"  Retry-matched {retry_strip} items via suffix stripping.")
    if retry_prefix:
        print(f"  Retry-matched {retry_prefix} items via prefix expansion.")
    if missing:
        raise SystemExit(
            f"\n  合并失败：{len(missing)} 个 item 在 itemicon.json 中找不到对应条目！"
            f"\n  前 5 个缺失: {missing[:5]}"
        )
    # 移除 advitempanel 自带的 icon 字段，统一使用 itemicon 的 iconName
    for item in items:
        item.pop("icon", None)
    print(f"  Merged icon data into {len(items):,} items.")


def _load_fluid_map():
    """加载 fluid.json 并构建 gtDisplayKey -> fluid_data 的查找表。"""
    path = reference_path("fluid.json")
    print(f"  Loading fluid data: {path} ({os.path.getsize(path) / 1024 / 1024:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    fluid_map = {}
    for entry in data["fluids"]:
        gtk = entry.get("gtDisplayKey", "")
        if gtk:
            fluid_map[gtk] = entry
    del data
    print(f"  Fluid map: {len(fluid_map):,} entries")
    return fluid_map


def _merge_fluid_data(items: list, fluid_map: dict):
    """将 fluid 数据合并到 items 上。按 item['key'] → fluid['gtDisplayKey'] 匹配。
    匹配后将 shortKey / key 改写为 fluid.key，其余字段 item 优先、fluid 补充。
    fluid 中未匹配到的条目会打印 warning。"""
    # 记录所有 item 的原始 key，用于检查 fluid 中未匹配的
    item_keys = {item.get("key", "") for item in items}

    merged = 0
    fluid_matched = set()
    for item in items:
        item_key = item.get("key", "")
        fluid = fluid_map.get(item_key)
        if fluid is None:
            continue

        fluid_key = fluid.get("key", "")
        # 改写 shortKey 和 key
        item["shortKey"] = fluid_key
        item["key"] = fluid_key

        # fluid 属性补充（item 已有则不覆盖；黑名单：key / gtDisplayKey 不上）
        _FLUID_SKIP = {"key", "gtDisplayKey"}
        for fk, fv in fluid.items():
            if fk in _FLUID_SKIP or fk in item:
                continue
            if fv is not None:
                item[fk] = fv

        fluid_matched.add(item_key)
        merged += 1

    unmatched = [gtk for gtk in fluid_map if gtk not in fluid_matched]
    if unmatched:
        print(f"  WARNING: {len(unmatched)} fluids not matched to any item:")
        for gtk in unmatched[:10]:
            f = fluid_map[gtk]
            print(f"    gtDisplayKey={gtk}  fluid.key={f.get('key','?')}")

    print(f"  Fluid-merged {merged} items (shortKey/key rewritten to fluid.key).")


def do_items(debug: bool):
    """Split advitempanel.json -> Items/"""
    print("=" * 50)
    print("[Items]")
    print("=" * 50)
    path = os.path.join(DUMPS_DIR, "advitempanel.json")
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"  Loading {path} ({size_mb:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    items = data["shortKey"]
    print(f"  Total: {len(items):,} items")
    del data

    # ① 合并 itemicon.json 数据（必须在 fluid 合并之前，因为之后 shortKey 会被改写）
    icon_map = _load_itemicon_map()
    _merge_item_icons(items, icon_map)

    # ② 合并 fluid.json 数据（改写 GregTech_FluidDisplay → fluid.key）
    fluid_map = _load_fluid_map()
    _merge_fluid_data(items, fluid_map)

    # ③ 注入自定义条目
    _inject_custom_items(items)

    # ④ 清理无效格式码
    _clean_tooltips(items)

    # ④b 修正天域之华 aspect：从 NBT 提取真实要素合并到 aspect
    _fix_wisp_aspects(items)

    # ④c IC2 充电工具：:1 变体复制为去 meta 裸 key
    _fix_ic2_chargeable_tools(items)

    # ⑤ key/shortKey 对调（提前到写入之前）
    for item in items:
        sk = item.pop("shortKey", None)
        k = item.get("key")
        if sk and k:
            item["rawKey"] = k
            item["key"] = sk
        elif sk:
            item["key"] = sk

    _add_bee_query_keys(items)

    # ⑤b 修正 aspectrecipeindex:aspect 的 key：用 originalName 替换无意义的 NBT hash
    _fix_aspectrecipeindex_keys(items)

    # ⑥ 退行：唯一 NBT 变体且 recipe 中未引用 → key 退行为 rawKey
    _regress_nbt_only_items(items)

    # ⑥b 复制 aspectrecipeindex:aspect 物品 → 单独写入 Aspects/（保留原 Items 中的一份）
    aspect_items = [it for it in items if it.get("rawKey") == "aspectrecipeindex:aspect"]
    if aspect_items:
        print(f"  [AspectItems] Copying {len(aspect_items)} aspect items → Aspects/")
        write_batches(aspect_items, split_array(aspect_items, "items", debug), "items",
                      os.path.join(VERSION, "Aspects"))

    # 调试模式沿用最多五批的输入范围；正式物品输出只保留按 key 分桶的一份。
    if debug:
        batches = split_array(items, "items", True)
        items = items[:batches[-1][1]] if batches else []
    write_item_names(items)
    clear_generated_pages(Path(VERSION) / "Items", {".tabx"})


def _scan_schema(items: list) -> list[dict]:
    """Scan all items and build a Tabx field list dynamically."""
    # Collect all keys and infer types
    key_types: dict[str, set] = {}
    for item in items:
        for k, v in item.items():
            key_types.setdefault(k, set()).add(type(v).__name__)

    # Priority ordering for first 5 fields
    priority = ["shortKey", "key", "translatedName", "originalName", "tooltips"]
    ordered = []
    for p in priority:
        if p in key_types:
            ordered.append(p)
    # Remaining fields, sorted alphabetically
    rest = sorted(k for k in key_types if k not in priority)
    ordered += rest

    fields = []
    for k in ordered:
        types = key_types[k]
        if "dict" in types or "list" in types:
            t = "string"   # nested objects -> JSON string
        elif "int" in types or "float" in types:
            t = "number"
        elif "bool" in types:
            t = "boolean"
        else:
            t = "string"
        field = {"name": k, "type": t, "title": {"en": k, "zh": k}}
        if "dict" in types or "list" in types:
            # 同列也可能有普通字符串；统一编码并声明，读取端无需猜测字符串内容。
            field["jsonEncoded"] = True
        fields.append(field)
    return fields


def _tabx_row(item: dict, fields: list[dict]) -> list:
    """Convert an item to a tabx data row based on field list."""
    row = []
    for f in fields:
        name = f["name"]
        val = item.get(name)
        if f.get("jsonEncoded") and val is not None:
            val = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
        row.append(val)  # None stays as null in JSON
    return row


def do_items_tabx(debug: bool):
    """Split advitempanel.json -> Items/ as .tabx tables (dynamic schema)"""
    print("=" * 50)
    print("[Items --tabx]")
    print("=" * 50)
    path = os.path.join(DUMPS_DIR, "advitempanel.json")
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"  Loading {path} ({size_mb:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    items = data["shortKey"]
    del data

    # ① 合并 itemicon.json 数据（必须在 fluid 合并之前）
    icon_map = _load_itemicon_map()
    _merge_item_icons(items, icon_map)

    # ② 合并 fluid.json 数据（改写 GregTech_FluidDisplay → fluid.key）
    fluid_map = _load_fluid_map()
    _merge_fluid_data(items, fluid_map)

    # ③ 注入自定义条目
    _inject_custom_items(items)

    # ④ 清理无效格式码
    _clean_tooltips(items)

    # ④b 修正天域之华 aspect：从 NBT 提取真实要素合并到 aspect
    _fix_wisp_aspects(items)

    # ④c IC2 充电工具：:1 变体复制为去 meta 裸 key
    _fix_ic2_chargeable_tools(items)

    # ⑤ key/shortKey 对调
    for item in items:
        sk = item.pop("shortKey", None)
        k = item.get("key")
        if sk and k:
            item["rawKey"] = k
            item["key"] = sk
        elif sk:
            item["key"] = sk

    _add_bee_query_keys(items)

    # ⑤b 修正 aspectrecipeindex:aspect 的 key：用 originalName 替换无意义的 NBT hash
    _fix_aspectrecipeindex_keys(items)

    # ⑥ 退行：唯一 NBT 变体且 recipe 中未引用 → key 退行为 rawKey
    _regress_nbt_only_items(items)

    # ⑥b 复制 aspectrecipeindex:aspect 物品 → 单独写入 Aspects/（保留原 Items 中的一份）
    aspect_items = [it for it in items if it.get("rawKey") == "aspectrecipeindex:aspect"]
    if aspect_items:
        print(f"  [AspectItems] Copying {len(aspect_items)} aspect items → Aspects/")
        write_batches(aspect_items, split_array(aspect_items, "items", debug), "items",
                      os.path.join(VERSION, "Aspects"))

    if debug:
        batches = split_array(items, "items", True)
        items = items[:batches[-1][1]] if batches else []
    write_item_tables(items)
    write_item_names(items, write_items=False)


def write_item_tables(items):
    """按最终 key 输出 TABX 桶；成功后移除旧格式与不再使用的数字分页。"""
    fields = _scan_schema(items)
    rows = [_tabx_row(item, fields) for item in items]
    bucket_count = choose_bucket_count(sum(len(encode_json(row)) + 1 for row in rows), DEFAULT_MAX_BYTES)
    metadata = {"sources": f"从{VERSION_TAG}导出", "schema": {"fields": fields},
                "hashAlgorithm": HASH_ALGORITHM, "bucketCount": bucket_count}
    groups = defaultdict(list)
    for item, row in zip(items, rows):
        key = item.get("key")
        if not isinstance(key, str) or not key:
            raise ValueError("物品分桶要求每条记录具有最终 key")
        groups[bucket_id(key, bucket_count)].append(row)
    output = Path(VERSION) / "Items"
    written = set()
    for bucket, rows in sorted(groups.items()):
        for part, content in enumerate(array_pages(rows, metadata, "data")):
            path = output / bucket / f"{part}.tabx"
            write_bounded(path, content, DEFAULT_MAX_BYTES)
            written.add(path)
    clear_generated_pages(output, {".json", ".tabx"}, written)
    print(f"  [Items TABX] {len(items):,} rows -> {len(written)} bucket pages")


def do_fluids(debug: bool):
    """Fluids are now merged into Items — no separate output."""
    print("=" * 50)
    print("[Fluids] — skipped (已合并到 Items 中，不单独产出)")
    print("=" * 50)


def _index_wildcard_item(item, wildcard_index, wildcard_nbt_index=None):
    """用已导出的完整物品键索引注册名；配方和矿辞共享相同的 damage/NBT 候选范围。"""
    raw = item.get("rawKey") or item.get("key", "")
    key = item.get("key", "")
    if not raw or not key or raw.endswith(":32767"):
        return
    base = raw.rsplit(":", 1)[0] if raw.count(":") >= 2 and raw.rsplit(":", 1)[1].isdigit() else raw
    wildcard_index.setdefault(base, set()).add(key)
    nbt = item.get("nbt") or ""
    if nbt and wildcard_nbt_index is not None:
        wildcard_nbt_index.setdefault((base, nbt_identity(nbt)), set()).add(key)


def normalize_ore_dictionary(entries, items):
    """发布矿辞只引用真实子类型；无候选通配成员写入报告，保留矿辞内其他有效成员。"""
    wildcard_index, item_keys = {}, set()
    for item in items:
        _index_wildcard_item(item, wildcard_index)
        item_keys.add(item["key"])
    flat, unresolved, missing_exact = {}, [], []
    expanded = 0
    for entry in entries:
        members, seen = [], set()
        for key in entry["items"]:
            if key.endswith(":32767"):
                candidates = sorted(wildcard_index.get(key[:-6], ()))
                expanded += 1
                if not candidates:
                    unresolved.append({"oreDict": entry["oreDict"], "key": key})
            else:
                candidates = [key]
                if key not in item_keys:
                    missing_exact.append({"oreDict": entry["oreDict"], "key": key})
            for candidate in candidates:
                if candidate not in seen:
                    seen.add(candidate)
                    members.append(candidate)
        flat[entry["oreDict"]] = members
    return flat, {"expandedWildcardMembers": expanded, "unresolvedWildcardMembers": unresolved,
                  "missingExactMembers": missing_exact}


def do_oredictionary(debug: bool):
    """Split oreDictionary.json -> OreDictionary/  as flat {oreDict: items} objects"""
    print("=" * 50)
    print("[OreDictionary]")
    print("=" * 50)
    path = os.path.join(DUMPS_DIR, "oreDictionary.json")
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"  Loading {path} ({size_mb:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    entries = data["oreDictionaries"]
    del data

    items_dir = Path(VERSION) / "Items"
    items = read_items(items_dir)
    first_item = next(items, None)
    if first_item is None:
        raise ValueError(
            f"矿辞处理需要当前版本非空的 Items 数据：{items_dir}；"
            "请先运行 python split_all.py items oredict"
        )
    flat, report = normalize_ore_dictionary(entries, chain((first_item,), items))
    Path(AUX_DIR).mkdir(parents=True, exist_ok=True)
    Path(AUX_DIR, "oredictionary-associations.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  Expanded {report['expandedWildcardMembers']:,} wildcard member(s); "
          f"{len(report['unresolvedWildcardMembers']):,} without exported candidates")

    keys = list(flat.keys())
    print(f"  Total: {len(keys):,} oreDict keys")

    # Split by size
    batches = []
    batch_start = 0
    current_size = len('{\n}'.encode("utf-8"))

    for i, k in enumerate(keys):
        pair_json = json.dumps({k: flat[k]}, ensure_ascii=False, indent=2)
        pair_body = pair_json[1:-1].rstrip()   # strip outer { }
        pair_bytes = len(pair_body.encode("utf-8"))
        separator = 2 if i > batch_start else 0
        total = pair_bytes + separator

        if current_size + total > MAX_FILE_SIZE and i > batch_start:
            batches.append((batch_start, i))
            if debug and len(batches) >= 5:
                print(f"    [DEBUG] Collected 5 batches, stopping early.")
                batch_start = -1
                break
            batch_start = i
            current_size = len('{\n}'.encode("utf-8")) + pair_bytes
        else:
            current_size += total

        if (i + 1) % 5000 == 0:
            print(f"    Batching: {i + 1:,}/{len(keys):,}, batch {len(batches) + 1}")

    if batch_start >= 0 and batch_start < len(keys):
        batches.append((batch_start, len(keys)))

    # Write
    output_dir = os.path.join(VERSION, "OreDictionary")
    os.makedirs(output_dir, exist_ok=True)
    total_out = 0

    for file_idx, (start, end) in enumerate(batches):
        payload = {key: flat[key] for key in keys[start:end]}
        out_path = os.path.join(output_dir, f"{file_idx}.json")
        size = write_version_json(out_path, payload)
        count = end - start
        print(f"  -> {file_idx}.json: {count:,} keys, {size / 1024:.1f} KB (OK)")
        total_out += count

    # 完整重建成功后清理缩减的旧页，防止旧矿辞成员再次参与查询。
    if not debug:
        keep = [Path(output_dir, f"{index}.json") for index in range(len(batches))]
        clear_generated_pages(output_dir, {".json"}, keep)
    print(f"  Done: {total_out:,} keys -> {len(batches)} file(s)\n")


def _split_flat_dict_impl(data: dict, output_dir: str, debug: bool):
    """Split a flat {key: value} dict into multiple JSON files by MAX_FILE_SIZE."""
    keys = list(data.keys())
    print(f"  Total: {len(keys):,} keys")

    # Pre-serialize
    serialized = []
    for k in keys:
        serialized.append(serialize_item({k: data[k]})[1:-1].rstrip())  # strip outer { }
        if len(serialized) % 10000 == 0:
            print(f"    Serialized: {len(serialized):,}/{len(keys):,}")

    shell = len('{\n}'.encode("utf-8"))
    batches = []
    batch_start = 0
    current_size = shell

    for i, entry_str in enumerate(serialized):
        entry_bytes = len(entry_str.encode("utf-8"))
        indent = 2
        lines = entry_str.count("\n") + 1
        indent_extra = lines * indent
        separator = 2 if i > batch_start else 0
        total = entry_bytes + indent_extra + separator

        if current_size + total > MAX_FILE_SIZE and i > batch_start:
            batches.append((batch_start, i))
            if debug and len(batches) >= 5:
                print(f"    [DEBUG] Collected 5 batches, stopping early.")
                batch_start = -1
                break
            batch_start = i
            current_size = shell + entry_bytes + indent_extra
        else:
            current_size += total

        if (i + 1) % 10000 == 0:
            print(f"    Batching: {i + 1:,}/{len(keys):,}, batch {len(batches) + 1}")

    if batch_start >= 0 and batch_start < len(keys):
        batches.append((batch_start, len(keys)))

    # Write
    os.makedirs(output_dir, exist_ok=True)
    total_out = 0

    for file_idx, (start, end) in enumerate(batches):
        payload = {key: data[key] for key in keys[start:end]}
        out_path = os.path.join(output_dir, f"{file_idx}.json")
        size = write_version_json(out_path, payload)
        count = end - start
        print(f"  -> {file_idx}.json: {count:,} keys, {size / 1024:.1f} KB (OK)")
        total_out += count

    print(f"  Done: {total_out:,} keys -> {len(batches)} file(s)\n")


def _dedupe_gtmaterial_by_localized_name(data: dict) -> int:
    """Remove duplicate gtmaterial entries sharing the same LocalizedName."""
    from collections import defaultdict

    by_ln = defaultdict(list)
    for key, val in data.items():
        ln = val.get("LocalizedName", "")
        if ln:
            by_ln[ln].append(key)

    removed = 0
    for ln, keys in by_ln.items():
        if len(keys) <= 1:
            continue

        def _sort_key(k):
            mod = data[k].get("Mod", "")
            is_gt = "GregTech" in (mod if isinstance(mod, list) else [mod])
            n_ore = len(data[k].get("OrePrefixes", {}))
            return (is_gt, n_ore)

        keys.sort(key=_sort_key, reverse=True)
        for k in keys[1:]:
            del data[k]
            removed += 1

    return removed


def _extract_ore_rows_from_gtmaterial(data: dict) -> list[list[str]]:
    """Build rows as [oreDict, tranlatedName] from gtmaterial ore entries."""
    import re

    rows = []
    for key, val in data.items():
        ore_prefixes = val.get("OrePrefixes", {})
        ore_value = ore_prefixes.get("ore")
        if not ore_value:
            continue

        match = re.search(r'(?:^|:)ore([A-Z][A-Za-z0-9_\-]*)$', ore_value)
        ore_dict = match.group(1) if match else None
        if not ore_dict and re.fullmatch(r'[A-Za-z0-9_\-]+', key):
            ore_dict = key
        if not ore_dict:
            continue

        translated_name = (
            val.get("LocalizedName")
            or val.get("Name")
            or val.get("DefaultName")
            or key
        )
        rows.append([ore_dict, translated_name])

    rows.sort(key=lambda row: row[0])
    return rows


def _append_missing_hee_ore_rows(rows: list[list[str]], oredict_map: dict, item_display_map: dict) -> list[list[str]]:
    """Append missing oreHee* entries that are absent from Gtmaterial.

    For Hardcore Ender Expansion ores, use the ore item's translatedName and
    strip the trailing '矿石' to derive the material-like Chinese name.
    """
    existing = {row[0] for row in rows}

    for ore_name, ore_items in oredict_map.items():
        if not isinstance(ore_name, str) or not ore_name.startswith("oreHee"):
            continue
        ore_dict = ore_name[len("ore"):]
        if ore_dict in existing:
            continue

        ore_key = _select_primary_ore_item_key(ore_items)
        if not ore_key:
            continue

        display = item_display_map.get(ore_key, {})
        ore_translated_name = display.get("translatedName", "") or ""
        if ore_translated_name.endswith("矿石"):
            translated_name = ore_translated_name[:-2]
        else:
            translated_name = ore_translated_name or ore_dict

        rows.append([ore_dict, translated_name])
        existing.add(ore_dict)

    rows.sort(key=lambda row: row[0])
    return rows


def _load_oredict_item_map() -> dict:
    """Load oreDictionary dump and build {oreDictName: [itemKey, ...]}."""
    path = os.path.join(DUMPS_DIR, "oreDictionary.json")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    result = {}
    for entry in data.get("oreDictionaries", []):
        ore_dict = entry.get("oreDict")
        if ore_dict:
            result[ore_dict] = entry.get("items", [])
    return result


def _load_item_display_map() -> dict:
    """Load advitempanel/itemicon dumps and build {itemKey: displayData}."""
    path = os.path.join(DUMPS_DIR, "advitempanel.json")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    icon_map = _load_itemicon_map()
    result = {}
    for item in data.get("shortKey", []):
        if not isinstance(item, dict):
            continue
        translated = item.get("translatedName") or item.get("originalName") or ""
        short_key = item.get("shortKey") or item.get("key") or item.get("rawKey") or ""
        icon = icon_map.get(short_key, {})
        display = {
            "translatedName": translated or short_key,
            "iconName": icon.get("iconName", ""),
            "frameCount": icon.get("frameCount", 0),
        }
        for key_name in ("shortKey", "key", "rawKey"):
            item_key = item.get(key_name)
            if item_key and item_key not in result:
                result[item_key] = dict(display)
    return result


def _normalize_oredict_item_key(item) -> str:
    """Extract item key from an oreDictionary item entry."""
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return item.get("key") or item.get("shortKey") or item.get("rawKey") or ""
    return ""


def _select_primary_ore_item_key(ore_items: list) -> str:
    """Pick one representative ore item, preferring gregtech entries."""
    normalized = [_normalize_oredict_item_key(item) for item in ore_items]
    normalized = [key for key in normalized if key]
    if not normalized:
        return ""
    for key in normalized:
        if key.startswith("gregtech:"):
            return key
    return normalized[0]


def _enrich_ore_rows(rows: list[list[str]], oredict_map: dict, item_display_map: dict) -> list[list[str]]:
    """Append oreTranslatedName/key/image columns using ore{oreDict} lookup."""
    enriched = []
    for ore_dict, translated_name in rows:
        ore_key = ""
        ore_translated_name = ""
        image = ""
        ore_items = oredict_map.get("ore" + ore_dict, [])
        ore_key = _select_primary_ore_item_key(ore_items)
        if ore_key:
            display = item_display_map.get(ore_key, {})
            ore_translated_name = display.get("translatedName", "") or ore_key
            if display.get("frameCount") == 1:
                image = display.get("iconName", "")
        enriched.append([ore_dict, translated_name, ore_translated_name, ore_key, image])
    return enriched


def _build_ore_dict_short_pattern(target: str, candidates: list[str]) -> str:
    """Build the shortest unique start-anchored regex fragment for one oreDict suffix."""
    others = [item for item in candidates if item != target]
    if not others:
        return target

    target_len = len(target)
    for prefix_len in range(1, target_len + 1):
        prefix = target[:prefix_len]
        if any(other.startswith(prefix) for other in others):
            continue
        if prefix_len == target_len:
            return target
        return prefix + ".*"

    return target


def _append_ore_dict_short_patterns(rows: list[list[str]]) -> list[list[str]]:
    """Append one shortest unique regex fragment column for oreDict matching."""
    ore_dicts = [row[0] for row in rows if row and row[0]]
    result = []

    for row in rows:
        ore_dict = row[0]
        result.append(list(row) + [_build_ore_dict_short_pattern(ore_dict, ore_dicts)])

    return result


def _write_ores_table_json(rows: list[list[str]]):
    """Write ore helper data in tabx-style schema to Ores/0.tabx."""
    output_dir = os.path.join(VERSION, "Ores")
    os.makedirs(output_dir, exist_ok=True)

    payload = {
        "sources": f"从{VERSION_TAG}导出（基于 Gtmaterial 的 ore 前缀提取）",
        "schema": {
            "fields": [
                {
                    "name": "oreDict",
                    "type": "string",
                    "title": {"en": "oreDict", "zh": "矿典"},
                },
                {
                    "name": "tranlatedName",
                    "type": "string",
                    "title": {"en": "tranlatedName", "zh": "中文名"},
                },
                {
                    "name": "oreTranslatedName",
                    "type": "string",
                    "title": {"en": "oreTranslatedName", "zh": "矿石中文名"},
                },
                {
                    "name": "key",
                    "type": "string",
                    "title": {"en": "key", "zh": "物品键值"},
                },
                {
                    "name": "image",
                    "type": "string",
                    "title": {"en": "image", "zh": "图标文件名"},
                },
                {
                    "name": "oreDictShort",
                    "type": "string",
                    "title": {"en": "oreDictShort", "zh": "矿典简称"},
                },
            ]
        },
        "data": rows,
    }

    out_path = os.path.join(output_dir, "0.tabx")
    size = write_version_json(out_path, payload)
    print(f"  -> Ores/0.tabx: {len(rows):,} rows, {size / 1024:.1f} KB (OK)")


def do_gtmaterial(debug: bool):
    """Split gtmaterial.json -> Gtmaterial/"""
    print("=" * 50)
    print("[GtMaterial]")
    print("=" * 50)
    path = os.path.join(DUMPS_DIR, "gtmaterial.json")
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"  Loading {path} ({size_mb:.1f} MB)...")
    # 预处理：裸 Infinity 不是合法 JSON，只替换值位置的（不在引号内）
    import re
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r'(?<=: )Infinity(?=[,\n\r])', '2147483647', raw)
    data = json.loads(raw)

    # 去重：同 LocalizedName 有多个条目（如 GregTech vs GT++），优先保留 GregTech
    from collections import defaultdict
    by_ln = defaultdict(list)
    for key, val in data.items():
        ln = val.get("LocalizedName", "")
        if ln:
            by_ln[ln].append(key)
    removed = 0
    for ln, keys in by_ln.items():
        if len(keys) <= 1:
            continue
        # 排序：GregTech 优先，其次 OrePrefixes 数量多的
        def _sort_key(k):
            mod = data[k].get("Mod", "")
            is_gt = "GregTech" in (mod if isinstance(mod, list) else [mod])
            n_ore = len(data[k].get("OrePrefixes", {}))
            return (is_gt, n_ore)
        keys.sort(key=_sort_key, reverse=True)
        for k in keys[1:]:  # 只保留最优，删除其余的
            del data[k]
            removed += 1
    if removed:
        print(f"  Deduped: removed {removed} duplicate LocalizedName entries (GregTech preferred)")

    oredict_map = _load_oredict_item_map()
    item_display_map = _load_item_display_map()
    ore_rows = _extract_ore_rows_from_gtmaterial(data)
    ore_rows = _append_missing_hee_ore_rows(ore_rows, oredict_map, item_display_map)
    ore_rows = _enrich_ore_rows(ore_rows, oredict_map, item_display_map)
    ore_rows = _append_ore_dict_short_patterns(ore_rows)
    _write_ores_table_json(ore_rows)

    _split_flat_dict_impl(data, os.path.join(VERSION, "Gtmaterial"), debug)


def do_ticmaterial(debug: bool):
    """Split ticmaterial.json -> Ticmaterial/"""
    print("=" * 50)
    print("[TicMaterial]")
    print("=" * 50)
    path = os.path.join(DUMPS_DIR, "ticmaterial.json")
    size_mb = os.path.getsize(path) / 1024 / 1024
    print(f"  Loading {path} ({size_mb:.1f} MB)...")
    with open(path, "r", encoding="utf-8") as stream:
        data = json.load(stream)
    _split_flat_dict_impl(data, os.path.join(VERSION, "Ticmaterial"), debug)


def do_tc4research(debug: bool):
    """按当前数据源分页研究表，保留 tc4Researches 及每条研究的原字段。"""
    source = Path(DUMPS_DIR) / "tc4research.json"
    mod = local_mod_name()
    select_owner = False
    if not source.is_file() or not source.stat().st_size:
        # 完整导出记录注册者后，附属可直接提取自己的研究，省去逐模组启动游戏。
        source = Path(REFERENCE_DUMPS_DIR) / "tc4research.json"
        if not mod or mod == "GTNH" or not source.is_file() or not source.stat().st_size:
            print(f"跳过缺失或空的研究输入: {Path(DUMPS_DIR) / 'tc4research.json'}")
            return
        select_owner = True
    data = json.loads(source.read_text(encoding="utf-8"))
    records = data["tc4Researches"]
    if mod == "GTNH":
        excluded = addon_mods()
        records = [row for row in records if (row.get("sourceMod") or "").casefold() not in excluded]
    elif select_owner:
        if records and not any(row.get("sourceMod") for row in records):
            print(f"跳过 {mod} 研究：完整导出尚未记录 sourceMod")
            return
        records = [row for row in records if (row.get("sourceMod") or "").casefold() == mod.casefold()]
        print(f"  [Tc4research] {mod}: selected {len(records)} by sourceMod")
    output = Path(VERSION) / "Tc4research"
    written = []
    for part, content in enumerate(array_pages(
            records, data, "tc4Researches", DEFAULT_MAX_BYTES, emit_empty=True)):
        path = output / f"{part}.json"
        write_bounded(path, content, HARD_MAX_BYTES)
        written.append(path)
        if debug and len(written) == 5:
            break
    if not debug:
        clear_generated_pages(output, {".json"}, keep=written)
    print(f"  [Tc4research] {len(records):,} researches -> {len(written)} file(s)")


def _fix_recipe_format(recipes: list, filepath: str) -> list:
    """修正非标准 NEI recipe 格式为标准格式。
    如果配方已经是标准格式（有 outputFluids / inputFluids / outputItems），
    则跳过，避免未来数据已标准化时重复破坏。"""
    fixed = []
    for r in recipes:
        r = dict(r)

        # 已经是标准格式 → 不改
        if any(k in r for k in ("outputFluids", "inputFluids", "outputItems", "inputItems")):
            fixed.append(r)
            continue

        if "GasSiphonRecipeHandler" in filepath:
            # 旧格式：{fluid, planet, depth, outputAmount}
            # → 新格式：{outputFluids, eut, duration, metadata}
            fluid_key = r.pop("fluid", {}).get("key", "")
            amount = r.pop("outputAmount", 0)
            r["outputFluids"] = [{"key": fluid_key, "amount": amount}]
            # 保留Dumper从模组计算得到的功率；旧文件缺功率不能伪造为0。
            r["duration"] = 0
            r.setdefault("baseRatePerSecond", amount * 2)
            planet = r.pop("planet", "")
            depth = r.pop("depth", 1)
            r["metadata"] = {"planet": planet, "depth": depth}
        elif "SpacePumpModuleRecipeHandler" in filepath:
            # 旧格式：{fluid, planetType, gasType, amount}
            # → 新格式：{outputFluids, eut, duration, metadata}
            fluid_key = r.pop("fluid", {}).get("key", "")
            amount = r.pop("amount", 0)
            r["outputFluids"] = [{"key": fluid_key, "amount": amount}]
            r["eut"] = 0
            r["duration"] = 0
            planet_type = r.pop("planetType", 0)
            gas_type = r.pop("gasType", 0)
            r["metadata"] = {"planetType": planet_type, "gasType": gas_type}
        fixed.append(r)
    print(f"    Fixed {len(fixed)} non-standard recipes -> standard format")
    return fixed


def _extract_crop_from_nbt(nbt_str):
    """从 NBT 字符串中提取 crop 值，如 crop:"cropsnh:huckleberry" → cropsnh:huckleberry。"""
    import re
    m = re.search(r'crop:"([^"]+)"', nbt_str)
    return m.group(1) if m else None


def _iter_recipe_item_entries(value):
    """Yield object-form items from scalar, object, and candidate-array slots."""
    if isinstance(value, list):
        for entry in value:
            yield from _iter_recipe_item_entries(entry)
    elif isinstance(value, dict):
        if "key" in value or "beeIdentity" in value:
            yield value
        else:
            for entry in value.values():
                yield from _iter_recipe_item_entries(entry)


def _write_recipe_pages(recipes, metadata, output_dir):
    """精确计入包装和页/行 index；每条至多编码两次，移动到下一页时重算 index。"""
    shell = encode_json({**metadata, "recipes": []})
    prefix, suffix = shell[:-2], b"]}"
    pages, batch, size = [], [], len(shell)
    assigned = []
    for recipe in recipes:
        entry = dict(recipe)
        entry["index"] = f"{len(pages)}/{len(batch)}"
        encoded = encode_json(entry)
        if batch and size + len(encoded) + 1 > DEFAULT_MAX_BYTES:
            pages.append(prefix + b",".join(batch) + suffix)
            batch, size = [], len(shell)
            entry["index"] = f"{len(pages)}/0"
            encoded = encode_json(entry)
        if size + len(encoded) + bool(batch) > HARD_MAX_BYTES:
            raise ValueError(f"单条配方含 metadata/index 后超过分页上限 {HARD_MAX_BYTES}: {entry['index']}")
        size += len(encoded) + bool(batch)
        batch.append(encoded)
        assigned.append((recipe, entry["index"]))
    if batch:
        pages.append(prefix + b",".join(batch) + suffix)
    # 所有新页通过硬限检查后才清除旧数字分页，避免坏单条导致旧输出先被删。
    clear_generated_pages(output_dir, {".json"})
    for part, content in enumerate(pages):
        write_bounded(Path(output_dir) / f"{part}.json", content, HARD_MAX_BYTES)
    for recipe, index in assigned:
        recipe["index"] = index
    return len(pages), len(assigned)


class _RecipeAssociations:
    """一次建立物品身份索引，在发布分页前归一化配方并保留最终位置诊断。"""

    def __init__(self):
        items_dir = os.path.join(VERSION, "Items")
        # 精确物品关联与蜜蜂物种级关联是两个独立索引。
        print("  [NBT fix] Building exact and bee identity indexes from Items...")
        nbt_index = {}
        bee_index = {}
        wildcard_index = {}
        wildcard_nbt_index = {}
        for item in read_items(items_dir):
            rk = item.get("rawKey", "") or item.get("key", "")
            nbt = item.get("nbt", "") or ""
            sk = item.get("key", "")
            _index_wildcard_item(item, wildcard_index, wildcard_nbt_index)
            if rk and sk and nbt:
                nbt_index.setdefault((rk, nbt_identity(nbt)), set()).add(sk)
            query_key = item.get("queryKey") or _bee_query_key(item.get("beeIdentity"))
            if query_key and sk:
                bee_index.setdefault(query_key, set()).add(sk)
        print(
            f"  [NBT fix] Indexed {len(nbt_index)} exact NBT pair(s) and "
            f"{len(bee_index)} bee identity key(s)"
        )

        self.slot_keys = ("inputItems", "outputItems", "otherItems", "keyItem", "exampleInput",
                     "inputFluids", "outputFluids", "normalDrops", "rareDrops",
                     "additionalDrops", "infernalDrops", "eliteItems", "ultraItems",
                     "infernoItems", "fishItems", "junkItems", "treasureItems")

        self.nbt_index = nbt_index
        self.bee_index = bee_index
        self.wildcard_index = wildcard_index
        self.wildcard_nbt_index = wildcard_nbt_index
        self.total = self.replaced = 0
        self.missing = set()
        self.diagnostics = []

    def associate(self, recipe, relative_file):
        nbt_index, bee_index = self.nbt_index, self.bee_index
        wildcard_index, wildcard_nbt_index = self.wildcard_index, self.wildcard_nbt_index
        modified = False
        # 元数据32767是任意子类型规则；使用物品库中真实变体构成候选槽。
        # 不生成伪造的32767图标，也不随意选择某一个NBT变体。
        def expand_wildcard(entry):
            if isinstance(entry, list):
                expanded = []
                for candidate in entry:
                    value = expand_wildcard(candidate)
                    expanded.extend(value if isinstance(value, list) else [value])
                return expanded
            raw = entry if isinstance(entry, str) else entry.get("key", "") if isinstance(entry, dict) else ""
            if not raw.endswith(":32767"):
                return entry
            nbt = entry.get("nbt") if isinstance(entry, dict) else None
            choices = sorted(wildcard_nbt_index.get((raw[:-6], nbt_identity(nbt)), ())
                             if nbt else wildcard_index.get(raw[:-6], ()))
            if not choices:
                unresolved = dict(entry) if isinstance(entry, dict) else {"key": raw}
                unresolved["displayKey"] = "unresolved-wildcard:" + raw
                unresolved["associationError"] = "missing_concrete_wildcard_candidates"
                return unresolved
            return [dict({k: v for k, v in entry.items() if k != "nbt"}, key=key, rawKey=raw)
                    if isinstance(entry, dict) else key for key in choices]

        if isinstance(recipe.get("inputItems"), list):
            previous = recipe["inputItems"]
            recipe["inputItems"] = [expand_wildcard(entry) for entry in previous]
            modified = modified or recipe["inputItems"] != previous
        # 注魔中央物品也是一个输入槽；候选属于同一槽位，不增加材料数量。
        if recipe.get("keyItem") is not None:
            previous = recipe["keyItem"]
            recipe["keyItem"] = expand_wildcard(previous)
            modified = modified or recipe["keyItem"] != previous
        for slot_key in self.slot_keys:
            items = recipe.get(slot_key)
            if not isinstance(items, (list, dict)):
                continue
            for item in _iter_recipe_item_entries(items):
                nbt = item.get("nbt", "") or ""
                key = item.get("key", "")
                if not key:
                    continue
                original_key = key
                expected_bee_type = BEE_TYPES_BY_RAW_KEY.get(key)
                identity = item.get("beeIdentity")
                query_key = _bee_query_key(identity)
                if identity is not None or expected_bee_type:
                    if not query_key or (expected_bee_type and identity.get("type") != expected_bee_type):
                        self.diagnostics.append({
                            "file": relative_file,
                            "_recipe": recipe,
                            "recipeIndex": recipe.get("index"),
                            "slot": slot_key,
                            "rawKey": key,
                            "beeIdentity": identity,
                            "issue": "missing_or_mismatched_identity_reexport_required",
                        })
                    else:
                        item["queryKey"] = query_key
                        modified = True

                if not nbt:
                    continue
                self.total += 1
                # 先尝试精确 NBT 匹配；重复候选不任意取第一项。
                exact_candidates = nbt_index.get((key, nbt_identity(nbt)), set())
                if len(exact_candidates) == 1:
                    sk = next(iter(exact_candidates))
                    item["rawKey"] = key
                    item["key"] = sk
                    del item["nbt"]
                    self.replaced += 1
                    modified = True
                elif len(exact_candidates) > 1:
                    item["associationError"] = "ambiguous_exact_item_association"
                    item["displayKey"] = "unresolved-nbt:" + key
                    modified = True
                    if query_key:
                        item["displayKey"] = query_key
                        item["associationError"] = "ambiguous_exact_item_association"
                        modified = True
                    self.diagnostics.append({
                        "file": relative_file,
                        "_recipe": recipe,
                        "recipeIndex": recipe.get("index"),
                        "slot": slot_key,
                        "rawKey": key,
                        "issue": "ambiguous_exact_item_association",
                        "candidates": sorted(exact_candidates),
                    })
                else:
                    self.missing.add((key, nbt))
                    item["associationError"] = "missing_exact_nbt_item"
                    item["displayKey"] = "unresolved-nbt:" + key
                    modified = True
                    if query_key:
                        display_candidates = bee_index.get(query_key, set())
                        if len(display_candidates) == 1:
                            item["displayKey"] = next(iter(display_candidates))
                            modified = True
                        else:
                            issue = ("missing_display_item" if not display_candidates
                                     else "ambiguous_display_item")
                            # Never let rawKey fallback choose an arbitrary bee variant.
                            item["displayKey"] = query_key
                            item["associationError"] = issue
                            modified = True
                            self.diagnostics.append({
                                "file": relative_file,
                                "_recipe": recipe,
                                "recipeIndex": recipe.get("index"),
                                "slot": slot_key,
                                "rawKey": original_key,
                                "queryKey": query_key,
                                "issue": issue,
                                "candidates": sorted(display_candidates),
                            })
                    elif expected_bee_type:
                        item["displayKey"] = f"unresolved-bee:{expected_bee_type}"
                        item["associationError"] = "missing_or_mismatched_identity_reexport_required"
                        modified = True
        return modified

    def write_report(self):
        total, replaced = self.total, self.replaced
        missing = self.missing
        bee_diagnostics = []
        for source in self.diagnostics:
            row = {key: value for key, value in source.items() if key != "_recipe"}
            index = source["_recipe"].get("index")
            row["recipeIndex"] = index
            if isinstance(index, str) and len(index.split("/")) == 2 and all(part.isdecimal() for part in index.split("/")):
                row["file"] = os.path.join(os.path.dirname(row["file"]), index.split("/")[0] + ".json")
            bee_diagnostics.append(row)
        remaining = total - replaced
        print(f"  [NBT fix] Total NBT items: {total}, replaced: {replaced}, remaining: {remaining}")

        if missing:
            csv_path = os.path.join(AUX_DIR, "nbt_missing.csv")
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
                import csv
                w = csv.writer(f)
                w.writerow(["key", "nbt"])
                for key, nbt in sorted(missing):
                    w.writerow([key, nbt])
            print(f"  [NBT fix] Missing (key, nbt) written to {csv_path}: {len(missing)} unique rows")

        if bee_diagnostics:
            diagnostics_path = os.path.join(AUX_DIR, "bee_association_diagnostics.json")
            with open(diagnostics_path, "w", encoding="utf-8") as f:
                json.dump(bee_diagnostics, f, ensure_ascii=False, indent=2)
            print(
                f"  [Bee identity] Diagnostics written to {diagnostics_path}: "
                f"{len(bee_diagnostics):,} row(s)"
            )



def _remove_rebuilt_recipe_route(handler):
    """成功改回顺序分页后，仅移除当前机器的路由，保留未重建机器的配置。"""
    root = Path(VERSION) / "Recipes"
    path = root / "routing-manifest.json"
    if not path.exists():
        return
    report = json.loads(path.read_bytes())
    handlers = report.get("handlers", {})
    buckets = report.get("recipeBuckets", {})
    if handler not in handlers and handler not in buckets:
        return
    handlers.pop(handler, None)
    buckets.pop(handler, None)
    if not handlers and not buckets:
        path.unlink()
        return
    # 汇总只描述仍保持分桶的机器；shared 页的条数不能用文件数代替。
    report["sourceRecipes"] = sum(details["sourceRecipes"] for details in handlers.values())
    report["writtenRecipes"] = report["sourceRecipes"]
    report["files"] = sum(len(files) for details in handlers.values()
                          for files in details["routes"].values())
    report["sharedRecipes"] = sum(
        len(json.loads((root / relative).read_bytes())["recipes"])
        for details in handlers.values() for relative in details["routes"].get("shared", [])
    )
    temporary = path.with_suffix(".json.tmp")
    write_bounded(temporary, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), HARD_MAX_BYTES)
    temporary.replace(path)


def do_recipes(debug: bool):
    """Split recipes/**/*.json -> recipes/{mod}/{handler}/"""
    print("=" * 50)
    print("[Recipes]")
    print("=" * 50)

    recipes_dir = os.path.join(DUMPS_DIR, "recipes")
    json_files = []
    for root, dirs, files in os.walk(recipes_dir):
        dirs[:] = [name for name in dirs if name.casefold() != "filtered"]
        json_files.extend(os.path.join(root, name) for name in files if name.endswith(".json"))
    json_files.sort()
    print(f"  Found {len(json_files)} file(s)")

    associations = _RecipeAssociations()
    for fi, fp in enumerate(json_files):
        if debug and fi >= 3:
            print(f"\n  [DEBUG] Processed 3 files, skipping rest.")
            break
        print(f"\n  Processing: {fp}")
        try:
            with open(fp, "r", encoding="utf-8") as recipe_file:
                data = json.load(recipe_file)
        except Exception as e:
            print(f"    Error: {e}")
            continue

        recipes = [r for r in data.get("recipes", []) if isinstance(r, dict)]
        if not recipes:
            print(f"    Skipping (empty or all-null recipes)")
            continue

        # 修正非标准 recipe 格式（行星气体钻机 / 太空钻机）
        if "GasSiphonRecipeHandler" in fp or "SpacePumpModuleRecipeHandler" in fp:
            recipes = _fix_recipe_format(recipes, fp)

        # Special case: bee genetics NBT data makes individual recipes >2MB
        if "AcclimatiserRecipeHandler_acclimatiser" in fp:
            print(f"    Skipping (bee genetics NBT too large)")
            continue

        metadata = {k: v for k, v in data.items() if k != "recipes"}
        del data

        # Write
        rel = os.path.relpath(fp, recipes_dir)
        subdir = os.path.splitext(rel)[0]
        output_dir = os.path.join(VERSION, "Recipes", subdir)
        os.makedirs(output_dir, exist_ok=True)
        # 原始 NBT 与通配候选先在内存归一化，发布目录只接收最终记录。
        from recipe_roles import normalize_recipe_roles
        for recipe in recipes:
            normalize_recipe_roles(recipe, metadata)
            associations.associate(recipe, os.path.join(subdir, "0.json"))
        page_count, total = _write_recipe_pages(recipes, metadata, output_dir)
        _remove_rebuilt_recipe_route(Path(subdir).as_posix())
        print(f"    Done: {total:,} recipes -> {page_count} file(s)")

    associations.write_report()



# ============================================================
# Entry point
# ============================================================

def do_clean(debug: bool):
    """删除旧导出目录"""
    import shutil
    if os.path.exists(VERSION):
        shutil.rmtree(VERSION)
        print(f"Deleted: {VERSION}")
    else:
        print(f"Not found: {VERSION}")


TASKS = {
    "items":      ("Items",        do_items),
    "fluids":     ("Fluids",       do_fluids),
    "oredict":  ("OreDictionary",  do_oredictionary),
    "recipes":    ("Recipes",      do_recipes),
    "gtmaterial": ("GtMaterial",   do_gtmaterial),
    "ticmaterial":("TicMaterial",  do_ticmaterial),
    "tc4research":("Tc4research", do_tc4research),
}

STANDALONE_TASKS = {
    "clean":      ("Clean",        do_clean),
}



def _rename_key_fields(output_dir: str, item_key: str):
    """将每个文件的 key→rawKey, shortKey→key, 删 shortKey"""
    if not os.path.isdir(output_dir):
        return
    arr_key = None
    for root, dirs, files in os.walk(output_dir):
        for fname in files:
            if not fname.endswith(".json") and not fname.endswith(".tabx"):
                continue
            path = os.path.join(root, fname)
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Items: {"items": [...]}, Recipes: {"recipes": [...]}
            if item_key in data:
                arr_key = item_key
            elif "items" in data:
                arr_key = "items"
            elif "recipes" in data:
                arr_key = "recipes"
            else:
                continue
            modified = False
            for obj in data.get(arr_key, []):
                if isinstance(obj, dict):
                    sk = obj.get("shortKey")
                    k = obj.get("key")
                    if sk and k:
                        obj["rawKey"] = k
                        obj["key"] = sk
                        del obj["shortKey"]
                        modified = True
                    elif sk and not k:
                        # 只有 shortKey 没有 key → shortKey → key
                        obj["key"] = sk
                        del obj["shortKey"]
                        modified = True
                    elif k and not sk and "nbt" not in obj:
                        # 无 NBT 且无 shortKey → key 保持不变（不需要 rawKey）
                        pass
            if modified:
                write_version_json(path, data)

    if arr_key:
        print(f"  [Rename] {output_dir}: key→rawKey, shortKey→key")
    else:
        print(f"  [Rename] {output_dir}: no items/recipes found, skipped")


def _rename_all():
    """Items key/shortKey 对调已在 do_items/do_items_tabx 中处理，无需额外操作"""
    pass


def configure_source(require_source: bool = True):
    """配置当前模组的数据源；单独处理研究时允许从完整表按来源提取。"""
    global DUMPS_DIR, REFERENCE_DUMPS_DIR, VERSION_TAG, VERSION, AUX_DIR
    VERSION_TAG = version_tag(required=True)
    mod, version = VERSION_TAG.split("/")
    local_mod = local_mod_name(required=True)
    REFERENCE_DUMPS_DIR = str(source_directory(os.environ.get("DUMPS_DIR", ""), "GTNH"))
    DUMPS_DIR = str(source_directory(REFERENCE_DUMPS_DIR, local_mod, check_exists=False))
    if require_source and not Path(DUMPS_DIR).is_dir():
        raise ValueError(
            f"数据源目录不存在: {DUMPS_DIR}。"
            "仅处理研究时可运行 python split_all.py tc4research，从完整导出按来源提取。"
        )
    VERSION = os.path.join(_PROJECT_ROOT, "wikitext", "Data", "Versions", mod, version)
    # NBT 判断依赖当前模组的配方，不能复用其他模组的扫描缓存。
    AUX_DIR = os.path.join(_PROJECT_ROOT, "databaseDump", "source_state", local_mod, version)
    os.makedirs(AUX_DIR, exist_ok=True)


def main():
    debug = "--debug" in sys.argv
    tabx = "--tabx" in sys.argv
    args = [a.lower() for a in sys.argv[1:] if not a.startswith("--")]

    all_tasks = {**TASKS, **STANDALONE_TASKS}
    if not args:
        tasks_to_run = list(TASKS.keys())
    else:
        tasks_to_run = [a for a in args if a in all_tasks]
        unknown = [a for a in args if a not in all_tasks]
        if unknown:
            print(f"Unknown task(s): {unknown}")
            print(f"Available: {', '.join(all_tasks.keys())}")
            return
    configure_source(require_source=any(key != "tc4research" for key in tasks_to_run))
    if debug:
        print("[DEBUG mode]\n")
    if tabx:
        print("[TABX mode]\n")

    for key in tasks_to_run:
        input_name = {"items": "advitempanel.json", "oredict": "oreDictionary.json",
                      "gtmaterial": "gtmaterial.json", "ticmaterial": "ticmaterial.json"}.get(key)
        if input_name:
            path = os.path.join(DUMPS_DIR, input_name)
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                print(f"跳过缺失或空的输入: {path}")
                continue
        if key == "items" and tabx:
            do_items_tabx(debug)
        else:
            name, func = all_tasks[key]
            func(debug)

    _rename_all()
    if "--recipe-buckets" in sys.argv:
        if "recipes" not in tasks_to_run:
            raise ValueError("--recipe-buckets 需要同时运行 recipes，确保 NBT 关联已经完成")
        from recipe_buckets import build_recipe_buckets
        report = build_recipe_buckets(VERSION)
        Path(AUX_DIR, "recipe-buckets.json").write_text(json.dumps(report, indent=2), "utf-8")
    if {"items", "oredict", "recipes"}.intersection(tasks_to_run):
        count = write_item_name_index(read_items(Path(VERSION) / "Items"), VERSION)
        resolution_path = Path(AUX_DIR) / "image-name-resolution.json"
        if resolution_path.exists():
            resolution = json.loads(resolution_path.read_bytes())
            resolution["indexFileCount"] = count
            resolution_path.write_text(json.dumps(resolution, ensure_ascii=False, indent=2), "utf-8")
        print(f"  [Item names] refreshed {count} pages from final items, ores and recipes")
        from storage_config import write_version_config
        module = version_module_title("Data:Versions/" + VERSION_TAG)
        output = Path(_PROJECT_ROOT) / "wikitext" / (module.replace(":", "/", 1) + ".lua")
        write_version_config(VERSION, output)
        print(f"  [Version config] {module}")
    print("All done!")


if __name__ == "__main__":
    main()
