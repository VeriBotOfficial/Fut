"""Native per-match events -> durable, idempotent owned-instance counters.

See docs/match-statistics-native-schema.md for producer evidence and remaining
phone-validation limits. Unknown input never becomes invented statistics.
"""
import hashlib
import json

import club_store as store
import match_protocol as protocol
import season_rankings

COUNTERS = ('games', 'goal', 'assist')
SOURCE = 'ue4-11.1.1-775915c-v1'


def decode_result(conn, req, active, resolved):
    """Read the current native producer's participation and event vectors.

An event vector contains one identity per event, NOT unique scorers. Empty
vectors may be omitted. Only our lists are interpreted; opponent lists use a
different schema. YES/NO flags are conservative accepted protocol values.
"""
    if resolved['gave_up']:
        return None
    eligible = {tuple(value) for value in active.get('eligible_player_instances', [])}
    if not eligible:
        raise protocol.MatchError('stats_missing_match_roster')
    aliases = {}
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='owned_identity_aliases'").fetchone():
        aliases = {(pid, old): new for pid, old, new in conn.execute(
            'SELECT id,old_serial,new_serial FROM owned_identity_aliases')}

    def identity(value):
        if not isinstance(value, dict):
            raise protocol.MatchError('invalid_stats_player_identity')
        pid, serial = value.get('id'), value.get('serial')
        if type(pid) is not int or type(serial) is not int or pid <= 0 or serial < 0:
            raise protocol.MatchError('invalid_stats_player_identity')
        key = (pid, serial)
        if key not in eligible:
            key = (pid, aliases.get(key, serial))
        if key not in eligible:
            raise protocol.MatchError('stats_player_not_in_match_roster')
        return key

    mr = req.get('match_result')
    players = mr.get('myteam_player_list') if isinstance(mr, dict) else None
    if not isinstance(players, list) or not 1 <= len(players) <= 64:
        raise protocol.MatchError('invalid_native_participation_list')
    rows = {}
    for value in players:
        if not isinstance(value, dict):
            raise protocol.MatchError('invalid_native_participation_row')
        key = identity(value.get('gameplayer_id'))
        if key in rows:
            raise protocol.MatchError('duplicate_player_stats')
        played = value.get('play_time')
        starting, joined = value.get('is_starting_member'), value.get('is_join_game')
        if type(played) is not int or not 0 <= played < 2**31:
            raise protocol.MatchError('invalid_native_play_time')
        if starting not in ('YES', 'NO') or joined not in ('YES', 'NO'):
            raise protocol.MatchError('unknown_native_participation_flag')
        rows[key] = {'gameplayer_id': {'id': key[0], 'serial': key[1]},
                     'games': int(played > 0 or starting == 'YES' or joined == 'YES'),
                     'goal': 0, 'assist': 0}
    own_score = resolved['home'] if resolved['side'] == 'HOME' else resolved['away']
    for field, counter in (('goal_player_list', 'goal'), ('assist_player_list', 'assist')):
        events = req.get(field, [])
        if not isinstance(events, list) or len(events) > 1000:
            raise protocol.MatchError('invalid_native_event_list')
        if len(events) > own_score:
            raise protocol.MatchError('player_stats_exceed_team_score')
        for event in events:
            key = identity(event)
            if key not in rows or not rows[key]['games']:
                raise protocol.MatchError('stats_scoring_without_appearance')
            rows[key][counter] += 1
    return [rows[key] for key in sorted(rows)]


def _state_key(mode, game_id):
    return 'efootxi_match_stats_state_' + mode + '_' + str(game_id)


def state_key(mode, game_id):
    return _state_key(mode, game_id)


def _rows_digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode('utf8')).hexdigest()


def participation_summary(req):
    """Small flag samples for phone validation, never names/account/token data."""
    mr = req.get('match_result')
    players = mr.get('myteam_player_list') if isinstance(mr, dict) else None
    summary = {'player_count': len(players) if isinstance(players, list) else None}
    for field in ('is_starting_member', 'is_join_game'):
        values = set()
        for row in (players[:64] if isinstance(players, list) else []):
            value = row.get(field) if isinstance(row, dict) else None
            values.add(value[:24] if isinstance(value, str) else type(value).__name__)
        summary[field] = sorted(values)[:8]
    for field in ('goal_player_list', 'assist_player_list'):
        value = req.get(field, [])
        summary[field] = len(value) if isinstance(value, list) else type(value).__name__
    times = [row.get('play_time') for row in (players[:64] if isinstance(players, list) else [])
             if isinstance(row, dict)]
    valid = [value for value in times if type(value) is int and 0 <= value < 2**31]
    summary['play_time_range'] = [min(valid), max(valid)] if valid else []
    summary['invalid_play_time_count'] = len(times) - len(valid)
    return summary


