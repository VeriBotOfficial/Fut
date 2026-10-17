"""Local match identity, conservative result decoding and test diagnostics.

No connection-status check belongs in settlement. A completed local match is
settled from its scores; an unrecognised end condition stays pending, rather
than being silently converted to a defeat or a captured response.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

import club_store as store

AI = 'ai_division'
SEASON = 'my_league'
LOCAL = 'local_vscom'
OTHER = 'other'
PREPARE_PROCESS_TOKEN = uuid.uuid4().hex
CONTEXT_KEY = 'efootxi_match_selected_mode'
SCORE_FIELDS = ('score_1st', 'score_2nd', 'score_ex1st', 'score_ex2nd')
NORMAL_REASONS = frozenset(('', 'NOERR', 'NORMAL', 'NORMAL_END'))
DIAGNOSTIC_ERRORS = frozenset('''
abandoned_game_id ambiguous_game_id ambiguous_score_sides conflicting_game_ids
conflicting_player_stats_retry conflicting_result_retry conflicting_season_fixture_ranking_retry
conflicting_season_ranking_retry duplicate_player_stats empty_match_squad
explicit_test_seed_copy_required invalid_game_id invalid_give_up_flag invalid_local_match_session
invalid_match_category invalid_match_end_reason invalid_match_result invalid_match_session
invalid_match_side invalid_match_squad invalid_match_squad_identity invalid_mode
invalid_native_event_list invalid_native_participation_list invalid_native_participation_row
invalid_native_play_time invalid_player_stats_delta invalid_player_stats_rows
invalid_saved_match_squad invalid_score_value invalid_season_opponent_event_list
invalid_season_ranking_context invalid_season_simulated_fixture invalid_special_squad_roster
invalid_squad_id invalid_stats_player_identity invalid_stored_player_counter
local_match_category_changed match_category_conflicts_with_game_id missing_game_id
missing_match_gameplan missing_score_fields missing_special_squad_gameplan
opponent_request_category_changed player_stats_exceed_team_score prepare_intent_requires_transaction
result_before_start season_not_selected season_opponent_goal_events_missing
season_opponent_player_not_in_roster season_opponent_roster_unavailable season_rankings_require_transaction
seed_cleanup_requires_independent_transaction stale_fixture stale_round
started_match_difficulty_changed started_match_roster_changed started_match_side_changed
stats_missing_match_roster stats_player_instance_not_owned stats_player_not_in_match_roster
stats_require_settlement_transaction stats_schema_source_required stats_scoring_without_appearance
two_score_rows_required unconfirmed_match_end unknown_game_id unknown_match_coach unknown_match_squad
unknown_native_participation_flag unknown_special_squad_coach unowned_or_duplicate_special_squad_player
wrong_match_mode
incomplete_match_starting_eleven match_squad_plan_identity_mismatch
unverified_match_tactics_reindex invalid_match_plan_member_index missing_match_starting_player
invalid_match_gameplan invalid_match_plan_order duplicate_match_starting_player
season_ranking_context_changed
'''.split())


class MatchError(ValueError):
    """A recoverable protocol/state mismatch; never a fabricated match loss."""


def positive_id(value):
    if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 < value < 2**63:
        raise MatchError('invalid_game_id')
    return value


def match_id(req, required=True):
    nested = req.get('match_result')
    if nested is not None and not isinstance(nested, dict):
        raise MatchError('invalid_match_result')
    supplied = [v for v in ((nested or {}).get('game_id'), req.get('game_id'))
                if v is not None]
    if not supplied:
        if required:
            raise MatchError('missing_game_id')
        return None
    ids = [positive_id(v) for v in supplied]
    if len(set(ids)) != 1:
        raise MatchError('conflicting_game_ids')
    return ids[0]


def category_mode(req):
    category = req.get('match_category')
    if category in (None, ''):
        return None
    if not isinstance(category, str):
        raise MatchError('invalid_match_category')
    if category.startswith('LEAGUE_COM'):
        return AI
    if any(token in category for token in ('SEASON', 'MYLEAGUE', 'MY_LEAGUE')):
        return SEASON
    return OTHER


def active_match(conn, mode):
    if mode == AI:
        return json.loads(store.get_meta(conn, 'ai_division_active_match', 'null'))
    if mode == LOCAL:
        return json.loads(store.get_meta(conn, 'local_vscom_active_match', 'null'))
    if mode == SEASON:
        season = json.loads(store.get_meta(conn, 'my_league_state_v1', 'null'))
        return (season or {}).get('active')
    return None


def receipt_key(mode, game_id):
    return mode + '_result_' + str(positive_id(game_id))


def prepare_active_for_request(conn, mode, active, req, session_token=None):
    """Resolve a new opponent fetch without inventing a result for a lost game.

