"""图片流程的数据源、状态目录和当前任务范围。调用方先加载 .env。"""

import csv
import json
import os
from pathlib import Path

from data_version import local_mod_name, source_directory, version_tag


def image_paths(script_dir: str) -> tuple[str, str, str, str]:
    _, version = version_tag(required=True).split("/")
    mod = local_mod_name(required=True)
    source = source_directory(os.environ.get("DUMPS_DIR", ""), mod, check_exists=False)
    script = Path(script_dir)
    # 保留 GTNH 已有的上传记录；独立模组不使用 GTNH 的任务清单。
    db = (script / "image_inventory.db" if mod == "GTNH" else
          script / "source_state" / mod / version / "image_inventory.db")
    resolved = script / "source_state" / mod / version / "image_redirect.resolved.csv"
    return str(source), str(db), str(source / "icons"), str(resolved)


def icon_names(directory: str) -> set[str]:
    path = Path(directory)
    if not path.is_dir():
        return set()
    return {file.name for file in path.iterdir() if file.is_file()}


def redirect_map(path: str) -> dict[str, str]:
    if not os.path.isfile(path):
        raise ValueError("缺少已处理的中文图片名称映射，请先运行 split_all.py items")
    from item_names import canonical_file, file_hash, image_name_config
    report_path = Path(path).with_name("image-name-resolution.json")
    report = json.loads(report_path.read_text("utf-8"))
    source = source_directory(os.environ.get("DUMPS_DIR", ""), local_mod_name(required=True))
    if report.get("sourceItemsSha256") != file_hash(source / "advitempanel.json"):
        raise ValueError("图片名称映射的数据源已变化，请重新运行 split_all.py items")
    if image_name_config().resolve() != Path(report["config"]).resolve():
        raise ValueError("人工图片名称配置路径已切换，请重新运行 split_all.py items")
    if report.get("configSha256") != file_hash(report["config"]):
        raise ValueError("人工图片名称配置已修改，请重新运行 split_all.py items")
    if report.get("sourceIconsSha256") != file_hash(report["sourceIconsPath"]):
        raise ValueError("图片映射数据已修改，请重新运行 split_all.py items")
    if report.get("redirectsSha256") != file_hash(path):
        raise ValueError("已处理重定向表被修改，请修改人工配置后重新生成")
    result = {}
    with open(path, encoding="utf-8-sig", newline="") as file:
        for row in csv.reader(file):
            if len(row) != 2 or not row[0].startswith("File:") or not row[1].startswith("File:"):
                raise ValueError("无效的已处理图片重定向行")
            title = "File:" + canonical_file(row[0].removeprefix("File:"))
            icon = row[1].removeprefix("File:").strip()
            if title in result and result[title] != icon:
                raise ValueError(f"同一中文图片标题有多个目标: {title}")
            result[title] = icon
    return result


def require_source(path: str) -> None:
    if not os.path.isdir(path):
        raise ValueError(f"数据源目录不存在: {path}")
