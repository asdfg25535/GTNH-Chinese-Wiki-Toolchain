import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

from image_state import init_db, pending_redirects, update_state
from item_names import build_artifacts, collect_names, default_image_candidate, generate_config, generate_preferred_config, load_choices, resolve_images, write_name_index
from image_version import redirect_map
from item_buckets import write_item_buckets
from recipe_buckets import build_recipe_buckets, recipe_route, SUPPORTED_HANDLERS
from routing_hash import bucket_id, short_hash


class NameRoutingTests(unittest.TestCase):
    def test_image_mod_priority_and_variant_preferences(self):
        keys = ['minecraft:stone', 'fluid.stone', 'gregtech:stone', 'IC2:stone', 'other:stone']
        for start, expected in enumerate(keys):
            rows = [{'key': key} for key in keys[start:]]
            for ordered in (rows, list(reversed(rows))):
                self.assertEqual(expected, default_image_candidate(ordered)[0])

        rows = [{'key': 'IC2:cell:9', 'rawKey': 'IC2:cell:9'},
                {'key': 'IC2:cell_hash', 'rawKey': 'IC2:cell', 'nbt': '{a:1}'},
                {'key': 'IC2:cell', 'rawKey': 'IC2:cell'},
                {'key': 'other:cell'}]
        self.assertEqual('IC2:cell', default_image_candidate(rows)[0])
        self.assertIsNone(default_image_candidate([{'key': 'other:a'}, {'key': 'other:b'}])[0])

    def test_deprecated_candidates_yield_to_active_items_and_manual_choices(self):
        for marker in ('已弃用', '即将移除', '§cDEPRECATED'):
            rows = [{'key': 'minecraft:old', 'translatedName': '同名', 'iconName': 'old.png',
                     'tooltips': marker},
                    {'key': 'other:current', 'translatedName': '同名', 'iconName': 'current.png'}]
            groups = collect_names(rows)[1]
            self.assertEqual({'File:同名.png': 'File:current.png'}, resolve_images(groups, {}, items=rows)[0])
            self.assertEqual({'File:同名.png': 'File:old.png'},
                             resolve_images(groups, {'同名.png': {'key': 'minecraft:old'}}, items=rows)[0])
            self.assertEqual('minecraft:old', default_image_candidate(rows[:1])[0])

    def test_empty_nbt_and_chargeable_alias_do_not_overwrite_existing_key(self):
        import split_all
        items = [{"key": "mod:item", "rawKey": "mod:item"},
                 {"key": "mod:item_gLuB0gegBqh1aRe", "rawKey": "mod:item", "nbt": "{}"}]
        split_all._regress_nbt_only_items(items)
        self.assertEqual("mod:item_gLuB0gegBqh1aRe", items[1]['key'])
        tools = [{"key": "IC2:itemArmorBatpack:1"}, {"key": "IC2:itemArmorBatpack:26"},
                 {"key": "IC2:itemArmorBatpack"}]
        split_all._fix_ic2_chargeable_tools(tools)
        self.assertEqual(3, len(tools))

    def test_non_contiguous_nbt_variants_are_not_regressed(self):
        import split_all
        items = [{"key": "mod:item_hash1", "rawKey": "mod:item"},
                 {"key": "other:item", "rawKey": "other:item"},
                 {"key": "mod:item_hash2", "rawKey": "mod:item"}]
        with patch.object(split_all, '_load_nbt_regress_cache', return_value=({"mod:item_hash1", "mod:item_hash2"}, set())):
            split_all._regress_nbt_only_items(items)
        self.assertEqual(["mod:item_hash1", "other:item", "mod:item_hash2"], [item['key'] for item in items])

    def test_item_key_buckets_preserve_entire_records_and_duplicates(self):
        items = [{"key": "mod:item_hash", "translatedName": "重名", "index": "1/3", "nbt": "{a:1}"},
                 {"key": "other:item", "translatedName": "重名", "tooltips": "完整说明"}]
        items.append(dict(items[0]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_item_buckets(items, root)
            actual = []
            for path in root.rglob('*.json'):
                rows = json.loads(path.read_text('utf-8'))['items']
                self.assertTrue(all(bucket_id(row['key'], json.loads(path.read_bytes())['bucketCount']) == path.parent.name for row in rows))
                actual.extend(rows)
            self.assertCountEqual(items, actual)
            write_item_buckets([], root)
            self.assertFalse(list(root.rglob('*.json')))

    def test_icon_prefix_lookup_preserves_original_first_match(self):
        from split_all import _merge_item_icons
        items = [{"shortKey": "mod:item_new"}, {"shortKey": "mod:exact"}]
        icons = {"mod:item_old": {"iconName": "first.png"},
                 "mod:item_other": {"iconName": "second.png"},
                 "mod:exact": {"iconName": "exact.png"}}
        _merge_item_icons(items, icons)
        self.assertEqual(["first.png", "exact.png"], [item['iconName'] for item in items])

    def test_large_name_list_spans_pages_and_old_pages_are_removed(self):
        keys = [f"example:item_{number:06d}" for number in range(1000)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertGreater(write_name_index({"重名": keys}, root, max_bytes=2048), 1)
            found = []
            for path in root.rglob('*.json'):
                self.assertLessEqual(path.stat().st_size, 2048)
                self.assertEqual(bucket_id("重名", json.loads(path.read_bytes())["bucketCount"]), path.parent.name)
                found.extend(json.loads(path.read_text('utf-8'))['names']['重名']['keys'])
            self.assertEqual(keys, sorted(found))
            write_name_index({"重名": keys[:1]}, root, max_bytes=2048)
            self.assertEqual(1, len(list(root.rglob('*.json'))))

    def test_name_records_merge_all_fields_across_pages(self):
        keys = [f"Forestry:beePrincessGE_variant{i:03d}" for i in range(30)]
        name = "$森林.公主蜂"
        semantic = {key: "bee:PRINCESS:forestry.speciesForest" for key in keys}
        raw = "Forestry:beePrincessGE"
        handlers = {"sourceHandlers": {name: ["forestry/bees", "minecraft/crafting"]},
                    "usageHandlers": {name: ["forestry/breeding", ["minecraft/crafting", [0, 3, 15]]]}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertGreater(write_name_index({name: keys}, root, 220, query_keys=semantic,
                               raw_keys={key: raw for key in keys}, handlers=handlers), 1)
            merged = {"keys": [], "rawKeys": [], "queryKeys": {}, "sourceHandlers": [], "usageHandlers": []}
            for path in sorted(root.rglob('*.json'), key=lambda p: int(p.stem)):
                self.assertLessEqual(path.stat().st_size, 220)
                page = json.loads(path.read_bytes())
                self.assertEqual(set(page), {'bucketCount', 'names'})
                for field, value in page['names'][name].items():
                    if field == 'queryKeys':
                        merged[field].update(value)
                    else:
                        merged[field].extend(value)
            self.assertEqual(merged, {'keys': keys, 'rawKeys': [raw], 'queryKeys': semantic,
                                     **{field: values[name] for field, values in handlers.items()}})

    def test_build_artifacts_groups_query_identities_under_name(self):
        item = {'key': 'Forestry:beePrincessGE_variant', 'translatedName': '森林公主蜂',
                'rawKey': 'Forestry:beePrincessGE', 'queryKey': 'bee:PRINCESS:forest',
                'nbt': '{Genome:{...}}', 'tooltips': '完整物品说明'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build_artifacts([item], root / 'version', root / 'state', root,
                            root / 'choices.json', write_items=False)
            page = json.loads(next((root / 'version/ItemNames').rglob('*.json')).read_bytes())
            self.assertEqual(page, {'bucketCount': 1, 'names': {'森林公主蜂': {
                'keys': [item['key']], 'rawKeys': [item['rawKey']],
                'queryKeys': {item['key']: item['queryKey']}}}})

    def test_plain_item_omits_redundant_raw_and_empty_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_name_index({'石头': ['minecraft:stone']}, root,
                             raw_keys={'minecraft:stone': 'minecraft:stone'},
                             query_keys={'minecraft:stone': '', 'unused': 'identity'})
            page = json.loads(next(root.rglob('*.json')).read_bytes())
            self.assertEqual(page, {'bucketCount': 1, 'names': {'石头': {'keys': ['minecraft:stone']}}})

    def test_index_exact_byte_limit_and_oversized_field(self):
        from bounded_json import encode_json, HARD_MAX_BYTES
        key, name, semantic = 'Forestry:beePrincessGE_variant', '森林公主蜂', 'bee:PRINCESS:forest'
        page = {'bucketCount': 1, 'names': {name: {'keys': [key], 'queryKeys': {key: semantic}}}}
        exact = len(encode_json(page))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(write_name_index({name: [key]}, root, exact, query_keys={key: semantic}), 1)
            self.assertEqual(next(root.rglob('*.json')).stat().st_size, exact)
            self.assertEqual(write_name_index({name: [key]}, root, exact - 1, query_keys={key: semantic}), 2)
            with self.assertRaisesRegex(ValueError, '单条名称'):
                write_name_index({name: [key]}, root, HARD_MAX_BYTES, query_keys={key: '中' * 700000})

    def test_handlers_include_ores_semantics_raw_outputs_and_other_items(self):
        from name_recipes import collect_handlers
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                'OreDictionary/0.json': {'oreA': ['mod:a'], 'oreWildcard': ['mod:block:32767']},
                'Recipes/mod/craft.json': {'recipes': [
                    {'inputItems': [['#oreA']], 'outputItems': [{'key': 'mod:variant', 'rawKey': 'mod:base'}]},
                    {'inputItems': ['semantic:a'], 'otherItems': ['mod:other']},
                    {'inputItems': ['#oreWildcard']},
                ]},
                'Recipes/mod/second/00/0.json': {'recipes': [{'outputItems': ['mod:a']}]},
                'Recipes/routing-manifest.json': {'handlers': {}},
            }
            for relative, content in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(content), 'utf-8')
            result = collect_handlers({'甲': ['mod:a'], '乙': ['mod:known'], '丙': ['mod:other'],
                                       '方块': ['mod:block:2']},
                                      {'mod:known': 'mod:base'}, {'mod:a': 'semantic:a'}, root)
            self.assertEqual(result['sourceHandlers'], {'甲': ['mod/second'], '乙': ['mod/craft'], '丙': ['mod/craft']})
            self.assertEqual(result['usageHandlers'], {'甲': ['mod/craft'], '丙': ['mod/craft'], '方块': ['mod/craft']})

    def test_forestry_centrifuge_products_are_sources_without_usage_handlers(self):
        from name_recipes import collect_handlers
        from recipe_roles import normalize_recipe_roles

        comb = 'gregtech:gt.comb:49'
        dust = 'gregtech:gt.metaitem.01:2849'
        recipe = {'inputItems': [comb], 'outputItems': [], 'otherItems': [
            'Forestry:beeswax', {'key': dust, 'chance': 5000, 'amount': 2},
        ]}
        normalize_recipe_roles(recipe, {'identifier': 'forestry.centrifuge'})

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = 'Forestry/NEIHandlerCentrifuge_centrifuge'
            path = root / 'Recipes' / handler / '0.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'recipes': [recipe]}), 'utf-8')
            result = collect_handlers({'碎石蜂窝': [comb], '黑花岗岩粉': [dust]}, {}, {}, root)

        self.assertEqual(result['sourceHandlers'], {'黑花岗岩粉': [handler]})
        self.assertEqual(result['usageHandlers'], {'碎石蜂窝': [handler]})
        self.assertEqual(recipe['outputItems'], [
            'Forestry:beeswax', {'key': dust, 'chance': 5000, 'amount': 2},
        ])

    def test_recipe_task_refreshes_name_handlers_after_writing_recipes(self):
        import split_all
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_item_buckets([{'key': 'mod:item', 'translatedName': '物品'}], root / 'Items')
            write_name_index({'物品': ['mod:item']}, root / 'ItemNames')
            state = root / 'state'
            state.mkdir()
            (state / 'image-name-resolution.json').write_text('{"indexFileCount":99}', 'utf8')

            def write_recipes(debug):
                path = root / 'Recipes/mod/crafting/0.json'
                path.parent.mkdir(parents=True)
                path.write_text('{"recipes":[{"outputItems":["mod:item"]}]}', 'utf-8')

            with patch.object(split_all, 'VERSION', str(root)), \
                 patch.object(split_all, 'VERSION_TAG', 'GTNotLeisure/0.2.7-rc2'), \
                 patch.object(split_all, '_PROJECT_ROOT', str(root)), \
                 patch.object(split_all, 'AUX_DIR', str(state)), \
                 patch.object(split_all, 'configure_source'), \
                 patch.dict(split_all.TASKS, {'recipes': ('Recipes', write_recipes)}), \
                 patch('sys.argv', ['split_all.py', 'recipes']):
                split_all.main()
            record = json.loads(next((root / 'ItemNames').rglob('*.json')).read_bytes())['names']['物品']
            self.assertEqual(record['sourceHandlers'], ['mod/crafting'])
            self.assertEqual(json.loads((state / 'image-name-resolution.json').read_bytes())['indexFileCount'], 1)
            config = root / 'wikitext/Module/Database/Versions/GTNotLeisure/0.2.7-rc2.lua'
            self.assertIn('itemNameBuckets = 1', config.read_text('utf8'))
            self.assertFalse((state / 'version-config.lua').exists())

    def test_input_bucket_entries_merge_ore_semantic_names_and_keep_shared(self):
        from name_recipes import collect_handlers
        shaped = 'minecraft/ShapedRecipeHandler_crafting'
        hammer = 'gregtech/GTNEIDefaultHandler_hammer'
        extruding = 'gregtech/GTNEIDefaultHandler_tic_part_extruding'
        assembler = 'gregtech/GTNEIDefaultHandler_assembler'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                'OreDictionary/0.json': {'oreA': ['mod:a'], 'tool': ['mod:tool:32767']},
                f'Recipes/{shaped}/00/0.json': {'routing': {'bucketCount': 16}, 'recipes': [
                    {'inputItems': [['#oreA', '#tool']], 'outputItems': ['mod:a']}]},
                f'Recipes/{shaped}/00/1.json': {'routing': {'bucketCount': 16}, 'recipes': [
                    {'inputItems': [{'key': 'variant', 'queryKey': 'semantic:b'}]}]},
                f'Recipes/{shaped}/03/0.json': {'routing': {'bucketCount': 16}, 'recipes': [
                    {'inputItems': ['semantic:b']}]},
                f'Recipes/{hammer}/shared/0.json': {'routing': {'bucketCount': 2}, 'recipes': [
                    {'inputItems': ['mod:shared']}]},
                f'Recipes/{extruding}/01/0.json': {'routing': {'bucketCount': 2}, 'recipes': [
                    {'inputItems': ['mod:a']}]},
                f'Recipes/{assembler}/0.json': {'recipes': [{'inputItems': ['mod:a']}]},
            }
            for relative, data in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), 'utf8')
            handlers = collect_handlers({'同名': ['mod:a', 'mod:b'], '工具': ['mod:tool:2'],
                                         '副产物配方输入': ['mod:shared']}, {}, {'mod:b': 'semantic:b'}, root)
            self.assertEqual(handlers['usageHandlers']['同名'],
                             [assembler, extruding, [shaped, [0, 3]]])
            self.assertEqual(handlers['usageHandlers']['工具'], [[shaped, [0]]])
            self.assertEqual(handlers['usageHandlers']['副产物配方输入'], [[hammer, []]])
            self.assertEqual(handlers['sourceHandlers']['同名'], [shaped])

    def test_input_file_numbers_follow_final_pages_and_omit_full_coverage(self):
        from name_recipes import collect_handlers
        macerator = 'gregtech/GTNEIDefaultHandler_macerator'
        assembler = 'gregtech/GTNEIDefaultHandler_assembler'
        integration = 'BuildCraft_Silicon/RecipeHandlerIntegrationTable_null'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = {
                'OreDictionary/0.json': {'oreA': ['mod:a'], 'tool': ['mod:tool:32767']},
                f'Recipes/{macerator}/0.json': {'recipes': [
                    {'inputItems': ['#oreA', '#tool', 'mod:common'], 'outputItems': ['mod:a']}]},
                f'Recipes/{macerator}/3.json': {'recipes': [
                    {'inputItems': ['mod:c', 'mod:common'], 'inputFluids': ['fluid:a'],
                     'otherItems': ['mod:aux'], 'keyItem': 'mod:key'}]},
                f'Recipes/{macerator}/12.json': {'recipes': [
                    {'inputItems': [{'key': 'variant', 'queryKey': 'semantic:b'}, 'mod:common']}]},
                f'Recipes/{assembler}/0.json': {'recipes': [{'inputItems': ['mod:a']}]},
                f'Recipes/{integration}/0.json': {'recipes': [{'inputItems': ['mod:a']}]},
                f'Recipes/{integration}/1.json': {'recipes': [{'inputItems': ['mod:c']}]},
            }
            for relative, data in fixtures.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), 'utf8')
            names = {'同名': ['mod:a', 'mod:b'], '工具': ['mod:tool:2'], '其他': ['mod:c'],
                     '全文件': ['mod:common'], '流体': ['fluid:a'], '辅助': ['mod:aux'], '关键': ['mod:key']}
            result = collect_handlers(names, {}, {'mod:b': 'semantic:b'}, root)
            self.assertEqual(result['usageHandlers']['同名'], [integration, assembler, [macerator, [0, 12]]])
            self.assertEqual(result['usageHandlers']['全文件'], [macerator])
            self.assertEqual(result['usageHandlers']['工具'], [[macerator, [0]]])
            for name in ('流体', '辅助', '关键'):
                self.assertEqual(result['usageHandlers'][name], [[macerator, [3]]])
            self.assertEqual(result['sourceHandlers']['同名'], [macerator])
            (root / f'Recipes/{macerator}/0.json').rename(root / f'Recipes/{macerator}/7.json')
            refreshed = collect_handlers(names, {}, {'mod:b': 'semantic:b'}, root)
            self.assertEqual(refreshed['usageHandlers']['同名'], [integration, assembler, [macerator, [7, 12]]])

    def test_existing_nbt_hash_and_unicode_bucket(self):
        self.assertEqual("gLuB0gegBqh1aRe", short_hash("{}"))
        self.assertEqual("OoSyzczEpOjdDBZ", short_hash("铁锭"))
        self.assertEqual("z0uV1u1ydGyUeKg", short_hash("minecraft:stone"))
        self.assertEqual("UA3EBiE1GzVnxP3", short_hash('{display:{Name:"中文"}}'))
        import hashlib
        for name in ("铁锭", "minecraft:stone", "A_B", "带有 NBT 的物品🧪"):
            digest = hashlib.sha256(name.encode("utf-8")).digest()
            self.assertEqual((digest[15] ^ digest[31]) % 64, int(bucket_id(name, 64)))

    def test_choices_do_not_discard_name_candidates(self):
        items = [{"key": "a:one", "translatedName": "铁锭", "iconName": "icon_a.png"},
                 {"key": "b:two_hash", "translatedName": "铁锭", "iconName": "icon_b.png"}]
        names, groups = collect_names(items)
        self.assertEqual(["a:one", "b:two_hash"], names["铁锭"])
        redirects, unresolved, invalid = resolve_images(groups, {})
        self.assertFalse(redirects)
        self.assertIn("铁锭.png", unresolved)
        choices = {"铁锭.png": {"key": "b:two_hash"}}
        redirects, unresolved, invalid = resolve_images(groups, choices)
        self.assertEqual({"File:铁锭.png": "File:icon_b.png"}, redirects)
        self.assertFalse(unresolved or invalid)
        self.assertEqual(collect_names(items), collect_names(list(reversed(items))))
        redirects, _, invalid = resolve_images(groups, choices, ["铁锭.png"])
        self.assertFalse(redirects)
        self.assertIn("铁锭.png", invalid)

    def test_candidate_report_never_changes_choices(self):
        items = [{'key': 'gregtech:ingot', 'translatedName': '铜锭', 'iconName': 'gt.png'},
                 {'key': 'other:ingot', 'translatedName': '铜锭', 'iconName': 'other.png'}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, report = root / 'choices.json', root / 'report.json'
            config.write_text(json.dumps({'schemaVersion': 2, 'choices': {
                '铜锭.png': {'key': None, 'reason': 'suppress'}}}), 'utf8')
            original = config.read_bytes()
            result = generate_config(items, report, config)
            self.assertEqual(result, generate_config(items, report, config))
            self.assertEqual(result['candidates']['铜锭.png']['defaultKey'], 'gregtech:ingot')
            self.assertIsNone(result['candidates']['铜锭.png']['choice']['key'])
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(list(root.glob('*.bak')))
            with self.assertRaises(ValueError):
                generate_config(items, config, config)

    def test_preferred_choices_override_defaults_and_null_suppresses(self):
        items = [{'key': 'gregtech:ingot', 'translatedName': '铜锭', 'iconName': 'gt.png'},
                 {'key': 'other:ingot', 'translatedName': '铜锭', 'iconName': 'other.png'}]
        groups = collect_names(items)[1]
        self.assertEqual(resolve_images(groups, {}, items=items)[0], {'File:铜锭.png': 'File:gt.png'})
        self.assertEqual(resolve_images(groups, {'铜锭.png': {'key': 'other:ingot'}}, items=items)[0],
                         {'File:铜锭.png': 'File:other.png'})
        redirects, _, invalid = resolve_images(groups, {'铜锭.png': {'key': None}}, items=items)
        self.assertFalse(redirects or invalid)
        redirects, _, invalid = resolve_images(groups, {'铜锭.png': {'key': 'missing'}}, items=items)
        self.assertFalse(redirects)
        self.assertIn('铜锭.png', invalid)

    def test_generated_preferences_use_dumper_order_after_existing_rules(self):
        items = [
            {'key': 'minecraft:old', 'translatedName': '冲突', 'iconName': 'old.png', 'tooltips': '已弃用'},
            {'key': 'gregtech:item:2_nbt', 'rawKey': 'gregtech:item:2', 'nbt': '{a:1}',
             'translatedName': '冲突', 'iconName': 'nbt.png'},
            {'key': 'other:early', 'translatedName': '冲突', 'iconName': 'early.png'},
            {'key': 'gregtech:item:1', 'translatedName': '冲突', 'iconName': 'one.png'},
            {'key': 'gregtech:item:2', 'translatedName': '冲突', 'iconName': 'two.png'},
            {'key': 'gregtech:default', 'translatedName': '默认', 'iconName': 'default.png'},
            {'key': 'other:default', 'translatedName': '默认', 'iconName': 'other.png'},
            {'key': 'other:manual_a', 'translatedName': '手工', 'iconName': 'manual_a.png'},
            {'key': 'other:manual_b', 'translatedName': '手工', 'iconName': 'manual_b.png'},
            {'key': 'other:skip_a', 'translatedName': '跳过', 'iconName': 'skip_a.png'},
            {'key': 'other:skip_b', 'translatedName': '跳过', 'iconName': 'skip_b.png'},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, output, icon_map = root / 'config.json', root / 'output.json', root / 'itemicon.json'
            original = {'schemaVersion': 2, 'choices': {
                '手工.png': {'key': 'other:manual_b', 'reason': '人工确认'},
                '跳过.png': {'key': None, 'reason': '不发布'},
            }}
            config.write_text(json.dumps(original), 'utf-8')
            before = config.read_bytes()
            icon_map.write_text(json.dumps({'shortKey': [{'iconName': icon} for icon in
                ['old.png', 'nbt.png', 'early.png', 'two.png', 'one.png', 'two.png']]}), 'utf-8')
            result = generate_preferred_config(items, output, config, icon_map)
            self.assertEqual(result, generate_preferred_config(list(reversed(items)), output, config, icon_map))
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual(json.loads(output.read_text('utf-8')), result)
            self.assertEqual(set(result['choices']), {'冲突.png', '手工.png', '跳过.png'})
            for title, choice in original['choices'].items():
                self.assertEqual(result['choices'][title], choice)
            self.assertEqual(result['choices']['冲突.png'], {'key': 'gregtech:item:2'})
            groups = collect_names(items)[1]
            self.assertIn('冲突.png', resolve_images(groups, original['choices'], items=items)[1])
            redirects, unresolved, invalid = resolve_images(groups, result['choices'], items=items)
            self.assertFalse(unresolved or invalid)
            self.assertEqual(redirects['File:冲突.png'], 'File:two.png')
            self.assertEqual(redirects['File:默认.png'], 'File:default.png')
            with self.assertRaisesRegex(ValueError, '不能覆盖'):
                generate_preferred_config(items, config, config, icon_map)

    def test_generated_preferences_match_transformed_keys_by_icon_and_require_complete_mapping(self):
        items = [{'key': 'fluid.z', 'translatedName': '同名_流体', 'iconName': 'first.png'},
                 {'key': 'fluid.a', 'translatedName': '同名 流体', 'iconName': 'second.png'}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, output, icon_map = root / 'config.json', root / 'output.json', root / 'itemicon.json'
            icon_map.write_text(json.dumps({'shortKey': [
                {'shortKey': 'gregtech:display:42', 'iconName': 'first.png'}]}), 'utf-8')
            with self.assertRaisesRegex(ValueError, '缺少候选'):
                generate_preferred_config(items, output, config, icon_map)
            self.assertFalse(output.exists())
            icon_map.write_text(json.dumps({'shortKey': [
                {'shortKey': 'gregtech:display:42', 'iconName': 'first.png'},
                {'shortKey': 'gregtech:display:19', 'iconName': 'second.png'}]}), 'utf-8')
            result = generate_preferred_config(items, output, config, icon_map)
            self.assertEqual(result['choices']['同名 流体.png']['key'], 'fluid.z')

    def test_gravitational_lens_preference_survives_addon_only_exports(self):
        base = {'key': 'gregtech:gt.metaitem.03:32277', 'translatedName': '引力透镜',
                'iconName': 'icon_gregtech_gt.metaitem.03_32277.png'}
        addon = {'key': 'TwistSpaceTechnology:MetaItem01:18', 'translatedName': '引力透镜',
                 'iconName': 'icon_TwistSpaceTechnology_MetaItem01_18.png'}
        choices = load_choices(Path(__file__).parent / 'config/preferred-items.json')
        choice = {'引力透镜.png': choices['引力透镜.png']}
        for items in ([base], [base, addon]):
            names, groups = collect_names(items)
            redirects, unresolved, invalid = resolve_images(groups, choice, items=items)
            self.assertEqual(redirects, {'File:引力透镜.png': 'File:' + base['iconName']})
            self.assertFalse(unresolved or invalid)
            self.assertEqual(set(names['引力透镜']), {item['key'] for item in items})
        # 附属独立发布时保留本体的中文别名，附属物品仍使用自身图标身份。
        redirects, _, invalid = resolve_images(collect_names([addon])[1], choice, items=[addon])
        self.assertFalse(redirects)
        self.assertEqual(invalid['引力透镜.png']['key'], base['key'])

    def test_build_defaults_use_full_nbt_records(self):
        items = [{'key': 'mod:item_a', 'rawKey': 'mod:item', 'nbt': '{a:1}',
                  'translatedName': '物品', 'iconName': 'a.png'},
                 {'key': 'mod:item_z', 'rawKey': 'mod:item', 'nbt': '{}',
                  'translatedName': '物品', 'iconName': 'z.png'}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = build_artifacts(iter(items), root / 'version', root / 'state', root, root / 'choices.json')
            self.assertFalse(report['unresolved'])
            self.assertIn('File:z.png', (root / 'state/image_redirect.resolved.csv').read_text('utf8'))

    def test_public_default_and_custom_path(self):
        import split_all
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(split_all.image_name_config(), Path(split_all.__file__).parent / 'config/preferred-items.json')
        with patch.dict(os.environ, {'IMAGE_NAME_CONFIG': 'custom.json'}):
            self.assertEqual(split_all.image_name_config(), Path('custom.json'))
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'choices.json'
            for data in ({'schemaVersion': 1, 'choices': {}},
                         {'schemaVersion': 2, 'choices': {'a.png': {'selectedKey': 'a'}}}):
                config.write_text(json.dumps(data), 'utf8')
                with self.assertRaises(ValueError):
                    load_choices(config)

    def test_build_is_local_and_config_change_invalidates_redirects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "dumps"
            source.mkdir()
            (source / "advitempanel.json").write_text("fixture", "utf-8")
            config = root / "choices.json"
            items = [{"key": "example:item_hash", "translatedName": "名称", "iconName": "icon_test.png"}]
            report = build_artifacts(items, root / "version", root / "state", source, config)
            item_page = root / "version/Items" / bucket_id("example:item_hash", 1) / "0.json"
            self.assertEqual(json.loads(item_page.read_bytes())["items"], items)
            self.assertEqual(report["itemFileCount"], 1)
            self.assertFalse((root / "version/ItemBuckets").exists())
            self.assertFalse(config.exists(), "普通预处理不得自动生成或覆盖人工配置")
            index = root / "version/ItemNames" / bucket_id("名称", 1) / "0.json"
            self.assertEqual({"名称": {"keys": ["example:item_hash"]}}, json.loads(index.read_text("utf-8"))["names"])
            with patch.dict(os.environ, {"DUMPS_DIR": str(source), "MOD_LOCAL_NAME": "GTNH", "IMAGE_NAME_CONFIG": str(config)}):
                self.assertEqual({"File:名称.png": "icon_test.png"}, redirect_map(str(root / "state/image_redirect.resolved.csv")))
                config.write_text('{"schemaVersion":2,"choices":{}}', "utf-8")
                with self.assertRaisesRegex(ValueError, "配置已修改"):
                    redirect_map(str(root / "state/image_redirect.resolved.csv"))

    def test_switching_config_path_requires_regeneration_even_with_same_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'dumps'
            source.mkdir()
            (source / 'advitempanel.json').write_text('fixture', 'utf8')
            first, second = root / 'a.json', root / 'b.json'
            for config in (first, second):
                config.write_text('{"schemaVersion":2,"choices":{}}', 'utf8')
            items = [{'key': 'mod:item', 'translatedName': '物品', 'iconName': 'item.png'}]
            build_artifacts(items, root / 'version', root / 'state', source, first)
            resolved = str(root / 'state/image_redirect.resolved.csv')
            with patch.dict(os.environ, {'DUMPS_DIR': str(source), 'MOD_LOCAL_NAME': 'GTNH',
                                         'IMAGE_NAME_CONFIG': str(first)}):
                self.assertTrue(redirect_map(resolved))
                os.environ['IMAGE_NAME_CONFIG'] = str(second)
                with self.assertRaisesRegex(ValueError, '配置路径已切换'):
                    redirect_map(resolved)
                build_artifacts(items, root / 'version', root / 'state', source, second)
                self.assertTrue(redirect_map(resolved))

    def test_multiple_aliases_and_legacy_uploaded_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "images.db"
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("CREATE TABLE images(icon_name TEXT PRIMARY KEY,redirect_from TEXT,icon_state TEXT,redirect_state TEXT)")
                conn.execute("INSERT INTO images VALUES ('icon.png','File:旧名.png','已上传','已上传')")
            init_db(db)
            aliases = {"File:旧名.png": "icon.png", "File:新名.png": "icon.png"}
            update_state(db, {"icon.png"}, aliases)
            self.assertEqual([("File:新名.png", "icon.png")], pending_redirects(db, aliases))
            with closing(sqlite3.connect(db)) as conn:
                self.assertEqual(2, conn.execute("SELECT COUNT(*) FROM redirects").fetchone()[0])
                self.assertEqual("已上传", conn.execute("SELECT icon_state FROM images").fetchone()[0])

    def test_legacy_collision_requires_reapplying_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "images.db"
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("CREATE TABLE images(icon_name TEXT PRIMARY KEY,redirect_from TEXT,icon_state TEXT,redirect_state TEXT)")
                conn.executemany("INSERT INTO images VALUES (?, 'File:重名.png', '已上传', '已上传')", [("a.png",), ("b.png",)])
            update_state(db, {"a.png", "b.png"}, {"File:重名.png": "a.png"})
            self.assertEqual([("File:重名.png", "a.png")], pending_redirects(db, {"File:重名.png": "a.png"}))

    def test_recipe_route_groups_nbt_outputs_and_preserves_exact_records(self):
        recipe = {"index": "0/0", "inputItems": [{"key": "input"}],
                  "outputItems": [[{"key": "exact_hash1", "rawKey": "example:item:3", "nbt": "{state:1}"},
                                   {"key": "exact_hash2", "rawKey": "example:item:3", "queryKey": "bee:species"}]],
                  "otherItems": []}
        self.assertEqual(bucket_id("example:item:3", 64), recipe_route(recipe, 64))
        self.assertEqual(bucket_id("fluid.water", 64), recipe_route({"outputFluids": [{"key": "fluid.water"}]}, 64))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "Recipes/gregtech/GTNEIDefaultHandler_assembler/0.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({"type": "fixture", "recipes": [recipe]}), "utf-8")
            second = {**recipe, "index": "1/0"}
            source.with_name("1.json").write_text(json.dumps({"type": "fixture", "recipes": [second]}), "utf-8")
            report = build_recipe_buckets(root)
            self.assertEqual(2, report["writtenRecipes"])
            paths = list(source.parent.rglob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0].parent.name, bucket_id("example:item:3", 1))
            data = json.loads(paths[0].read_bytes())
            self.assertEqual(data["recipes"], [recipe, second])
            self.assertEqual(data["routing"]["identityFields"], ["rawKey", "key"])

    def test_ore_output_uses_its_literal_route_identity(self):
        for output in ('#ingotIron', {'key': '#ingotIron'},
                       {'key': '#ingotIron', 'rawKey': '#ingotIron'}):
            with self.subTest(output=output):
                self.assertEqual(recipe_route({'outputItems': [output]}, 16), bucket_id('#ingotIron', 16))

    def test_handler_bucket_counts_manifest_routes_and_records_agree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            originals = {}
            for handler, count in SUPPORTED_HANDLERS.items():
                recipes = [{"index": f"0/{i}", "outputItems": [{"key": f"mod:item:{i}", "nbt": "{中文:1}"}]} for i in range(80)]
                originals[handler] = recipes
                source = root / "Recipes" / handler
                source.mkdir(parents=True)
                for page in (0, 1):
                    (source / f"{page}.json").write_text(json.dumps({"recipes": recipes[page*40:(page+1)*40]}), "utf-8")
            report = build_recipe_buckets(root)
            self.assertNotIn("bucketCount", report)
            self.assertEqual(report["recipeBuckets"], {handler: 1 for handler in SUPPORTED_HANDLERS})
            self.assertEqual(json.loads((root / "Recipes/routing-manifest.json").read_bytes()), report)
            for handler, count in report["recipeBuckets"].items():
                self.assertEqual(report["handlers"][handler]["bucketCount"], count)
                actual = []
                for route, paths in report["handlers"][handler]["routes"].items():
                    self.assertLess(int(route), count)
                    for path in paths:
                        page = json.loads((root / "Recipes" / path).read_bytes())
                        self.assertEqual(page["routing"]["bucketCount"], count)
                        for recipe in page["recipes"]:
                            self.assertEqual(recipe_route(recipe, count), route)
                            self.assertEqual(bucket_id(recipe["outputItems"][0]["key"], count), route)
                        actual.extend(page["recipes"])
                self.assertEqual(sorted(actual, key=lambda r: int(r["index"].split("/")[1])), originals[handler])

    def test_multiple_output_raw_keys_preserve_flat_pool(self):
        recipe = {"index": "0/0", "outputItems": [[{"key": "a_nbt", "rawKey": "a"},
                                                  {"key": "b_nbt", "rawKey": "b"}]]}
        self.assertIsNone(recipe_route(recipe, 64))
        self.assertIsNone(recipe_route({"outputItems": [{"queryKey": "bee:species"}]}, 64))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            handler = "gregtech/GTNEIDefaultHandler_assembler"
            source = root / "Recipes" / handler
            source.mkdir(parents=True)
            original = json.dumps({"recipes": [recipe]}).encode()
            for page in (0, 1):
                (source / f"{page}.json").write_bytes(original)
            report = build_recipe_buckets(root)
            self.assertIn(handler, report["skipped"])
            self.assertEqual(report["writtenRecipes"], 0)
            self.assertTrue(all(path.read_bytes() == original for path in source.glob("*.json")))
            self.assertEqual(len(list(source.glob("*.json"))), 2)

    def test_only_selected_multiple_file_handlers_and_hammer_shared(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            single = {"outputItems": [{"key": "a"}]}
            multiple = {"outputItems": [{"key": "a"}, {"key": "b"}]}
            for handler, recipes in (("GTNEIDefaultHandler_hammer", [single, multiple]),
                                     ("GTNEIDefaultHandler_macerator", [single]),
                                     ("GTNEIDefaultHandler_assembler", [multiple])):
                for page in (0, 1):
                    path = root / "Recipes/gregtech" / handler / f"{page}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"recipes": recipes}), "utf-8")
            report = build_recipe_buckets(root)
            self.assertEqual(["gregtech/GTNEIDefaultHandler_hammer"], list(report["handlers"]))
            self.assertEqual(2, report["sharedRecipes"])
            self.assertEqual(report["recipeBuckets"], {"gregtech/GTNEIDefaultHandler_hammer": 1})
            shared = root / "Recipes/gregtech/GTNEIDefaultHandler_hammer/shared/0.json"
            self.assertEqual(2, len(json.loads(shared.read_text("utf-8"))["recipes"]))
            self.assertTrue((root / "Recipes/gregtech/GTNEIDefaultHandler_macerator/0.json").exists())
            self.assertTrue((root / "Recipes/gregtech/GTNEIDefaultHandler_assembler/0.json").exists())
            self.assertFalse((root / "Recipes/gregtech/GTNEIDefaultHandler_hammer/0.json").exists())
            self.assertFalse((root / "RecipeBuckets").exists())



class DefaultImageRulesTest(unittest.TestCase):
    def test_nbt_representative_is_stable_and_scoped(self):
        a = {'key': 'mod:item_a', 'rawKey': 'mod:item', 'nbt': '{a:1}'}
        b = {'key': 'mod:item_b', 'rawKey': 'mod:item', 'nbt': '{a:2}'}
        self.assertEqual(self.choose(b, a), a['key'])
        self.assertEqual(self.choose(a, b), a['key'])
        self.assertEqual(self.choose(a, b, {'key': 'mod:item'}), 'mod:item')
        self.assertIsNone(self.choose(a, {'key': 'mod:other_z', 'rawKey': 'mod:other', 'nbt': '{a:2}'}))

    def test_current_controller_and_one_slot_battery(self):
        a = {'key': 'gregtech:gt.blockmachines:1', 'tooltips': '§4已弃用，此控制器将移除'}
        b = {'key': 'gregtech:gt.blockmachines:2', 'tooltips': '新控制器'}
        self.assertEqual(self.choose(a, b), b['key'])
        a.update(translatedName='低压电池箱', tooltips='低压电池箱<br><br>16个槽位<br>容量')
        b.update(translatedName='低压电池箱', tooltips='低压电池箱<br><br>1个槽位<br>容量')
        self.assertEqual(self.choose(a, b), b['key'])
        b['tooltips'] = '4个槽位'
        self.assertIsNone(self.choose(a, b))

    def test_fluid_owner_is_not_display_item_mod(self):
        a = {'key': 'fluid.a', 'mod': 'GregTech', 'registeredBy': 'TConstruct'}
        b = {'key': 'fluid.b', 'mod': 'GregTech', 'registeredBy': 'gregtech'}
        self.assertEqual(self.choose(a, b), b['key'])
        del b['registeredBy']
        self.assertIsNone(self.choose(a, b))

    def choose(self, *items):
        from item_names import default_image_candidate
        return default_image_candidate(list(items))[0]

    def test_nbt_and_damage_are_scoped_to_same_item(self):
        self.assertEqual(self.choose({'key': 'a:x_empty', 'rawKey': 'a:x', 'nbt': '{}'},
                                     {'key': 'a:x_full', 'rawKey': 'a:x', 'nbt': '{a:1}'}), 'a:x_empty')
        self.assertEqual(self.choose({'key': 'a:x'}, {'key': 'a:x:3'},
                                     {'key': 'a:x_hash', 'rawKey': 'a:x', 'nbt': '{a:1}'}), 'a:x')
        self.assertIsNone(self.choose({'key': 'a:x'}, {'key': 'a:y:3'}))
        self.assertIsNone(self.choose({'key': 'a:x:2'}, {'key': 'a:x:3'}))

    def test_fluid_and_cross_mod_priority(self):
        self.assertEqual(self.choose({'key': 'fluid.water', 'nbt': '{display:1}'},
                                     {'key': 'gregtech:water'}, {'key': 'minecraft:water'}), 'minecraft:water')
        self.assertEqual(self.choose({'key': 'gregtech:ingot:7'}, {'key': 'other:ingot'}),
                         'gregtech:ingot:7')
        self.assertIsNone(self.choose({'key': 'fluid.a'}, {'key': 'fluid.b'}))

    def test_stone_ore_preserves_other_metadata_meanings(self):
        self.assertEqual(self.choose({'key': 'gregtech:gt.blockores2:35'},
                                     {'key': 'gregtech:gt.blockores2:1035'},
                                     {'key': 'gregtech:gt.blockores3:35'}), 'gregtech:gt.blockores2:35')
        self.assertIsNone(self.choose({'key': 'gregtech:gt.blockores2:35'},
                                     {'key': 'gregtech:gt.blockores2:8035'}))
        self.assertEqual(self.choose({'key': 'gregtech:gt.blockores2:16035'},
                                     {'key': 'gregtech:gt.blockores2:17035'}), 'gregtech:gt.blockores2:16035')


if __name__ == "__main__":
    unittest.main()
