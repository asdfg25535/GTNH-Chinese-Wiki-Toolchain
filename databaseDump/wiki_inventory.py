"""
wiki_inventory.py
扫描本地 wikitext/ 文件 + 查询 wiki 当前状态 → 生成任务清单。

三种状态:
    pending_upload   本地有文件 → wiki 上需要存在（新增或更新）
    pending_delete   wiki 上有页面，但本地已没有对应文件 → 需要删除
    done             本地和 wiki 已同步

用法:
    python wiki_inventory.py               # 增量更新
    python wiki_inventory.py --refresh     # 全量重建
"""

import os
import sys
import json
import sqlite3
from contextlib import closing
import urllib.request
import urllib.parse
import http.cookiejar

from data_version import in_scope, require_scope, version_module_title, version_tag

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def _load_dotenv():
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

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = os.environ.get("BASE_URL", "https://gtnh.huijiwiki.com/api.php")
AUTHKEY = os.environ.get("AUTHKEY", "")
USERNAME = os.environ.get("EDITOR_USERNAME", "")
PASSWORD = os.environ.get("EDITOR_PASSWORD", "")
WIKITEXT_DIR = os.path.join(_PROJECT_ROOT, "wikitext")
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_inventory.db")
WIKI_PREFIX = "Data:Versions/" + version_tag() + "/"

# ── HTTP ─────────────────────────────────────────────────────

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _get(params: dict) -> dict:
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-authkey": AUTHKEY, "User-Agent": "WikiBot/1.0"})
    with opener.open(req) as resp:
        return json.loads(resp.read())


