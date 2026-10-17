"""Native-shape My League selection and full-roster match serialization."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
sys.path.insert(0, str(REPO.parents[1] / 'analysis/v120-investigation/draw-skills/host-deps'))
import club_store as store
import gate_logic
import match_protocol as protocol
import my_league
import ziru_data


class MyLeagueMatchTest(unittest.TestCase):
    def setUp(self):
        data = REPO / 'build/gateway-fixture/outputs/reference/ziru-data'
        if not (data / 'ml_start_game.json').is_file():
            self.skipTest('Optional extracted APK protocol fixture is unavailable')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        self.addCleanup(self.conn.close)
        self.conn.executescript(store.SCHEMA)
        for mock in (patch.object(ziru_data, 'DATA', data), patch.object(ziru_data, '_cache', {})):
            mock.start()
            self.addCleanup(mock.stop)
        self.gate = gate_logic.GateLogic({}, conn=self.conn)
        self.refs = [{'gameplayer_id': {'id': 9000+i, 'serial': 1}} for i in range(23)]
        for i, ref in enumerate(self.refs):
            params = [0] * 145
            params[107] = int(i == 0)  # innate Double Touch, independently of learned slots
            player = dict(ref, parameters=params, additional_skill_id=[1] if i == 22 else [],
                          initial_booster_id_list=[0, 0, 0])
            self.conn.execute('insert into gameplayers values(?,?,?,?,?)',
                              (9000+i, 1, json.dumps(player), 'test', 'now'))
        for sid in (1, 2):
            coach = {'coach_id': {'id': 80+sid, 'serial': 1}, 'short_counter': 86+sid,
                     'ability_id': [5, 26, 16, 0]}
            self.conn.execute('insert into coaches values(?,?,?)', (80+sid, 1, json.dumps(coach)))
            plan = copy.deepcopy(ziru_data.ml_start_game()['squad_data_home']['squad_gameplan'])
            plan['formation'][0]['team_style'] = sid
            squad = {'squad_id': sid, 'base_team': [{'base_team_id': 101}],
                     'squad_data': {'coach': {'coach_id': coach['coach_id']},
                                    'player_list': self.refs if sid == 1 else list(reversed(self.refs)),
                                    'squad_gameplan': plan}}
            self.conn.execute('insert into squads values(?,?)', (sid, json.dumps(squad)))
        store.set_meta(self.conn, 'main_squad_id', '1')
        store.set_meta(self.conn, 'base_team_id', '101')
        store.set_uniforms(self.conn, [108 * store.UNIFORM_STRIDE, 108 * store.UNIFORM_STRIDE + 1])
        self.conn.commit()

    def select(self, team=101, rqid=1, **extra):
        request = dict(select_team_id=team, select_compe_unit_id=0, rqid=rqid, user_id=7)
        request.update(extra)
        return my_league.select(self.conn, {}, request)

    def start(self, team=101, squad_id=1, request_refs=False):
        store.set_meta(self.conn, 'main_squad_id', str(squad_id))
        self.conn.commit()
        self.assertEqual(self.select(team)['result'], 'NOERR')
        prepared = my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        req = dict(game_id=prepared['game_id'], squad_id=squad_id, rqid=5, user_id=7)
        if request_refs:
            req['player_list_home'] = list(reversed(self.refs))
        refs = my_league.start_refs(self.conn, req)
        entries = self.gate._home_entries(refs, log_substitutes=False)
        return my_league.start(self.conn, {}, req, entries)

    def test_first_entry_uses_native_neutral_selection_sentinels(self):
        captured = {'season_root_info': {'team_id': 108, 'league_compe_unit_id': 119,
                     'league_team_id_list': [108], 'is_timer_alert': 'NO'}}
        reply = my_league.root_view(self.conn, captured)
        self.assertEqual(reply['season_root_info'], {'team_id': 0, 'league_compe_unit_id': 65535,
                         'league_team_id_list': [], 'is_timer_alert': 'NO'})
        self.assertEqual(captured['season_root_info']['team_id'], 108)

    def test_real_selected_teams_replace_preview_and_persist(self):
        self.assertEqual(self.select(101)['team_id'], 101)
        my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        response = self.select(108, rqid=2)
        self.assertEqual((response['team_id'], response['league_compe_unit_id']), (108, 119))
        self.assertEqual(response['special_dream_squad_data']['rental_team_id'], 108)
        self.assertIsNone(my_league.state(self.conn)['active'])
        view = my_league.root_view(self.conn, {'season_root_info': {}})['season_root_info']
        self.assertEqual((view['team_id'], view['league_compe_unit_id']), (108, 119))
        self.assertIn(108, view['league_team_id_list'])
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        try:
            self.assertEqual(my_league.state(reopened)['team_id'], 108)
        finally:
            reopened.close()

    def test_unknown_selection_schema_does_not_choose_101(self):
        response = my_league.select(self.conn, {}, {'team_id': 108, 'rqid': 4})
        self.assertEqual(response['result'], 'ERR_INVALID_PARAM')
        self.assertIsNone(my_league.state(self.conn))
        event = json.loads(self.conn.execute('select detail from events order by id desc').fetchone()[0])
        self.assertEqual(event['reason'], 'unknown_selected_team')
        self.assertIn('team_id', event['fields'])

    def test_wrong_competition_does_not_replace_selected_team(self):
        self.select(101)
        response = self.select(108, rqid=2, select_compe_unit_id=113)
        self.assertEqual(response['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn)['team_id'], 101)

    def test_zero_resume_and_transport_retry_do_not_reset_schedule(self):
        self.select(101)
        prepared = my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        before = my_league.state(self.conn)
        self.assertEqual(self.select(0, rqid=2)['team_id'], 101)
        self.assertEqual(self.select(101, rqid=3)['team_id'], 101)
        self.assertEqual(my_league.state(self.conn), before)
        self.assertEqual(before['active']['game_id'], prepared['game_id'])

    def test_selected_team_kits_do_not_inherit_barcelona_even_when_club_matches(self):
        response = self.start(team=101)
        self.assertEqual(response['result'], 'NOERR')
        self.assertEqual(response['squad_data_home']['team_id'], 101)
        self.assertEqual(response['uniform_list_home'],
                         {'base_team_id': 101, 'uniform_list': store.default_uniforms(101)})
        away = my_league.state(self.conn)['active']['opponent']['team_data']['base_team_id']
        self.assertEqual(response['uniform_list_away']['uniform_list'], store.default_uniforms(away))
        self.assertEqual(store.stored_uniforms(self.conn)[0], 108 * store.UNIFORM_STRIDE)

    def test_selected_plan_and_all_substitutes_use_normalized_live_skills(self):
        response = self.start(squad_id=2)
        self.assertEqual(response['result'], 'NOERR')
        home = response['squad_data_home']
        self.assertEqual(home['coach']['coach_id'], {'id': 82, 'serial': 1})
        self.assertEqual(home['squad_gameplan']['formation'][0]['team_style'], 2)
        entries = home['player_list']
        self.assertEqual([p['gameplayer_id'] for p in entries],
                         [p['gameplayer_id'] for p in reversed(self.refs)])
        self.assertEqual(len(entries), 23)
        self.assertTrue(all(len(p['parameters']) == 147 for p in entries))
        self.assertEqual(entries[0]['parameters'][107], 1)  # learned on the saved bench card
        self.assertEqual(entries[-1]['parameters'][107], 1)  # innate on the other card
        self.assertEqual(entries[1]['parameters'][107], 0)  # never grant it to everyone
        self.assertEqual(store.player_by_key(self.conn, 9022, 1)['parameters'][107], 0)
        self.assertEqual(len(my_league.state(self.conn)['active']['eligible_player_instances']), 23)

    def test_cached_match_roster_cannot_replace_saved_plan_order(self):
        response = self.start(squad_id=1, request_refs=True)
        self.assertEqual(response['squad_data_home']['player_list'][0]['gameplayer_id']['id'], 9000)

    def test_gate_integration_uses_selected_plan_without_request_roster(self):
        store.set_meta(self.conn, 'main_squad_id', '2')
        self.conn.commit()
        self.select(101)
        prepared = my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        response = self.gate.handle('CMD_START_GAME',
                                   {'game_id': prepared['game_id'], 'squad_id': 2, 'rqid': 5, 'user_id': 7})
        self.assertEqual(response['result'], 'NOERR')
        home = response['squad_data_home']
        self.assertEqual(len(home['player_list']), 23)
        self.assertEqual(home['player_list'][0]['gameplayer_id']['id'], 9022)
        self.assertEqual(home['player_list'][0]['parameters'][107], 1)
        self.assertEqual(home['coach']['coach_id']['id'], 82)

    def test_missing_plan_or_coach_never_uses_captured_manager(self):
        self.select(101)
        prepared = my_league.prepare(self.conn, {'match_category': 'MYLEAGUE'})
        for sid in (99, 1):
            store.set_meta(self.conn, 'main_squad_id', str(sid)); self.conn.commit()
            if sid == 1:
                self.conn.execute('delete from coaches'); self.conn.commit()
            req = dict(game_id=prepared['game_id'], squad_id=sid, rqid=5, user_id=7)
            response = my_league.start(self.conn, {}, req, [{'gameplayer_id': {'id': 9000, 'serial': 1}}])
            self.assertEqual(response['result'], 'ERR_INVALID_PARAM')
            self.assertFalse(my_league.state(self.conn)['active']['started'])
        with self.assertRaises(protocol.MatchError):
            store.set_meta(self.conn, 'main_squad_id', '99'); self.conn.commit()
            my_league.start_refs(self.conn, {})


if __name__ == '__main__':
    unittest.main()
