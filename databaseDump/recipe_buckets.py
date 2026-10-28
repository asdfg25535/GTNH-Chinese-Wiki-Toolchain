"""按输出 rawKey 将已适配配方池原位改为分桶分页。"""

import json
from collections import defaultdict
from pathlib import Path

from bounded_json import DEFAULT_MAX_BYTES, HARD_MAX_BYTES, encode_json, validate_limit, write_array_pages, write_bounded
from routing_hash import HASH_ALGORITHM, bucket_id, choose_bucket_count

ROUTE_FIELDS = ("outputItems", "outputFluids")


def output_route_keys(value):
    """同一注册名、damage 的 NBT 产物共用路由，完整 key 留在配方中供精确匹配。"""
    if isinstance(value, str) and value:
        yield value
    elif isinstance(value, list):
        for entry in value:
            yield from output_route_keys(entry)
    elif isinstance(value, dict):
        yield from output_route_keys(value.get("rawKey") or value.get("key"))


def recipe_route(recipe, bucket_count):
    keys = {key for field in ROUTE_FIELDS for key in output_route_keys(recipe.get(field))}
    return bucket_id(next(iter(keys)), bucket_count) if len(keys) == 1 else None


def output_slots(recipe):
    """候选列表算一个输出槽；物品数量不影响输出槽数量。"""
    return sum(len(recipe.get(field) or []) for field in ROUTE_FIELDS)


# 只处理本次确认的八个大配方池；其他来源保留原目录。
SUPPORTED_HANDLERS = {
    "minecraft/ShapedRecipeHandler_crafting": "有序合成",
    "minecraft/ShapelessRecipeHandler_crafting": "无序合成",
    "gregtech/GTNEIDefaultHandler_tic_bolt_molding": "匠魂弩箭固化",
    "gregtech/GTNEIDefaultHandler_tic_part_extruding": "匠魂部件压模",
    "gregtech/GTNEIDefaultHandler_assembler": "组装机",
    "gregtech/GTNEIDefaultHandler_alloy_smelter_recycling": "合金炉回收",
    "gregtech/GTNEIDefaultHandler_fluidsolidifier": "流体固化器",
    "gregtech/GTNEIDefaultHandler_hammer": "锻造锤",
}
HAMMER = "gregtech/GTNEIDefaultHandler_hammer"


def write_bucket(recipes, metadata, target, max_bytes):
    return write_array_pages(recipes, metadata, "recipes", target, max_bytes)


def build_recipe_buckets(version_dir, max_bytes=DEFAULT_MAX_BYTES):
    validate_limit(max_bytes)
    version_dir = Path(version_dir)
    source = version_dir / "Recipes"
    output = source
    # 输入为 split_all 生成的顺序分页；重跑从原始来源重新生成这些页面。
    for handler in SUPPORTED_HANDLERS:
        directory = source / handler
        for child in directory.iterdir() if directory.is_dir() else ():
            if child.is_dir() and (child.name == "shared" or
                                   child.name.isdecimal()):
                if any(path.stem.isdecimal() for path in child.glob("*.json")):
                    raise ValueError(f"配方池已分桶，请从原始来源重新生成后再分桶: {handler}")
    written, handlers, skipped = set(), {}, {}
    replaced_files = []
    total = written_recipes = shared = 0
    for handler, name in SUPPORTED_HANDLERS.items():
        files = sorted((path for path in (source / handler).glob("[0-9]*.json")
                        if path.stem.isdecimal()), key=lambda path: int(path.stem))
        if len(files) <= 1:
            skipped[handler] = "当前来源少于两个文件，保留原文件"
            continue
        entries, metadata, invalid = [], None, False
        for path in files:
            data = json.loads(path.read_text("utf-8"))
            if metadata is None:
                metadata = {key: value for key, value in data.items() if key != "recipes"}
            for recipe in data.get("recipes", []):
                slots = output_slots(recipe)
                # 不扩展到辅助字段或其他多输出结构；形态变化时整池退回原文件。
                if recipe.get("otherItems") or not slots:
                    invalid = True
                if slots == 1 and recipe_route(recipe, 1) is None:
                    invalid = True
                if slots != 1 and handler != HAMMER:
                    invalid = True
                entries.append(recipe)
        if invalid:
            skipped[handler] = "输出形态或非唯一 rawKey 超出本次范围，保留原文件"
            continue
        # shared 多产物配方独立分页；数字桶按可定位产物的记录量选择。
        bucket_count = choose_bucket_count(sum(len(encode_json(recipe)) + 1 for recipe in entries
                                              if output_slots(recipe) == 1), max_bytes)
        grouped = defaultdict(list)
        for recipe in entries:
            route = "shared" if output_slots(recipe) > 1 else recipe_route(recipe, bucket_count)
            shared += route == "shared"
            total += 1
            grouped[route].append(recipe)
            written_recipes += 1
        metadata["routing"] = {
            "hashAlgorithm": HASH_ALGORITHM, "bucketCount": bucket_count,
            # 身份字段按顺序取首个非空值：rawKey 优先，基础物品可直接使用 key。
            "fields": list(ROUTE_FIELDS), "identityFields": ["rawKey", "key"],
            "sharedDirectory": "shared" if handler == HAMMER else None,
        }
        routes = {}
        for route, recipes in sorted(grouped.items()):
            paths = write_bucket(recipes, metadata, output / handler / route, max_bytes)
            written.update(paths)
            routes[route] = [path.relative_to(output).as_posix() for path in paths]
        replaced_files.extend(files)
        handlers[handler] = {
            "name": name, "bucketCount": bucket_count, "sourceFiles": [path.relative_to(source).as_posix() for path in files],
            "sourceRecipes": len(entries), "routes": routes,
        }
    report = {
        "sourceRecipes": total, "writtenRecipes": written_recipes, "sharedRecipes": shared,
        "files": len(written), "hashAlgorithm": HASH_ALGORITHM,
        "recipeBuckets": {handler: details["bucketCount"] for handler, details in handlers.items()},
        "handlers": handlers, "skipped": skipped,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_bounded(output / "routing-manifest.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"), HARD_MAX_BYTES)
    for path in replaced_files:
        path.unlink()
    print(f"  [Recipe buckets] {len(handlers)} handlers; {total:,} recipes; {shared} shared; {len(written)} files")
    return report
