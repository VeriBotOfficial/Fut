"""Receipts and player statistics for computer matches created by this gateway.

An unknown/online category cannot create an active match through START or
RESULT. Only the local VSCOM opponent handler calls prepare after it has served
a computer opponent. No league points, season progress or captured rewards are
awarded by this mode.
"""
import copy
import json
import uuid

import club_store as store
import match_protocol as match
import match_stats
import ziru_data

ACTIVE_KEY = 'local_vscom_active_match'
PROCESS_TOKEN = uuid.uuid4().hex
ABANDONED_PREFIX = 'local_vscom_abandoned_'


def _begin(conn):
    if conn.in_transaction:
        raise RuntimeError('Local VSCOM requires an independent transaction')
    conn.execute('BEGIN IMMEDIATE')


def _category(req, active):
    requested = req.get('match_category')
    recorded = active.get('match_category')
    if requested not in (None, '') and recorded not in (None, '') and requested != recorded:
        raise match.MatchError('local_match_category_changed')


def is_abandoned(conn, req):
    """An abandoned local ID cannot be reused as an untracked match request."""
    game_id = match.match_id(req, required=False)
    return bool(game_id is not None and store.get_meta(
        conn, ABANDONED_PREFIX + str(game_id), ''))


def prepare(conn, response, req, *, session_token=None):
    """Keep exact fetch retries, but let a new fetch leave an abandoned game.

The game cannot resume a running local match after its process exits. A new
gateway session, or a new opponent request after START, therefore prepares a
fresh game without inventing a result for the unfinished one. Before START,
same-category requests preserve the preselected opponent.
"""
    _begin(conn)
    active = None
    try:
        session = PROCESS_TOKEN if session_token is None else session_token
        if not isinstance(session, str) or not session or len(session) > 128:
            raise match.MatchError('invalid_local_match_session')
        if match.category_mode(req) not in (None, match.OTHER):
            raise match.MatchError('wrong_match_mode')
        rqid = req.get('rqid')
        identifiable_request = (type(rqid) is int or
                                isinstance(rqid, str) and 0 < len(rqid) <= 128)
        recorded_rqid = rqid if identifiable_request else None
        active = match.active_match(conn, match.LOCAL)
        if active:
            same_request = (identifiable_request and
                            active.get('opponent_request_session') == session and
                            active.get('opponent_request_rqid') == recorded_rqid)
            requested, recorded = req.get('match_category'), active.get('match_category')
            category_changed = (requested not in (None, '') and
                                recorded not in (None, '') and requested != recorded)
            if same_request:
                # Reusing an rqid for a different explicit category is not a
                # retry and must not silently replace the selected match.
                _category(req, active)
            elif active.get('started') or category_changed:
                abandoned = {
                    'game_id': active['game_id'], 'started': bool(active.get('started')),
                    'reason': ('new_session' if active.get('opponent_request_session') != session
                               else 'new_opponent_request'),
                    'match_category': recorded,
                }
                store.set_meta(conn, ABANDONED_PREFIX + str(active['game_id']),
                               json.dumps(abandoned))
                store.log_event(conn, 'local_vscom_abandoned', abandoned)
                active = None
            elif recorded in (None, '') and requested not in (None, ''):
                active['match_category'] = requested
        if active is None:
            opponent_side = match.side(response.get('side', 'AWAY'))
            active = {'game_id': match.next_game_id(conn), 'started': False,
                      'side': 'HOME' if opponent_side == 'AWAY' else 'AWAY',
                      'match_category': req.get('match_category'),
                      'opponent': copy.deepcopy(response)}
        active.update(opponent_request_session=session,
                      opponent_request_rqid=recorded_rqid)
        store.set_meta(conn, ACTIVE_KEY, json.dumps(active))
        # An exact retry uses the frozen opponent, even after the match starts.
        out = copy.deepcopy(active['opponent'])
        out.update(game_id=active['game_id'], rqid=req.get('rqid'),
                   msgid=response.get('msgid', 'CMD_GET_VSCOM_OPPONENT'), result='NOERR')
        match.remember_mode(conn, match.LOCAL)
        match.diagnostic(conn, 'prepared', req, match.LOCAL, active)
        conn.commit()
        return out
    except match.MatchError as exc:
        conn.rollback()
        return match.reject(conn, response.get('msgid', 'CMD_GET_VSCOM_OPPONENT'),
                            req, exc, match.LOCAL, active)
    except Exception:
        conn.rollback()
        raise


