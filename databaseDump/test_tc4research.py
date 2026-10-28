"""研究分页保留原记录，并严格使用当前数据源。"""

import json
import os
from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bounded_json import HARD_MAX_BYTES, encode_json
from data_version import addon_mods
import split_all


class ResearchPagesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / 'dumps'
        self.source.mkdir()
        self.version = root / 'version'
        self.output = self.version / 'Tc4research'
        context = patch.multiple(split_all, DUMPS_DIR=str(self.source),
                                 REFERENCE_DUMPS_DIR=str(self.source), VERSION=str(self.version),
                                 local_mod_name=lambda: 'GTNH')
        context.start()
        self.addCleanup(context.stop)

    def write_source(self, rows, **metadata):
        (self.source / 'tc4research.json').write_bytes(encode_json({**metadata, 'tc4Researches': rows}))

    def pages(self):
        return sorted(self.output.glob('*.json'), key=lambda path: int(path.stem))

    def test_default_task_preserves_fields_and_replaces_obsolete_pages(self):
        self.assertIs(split_all.TASKS['tc4research'][1], split_all.do_tc4research)
        rows = [{'Key': 'RESEARCH', 'Name': '研究', 'Category': '基本信息', 'IsAutoUnlock': True},
                {'Key': 'TINYBOWTIE', 'Name': '蝴蝶结', 'ItemTriggers': ['黑色羊毛'],
                 'AspectTriggers': ['Pannus'], 'IsHidden': True}]
        self.write_source(rows)
        self.output.mkdir(parents=True)
        (self.output / '9.json').write_text('{}', encoding='utf-8')
        split_all.do_tc4research(False)
        self.assertEqual([path.name for path in self.pages()], ['0.json'])
        self.assertEqual(json.loads(self.pages()[0].read_bytes()), {'tc4Researches': rows})
        self.assertNotIn('index', json.loads(self.pages()[0].read_bytes())['tc4Researches'][0])

    def test_final_utf8_limit_splits_large_records_without_losing_metadata(self):
        rows = [{'Key': str(index), 'Name': '研究😀' * 120000} for index in range(3)]
        self.write_source(rows, source='original metadata')
        split_all.do_tc4research(False)
        self.assertEqual(len(self.pages()), 3)
        found = []
        for page in self.pages():
            self.assertLessEqual(page.stat().st_size, HARD_MAX_BYTES)
            data = json.loads(page.read_bytes())
            self.assertEqual(data['source'], 'original metadata')
            found.extend(data['tc4Researches'])
        self.assertEqual(found, rows)

    def test_exact_hard_limit_is_accepted_and_one_byte_more_is_rejected(self):
        row = {'Key': 'TEST', 'Name': ''}
        row['Name'] = 'x' * (HARD_MAX_BYTES - len(encode_json({'tc4Researches': [row]})))
        with patch.object(split_all, 'DEFAULT_MAX_BYTES', HARD_MAX_BYTES):
            self.write_source([row])
            split_all.do_tc4research(False)
            page = self.output / '0.json'
            self.assertEqual(page.stat().st_size, HARD_MAX_BYTES)
            original = page.read_bytes()
            row['Name'] += 'x'
            self.write_source([row])
            with self.assertRaisesRegex(ValueError, '超过分页上限'):
                split_all.do_tc4research(False)
            self.assertEqual(page.read_bytes(), original)

    def test_missing_filtered_source_never_borrows_full_research_table(self):
        self.write_source([{'Key': 'BASE', 'Parents': ['ADDON_CHANGED_PARENT']}])
        filtered = self.source / 'filtered' / 'ExampleAddon'
        filtered.mkdir(parents=True)
        with patch.object(split_all, 'DUMPS_DIR', str(filtered)), \
                patch.object(split_all, 'local_mod_name', return_value='ExampleAddon'), \
                patch.object(split_all, 'reference_path', side_effect=AssertionError('research must use its own source')):
            split_all.do_tc4research(False)
            self.assertFalse(self.output.exists())
            (filtered / 'tc4research.json').write_bytes(b'')
            split_all.do_tc4research(False)
            self.assertFalse(self.output.exists())
            (filtered / 'tc4research.json').write_bytes(encode_json({'tc4Researches': [{'Key': 'ADDON'}]}))
            split_all.do_tc4research(False)
        self.assertEqual(json.loads(self.pages()[0].read_bytes()), {'tc4Researches': [{'Key': 'ADDON'}]})

    def test_full_export_selects_registration_owner_across_shared_categories(self):
        rows = [{'Key': 'BASE', 'Category': '基本信息', 'sourceMod': 'Thaumcraft'},
                {'Key': 'ADDON_SHARED', 'Category': '基本信息', 'sourceMod': 'ExampleAddon'},
                {'Key': 'BASE_SHARED', 'Category': '附属分类', 'sourceMod': 'Thaumcraft'},
                {'Key': 'ADDON_OWN', 'Category': '附属分类', 'sourceMod': 'ExampleAddon'},
                {'Key': 'UNKNOWN', 'Category': '附属分类'}]
        self.write_source(rows)
        with patch.object(split_all, 'DUMPS_DIR', str(self.source / 'filtered/exampleaddon')), \
                patch.object(split_all, 'local_mod_name', return_value='exampleaddon'):
            split_all.do_tc4research(False)
        self.assertEqual(json.loads(self.pages()[0].read_bytes())['tc4Researches'], [rows[1], rows[3]])

    def test_public_addon_ids_are_casefolded_registration_ids(self):
        self.assertEqual(addon_mods(), {'twistspacetechnology', 'sciencenotleisure', 'programmablehatches'})

    def test_base_and_addon_scopes_partition_the_complete_research_table(self):
        rows = [
            {'Key': 'TST_BASE', 'Category': '共享分类', 'sourceMod': 'Thaumcraft'},
            {'Key': 'TST_ADDON', 'Category': '共享分类', 'sourceMod': 'TwistSpaceTechnology'},
            {'Key': 'GTNL_BASE', 'Category': '附属分类', 'sourceMod': 'dreamcraft'},
            {'Key': 'GTNL_ADDON', 'Category': '附属分类', 'sourceMod': 'sciencenotleisure'},
            {'Key': 'PH_BASE', 'Category': '基本信息', 'sourceMod': 'KekzTech'},
            {'Key': 'PH_ADDON', 'Category': '基本信息', 'sourceMod': 'ProgrammableHatches'},
            {'Key': 'SAME_PREFIX', 'Category': '共享分类', 'sourceMod': 'TwistSpaceTechnologyExtension'},
            {'Key': 'OLD', 'Category': '附属分类'},
            {'Key': 'UNKNOWN', 'sourceMod': None},
        ]
        self.write_source(rows, source='complete snapshot')
        scopes = {}
        for mod in ('GTNH', 'twistspacetechnology', 'sciencenotleisure', 'programmablehatches'):
            source = self.source if mod == 'GTNH' else self.source / 'filtered' / mod
            with patch.object(split_all, 'DUMPS_DIR', str(source)), \
                    patch.object(split_all, 'VERSION', str(self.version / mod)), \
                    patch.object(split_all, 'local_mod_name', return_value=mod):
                split_all.do_tc4research(False)
            document = json.loads((self.version / mod / 'Tc4research/0.json').read_bytes())
            self.assertEqual(document['source'], 'complete snapshot')
            scopes[mod] = document['tc4Researches']

        self.assertEqual(scopes['GTNH'], [rows[index] for index in (0, 2, 4, 6, 7, 8)])
        for mod, index in (('twistspacetechnology', 1), ('sciencenotleisure', 3), ('programmablehatches', 5)):
            self.assertEqual(scopes[mod], [rows[index]])
        # Compare complete rows as a multiset: each original record appears exactly once across all scopes.
        found = [json.dumps(row, sort_keys=True) for selected in scopes.values() for row in selected]
        self.assertEqual(Counter(found), Counter(json.dumps(row, sort_keys=True) for row in rows))

    def test_full_export_without_selected_owner_writes_empty_page(self):
        self.write_source([{'Key': 'BASE', 'sourceMod': 'Thaumcraft'}])
        with patch.object(split_all, 'DUMPS_DIR', str(self.source / 'filtered/ExampleAddon')), \
                patch.object(split_all, 'local_mod_name', return_value='ExampleAddon'):
            split_all.do_tc4research(False)
        self.assertEqual(json.loads(self.pages()[0].read_bytes()), {'tc4Researches': []})

    def test_empty_export_writes_empty_page_and_clears_old_records(self):
        self.write_source([{'Key': 'OLD'}])
        split_all.do_tc4research(False)
        self.write_source([])
        split_all.do_tc4research(False)
        self.assertEqual(json.loads(self.pages()[0].read_bytes()), {'tc4Researches': []})


class ResearchEntryPointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'dumps'
        self.source.mkdir()
        self.filtered = self.source / 'filtered' / 'TwistSpaceTechnology'
        self.output = self.root / 'wikitext/Data/Versions/TST/fixture/Tc4research/0.json'
        self.rows = [
            {'Key': 'BASE', 'Category': '共享分类', 'sourceMod': 'Thaumcraft'},
            {'Key': 'ADDON', 'Category': '共享分类', 'sourceMod': 'TwistSpaceTechnology'},
        ]
        (self.source / 'tc4research.json').write_bytes(encode_json({'tc4Researches': self.rows}))
        environment = patch.dict(os.environ, {
            'DUMPS_DIR': str(self.source),
            'MOD_LOCAL_NAME': 'TwistSpaceTechnology',
            'MOD_REMOTE_NAME': 'TST',
            'MOD_VERSION': 'fixture',
        })
        environment.start()
        self.addCleanup(environment.stop)
        # main configures these module globals from the environment for each invocation.
        globals_to_restore = {name: getattr(split_all, name) for name in (
            'DUMPS_DIR', 'REFERENCE_DUMPS_DIR', 'VERSION_TAG', 'VERSION', 'AUX_DIR')}
        context = patch.multiple(split_all, _PROJECT_ROOT=str(self.root), **globals_to_restore)
        context.start()
        self.addCleanup(context.stop)

    def test_research_task_extracts_full_export_without_filtered_directory(self):
        with patch('sys.argv', ['split_all.py', 'tc4research']):
            split_all.main()
        self.assertEqual(json.loads(self.output.read_bytes()), {'tc4Researches': [self.rows[1]]})
        self.assertFalse(self.filtered.exists())

    def test_base_only_default_batch_preserves_research_without_addon_directories(self):
        rows = [
            {'Key': 'WARPWARNING', 'Category': '基本信息', 'sourceMod': 'dreamcraft'},
            {'Key': 'ICHORJAR', 'Category': '炼金术', 'sourceMod': 'kekztech'},
        ]
        # Both current ownership records and older exports run through the normal GTNH batch.
        for has_source in (True, False):
            with self.subTest(has_source=has_source):
                selected = rows if has_source else [
                    {key: value for key, value in row.items() if key != 'sourceMod'} for row in rows
                ]
                document = {'source': 'base-only fixture', 'tc4Researches': selected}
                (self.source / 'tc4research.json').write_bytes(encode_json(document))
                with patch.dict(os.environ, {'MOD_LOCAL_NAME': 'GTNH', 'MOD_REMOTE_NAME': 'GTNH'}), \
                        patch('sys.argv', ['split_all.py']):
                    split_all.main()
                versions = self.root / 'wikitext/Data/Versions'
                output = versions / 'GTNH/fixture/Tc4research/0.json'
                self.assertEqual(json.loads(output.read_bytes()), document)
                self.assertEqual({path.name for path in versions.iterdir()}, {'GTNH'})
                self.assertFalse((self.source / 'filtered').exists())

    def test_absent_addon_research_task_writes_empty_result(self):
        document = {'tc4Researches': [self.rows[0]]}
        source = self.source / 'tc4research.json'
        source.write_bytes(encode_json(document))
        original = source.read_bytes()
        with patch('sys.argv', ['split_all.py', 'tc4research']):
            split_all.main()
        self.assertEqual(json.loads(self.output.read_bytes()), {'tc4Researches': []})
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(self.filtered.exists())

    def test_default_and_other_tasks_require_their_filtered_directory(self):
        for arguments in ([], ['items'], ['tc4research', 'recipes']):
            with self.subTest(arguments=arguments), \
                    patch('sys.argv', ['split_all.py', *arguments]), \
                    self.assertRaisesRegex(ValueError, 'python split_all.py tc4research'):
                split_all.main()
            self.assertFalse(self.output.exists())

    def test_configure_source_remains_strict_for_other_callers(self):
        with self.assertRaisesRegex(ValueError, '数据源目录不存在'):
            split_all.configure_source()

    def test_research_task_uses_existing_filtered_research(self):
        self.filtered.mkdir(parents=True)
        own_rows = [{'Key': 'LEGACY_ADDON', 'Category': '共享分类'}]
        (self.filtered / 'tc4research.json').write_bytes(encode_json({'tc4Researches': own_rows}))
        with patch('sys.argv', ['split_all.py', 'tc4research']):
            split_all.main()
        self.assertEqual(json.loads(self.output.read_bytes()), {'tc4Researches': own_rows})


if __name__ == '__main__':
    unittest.main()