Caller owns the prepare transaction and persists the returned active match.
An exact session/rqid retry preserves a started game. A fresh fetch after a
hard exit can replace it, while an unstarted same-category preview is reused.
"""
    if not conn.in_transaction:
        raise MatchError('prepare_intent_requires_transaction')
    if mode not in (AI, SEASON, LOCAL):
        raise MatchError('invalid_mode')
    session = PREPARE_PROCESS_TOKEN if session_token is None else session_token
    if not isinstance(session, str) or not session or len(session) > 128:
        raise MatchError('invalid_match_session')
    rqid = req.get('rqid')
    identifiable = (type(rqid) is int or isinstance(rqid, str) and 0 < len(rqid) <= 128)
    intent = {'opponent_request_session': session,
              'opponent_request_rqid': rqid if identifiable else None}
    requested = req.get('match_category')
    if active:
        recorded = active.get('match_category')
        category_changed = (requested not in (None, '') and recorded not in (None, '')
                            and requested != recorded)
        same_request = (identifiable and active.get('opponent_request_session') == session
                        and active.get('opponent_request_rqid') == intent['opponent_request_rqid'])
        if same_request:
            if category_changed:
                raise MatchError('opponent_request_category_changed')
        elif active.get('started') or category_changed:
            game_id = positive_id(active['game_id'])
            abandoned = {'game_id': game_id, 'mode': mode,
                         'started': bool(active.get('started')),
                         'reason': ('new_session' if active.get('opponent_request_session') != session
                                    else 'new_opponent_request')}
            store.set_meta(conn, 'efootxi_match_abandoned_' + mode + '_' + str(game_id),
                           json.dumps(abandoned))
            store.log_event(conn, 'efootxi_match_abandoned', abandoned)
            active = None
        elif recorded in (None, '') and requested not in (None, ''):
            active['match_category'] = requested
    return active, intent


def resolve_mode(conn, req, include_receipts=True):
    """Resolve a start/result by stored ID, never by an unrelated active mode."""
    category = category_mode(req)
    game_id = match_id(req, required=include_receipts)
    if game_id is None:
        if not include_receipts and category in (None, OTHER):
            return OTHER
        raise MatchError('missing_game_id')
    matches = []
    for mode in (AI, SEASON, LOCAL):
        active = active_match(conn, mode)
        same_active = bool(active and positive_id(active.get('game_id')) == game_id)
        receipt = include_receipts and store.get_meta(conn, receipt_key(mode, game_id), '')
        if same_active or receipt:
            matches.append(mode)
    if len(matches) > 1:
        raise MatchError('ambiguous_game_id')
    if not matches:
        if (store.get_meta(conn, 'local_vscom_abandoned_' + str(game_id), '') or
                any(store.get_meta(conn, 'efootxi_match_abandoned_' + mode + '_' + str(game_id), '')
                    for mode in (AI, SEASON, LOCAL))):
            raise MatchError('abandoned_game_id')
        if category == OTHER or (not include_receipts and category is None):
            return OTHER
        raise MatchError('unknown_game_id')
    mode = matches[0]
    # OTHER is a category classification, never authority to record a match.
    # A local computer match is recognized only by its previously saved ID.
    allowed_category = OTHER if mode == LOCAL else mode
    if category is not None and category != allowed_category:
        raise MatchError('match_category_conflicts_with_game_id')
    return mode


def remember_mode(conn, mode):
    """Called when the player enters a mode's own menu, not on result replay."""
    if mode not in (AI, SEASON, LOCAL):
        raise MatchError('invalid_mode')
    in_transaction = conn.in_transaction
    store.set_meta(conn, CONTEXT_KEY, mode)
    if not in_transaction:
        conn.commit()


