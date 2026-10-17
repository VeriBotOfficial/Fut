"""Restarting an abandoned match must preserve the league, not freeze its squad."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_match_settlement as settlement_tests
import ai_division
import club_store as store
import match_protocol as protocol
import my_league
import ziru_data


class LeagueMatchResumeTest(unittest.TestCase):
    def setUp(self):
        # Reuse the settlement fixture's prevalidated gameplan seam: this
        # two-player save tests match identity/restart state, not scene entry.
        settlement_tests.MatchSettlementTest.setUp(self)
        settlement_tests.MatchSettlementTest.seed_season(self)
        self.session = 'league-session-before-restart'
        self.refs = [{'gameplayer_id': {'id': 12, 'serial': 1}},
                     {'gameplayer_id': {'id': 12, 'serial': 2}}]
        for row in self.refs:
            identity = row['gameplayer_id']
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (identity['id'], identity['serial'], json.dumps(row)))
        self.conn.commit()
        self.player_rows = self.players()

    def players(self):
        return list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial'))

    def category(self, mode):
        return 'LEAGUE_COM' if mode == protocol.AI else 'MYLEAGUE'

    def fetch(self, mode, rqid=2, category=None, cpu_level=None):
        request = {'user_id': 7, 'match_category': category or self.category(mode)}
        if rqid is not None:
            request['rqid'] = rqid
        if cpu_level is not None:
            request['cpu_level'] = cpu_level
        if mode == protocol.AI:
            return ai_division.prepare(self.conn,
                {'side': 'AWAY', 'team_data': {'base_team_id': 103}, 'result': 'NOERR'},
                request, session_token=self.session)
        return my_league.prepare(self.conn, request, session_token=self.session)

    def start(self, mode, gid, refs=None, category=None):
        request = {'game_id': gid, 'match_category': category or self.category(mode),
                   'user_id': 7, 'rqid': 3, 'side': 'HOME', 'squad_id': 1}
        rows = self.refs if refs is None else refs
        if mode == protocol.AI:
            return ai_division.start(self.conn, request, rows)
        return my_league.start(self.conn, {}, request, rows)['result'] == 'NOERR'

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.session += '-restart'

    def progress(self, mode):
        value = (ai_division if mode == protocol.AI else my_league).state(self.conn)
        value = copy.deepcopy(value)
        value.pop('active', None)
        value.pop('cpu_level', None)
        return value, store.season_points(self.conn)

    def abandoned(self, mode, gid):
        return store.get_meta(self.conn, 'efootxi_match_abandoned_' + mode + '_' + str(gid), '')

    def result_request(self, mode, gid):
        request = settlement_tests.MatchSettlementTest.request(self, gid, home=1, away=0,
                       match_category=self.category(mode))
        request['match_result']['myteam_player_list'] = [
            dict(copy.deepcopy(row), play_time=90, is_starting_member='YES', is_join_game='YES')
            for row in self.refs]
        request['goal_player_list'] = [copy.deepcopy(self.refs[0]['gameplayer_id'])]
        request['assist_player_list'] = [copy.deepcopy(self.refs[1]['gameplayer_id'])]
        return request

    def finish(self, mode, req):
        return settlement_tests.MatchSettlementTest.finish(self, mode, req)

    def assert_abandonment_has_no_result(self, mode, gid, progress):
        self.assertTrue(self.abandoned(mode, gid))
        self.assertEqual(self.progress(mode), progress)
        self.assertEqual(self.players(), self.player_rows)
        self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(mode, gid), ''), '')
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='efootxi_match_stats'").fetchone()[0], 0)

    def test_restart_new_fetch_can_start_reordered_squad_without_forfeit_or_progress(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, old))
                before = self.progress(mode)
                self.restart()
                new = self.fetch(mode)['game_id']
                self.assertNotEqual(old, new)
                self.assert_abandonment_has_no_result(mode, old, before)
                self.assertTrue(self.start(mode, new, list(reversed(self.refs))))
                active = protocol.active_match(self.conn, mode)
                self.assertEqual(active['eligible_player_instances'], [[12, 2], [12, 1]])
                self.assertFalse(self.start(mode, old))
                self.assertEqual(self.finish(mode, self.result_request(mode, old))['result'], 'ERR_INVALID_PARAM')
                self.assertEqual(protocol.active_match(self.conn, mode), active)
                # Even an unrelated category must not fall through to an old template.
                for include_receipts in (False, True):
                    with self.assertRaisesRegex(protocol.MatchError, 'abandoned_game_id'):
                        protocol.resolve_mode(self.conn, {'game_id': old, 'match_category': 'FRIEND'},
                                              include_receipts=include_receipts)

    def test_same_session_same_request_preserves_started_match_and_roster(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                first = self.fetch(mode)
                self.assertTrue(self.start(mode, first['game_id']))
                active = protocol.active_match(self.conn, mode)
                second = self.fetch(mode)
                self.assertEqual(first, second)
                self.assertEqual(protocol.active_match(self.conn, mode), active)
                self.assertFalse(self.abandoned(mode, first['game_id']))

    def test_new_request_after_start_replaces_identity_and_can_change_category(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, old))
                before = self.progress(mode)
                category = self.category(mode) + '_EVENT'
                new = self.fetch(mode, rqid=8, category=category)['game_id']
                self.assertNotEqual(new, old)
                self.assert_abandonment_has_no_result(mode, old, before)
                self.assertTrue(self.start(mode, new, list(reversed(self.refs)), category))

    def test_missing_rqid_cannot_claim_to_retry_a_started_match(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode, rqid=None)['game_id']
                self.assertTrue(self.start(mode, old))
                new = self.fetch(mode, rqid=None)['game_id']
                self.assertNotEqual(new, old)
                self.assertTrue(self.abandoned(mode, old))

    def test_unstarted_preview_reuses_identity_after_restart(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.restart()
                self.assertEqual(self.fetch(mode, rqid=8)['game_id'], old)
                active = protocol.active_match(self.conn, mode)
                self.assertFalse(active['started'])
                self.assertEqual(active['opponent_request_session'], self.session)
                self.assertFalse(self.abandoned(mode, old))

    def test_same_request_cannot_change_category(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, old))
                active = protocol.active_match(self.conn, mode)
                self.assertEqual(self.fetch(mode, category=self.category(mode) + '_EVENT')['result'],
                                 'ERR_INVALID_PARAM')
                self.assertEqual(protocol.active_match(self.conn, mode), active)
                self.assertFalse(self.abandoned(mode, old))

    def test_completed_receipt_replays_after_new_match_without_second_stats_or_reward(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, old))
                request = self.result_request(mode, old)
                first = self.finish(mode, request)
                self.assertEqual(first['result'], 'NOERR')
                stats = json.loads(store.get_meta(self.conn,
                    'efootxi_match_stats_state_' + mode + '_' + str(old)))
                self.assertEqual(stats['status'], 'applied')
                players, progress = self.players(), self.progress(mode)
                self.restart()
                new = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, new))
                active = protocol.active_match(self.conn, mode)
                self.assertNotEqual(old, new)
                self.assertEqual(self.finish(mode, request), first)
                self.assertEqual(self.players(), players)
                self.assertEqual(self.progress(mode), progress)
                self.assertEqual(protocol.active_match(self.conn, mode), active)

    def test_failed_new_opponent_rolls_back_abandonment(self):
        for mode in (protocol.AI, protocol.SEASON):
            with self.subTest(mode=mode):
                old = self.fetch(mode)['game_id']
                self.assertTrue(self.start(mode, old))
                active = protocol.active_match(self.conn, mode)
                target = 'normalize_opponent_team' if mode == protocol.AI else 'ml_get_vscom_opponent'
                with patch.object(ziru_data, target, side_effect=RuntimeError('fixture failed')):
                    with self.assertRaisesRegex(RuntimeError, 'fixture failed'):
                        self.fetch(mode, rqid=8)
                self.assertEqual(protocol.active_match(self.conn, mode), active)
                self.assertFalse(self.abandoned(mode, old))

    def test_started_same_request_cannot_change_myleague_difficulty(self):
        old = self.fetch(protocol.SEASON, cpu_level='EASY')['game_id']
        self.assertTrue(self.start(protocol.SEASON, old))
        active = protocol.active_match(self.conn, protocol.SEASON)
        self.assertEqual(self.fetch(protocol.SEASON, cpu_level='LEGEND')['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.SEASON), active)
        self.assertEqual(self.fetch(protocol.SEASON)['cpu_level'], 'EASY')
        self.assertEqual(protocol.active_match(self.conn, protocol.SEASON), active)
        self.assertFalse(self.abandoned(protocol.SEASON, old))
        fresh = self.fetch(protocol.SEASON, rqid=9, cpu_level='LEGEND')
        self.assertNotEqual(fresh['game_id'], old)
        self.assertEqual(fresh['cpu_level'], 'LEGEND')
        self.assertTrue(self.start(protocol.SEASON, fresh['game_id']))

    def test_unstarted_myleague_preview_can_still_change_difficulty(self):
        old = self.fetch(protocol.SEASON, cpu_level='EASY')['game_id']
        updated = self.fetch(protocol.SEASON, cpu_level='LEGEND')
        self.assertEqual(updated['game_id'], old)
        self.assertEqual(updated['cpu_level'], 'LEGEND')
        self.assertFalse(self.abandoned(protocol.SEASON, old))


if __name__ == '__main__':
    unittest.main()
