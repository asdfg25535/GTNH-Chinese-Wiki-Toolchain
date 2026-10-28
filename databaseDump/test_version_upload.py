"""版本模块沿用远端命名，并在同版本数据页全部成功后上传。"""

from contextlib import closing, redirect_stdout
import http.cookiejar
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import data_upload
from data_version import version_module_title, version_tag
import wiki_inventory


class VersionUploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wikitext = self.root / 'wikitext'
        self.db = self.root / 'tasks.db'
        self.prefix = 'Data:Versions/GTNotLeisure/0.2.7-rc2/'
        self.module = 'Module:Database/Versions/GTNotLeisure/0.2.7-rc2'
        self.data_titles = [self.prefix + 'Items/00/0.json', self.prefix + 'ItemNames/00/0.json']
        self.enterContext(patch.multiple(wiki_inventory, WIKITEXT_DIR=str(self.wikitext),
                                        DB_PATH=str(self.db), WIKI_PREFIX=self.prefix))
        self.enterContext(patch.multiple(data_upload, WIKITEXT_DIR=str(self.wikitext),
                                        TASK_DB=str(self.db), WIKI_PREFIX=self.prefix))
        self.enterContext(redirect_stdout(io.StringIO()))
        wiki_inventory.init_db(False)
        for title in self.data_titles:
            self.write_page(title, '{}')
        self.write_page(self.module, 'return { storage = { itemBuckets = 1 } }')

    def write_page(self, title, content):
        path = self.wikitext / wiki_inventory.title_to_path(title)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, 'utf8')
        return path

    def rebuild(self, refresh=False):
        with patch.object(wiki_inventory, 'login'), \
                patch.object(wiki_inventory, 'fetch_wiki_titles', return_value=set()):
            wiki_inventory.rebuild_tasks(refresh)

    def run_upload(self, args=(), edit=None):
        with patch.object(data_upload, 'login_once', return_value=http.cookiejar.CookieJar()), \
                patch.object(data_upload.signal, 'signal'), \
                patch.object(data_upload, 'edit_page', side_effect=edit, return_value={}) as save:
            code = data_upload.main(list(args))
        return code, [call.args[1] for call in save.call_args_list]

    def status(self, title):
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT status FROM tasks WHERE title=?', (title,)).fetchone()[0]

    def test_remote_name_and_module_extension(self):
        with patch.dict(os.environ, MOD_LOCAL_NAME='sciencenotleisure',
                        MOD_REMOTE_NAME='GTNotLeisure', MOD_VERSION='0.2.7-rc2'):
            self.assertEqual(version_module_title('Data:Versions/' + version_tag()), self.module)
        self.assertEqual(wiki_inventory.title_to_path(self.module),
                         'Module/Database/Versions/GTNotLeisure/0.2.7-rc2.lua')
        for title in self.data_titles + [self.module, self.module + '.lua']:
            path = wiki_inventory.title_to_path(title)
            self.assertEqual(wiki_inventory.path_to_title(path.replace('/', '\\')), title)
        with self.assertRaises(ValueError):
            version_module_title('Data:Versions/GTNH/../x')

    def test_inventory_and_uploader_limit_to_exact_version_module(self):
        outside = [self.module + '-next', self.module + '/child',
                   'Module:Database/Versions/sciencenotleisure/0.2.7-rc2',
                   'Module:Database/Versions', 'Module:Database',
                   'Data:Versions/GTNotLeisure/0.2.7-rc20/Items/00/0.json']
        for title in outside:
            self.write_page(title, 'return {}')
        self.rebuild()
        self.assertCountEqual([title for _, title in data_upload.get_pending_uploads()],
                              self.data_titles + [self.module])
        # 旧清单中即使存在其他模块，也只接受当前版本的精确标题。
        with closing(sqlite3.connect(self.db)) as conn:
            for title in outside + [self.prefix + 'Index/0.tabx']:
                conn.execute('INSERT INTO tasks (title, path) VALUES (?, ?)',
                             (title, wiki_inventory.title_to_path(title)))
            conn.commit()
        code, saved = self.run_upload()
        self.assertEqual(code, 0)
        self.assertCountEqual(saved, self.data_titles + [self.module])
        self.assertEqual(saved[-1], self.module)

    def test_config_waits_for_data_success_and_failed_data_can_resume(self):
        self.rebuild()

        def fail_one(_opener, title, _text, **_kwargs):
            if title == self.data_titles[0]:
                raise RuntimeError('test data failure')
            return {}

        code, saved = self.run_upload(edit=fail_one)
        self.assertEqual(code, 1)
        self.assertNotIn(self.module, saved)
        self.assertEqual(self.status(self.module), 'pending_upload')
        code, saved = self.run_upload()
        self.assertEqual(code, 0)
        self.assertEqual(saved, [self.data_titles[0], self.module])
        self.assertEqual(self.status(self.module), 'done')

    def test_debug_leaves_config_pending_even_when_all_data_fits(self):
        self.rebuild()
        code, saved = self.run_upload(['--debug'])
        self.assertEqual(code, 0)
        self.assertCountEqual(saved, self.data_titles)
        self.assertEqual(self.status(self.module), 'pending_upload')
        code, saved = self.run_upload()
        self.assertEqual(code, 0)
        self.assertEqual(saved, [self.module])

    def test_interruption_keeps_config_pending(self):
        self.rebuild()

        def interrupt(_opener, _title, _text, **_kwargs):
            data_upload._stop.set()
            return {}

        code, saved = self.run_upload(['-t', '1'], edit=interrupt)
        self.assertEqual(code, 130)
        self.assertNotIn(self.module, saved)
        self.assertEqual(self.status(self.module), 'pending_upload')

    def test_config_failure_retries_only_config(self):
        self.rebuild()

        def fail_config(_opener, title, _text, **_kwargs):
            if title == self.module:
                self.assertTrue(all(self.status(data) == 'done' for data in self.data_titles))
                raise RuntimeError('test config failure')
            return {'nochange': ''}

        code, saved = self.run_upload(edit=fail_config)
        self.assertEqual(code, 1)
        self.assertEqual(saved[-1], self.module)
        self.assertEqual(self.status(self.module), 'pending_upload')
        code, saved = self.run_upload()
        self.assertEqual(code, 0)
        self.assertEqual(saved, [self.module])

    def test_inventory_keeps_done_config_and_detects_changes(self):
        self.rebuild()
        self.assertEqual(self.run_upload()[0], 0)
        self.rebuild()
        self.assertEqual(data_upload.get_pending_uploads(), [])
        self.write_page(self.module, 'return { storage = { itemBuckets = 128, itemNameBuckets = 64 } }')
        self.rebuild()
        self.assertEqual([title for _, title in data_upload.get_pending_uploads()], [self.module])
        self.assertEqual(self.run_upload()[0], 0)
        self.rebuild(refresh=True)
        self.assertCountEqual([title for _, title in data_upload.get_pending_uploads()],
                              self.data_titles + [self.module])

    def test_spaces_and_underscores_share_the_same_upload_scope(self):
        prefix = 'Data:Versions/Remote Mod/1/'
        module = 'Module:Database/Versions/Remote_Mod/1'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('INSERT INTO tasks (title, path) VALUES (?, ?)',
                         (module, wiki_inventory.title_to_path(module)))
            conn.commit()
        with patch.object(data_upload, 'WIKI_PREFIX', prefix):
            self.assertEqual(data_upload.get_pending_uploads(), [(wiki_inventory.title_to_path(module), module)])


if __name__ == '__main__':
    unittest.main()
