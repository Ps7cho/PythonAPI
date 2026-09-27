from app.relationship_layout import essence_orb_ability_layout


def test_matrix_layout_is_stable_collision_free_and_reports_recipe_gaps():
    essences = [
        {'consumable_slug': 'essence-z', 'active': True},
        {'consumable_slug': 'essence-a', 'active': True},
        {'consumable_slug': 'essence-retired', 'active': False},
    ]
    consumables = [
        {'slug': 'essence-z', 'name': 'Azure'}, {'slug': 'essence-a', 'name': 'Zephyr'},
        {'slug': 'orb-b', 'name': 'Burst Orb'}, {'slug': 'orb-a', 'name': 'Anchor Orb'},
    ]
    abilities = [
        {'id': 'ability-1', 'slug': 'first', 'name': 'First'},
        {'id': 'ability-2', 'slug': 'second', 'name': 'Second'},
        {'id': 'ability-3', 'slug': 'third', 'name': 'Third'},
    ]
    outcomes = [
        {'essence_slug': 'essence-a', 'orb_slug': 'orb-b', 'ability_id': 'ability-3'},
        {'essence_slug': 'essence-z', 'orb_slug': 'orb-b', 'ability_id': 'ability-2'},
        {'essence_slug': 'essence-z', 'orb_slug': 'orb-a', 'ability_id': 'ability-1'},
    ]

    layout = essence_orb_ability_layout(essences, consumables, abilities, outcomes)

    assert layout['algorithm'] == 'essence-orb-matrix'
    assert [(cell['essence_slug'], cell['orb_slug']) for cell in layout['cells']] == [
        ('essence-z', 'orb-a'), ('essence-z', 'orb-b'), ('essence-a', 'orb-b')]
    assert layout['empty_cells'] == [
        {'row': 1, 'column': 0, 'essence_slug': 'essence-a', 'orb_slug': 'orb-a'}]
    abilities_in_layout = [node for node in layout['nodes'] if node['type'] == 'ability']
    assert len({(node['x'], node['y']) for node in abilities_in_layout}) == len(abilities_in_layout)
    assert not any(node.get('slug') == 'essence-retired' for node in layout['nodes'])
    assert len(layout['edges']) == 2 * len(outcomes)


def test_inspector_exposes_worldsmith_relationship_layout(client):
    data = client.get('/api/abilities?inspect=true').json()
    layout = data['essence_orb_ability_layout']

    assert layout['nodes'] and layout['cells']
    assert len(layout['edges']) == 2 * len(data['orb_outcomes'])
    assert {cell['ability_id'] for cell in layout['cells']} == {
        outcome['ability_id'] for outcome in data['orb_outcomes']}