def start(conn, req, home_entries, team_id=None):
    _begin(conn)
    active = None
    try:
        if match.resolve_mode(conn, req, include_receipts=False) != match.LOCAL:
            raise match.MatchError('wrong_match_mode')
        active = match.active_match(conn, match.LOCAL)
        _category(req, active)
        user_side = match.side(req.get('side') or active['side'])
        snapshot = {}
        match_stats.snapshot_roster(snapshot, home_entries)
        current = snapshot.get('eligible_player_instances')
        if not current:
            raise match.MatchError('empty_match_squad')
        if active.get('started'):
            if user_side != active['side']:
                raise match.MatchError('started_match_side_changed')
            if active.get('eligible_player_instances') != current:
                raise match.MatchError('started_match_roster_changed')
        else:
            active.update(snapshot)
            active.update(started=True, side=user_side, user_id=req.get('user_id'),
                          team_id=team_id or store.club_team_id(conn),
                          user_name=store.get_meta(conn, 'user_name', ''))
            store.set_meta(conn, ACTIVE_KEY, json.dumps(active))
        match.diagnostic(conn, 'started', req, match.LOCAL, active)
        conn.commit()
        return True
    except match.MatchError as exc:
        conn.rollback()
        match.reject(conn, 'CMD_START_GAME', req, exc, match.LOCAL, active)
        return False
    except Exception:
        conn.rollback()
        raise


def _response(req, active, resolved):
    out = ziru_data.payload('vscom_game_result_template.json')
    out.update(msgid='CMD_GET_VSCOM_GAME_RESULT', rqid=req.get('rqid'), result='NOERR')
    end = out['game_end_result']
    result = end['match_result']
    normal, abnormal = match.end_reasons(match.LOCAL, resolved)
    result.update(game_res=resolved['result'], gameend_reason=normal,
                  abnormalend_reason=abnormal,
                  score_data={'home_score': resolved['home'], 'away_score': resolved['away']})
    player = result.get('player_data') or {}
    player.update(user_id=active.get('user_id') or req.get('user_id', 0),
                  team_id=active.get('team_id', 0), side=resolved['side'],
                  rating=0, oldrating=0, cont_win_num=0)
    if isinstance(player.get('user_info'), dict):
        player['user_info'].update(user_id=player['user_id'],
                                   user_name=active.get('user_name', ''))
    result['player_data'] = player
    extra = end.setdefault('extra_match_result', {})
    for key in ('league_match_result', 'event_match_result', 'season_match_result',
                'gameplayer_update_info', 'gameplayer_exp_list', 'campaign_pass_result',
                'get_trial_present', 'ml_event_match_result', 'user_compe_core_info'):
        extra[key] = []
    for key in ('achievement_notification', 'sheet_list', 'fame_notification', 'fameIdList',
                'revealed_bingo_link_card_list', 'empty_slot_info_list'):
        end[key] = []
    if isinstance(end.get('analyst_get_game_result_info'), dict):
        end['analyst_get_game_result_info'].update(advice_info_match_list=[],
                                                 analyse_json='[]', advice_json='[]')
    return out


def finish(conn, response, req):
    _begin(conn)
    active = None
    try:
        if match.resolve_mode(conn, req) != match.LOCAL:
            raise match.MatchError('wrong_match_mode')
        cached = match.replay(conn, match.LOCAL, req)
        if cached is not None:
            conn.commit()
            return cached
        active = match.active_match(conn, match.LOCAL)
        if not active or not active.get('started'):
            raise match.MatchError('result_before_start')
        _category(req, active)
        resolved = match.outcome(req, active)
        out = _response(req, active, resolved)
        match_stats.record_result(conn, match.LOCAL, req, active, resolved)
        match_stats.update_response(conn, match.LOCAL, match.match_id(req), out)
        match.save_receipt(conn, match.LOCAL, req, out, resolved)
        store.set_meta(conn, ACTIVE_KEY, 'null')
        match.diagnostic(conn, 'settled', req, match.LOCAL, active, resolved, response=out)
        conn.commit()
        return out
    except match.MatchError as exc:
        conn.rollback()
        return match.reject(conn, 'CMD_GET_VSCOM_GAME_RESULT', req, exc, match.LOCAL, active)
    except Exception:
        conn.rollback()
        raise
