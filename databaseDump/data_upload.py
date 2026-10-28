"""
data_upload.py
多线程批量上传 — 从 wiki_inventory.db 读取 pending_upload 任务，
上传成功后标记为 done，同步更新文件指纹。
当前版本的数据页全部完成后，再上传对应的版本配置模块。

用法:
    python data_upload.py                   # 全部上传（8 线程）
    python data_upload.py --debug           # 前 20 个文件
    python data_upload.py -t 16             # 16 线程
"""

import os
import sys
import io
import json
import copy
import signal
import sqlite3
from contextlib import closing
import threading
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
WIKITEXT_DIR = os.path.join(_PROJECT_ROOT, "wikitext")
TASK_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_inventory.db")
USERNAME = os.environ.get("BOT_USERNAME", "")
PASSWORD = os.environ.get("BOT_PASSWORD", "")
WIKI_PREFIX = "Data:Versions/" + version_tag() + "/"

_stop = threading.Event()


# ── Task DB helpers ────────────────────────────────────────────

def get_pending_uploads() -> list[tuple[str, str]]:
    """读取 wiki_inventory.db 中所有 pending_upload 任务 → [(path, title), ...]"""
    with closing(sqlite3.connect(TASK_DB)) as conn:
        rows = conn.execute(
            "SELECT path, title FROM tasks WHERE status='pending_upload' AND path IS NOT NULL ORDER BY path"
        ).fetchall()
    # 已停用 Index/*.tabx，避免本地旧文件或旧任务重新创建已删除的页面。
    config_title = version_module_title(WIKI_PREFIX).replace(" ", "_")
    return [
        (path, title) for path, title in rows
        if title.replace(" ", "_") == config_title
        or (in_scope(title, WIKI_PREFIX)
            and not (in_scope(title, WIKI_PREFIX + "Index/") and title.endswith(".tabx")))
    ]


def mark_upload_done(path: str, title: str, mtime: float, size: int):
    """上传成功 → 标记 done"""
    with closing(sqlite3.connect(TASK_DB)) as conn:
        conn.execute(
            "UPDATE tasks SET status='done', mtime=?, size=? WHERE title=?",
            (mtime, size, title)
        )
        conn.commit()


def get_pending_deletes() -> list[str]:
    """读取 pending_delete 任务 → [title, ...]"""
    with closing(sqlite3.connect(TASK_DB)) as conn:
        rows = conn.execute(
            "SELECT title FROM tasks WHERE status='pending_delete' ORDER BY title"
        ).fetchall()
    return [r[0] for r in rows if in_scope(r[0], WIKI_PREFIX)]


def mark_delete_done(title: str):
    """删除成功 → 移除记录"""
    with closing(sqlite3.connect(TASK_DB)) as conn:
        conn.execute("DELETE FROM tasks WHERE title=?", (title,))
        conn.commit()


# ── HTTP ─────────────────────────────────────────────────────

def _make_opener(jar):
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def _get(opener, params: dict) -> dict:
    url = BASE + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-authkey": AUTHKEY, "User-Agent": "WikiBot/1.0"})
    with opener.open(req) as resp:
        return json.loads(resp.read())


