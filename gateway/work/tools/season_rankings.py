"""My League's season-only scorer/assist tables, not lifetime card counters.

The current native row decoder 0x7565198 consumes rank/player_id/team_id/value/
is_com (there is no serial). Real matches retain observed events; the other
fixtures retain deterministic offline simulations using each club's real roster.
No read operation advances fixtures or invents missing historical events.
"""
from __future__ import annotations

import copy
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import club_store as store
import match_protocol as protocol
import ziru_data

PREFIX = 'efootxi_season_rankings_v1_'
CONTEXT = 'season_ranking_context'
CANONICAL_PATH = Path(__file__).resolve().parents[2] / 'outputs/reference/canonical_rosters.json'
_canonical_cache = {}


def _canonical_data():
    path = Path(CANONICAL_PATH)
    if not path.is_file():
        return None
    key = str(path)
    if key not in _canonical_cache:
        document = json.loads(path.read_text(encoding='utf8'))
        if (document.get('schema') != 1 or not isinstance(document.get('rosters'), dict)
                or not isinstance(document.get('positions'), dict)):
            raise ValueError('Invalid reviewed native roster data')
        _canonical_cache[key] = document
    return _canonical_cache[key]


def _rosters():
    canonical = _canonical_data()
    if canonical:
        return canonical['rosters']
    try:
        return ziru_data.rosters()
    except ziru_data.NotPorted:
        # A missing optional roster cannot break an otherwise playable match.
        # The individual CPU events remain explicitly unavailable instead.
        return {}


def _positive(value):
    return type(value) is int and 0 < value < 2**63


def _scope(context):
    if not isinstance(context, dict) or any(not _positive(context.get(field))
            for field in ('season', 'competition_id', 'team_id')):
        raise protocol.MatchError('invalid_season_ranking_context')
    return PREFIX + '_'.join(str(context[field]) for field in
                            ('season', 'competition_id', 'team_id')) + '_'


def freeze_context(active, season):
    """Freeze the club/season and actual built-in opponent before kick-off."""
    if CONTEXT in active and active.get('started'):
        return
    if CONTEXT in active and _scope(active[CONTEXT]) != _scope(season):
        raise protocol.MatchError('season_ranking_context_changed')
    context = {field: season[field] for field in ('season', 'competition_id', 'team_id')}
    _scope(context)
    context['fixture'] = active['fixture']
    opponent = (active.get('opponent') or {}).get('team_data') or {}
    context['opponent_team_id'] = opponent.get('base_team_id')
    roster = _rosters().get(str(context['opponent_team_id']), [])
    context['opponent_roster'] = sorted({pid for pid in roster if _positive(pid)})
    canonical = _canonical_data()
    context['roster_source'] = (canonical['source']['cpk_sha256'] if canonical
                                else 'ported_rosters')
    active[CONTEXT] = context


def _transaction(conn):
    if not conn.in_transaction:
        raise protocol.MatchError('season_rankings_require_transaction')


def refresh_opponent_roster(active):
    """Upgrade old ported validation IDs, retaining the exact match context.

    This only supplies allowed native IDs for the same frozen opponent team.
    It neither supplies goal events nor changes accepted score/own identities.
    """
    context, canonical = active.get(CONTEXT), _canonical_data()
    if not context or not canonical:
        return False
    source = canonical['source']['cpk_sha256']
    roster = canonical['rosters'].get(str(context.get('opponent_team_id')))
    if context.get('roster_source') == source or not roster:
        return False
    context['opponent_roster'] = sorted(roster)
    context['roster_source'] = source
    return True


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode('utf8')).hexdigest()


def _rows(team, is_com, goals, assists):
    counts = defaultdict(lambda: {'goal': 0, 'assist': 0})
    for field, values in (('goal', goals), ('assist', assists)):
        for pid in values:
            counts[pid][field] += 1
    return [dict(player_id=pid, team_id=team, is_com=is_com, **counts[pid])
            for pid in sorted(counts)]


def _own_rows(rows, context):
    # Rows already passed the actual producer/owned-instance validator. The
    # native season UI groups copies of the same card because it has no serial.
    goals, assists = [], []
    for row in rows:
        pid = row['gameplayer_id']['id']
        goals.extend([pid] * row['goal'])
        assists.extend([pid] * row['assist'])
    return _rows(context['team_id'], 'NO', goals, assists)


