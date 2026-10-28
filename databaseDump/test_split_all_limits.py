"""旧发布页的最终字节限制，以及 Items/Recipes 单份分桶输出契约。"""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bounded_json import HARD_MAX_BYTES, encode_json
from item_names import read_items
from routing_hash import bucket_id
import split_all


class SplitOutputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.version = self.root / "version"
        self.state = self.root / "state"
        for path in (self.source, self.version / "Items", self.state):
            path.mkdir(parents=True)
        context = patch.multiple(split_all, VERSION=str(self.version), VERSION_TAG="Example/1",
                                 DUMPS_DIR=str(self.source), REFERENCE_DUMPS_DIR=str(self.source),
                                 AUX_DIR=str(self.state))
        context.start()
        self.addCleanup(context.stop)

    def test_oredictionary_wildcards_expand_real_damage_and_nbt_keys(self):
        items = [
            {"key": "example:item", "rawKey": "example:item"},
            {"key": "example:item:9", "rawKey": "example:item:9"},
            {"key": "example:item:9_hash", "rawKey": "example:item:9", "nbt": "{v:1}"},
            {"key": "example:item:32767", "rawKey": "example:item:32767"},
        ]
        entries = [{"oreDict": "ore", "items": ["example:item:32767", "example:item:9", "example:missing:32767", "example:other"]}]
        flat, report = split_all.normalize_ore_dictionary(entries, items)
        self.assertEqual(flat, {"ore": ["example:item", "example:item:9", "example:item:9_hash", "example:other"]})
        self.assertEqual(report['expandedWildcardMembers'], 2)
        self.assertEqual(report['unresolvedWildcardMembers'], [{"oreDict": "ore", "key": "example:missing:32767"}])
        second, second_report = split_all.normalize_ore_dictionary(
            [{"oreDict": key, "items": values} for key, values in flat.items()], items)
        self.assertEqual(second, flat)
        self.assertEqual(second_report['expandedWildcardMembers'], 0)

    def test_oredictionary_writer_reports_missing_and_respects_hard_limit(self):
        (self.version / "Items/0.json").write_bytes(encode_json({"items": [
            {"key": "example:critter:1", "rawKey": "example:critter:1"}]}))
        (self.source / "oreDictionary.json").write_bytes(encode_json({"oreDictionaries": [
            {"oreDict": "critter", "items": ["example:old:32767", "example:critter:32767"]}]}))
        split_all.do_oredictionary(False)
        page = self.version / "OreDictionary/0.json"
        self.assertEqual(json.loads(page.read_bytes()), {"critter": ["example:critter:1"]})
        self.assertLessEqual(page.stat().st_size, HARD_MAX_BYTES)
        report = json.loads((self.state / "oredictionary-associations.json").read_bytes())
        self.assertEqual(len(report['unresolvedWildcardMembers']), 1)
        (self.source / "oreDictionary.json").write_bytes(encode_json({"oreDictionaries": [
            {"oreDict": "huge", "items": ["x" * HARD_MAX_BYTES]}]}))
        previous = page.read_bytes()
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all.do_oredictionary(False)
        self.assertEqual(page.read_bytes(), previous)

    def test_oredictionary_requires_nonempty_items_before_changing_pages_or_report(self):
        (self.source / "oreDictionary.json").write_bytes(encode_json({"oreDictionaries": [
            {"oreDict": "ore", "items": ["example:item:32767"]}]}))
        report = self.state / "oredictionary-associations.json"
        report.write_bytes(b"previous report")
        for case in ("missing-directory", "no-pages", "empty-json", "empty-tabx"):
            with self.subTest(case=case):
                version = self.root / case
                items_dir = version / "Items"
                if case != "missing-directory":
                    items_dir.mkdir(parents=True)
                if case == "empty-json":
                    (items_dir / "0.json").write_bytes(encode_json({"items": []}))
                elif case == "empty-tabx":
                    (items_dir / "0.tabx").write_bytes(encode_json({
                        "schema": {"fields": [{"name": "key", "type": "string"}]}, "data": []}))
                output = version / "OreDictionary"
                output.mkdir(parents=True)
                previous = {"0.json": b"previous first page", "1.json": b"previous second page"}
                for name, content in previous.items():
                    (output / name).write_bytes(content)

                with patch.object(split_all, "VERSION", str(version)):
                    with self.assertRaisesRegex(ValueError, "非空的 Items 数据.*python split_all.py items oredict"):
                        split_all.do_oredictionary(False)

                self.assertEqual({path.name: path.read_bytes() for path in output.iterdir()}, previous)
                self.assertEqual(report.read_bytes(), b"previous report")

    def test_oredictionary_removes_obsolete_pages_only_after_full_success(self):
        (self.version / "Items/0.json").write_bytes(encode_json({"items": [{"key": "example:one"}]}))
        source = self.source / "oreDictionary.json"
        source.write_bytes(encode_json({"oreDictionaries": [
            {"oreDict": "first", "items": ["example:one"]},
            {"oreDict": "second", "items": ["example:two"]}]}))
        with patch.object(split_all, "MAX_FILE_SIZE", 35):
            split_all.do_oredictionary(False)
        output = self.version / "OreDictionary"
        self.assertTrue((output / "1.json").exists())
        marker = output / "manifest.json"
        marker.write_bytes(b'{}')
        source.write_bytes(encode_json({"oreDictionaries": [{"oreDict": "first", "items": []}]}))
        split_all.do_oredictionary(True)
        self.assertTrue((output / "1.json").exists())
        with patch.object(split_all, "write_version_json", side_effect=ValueError("test write failure")):
            with self.assertRaisesRegex(ValueError, "test write failure"):
                split_all.do_oredictionary(False)
        self.assertTrue((output / "1.json").exists())
        split_all.do_oredictionary(False)
        self.assertEqual([path.name for path in output.glob('*.json') if path.stem.isdecimal()], ['0.json'])
        self.assertTrue(marker.exists())

    def test_batch_writer_preserves_ranges_indexes_and_exact_utf8(self):
        items = [{"key": "example:一", "value": "中文😀"}, {"key": "example:two"}, {"key": "example:three"}]
        output = self.version / "Aspects"
        split_all.write_batches(items, [(0, 1), (1, 3)], "items", output)
        self.assertEqual((output / "0.json").read_bytes(),
                         encode_json({"items": [{**items[0], "index": "0/0"}]}))
        self.assertEqual([row["index"] for row in json.loads((output / "1.json").read_bytes())["items"]],
                         ["1/0", "1/1"])
        self.assertNotIn("index", items[0])

    def test_single_oversized_record_does_not_replace_existing_page(self):
        output = self.version / "Aspects"
        output.mkdir()
        page = output / "0.json"
        page.write_bytes(b"previous content")
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all.write_batches([{"value": "中" * 700000}], [(0, 1)], "items", output)
        self.assertEqual(page.read_bytes(), b"previous content")

    def test_association_single_record_growth_is_rejected_before_overwrite(self):
        output = self.version / "Recipes/example/handler"
        output.mkdir(parents=True)
        payload = {"recipes": [{"index": "0/0", "inputItems": [{"key": "example:x", "nbt": "{v:1}"}]}],
                   "metadata": ""}
        payload["metadata"] = "x" * (HARD_MAX_BYTES - 10 - len(encode_json(payload)))
        page = output / "0.json"
        original = encode_json(payload)
        page.write_bytes(original)
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        (source / "handler.json").write_bytes(original)
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all.do_recipes(False)
        self.assertEqual(page.read_bytes(), original)
        self.assertLessEqual(page.stat().st_size, HARD_MAX_BYTES)

    def test_recipe_wrapper_counts_towards_limit(self):
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        (source / "handler.json").write_bytes(encode_json({
            "metadata": "x" * HARD_MAX_BYTES, "recipes": [{"outputItems": [{"key": "example:x"}]}]}))
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all.do_recipes(False)
        self.assertFalse((self.version / "Recipes/example/handler/0.json").exists())

    def test_recipe_batches_use_actual_compact_sizes_and_final_indexes(self):
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        recipes = [{"id": i, "description": "中文😀" * 15000,
                    "outputItems": [{"key": "example:x"}]} for i in range(20)]
        (source / "handler.json").write_bytes(encode_json({"type": "fixture", "recipes": recipes}))
        split_all.do_recipes(False)
        output = self.version / "Recipes/example/handler"
        pages = sorted(output.glob("*.json"), key=lambda path: int(path.stem))
        self.assertGreater(len(pages), 1)
        found = []
        for path in pages:
            self.assertLessEqual(path.stat().st_size, HARD_MAX_BYTES)
            for position, recipe in enumerate(json.loads(path.read_bytes())["recipes"]):
                self.assertEqual(recipe.pop("index"), f"{path.stem}/{position}")
                found.append(recipe)
        self.assertEqual(found, recipes)

    def test_raw_large_candidate_recipe_is_associated_before_any_published_write(self):
        nbt = '{value:"' + 'x' * 700 + '"}'
        (self.version / "Items/0.json").write_bytes(encode_json({'items': [
            {'key': 'example:facade_hash', 'rawKey': 'example:facade', 'nbt': nbt}]}))
        source = self.source / 'recipes/example'
        source.mkdir(parents=True)
        payload = {'recipes': [{'inputItems': [[{'key': 'example:facade', 'nbt': nbt} for _ in range(3000)]],
                                'outputItems': [{'key': 'example:result'}]}]}
        raw = encode_json(payload)
        self.assertGreater(len(raw), HARD_MAX_BYTES)
        (source / 'integration.json').write_bytes(raw)
        with patch.object(split_all, 'write_bounded', wraps=split_all.write_bounded) as write:
            split_all.do_recipes(False)
        self.assertTrue(write.called)
        self.assertTrue(all(len(call.args[1]) <= HARD_MAX_BYTES for call in write.call_args_list))
        page = self.version / 'Recipes/example/integration/0.json'
        candidates = json.loads(page.read_bytes())['recipes'][0]['inputItems'][0]
        self.assertEqual(len(candidates), 3000)
        self.assertTrue(all(row['key'] == 'example:facade_hash' and 'nbt' not in row for row in candidates))

    def test_recipe_debug_limits_handlers_and_keeps_all_pages_and_diagnostics(self):
        source = self.source / 'recipes/example'
        source.mkdir(parents=True)
        for handler, count in (('a', 7), ('b', 1), ('c', 1), ('d', 1)):
            recipes = [{'description': 'x' * 200, 'outputItems': [
                {'key': 'Forestry:beeDroneGE', 'nbt': '{v:1}'}]} for _ in range(count)]
            (source / f'{handler}.json').write_bytes(encode_json({'recipes': recipes}))
        with patch.object(split_all, 'DEFAULT_MAX_BYTES', 400):
            split_all.do_recipes(True)
        self.assertEqual(len(list((self.version / 'Recipes/example/a').glob('*.json'))), 7)
        self.assertFalse((self.version / 'Recipes/example/d').exists())
        diagnostics = json.loads((self.state / 'bee_association_diagnostics.json').read_bytes())
        self.assertEqual(len(diagnostics), 9)
        for row in diagnostics:
            path = self.version / 'Recipes' / row['file']
            page, position = map(int, row['recipeIndex'].split('/'))
            self.assertEqual(path.stem, str(page))
            self.assertEqual(json.loads(path.read_bytes())['recipes'][position]['index'], row['recipeIndex'])

    def test_nbt_growth_is_paginated_after_association_with_final_diagnostic_locations(self):
        variants = [{"key": f"example:variant{i}_" + "x" * 180, "rawKey": f"example:item:{i}"}
                    for i in range(4000)]
        (self.version / "Items/0.json").write_bytes(encode_json({"items": variants}))
        output = self.version / "Recipes/example/growing"
        output.mkdir(parents=True)
        recipes = [{"id": i, "index": f"0/{i}", "inputItems": ["example:item:32767"],
                    "outputItems": [{"key": "Forestry:beeDroneGE", "nbt": "{v:1}"}]} for i in range(3)]
        (output / "0.json").write_bytes(encode_json({"type": "fixture", "recipes": recipes}))
        stable = self.version / "Recipes/example/stable/0.json"
        stable.parent.mkdir()
        stable.write_text('{ "recipes": [{"index":"0/0","outputItems":["example:x"]}] }', 'utf8')
        unchanged = stable.read_bytes()
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        (source / "growing.json").write_bytes((output / "0.json").read_bytes())
        split_all.do_recipes(False)
        self.assertEqual(stable.read_bytes(), unchanged)
        pages = sorted(output.glob("*.json"), key=lambda path: int(path.stem))
        self.assertGreater(len(pages), 1)
        found = []
        for path in pages:
            self.assertLessEqual(path.stat().st_size, HARD_MAX_BYTES)
            for position, recipe in enumerate(json.loads(path.read_bytes())["recipes"]):
                self.assertEqual(recipe["index"], f"{path.stem}/{position}")
                self.assertEqual(len(recipe["inputItems"][0]), len(variants))
                found.append(recipe["id"])
        self.assertEqual(found, [0, 1, 2])
        diagnostics = json.loads((self.state / "bee_association_diagnostics.json").read_bytes())
        self.assertEqual(len(diagnostics), 3)
        self.assertTrue(any(row["recipeIndex"] == "1/0" for row in diagnostics))
        for row in diagnostics:
            path = self.version / "Recipes" / row["file"]
            page, position = map(int, row["recipeIndex"].split('/'))
            self.assertEqual(path.stem, str(page))
            self.assertEqual(json.loads(path.read_bytes())["recipes"][position]["index"], row["recipeIndex"])

    def test_recipe_regeneration_removes_old_bucket_pages_only(self):
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        (source / "handler.json").write_bytes(encode_json({"recipes": [{"outputItems": [{"key": "example:x"}]}]}))
        output = self.version / "Recipes/example/handler"
        (output / "17").mkdir(parents=True)
        (output / "17/0.json").write_text('{"recipes":[]}', "utf-8")
        (output / "2.json").write_text('{"recipes":[]}', "utf-8")
        (output / "manifest.json").write_text('{}', "utf-8")
        routing_manifest = self.version / "Recipes/routing-manifest.json"
        routing_manifest.write_text('{"handlers":{"example/handler":{}}}', "utf-8")
        auxiliary = self.version / "Recipes/notes.json"
        auxiliary.write_text('{"note":"keep"}', "utf-8")
        split_all.do_recipes(False)
        self.assertFalse(routing_manifest.exists())
        self.assertEqual(auxiliary.read_text("utf-8"), '{"note":"keep"}')
        self.assertEqual(sorted(path.relative_to(output).as_posix() for path in output.rglob("*.json")),
                         ["0.json", "manifest.json"])
        self.assertEqual(json.loads((output / "0.json").read_bytes())["recipes"][0]["index"], "0/0")

    def test_ore_table_and_flat_dictionary_check_final_bytes(self):
        rows = [["例😀", "name", "ore", "example:ore", "icon.png", "O"]]
        split_all._write_ores_table_json(rows)
        table = self.version / "Ores/0.tabx"
        self.assertEqual(json.loads(table.read_bytes())["data"], rows)
        previous = table.read_bytes()
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all._write_ores_table_json([["中" * 700000]])
        self.assertEqual(table.read_bytes(), previous)
        output = self.version / "OreDictionary"
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all._split_flat_dict_impl({"example": "x" * HARD_MAX_BYTES}, str(output), False)
        self.assertFalse((output / "0.json").exists())

    def test_items_modes_keep_one_bucket_copy_and_decode_nested_columns(self):
        items = [
            {"key": "example:a", "shortKey": "example:a", "translatedName": "甲", "nbt": "{}",
             "extra": {"list": ["中文", 2]}, "index": "source-index"},
            {"key": "example:b", "shortKey": "example:b", "translatedName": "乙", "nbt": "{}",
             "extra": "literal string", "index": None},
        ]
        (self.source / "advitempanel.json").write_bytes(encode_json({"shortKey": items}))
        (self.source / "itemicon.json").write_bytes(encode_json({"shortKey": [
            {"shortKey": row["shortKey"], "iconName": row["key"].replace(":", "_") + ".png"}
            for row in items]}))
        (self.source / "fluid.json").write_bytes(encode_json({"fluids": []}))
        (self.version / "Items/0.json").write_text('{"items":[]}', "utf-8")
        with patch.object(split_all, "_inject_custom_items"), \
             patch.object(split_all, "image_name_config", return_value=self.state / "choices.json"):
            split_all.do_items(False)
            json_items = sorted(read_items(self.version / "Items"), key=lambda row: row["key"])
            self.assertEqual(json_items[0]["index"], "source-index")
            self.assertIsNone(json_items[1]["index"])
            self.assertFalse(list((self.version / "Items").glob("*.json")))
            split_all.do_items_tabx(False)
            self.assertFalse(list((self.version / "Items").rglob("*.json")))
            tables = list((self.version / "Items").rglob("*.tabx"))
            self.assertTrue(tables)
            for table in tables:
                data = json.loads(table.read_bytes())
                self.assertTrue(any(field.get("jsonEncoded") for field in data["schema"]["fields"]))
                self.assertLessEqual(table.stat().st_size, HARD_MAX_BYTES)
            self.assertEqual(sorted(read_items(self.version / "Items"), key=lambda row: row["key"]), json_items)
            split_all.do_items(False)
            self.assertFalse(list((self.version / "Items").rglob("*.tabx")))
            for path in (self.version / "Items").rglob("*.json"):
                for row in json.loads(path.read_bytes())["items"]:
                    self.assertEqual(path.parent.name, bucket_id(row["key"], json.loads(path.read_bytes())["bucketCount"]))
            self.assertFalse((self.version / "ItemBuckets").exists())

    def test_oversized_tabx_row_does_not_remove_previous_json(self):
        old = self.version / "Items/0.json"
        original = encode_json({"items": [{"key": "example:previous"}]})
        old.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "超过分页上限"):
            split_all.write_item_tables([{"key": "example:huge", "nested": {"value": "中" * 700000}}])
        self.assertEqual(old.read_bytes(), original)
        self.assertFalse(list((self.version / "Items").rglob("*.tabx")))

    def test_tabx_items_choose_one_bucket_and_remove_three_digit_stale_pages(self):
        key = next(f'mod:item{i}' for i in range(5000) if bucket_id(f'mod:item{i}', 128) == '100')
        stale = self.version / 'Items/127/0.tabx'
        stale.parent.mkdir()
        stale.write_bytes(b'{}')
        split_all.write_item_tables([{'key': key, 'translatedName': '测试'}])
        page = self.version / 'Items/00/0.tabx'
        data = json.loads(page.read_bytes())
        self.assertEqual(data['bucketCount'], 1)
        self.assertEqual(data['hashAlgorithm'], 'sha256-xor128-base62-15')
        self.assertFalse(stale.exists())
        self.assertEqual(list(read_items(self.version / 'Items')), [{'key': key, 'translatedName': '测试'}])

    def test_nbt_association_reads_nested_tabx_bucket(self):
        items = [{"key": "example:bee_hash", "rawKey": "Forestry:beeDroneGE", "nbt": "{v:1}",
                  "beeIdentity": {"speciesUid": "example.species", "type": "DRONE"}}]
        split_all.write_item_tables(items)
        output = self.version / "Recipes/example/handler"
        output.mkdir(parents=True)
        page = output / "0.json"
        page.write_bytes(encode_json({"recipes": [{"index": "0/0", "outputItems": [
            {"key": "Forestry:beeDroneGE", "nbt": "{v:1}", "beeIdentity": items[0]["beeIdentity"]}]}]}))
        source = self.source / "recipes/example"
        source.mkdir(parents=True)
        (source / "handler.json").write_bytes(page.read_bytes())
        split_all.do_recipes(False)
        result = json.loads(page.read_bytes())["recipes"][0]["outputItems"][0]
        self.assertEqual(result["key"], "example:bee_hash")
        self.assertEqual(result["queryKey"], "bee:DRONE:example.species")
        self.assertNotIn("nbt", result)


if __name__ == "__main__":
    unittest.main()