def _post_form(opener, params: dict) -> dict:
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(BASE, data=data,
        headers={"X-authkey": AUTHKEY, "User-Agent": "WikiBot/1.0",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with opener.open(req) as resp:
        return json.loads(resp.read())


def login_once(username: str, password: str) -> http.cookiejar.CookieJar:
    jar = http.cookiejar.CookieJar()
    opener = _make_opener(jar)
    r = _get(opener, {"action": "query", "meta": "tokens", "type": "login", "format": "json"})
    lt = r["query"]["tokens"]["logintoken"]
    r = _post_form(opener, {
        "action": "login", "format": "json",
        "lgname": username, "lgpassword": password, "lgtoken": lt,
    })
    if r["login"]["result"] != "Success":
        raise RuntimeError(f"Login failed: {r['login'].get('reason', r['login']['result'])}")
    print(f"Logged in as: {r['login']['lgusername']}")
    return jar


def clone_jar(src: http.cookiejar.CookieJar) -> http.cookiejar.CookieJar:
    dst = http.cookiejar.CookieJar()
    dst._cookies = copy.deepcopy(src._cookies)
    return dst


# ── Upload ───────────────────────────────────────────────────

def edit_page(opener, title: str, text: str, summary: str = "", bot: bool = True) -> dict:
    """编辑或创建页面。返回 API 响应的 edit 部分。"""
    r = _get(opener, {"action": "query", "meta": "tokens", "format": "json"})
    csrf = r["query"]["tokens"]["csrftoken"]

    params = {
        "action": "edit",
        "format": "json",
        "title": title,
        "text": text,
        "summary": summary,
        "token": csrf,
    }
    if bot:
        params["bot"] = "1"

    for attempt in range(2):
        r = _post_form(opener, params)
        if "error" in r:
            err = r["error"]["info"]
            if ("Invalid CSRF" in err or "badtoken" in err.lower()) and attempt == 0:
                r2 = _get(opener, {"action": "query", "meta": "tokens", "format": "json"})
                params["token"] = r2["query"]["tokens"]["csrftoken"]
                continue
            raise RuntimeError(err)
        return r["edit"]


# ── Delete ───────────────────────────────────────────────────

def delete_page(opener, title: str, reason: str = "", bot: bool = True) -> dict:
    """删除页面。"""
    r = _get(opener, {"action": "query", "meta": "tokens", "format": "json"})
    csrf = r["query"]["tokens"]["csrftoken"]
    params = {
        "action": "delete",
        "format": "json",
        "title": title,
        "token": csrf,
    }
    if reason:
        params["reason"] = reason
    if bot:
        params["bot"] = "1"

    for attempt in range(2):
        r = _post_form(opener, params)
        if "error" in r:
            err = r["error"]["info"]
            if ("Invalid CSRF" in err or "badtoken" in err.lower()) and attempt == 0:
                r2 = _get(opener, {"action": "query", "meta": "tokens", "format": "json"})
                params["token"] = r2["query"]["tokens"]["csrftoken"]
                continue
            raise RuntimeError(err)
        return r


def delete_worker(titles: list[str], total: int, counters: dict, lock: threading.Lock,
                  master_jar):
    """线程：删除自己分到的页面。"""
    opener = _make_opener(clone_jar(master_jar))
    for title in titles:
        if _stop.is_set():
            return
        try:
            delete_page(opener, title, reason="机器人：批量清理旧数据", bot=True)
            mark_delete_done(title)
            status, msg = "OK", ""
        except Exception as e:
            err = str(e)
            status, msg = "FAIL", err

        with lock:
            counters[status] = counters.get(status, 0) + 1
            counters["done"] += 1
            extra = f" - {msg}" if msg else ""
            print(f"[{counters['done']}/{total}] {status} DELETE {title}{extra}")


# ── Worker (Upload) ──────────────────────────────────────────

def worker(task_list: list, total: int, counters: dict, lock: threading.Lock,
           master_jar):
    """线程任务：上传自己分到的所有 pending_upload 任务。"""
    opener = _make_opener(clone_jar(master_jar))
    wikitext_abs = os.path.abspath(WIKITEXT_DIR)

    for (rel_path, title) in task_list:
        if _stop.is_set():
            return
        abs_path = os.path.join(wikitext_abs, rel_path)

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                text = f.read()
            result = edit_page(opener, title, text,
                               summary="机器人：批量上传数据", bot=True)
            if "nochange" in result:
                status, msg = "SKIP", ""
            else:
                status, msg = "OK", ""

            # 记录文件指纹
            stat = os.stat(abs_path)
            mark_upload_done(rel_path, title, stat.st_mtime, stat.st_size)
        except Exception as e:
            err = str(e)
            status, msg = "FAIL", err

        with lock:
            counters[status] = counters.get(status, 0) + 1
            counters["done"] += 1
            extra = f" - {msg}" if msg else ""
            print(f"[{counters['done']}/{total}] {status} {title}{extra}")


# ── Main ─────────────────────────────────────────────────────

def main(argv=None):
    require_scope(WIKI_PREFIX)
    args = sys.argv[1:] if argv is None else argv
    debug = "--debug" in args
    threads = 8
    failures = 0
    _stop.clear()

    for i, a in enumerate(args):
        if a == "-t" and i + 1 < len(args):
            threads = int(args[i + 1])
    if threads < 1:
        raise ValueError("线程数必须大于 0")

    # 单次登录
    master_jar = login_once(USERNAME, PASSWORD)

    def on_interrupt(_sig, _frame):
        print("\n\nInterrupted! Stopping...")
        _stop.set()
    signal.signal(signal.SIGINT, on_interrupt)

    # ── Upload ──
    pending = get_pending_uploads()
    upload_tasks = [(path, title) for path, title in pending if in_scope(title, WIKI_PREFIX)]
    config_tasks = [(path, title) for path, title in pending if not in_scope(title, WIKI_PREFIX)]
    if upload_tasks:
        if debug:
            upload_tasks = upload_tasks[:20]
            threads = 2
            print(f"[DEBUG] Only {len(upload_tasks)} uploads")

        total = len(upload_tasks)
        print(f"{total:,} pending uploads, {threads} threads")

        chunks = [[] for _ in range(threads)]
        for i, t in enumerate(upload_tasks):
            chunks[i % threads].append(t)

        counters = {"OK": 0, "SKIP": 0, "FAIL": 0, "done": 0}
        lock = threading.Lock()
        workers = []
        for tid in range(threads):
            if not chunks[tid]:
                continue
            t = threading.Thread(
                target=worker,
                args=(chunks[tid], total, counters, lock, master_jar),
                daemon=True,
            )
            t.start()
            workers.append(t)
            print(f"  Thread {tid}: {len(chunks[tid])} uploads")

        print(f"\nUploading {total} pages... (Ctrl+C to stop)\n")

        for t in workers:
            while t.is_alive():
                t.join(timeout=0.5)

        remaining = sum(in_scope(title, WIKI_PREFIX) for _, title in get_pending_uploads())
        print(f"Upload done! OK: {counters['OK']}, SKIP: {counters['SKIP']}, FAIL: {counters['FAIL']}")
        failures += counters['FAIL']
        if remaining:
            print(f"{remaining} uploads still pending (re-run to retry failures)")
    else:
        print("No pending data uploads.")

    # 配置中的桶数对应完整的数据集；调试上传、失败和中断均留待下次发布。
    if config_tasks and not _stop.is_set():
        data_pending = any(in_scope(title, WIKI_PREFIX) for _, title in get_pending_uploads())
        if debug or failures or data_pending:
            print("Version config pending: run a complete successful data upload to publish it.")
        else:
            print("Uploading version config...")
            counters = {"OK": 0, "SKIP": 0, "FAIL": 0, "done": 0}
            worker(config_tasks, len(config_tasks), counters, threading.Lock(), master_jar)
            failures += counters["FAIL"]

    # ── Delete ──
    # delete_titles = get_pending_deletes()
    # if delete_titles:
    #     if debug:
    #         delete_titles = delete_titles[:5]
    #
    #     total = len(delete_titles)
    #     print(f"\n{total:,} pending deletes, {min(threads, total)} threads")
    #
    #     chunks = [[] for _ in range(min(threads, total))]
    #     for i, t in enumerate(delete_titles):
    #         chunks[i % len(chunks)].append(t)
    #
    #     counters = {"OK": 0, "FAIL": 0, "done": 0}
    #     lock = threading.Lock()
    #     workers = []
    #     for tid in range(len(chunks)):
    #         if not chunks[tid]:
    #             continue
    #         t = threading.Thread(
    #             target=delete_worker,
    #             args=(chunks[tid], total, counters, lock, master_jar),
    #             daemon=True,
    #         )
    #         t.start()
    #         workers.append(t)
    #
    #     print(f"Deleting {total} pages... (Ctrl+C to stop)\n")
    #
    #     for t in workers:
    #         while t.is_alive():
    #             t.join(timeout=0.5)
    #
    #     remaining = len(get_pending_deletes())
    #     print(f"Delete done! OK: {counters['OK']}, FAIL: {counters['FAIL']}")
    #     failures += counters['FAIL']
    #     if remaining:
    #         print(f"{remaining} deletes still pending (re-run to retry failures)")
    # else:
    #     print("No pending deletes.")
    return 130 if _stop.is_set() else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())
