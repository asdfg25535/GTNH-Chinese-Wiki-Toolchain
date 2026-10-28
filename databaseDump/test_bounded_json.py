import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bounded_json import HARD_MAX_BYTES, array_pages, encode_json, map_array_pages, write_bounded
from item_buckets import write_item_buckets
from item_names import read_items, write_name_index
from recipe_buckets import build_recipe_buckets, write_bucket
from routing_hash import bucket_id, HASH_ALGORITHM


class BoundedJsonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def read_records(self, field, limit):
        records = []
        for path in sorted(self.root.rglob('*.json')):
            self.assertLessEqual(path.stat().st_size, limit)
            records.extend(json.loads(path.read_bytes())[field])
        return records

    def test_exact_utf8_boundary_and_one_byte_over(self):
        metadata = {'来源': '中文😀', 'long': 'm' * 200}
        record = {'key': '你好😀', 'value': 'x'}
        expected = encode_json({**metadata, 'recipes': [record]})
        pages = list(array_pages([record], metadata, 'recipes', len(expected)))
        self.assertEqual(pages, [expected])
        with self.assertRaises(ValueError):
            list(array_pages([record], metadata, 'recipes', len(expected) - 1))
        self.assertEqual(self.read_records('recipes', HARD_MAX_BYTES), [])

    def test_final_write_guard_preserves_destination(self):
        target = self.root / 'page.json'
        content = b'"' + b'x' * (HARD_MAX_BYTES - 2) + b'"'
        write_bounded(target, content, HARD_MAX_BYTES)
        with self.assertRaises(ValueError):
            write_bounded(target, content + b' ', HARD_MAX_BYTES)
        self.assertEqual(target.read_bytes(), content)

    def test_paging_serializes_each_record_once(self):
        rows = [{'key': f'key{i}'} for i in range(100)]
        with patch('bounded_json.encode_json', wraps=encode_json) as encode:
            pages = list(array_pages(iter(rows), {}, 'items', 100))
        self.assertEqual(encode.call_count, len(rows) + 1)  # One shell, one encoding per row.
        self.assertEqual([row for page in pages for row in json.loads(page)['items']], rows)

    def test_public_limits_cannot_exceed_hard_limit_even_with_no_records(self):
        calls = [
            lambda limit: write_item_buckets([], self.root, limit),
            lambda limit: write_name_index({}, self.root, limit),
            lambda limit: write_bucket([], {}, self.root, limit),
            lambda limit: build_recipe_buckets(self.root, limit),
        ]
        for limit in (HARD_MAX_BYTES + 1, 0, -1, True, 100.0):
            for call in calls:
                with self.subTest(limit=limit, call=call), self.assertRaises(ValueError):
                    call(limit)

    def test_item_bucket_exact_size_and_lossless_pagination(self):
        row = {'key': '同一个😀', 'translatedName': '中文😀', 'nbt': 'a' * 30}
        metadata = {'hashAlgorithm': HASH_ALGORITHM, 'bucketCount': 4}
        limit = len(encode_json({**metadata, 'items': [row]}))
        self.assertEqual(write_item_buckets([row] * 5, self.root, limit), 5)
        self.assertEqual(self.read_records('items', limit), [row] * 5)
        with self.assertRaises(ValueError):
            write_item_buckets([row], self.root, limit - 1)

    def test_items_migration_removes_only_generated_numeric_pages(self):
        (self.root / '0.json').write_bytes(encode_json({'items': [{'key': 'old'}]}))
        note = self.root / 'notes.json'
        note.write_bytes(b'{}')
        unused = self.root / '99/7.json'
        unused.parent.mkdir()
        unused.write_bytes(b'{}')
        rows = [{'key': 'new', 'translatedName': '新物品'}]
        write_item_buckets(rows, self.root)
        self.assertFalse((self.root / '0.json').exists())
        self.assertFalse(unused.exists())
        self.assertTrue(note.exists())
        note.unlink()
        self.assertEqual(list(read_items(self.root)), rows)

    def test_bucket_hash_and_shrink_cleanup(self):
        import hashlib
        key = next(f'mod:item{i}' for i in range(5000) if bucket_id(f'mod:item{i}', 128) == '100')
        checksum = hashlib.sha256(key.encode('utf8')).digest()
        self.assertEqual(bucket_id(key, 128), f'{(checksum[15] ^ checksum[31]) % 128:02d}')
        self.assertEqual(bucket_id(key, 64), f'{(checksum[15] ^ checksum[31]) % 64:02d}')
        for name in ('63/0.json', '100/8.json', '127/0.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'{}')
        write_item_buckets([{'key': key}], self.root)
        self.assertEqual([p.relative_to(self.root).as_posix() for p in self.root.rglob('*.json')], ['00/0.json'])
        self.assertEqual(json.loads((self.root / '00/0.json').read_bytes())['bucketCount'], 1)
        write_item_buckets([], self.root)
        self.assertFalse(list(self.root.rglob('*.json')))

    def test_recipe_metadata_and_commas_are_counted(self):
        rows = [{'index': f'0/{i}', 'key': '😀'} for i in range(8)]
        metadata = {'type': '中文' * 50, 'routing': {'fields': ['outputItems']}}
        limit = len(encode_json({**metadata, 'recipes': rows[:2]}))
        self.assertEqual(len(write_bucket(rows, metadata, self.root, limit)), 4)
        self.assertEqual(self.read_records('recipes', limit), rows)
        with self.assertRaises(ValueError):
            write_bucket([rows[0]], {'metadata': 'x' * limit}, self.root, limit)

    def test_name_keys_split_losslessly_at_exact_boundary(self):
        name = '重名😀'
        keys = [f'键😀{i:02d}' for i in range(20)]
        metadata = {'bucketCount': 1}
        limit = len(encode_json({**metadata, 'names': {name: {'keys': keys[:2]}}}))
        self.assertEqual(write_name_index({name: keys}, self.root, limit), 10)
        pages = [json.loads(path.read_bytes()) for path in self.root.rglob('*.json')]
        self.assertTrue(all(set(page['names']) == {name} for page in pages))
        self.assertTrue(all(path.stat().st_size <= limit for path in self.root.rglob('*.json')))
        self.assertCountEqual([key for page in pages for key in page['names'][name]['keys']], keys)

    def test_single_name_or_key_cannot_overflow(self):
        for names in ({'n' * 500: ['k']}, {'n': ['k' * 500]}, {'n' * 500: []}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                write_name_index(names, self.root, 300)
        self.assertFalse(list(self.root.rglob('*.json')))

    def test_name_map_has_no_duplicate_keys_and_preserves_literal_names(self):
        expected = {name: [f'key😀{i}' for i in range(40)] for name in ('铁锭', 'a.b', '$名称', '😀')}
        actual = {name: [] for name in expected}

        def unique_object(pairs):
            self.assertEqual(len(pairs), len(dict(pairs)), 'A page must not repeat a JSON object key')
            return dict(pairs)

        for content in map_array_pages(expected, {'metadata': '中文😀'}, 'names', 220):
            self.assertLessEqual(len(content), 220)
            names = json.loads(content, object_pairs_hook=unique_object)['names']
            for name, keys in names.items():
                actual[name].extend(keys)
        self.assertEqual(actual, expected)

    def test_names_exact_boundary_one_byte_too_small(self):
        names = {'中文😀': ['key😀']}
        metadata = {'bucketCount': 1}
        limit = len(encode_json({**metadata, 'names': {'中文😀': {'keys': ['key😀']}}}))
        write_name_index(names, self.root, limit)
        self.read_records('names', limit)
        with self.assertRaises(ValueError):
            write_name_index(names, self.root, limit - 1)

    def test_oversized_manifest_is_not_written(self):
        handler = 'minecraft/ShapedRecipeHandler_crafting'
        source = self.root / 'Recipes' / handler
        source.mkdir(parents=True)
        for i in range(2):
            (source / f'{i}.json').write_bytes(encode_json({'recipes': [{'outputItems': ['key']}]}))
        with patch('recipe_buckets.SUPPORTED_HANDLERS', {handler: 'n' * HARD_MAX_BYTES}):
            with self.assertRaises(ValueError):
                build_recipe_buckets(self.root)
        self.assertFalse((self.root / 'Recipes/routing-manifest.json').exists())
        self.assertTrue((source / '0.json').exists(), 'Failed generation must retain original pages')
        self.assertTrue(all(path.stat().st_size <= HARD_MAX_BYTES for path in self.root.rglob('*.json')))

    def test_recipe_repeat_detects_three_digit_bucket_without_changes(self):
        source = self.root / 'Recipes/minecraft/ShapedRecipeHandler_crafting/100'
        source.mkdir(parents=True)
        page = source / '0.json'
        page.write_bytes(encode_json({'recipes': [{'outputItems': ['key']}]}))
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        with self.assertRaisesRegex(ValueError, '已分桶'):
            build_recipe_buckets(self.root)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*.json')}, before)

    def test_recipe_buckets_replace_flat_pages_and_repeat_refuses_without_changes(self):
        handler = 'minecraft/ShapedRecipeHandler_crafting'
        source = self.root / 'Recipes' / handler
        source.mkdir(parents=True)
        for i in range(2):
            (source / f'{i}.json').write_bytes(encode_json({'recipes': [{'outputItems': ['key']}]}))
        notes = source / '2-notes.json'
        notes.write_bytes(b'{"note":"preserve"}')
        report = build_recipe_buckets(self.root)
        self.assertFalse((source / '0.json').exists())
        self.assertFalse((source / '1.json').exists())
        self.assertTrue(notes.exists())
        self.assertFalse((self.root / 'RecipeBuckets').exists())
        for paths in report['handlers'][handler]['routes'].values():
            self.assertTrue(all((self.root / 'Recipes' / path).is_file() for path in paths))
        before = {path: path.read_bytes() for path in self.root.rglob('*.json')}
        with self.assertRaisesRegex(ValueError, '已分桶'):
            build_recipe_buckets(self.root)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob('*.json')}, before)


if __name__ == '__main__':
    unittest.main()
