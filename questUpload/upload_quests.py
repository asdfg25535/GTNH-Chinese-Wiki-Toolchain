"""上传当前版本的 QuestLines / Quests JSON 页，记录状态并支持失败续传。"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import copy
from dataclasses import dataclass
import hashlib
import http.cookiejar
import json
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import urllib.parse
import urllib.request

from split_quests import PROJECT_ROOT, load_config
from databaseDump.bounded_json import HARD_MAX_BYTES
from databaseDump.data_version import title_part

STATE_DB = Path(__file__).resolve().parent / "upload_state.db"
UPLOAD_KEYS = {"BASE_URL", "AUTHKEY", "BOT_USERNAME", "BOT_PASSWORD",
               "MOD_REMOTE_NAME", "MOD_VERSION"}


@dataclass(frozen=True)
class Page:
    title: str
    text: str
    sha256: str


def scan_pages(wikitext, mod, version):
    """只从指定版本两个类别的数字页构造标题；上传内容使用此次扫描的快照。"""
    mod, version = title_part(mod), title_part(version)
    versions = (wikitext / "Data/Versions").resolve()
    root = versions / mod / version
    if not root.resolve().is_relative_to(versions):
        raise ValueError(f"版本目录越出 wikitext/Data/Versions: {root}")
    pages = []
    for category, field in (("QuestLines", "questLines"), ("Quests", "quests")):
        folder = root / category
        paths = sorted((p for p in folder.glob("*.json") if p.stem.isdecimal()),
                       key=lambda p: int(p.stem))
        if not paths:
            raise ValueError(f"未找到 {folder} 的数据页，请先运行 split_quests.py")
        if [p.name for p in paths] != [f"{index}.json" for index in range(len(paths))]:
            raise ValueError(f"{folder} 的分页必须从 0.json 开始且连续")
        for path in paths:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"数据页路径越出当前版本目录: {path}")
            content = path.read_bytes()
            if len(content) > HARD_MAX_BYTES:
                raise ValueError(f"{path} 超过 {HARD_MAX_BYTES} 字节上限")
            text = content.decode("utf-8")
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get(field), list):
                raise ValueError(f"{path} 必须包含 {field} 数组")
            title = f"Data:Versions/{mod}/{version}/{category}/{path.name}"
            pages.append(Page(title, text, hashlib.sha256(content).hexdigest()))
    return pages


def pending_pages(db_path, api_url, pages, force=False):
    if force or not db_path.exists():
        return pages
    # dry-run 不创建状态库，不更新任何状态。
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        done = dict(conn.execute(
            "SELECT title, sha256 FROM uploads WHERE api_url=? AND status='done'", (api_url,)))
    return [page for page in pages if done.get(page.title.replace(" ", "_")) != page.sha256]


def queue_pages(conn, api_url, pages):
    conn.execute("""CREATE TABLE IF NOT EXISTS uploads (
        api_url TEXT NOT NULL, title TEXT NOT NULL, sha256 TEXT NOT NULL,
        status TEXT NOT NULL, PRIMARY KEY (api_url, title)
    )""")
    conn.executemany("""INSERT INTO uploads VALUES (?, ?, ?, 'pending_upload')
        ON CONFLICT(api_url, title) DO UPDATE SET sha256=excluded.sha256, status='pending_upload'
    """, [(api_url, page.title.replace(" ", "_"), page.sha256) for page in pages])
    conn.commit()


class WikiClient:
    """沿用 data_upload.py 的机器人登录、X-authkey 和 CSRF 编辑流程。"""

    def __init__(self, config, cookies=None):
        self.url = config["BASE_URL"]
        self.authkey = config.get("AUTHKEY", "")
        self.cookies = http.cookiejar.CookieJar()
        if cookies is not None:
            for cookie in cookies:
                self.cookies.set_cookie(copy.copy(cookie))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, params, *, post=False):
        encoded = urllib.parse.urlencode({"format": "json", **params}).encode("utf-8")
        headers = {"X-authkey": self.authkey, "User-Agent": "WikiBot/1.0"}
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            request = urllib.request.Request(self.url, data=encoded, headers=headers)
        else:
            separator = "&" if "?" in self.url else "?"
            request = urllib.request.Request(self.url + separator + encoded.decode("ascii"), headers=headers)
        with self.opener.open(request, timeout=60) as response:
            return json.loads(response.read())

    @staticmethod
    def check_response(response):
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"{error.get('code', 'API error')}: {error.get('info', '')}")

    def token(self, kind="csrf"):
        response = self.request({"action": "query", "meta": "tokens", "type": kind})
        self.check_response(response)
        return response["query"]["tokens"][kind + "token"]

    def login(self, username, password):
        response = self.request({"action": "login", "lgname": username, "lgpassword": password,
                                 "lgtoken": self.token("login")}, post=True)
        self.check_response(response)
        if response.get("login", {}).get("result") != "Success":
            raise RuntimeError("机器人登录失败，请检查 BOT_USERNAME / BOT_PASSWORD")

    def edit(self, page):
        params = {"action": "edit", "title": page.title, "text": page.text,
                  "summary": "机器人：上传汉化任务书数据", "bot": "1", "assert": "user",
                  "token": self.token()}
        for attempt in range(2):
            response = self.request(params, post=True)
            if response.get("error", {}).get("code") == "badtoken" and attempt == 0:
                params["token"] = self.token()
                continue
            self.check_response(response)
            result = response.get("edit", {})
            if result.get("result") != "Success":
                raise RuntimeError("API 未确认编辑成功，保留待上传状态")
            return "SKIP" if "nochange" in result else "OK"


def upload_pages(config, pages, db_path, threads):
    client = WikiClient(config)
    client.login(config["BOT_USERNAME"], config["BOT_PASSWORD"])
    stop = threading.Event()

    def interrupt(_signum, _frame):
        if not stop.is_set():
            print("\n收到中断；等待正在进行的请求结束，其余页面留待下次续传。")
        stop.set()

    def worker(page):
        if stop.is_set():
            return "PENDING"
        return WikiClient(config, client.cookies).edit(page)

    previous_handler = signal.signal(signal.SIGINT, interrupt)
    counters = {"OK": 0, "SKIP": 0, "FAIL": 0, "PENDING": 0}
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            queue_pages(conn, config["BASE_URL"], pages)
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(worker, page): page for page in pages}
                for number, future in enumerate(as_completed(futures), 1):
                    page = futures[future]
                    try:
                        status = future.result()
                    except Exception as exc:
                        status = "FAIL"
                        print(f"[{number}/{len(pages)}] FAIL {page.title}: {exc}")
                    else:
                        if status in {"OK", "SKIP"}:
                            conn.execute("""UPDATE uploads SET status='done'
                                WHERE api_url=? AND title=? AND sha256=?""",
                                (config["BASE_URL"], page.title.replace(" ", "_"), page.sha256))
                            conn.commit()
                        print(f"[{number}/{len(pages)}] {status} {page.title}")
                    counters[status] += 1
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(f"上传结束：成功 {counters['OK']}，远端未变化 {counters['SKIP']}，"
          f"失败 {counters['FAIL']}，中断未上传 {counters['PENDING']}。")
    return 130 if stop.is_set() else (1 if counters["FAIL"] else 0)


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("线程数必须大于 0")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=PROJECT_ROOT / ".env")
    parser.add_argument("--dry-run", action="store_true", help="只校验本地数据并列出计划，不联网或写状态库")
    parser.add_argument("--force", action="store_true", help="重新上传所有本地任务页，忽略已有成功记录")
    parser.add_argument("--debug", action="store_true", help="只处理前 20 个待上传页面")
    parser.add_argument("-t", "--threads", type=positive_int, default=8, help="并发线程数（默认 8）")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env_file, keys=UPLOAD_KEYS, required={"MOD_REMOTE_NAME", "MOD_VERSION"})
        config["BASE_URL"] = config.get("BASE_URL", "https://gtnh.huijiwiki.com/api.php").strip()
        url = urllib.parse.urlsplit(config["BASE_URL"])
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.password:
            raise ValueError("BASE_URL 必须是有效的 HTTP(S) API 地址，不得内嵌用户名或密码")
        pages = scan_pages(PROJECT_ROOT / "wikitext", config["MOD_REMOTE_NAME"], config["MOD_VERSION"])
        pending = pending_pages(STATE_DB, config["BASE_URL"], pages, args.force)
        selected = pending[:20] if args.debug else pending
        print(f"目标：{config['BASE_URL']}")
        print(f"本地 {len(pages)} 页；已完成且内容未变 {len(pages) - len(pending)} 页；"
              f"待上传 {len(pending)} 页；本次处理 {len(selected)} 页。")
        if args.dry_run:
            for page in selected:
                print(f"UPLOAD {page.title}")
            return 0
        if not selected:
            return 0
        for key in ("BOT_USERNAME", "BOT_PASSWORD"):
            if not config.get(key):
                raise ValueError(f"请在 {args.env_file} 或环境变量中设置 {key}")
        return upload_pages(config, selected, STATE_DB, min(args.threads, len(selected)))
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
