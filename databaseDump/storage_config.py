"""从本次导出页面生成版本模块的 storage 配置。"""

import json
from pathlib import Path


def read_storage(version_dir):
    version_dir = Path(version_dir)
    # 空类别使用 1 桶，查询对应的空 00 目录。
    storage = {"itemBuckets": 1, "itemNameBuckets": 1}
    for directory, field in (("Items", "itemBuckets"), ("ItemNames", "itemNameBuckets")):
        pages = sorted(path for path in (version_dir / directory).glob("[0-9]*/[0-9]*.*")
                       if path.parent.name.isdecimal() and path.stem.isdecimal()
                       and path.suffix in (".json", ".tabx"))
        if pages:
            storage[field] = json.loads(pages[0].read_bytes())["bucketCount"]
    manifest = version_dir / "Recipes/routing-manifest.json"
    storage["recipeBuckets"] = json.loads(manifest.read_bytes())["recipeBuckets"] if manifest.exists() else {}
    return storage


def write_version_config(version_dir, output):
    """生成可发布到 模块:Database/Versions/<模组>/<版本> 的 Lua 内容。"""
    storage = read_storage(version_dir)
    lines = ["-- 本次导出的分桶配置，与对应版本的数据页一同更新。", "return {", "    storage = {"]
    for field in ("itemBuckets", "itemNameBuckets"):
        lines.append(f"        {field} = {storage[field]},")
    lines.append("        recipeBuckets = {")
    for handler, count in sorted(storage["recipeBuckets"].items()):
        quoted = json.dumps(handler, ensure_ascii=False)
        lines.append(f"            [{quoted}] = {count},")
    lines.extend(["        },", "    },", "}", ""])
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), "utf-8")
    return storage
