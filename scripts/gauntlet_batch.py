"""Unattended Gauntlet comparisons using only the public game API (stdlib only)."""
import argparse
import json
import math
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from uuid import UUID


class ApiError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f'HTTP {status}: {message}')
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a redirected host.
        return None


class Api:
    def __init__(self, base, token):
        parsed = urlparse(base)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('API URL must be an HTTP(S) address without credentials, query or fragment.')
        if parsed.scheme != 'https' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
            raise ValueError('Remote API connections require HTTPS.')
        self.base, self.token = base.rstrip('/'), token
        self.opener = build_opener(NoRedirect())

    def __call__(self, path, body=None):
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                          headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise ApiError(error.code, error.read().decode(errors='replace')[:500]) from error


def validate(config):
    if not isinstance(config, dict) or set(config) - {'cases'}:
        raise ValueError('Configuration must contain only cases.')
    cases = config.get('cases')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('Provide 1–100 cases.')
    result, labels = [], set()
    for source in cases:
        if not isinstance(source, dict) or set(source) - {'name', 'adventurer_ids', 'definition_slug', 'ability_priority', 'repeats', 'max_stages', 'max_actions', 'max_seconds', 'heal_below'}:
            raise ValueError('Unknown case fields; see the example configuration.')
        case = dict(definition_slug='endless-road', ability_priority=['power_strike', 'attack'],
                    repeats=1, max_stages=50, max_actions=5000, max_seconds=600, heal_below=0.5)
        case.update(source)
        name = case.get('name')
        if not isinstance(name, str) or not name.strip() or name in labels:
            raise ValueError('Case names must be nonempty and unique.')
        labels.add(name)
        ids = case.get('adventurer_ids')
        if not isinstance(ids, list) or not ids:
            raise ValueError('Each case needs adventurer_ids.')
        case['adventurer_ids'] = [str(UUID(i)) for i in ids]
        if len(set(case['adventurer_ids'])) != len(ids):
            raise ValueError('Duplicate adventurer in a case.')
        for key, ceiling in [('repeats', 100), ('max_stages', 1000), ('max_actions', 100000), ('max_seconds', 86400)]:
            if type(case[key]) is not int or not 1 <= case[key] <= ceiling:
                raise ValueError(f'{key} must be an integer from 1 to {ceiling}.')
        if not isinstance(case['definition_slug'], str) or not case['definition_slug']:
            raise ValueError('definition_slug must be a nonempty string.')
        priority = case['ability_priority']
        if not isinstance(priority, list) or not priority or len(priority) > 100 or any(not isinstance(a, str) or not a for a in priority):
            raise ValueError('ability_priority must contain 1–100 ability slugs (or wait).')
        if type(case['heal_below']) not in (int, float) or not 0 < case['heal_below'] <= 1:
            raise ValueError('heal_below must be greater than 0 and at most 1.')
        result.append(case)
    if sum(c['repeats'] for c in result) > 1000:
        raise ValueError('A batch is limited to 1000 runs.')
    return result


def commands(encounter, case):
    """Choose commands, never calculate outcomes; the server validates each cast."""
    actor = next(p for p in encounter['participants'] if p['hp'] > 0 and not p['acted'])
    base = {'actor_id': actor['id'], 'expected_turn': encounter['turn']}
    living = [p for p in encounter['participants'] if p['hp'] > 0]
    enemies = sorted((e for e in encounter['enemies'] if e['hp'] > 0), key=lambda e: (e['hp'], e['id']))
    for slug in case['ability_priority']:
        if slug == 'wait':
            break
        spec = next((a for a in actor.get('equipped_abilities', []) if slug in (a.get('catalog_slug'), a['slug'])), None)
        if spec is None:
            continue
        if encounter['turn'] < actor.get('ability_ready_turns', {}).get(spec['slug'], 1) or time.time() < actor.get('ability_ready_at', {}).get(spec['slug'], 0):
            continue
        command = {**base, 'ability_id': spec['slug']}
        if spec.get('requires_weapon'):
            weapons = [w for w in actor.get('weapons', []) if not spec.get('allowed_weapon_tags') or set(w.get('tags', [])) & set(spec['allowed_weapon_tags'])]
            if not weapons:
                continue
            command['weapon_id'] = max(weapons, key=lambda w: (w['base_damage'], w['id']))['id']
        allies = [actor] if spec['target_type'] == 'self' else living
        weakest = min(allies, key=lambda p: (p['hp'] / p['max_hp'], p['id']))
        if spec['effect'] == 'heal' and weakest['hp'] / weakest['max_hp'] >= case['heal_below']:
            continue
        if spec['effect'] == 'cleanse':
            afflicted = [p for p in allies if p.get('statuses')]
            if not afflicted:
                continue
            weakest = afflicted[0]
        if spec['target_type'] == 'enemy' and enemies:
            command['target_id'] = enemies[0]['id']
        elif spec['target_type'] == 'ally':
            command['target_id'] = weakest['id']
        yield command
    yield {**base, 'action': 'wait'}


