"""Exercise actual season menu/start/result routes on a disposable owned save.

The captured reply templates are data. Routing, deterministic CPU simulation,
owned-player resolution, season transactions and restart receipts are real.
"""
import copy
import sqlite3
import unittest

import test_match_myleague as base_tests
import club_store as store
import gate_logic
import match_protocol as protocol
import my_league
import ziru_data


class MyLeagueFixtureFlowTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueMatchTest.setUp(self)
        # Released test APKs enable exact owned-instance identity checks.
        # Do not exercise the old pre-migration substitute-player fallback.
        store.player_identity.migrate(self.conn)
        self.refs = store.player_identity.canonical_request(self.conn, self.refs)
        self.assertEqual(self.select()['result'], 'NOERR')
        self.gate.responses['CMD_GET_SEASON_INFO'] = ziru_data.season_info('f1')

    def select(self):
        return base_tests.MyLeagueMatchTest.select(self)

    def info(self):
        return self.gate.handle('CMD_GET_SEASON_INFO', dict(rqid=40, user_id=7))

    def business_state(self):
        return (my_league.state(self.conn), store.season_points(self.conn),
                list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial')))

    def prepare(self):
        return self.gate.handle('CMD_GET_VSCOM_OPPONENT',
            dict(match_category='MYLEAGUE', rqid=20, user_id=7))

    def start(self):
        prepared = self.prepare()
        reply = self.gate.handle('CMD_START_GAME', dict(game_id=prepared['game_id'],
            match_category='MYLEAGUE', player_list_home=[], player_list_away=[],
            side='HOME', rqid=21, user_id=7))
        self.assertEqual(reply['result'], 'NOERR')
        return prepared['game_id']

    def result_request(self, gid):
        active = my_league.state(self.conn)['active']
        h, a = (2, 0) if active['side'] == 'HOME' else (0, 2)
        players = [dict(copy.deepcopy(ref), play_time=90 if i < 11 else 0,
            is_starting_member='YES' if i < 11 else 'NO',
            is_join_game='YES' if i < 11 else 'NO') for i, ref in enumerate(self.refs)]
        return dict(msgid='CMD_GET_VSCOM_GAME_RESULT', rqid=30, user_id=7,
            match_category='MYLEAGUE', intentional_give_up='NO',
            match_result=dict(game_id=gid, gameend_reason='NORMAL_END',
                abnormalend_reason='NOERR', myteam_player_list=players),
            score=[dict(side='HOME', score_1st=h, score_2nd=0),
                   dict(side='AWAY', score_1st=a, score_2nd=0)],
            goal_player_list=[copy.deepcopy(self.refs[0]['gameplayer_id'])] * 2,
            assist_player_list=[copy.deepcopy(self.refs[1]['gameplayer_id'])])

    def cpu_rows(self, rows):
        team = my_league.state(self.conn)['team_id']
        return [m for m in rows if team not in (m['home_team_id'], m['away_team_id'])]

    def test_current_round_has_cpu_results_but_menu_reads_do_not_count_any_matches(self):
        before = self.business_state()
        first = self.info()
        self.assertEqual(first['result'], 'NOERR')
        self.assertTrue(any(m['home_goals'] or m['away_goals']
                            for m in self.cpu_rows(first['auto_match_list'])))
        own = next(m for m in first['auto_match_list']
                   if first['team_id'] in (m['home_team_id'], m['away_team_id']))
        self.assertEqual((own['home_goals'], own['away_goals']), (0, 0))
        self.assertTrue(all(r['wins'] + r['draws'] + r['losses'] == 0
                            for r in first['ranking']))
        second = self.info()
        self.assertEqual(second['auto_match_list'], first['auto_match_list'])
        self.assertEqual(self.business_state(), before)

    def test_start_rejection_and_phantom_result_do_not_advance_or_award_points(self):
        gid = self.prepare()['game_id']
        before = self.business_state()
        rejected = self.gate.handle('CMD_START_GAME', dict(game_id=gid,
            match_category='MYLEAGUE', rqid=22, user_id=7,
            player_list_home={}))
        self.assertEqual(rejected['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(my_league.state(self.conn)['active']['started'])
        failed_result = dict(match_category='MYLEAGUE', rqid=23, user_id=7,
            match_result=dict(game_id=gid, gameend_reason='NORMAL_END',
                abnormalend_reason='NOERR', myteam_player_list=[]),
            score=[dict(score_1st=0), dict(score_1st=0)])
        reply = self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', failed_result)
        self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.business_state(), before)
        self.assertEqual(store.get_meta(self.conn, protocol.receipt_key(protocol.SEASON, gid), ''), '')
        self.assertEqual(self.info()['match_history_list'], [])
        self.assertEqual(self.business_state(), before)

    def test_completed_fixture_commits_exact_preview_and_retry_does_not_count_twice(self):
        preview = self.info()['auto_match_list']
        gid = self.start()
        request = self.result_request(gid)
        reply = self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', request)
        self.assertEqual(reply['result'], 'NOERR')
        saved = my_league.state(self.conn)
        first = saved['played']['1']['matches']
        self.assertEqual(self.cpu_rows(first), self.cpu_rows(preview))
        self.assertEqual(saved['fixture'], 2)
        table = reply['game_end_result']['extra_match_result']['season_match_result']['ranking']
        self.assertEqual(len(table), len(self.info()['ranking']))
        self.assertTrue(all(r['wins'] + r['draws'] + r['losses'] == 1 for r in table))
        before = self.business_state()
        request['rqid'] = 31
        self.assertEqual(self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', request)['result'], 'NOERR')
        self.assertEqual(self.business_state(), before)
        schedule = self.gate.handle('CMD_GET_SEASON_SCHEDULE', dict(rqid=50))
        self.assertEqual(schedule['schedule_list'][0]['match_list'], first)
        self.assertTrue(all(m['home_goals'] == m['away_goals'] == 0
                            for m in schedule['schedule_list'][1]['match_list']))

    def test_cpu_preview_survives_restart_and_is_not_persisted_as_a_completed_round(self):
        preview = self.info()['auto_match_list']
        reopened = sqlite3.connect(self.conn.execute('PRAGMA database_list').fetchone()[2])
        self.addCleanup(reopened.close)
        before = my_league.state(reopened)
        gate = gate_logic.GateLogic({'CMD_GET_SEASON_INFO': ziru_data.season_info('f1')}, conn=reopened)
        reply = gate.handle('CMD_GET_SEASON_INFO', dict(rqid=41, user_id=7))
        self.assertEqual(reply['auto_match_list'], preview)
        self.assertEqual(my_league.state(reopened), before)
        self.assertEqual(before['played'], {})

    def test_finished_season_menu_returns_saved_last_round_not_new_cpu_simulation(self):
        s = my_league.state(self.conn)
        s['schedule_v2'] = s['schedule_v2'][:1]
        my_league.save(self.conn, s)
        self.conn.commit()
        gid = self.start()
        self.assertEqual(self.gate.handle('CMD_GET_VSCOM_GAME_RESULT',
                                         self.result_request(gid))['result'], 'NOERR')
        before = self.business_state()
        reply = self.info()
        self.assertEqual(reply['is_end_season'], 'YES')
        self.assertEqual(reply['auto_match_list'], before[0]['played']['1']['matches'])
        self.assertEqual(self.business_state(), before)


if __name__ == '__main__':
    unittest.main()
