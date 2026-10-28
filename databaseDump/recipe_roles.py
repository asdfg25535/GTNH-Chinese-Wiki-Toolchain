"""Classify NEI auxiliary stacks before recipe pages and output buckets are written."""

INPUT_SOURCES = {
    'codechicken.nei.recipe.FurnaceRecipeHandler',
    'codechicken.nei.recipe.FuelRecipeHandler',
    'ganymedes01.etfuturum.compat.nei.BlastFurnaceRecipeHandler',
    'ganymedes01.etfuturum.compat.nei.SmokerRecipeHandler',
}
# Research-station auxiliary stacks display already-written research results; keep them in otherItems.
INPUT_IDENTIFIERS = {'cropsnh.recipes.cropGeneExtractor'}
OUTPUT_IDENTIFIERS = {
    'EnderIOSagMill', 'EnderIOSoulBinder', 'thaumictinkerer.infusedseeds',
    'wgInfernalBlastfurnace', 'extratrees.lumbermill', 'bq_quest',
    'genetics.sequencer', 'kubatech.eec',
    'forestry.centrifuge',  # 林业离心机的辅助物品全部归入产物，保持原槽位顺序。
    'harvestcraftAnimalTrapDrops', 'harvestcraftFishTrapDrops', 'harvestcraftGardenDrops',
}
OUTPUT_SOURCES = {
    'alkalus.main.nei.NEI_Handler_Distillery',
}


def normalize_recipe_roles(recipe, metadata):
    """Move known ingredients/byproducts out of otherItems, preserving candidate order.

    NEI getOtherStacks combines fuel, catalysts and byproducts. Source-specific
    roles are resolved here so querying, calculators and renderers share them.
    Unrecognized auxiliary stacks retain their original meaning and fields.
    """
    source = metadata.get('source') or ''
    identifier = metadata.get('identifier') or ''
    others = recipe.get('otherItems')
    if not others:
        return

    if source in INPUT_SOURCES or identifier in INPUT_IDENTIFIERS or source.endswith('ImbuingStationRecipeHandler'):
        recipe['inputItems'] = list(recipe.get('inputItems') or []) + others
    elif source == 'alkalus.main.nei.NEI_Handler_Oven':
        # The oven exports optional fuel, then its byproduct and the required clay jar.
        inputs = list(recipe.get('inputItems') or [])
        inputs.extend([None] * max(0, 3 - len(inputs)))
        if len(others) == 3:
            inputs[1] = others[0]
        inputs[2] = others[-1]
        recipe['inputItems'] = inputs
        recipe['outputItems'] = list(recipe.get('outputItems') or []) + [others[-2]]
    elif identifier in OUTPUT_IDENTIFIERS or source in OUTPUT_SOURCES or source.endswith('LootBagRecipeHandler'):
        recipe['outputItems'] = list(recipe.get('outputItems') or []) + others
    else:
        return
    recipe.pop('otherItems', None)
