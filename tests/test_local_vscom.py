"""Local ordinary AI routing, transaction boundaries and persistent receipts.

The stats adapter is deliberately mocked here; its wire schema and counter
math have separate tests. These checks establish which matches may call it.
"""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_ai_start_roster as roster_tests
import ai_division
import club_store as store
import gate_logic
import local_vscom
import match_protocol as protocol
import match_stats
import opponent_squad
import ziru_data


def result_template():
    return {'game_end_result': {
        'match_result': {'game_res': 'LOSE', 'player_data': {
            'user_id': 999, 'team_id': 999, 'user_info': {'user_id': 999}}},
        'extra_match_result': {'league_match_result': {'captured': True},
                               'season_match_result': {'captured': True}}}}


class LocalVscomTest(unittest.TestCase):
    def setUp(self):
        roster_tests.AiStartRosterTest.setUp(self)
        self.original_ai = store.get_meta(self.conn, 'ai_division_active_match')
        self.original_season = json.dumps({'fixture': 8, 'active': None})
        store.set_meta(self.conn, 'my_league_state_v1', self.original_season)
        store.set_meta(self.conn, 'season_point', '123')
        self.conn.commit()
        patches = [
            patch.object(ziru_data, 'opponent_squad', return_value={
                'game_id': 111, 'side': 'AWAY', 'team_data': {'base_team_id': 103},
                'player_list': [], 'result': 'NOERR'}),
            patch.object(opponent_squad, 'build', return_value=None),
            patch.object(ziru_data, 'payload', side_effect=lambda name: copy.deepcopy(result_template())),
            patch.object(match_stats, 'record_result', create=True),
        ]
        for item in patches:
            value = item.start()
            self.addCleanup(item.stop)
        self.record = value

    def prepare(self, category='FRIEND'):
        response = self.gate.handle('CMD_GET_VSCOM_OPPONENT', {
            'rqid': 2, 'user_id': 7, 'match_category': category})
        self.assertEqual(response['result'], 'NOERR', response)
        self.assertNotEqual(response['game_id'], 111)
        return response['game_id']

    def start(self, gid, **extra):
        request = dict(game_id=gid, rqid=3, user_id=7, match_category='FRIEND', squad_id=1)
        request.update(extra)
        return self.gate.handle('CMD_START_GAME', request)

    def result_request(self, gid, **extra):
        request = {'rqid': 4, 'user_id': 7, 'match_category': 'FRIEND',
                   'match_result': {'game_id': gid, 'gameend_reason': 'NORMAL_END',
                                    'abnormalend_reason': 'NOERR'},
                   'score': [{'score_1st': 2}, {'score_1st': 1}]}
        request.update(extra)
        return request

    def finish(self, gid, **extra):
        return self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', self.result_request(gid, **extra))

    def test_only_prepared_local_opponent_id_can_route_to_statistics(self):
        request = self.result_request(999999)
        self.assertEqual(protocol.resolve_mode(self.conn, request), protocol.OTHER)
        self.assertEqual(local_vscom.finish(self.conn, {}, request)['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(local_vscom.start(self.conn, dict(game_id=999999, match_category='FRIEND'), self.refs))
        self.assertIsNone(protocol.active_match(self.conn, protocol.LOCAL))
        self.record.assert_not_called()
        gid = self.prepare()
        self.assertEqual(protocol.resolve_mode(self.conn, self.result_request(gid)), protocol.LOCAL)
        with self.assertRaises(protocol.MatchError):
            protocol.resolve_mode(self.conn, self.result_request(gid, match_category='LEAGUE_COM'))

    def test_gate_settlement_calls_adapter_once_and_receipt_survives_restart(self):
        gid = self.prepare()
        started = self.start(gid)
        self.assertEqual(started['result'], 'NOERR')
        active = protocol.active_match(self.conn, protocol.LOCAL)
        sent = started['squad_data_home']['player_list']
        self.assertEqual(active['eligible_player_instances'],
                         [[row['gameplayer_id']['id'], row['gameplayer_id']['serial']] for row in sent])
        first = self.finish(gid)
        self.assertEqual(first['result'], 'NOERR')
        self.assertEqual(first['game_end_result']['match_result']['game_res'], 'WIN')
        self.record.assert_called_once()
        args = self.record.call_args.args
        self.assertEqual(args[:2], (self.conn, protocol.LOCAL))
        self.assertEqual(args[3], active)
        self.assertTrue(store.get_meta(self.conn, protocol.receipt_key(protocol.LOCAL, gid)))
        self.assertIsNone(protocol.active_match(self.conn, protocol.LOCAL))
        self.assertEqual(store.get_meta(self.conn, 'ai_division_active_match'), self.original_ai)
        self.assertEqual(store.get_meta(self.conn, 'my_league_state_v1'), self.original_season)
        self.assertEqual(store.season_points(self.conn), 123)
        extra = first['game_end_result']['extra_match_result']
        self.assertEqual(extra['league_match_result'], [])
        self.assertEqual(extra['season_match_result'], [])
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.gate = gate_logic.GateLogic({}, conn=self.conn)
        second = self.finish(gid, rqid=25)
        expected = copy.deepcopy(first)
        expected['rqid'] = 25
        self.assertEqual(second, expected)
        self.record.assert_called_once()

    def test_prepared_but_not_started_match_is_not_counted(self):
        gid = self.prepare()
        self.assertEqual(self.finish(gid)['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(protocol.active_match(self.conn, protocol.LOCAL)['started'])
        self.record.assert_not_called()

    def test_same_match_retries_keep_roster_and_opponent_unchanged(self):
        gid = self.prepare()
        self.assertEqual(self.start(gid)['result'], 'NOERR')
        before = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertEqual(self.prepare(), gid)
        self.assertEqual(self.start(gid)['result'], 'NOERR')
        self.assertEqual(self.start(gid, player_list_home=list(reversed(self.refs)))['result'],
                         'ERR_INVALID_PARAM')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)

    def test_receipt_failure_rolls_back_stats_and_retains_active_match(self):
        gid = self.prepare()
        self.start(gid)
        def staged_stats(conn, *args):
            self.assertTrue(conn.in_transaction)
            store.set_meta(conn, 'test_stats_written', '1')
        self.record.side_effect = staged_stats
        with patch.object(protocol, 'save_receipt', side_effect=RuntimeError('disk failure')):
            with self.assertRaisesRegex(RuntimeError, 'disk failure'):
                self.finish(gid)
        self.assertEqual(store.get_meta(self.conn, 'test_stats_written', ''), '')
        self.assertTrue(protocol.active_match(self.conn, protocol.LOCAL)['started'])
        self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(protocol.LOCAL, gid), ''), '')
        self.assertEqual(self.finish(gid)['result'], 'NOERR')
        self.assertEqual(store.get_meta(self.conn, 'test_stats_written'), '1')

    def test_new_match_id_and_old_receipt_cannot_consume_new_match(self):
        old = self.prepare()
        self.start(old)
        self.finish(old)
        new = self.prepare()
        self.assertNotEqual(old, new)
        before = protocol.active_match(self.conn, protocol.LOCAL)
        self.assertEqual(self.finish(old)['result'], 'NOERR')
        self.assertEqual(protocol.active_match(self.conn, protocol.LOCAL), before)
        self.record.assert_called_once()

    def test_unknown_end_reason_and_category_change_do_not_count_or_clear_match(self):
        gid = self.prepare()
        self.start(gid)
        bad = self.result_request(gid)
        bad['match_result']['abnormalend_reason'] = 'NETWORK_DISCONNECTED'
        self.assertEqual(self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', bad)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.finish(gid, match_category='ONLINE_RANK')['result'], 'ERR_INVALID_PARAM')
        self.assertTrue(protocol.active_match(self.conn, protocol.LOCAL)['started'])
        self.record.assert_not_called()


if __name__ == '__main__':
    unittest.main()
