"""Settlement tests use disposable SQLite saves, never the released seed DB."""
import copy
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
HOST_DEPS = REPO.parents[1] / 'analysis/v120-investigation/draw-skills/host-deps'
if HOST_DEPS.is_dir():
    sys.path.insert(0, str(HOST_DEPS))

import ai_division
import club_store as store
import match_protocol as protocol
import my_league
import myleague_plan
import ziru_data


def result_template(mode):
    """Compact fixtures retain the shapes/enums of shipped captured replies."""
    normal, abnormal = ('NORMAL', 'NORMAL') if mode == protocol.AI else ('NORMAL_END', 'NOERR')
    return {'msgid': 'CMD_GET_VSCOM_GAME_RESULT', 'result': 'NOERR', 'rqid': 1,
            'game_end_result': {
                'match_result': {'game_res': 'DRAW', 'gameend_reason': normal,
                                 'abnormalend_reason': abnormal,
                                 'score_data': {'home_score': 0, 'away_score': 0},
                                 'player_data': {'side': 'HOME', 'team_id': 999,
                                                 'user_info': {'user_id': 999}}},
                'extra_match_result': {
                    'league_match_result': {'league_info': {}, 'is_invalid': 'NO'} if mode == protocol.AI else [],
                    'season_match_result': {} if mode == protocol.SEASON else [],
                    'campaign_pass_result': {} if mode == protocol.SEASON else []}}}


class MatchSettlementTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'local-club.sqlite3'
        self.conn = sqlite3.connect(self.db_path)
        self.conn.executescript(store.SCHEMA)
        self.addCleanup(lambda: self.conn.close())
        store.set_meta(self.conn, 'base_team_id', '101')
        store.set_meta(self.conn, 'user_id', '7')
        store.set_meta(self.conn, 'user_name', 'test')
        coach = {'coach_id': {'id': 80, 'serial': 1}, 'short_counter': 88}
        self.conn.execute('insert into coaches values(?,?,?)', (80, 1, json.dumps(coach)))
        squad = {'squad_id': 1, 'squad_data': {'coach': {'coach_id': coach['coach_id']},
                 'squad_gameplan': {'formation': [{'team_style': 1}]},
                 'player_list': [{'gameplayer_id': {'id': 12, 'serial': 1}}]}}
        self.conn.execute('insert into squads values(?,?)', (1, json.dumps(squad)))
        self.conn.commit()
        fixtures = {
            'vscom_game_result_template.json': result_template(protocol.AI),
            'ml_game_result.json': result_template(protocol.SEASON),
            'ml_get_vscom_opponent.json': {'side': 'AWAY', 'team_data': {}, 'result': 'NOERR'},
            'ml_start_game.json': {'msgid': 'CMD_START_GAME', 'squad_data_home': {},
                                  'uniform_list_home': {}, 'uniform_list_away': {}}}
        self.patches = [
            # These one/few-player fixtures isolate settlement, receipts and
            # counters after scene-entry validation. Native 11-player/index
            # validation runs unmocked in the dedicated My League plan and
            # full-roster integration suites.
            patch.object(myleague_plan, 'gameplan_for_entries',
                         side_effect=lambda saved, entries: copy.deepcopy(saved['squad_gameplan'])),
            patch.object(ziru_data, 'payload', side_effect=lambda name: copy.deepcopy(fixtures[name])),
            patch.object(ziru_data, 'normalize_opponent_team', side_effect=lambda value: value),
            patch.object(ziru_data, 'team_label', return_value='Opponent'),
            patch.object(ziru_data, 'team_names', return_value={}),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def seed_season(self, side='HOME'):
        home, away = (101, 103) if side == 'HOME' else (103, 101)
        value = {'team_id': 101, 'competition_id': 113, 'fixture': 1, 'played': {},
                 'completed': False, 'active': None, 'cpu_level': 'NORMAL',
                 'season': 1 + (my_league.state(self.conn) or {}).get('season', 0),
                 'schedule_v2': [
                     {'fixture': 1, 'match_list': [{'home_team_id': home, 'away_team_id': away}]},
                     {'fixture': 2, 'match_list': [{'home_team_id': away, 'away_team_id': home}]}]}
        my_league.save(self.conn, value)
        self.conn.commit()

    def prepare(self, mode, side='HOME', start=True):
        if mode == protocol.AI:
            response = {'side': 'AWAY' if side == 'HOME' else 'HOME', 'team_data': {'base_team_id': 103}}
            prepared = ai_division.prepare(self.conn, response, {'match_category': 'LEAGUE_COM'})
        else:
            self.seed_season(side)
            prepared = my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        game_id = prepared['game_id']
        if start:
            request = {'game_id': str(game_id), 'side': side, 'user_id': 7, 'rqid': 10}
            if mode == protocol.AI:
                self.assertTrue(ai_division.start(self.conn, request))
            else:
                response = my_league.start(self.conn, {}, request,
                                          [{'gameplayer_id': {'id': 12, 'serial': 1}}])
                self.assertEqual(response['result'], 'NOERR')
        return game_id

    def request(self, game_id, home=3, away=1, **extra):
        request = {'msgid': 'CMD_GET_VSCOM_GAME_RESULT', 'rqid': 20, 'user_id': 7,
                   'match_result': {'game_id': game_id, 'gameend_reason': 'NORMAL_END',
                                    'abnormalend_reason': 'NOERR'},
                   'score': [{'score_1st': home, 'score_2nd': 0},
                             {'score_1st': away, 'score_2nd': 0}]}
        request.update(extra)
        return request

    def finish(self, mode, req):
        # Deliberately pass the wrong mode's capture. Handlers must pick their
        # own response schema rather than leaking historical command templates.
        response = result_template(protocol.SEASON if mode == protocol.AI else protocol.AI)
        return (ai_division if mode == protocol.AI else my_league).finish(self.conn, response, req)

    def last_diagnostic(self):
        row = self.conn.execute("SELECT detail FROM events WHERE kind='efootxi_match' ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row[0])

    def test_home_and_away_win_draw_loss_both_modes(self):
        for mode in (protocol.AI, protocol.SEASON):
            for side in ('HOME', 'AWAY'):
                for ours, theirs, expected in ((3, 1, 'WIN'), (2, 2, 'DRAW'), (0, 2, 'LOSE')):
                    with self.subTest(mode=mode, side=side, expected=expected):
                        protocol.initialize_test_seed(self.conn, new_test_seed=True)
                        store.set_meta(self.conn, 'season_point', '0')
                        self.conn.commit()
                        game_id = self.prepare(mode, side)
                        home, away = (ours, theirs) if side == 'HOME' else (theirs, ours)
                        out = self.finish(mode, self.request(game_id, home, away))
                        self.assertEqual(out['result'], 'NOERR')
                        result = out['game_end_result']['match_result']
                        self.assertEqual(result['game_res'], expected)
                        self.assertEqual(result['score_data'], {'home_score': home, 'away_score': away})
                        self.assertEqual(result['player_data']['side'], side)
                        self.assertEqual(result['player_data']['team_id'], 101)
                        if mode == protocol.AI:
                            state = ai_division.state(self.conn)
                            self.assertEqual(state['points'], {'WIN': 3, 'DRAW': 1, 'LOSE': 0}[expected])
                            self.assertEqual(state['matches'], 1)
                        else:
                            state = my_league.state(self.conn)
                            row = next(r for r in my_league.ranking(state) if r['team_id'] == 101)
                            self.assertEqual(row['goals'], ours)
                            self.assertEqual(row['goals_against'], theirs)
                            self.assertEqual(row['winning_points'], {'WIN': 3, 'DRAW': 1, 'LOSE': 0}[expected])
                            self.assertEqual(store.season_points(self.conn), {'WIN': 300, 'DRAW': 100, 'LOSE': 0}[expected])

    def test_normal_end_does_not_need_internet(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                game_id = self.prepare(mode)
                with patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')):
                    out = self.finish(mode, self.request(game_id))
                result = out['game_end_result']['match_result']
                self.assertEqual(result['game_res'], 'WIN')
                self.assertEqual((result['gameend_reason'], result['abnormalend_reason']),
                                         ('NORMAL_END', 'NOERR'))

    def test_explicit_row_sides_are_used_without_assuming_list_order(self):
        game_id = self.prepare(protocol.SEASON, 'AWAY')
        request = self.request(game_id)
        request['score'] = [{'side': 'AWAY', 'score_1st': 3}, {'side': 'HOME', 'score_1st': 1}]
        out = self.finish(protocol.SEASON, request)
        result = out['game_end_result']['match_result']
        self.assertEqual(result['game_res'], 'WIN')
        self.assertEqual(result['score_data'], {'home_score': 1, 'away_score': 3})
        self.assertEqual(self.last_diagnostic()['settlement']['score_source'], 'explicit_row_side')

    def test_unlabelled_score_order_is_not_silently_reversed(self):
        game_id = self.prepare(protocol.SEASON, 'AWAY')
        out = self.finish(protocol.SEASON, self.request(game_id, 3, 1))
        self.assertEqual(out['game_end_result']['match_result']['game_res'], 'LOSE')
        self.assertEqual(self.last_diagnostic()['settlement']['score_source'], 'legacy_stadium_order_unverified')

    def test_malformed_scores_leave_match_pending_and_can_be_retried(self):
        bad_scores = [None, [], [{'score_1st': '3'}, {'score_1st': 1}],
                      [{'score_1st': True}, {'score_1st': 1}],
                      [{'score_1st': -1}, {'score_1st': 0}],
                      [{'side': 'HOME', 'score_1st': 3}, {'side': 'HOME', 'score_1st': 1}]]
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode)
            for scores in bad_scores:
                with self.subTest(mode=mode, score=scores):
                    request = self.request(game_id, score=scores)
                    out = self.finish(mode, request)
                    self.assertEqual(out['result'], 'ERR_INVALID_PARAM')
                    self.assertIsNotNone(protocol.active_match(self.conn, mode))
                    self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(mode, game_id), ''), '')
            out = self.finish(mode, self.request(game_id))
            self.assertEqual(out['game_end_result']['match_result']['game_res'], 'WIN')

    def test_unknown_end_reason_never_turns_into_loss(self):
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode)
            request = self.request(game_id)
            request['match_result']['abnormalend_reason'] = 'NETWORK_DISCONNECTED'
            out = self.finish(mode, request)
            self.assertEqual(out['result'], 'ERR_INVALID_PARAM')
            self.assertIsNotNone(protocol.active_match(self.conn, mode))
            self.assertEqual(self.last_diagnostic()['error'], 'unconfirmed_match_end')
            out = self.finish(mode, self.request(game_id))
            self.assertEqual(out['game_end_result']['match_result']['game_res'], 'WIN')

    def test_explicit_give_up_without_score_records_forfeit_for_both_sides(self):
        for mode in (protocol.AI, protocol.SEASON):
            for side in ('HOME', 'AWAY'):
                game_id = self.prepare(mode, side)
                request = self.request(game_id, intentional_give_up='YES')
                request['match_result']['abnormalend_reason'] = 'NORMAL_GIVEUP'
                del request['score']
                out = self.finish(mode, request)
                result = out['game_end_result']['match_result']
                self.assertEqual(result['game_res'], 'LOSE')
                self.assertEqual(result['abnormalend_reason'], 'NORMAL_GIVEUP')
                self.assertEqual(result['score_data'], {'home_score': 0, 'away_score': 3} if side == 'HOME'
                                 else {'home_score': 3, 'away_score': 0})

    def test_normal_giveup_reason_also_forfeits_leading_score(self):
        game_id = self.prepare(protocol.AI)
        request = self.request(game_id, 10, 0)
        request['match_result']['abnormalend_reason'] = 'NORMAL_GIVEUP'
        out = self.finish(protocol.AI, request)
        self.assertEqual(out['game_end_result']['match_result']['game_res'], 'LOSE')

    def test_receipt_survives_restart_and_only_settles_once(self):
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode)
            request = self.request(game_id)
            first = self.finish(mode, request)
            state_before = store.get_meta(self.conn, ai_division.STATE_KEY if mode == protocol.AI else my_league.KEY)
            points_before = store.season_points(self.conn)
            self.conn.close()
            self.conn = sqlite3.connect(self.db_path)
            request['rqid'] = 25
            request['match_result']['game_id'] = str(game_id)
            second = self.finish(mode, request)
            expected = copy.deepcopy(first); expected['rqid'] = 25
            self.assertEqual(second, expected)
            self.assertEqual(state_before, store.get_meta(self.conn, ai_division.STATE_KEY if mode == protocol.AI else my_league.KEY))
            self.assertEqual(store.season_points(self.conn), points_before)

    def test_conflicting_result_retry_is_not_silently_accepted(self):
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode)
            first = self.finish(mode, self.request(game_id, 3, 1))
            conflicting = self.finish(mode, self.request(game_id, 0, 5))
            self.assertEqual(conflicting['result'], 'ERR_INVALID_PARAM')
            self.assertEqual(self.last_diagnostic()['error'], 'conflicting_result_retry')
            self.assertEqual(json.loads(store.get_meta(self.conn, protocol.receipt_key(mode, game_id))), first)

    def test_old_receipt_does_not_consume_new_active_match(self):
        old_id = self.prepare(protocol.AI)
        self.finish(protocol.AI, self.request(old_id))
        new_id = self.prepare(protocol.AI)
        self.assertNotEqual(old_id, new_id)
        self.finish(protocol.AI, self.request(old_id))
        self.assertEqual(protocol.active_match(self.conn, protocol.AI)['game_id'], new_id)
        self.assertEqual(ai_division.state(self.conn)['matches'], 1)

    def test_cross_mode_and_category_conflicts_preserve_both_active_matches(self):
        ai_id = self.prepare(protocol.AI)
        season_id = self.prepare(protocol.SEASON)
        self.assertNotEqual(ai_id, season_id)
        self.assertEqual(protocol.resolve_mode(self.conn, self.request(ai_id)), protocol.AI)
        self.assertEqual(protocol.resolve_mode(self.conn, self.request(season_id)), protocol.SEASON)
        self.assertFalse(my_league.handles_result(self.conn, self.request(ai_id)))
        self.assertEqual(self.finish(protocol.SEASON, self.request(ai_id))['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.finish(protocol.AI, self.request(season_id))['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.finish(protocol.AI, self.request(ai_id, match_category='MYLEAGUE'))['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.AI)['game_id'], ai_id)
        self.assertEqual(protocol.active_match(self.conn, protocol.SEASON)['game_id'], season_id)

    def test_missing_category_uses_id_and_current_menu_not_old_season_presence(self):
        self.prepare(protocol.SEASON)
        ai_division.info(self.conn, {})
        self.assertEqual(protocol.opponent_mode(self.conn, {}), protocol.AI)
        ai_id = self.prepare(protocol.AI, start=False)
        self.assertTrue(ai_division.start(self.conn, {'game_id': ai_id}))
        self.assertEqual(protocol.resolve_mode(self.conn, {'game_id': ai_id}), protocol.AI)
        with self.assertRaises(protocol.MatchError):
            protocol.resolve_mode(self.conn, {'game_id': 12345})

    def test_unknown_game_does_not_return_historical_result_template(self):
        for mode in (protocol.AI, protocol.SEASON):
            out = self.finish(mode, self.request(12345))
            self.assertEqual(out['result'], 'ERR_INVALID_PARAM')
            self.assertNotIn('game_end_result', out)

    def test_untracked_start_paths_are_not_blocked_by_tracked_mode_routing(self):
        self.assertEqual(protocol.resolve_mode(self.conn, {'game_id': 12345}, include_receipts=False), protocol.OTHER)
        self.assertEqual(protocol.resolve_mode(self.conn, {'game_id': 12345, 'match_category': 'FRIEND'}, include_receipts=False), protocol.OTHER)
        self.assertEqual(protocol.resolve_mode(self.conn, {}, include_receipts=False), protocol.OTHER)
        self.assertEqual(protocol.opponent_mode(self.conn, {}), protocol.OTHER)
        with self.assertRaises(protocol.MatchError):
            protocol.resolve_mode(self.conn, {'game_id': 12345, 'match_category': 'LEAGUE_COM'}, include_receipts=False)

    def test_result_before_start_does_not_consume_match(self):
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode, start=False)
            out = self.finish(mode, self.request(game_id))
            self.assertEqual(out['result'], 'ERR_INVALID_PARAM')
            self.assertFalse(protocol.active_match(self.conn, mode)['started'])

    def test_nested_null_id_uses_valid_top_level_id(self):
        game_id = self.prepare(protocol.AI)
        request = self.request(None)
        request['game_id'] = str(game_id)
        self.assertEqual(self.finish(protocol.AI, request)['result'], 'NOERR')

    def test_malformed_conflicting_and_precision_losing_ids_are_rejected(self):
        game_id = self.prepare(protocol.AI)
        for value in (True, 0, -1, float(game_id), 'bad', 2**63):
            with self.subTest(value=value):
                self.assertEqual(self.finish(protocol.AI, self.request(value))['result'], 'ERR_INVALID_PARAM')
        req = self.request(game_id)
        req['game_id'] = game_id + 1
        self.assertEqual(self.finish(protocol.AI, req)['result'], 'ERR_INVALID_PARAM')
        self.assertIsNotNone(protocol.active_match(self.conn, protocol.AI))

    def test_receipt_failure_rolls_back_scores_and_preserves_retriable_match(self):
        for mode in (protocol.AI, protocol.SEASON):
            game_id = self.prepare(mode)
            state_key = ai_division.STATE_KEY if mode == protocol.AI else my_league.KEY
            old_state = store.get_meta(self.conn, state_key)
            old_points = store.season_points(self.conn)
            with patch.object(protocol, 'save_receipt', side_effect=sqlite3.OperationalError('disk full')):
                with self.assertRaises(sqlite3.OperationalError):
                    self.finish(mode, self.request(game_id))
            self.assertEqual(store.get_meta(self.conn, state_key), old_state)
            self.assertEqual(store.season_points(self.conn), old_points)
            self.assertIsNotNone(protocol.active_match(self.conn, mode))
            self.assertEqual(self.finish(mode, self.request(game_id))['result'], 'NOERR')

    def test_seed_cleanup_requires_explicit_copy_flag_and_preserves_players(self):
        game_id = self.prepare(protocol.SEASON)
        self.conn.execute("INSERT INTO gameplayers(id,serial,json) VALUES(1,1,'{}')")
        self.conn.commit()
        with self.assertRaises(protocol.MatchError):
            protocol.initialize_test_seed(self.conn)
        self.assertEqual(protocol.active_match(self.conn, protocol.SEASON)['game_id'], game_id)
        protocol.initialize_test_seed(self.conn, new_test_seed=True)
        self.assertIsNone(protocol.active_match(self.conn, protocol.SEASON))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM gameplayers').fetchone()[0], 1)
        self.assertEqual(store.get_meta(self.conn, 'base_team_id'), '101')

    def test_diagnostics_contain_settlement_fields_without_account_credentials(self):
        game_id = self.prepare(protocol.AI)
        request = self.request(game_id, token='secret', password='secret', device_id='secret')
        self.finish(protocol.AI, request)
        raw = self.conn.execute("SELECT detail FROM events WHERE kind='efootxi_match' ORDER BY id DESC LIMIT 1").fetchone()[0]
        diagnostic = json.loads(raw)
        self.assertEqual(diagnostic['game_id'], game_id)
        self.assertEqual(diagnostic['settlement']['result'], 'WIN')
        self.assertNotIn('secret', raw)

    def test_current_native_normal_enums_replace_reference_apk_template_values(self):
        # The reference APK's NORMAL/NORMAL pair is not a contract for this
        # client's native consumer. Its valid normal pair is shared by modes.
        for mode in (protocol.AI, protocol.SEASON, protocol.LOCAL):
            self.assertEqual(protocol.end_reasons(mode, {'gave_up': False}),
                             ('NORMAL_END', 'NOERR'))

    def test_false_or_zero_end_reason_is_not_silently_treated_as_normal(self):
        for mode in (protocol.AI, protocol.SEASON):
            for value in (False, 0, []):
                with self.subTest(mode=mode, reason=value):
                    game_id = self.prepare(mode)
                    req = self.request(game_id)
                    req['match_result']['abnormalend_reason'] = value
                    before = store.get_meta(self.conn, ai_division.STATE_KEY if mode == protocol.AI else my_league.KEY)
                    response = self.finish(mode, req)
                    self.assertEqual(response['result'], 'ERR_INVALID_PARAM')
                    self.assertEqual(before, store.get_meta(self.conn, ai_division.STATE_KEY if mode == protocol.AI else my_league.KEY))
                    self.assertIsNotNone(protocol.active_match(self.conn, mode))

    def test_invalid_terminal_reason_is_logged_and_keeps_retryable_match(self):
        game_id = self.prepare(protocol.AI)
        req = self.request(game_id, 4, 0, is_invalid='YES', invalid_reason='UNVERIFIED_NATIVE_REASON')
        req['match_result']['abnormalend_reason'] = 'UNVERIFIED_NATIVE_REASON'
        req['private_token'] = 'do-not-log-this-token'
        response = self.finish(protocol.AI, req)
        self.assertEqual(response['result'], 'ERR_INVALID_PARAM')
        self.assertNotIn('game_end_result', response)
        diagnostic = self.last_diagnostic()
        self.assertEqual(diagnostic['reported_validity']['request.is_invalid'], 'YES')
        self.assertEqual(diagnostic['reported_validity']['match_result.abnormalend_reason'], 'UNVERIFIED_NATIVE_REASON')
        self.assertNotIn('do-not-log-this-token', json.dumps(diagnostic))
        self.assertIsNotNone(protocol.active_match(self.conn, protocol.AI))
        corrected = self.finish(protocol.AI, self.request(game_id, 4, 0))
        self.assertEqual(corrected['game_end_result']['match_result']['game_res'], 'WIN')
        self.assertEqual(corrected['game_end_result']['extra_match_result']['league_match_result']['is_invalid'], 'NO')


if __name__ == '__main__':
    unittest.main()