def opponent_mode(conn, req):
    category = category_mode(req)
    if category is not None:
        return category
    if match_id(req, required=False) is not None:
        return resolve_mode(conn, req, include_receipts=False)
    # Pre-match requests may omit category and game_id. Use the menu the user
    # actually entered, not the mere presence of a seeded My League save.
    selected = store.get_meta(conn, CONTEXT_KEY, '')
    if selected in (AI, SEASON, LOCAL):
        return selected
    # Preserve untracked modes' pre-match path. A saved season alone must not
    # turn an otherwise ordinary local match into a My League fixture.
    return OTHER


def next_game_id(conn):
    previous = [int(store.get_meta(conn, key, '0') or 0) for key in (
        'efootxi_match_sequence', 'ai_division_game_sequence', 'my_league_sequence')]
    value = max(time.time_ns() // 1000, max(previous) + 1)
    value = positive_id(value)
    store.set_meta(conn, 'efootxi_match_sequence', value)
    return value


def side(value):
    if value not in ('HOME', 'AWAY'):
        raise MatchError('invalid_match_side')
    return value


def scores(req):
    rows = req.get('score')
    if not isinstance(rows, list) or len(rows) != 2:
        raise MatchError('two_score_rows_required')
    totals = []
    labels = []
    for row in rows:
        if not isinstance(row, dict) or not any(k in row for k in SCORE_FIELDS):
            raise MatchError('missing_score_fields')
        values = [row.get(k, 0) for k in SCORE_FIELDS]
        if any(type(v) is not int or not 0 <= v < 2**31 for v in values):
            raise MatchError('invalid_score_value')
        totals.append(sum(values))
        labels.append(row.get('side'))
    if any(value is not None for value in labels):
        if any(value not in ('HOME', 'AWAY') for value in labels) or set(labels) != {'HOME', 'AWAY'}:
            raise MatchError('ambiguous_score_sides')
        by_side = dict(zip(labels, totals))
        return by_side['HOME'], by_side['AWAY'], 'explicit_row_side'
    # Current shipped handlers and response fixtures use stadium home/away.
    # No captured request proves an alternative ordering. Preserve it, record
    # its provenance, and do not guess a reverse order for away matches.
    return totals[0], totals[1], 'legacy_stadium_order_unverified'


def outcome(req, active):
    match_id(req)
    user_side = side(active.get('side'))
    mr = req.get('match_result') or {}
    intentional = req.get('intentional_give_up')
    if intentional not in (None, '', 'YES', 'NO'):
        raise MatchError('invalid_give_up_flag')
    reason = mr.get('abnormalend_reason')
    end_reason = mr.get('gameend_reason')
    reason = '' if reason is None else reason
    end_reason = '' if end_reason is None else end_reason
    if not isinstance(reason, str) or not isinstance(end_reason, str):
        raise MatchError('invalid_match_end_reason')
    # The current native request constructor (0x7758e1c) defaults the root
    # intentional_give_up string to YES. It is not reliable authority over an
    # explicit terminal reason: the supplied 73 reference also documents
    # NOERR + YES after a normally completed 11:0 game (vscom_result.gave_up).
    # Prefer the actual reason, retaining the flag only as a legacy fallback
    # when neither reason exists. The score still decides WIN/DRAW/LOSE.
    authoritative = reason or end_reason
    if authoritative == 'NORMAL_GIVEUP':
        gave_up = True
    elif authoritative in NORMAL_REASONS and authoritative:
        gave_up = False
    elif authoritative:
        raise MatchError('unconfirmed_match_end')
    else:
        gave_up = intentional == 'YES'
    if gave_up:
        home, away = (0, 3) if user_side == 'HOME' else (3, 0)
        source = 'explicit_give_up'
    else:
        if reason not in NORMAL_REASONS or end_reason not in NORMAL_REASONS:
            raise MatchError('unconfirmed_match_end')
        home, away, source = scores(req)
    ours, theirs = (home, away) if user_side == 'HOME' else (away, home)
    result = 'WIN' if ours > theirs else 'DRAW' if ours == theirs else 'LOSE'
    return {'home': home, 'away': away, 'side': user_side, 'result': result,
            'gave_up': gave_up, 'score_source': source}


def end_reasons(mode, resolved):
    if resolved['gave_up']:
        return 'NORMAL_GIVEUP', 'NORMAL_GIVEUP'
    # Current 11.1.1 native response consumer (raw 0x76921b0) shares these
    # enums across modes. The reference APK's NORMAL/NORMAL template is not
    # valid here: unknown abnormal reasons fall back to DISCONN_HB, not NOERR.
    return 'NORMAL_END', 'NOERR'


def diagnostic(conn, event, req, mode=None, active=None, resolved=None, error=None, response=None, command=None):
    mr = req.get('match_result')
    mr = mr if isinstance(mr, dict) else {}
    rows = req.get('score')
    safe_rows = None
    if isinstance(rows, list):
        safe_rows = [{k: r[k] for k in SCORE_FIELDS + ('side',) if k in r}
                     if isinstance(r, dict) else type(r).__name__ for r in rows[:2]]
    detail = {'schema': 1, 'event': event, 'mode': mode,
              'game_id': mr.get('game_id', req.get('game_id')),
              'match_category': req.get('match_category'), 'request_side': req.get('side'),
              'gameend_reason': mr.get('gameend_reason'),
              'abnormalend_reason': mr.get('abnormalend_reason'),
              'intentional_give_up': req.get('intentional_give_up'), 'score': safe_rows}
    if command in ('CMD_START_GAME', 'CMD_GET_VSCOM_GAME_RESULT', 'CMD_GET_VSCOM_OPPONENT',
                   'CMD_GET_MYCLUB_VSCOM_OPPONENT', 'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO'):
        detail['command'] = command
    if command == 'CMD_START_GAME':
        roster = req.get('player_list_home')
        detail['start_roster'] = {'provided': 'player_list_home' in req,
                                 'count': len(roster) if isinstance(roster, list) else None}
        if isinstance(roster, list):
            detail['start_roster']['empty_slots'] = sum(
                isinstance(slot, dict) and isinstance(slot.get('gameplayer_id'), dict) and
                type(slot['gameplayer_id'].get('id')) is int and
                type(slot['gameplayer_id'].get('serial')) is int and
                slot['gameplayer_id']['id'] == slot['gameplayer_id']['serial'] == 0
                for slot in roster)
    # A phone may reject a match before sending its final result. Keep field
    # names and narrowly selected validity/end values so the terminal request
    # can be distinguished from a missing terminal request. These fields have
    # not been captured in a failing request; logging does not give them new
    # business semantics or turn an abnormal match into a win.
    detail['request_fields'] = sorted(str(k)[:60] for k in req)[:40]
    detail['match_result_fields'] = sorted(str(k)[:60] for k in mr)[:40]
    validity = {}
    keys = ('is_invalid', 'is_valid', 'invalid_reason', 'game_status', 'match_status',
            'gameend_reason', 'abnormalend_reason')
    for path, source in (('request', req), ('match_result', mr)):
        for key in keys:
            if key not in source:
                continue
            value = source[key]
            if isinstance(value, str):
                value = value[:80]
            elif value is not None and type(value) not in (bool, int):
                value = type(value).__name__
            validity[path + '.' + key] = value
    if validity:
        detail['reported_validity'] = validity
    if active:
        detail['active'] = {k: active.get(k) for k in ('game_id', 'side', 'started', 'round', 'fixture')}
    if resolved:
        detail['settlement'] = resolved
    if error:
        # Keep a known static code, never an arbitrary exception containing
        # account data. Older releases wrote a sentence the notice hid entirely.
        text = str(error)
        aliases = {'Unknown or duplicate owned player instance in match squad':
                   'unowned_or_duplicate_match_squad_player'}
        detail['error'] = aliases.get(text, text if isinstance(error, MatchError) and
                                      text in DIAGNOSTIC_ERRORS else 'unclassified_request_error')
    import match_stats
    detail['participation'] = match_stats.participation_summary(req)
    try:
        game_id = match_id(req)
        raw = store.get_meta(conn, match_stats.state_key(mode, game_id), '')
        state = json.loads(raw) if raw else {}
        if state:
            detail['statistics'] = {k: state.get(k) for k in ('status', 'error') if k in state}
    except (MatchError, ValueError, TypeError):
        pass
    if isinstance(response, dict):
        end = response.get('game_end_result') or {}
        result_data = end.get('match_result') or {}
        extra = end.get('extra_match_result') or {}
        league = extra.get('league_match_result') or {}
        detail['response'] = {'result': response.get('result'),
            **{k: result_data.get(k) for k in ('game_res', 'gameend_reason', 'abnormalend_reason')},
            'is_invalid': league.get('is_invalid') if isinstance(league, dict) else None,
            'player_update_count': len(extra.get('gameplayer_update_info') or [])}
    # events.log_event has a 4000-character cap. Drop optional field-name lists
    # first so the stored value always remains valid JSON for phone feedback.
    for key in ('request_fields', 'match_result_fields', 'reported_validity'):
        if len(json.dumps(detail, ensure_ascii=False)) <= 3800:
            break
        detail.pop(key, None)
    store.log_event(conn, 'efootxi_match', detail)


def reject(conn, mid, req, error, mode=None, active=None):
    # Caller must roll back any failed business transaction before using this.
    in_transaction = conn.in_transaction
    if active is None and mode in (AI, SEASON, LOCAL):
        candidate = active_match(conn, mode)
        try:
            if candidate and match_id(req, required=False) == candidate.get('game_id'):
                active = candidate
        except MatchError:
            pass
    diagnostic(conn, 'rejected', req, mode, active, error=error, command=mid)
    if not in_transaction:
        conn.commit()
    return {'msgid': mid, 'rqid': req.get('rqid'), 'result': 'ERR_INVALID_PARAM'}


def _receipt_meta_key(mode, game_id):
    return 'efootxi_match_receipt_' + mode + '_' + str(game_id)


def _fingerprint(resolved):
    fields = {key: resolved[key] for key in ('home', 'away', 'side', 'result', 'gave_up')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode('utf8')).hexdigest()


def replay(conn, mode, req):
    game_id = match_id(req)
    raw = store.get_meta(conn, receipt_key(mode, game_id), '')
    if not raw:
        return None
    metadata = store.get_meta(conn, _receipt_meta_key(mode, game_id), '')
    resolved = None
    if metadata:
        metadata = json.loads(metadata)
        resolved = outcome(req, {'side': metadata['side']})
        if _fingerprint(resolved) != metadata['fingerprint']:
            raise MatchError('conflicting_result_retry')
    response = json.loads(raw)
    import match_stats
    match_stats.replay_result(conn, mode, req)
    match_stats.update_response(conn, mode, game_id, response)
    end = response.get('game_end_result')
    result_data = end.get('match_result') if isinstance(end, dict) else None
    if (resolved is not None and not resolved['gave_up'] and isinstance(result_data, dict) and
            (result_data.get('gameend_reason'), result_data.get('abnormalend_reason')) ==
            ('NORMAL', 'NORMAL')):
        # Only repair a known old presentation pair after the incoming result
        # matches the saved outcome fingerprint. This neither re-settles the
        # score/rewards nor invents statistics for historical incomplete data.
        result_data['gameend_reason'], result_data['abnormalend_reason'] = end_reasons(mode, resolved)
        store.set_meta(conn, receipt_key(mode, game_id), json.dumps(response))
        store.log_event(conn, 'efootxi_match_response_repaired', {
            'schema': 1, 'mode': mode, 'game_id': game_id,
            'change': 'native_normal_end_reasons'})
    response['rqid'] = req.get('rqid')
    diagnostic(conn, 'replayed', req, mode, resolved=resolved, response=response)
    return response


def save_receipt(conn, mode, req, response, resolved):
    game_id = match_id(req)
    store.set_meta(conn, receipt_key(mode, game_id), json.dumps(response))
    store.set_meta(conn, _receipt_meta_key(mode, game_id), json.dumps({
        'side': resolved['side'], 'fingerprint': _fingerprint(resolved),
        'end_classifier': 'terminal_reason_first_v2'}))


def initialize_test_seed(conn, *, new_test_seed=False):
    """Build-time operation on an explicit copied test seed, never on startup."""
    if not new_test_seed:
        raise MatchError('explicit_test_seed_copy_required')
    if conn.in_transaction:
        raise MatchError('seed_cleanup_requires_independent_transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        keys = [key for (key,) in conn.execute('SELECT key FROM meta')
                if key.startswith(('my_league_', 'ai_division_', 'local_vscom_', 'efootxi_match_', 'efootxi_season_rankings_'))
                or key in ('vscom_profile_index', 'vscom_opponent_team_id')]
        conn.executemany('DELETE FROM meta WHERE key=?', [(key,) for key in keys])
        store.set_meta(conn, 'efootxi_match_seed_version', '1200')
        conn.commit()
        return len(keys)
    except Exception:
        conn.rollback()
        raise