def record_result(conn, mode, req, active, resolved):
    """Same transaction as score/rewards. Invalid stats leave a valid win intact.

Keep a frozen roster and outcome for a retried complete request. A missing
statistics payload is pending, not zero; a later complete retry may fill it
once without awarding match points twice.
"""
    if not conn.in_transaction:
        raise protocol.MatchError('stats_require_settlement_transaction')
    game_id = protocol.match_id(req)
    if mode == protocol.SEASON:
        season_rankings.refresh_opponent_roster(active)
    state = {'source': SOURCE, 'active': {'eligible_player_instances':
             active.get('eligible_player_instances', [])}, 'resolved': resolved}
    if mode == protocol.SEASON and active.get(season_rankings.CONTEXT):
        state['active'][season_rankings.CONTEXT] = active[season_rankings.CONTEXT]
    confirmed_rows = None
    conn.execute('SAVEPOINT individual_stats')
    try:
        rows = decode_result(conn, req, active, resolved)
        if rows is None:
            state['status'] = 'excluded_give_up'
        else:
            apply_confirmed_deltas(conn, mode, game_id, rows, active, resolved, source=SOURCE)
            confirmed_rows = rows
            state.update(status='applied', fingerprint=_rows_digest(rows))
        conn.execute('RELEASE SAVEPOINT individual_stats')
    except protocol.MatchError as exc:
        conn.execute('ROLLBACK TO SAVEPOINT individual_stats')
        conn.execute('RELEASE SAVEPOINT individual_stats')
        state.update(status='pending', error=str(exc))
        store.log_event(conn, 'efootxi_match_stats', {
            'schema': 2, 'status': 'pending', 'mode': mode, 'game_id': game_id,
            'error': str(exc), 'shape': request_shape(req),
            'participation': participation_summary(req)})
    store.set_meta(conn, _state_key(mode, game_id), json.dumps(state))
    if mode == protocol.SEASON:
        season_rankings.record_match(conn, req, active, resolved, confirmed_rows)
    return state['status']


def replay_result(conn, mode, req):
    """An already-settled score is not authority to change its scorer list."""
    raw = store.get_meta(conn, _state_key(mode, protocol.match_id(req)), '')
    if not raw:
        return  # pre-adapter receipts lack the frozen match roster
    state = json.loads(raw)
    if state['status'] == 'pending':
        record_result(conn, mode, req, state['active'], state['resolved'])
    elif state['status'] == 'applied':
        rows = decode_result(conn, req, state['active'], state['resolved'])
        if _rows_digest(rows) != state['fingerprint']:
            raise protocol.MatchError('conflicting_player_stats_retry')
        if mode == protocol.SEASON:
            season_rankings.record_match(conn, req, state['active'], state['resolved'], rows)
            store.set_meta(conn, _state_key(mode, protocol.match_id(req)), json.dumps(state))


def update_response(conn, mode, game_id, response):
    """Refresh the native client after confirmed durable statistics.

    Native extra-match decoder 0x7554bc8 reads counter *vectors*, while the
    owned gameplayer decoder 0x757a468 reads scalar lifetime counters. Consumer
    0x765ea8c writes the last vector value to the cached player's total. A
    singleton is sufficient; no guessed per-mode slot or synthetic delta is
    needed. Replays read current totals, so an old result cannot rewind a
    player's more recent totals. This function never changes the save.
    """
    raw = store.get_meta(conn, _state_key(mode, protocol.positive_id(game_id)), '')
    if not raw:
        return
    state = json.loads(raw)
    end = response.get('game_end_result')
    if not isinstance(end, dict):
        return
    extra = end.get('extra_match_result')
    if not isinstance(extra, dict):
        return
    updates = []
    if state.get('status') == 'applied':
        for pid, serial in state.get('active', {}).get('eligible_player_instances', []):
            row = conn.execute('SELECT json FROM gameplayers WHERE id=? AND serial=?',
                               (pid, serial)).fetchone()
            if not row:
                continue  # A historical replay cannot restore a released card.
            player = json.loads(row[0])
            if any(type(player.get(k)) is not int or not 0 <= player[k] < 2**31
                   for k in COUNTERS):
                continue
            updates.append({'gameplayer_id': {'id': pid, 'serial': serial},
                            **{k: [player[k]] for k in COUNTERS}})
    extra['gameplayer_update_info'] = updates


def snapshot_roster(active, refs):
    """Eligible squad identities; presence here is NOT proof of appearance."""
    identities = []
    for value in refs or []:
        if not isinstance(value, dict):
            continue
        identity = value.get('gameplayer_id') or value
        if not isinstance(identity, dict):
            continue
        pid, serial = identity.get('id'), identity.get('serial')
        if type(pid) is int and type(serial) is int and pid > 0 and serial > 0:
            key = [pid, serial]
            if key not in identities:
                identities.append(key)
    if identities:
        active['eligible_player_instances'] = identities


