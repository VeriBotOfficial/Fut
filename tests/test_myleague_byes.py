"""Odd-size leagues have real rest rounds, not a selected-team 0:0 game."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_match_myleague as base_tests
import club_store as store
import gate_logic
import match_protocol as protocol
import my_league
import season_rankings
import ziru_data


class MyLeagueByeTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueMatchTest.setUp(self)
        self.gate.responses['CMD_GET_SEASON_INFO'] = ziru_data.season_info('f1')
        self.rqid = 100

    def select(self, team=6202, rqid=None):
        self.rqid += 1
        return base_tests.MyLeagueMatchTest.select(self, team, self.rqid if rqid is None else rqid)

    def info(self):
        return self.gate.handle('CMD_GET_SEASON_INFO', dict(rqid=500000))

    def request(self, game_id):
        active = my_league.state(self.conn)['active']
        home, away = (2, 0) if active['side'] == 'HOME' else (0, 2)
        return dict(msgid='CMD_GET_VSCOM_GAME_RESULT', rqid=self.rqid + 100000,
            match_category='SEASON_MODE', intentional_give_up='NO',
            match_result=dict(game_id=game_id, gameend_reason='NORMAL_END',
                abnormalend_reason='NOERR', myteam_player_list=[
                    dict(copy.deepcopy(ref), play_time=90 if index < 11 else 0,
                         is_starting_member='YES' if index < 11 else 'NO', is_join_game='NO')
                    for index, ref in enumerate(self.refs)]),
            score=[dict(side='HOME', score_1st=home, score_2nd=0),
                   dict(side='AWAY', score_1st=away, score_2nd=0)],
            goal_player_list=[copy.deepcopy(self.refs[0]['gameplayer_id'])] * 2,
            assist_player_list=[copy.deepcopy(self.refs[1]['gameplayer_id'])])

    def play(self):
        self.rqid += 1
        prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT',
            dict(rqid=self.rqid, match_category='SEASON_MODE'))
        self.assertEqual(prepared['result'], 'NOERR')
        started = self.gate.handle('CMD_START_GAME', dict(rqid=self.rqid + 1000,
            game_id=prepared['game_id'], match_category='SEASON_MODE', side='HOME',
            player_list_home=copy.deepcopy(self.refs), player_list_away=[], is_vs_aimatch='YES'))
        self.assertEqual(started['result'], 'NOERR')
        req = self.request(prepared['game_id'])
        ended = self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', req)
        self.assertEqual(ended['result'], 'NOERR')
        self.assertEqual(ended['game_end_result']['match_result']['game_res'], 'WIN')
        return req, ended

    def fingerprint(self):
        return (my_league.state(self.conn), store.season_points(self.conn),
                list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial')),
                list(self.conn.execute('SELECT key,value FROM meta WHERE key LIKE ? ORDER BY key',
                                       (season_rankings.PREFIX + '%',))))

    def completed_missing_fixture(self):
        """A valid completed historical season with one old incomplete ledger."""
        self.assertEqual(self.select(team=1248)['result'], 'NOERR')
        state = my_league.state(self.conn)
        for fixture in my_league.fixtures(state):
            matches = my_league.fixture_matches(state, fixture)
            for match in matches:
                if state['team_id'] in (match['home_team_id'], match['away_team_id']):
                    match.update(home_goals=2 if match['home_team_id'] == state['team_id'] else 0,
                                 away_goals=2 if match['away_team_id'] == state['team_id'] else 0)
            state['played'][str(fixture['fixture'])] = dict(result='WIN', matches=matches)
        state['completed'] = True; state['fixture'] = len(state['played'])
        fixture = next(number for number, played in state['played'].items()
            if any(1249 in (match['home_team_id'], match['away_team_id'])
                   and state['team_id'] not in (match['home_team_id'], match['away_team_id'])
                   and (match['home_goals'] if match['home_team_id'] == 1249 else match['away_goals']) > 0
                   for match in played['matches']))
        with patch.object(season_rankings, 'CANONICAL_PATH', Path(self.temp.name) / 'old-no-canonical.json'):
            self.conn.execute('BEGIN IMMEDIATE')
            season_rankings.record_fixture(self.conn, state, int(fixture), state['played'][fixture]['matches'])
            my_league.save(self.conn, state)
            store.set_season_points(self.conn, 9123)
            self.conn.commit()
        key = season_rankings._scope(state) + 'fixture_' + fixture
        saved = json.loads(store.get_meta(self.conn, key))
        self.assertIn(1249, saved['unavailable_teams'])
        other_key = season_rankings.PREFIX + '999_149_1248_fixture_1'
        store.set_meta(self.conn, other_key, json.dumps(dict(source='simulated_cpu_fixtures',
            fingerprint='other-season-unchanged', rows=[], unavailable_teams=[5145])))
        self.conn.commit()
        return state, key, saved, other_key

    def test_real_first_round_bye_selects_next_match_without_own_draw_or_points(self):
        before_players = list(self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial'))
        before_points = store.season_points(self.conn)
        self.assertEqual(self.select(rqid=1)['result'], 'NOERR')
        state = my_league.state(self.conn)
        self.assertEqual(state['fixture'], 2)
        self.assertEqual(set(state['played']), {'1'})
        self.assertTrue(state['played']['1']['bye'])
        self.assertNotIn('result', state['played']['1'])
        self.assertEqual(len(state['played']['1']['matches']), 6)
        self.assertTrue(all(6202 not in (m['home_team_id'], m['away_team_id'])
                            for m in state['played']['1']['matches']))
        table = {row['team_id']: row for row in my_league.ranking(state)}
        self.assertEqual(table[6202]['wins'] + table[6202]['draws'] + table[6202]['losses'], 0)
        self.assertEqual(sum(row['wins'] + row['draws'] + row['losses'] for row in table.values()), 12)
        self.assertEqual(store.season_points(self.conn), before_points)
        self.assertEqual(list(self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial')), before_players)
        info = self.info()
        self.assertEqual(info['fixture'], 2); self.assertEqual(info['match_history_list'], [])
        self.assertTrue(all(row['is_com'] == 'YES' for row in info['goal_ranking']))
        before = self.fingerprint()
        self.assertEqual(self.select(rqid=1)['result'], 'NOERR')
        self.assertEqual(self.select(rqid=2)['result'], 'NOERR')
        self.assertEqual(self.select(team=0, rqid=3)['result'], 'NOERR')
        self.assertEqual(self.fingerprint(), before)
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        try:
            other = gate_logic.GateLogic({'CMD_GET_SEASON_INFO': ziru_data.season_info('f1')}, conn=reopened)
            self.assertEqual(other.handle('CMD_GET_SEASON_INFO', {})['fixture'], 2)
            self.assertEqual(my_league.state(reopened), before[0])
        finally:
            reopened.close()

    def test_real_13_team_season_first_and_middle_byes_count_24_games_not_26(self):
        self.assertEqual(self.select()['result'], 'NOERR')
        while not my_league.state(self.conn)['completed']:
            request, _ = self.play()
            before = self.fingerprint()
            self.assertEqual(self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', request)['result'], 'NOERR')
            self.assertEqual(self.fingerprint(), before)
        state = my_league.state(self.conn)
        byes = [int(fx) for fx, row in state['played'].items() if row.get('bye')]
        self.assertEqual(sorted(byes), [1, 14])
        self.assertEqual(len(state['played']), 26)
        self.assertEqual(self.info()['match_history_list'], ['WIN'] * 24)
        self.assertEqual(store.season_points(self.conn), 24 * 300)
        for row in my_league.ranking(state):
            self.assertEqual(row['wins'] + row['draws'] + row['losses'], 24)
        player = store.player_by_key(self.conn, 9000, 1)
        self.assertEqual((player['games'], player['goal']), (24, 48))
        assists = store.player_by_key(self.conn, 9001, 1)
        self.assertEqual(assists['assist'], 24)

    def test_final_bye_completes_season_and_displays_real_last_round(self):
        self.assertEqual(self.select(team=6207)['result'], 'NOERR')
        while not my_league.state(self.conn)['completed']:
            self.play()
        state = my_league.state(self.conn)
        self.assertTrue(state['played']['26']['bye'])
        self.assertEqual(state['fixture'], 26)
        self.assertEqual(self.info()['auto_match_list'], state['played']['26']['matches'])
        self.assertEqual(self.info()['is_end_season'], 'YES')
        self.assertEqual(self.info()['match_history_list'], ['WIN'] * 24)
        self.assertEqual(store.season_points(self.conn), 7200)
        # A legacy build could have stopped on the terminal bye. A prepare
        # request must persist that round rather than roll it back on no-game.
        state['completed'] = False; state['played'].pop('26')
        self.conn.execute('DELETE FROM meta WHERE key LIKE ?',
                          (season_rankings.PREFIX + '%fixture_26',))
        my_league.save(self.conn, state); self.conn.commit()
        before_players = list(self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial'))
        reply = self.gate.handle('CMD_GET_VSCOM_OPPONENT',
            dict(match_category='SEASON_MODE', rqid=800000))
        self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        self.assertTrue(my_league.state(self.conn)['completed'])
        self.assertEqual(store.season_points(self.conn), 7200)
        self.assertEqual(list(self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial')), before_players)

    def test_legacy_bye_menu_is_read_only_then_prepare_repairs_round(self):
        self.assertEqual(self.select()['result'], 'NOERR')
        state = my_league.state(self.conn); state['fixture'] = 1; state['played'] = {}
        self.conn.execute('DELETE FROM meta WHERE key LIKE ?', (season_rankings.PREFIX + '%',))
        my_league.save(self.conn, state); self.conn.commit()
        before = self.fingerprint()
        info = self.info(); self.assertEqual(info['fixture'], 2)
        self.assertEqual(self.fingerprint(), before)
        reply = self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(match_category='SEASON_MODE', rqid=800001))
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual(my_league.state(self.conn)['fixture'], 2)
        self.assertTrue(my_league.state(self.conn)['played']['1']['bye'])
        self.assertEqual(store.season_points(self.conn), before[1])

    def test_bye_cpu_stats_and_season_save_rollback_together_on_failure(self):
        original = season_rankings.record_fixture

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('test interrupted bye transaction')

        with patch.object(season_rankings, 'record_fixture', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'test interrupted'):
                self.select()
        self.assertIsNone(my_league.state(self.conn))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM meta WHERE key LIKE ?',
                                          (season_rankings.PREFIX + '%',)).fetchone()[0], 0)
        self.assertEqual(self.select()['result'], 'NOERR')
        self.assertEqual(my_league.state(self.conn)['fixture'], 2)

    def test_started_other_club_blocks_selection_before_any_bye_is_simulated(self):
        self.assertEqual(self.select(team=101)['result'], 'NOERR')
        prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(match_category='SEASON_MODE', rqid=700001))
        started = self.gate.handle('CMD_START_GAME', dict(game_id=prepared['game_id'],
            match_category='SEASON_MODE', player_list_home=self.refs, side='HOME', rqid=700002))
        self.assertEqual(started['result'], 'NOERR')
        before = self.fingerprint()
        self.assertEqual(self.select()['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.fingerprint(), before)

    def test_completed_old_season_zero_resume_repairs_once_and_menu_stays_read_only(self):
        state, key, old, other_key = self.completed_missing_fixture()
        before = self.fingerprint(); other = store.get_meta(self.conn, other_key)
        self.info(); self.assertEqual(self.fingerprint(), before)
        keys_before = [row[0] for row in before[3]]
        self.assertEqual(self.select(team=0)['result'], 'NOERR')
        repaired = json.loads(store.get_meta(self.conn, key))
        self.assertEqual(repaired['unavailable_teams'], [])
        self.assertEqual(repaired['rows'][:len(old['rows'])], old['rows'])
        self.assertGreater(sum(row['goal'] for row in repaired['rows'] if row['team_id'] == 1249), 0)
        self.assertEqual(store.get_meta(self.conn, other_key), other)
        after = self.fingerprint()
        self.assertEqual(after[:3], before[:3])
        self.assertEqual([row[0] for row in after[3]], keys_before)
        self.assertEqual(self.select(team=0)['result'], 'NOERR')
        self.assertEqual(self.fingerprint(), after)
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        try:
            gate = gate_logic.GateLogic({'CMD_GET_SEASON_INFO': ziru_data.season_info('f1')}, conn=reopened)
            info = gate.handle('CMD_GET_SEASON_INFO', {})
            self.assertEqual(info['is_end_season'], 'YES')
            self.assertEqual(my_league.state(reopened), state)
            self.assertEqual(store.get_meta(reopened, key), store.get_meta(self.conn, key))
        finally:
            reopened.close()

    def test_completed_old_season_prepare_repairs_before_no_game_return(self):
        state, key, _, other_key = self.completed_missing_fixture()
        before = self.fingerprint(); other = store.get_meta(self.conn, other_key)
        prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT',
            dict(match_category='SEASON_MODE', rqid=900001))
        self.assertEqual(prepared['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), state)
        self.assertEqual(json.loads(store.get_meta(self.conn, key))['unavailable_teams'], [])
        self.assertEqual(store.get_meta(self.conn, other_key), other)
        self.assertEqual(self.fingerprint()[:3], before[:3])
        after = self.fingerprint()
        self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(match_category='SEASON_MODE', rqid=900002))
        self.assertEqual(self.fingerprint(), after)


if __name__ == '__main__':
    unittest.main()
