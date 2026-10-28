import json
from pathlib import Path
import tempfile
import unittest

from bounded_json import encode_json
from item_buckets import write_item_buckets
from item_names import write_name_index
from recipe_buckets import build_recipe_buckets
from routing_hash import bucket_id, choose_bucket_count
from storage_config import read_storage, write_version_config


class AdaptiveBucketTests(unittest.TestCase):
    def test_empty_categories_use_one_bucket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage = write_version_config(root, root / 'version-config.lua')
            self.assertEqual(storage, {'itemBuckets': 1, 'itemNameBuckets': 1, 'recipeBuckets': {}})

    def test_size_thresholds(self):
        for size, expected in ((0, 1), (100, 1), (101, 2), (200, 2), (201, 4), (801, 16)):
            self.assertEqual(choose_bucket_count(size, 100), expected)

    def test_items_names_shrink_and_generated_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rows = [{'key': f'mod:中文{i}', 'translatedName': f'名称{i}', 'note': '中' * 20}
                    for i in range(100)]
            write_item_buckets(rows, root / 'Items', 500)
            write_name_index({row['translatedName']: [row['key']] for row in rows}, root / 'ItemNames', 500)
            storage = write_version_config(root, root / 'version-config.lua')
            self.assertGreater(storage['itemBuckets'], 1)
            self.assertGreater(storage['itemNameBuckets'], 1)
            found = []
            for path in (root / 'Items').rglob('*.json'):
                page = json.loads(path.read_bytes())
                self.assertLessEqual(path.stat().st_size, 500)
                self.assertEqual(page['bucketCount'], storage['itemBuckets'])
                for row in page['items']:
                    self.assertEqual(bucket_id(row['key'], storage['itemBuckets']), path.parent.name)
                found.extend(page['items'])
            self.assertCountEqual(found, rows)
            for path in (root / 'ItemNames').rglob('*.json'):
                page = json.loads(path.read_bytes())
                self.assertLessEqual(path.stat().st_size, 500)
                for name in page['names']:
                    self.assertEqual(bucket_id(name, storage['itemNameBuckets']), path.parent.name)
            stale = root / 'ItemNames/100/9.json'
            stale.parent.mkdir()
            stale.write_text('{}', 'utf8')
            write_item_buckets(rows[:1], root / 'Items')
            write_name_index({rows[0]['translatedName']: [rows[0]['key']]}, root / 'ItemNames')
            self.assertFalse(stale.exists())
            for category in ('Items', 'ItemNames'):
                self.assertEqual([p.relative_to(root / category).as_posix()
                                  for p in (root / category).rglob('*.json')], ['00/0.json'])
            self.assertEqual(read_storage(root), {'itemBuckets': 1, 'itemNameBuckets': 1, 'recipeBuckets': {}})

    def test_same_handler_chooses_each_dataset_count_and_preserves_shared(self):
        handler = 'gregtech/GTNEIDefaultHandler_hammer'
        with tempfile.TemporaryDirectory() as temporary:
            counts = []
            for dataset, length in (('addon', 2), ('base', 80)):
                root = Path(temporary) / dataset
                source = root / 'Recipes' / handler
                source.mkdir(parents=True)
                rows = [{'index': f'0/{i}', 'outputItems': [{'key': f'mod:item{i}'}], 'note': '中' * 10}
                        for i in range(length)]
                shared = {'index': '1/0', 'outputItems': [{'key': 'mod:a'}, {'key': 'mod:b'}]}
                (source / '0.json').write_bytes(encode_json({'name': '锻造锤', 'recipes': rows}))
                (source / '1.json').write_bytes(encode_json({'name': '锻造锤', 'recipes': [shared]}))
                report = build_recipe_buckets(root, 900)
                count = report['recipeBuckets'][handler]
                counts.append(count)
                self.assertEqual(read_storage(root)['recipeBuckets'], {handler: count})
                found = []
                for route, paths in report['handlers'][handler]['routes'].items():
                    for relative in paths:
                        path = root / 'Recipes' / relative
                        self.assertLessEqual(path.stat().st_size, 900)
                        page = json.loads(path.read_bytes())
                        for row in page['recipes']:
                            expected = 'shared' if row == shared else bucket_id(row['outputItems'][0]['key'], count)
                            self.assertEqual(route, expected)
                        found.extend(page['recipes'])
                self.assertCountEqual(found, rows + [shared])
            self.assertEqual(counts[0], 1)
            self.assertGreater(counts[1], 1)


if __name__ == '__main__':
    unittest.main()
