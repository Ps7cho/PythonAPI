"""Deterministic layouts for relationships shown by content-authoring clients."""


def essence_orb_ability_layout(essences, consumables, abilities, outcomes):
    """Place orb outcomes in a matrix so every recipe can be read at a glance.

    Essences define rows and orbs define columns.  An ability is placed at the
    intersection that creates it.  Unlike a generic force-directed graph, this
    remains stable between requests and avoids overlapping nodes as the catalog
    grows or contains incomplete recipe sets.
    """
    item_by_slug = {item['slug']: item for item in consumables}
    ability_by_id = {str(ability['id']): ability for ability in abilities}
    active_essences = sorted(
        (essence for essence in essences if essence.get('active')),
        key=lambda essence: (
            item_by_slug.get(essence['consumable_slug'], {}).get('name', '').casefold(),
            essence['consumable_slug'],
        ),
    )
    orb_slugs = sorted(
        {outcome['orb_slug'] for outcome in outcomes},
        key=lambda slug: (item_by_slug.get(slug, {}).get('name', '').casefold(), slug),
    )

    label_width, column_width, row_height = 220, 280, 120
    header_height = 88
    nodes = []
    for column, slug in enumerate(orb_slugs):
        item = item_by_slug.get(slug, {})
        nodes.append(dict(id=f'orb:{slug}', type='orb', slug=slug,
                          label=item.get('name', slug), x=label_width + column * column_width,
                          y=0, column=column))
    for row, essence in enumerate(active_essences):
        slug = essence['consumable_slug']
        item = item_by_slug.get(slug, {})
        nodes.append(dict(id=f'essence:{slug}', type='essence', slug=slug,
                          label=item.get('name', slug), x=0,
                          y=header_height + row * row_height, row=row))

    row_by_slug = {essence['consumable_slug']: row for row, essence in enumerate(active_essences)}
    column_by_slug = {slug: column for column, slug in enumerate(orb_slugs)}
    cells, edges = [], []
    for outcome in sorted(outcomes, key=lambda value: (
            row_by_slug.get(value['essence_slug'], len(row_by_slug)),
            column_by_slug.get(value['orb_slug'], len(column_by_slug)),
            str(value['ability_id']))):
        if outcome['essence_slug'] not in row_by_slug or outcome['orb_slug'] not in column_by_slug:
            continue
        ability_id = str(outcome['ability_id'])
        ability = ability_by_id.get(ability_id, {})
        row, column = row_by_slug[outcome['essence_slug']], column_by_slug[outcome['orb_slug']]
        # A definition may intentionally be reused by more than one recipe, so
        # the visual node is recipe-specific while retaining its ability_id.
        node_id = f'ability:{outcome["essence_slug"]}:{outcome["orb_slug"]}'
        cells.append(dict(row=row, column=column, essence_slug=outcome['essence_slug'],
                          orb_slug=outcome['orb_slug'], ability_id=ability_id))
        nodes.append(dict(id=node_id, type='ability', ability_id=ability_id,
                          slug=ability.get('slug'), label=ability.get('name', outcome.get('name', ability_id)),
                          x=label_width + column * column_width,
                          y=header_height + row * row_height, row=row, column=column))
        edges.extend((
            dict(source=f'essence:{outcome["essence_slug"]}', target=node_id, relation='infuses'),
            dict(source=f'orb:{outcome["orb_slug"]}', target=node_id, relation='unlocks'),
        ))

    occupied = {(cell['row'], cell['column']) for cell in cells}
    empty_cells = [dict(row=row, column=column,
                        essence_slug=essence['consumable_slug'], orb_slug=orb_slug)
                   for row, essence in enumerate(active_essences)
                   for column, orb_slug in enumerate(orb_slugs)
                   if (row, column) not in occupied]
    return dict(
        algorithm='essence-orb-matrix', version=1,
        width=label_width + max(1, len(orb_slugs)) * column_width,
        height=header_height + max(1, len(active_essences)) * row_height,
        nodes=nodes, edges=edges, cells=cells, empty_cells=empty_cells,
    )
