"""数据页的模组名与版本配置（调用方先加载 .env）。"""

import json
import os
from pathlib import Path


def addon_mods() -> set[str]:
    """额外附属的注册 Mod ID；用于从基础整合包数据中划分附属范围。"""
    path = Path(__file__).parent / "config" / "addon-mods.json"
    return {mod.casefold() for mod in json.loads(path.read_text(encoding="utf-8"))}


def title_part(value: str) -> str:
    value = value.strip()
    if (not value or value in {".", ".."} or value.endswith(".")
            or any(char in value for char in '/\\:#<>[]{}|%?*"')
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"不是有效的 mod 名或版本号: {value!r}")
    return value


def _mod_name(key: str, required: bool) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        if required:
            raise ValueError(f"请在 .env 设置 {key}")
        return ""
    return title_part(value)


def local_mod_name(required: bool = False) -> str:
    """本地数据源名。"""
    return _mod_name("MOD_LOCAL_NAME", required)


def remote_mod_name(required: bool = False) -> str:
    """Wiki 目标目录名。"""
    return _mod_name("MOD_REMOTE_NAME", required)


def version_tag(required: bool = False) -> str:
    mod = remote_mod_name(required=required)
    version = os.environ.get("MOD_VERSION", "").strip()
    if not mod or not version:
        if required:
            raise ValueError("请在 .env 设置 MOD_REMOTE_NAME和 MOD_VERSION")
        return ""
    return f"{title_part(mod)}/{title_part(version)}"


def in_scope(title: str, prefix: str) -> bool:
    """空格和下划线等价；斜线确保只匹配当前版本的子页面。"""
    return title.replace(" ", "_").startswith(prefix.rstrip("/").replace(" ", "_") + "/")


def require_scope(prefix: str) -> None:
    parts = prefix.rstrip("/").split("/")
    if len(parts) != 3 or parts[0] != "Data:Versions" or not all(parts[1:]):
        raise ValueError("请在 .env 设置 MOD_REMOTE_NAME和 MOD_VERSION")
    title_part(parts[1])
    title_part(parts[2])


def version_module_title(data_prefix: str) -> str:
    """数据版本对应的通用配置模块；沿用远端模组名与版本号。"""
    require_scope(data_prefix)
    version = data_prefix.rstrip("/").removeprefix("Data:Versions/")
    return "Module:Database/Versions/" + version


def source_directory(dumps_dir: str, mod: str, check_exists: bool = True) -> Path:
    """GTNH 使用 dumps 顶层，其他模组只使用 filtered 中自己的目录。"""
    if not dumps_dir:
        raise ValueError("请在 .env 设置 DUMPS_DIR")
    mod = title_part(mod)
    dumps = Path(dumps_dir).expanduser().resolve()
    source = dumps if mod == "GTNH" else (dumps / "filtered" / mod).resolve()
    if mod != "GTNH" and not source.is_relative_to((dumps / "filtered").resolve()):
        raise ValueError(f"mod 目录不在 filtered 内: {source}")
    if check_exists and not source.is_dir():
        raise ValueError(f"数据源目录不存在: {source}")
    return source
