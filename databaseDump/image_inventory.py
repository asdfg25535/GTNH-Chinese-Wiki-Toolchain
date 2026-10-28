"""更新本地图片与中文别名状态；--stats 查看统计，--clean-dynamic 清理动画条带任务。"""

import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from image_state import init_db as initialize_state, update_state
from image_plan import prepare_plan
from image_version import image_paths, require_source


def _load_dotenv():
    directory = Path(__file__).resolve().parent
    for parent in (directory, directory.parent, directory.parent.parent):
        path = parent / ".env"
        if path.exists():
            for line in path.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                key, _, value = line.partition("=")
                if key.strip() and value.strip():
                    os.environ.setdefault(key.strip(), value.strip())
            return


_load_dotenv()
DUMPS_DIR, DB_PATH, ICONS_DIR, NEW_CSV = image_paths(os.path.dirname(os.path.abspath(__file__)))


def init_db():
    initialize_state(DB_PATH)


def stats():
    if not Path(DB_PATH).exists():
        print("No image inventory database")
        return
    with closing(sqlite3.connect(DB_PATH)) as conn:
        legacy = "redirect_from" in {row[1] for row in conn.execute("PRAGMA table_info(images)")}
        for table, column in (("images", "icon_state"), ("images" if legacy else "redirects", "redirect_state")):
            condition = " WHERE redirect_from != ''" if legacy and column == "redirect_state" else ""
            rows = conn.execute(f"SELECT {column}, COUNT(*) FROM {table}{condition} GROUP BY {column}")
            for state, count in rows:
                print(f"  {column} {state}: {count:,}")


def update_inventory():
    require_source(DUMPS_DIR)
    plan = prepare_plan(DUMPS_DIR, NEW_CSV)
    redirects = plan['redirects']
    icons = set(plan['uploadIcons'])
    update_state(DB_PATH, icons, redirects)
    print(f"Current icons: {len(icons):,}; current redirects: {len(redirects):,}")
    stats()


def clean_dynamic():
    init_db()
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute("DELETE FROM redirects WHERE icon_name LIKE '%\\_dynamic.png' ESCAPE '\\'")
        count = conn.execute("DELETE FROM images WHERE icon_name LIKE '%\\_dynamic.png' ESCAPE '\\'").rowcount
    print(f"Deleted {count:,} animation-strip tasks")


def main():
    if "--stats" in sys.argv:
        stats()
    elif "--clean-dynamic" in sys.argv:
        clean_dynamic()
    else:
        update_inventory()


if __name__ == "__main__":
    main()