def run_batch(api, config, checkpoint=lambda report: None, delay=0.1):
    cases = validate(config)
    if not math.isfinite(delay) or not 0 <= delay <= 30:
        raise ValueError('delay must be between 0 and 30 seconds.')
    # Validate all requested parties/definitions before starting any runs.
    api('/auth/me')
    heroes = {h['id']: h for h in api('/adventurers')}
    definitions = {d['slug']: d for d in api('/gauntlets')}
    for case in cases:
        definition = definitions.get(case['definition_slug'])
        if not definition or len(case['adventurer_ids']) > definition['party_limit']:
            raise ValueError(f"Invalid definition or party size: {case['name']}")
        for identifier in case['adventurer_ids']:
            hero = heroes.get(identifier)
            if not hero or not hero['is_alive'] or hero['health'] <= 0 or hero.get('active_encounter_id'):
                raise ValueError(f"Unavailable or unauthorized adventurer in {case['name']}: {identifier}")
    report = {'cases': cases, 'runs': [], 'complete': False}
    checkpoint(report)
    for case in cases:
        for repeat in range(1, case['repeats'] + 1):
            row = {'case': case['name'], 'repeat': repeat, 'actions': 0, 'rejected_actions': 0, 'outcome': 'starting'}
            report['runs'].append(row)
            checkpoint(report)
            started = time.monotonic()
            try:
                created = api('/gauntlet-runs', {k: case[k] for k in ('definition_slug', 'adventurer_ids')})
                encounter = created['encounter']
                row.update(run_id=created['run']['id'], encounter_id=encounter['id'], outcome='running')
                row['unavailable_priorities'] = {
                    actor['id']: [slug for slug in case['ability_priority'] if slug != 'wait' and not any(
                        slug in (ability.get('catalog_slug'), ability['slug']) for ability in actor.get('equipped_abilities', []))]
                    for actor in encounter['participants']}
                checkpoint(report)
                while True:
                    row.update(encounter_id=encounter['id'], reached=encounter['gauntlet']['reached'], completed=encounter['gauntlet']['completed'])
                    capped = row['actions'] >= case['max_actions'] or time.monotonic() - started >= case['max_seconds']
                    if encounter['state'] == 'defeat':
                        row['outcome'] = 'failed'
                        break
                    if encounter['state'] == 'victory':
                        if capped or row['completed'] >= case['max_stages']:
                            api('/encounters/' + encounter['id'] + '/continue', {'return_to_village': True})
                            row['outcome'] = 'capped_retired'
                            break
                        encounter = api('/encounters/' + encounter['id'] + '/continue', {})
                    elif capped:
                        row['outcome'] = 'paused_limit'
                        break
                    else:
                        for command in commands(encounter, case):
                            try:
                                encounter = api('/encounters/' + encounter['id'] + '/actions', command)
                                row['actions'] += 1
                                break
                            except ApiError as error:
                                # Invalid casts may arise from status/modifier changes. Only
                                # validation errors fall back; never retry ambiguous writes.
                                if error.status != 400:
                                    raise
                                row['rejected_actions'] += 1
                        else:
                            raise RuntimeError('No legal action, including wait.')
                    checkpoint(report)
                    time.sleep(delay)
                row['summary'] = api('/gauntlet-runs/' + row['run_id'])
            except (Exception, KeyboardInterrupt) as error:
                row.update(outcome='interrupted' if isinstance(error, KeyboardInterrupt) else 'error', error=str(error))
            row['elapsed_seconds'] = round(time.monotonic() - started, 2)
            checkpoint(report)
            if row['outcome'] not in ('failed', 'capped_retired'):
                # An active/ambiguous run must not be silently abandoned for another case.
                return report
    report['complete'] = True
    report['comparison'] = []
    for case in cases:
        rows = [row for row in report['runs'] if row['case'] == case['name']]
        failures = [row for row in rows if row['outcome'] == 'failed']
        report['comparison'].append({
            'case': case['name'], 'runs': len(rows), 'capped_runs': len(rows) - len(failures),
            'best_completed': max(row['completed'] for row in rows),
            'best_reached': max(row['reached'] for row in rows),
            'mean_completed_uncapped': sum(row['completed'] for row in failures) / len(failures) if failures else None})
    checkpoint(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--api', required=True, help='API base including /api; HTTPS except localhost')
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='New JSON report file; never overwrites an existing report')
    parser.add_argument('--delay', type=float, default=0.1, help='Seconds between commands (default 0.1)')
    args = parser.parse_args()
    token = os.environ.get('GAUNTLET_TOKEN')
    if not token:
        parser.error('Set GAUNTLET_TOKEN to your account bearer token.')
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    validate(config)
    api = Api(args.api, token)
    # Claim an unused output name before creating any runs.
    with args.output.open('x', encoding='utf-8') as output:
        def checkpoint(report):
            output.seek(0)
            json.dump(report, output, indent=2)
            output.truncate()
            output.flush()
        report = run_batch(api, config, checkpoint, args.delay)
    for row in report['runs']:
        print(f"{row['case']} #{row['repeat']}: {row['outcome']}; completed={row.get('completed', 0)}, reached={row.get('reached', 0)}; run={row.get('run_id', 'unknown')}")
    return 0 if report['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