def _opponent_rows(req, active, resolved, context):
    team = context.get('opponent_team_id')
    roster = set(context.get('opponent_roster') or [])
    if not _positive(team) or not roster:
        raise protocol.MatchError('season_opponent_roster_unavailable')
    ceiling = resolved['away'] if resolved['side'] == 'HOME' else resolved['home']
    if ceiling and 'opp_goal_player_list' not in req:
        raise protocol.MatchError('season_opponent_goal_events_missing')
    parsed = []
    # Native producer 0x775915c serializes one scalar int64 per opponent event,
    # unlike our {id,serial} events. Keep repetitions; never mix the two sides.
    for key in ('opp_goal_player_list', 'opp_assist_player_list'):
        values = req.get(key, [])
        if not isinstance(values, list) or len(values) > ceiling:
            raise protocol.MatchError('invalid_season_opponent_event_list')
        if any(not _positive(pid) or pid not in roster for pid in values):
            raise protocol.MatchError('season_opponent_player_not_in_roster')
        parsed.append(values)
    return _rows(team, 'YES', *parsed)


def _merge_component(record, field, rows):
    if field in record and record[field] != rows:
        raise protocol.MatchError('conflicting_season_ranking_retry')
    record[field] = rows


def record_match(conn, req, active, resolved, own_rows):
    """Atomically add real-match events, allowing a missing component to retry.

    A bad opponent vector does not erase valid owned-player counters or change
    the score. Its own diagnostic remains pending. Retries may fill a missing
    component, but cannot replace one already accepted. Old-season retries use
    their frozen context rather than whichever club is selected now.
    """
    _transaction(conn)
    refresh_opponent_roster(active)
    context = active.get(CONTEXT)
    if not context or resolved['gave_up']:
        return  # Older matches have no trustworthy season association.
    scope = _scope(context)
    key = scope + 'match_' + str(protocol.match_id(req))
    old = store.get_meta(conn, key, '')
    record = json.loads(old) if old else dict(source='observed_native_match',
                                             context=copy.deepcopy(context))
    proposed = copy.deepcopy(record)
    if 'opponent' not in proposed:
        proposed['context'] = copy.deepcopy(context)
    if own_rows is not None:
        _merge_component(proposed, 'own', _own_rows(own_rows, context))
    try:
        opponent = _opponent_rows(req, active, resolved, context)
    except protocol.MatchError as exc:
        proposed['opponent_pending'] = str(exc)
    else:
        _merge_component(proposed, 'opponent', opponent)
        proposed.pop('opponent_pending', None)
    store.set_meta(conn, key, json.dumps(proposed))


def _pick(roster, seed, label, index):
    value = hashlib.sha256((str(seed) + ':' + label + ':' + str(index)).encode()).digest()
    return roster[int.from_bytes(value[:8], 'big') % len(roster)]


def _weighted_roster(conn, roster, assists=False):
    """Prefer plausible roles where the registered base card supplies one.

    parameters[0] uses the verified native GK/CB/LB/.../CF order documented
    in opponent_squad.py. Missing role metadata remains neutral; roster order
    is not assumed to be a formation and card IDs are not guessed from names.
    """
    canonical = _canonical_data()
    positions = {pid: canonical['positions'][str(pid)] for pid in roster
                 if canonical and str(pid) in canonical['positions']}
    missing = [pid for pid in roster if pid not in positions]
    if missing and conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_cards'").fetchone():
        placeholders = ','.join('?' for _ in missing)
        for pid, raw in conn.execute('SELECT card_id,payload FROM managed_cards WHERE card_id IN ('
                                     + placeholders + ')', tuple(str(pid) for pid in missing)):
            params = json.loads(raw).get('parameters') or []
            if params and type(params[0]) is int and 0 <= params[0] <= 12:
                positions[int(pid)] = params[0]
    weights = ((0, 1, 3, 3, 4, 5, 7, 7, 8, 7, 7, 5, 3) if assists else
               (0, 1, 2, 2, 2, 4, 6, 6, 7, 9, 9, 10, 12))
    return [pid for pid in roster for _ in range(weights[positions[pid]]
            if pid in positions else 3)]