def _post(params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(BASE, data=data,
        headers={"X-authkey": AUTHKEY, "User-Agent": "WikiBot/1.0",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req) as resp:
        return json.loads(resp.read())


def login():
    r = _get({"action": "query", "meta": "tokens", "type": "login", "format": "json"})
    lt = r["query"]["tokens"]["logintoken"]
    r = _post({
        "action": "login", "format": "json",
        "lgname": USERNAME, "lgpassword": PASSWORD, "lgtoken": lt,
    })
    if r["login"]["result"] != "Success":
        raise RuntimeError(f"Login failed: {r['login'].get('reason', r['login']['result'])}")
    print(f"Logged in as: {r['login']['lgusername']}")


# ── Path / Title conversion ──────────────────────────────────

def path_to_title(rel_path: str) -> str:
    """本地相对路径 → wiki 标题。
    Data/Versions/GTNH/.../Items/0.json → Data:Versions/GTNH/.../Items/0.json
    Module/Database/Versions/GTNH/1.lua → Module:Database/Versions/GTNH/1
    """
    parts = rel_path.replace("\\", "/").split("/", 1)
    if len(parts) == 2:
        if parts[0] == "Module":
            parts[1] = parts[1].removesuffix(".lua")
        return parts[0] + ":" + parts[1]
    return "0:" + parts[0]


def title_to_path(title: str) -> str:
    """wiki 标题 → 本地相对路径。"""
    ns, sep, rest = title.partition(":")
    if sep:
        if ns == "Module":
            rest += ".lua"
        return ns + "/" + rest
    return "0/" + rest


# ── Database ─────────────────────────────────────────────────

def init_db(refresh: bool):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        if refresh:
            conn.execute("DROP TABLE IF EXISTS tasks")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                title  TEXT PRIMARY KEY,
                path   TEXT,
                status TEXT NOT NULL DEFAULT 'pending_upload',
                mtime  REAL,
                size   INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON tasks(status)")
        conn.commit()


def get_pending_uploads() -> list[tuple[str, str]]:
    """获取待上传任务列表，返回 [(path, title), ...]"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT path, title FROM tasks WHERE status='pending_upload' AND path IS NOT NULL ORDER BY path"
        ).fetchall()
    return rows


def get_pending_deletes() -> list[str]:
    """获取待删除的 wiki 标题列表。"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE status='pending_delete' ORDER BY title"
        ).fetchall()
    return [r[0] for r in rows]


def mark_upload_done(path: str, title: str, mtime: float, size: int):
    """上传成功 → done"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute(
            "UPDATE tasks SET status='done', mtime=?, size=? WHERE title=?",
            (mtime, size, title)
        )
        conn.commit()


def mark_delete_done(title: str):
    """删除成功 → 移除记录"""
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM tasks WHERE title=?", (title,))
        conn.commit()


def get_stats() -> dict:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# ── Scan & Cross-reference ───────────────────────────────────

def scan_local_files() -> dict[str, tuple[float, int]]:
    """扫描当前版本的数据页与配置模块，返回 {rel_path: (mtime, size)}。"""
    files = {}
    scan_root = os.path.join(WIKITEXT_DIR, title_to_path(WIKI_PREFIX).replace("/", os.sep))
    if not os.path.isdir(scan_root):
        print(f"  Directory not found: {scan_root}")
        return files
    wikitext_abs = os.path.abspath(WIKITEXT_DIR)
    for root, dirs, names in os.walk(scan_root):
        for name in names:
            abs_path = os.path.join(root, name)
            rel = os.path.relpath(abs_path, wikitext_abs).replace("\\", "/")
            stat = os.stat(abs_path)
            files[rel] = (stat.st_mtime, stat.st_size)

    config_path = title_to_path(version_module_title(WIKI_PREFIX))
    config_abs = os.path.join(wikitext_abs, config_path)
    if os.path.isfile(config_abs):
        stat = os.stat(config_abs)
        files[config_path] = (stat.st_mtime, stat.st_size)
    return files


def fetch_wiki_titles() -> set[str]:
    """从 wiki 抓取所有匹配前缀的页面标题（空格归一化为下划线）。"""
    titles = set()
    offset = 0
    params = {
        "action": "query",
        "list": "prefixsearch",
        "pssearch": WIKI_PREFIX,
        "pslimit": 500,
        "format": "json",
    }
    while True:
        r = _get(params)
        batch = r["query"]["prefixsearch"]
        for p in batch:
            # MediaWiki 将 _ 和空格视为等价，统一为下划线
            if in_scope(p["title"], WIKI_PREFIX):
                titles.add(p["title"].replace(" ", "_"))
        print(f"  Fetched {len(batch)}, total: {len(titles)}")
        if len(batch) < 500:
            break
        offset += len(batch)
        params["psoffset"] = offset
    return titles


def rebuild_tasks(refresh: bool):
    """扫描本地文件 + wiki 状态 → 重建任务清单。"""
    print("Scanning local files...")
    local_files = scan_local_files()
    print(f"  Found {len(local_files):,} files")

    # 加载旧任务中的 done 记录（用于变更检测）
    old_done = {}
    if not refresh:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            for row in conn.execute(
                "SELECT title, path, mtime, size FROM tasks WHERE status='done' AND path IS NOT NULL"
            ):
                old_done[row[0]] = (row[1], row[2], row[3])

    login()
    print(f"Fetching wiki pages under '{WIKI_PREFIX}'...")
    wiki_titles = fetch_wiki_titles()
    print(f"  Found {len(wiki_titles):,} wiki pages")

    # 构建 path → title 映射（本地文件可能对应同一个 title）
    local_titles = {}  # title → path
    for rel_path in local_files:
        title = path_to_title(rel_path)
        local_titles[title] = rel_path

    tasks = []
    upload_count = 0
    delete_count = 0
    done_count = 0

    # ① 本地文件的对应 wiki 标题
    for rel_path, (mtime, size) in sorted(local_files.items()):
        title = path_to_title(rel_path)

        if title in old_done:
            old_path, old_mtime, old_size = old_done[title]
            if old_mtime == mtime and old_size == size and old_path == rel_path:
                tasks.append((title, rel_path, "done", mtime, size))
                done_count += 1
                continue

        # 需要上传（无论 wiki 上是否存在，直接覆盖）
        tasks.append((title, rel_path, "pending_upload", mtime, size))
        upload_count += 1

    # ② wiki 上有但本地没有的 → 待删除
    for title in sorted(wiki_titles):
        if title not in local_titles:
            tasks.append((title, None, "pending_delete", None, None))
            delete_count += 1

    # 写入
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.execute("DELETE FROM tasks")
        conn.executemany(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", tasks
        )
        conn.commit()

    print(f"\nTask list rebuilt:")
    print(f"  pending_upload:  {upload_count}")
    print(f"  pending_delete:  {delete_count}")
    print(f"  done:            {done_count}")


# ── Main ─────────────────────────────────────────────────────

def main(argv=None):
    require_scope(WIKI_PREFIX)
    args = sys.argv[1:] if argv is None else argv
    refresh = "--refresh" in args
    stats_only = "--stats" in args

    if stats_only:
        stats = get_stats()
        if stats:
            print(f"Tasks: {stats}")
        else:
            print("DB is empty. Run without --stats to build.")
        return

    init_db(refresh)
    rebuild_tasks(refresh)

    stats = get_stats()
    print(f"\nFinal: {stats}")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
