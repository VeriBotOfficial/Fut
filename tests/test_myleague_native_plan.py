"""Native My League saves and starts use the separate special-plan protocol."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest

import test_match_myleague as base_tests
import club_store as store
import gate_logic
import match_protocol as protocol
import my_league


class MyLeagueNativePlanTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueMatchTest.setUp(self)
        self.assertEqual(self.select()['result'], 'NOERR')

    def select(self, team=101, rqid=1, **extra):
        return base_tests.MyLeagueMatchTest.select(self, team, rqid, **extra)

    def request(self, event_id=0, reverse=True, **extra):
        plan = copy.deepcopy(store.squads(self.conn)[1]['squad_data'])
        plan['player_list'] = copy.deepcopy(list(reversed(self.refs)) if reverse else self.refs)
        request = dict(event_id=event_id, rqid=10, user_id=7,
                       special_squad_data=dict(squad_data=plan, rental_team_id=108,
                           rental_gameplayer_list=[], liveupdate_version=2026091706,
                           initial_settings='NO'),
                       squad_update=dict(is_used_position_edit='YES'), squad_log={})
        request.update(extra)
        return request

    def save_special(self, request=None):
        return self.gate.handle('CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO',
                                self.request() if request is None else request)

    def native_start(self, **extra):
        prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT',
                                   dict(match_category='MYLEAGUE', rqid=20, user_id=7))
        request = dict(match_category='MYLEAGUE', game_id=prepared['game_id'],
                       player_list_home=[], player_list_away=[], team_id=101,
                       side='AWAY', is_vs_aimatch='YES', rqid=21, user_id=7)
        request.update(extra)
        return self.gate.handle('CMD_START_GAME', request)

    def test_native_special_save_then_empty_array_start_restores_23_current_players(self):
        ordinary = store.squads(self.conn)
        self.assertEqual(self.save_special()['result'], 'NOERR')
        reply = self.native_start()
        self.assertEqual(reply['result'], 'NOERR')
        home = reply['squad_data_home']
        self.assertEqual(len(home['player_list']), 23)
        self.assertEqual([p['gameplayer_id'] for p in home['player_list']],
                         [p['gameplayer_id'] for p in reversed(self.refs)])
        self.assertEqual(home['coach']['coach_id'], {'id': 82, 'serial': 1})
        self.assertEqual(home['squad_gameplan']['formation'][0]['team_style'], 2)
        self.assertEqual(home['player_list'][0]['parameters'][107], 1)
        self.assertEqual(home['team_id'], 101)
        self.assertEqual(reply['uniform_list_home']['base_team_id'], 101)
        self.assertEqual(store.squads(self.conn), ordinary)
        self.assertEqual(len(my_league.state(self.conn)['active']['eligible_player_instances']), 23)

    def test_existing_save_without_special_plan_accepts_native_empty_roster(self):
        reply = self.native_start()
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual([p['gameplayer_id'] for p in reply['squad_data_home']['player_list']],
                         [p['gameplayer_id'] for p in self.refs])
        self.assertEqual(reply['squad_data_home']['coach']['coach_id']['id'], 81)
        self.assertIsNone(my_league.season_special(my_league.state(self.conn)))

    def test_cached_match_roster_cannot_replace_special_gameplan_players(self):
        self.save_special()
        reply = self.native_start(player_list_home=self.refs)
        self.assertEqual([p['gameplayer_id'] for p in reply['squad_data_home']['player_list']],
                         [p['gameplayer_id'] for p in reversed(self.refs)])
        self.assertEqual(reply['squad_data_home']['coach']['coach_id']['id'], 82)

    def test_non_native_squad_id_cannot_mix_ordinary_plan_with_special_players(self):
        self.save_special()
        reply = self.native_start(squad_id=1)
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual(reply['squad_data_home']['coach']['coach_id']['id'], 82)
        self.assertEqual(reply['squad_data_home']['player_list'][0]['gameplayer_id'],
                         self.refs[-1]['gameplayer_id'])

    def test_special_plan_and_native_initial_settings_survive_database_reopen(self):
        self.save_special()
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        self.addCleanup(reopened.close)
        reply = my_league.select(reopened, {}, dict(select_team_id=0, rqid=30, user_id=7))
        data = reply['special_dream_squad_data']
        self.assertEqual(data['initial_settings'], 'YES')
        self.assertEqual(data['rental_team_id'], 101)
        self.assertEqual(data['squad_data']['player_list'][0]['gameplayer_id'],
                         self.refs[-1]['gameplayer_id'])
        self.assertEqual(my_league.selected_squad(reopened, {})['squad_data'], data['squad_data'])

    def test_returned_season_plan_is_ready_without_rewriting_saved_client_flag(self):
        self.save_special()
        saved = my_league.state(self.conn)
        self.assertEqual(saved['special_squad']['data']['initial_settings'], 'NO')
        reply = self.select(0, rqid=31)
        self.assertEqual(reply['special_dream_squad_data']['initial_settings'], 'YES')
        self.assertEqual(reply['special_dream_squad_data']['squad_data'],
                         saved['special_squad']['data']['squad_data'])
        self.assertEqual(my_league.state(self.conn), saved)

    def test_ready_season_plan_does_not_require_a_present_ordinary_main_plan(self):
        self.save_special()
        store.set_meta(self.conn, 'main_squad_id', '999999')
        self.conn.commit()
        reply = self.select(0, rqid=32)
        special = reply['special_dream_squad_data']
        self.assertEqual(special['initial_settings'], 'YES')
        self.assertEqual(special['squad_data']['player_list'][0]['gameplayer_id'],
                         self.refs[-1]['gameplayer_id'])
        started = self.native_start(match_category='SEASON_MODE')
        self.assertEqual(started['result'], 'NOERR')
        self.assertEqual(started['squad_data_home']['coach']['coach_id']['id'], 82)
        self.assertEqual([row['gameplayer_id'] for row in started['squad_data_home']['player_list']],
                         [row['gameplayer_id'] for row in reversed(self.refs)])

    def test_other_event_or_mode_does_not_overwrite_the_bound_season_plan(self):
        self.save_special()
        before = my_league.state(self.conn)
        self.save_special(self.request(event_id=900, reverse=False))
        self.assertEqual(my_league.state(self.conn), before)
        protocol.remember_mode(self.conn, protocol.AI)
        self.save_special(self.request(reverse=False))
        self.assertEqual(my_league.state(self.conn), before)

    def test_different_team_starts_a_new_plan_and_does_not_reuse_old_event_binding(self):
        self.save_special()
        self.assertEqual(self.select(108, rqid=40)['result'], 'NOERR')
        entered = self.select(0, rqid=41)['special_dream_squad_data']
        self.assertEqual(entered['rental_team_id'], 108)
        self.assertEqual(entered['squad_data']['player_list'][0]['gameplayer_id'],
                         self.refs[0]['gameplayer_id'])
        self.assertEqual(self.save_special(self.request(event_id=900))['result'], 'NOERR')
        saved = my_league.state(self.conn)['special_squad']
        self.assertEqual(saved['event_id'], 900)
        self.assertEqual(saved['data']['rental_team_id'], 108)

    def test_partial_save_keeps_existing_players_coach_and_other_formations(self):
        self.save_special()
        before = copy.deepcopy(my_league.season_special(my_league.state(self.conn)))
        changed = copy.deepcopy(before['squad_data']['squad_gameplan'])
        changed['formation'][0]['team_style'] = 4
        request = dict(event_id=0, rqid=11, user_id=7,
                       special_squad_data=dict(squad_data={'squad_gameplan': changed}))
        self.assertEqual(self.save_special(request)['result'], 'NOERR')
        after = my_league.season_special(my_league.state(self.conn))
        self.assertEqual(after['squad_data']['player_list'], before['squad_data']['player_list'])
        self.assertEqual(after['squad_data']['coach'], before['squad_data']['coach'])
        self.assertEqual(after['squad_data']['squad_gameplan']['formation'][1],
                         before['squad_data']['squad_gameplan']['formation'][1])

    def test_unknown_player_or_coach_is_rejected_without_replacing_saved_plan(self):
        self.save_special()
        before = my_league.state(self.conn)
        for field in ('player', 'coach'):
            request = self.request()
            sd = request['special_squad_data']['squad_data']
            if field == 'player':
                sd['player_list'][0]['gameplayer_id'] = {'id': 999999, 'serial': 1}
            else:
                sd['coach'] = [{'coach_id': {'id': 999999, 'serial': 1}}]
            self.assertEqual(self.save_special(request)['result'], 'ERR_INVALID_PARAM')
            self.assertEqual(my_league.state(self.conn), before)

    def test_released_player_is_shown_empty_and_never_recreated_from_special_save(self):
        self.save_special()
        removed = self.refs[-1]['gameplayer_id']
        self.conn.execute('DELETE FROM gameplayers WHERE id=? AND serial=?',
                          (removed['id'], removed['serial']))
        self.conn.commit()
        entered = self.select(0, rqid=44)['special_dream_squad_data']
        self.assertEqual(entered['squad_data']['player_list'][0]['gameplayer_id'],
                         {'id': 0, 'serial': 0})
        self.assertIsNone(store.player_by_key(self.conn, removed['id'], removed['serial']))
        self.assertEqual(self.save_special()['result'], 'ERR_INVALID_PARAM')

    def test_native_save_accepts_only_request_field_and_start_rejects_wrong_roster_type(self):
        request = self.request()
        request['special_dream_squad_data'] = request.pop('special_squad_data')
        self.assertEqual(self.save_special(request)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.native_start(player_list_home={})['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(my_league.state(self.conn)['active']['started'])


if __name__ == '__main__':
    unittest.main()
