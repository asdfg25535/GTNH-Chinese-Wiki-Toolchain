import copy
import unittest

from recipe_roles import normalize_recipe_roles


class RecipeRoleTests(unittest.TestCase):
    def test_fuel_candidates_preserve_nbt_order_and_are_moved_once(self):
        fuel = [{'key': 'mod:charged', 'nbt': '{charge:1}', 'amount': 2}, {'key': 'mod:fuel'}]
        recipe = {'inputItems': [{'key': 'mod:ore'}], 'otherItems': [fuel]}
        metadata = {'source': 'codechicken.nei.recipe.FurnaceRecipeHandler'}
        normalize_recipe_roles(recipe, metadata)
        expected = copy.deepcopy(recipe)
        normalize_recipe_roles(recipe, metadata)
        self.assertEqual(expected, recipe)
        self.assertEqual(recipe['inputItems'][1], fuel)
        self.assertNotIn('otherItems', recipe)

    def test_gene_extractor_keeps_consumption_flags(self):
        recipe = {'inputItems': [{'key': 'seed'}, {'key': 'circuit', 'nc': True}],
                  'otherItems': [{'key': 'stick', 'amount': 1}]}
        normalize_recipe_roles(recipe, {'identifier': 'cropsnh.recipes.cropGeneExtractor'})
        self.assertTrue(recipe['inputItems'][1]['nc'])
        self.assertEqual(recipe['inputItems'][2], {'key': 'stick', 'amount': 1})

    def test_research_station_keeps_written_result_as_auxiliary_display(self):
        recipe = {'inputItems': ['gregtech:gt.blockmachines:15440'],
                  'outputItems': ['gregtech:gt.blockmachines:15445'],
                  'otherItems': [{'key': 'gregtech:gt.metaitem.01:32708',
                                  'nbt': '{display:{Name:"Written research result"},output:{Damage:15445s}}'}]}
        before = copy.deepcopy(recipe)
        normalize_recipe_roles(recipe, {'identifier': 'gt.recipe.researchStation'})
        self.assertEqual(before, recipe)

    def test_oven_keeps_optional_fuel_slot_and_byproduct(self):
        for fuel in ([], [{'key': 'fuel'}]):
            recipe = {'inputItems': [{'key': 'wood'}], 'outputItems': [{'key': 'ash'}],
                      'otherItems': fuel + [{'key': 'vapour', 'chance': 3000}, {'key': 'jar'}]}
            normalize_recipe_roles(recipe, {'source': 'alkalus.main.nei.NEI_Handler_Oven'})
            self.assertEqual(recipe['inputItems'], [{'key': 'wood'}, fuel[0] if fuel else None, {'key': 'jar'}])
            self.assertEqual(recipe['outputItems'], [{'key': 'ash'}, {'key': 'vapour', 'chance': 3000}])

    def test_byproduct_is_output_before_bucketing(self):
        recipe = {'outputItems': [{'key': 'plank'}], 'otherItems': [{'key': 'dust', 'chance': 1000}]}
        normalize_recipe_roles(recipe, {'identifier': 'extratrees.lumbermill'})
        self.assertEqual(recipe['outputItems'][1], {'key': 'dust', 'chance': 1000})
        self.assertNotIn('otherItems', recipe)

    def test_display_collections_and_catalyst_conditions_keep_their_roles(self):
        for metadata in ({'identifier': 'botania.manaPool'},
                         {'source': 'team.chisel.compat.nei.RecipeHandlerChisel'},
                         {'source': 'flaxbeard.thaumicexploration.integration.nei.ReplicatorHandler'},
                         {'identifier': 'unknown'}):
            recipe = {'inputItems': [{'key': 'item'}], 'otherItems': [{'key': 'display'}]}
            before = copy.deepcopy(recipe)
            normalize_recipe_roles(recipe, metadata)
            self.assertEqual(before, recipe)


if __name__ == '__main__':
    unittest.main()