def _simulate_matches(conn, season, fixture, matches, only_teams=None):
    rows, unavailable = [], []
    for match in matches:
        if season['team_id'] in (match['home_team_id'], match['away_team_id']):
            continue
        for side in ('home', 'away'):
            team, score = match[side + '_team_id'], match[side + '_goals']
            if only_teams is not None and team not in only_teams:
                continue
            if not _positive(team) or type(score) is not int or not 0 <= score <= 1000:
                raise protocol.MatchError('invalid_season_simulated_fixture')
            roster = sorted({pid for pid in _rosters().get(str(team), [])
                             if _positive(pid)})
            if not roster:
                unavailable.append(team)
                continue
            scorers = _weighted_roster(conn, roster)
            assisters = _weighted_roster(conn, roster, assists=True)
            if not scorers:
                unavailable.append(team)
                continue
            seed = ':'.join(str(value) for value in (season['season'],
                season['competition_id'], fixture, match['home_team_id'],
                match['away_team_id'], team))
            goals, assists = [], []
            for index in range(score):
                scorer = _pick(scorers, seed, 'scorer', index)
                goals.append(scorer)
                others = [pid for pid in assisters if pid != scorer]
                # Some goals are unassisted. It is an offline simulation, not
                # a fabricated replay of a match the user actually played.
                if others and _pick(list(range(5)), seed, 'assisted', index) != 0:
                    assists.append(_pick(others, seed, 'assist', index))
            rows.extend(_rows(team, 'YES', goals, assists))
    return rows, sorted(set(unavailable))


def record_fixture(conn, season, fixture, matches):
    """Save other CPU fixtures' simulated player events alongside their scores.

    This never simulates the selected club's fixture: both its teams instead
    use the native real-match events above. A previously missing roster may
    fill its own saved score once; accepted other-team events are preserved.
    """
    _transaction(conn)
    scope = _scope(season)
    key = scope + 'fixture_' + str(protocol.positive_id(fixture))
    fingerprint = _digest(matches)
    old = store.get_meta(conn, key, '')
    if old:
        saved = json.loads(old)
        if saved['fingerprint'] != fingerprint:
            raise protocol.MatchError('conflicting_season_fixture_ranking_retry')
        pending = set(saved.get('unavailable_teams') or [])
        if pending:
            recovered, unavailable = _simulate_matches(conn, season, fixture, matches, pending)
            if recovered or set(unavailable) != pending:
                saved['rows'].extend(recovered)
                saved['unavailable_teams'] = unavailable
                store.set_meta(conn, key, json.dumps(saved))
        return
    rows, unavailable = _simulate_matches(conn, season, fixture, matches)
    store.set_meta(conn, key, json.dumps(dict(source='simulated_cpu_fixtures',
        fingerprint=fingerprint, fixture=fixture, rows=rows,
        unavailable_teams=unavailable)))


def repair_missing_fixtures(conn, season):
    """Fill formerly unavailable CPU rosters from already saved fixture scores.

    The caller owns a normal write transaction. No future fixture is created
    and no known player's existing simulated record is recomputed.
    """
    _transaction(conn)
    scope = _scope(season)
    for fixture, played in season.get('played', {}).items():
        key = scope + 'fixture_' + str(protocol.positive_id(fixture))
        raw = store.get_meta(conn, key, '')
        if raw and json.loads(raw).get('unavailable_teams'):
            record_fixture(conn, season, int(fixture), played['matches'])


def populate(conn, season, response):
    """Read this season's retained events without changing its progress."""
    scope = _scope(season)
    totals = defaultdict(lambda: {'goal': 0, 'assist': 0})
    for (raw,) in conn.execute('SELECT value FROM meta WHERE key>=? AND key<?',
                              (scope, scope + '\uffff')):
        saved = json.loads(raw)
        rows = saved.get('rows', []) if saved.get('source') == 'simulated_cpu_fixtures' else (
            saved.get('own', []) + saved.get('opponent', []))
        for row in rows:
            total = totals[(row['player_id'], row['team_id'], row['is_com'])]
            for field in ('goal', 'assist'):
                total[field] += row[field]
    for field in ('goal', 'assist'):
        ordered = sorted(((identity, value[field]) for identity, value in totals.items()
                          if value[field] > 0),
                         key=lambda item: (-item[1], item[0][1], item[0][2], item[0][0]))
        ranks, rank, previous = [], 0, None
        for index, ((pid, team, is_com), count) in enumerate(ordered, 1):
            if count != previous:
                rank, previous = index, count
            ranks.append(dict(rank=rank, player_id=pid, team_id=team,
                              value=count, is_com=is_com))
        response[field + '_ranking'] = ranks
    return response
