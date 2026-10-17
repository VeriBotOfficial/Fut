"""Real statistics adapters through all three gate settlement routes.

Only captured reply templates/opponent fixtures are compact stand-ins. Native
participation decoding, stat writes, transactions, receipts, gate routing and
detail serialization are the actual implementation on disposable file DBs.
"""
import copy
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_match_settlement as settlement_tests
import ai_division
import club_store as store
import gate_logic
import local_vscom
import match_protocol as protocol
import my_league


MODES = (protocol.AI, protocol.SEASON, protocol.LOCAL)


class NativeStatsSettlementTest(unittest.TestCase):
    seed_season = settlement_tests.MatchSettlementTest.seed_season
    request = settlement_tests.MatchSettlementTest.request

    def setUp(self):
        settlement_tests.MatchSettlementTest.setUp(self)
        self.keys = ((12, 1), (12, 2), (20, 3), (30, 4))
        self.refs = [{'gameplayer_id': {'id': pid, 'serial': serial}} for pid, serial in self.keys]
        for ref in self.refs:
            gid = ref['gameplayer_id']
            player = dict(ref, games=10, goal=20, assist=30, parameters=[0] * 147,
                          initial_booster_id_list=[0, 0, 0], additional_skill_id=[],
                          level_info={'level': 1, 'level_limit': 1, 'customize_point': 0,
                                      'exp_total': 0, 'exp_current_level': 0, 'exp_to_next_level': 0})
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (gid['id'], gid['serial'], json.dumps(player)))
        squad = store.squads(self.conn)[0]
        squad['squad_data']['player_list'] = copy.deepcopy(self.refs)
        store.save_squad(self.conn, squad)
        self.conn.execute('CREATE TABLE owned_identity_aliases('
                          'id INTEGER,old_serial INTEGER,new_serial INTEGER,PRIMARY KEY(id,old_serial))')
        store.set_meta(self.conn, 'owned_identity_version', '1')
        self.conn.commit()
        self.gate = gate_logic.GateLogic({}, conn=self.conn)

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        self.gate = gate_logic.GateLogic({}, conn=self.conn)

    def start(self, mode, side='HOME'):
        protocol.initialize_test_seed(self.conn, new_test_seed=True)
        store.set_meta(self.conn, 'season_point', '0')
        self.conn.commit()
        if mode == protocol.LOCAL:
            prepared = local_vscom.prepare(self.conn,
                {'side': 'AWAY' if side == 'HOME' else 'HOME', 'team_data': {'base_team_id': 103}},
                {'match_category': 'FRIEND', 'rqid': 8})
            gid = prepared['game_id']
        else:
            gid = settlement_tests.MatchSettlementTest.prepare(self, mode, side, start=False)
        category = {protocol.AI: 'LEAGUE_COM', protocol.SEASON: 'MYLEAGUE', protocol.LOCAL: 'FRIEND'}[mode]
        response = self.gate.handle('CMD_START_GAME', {'game_id': gid, 'side': side,
            'user_id': 7, 'rqid': 10, 'squad_id': 1, 'match_category': category,
            'player_list_home': copy.deepcopy(self.refs)})
        self.assertEqual(response['result'], 'NOERR', response)
        active = protocol.active_match(self.conn, mode)
        self.assertEqual(active['eligible_player_instances'], [list(key) for key in self.keys])
        self.assertEqual([row['gameplayer_id'] for row in response['squad_data_home']['player_list']],
                         [row['gameplayer_id'] for row in self.refs])
        return gid

    def native_request(self, gid, side='HOME'):
        req = self.request(gid, 3 if side == 'HOME' else 1, 1 if side == 'HOME' else 3)
        req['match_result']['myteam_player_list'] = [
            dict(self.refs[0], play_time=90, is_starting_member='YES', is_join_game='NO'),
            dict(self.refs[1], play_time=30, is_starting_member='NO', is_join_game='YES'),
            dict(self.refs[2], play_time=0, is_starting_member='NO', is_join_game='NO'),
            dict(self.refs[3], play_time=90, is_starting_member='YES', is_join_game='NO')]
        req['goal_player_list'] = [dict(self.refs[i]['gameplayer_id']) for i in (0, 0, 1)]
        req['assist_player_list'] = [dict(self.refs[3]['gameplayer_id']) for _ in range(2)]
        return req

    def finish(self, req):
        return self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', req)

    def counters(self):
        return {key: {field: store.player_by_key(self.conn, *key)[field]
                      for field in ('games', 'goal', 'assist')} for key in self.keys}

    def expected(self, before):
        after = copy.deepcopy(before)
        for key in ((12, 1), (12, 2), (30, 4)):
            after[key]['games'] += 1
        after[12, 1]['goal'] += 2
        after[12, 2]['goal'] += 1
        after[30, 4]['assist'] += 2
        return after

    def progress(self):
        return {key: store.get_meta(self.conn, key, '') for key in (
            ai_division.STATE_KEY, my_league.KEY, 'season_point')}

    def stats_state(self, mode, gid):
        return json.loads(store.get_meta(self.conn, f'efootxi_match_stats_state_{mode}_{gid}', 'null'))

    def test_three_modes_write_exact_instance_counters_and_persist_after_reopen(self):
        for mode in MODES:
            for side in ('HOME', 'AWAY'):
                with self.subTest(mode=mode, side=side):
                    gid = self.start(mode, side)
                    before = self.counters()
                    result = self.finish(self.native_request(gid, side))
                    self.assertEqual(result['result'], 'NOERR', result)
                    self.assertEqual(self.counters(), self.expected(before))
                    self.assertEqual(self.stats_state(mode, gid)['status'], 'applied')
                    self.restart()
                    self.assertEqual(self.counters(), self.expected(before))
                    self.assertIsNone(protocol.active_match(self.conn, mode))

    def test_duplicate_and_conflicting_scorer_requests_cannot_change_counters_or_points(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                req = self.native_request(gid)
                self.assertEqual(self.finish(req)['result'], 'NOERR')
                saved, progress = self.counters(), self.progress()
                self.restart()
                retried = copy.deepcopy(req)
                retried['rqid'] = 99
                self.assertEqual(self.finish(retried)['result'], 'NOERR')
                self.assertEqual(self.counters(), saved)
                self.assertEqual(self.progress(), progress)
                conflicting = copy.deepcopy(retried)
                conflicting['goal_player_list'][0] = dict(self.refs[1]['gameplayer_id'])
                self.assertEqual(self.finish(conflicting)['result'], 'ERR_INVALID_PARAM')
                self.assertEqual(self.counters(), saved)
                self.assertEqual(self.progress(), progress)

    def test_pending_payload_can_be_completed_after_restart_without_second_match_reward(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                before = self.counters()
                complete = self.native_request(gid)
                incomplete = copy.deepcopy(complete)
                del incomplete['match_result']['myteam_player_list']
                first = self.finish(incomplete)
                self.assertEqual(first['result'], 'NOERR')
                self.assertEqual(first['game_end_result']['match_result']['game_res'], 'WIN')
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.stats_state(mode, gid)['status'], 'pending')
                progress = self.progress()
                self.restart()
                complete['rqid'] = 100
                self.assertEqual(self.finish(complete)['result'], 'NOERR')
                self.assertEqual(self.stats_state(mode, gid)['status'], 'applied')
                self.assertEqual(self.counters(), self.expected(before))
                self.assertEqual(self.progress(), progress)
                complete['rqid'] = 101
                self.assertEqual(self.finish(complete)['result'], 'NOERR')
                self.assertEqual(self.counters(), self.expected(before))
                self.assertEqual(self.progress(), progress)

    def test_receipt_failure_rolls_back_real_counters_progress_and_stats_state(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                req = self.native_request(gid)
                before, progress = self.counters(), self.progress()
                active = protocol.active_match(self.conn, mode)
                with patch.object(protocol, 'save_receipt', side_effect=RuntimeError('receipt disk error')):
                    with self.assertRaisesRegex(RuntimeError, 'receipt disk error'):
                        self.finish(req)
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.progress(), progress)
                self.assertEqual(protocol.active_match(self.conn, mode), active)
                self.assertIsNone(self.stats_state(mode, gid))
                self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(mode, gid), ''), '')
                self.assertEqual(self.finish(req)['result'], 'NOERR')
                self.assertEqual(self.counters(), self.expected(before))

    def test_sqlite_failure_during_real_stat_update_rolls_back_all_players_and_result(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                req = self.native_request(gid)
                before, progress = self.counters(), self.progress()
                self.conn.execute("CREATE TRIGGER fail_second_stat BEFORE UPDATE ON gameplayers "
                                  "WHEN NEW.id=12 AND NEW.serial=2 "
                                  "BEGIN SELECT RAISE(ABORT,'stats disk failure'); END")
                self.conn.commit()
                with self.assertRaises(sqlite3.DatabaseError):
                    self.finish(req)
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.progress(), progress)
                self.assertTrue(protocol.active_match(self.conn, mode)['started'])
                self.assertIsNone(self.stats_state(mode, gid))
                self.conn.execute('DROP TRIGGER fail_second_stat')
                self.conn.commit()
                self.assertEqual(self.finish(req)['result'], 'NOERR')
                self.assertEqual(self.counters(), self.expected(before))

    def test_player_detail_and_login_show_saved_statistics_after_restart(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                self.assertEqual(self.finish(self.native_request(gid))['result'], 'NOERR')
                expected = self.counters()
                self.restart()
                for key in self.keys:
                    detail = self.gate.handle('CMD_GET_MYCLUB_PLAYER_DATA', {
                        'rqid': 110, 'gameplayer_id': {'id': key[0], 'serial': key[1]}})
                    self.assertEqual(detail['result'], 'NOERR')
                    row = detail['gameplayer_list'][0]
                    self.assertEqual({field: row[field] for field in ('games', 'goal', 'assist')}, expected[key])
                login = self.gate.handle('CMD_LOGIN', {'rqid': 111})
                rows = {store.player_key(row): row for row in login['entry_info']['gameplayer_list']}
                for key in self.keys:
                    self.assertEqual({field: rows[key][field] for field in ('games', 'goal', 'assist')}, expected[key])

    def test_forfeit_does_not_record_partial_appearances_goals_or_assists(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                before = self.counters()
                req = self.native_request(gid)
                req['intentional_give_up'] = 'YES'
                req['match_result']['abnormalend_reason'] = 'NORMAL_GIVEUP'
                response = self.finish(req)
                self.assertEqual(response['result'], 'NOERR')
                self.assertEqual(response['game_end_result']['match_result']['game_res'], 'LOSE')
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.stats_state(mode, gid)['status'], 'excluded_give_up')


if __name__ == '__main__':
    unittest.main()
