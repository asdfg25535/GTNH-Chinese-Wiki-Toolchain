"""从最终配方和矿辞收集每个物品名称的来源、用途处理器。"""

import json
from collections import defaultdict
from pathlib import Path


SLOT_FIELDS = {
    "sourceHandlers": (
        "outputItems", "outputFluids", "otherItems", "normalDrops", "rareDrops",
        "additionalDrops", "infernalDrops", "fishItems", "junkItems", "treasureItems",
        "eliteItems", "ultraItems", "infernoItems",
    ),
    "usageHandlers": ("inputItems", "inputFluids", "otherItems", "keyItem"),
}


# 输入名称分桶收益较高的配方池；桶号取最终文件目录。
# 有序合成、匠魂弩箭固化、组装机、合金炉回收和锻造锤。
INPUT_BUCKET_HANDLERS = {
    "minecraft/ShapedRecipeHandler_crafting",
    "gregtech/GTNEIDefaultHandler_tic_bolt_molding",
    "gregtech/GTNEIDefaultHandler_assembler",
    "gregtech/GTNEIDefaultHandler_alloy_smelter_recycling",
    "gregtech/GTNEIDefaultHandler_hammer",
}


# 普通分页中输入分布较集中的配方池；文件号取最终数字页名。
INPUT_FILE_HANDLERS = {
    "appliedenergistics2/NEIFacadeRecipeHandler_AEFacadeCrafting",  # 伪装板合成
    "gendustry/SamplerHandler_null",  # 基因采样机
    "gregtech/GTNEIDefaultHandler_arc_furnace_recycling",  # 电弧炉回收
    "gregtech/GTNEIDefaultHandler_assembler",  # 组装机
    "gregtech/GTNEIDefaultHandler_fluid_extractor_recycling",  # 流体提取机回收
    "gregtech/GTNEIDefaultHandler_macerator",  # 研磨机
    "gregtech/GTNEIDefaultHandler_macerator_recycling",  # 研磨机回收
    "sciencenotleisure/GTNEIDefaultHandler_shimmer",  # 微光嬗变
    "Thaumcraft/ItemsContainingAspectHandler_items_containing_aspect",  # 包含要素的物品
}


def entries(value):
    """槽位可以是字符串、物品对象或嵌套的可选项数组。"""
    if isinstance(value, (str, dict)):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from entries(child)


def collect_handlers(names, raw_keys, query_keys, version_dir):
    """收集来源和用途；输入定位复用 [handler, [桶号或文件号...]]。"""
    root = Path(version_dir)
    tokens, raw_names, wildcard_names = (defaultdict(set) for _ in range(3))
    for name, keys in names.items():
        for key in keys:
            raw = raw_keys.get(key, key)
            tokens[key].add(name)
            if query_keys.get(key):
                tokens[query_keys[key]].add(name)
            raw_names[raw].add(name)
            base, separator, damage = raw.rpartition(":")
            wildcard_names[base if separator and damage.isdigit() else raw].add(name)

    for file in sorted((root / "OreDictionary").rglob("*.json")):
        for ore, values in json.loads(file.read_bytes()).items():
            for entry in entries(values):
                key = entry if isinstance(entry, str) else entry.get("key", "")
                tokens["#" + ore].update(tokens.get(key, ()))
                if key.endswith(":32767"):
                    tokens["#" + ore].update(wildcard_names.get(key[:-6], ()))

    indexes = {field: defaultdict(set) for field in SLOT_FIELDS}
    input_routes = defaultdict(lambda: defaultdict(set))
    file_numbers = defaultdict(set)
    for file in sorted((root / "Recipes").rglob("*.json")):
        if file.name == "routing-manifest.json":
            continue
        data = json.loads(file.read_bytes())
        parts = file.relative_to(root / "Recipes").parts
        handler = "/".join(parts[:2]).removesuffix(".json")
        bucketed = handler in INPUT_BUCKET_HANDLERS and data.get("routing")
        paged = handler in INPUT_FILE_HANDLERS and len(parts) == 3 and file.stem.isdecimal()
        number = None
        if bucketed and parts[2].isdecimal():
            number = int(parts[2])
        elif paged:
            number = int(file.stem)
            file_numbers[handler].add(number)
        for recipe in data.get("recipes", []):
            for field, slots in SLOT_FIELDS.items():
                matched = set()
                for slot in slots:
                    for entry in entries(recipe.get(slot)):
                        values = [entry] if isinstance(entry, str) else [entry.get("key"), entry.get("queryKey")]
                        for token in values:
                            matched.update(tokens.get(token, ()))
                            if field == "sourceHandlers":
                                matched.update(raw_names.get(token, ()))
                        # 单产物名称查询可合并为 rawKey；跨版本合并候选后也可能达到该阈值。
                        if field == "sourceHandlers" and isinstance(entry, dict):
                            matched.update(raw_names.get(entry.get("rawKey"), ()))
                for name in matched:
                    indexes[field][name].add(handler)
                    if field == "usageHandlers" and (bucketed or paged):
                        routes = input_routes[name][handler]
                        if number is not None:
                            routes.add(number)
    result = {field: {name: sorted(paths) for name, paths in index.items()}
              for field, index in indexes.items()}
    for name, handlers in input_routes.items():
        result["usageHandlers"][name] = [
            [path, sorted(handlers[path])]
            if path in handlers and (path not in file_numbers or handlers[path] != file_numbers[path])
            else path
            for path in result["usageHandlers"][name]
        ]
    return result
