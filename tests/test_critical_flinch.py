from copy import deepcopy
from uuid import UUID
import pytest
from app.attributes import derived_stats
from app.combat import CombatAbility, execute_cast, execute_action, consume_flinch, InvalidCombatAction, roll_chance
from app.database import SessionLocal
from app.models import Encounter
from test_encounters import create, act

class Rolls:
    def __init__(self, *values): self.values = iter(values)
    def random(self): return next(self.values)

def fighter(id, team):
    return dict(id=id, name=id, team=team, hp=100, max_hp=100, power=20,
                derived_stats={'critical_chance_percent': 40, 'flinch_chance_percent': 20})

def test_formulas_caps_and_probability_boundaries():
    assert derived_stats({'Precision': 10, 'Luck': 10})['critical_chance_percent'] == 6
    assert derived_stats({'Might': 10, 'Speed': 10})['flinch_chance_percent'] == 4
    stats = derived_stats({k: 1000 for k in ('Precision','Luck','Might','Speed')})
    assert stats['critical_chance_percent'] == 40 and stats['flinch_chance_percent'] == 20
    assert roll_chance(40, Rolls(.3999))
    assert not roll_chance(40, Rolls(.4))
    assert not roll_chance(0, Rolls())

def test_critical_and_flinch_shared_damage_mitigation():
    actor, target = fighter('p','a'), fighter('e','b')
    target.update(guarding=True, guard_reduction_percent=60, guard_turn=1)
    target['derived_stats']['damage_reduction_percent'] = 20
    result = execute_action(actor, CombatAbility('hit','Hit',damage=20), target, turn=1, rng=Rolls(0,0))
    assert result['amount'] == 10  # 20 * 1.5 * .8 * .4
    assert result['critical'] and result['flinch'] and target['flinched']
    assert consume_flinch(target,1)['effect'] == 'flinch'
    assert consume_flinch(target,1) is None
    second = execute_action(actor, CombatAbility('hit','Hit',damage=2), target, turn=2, rng=Rolls(.9))
    assert not second['flinch']
    third = execute_action(actor, CombatAbility('hit','Hit',damage=2), target, turn=3, rng=Rolls(.9,0))
    assert third['flinch']

def test_support_no_rolls_and_lethal_hit_no_flinch():
    actor, target = fighter('p','a'), fighter('e','b')
    result = execute_action(actor, CombatAbility('guard','Guard',effect='guard',target_type='self'), actor, turn=1, rng=Rolls())
    assert not result['critical'] and not result['flinch']
    target['hp']=1
    result = execute_action(actor, CombatAbility('hit','Hit',damage=20), target, turn=1, rng=Rolls(0))
    assert result['critical'] and not result['flinch'] and target['hp']==0

def test_failed_area_cast_publishes_no_proc_or_damage():
    actor, first, invalid = fighter('p','a'), fighter('e','b'), fighter('ally','a')
    before=deepcopy([actor,first,invalid])
    with pytest.raises(InvalidCombatAction):
        execute_cast(actor,CombatAbility('area','Area',damage=10,max_targets=None),[first,invalid],turn=1,rng=Rolls(0,0))
    assert [actor,first,invalid]==before

def test_flinch_skips_enemy_action_recovery_and_retry(client,monkeypatch):
    encounter=create(client)
    with SessionLocal.begin() as db:
        saved=db.get(Encounter,UUID(encounter['id']))
        players=deepcopy(saved.participants)
        players[0]['derived_stats'].update(critical_chance_percent=40,flinch_chance_percent=20)
        saved.participants=players
    monkeypatch.setattr('app.encounter_service.combat_rng',lambda *args: Rolls(0,0))
    state=act(client,encounter).json()
    assert state['action_results'][0]['critical'] and state['action_results'][0]['flinch']
    assert state['participants'][0]['hp']==100
    assert any(r['effect']=='flinch' for r in state['action_results'])
    assert act(client,encounter).status_code==409
    saved=client.get('/api/encounters/'+state['id']).json()
    assert saved['enemies']==state['enemies']
    state=act(client,state).json()
    assert not state['action_results'][0]['flinch']
    assert state['participants'][0]['hp']==94

def test_raid_rng_uses_saved_seed_not_run_uuid(client):
    from app.encounter_service import combat_rng
    from test_encounter_groups import start
    _,a=start(client,'daily-raid')
    _,b=start(client,'daily-raid')
    with SessionLocal() as db:
        first=db.get(Encounter,UUID(a['id']));second=db.get(Encounter,UUID(b['id']))
        ra=combat_rng(first,first.participants[0]);rb=combat_rng(second,second.participants[0])
        assert [ra.random() for _ in range(10)]==[rb.random() for _ in range(10)]
