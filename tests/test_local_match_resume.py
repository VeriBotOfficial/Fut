"""A hard exit must not permanently lock a local game's old squad/category."""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_ai_start_roster as roster_tests
import test_local_vscom as local_tests
import club_store as store
import gate_logic
import local_vscom
import match_protocol as protocol
import ziru_data


class LocalMatchResumeTest(unittest.TestCase):
    def setUp(self):
        roster_tests.AiStartRosterTest.setUp(self)
        self.session = 'test-session-before-restart'
        self.opponent = {'side': 'AWAY', 'team_data': {'base_team_id': 103},
                         'player_list': [], 'result': 'NOERR'}
        mocked = patch.object(ziru_data, 'payload',
                              side_effect=lambda name: copy.deepcopy(local_tests.result_template()))
        mocked.start()
        self.addCleanup(mocked.stop)
        self.player_rows = list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial'))
        self.original_ai = store.get_meta(self.conn, 'ai_division_active_match')

    def fetch(self, rqid=2, category='FRIEND', opponent=None):
        request = {'user_id': 7, 'match_category': category}
        if rqid is not None:
            request['rqid'] = rqid
        return local_vscom.prepare(self.conn, self.opponent if opponent is None else opponent,
                                   request, session_token=self.session)

    def start(self, gid, category='FRIEND', refs=None):
        return local_vscom.start(self.conn, {'game_id': gid, 'match_category': category,
                                             'user_id': 7, 'rqid': 3},
                                 self.refs if refs is None else refs, team_id=101)

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.gate = gate_logic.GateLogic({}, conn=self.conn)
        self.session = 'test-session-after-restart'

    def result_request(self, gid, category='FRIEND'):
        first = copy.deepcopy(self.refs[0]['gameplayer_id'])
        return {'rqid': 4, 'user_id': 7, 'match_category': category,
                'match_result': {'game_id': gid, 'gameend_reason': 'NORMAL_END',
                                 'abnormalend_reason': 'NOERR', 'myteam_player_list': [{
                    'gameplayer_id': first, 'play_time': 90,
                    'is_starting_member': 'YES', 'is_join_game': 'YES'}]},
                'goal_player_list': [first], 'score': [{'score_1st': 1}, {'score_1st': 0}]}

    def assert_no_invented_result(self, gid):
        self.assertEqual(list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial')),
                         self.player_rows)
        self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(protocol.LOCAL, gid), ''), '')
        self.assertEqual(store.get_meta(self.conn, 'ai_division_active_match'), self.original_ai)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='efootxi_match_stats'").fetchone()[0], 0)

    def test_hard_restart_new_fetch_allows_reordered_squad_and_different_category(self):
        old = self.fetch()['game_id']
        self.assertTrue(self.start(old))
        self.restart()
        new = self.fetch(category='EVENT_COM')['game_id']
        self.assertNotEqual(new, old)
        self.assertTrue(local_vscom.is_abandoned(self.conn, {'game_id': old}))
        self.assert_no_invented_result(old)
        self.assertTrue(self.start(new, category='EVENT_COM', refs=list(reversed(self.refs))))
        expected = [[row['gameplayer_id']['id'], row['gameplayer_id']['serial']]
                    for row in reversed(self.refs)]
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL)['eligible_player_instances'], expected)
        before = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertFalse(self.start(old))
        self.assertEqual(self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', self.result_request(old))['result'],
                         'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)

    def test_same_session_same_fetch_rqid_preserves_started_id_and_opponent(self):
        first = self.fetch()
        self.assertTrue(self.start(first['game_id']))
        active = protocol.active_match(self.conn, protocol.LOCAL)
        different_opponent = copy.deepcopy(self.opponent)
        different_opponent['team_data']['base_team_id'] = 999
        second = self.fetch(opponent=different_opponent)
        self.assertEqual(second, first)
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), active)
        self.assertFalse(local_vscom.is_abandoned(self.conn, {'game_id': first['game_id']}))

    def test_new_fetch_rqid_after_start_creates_new_game_without_forfeit(self):
        old = self.fetch()['game_id']
        self.assertTrue(self.start(old))
        new = self.fetch(rqid=5)['game_id']
        self.assertNotEqual(new, old)
        self.assert_no_invented_result(old)
        self.assertTrue(self.start(new, refs=list(reversed(self.refs))))
        self.assertEqual(local_vscom.finish(self.conn, {}, self.result_request(old))['result'],
                         'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL)['game_id'], new)

    def test_missing_rqid_does_not_pretend_to_be_a_retry_after_start(self):
        old = self.fetch(rqid=None)['game_id']
        self.assertTrue(self.start(old))
        new = self.fetch(rqid=None)['game_id']
        self.assertNotEqual(new, old)
        self.assert_no_invented_result(old)

    def test_unstarted_same_category_preserves_preselection_across_restart(self):
        old = self.fetch()['game_id']
        self.restart()
        new = self.fetch(rqid=8)['game_id']
        self.assertEqual(new, old)
        active = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertFalse(active['started'])
        self.assertEqual(active['opponent_request_session'], self.session)
        self.assertEqual(active['opponent_request_rqid'], 8)
        self.assertFalse(local_vscom.is_abandoned(self.conn, {'game_id': old}))

    def test_unstarted_new_explicit_category_can_replace_preselection(self):
        old = self.fetch()['game_id']
        new = self.fetch(rqid=5, category='EVENT_COM')['game_id']
        self.assertNotEqual(new, old)
        self.assertTrue(local_vscom.is_abandoned(self.conn, {'game_id': old}))
        self.assert_no_invented_result(old)
        self.assertTrue(self.start(new, category='EVENT_COM'))

    def test_same_request_id_with_changed_category_rejects_without_losing_game(self):
        old = self.fetch()['game_id']
        self.assertTrue(self.start(old))
        before = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertEqual(self.fetch(category='EVENT_COM')['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)
        self.assertFalse(local_vscom.is_abandoned(self.conn, {'game_id': old}))

    def test_completed_receipt_still_replays_after_restart_and_new_match(self):
        old = self.fetch()['game_id']
        self.assertTrue(self.start(old))
        request = self.result_request(old)
        first = local_vscom.finish(self.conn, {}, request)
        self.assertEqual(first['result'], 'NOERR')
        saved_players = list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial'))
        self.restart()
        new = self.fetch(rqid=9)['game_id']
        self.assertTrue(self.start(new))
        before = protocol.active_match(self.conn, protocol.LOCAL)
        replayed = self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', request)
        self.assertEqual(replayed, first)
        self.assertNotEqual(new, old)
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)
        self.assertEqual(list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial')),
                         saved_players)

    def test_failed_replacement_rolls_back_abandonment_and_retains_old_match(self):
        old = self.fetch()['game_id']
        self.assertTrue(self.start(old))
        before = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertEqual(self.fetch(rqid=5, opponent={'side': 'INVALID'})['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)
        self.assertFalse(local_vscom.is_abandoned(self.conn, {'game_id': old}))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events WHERE kind='local_vscom_abandoned'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