def request_shape(req):
    """Bounded structure only: no tokens, identifiers, player names or values."""
    paths = []

    def walk(value, path, depth):
        if len(paths) >= 12 or depth > 3:
            return
        if isinstance(value, dict):
            fields = {str(k)[:48]: type(v).__name__ for k, v in list(value.items())[:16]}
            paths.append({'path': path, 'fields': fields})
            for key, child in value.items():
                text = str(key).lower()
                if depth == 0 and not any(x in text for x in ('match', 'player', 'stat', 'goal', 'assist', 'score', 'record')):
                    continue
                if isinstance(child, (dict, list)):
                    walk(child, path + '.' + str(key)[:48], depth + 1)
        elif isinstance(value, list):
            paths.append({'path': path, 'type': 'list', 'count': len(value)})
            if value:
                walk(value[0], path + '[0]', depth + 1)

    walk(req, '$', 0)
    while len(json.dumps(paths, ensure_ascii=False)) > 2800 and paths:
        paths.pop()
    return paths


def observe_unmapped(conn, mode, req):
    shape = request_shape(req)
    digest = hashlib.sha256(json.dumps(shape, sort_keys=True).encode('utf8')).hexdigest()
    # One structural sample per mode/signature; avoid per-match log flooding.
    key = 'efootxi_match_stats_shape_' + mode
    if store.get_meta(conn, key, '') == digest:
        return
    store.set_meta(conn, key, digest)
    store.log_event(conn, 'efootxi_match_stats', {
        'schema': 1, 'status': 'awaiting_native_stats_schema', 'mode': mode,
        'game_id': protocol.match_id(req), 'shape': shape})


def apply_confirmed_deltas(conn, mode, game_id, rows, active, resolved, *, source):
    """Internal adapter API. Caller owns the same transaction as settlement.

Rows must already be decoded per-match deltas by an evidence-backed native
adapter: {gameplayer_id:{id,serial}, games:0|1, goal:int, assist:int}.
There is intentionally no heuristic mapping from arbitrary wire field names.
"""
    if not conn.in_transaction:
        raise protocol.MatchError('stats_require_settlement_transaction')
    game_id = protocol.positive_id(game_id)
    if mode not in (protocol.AI, protocol.SEASON, protocol.LOCAL) or not isinstance(source, str) or not source:
        raise protocol.MatchError('stats_schema_source_required')
    if not isinstance(rows, list):
        raise protocol.MatchError('invalid_player_stats_rows')
    eligible = {tuple(value) for value in active.get('eligible_player_instances', [])}
    if not eligible:
        raise protocol.MatchError('stats_missing_match_roster')
    normalized = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('gameplayer_id'), dict):
            raise protocol.MatchError('invalid_stats_player_identity')
        identity = row['gameplayer_id']
        pid, serial = identity.get('id'), identity.get('serial')
        if type(pid) is not int or type(serial) is not int or (pid, serial) not in eligible:
            raise protocol.MatchError('stats_player_not_in_match_roster')
        if (pid, serial) in seen:
            raise protocol.MatchError('duplicate_player_stats')
        seen.add((pid, serial))
        counters = {k: row.get(k) for k in COUNTERS}
        if any(type(v) is not int or v < 0 for v in counters.values()) or counters['games'] not in (0, 1):
            raise protocol.MatchError('invalid_player_stats_delta')
        if not counters['games'] and (counters['goal'] or counters['assist']):
            raise protocol.MatchError('stats_scoring_without_appearance')
        normalized.append({'id': pid, 'serial': serial, **counters})
    own_goals = resolved['home'] if resolved['side'] == 'HOME' else resolved['away']
    if not resolved['gave_up'] and (sum(r['goal'] for r in normalized) > own_goals
                                  or sum(r['assist'] for r in normalized) > own_goals):
        raise protocol.MatchError('player_stats_exceed_team_score')
    normalized.sort(key=lambda row: (row['id'], row['serial']))
    fingerprint = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode('utf8')).hexdigest()
    key = 'efootxi_match_stats_receipt_' + mode + '_' + str(game_id)
    old = store.get_meta(conn, key, '')
    if old:
        if json.loads(old)['fingerprint'] != fingerprint:
            raise protocol.MatchError('conflicting_player_stats_retry')
        return False
    # Validate every row before any write, including missing/sold instances.
    updates = []
    for row in normalized:
        saved = conn.execute('SELECT json FROM gameplayers WHERE id=? AND serial=?',
                             (row['id'], row['serial'])).fetchone()
        if not saved:
            raise protocol.MatchError('stats_player_instance_not_owned')
        player = json.loads(saved[0])
        for counter in COUNTERS:
            current = player.get(counter, 0)
            if type(current) is not int or current < 0:
                raise protocol.MatchError('invalid_stored_player_counter')
            player[counter] = current + row[counter]
        updates.append((json.dumps(player, ensure_ascii=False), row['id'], row['serial']))
    conn.executemany('UPDATE gameplayers SET json=? WHERE id=? AND serial=?', updates)
    store.set_meta(conn, key, json.dumps({'fingerprint': fingerprint, 'source': source}))
    store.log_event(conn, 'efootxi_match_stats', {
        'schema': 1, 'status': 'applied', 'mode': mode, 'game_id': game_id,
        'source': source, 'appeared': sum(r['games'] for r in normalized),
        'goals': sum(r['goal'] for r in normalized), 'assists': sum(r['assist'] for r in normalized)})
    return True
