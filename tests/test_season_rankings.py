"""Per-season native leaderboard rows, actual events and CPU simulations."""
import copy
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))

import club_store as store
import match_protocol as protocol
import match_stats
import season_rankings as rankings
import ziru_data


class SeasonRankingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'club.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.conn.executescript(store.SCHEMA)
        self.conn.execute('CREATE TABLE managed_cards(card_id TEXT PRIMARY KEY,payload TEXT)')
        for pid, serial in ((900, 1), (900, 2), (901, 3)):
            player = dict(gameplayer_id=dict(id=pid, serial=serial), games=100, goal=50, assist=40)
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (pid, serial, json.dumps(player)))
        self.rosters = {'101': [1011, 1012], '102': [1020, 1021, 1022], '103': [1030, 1031]}
        for pid, position in ((1020, 0), (1021, 12), (1022, 8), (1030, 0), (1031, 12)):
            self.conn.execute('INSERT INTO managed_cards VALUES(?,?)',
                              (str(pid), json.dumps({'parameters': [position]})))
        mock = patch.object(ziru_data, 'rosters', return_value=self.rosters)
        mock.start(); self.addCleanup(mock.stop)
        canonical = patch.object(rankings, 'CANONICAL_PATH', Path(self.temp.name) / 'no-canonical.json')
        canonical.start(); self.addCleanup(canonical.stop)
        self.season = dict(season=1, competition_id=113, team_id=100)
        self.active = dict(game_id=100, fixture=1, side='HOME', started=True,
                           eligible_player_instances=[[900, 1], [900, 2], [901, 3]],
                           opponent={'team_data': {'base_team_id': 101}})
        rankings.freeze_context(self.active, self.season)
        self.resolved = dict(home=3, away=2, side='HOME', gave_up=False, result='WIN')
        self.conn.commit()

    @staticmethod
    def identity(pid=900, serial=1):
        return dict(id=pid, serial=serial)

    def request(self, game_id=100):
        return dict(match_result=dict(game_id=game_id, myteam_player_list=[
            dict(gameplayer_id=self.identity(pid, serial), play_time=90,
                 is_starting_member='YES', is_join_game='NO')
            for pid, serial in ((900, 1), (900, 2), (901, 3))]),
            goal_player_list=[self.identity(), self.identity(), self.identity(900, 2)],
            assist_player_list=[self.identity(901, 3), self.identity(901, 3)],
            opp_goal_player_list=[1011, 1011], opp_assist_player_list=[1012])

    def record(self, req=None, active=None, resolved=None):
        return match_stats.record_result(self.conn, protocol.SEASON,
            self.request() if req is None else req, self.active if active is None else active,
            self.resolved if resolved is None else resolved)

    def tables(self, season=None):
        return rankings.populate(self.conn, self.season if season is None else season, {})

    def records(self):
        return [json.loads(row[0]) for row in self.conn.execute(
            'SELECT value FROM meta WHERE key LIKE ?', (rankings.PREFIX + '%',))]

    def test_actual_native_events_include_both_sides_and_only_this_season(self):
        self.conn.execute('BEGIN IMMEDIATE'); self.assertEqual(self.record(), 'applied'); self.conn.commit()
        tables = self.tables()
        self.assertEqual(tables['goal_ranking'], [
            dict(rank=1, player_id=900, team_id=100, value=3, is_com='NO'),
            dict(rank=2, player_id=1011, team_id=101, value=2, is_com='YES')])
        self.assertEqual(tables['assist_ranking'], [
            dict(rank=1, player_id=901, team_id=100, value=2, is_com='NO'),
            dict(rank=2, player_id=1012, team_id=101, value=1, is_com='YES')])
        # Card copies aggregate only in the serial-less native season table.
        counters = [json.loads(row[0])['goal'] for row in self.conn.execute(
            'SELECT json FROM gameplayers WHERE id=900 ORDER BY serial')]
        self.assertEqual(counters, [52, 51])

    def test_real_match_replay_survives_reopen_and_never_double_counts(self):
        self.conn.execute('BEGIN IMMEDIATE'); self.record(); self.conn.commit()
        expected = self.tables(); self.conn.close(); self.conn = sqlite3.connect(self.path)
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.replay_result(self.conn, protocol.SEASON, self.request())
        self.conn.commit(); self.assertEqual(self.tables(), expected)
        self.assertEqual(json.loads(self.conn.execute('SELECT json FROM gameplayers WHERE id=900 AND serial=1').fetchone()[0])['games'], 101)

    def test_pending_own_stats_backfill_without_repeating_opponent_events(self):
        request = self.request(); request['match_result'].pop('myteam_player_list')
        self.conn.execute('BEGIN IMMEDIATE'); self.assertEqual(self.record(request), 'pending'); self.conn.commit()
        self.assertEqual(self.tables()['goal_ranking'], [
            dict(rank=1, player_id=1011, team_id=101, value=2, is_com='YES')])
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.replay_result(self.conn, protocol.SEASON, self.request()); self.conn.commit()
        self.assertEqual([row['value'] for row in self.tables()['goal_ranking']], [3, 2])

    def test_bad_opponent_vector_does_not_discard_valid_owned_counters(self):
        for bad in ([9999], [True], [{'id': 1011, 'serial': 0}], [1011] * 3, '1011'):
            with self.subTest(bad=bad):
                request = self.request(); request['opp_goal_player_list'] = bad
                self.conn.execute('BEGIN IMMEDIATE'); self.assertEqual(self.record(request), 'applied')
                saved = self.records()[0]
                self.assertNotIn('opponent', saved); self.assertIn('opponent_pending', saved)
                self.assertEqual(self.tables()['goal_ranking'][0]['value'], 3)
                self.conn.rollback()

    def test_pending_opponent_can_backfill_when_owned_stats_already_applied(self):
        request = self.request(); request['opp_goal_player_list'] = [9999]
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.replay_result(self.conn, protocol.SEASON, self.request()); self.conn.commit()
        self.assertEqual([row['value'] for row in self.tables()['goal_ranking']], [3, 2])
        self.assertNotIn('opponent_pending', self.records()[0])

    def test_missing_nonzero_opponent_goals_remain_pending_then_backfill(self):
        request = self.request(); request.pop('opp_goal_player_list')
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        self.assertNotIn('opponent', self.records()[0])
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.replay_result(self.conn, protocol.SEASON, self.request()); self.conn.commit()
        self.assertEqual([row['value'] for row in self.tables()['goal_ranking']], [3, 2])

    def test_zero_score_omitted_events_and_partial_explicit_scorers_are_not_invented(self):
        request = self.request(); request.pop('opp_goal_player_list'); request.pop('opp_assist_player_list')
        self.conn.execute('BEGIN IMMEDIATE')
        self.record(request, resolved=dict(self.resolved, away=0))
        self.assertEqual(self.records()[0]['opponent'], [])
        self.conn.rollback()
        request = self.request(); request['opp_goal_player_list'] = [1011]
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        self.assertEqual(next(row['value'] for row in self.tables()['goal_ranking']
                              if row['is_com'] == 'YES'), 1)

    def test_conflicting_accepted_opponent_events_are_rejected(self):
        self.conn.execute('BEGIN IMMEDIATE'); self.record(); self.conn.commit()
        before = self.tables(); request = self.request(); request['opp_goal_player_list'] = [1012, 1012]
        self.conn.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(protocol.MatchError, 'conflicting_season_ranking_retry'):
            match_stats.replay_result(self.conn, protocol.SEASON, request)
        self.conn.rollback(); self.assertEqual(self.tables(), before)

    def test_pending_old_season_retry_cannot_pollute_new_season(self):
        request = self.request(); request['match_result'].pop('myteam_player_list')
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        newer = dict(self.season, season=2, team_id=104)
        self.assertEqual(self.tables(newer), {'goal_ranking': [], 'assist_ranking': []})
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.replay_result(self.conn, protocol.SEASON, self.request()); self.conn.commit()
        self.assertEqual(self.tables()['goal_ranking'][0]['value'], 3)
        self.assertEqual(self.tables(newer), {'goal_ranking': [], 'assist_ranking': []})

    def test_fixture_simulation_preserves_team_membership_and_score_conservation(self):
        matches = [dict(home_team_id=100, away_team_id=101, home_goals=3, away_goals=2),
                   dict(home_team_id=102, away_team_id=103, home_goals=6, away_goals=2)]
        self.conn.execute('BEGIN IMMEDIATE')
        rankings.record_fixture(self.conn, self.season, 1, matches)
        self.record(); self.conn.commit(); expected = self.tables()
        cpu_goals = defaultdict(int); cpu_assists = defaultdict(int)
        for field, total in (('goal_ranking', cpu_goals), ('assist_ranking', cpu_assists)):
            for row in expected[field]:
                if row['is_com'] == 'YES':
                    self.assertIn(row['player_id'], self.rosters[str(row['team_id'])])
                    self.assertNotIn(row['player_id'], (1020, 1030))  # verified GK metadata
                    total[row['team_id']] += row['value']
        self.assertEqual(dict(cpu_goals), {102: 6, 101: 2, 103: 2})
        for team, assists in cpu_assists.items(): self.assertLessEqual(assists, cpu_goals[team])
        self.conn.execute('BEGIN IMMEDIATE')
        rankings.record_fixture(self.conn, self.season, 1, matches)
        match_stats.replay_result(self.conn, protocol.SEASON, self.request())
        self.conn.commit(); self.assertEqual(self.tables(), expected)

    def test_tied_rank_uses_competition_ranking_and_native_scalar_fields(self):
        request = self.request(); request['goal_player_list'] = [self.identity(), self.identity(), self.identity(901, 3)]
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        rows = self.tables()['goal_ranking']
        self.assertEqual([row['rank'] for row in rows], [1, 1, 3])
        for row in rows:
            self.assertEqual(set(row), {'rank', 'player_id', 'team_id', 'value', 'is_com'})
            self.assertTrue(all(type(row[field]) is int for field in ('rank', 'player_id', 'team_id', 'value')))
            self.assertIn(row['is_com'], ('YES', 'NO'))

    def test_unavailable_roster_is_explicit_and_does_not_use_foreign_ids(self):
        matches = [dict(home_team_id=102, away_team_id=999, home_goals=2, away_goals=2)]
        self.conn.execute('BEGIN IMMEDIATE')
        rankings.record_fixture(self.conn, self.season, 1, matches); self.conn.commit()
        saved = self.records()[0]; self.assertEqual(saved['unavailable_teams'], [999])
        self.assertEqual({row['team_id'] for row in self.tables()['goal_ranking']}, {102})

    def test_missing_optional_roster_does_not_break_match_or_fabricate_cpu_events(self):
        active = copy.deepcopy(self.active); active.pop(rankings.CONTEXT)
        with patch.object(ziru_data, 'rosters', side_effect=ziru_data.NotPorted('test missing')):
            rankings.freeze_context(active, self.season)
            self.conn.execute('BEGIN IMMEDIATE'); self.record(active=active)
            rankings.record_fixture(self.conn, self.season, 1,
                [dict(home_team_id=102, away_team_id=103, home_goals=2, away_goals=1)])
            self.conn.commit()
        self.assertEqual({row['is_com'] for row in self.tables()['goal_ranking']}, {'NO'})

    def test_atomic_rollback_and_read_only_refresh(self):
        self.conn.execute('BEGIN IMMEDIATE'); self.record()
        rankings.record_fixture(self.conn, self.season, 1,
            [dict(home_team_id=102, away_team_id=103, home_goals=2, away_goals=1)])
        self.conn.rollback(); self.assertEqual(self.records(), [])
        before = self.conn.total_changes
        self.assertEqual(self.tables(), {'goal_ranking': [], 'assist_ranking': []})
        self.assertEqual(self.conn.total_changes, before)

    def test_new_roster_fills_only_missing_cpu_teams_without_rerolling_known_ones(self):
        matches = [dict(home_team_id=102, away_team_id=5145, home_goals=2, away_goals=1)]
        season = dict(self.season, played={'1': {'matches': matches}})
        self.conn.execute('BEGIN IMMEDIATE')
        rankings.record_fixture(self.conn, season, 1, matches); self.conn.commit()
        old = self.records()[0]
        self.assertEqual(old['unavailable_teams'], [5145])
        with patch.object(rankings, 'CANONICAL_PATH', REPO / 'gateway/outputs/reference/canonical_rosters.json'):
            self.conn.execute('BEGIN IMMEDIATE')
            rankings.repair_missing_fixtures(self.conn, season); self.conn.commit()
            repaired = self.records()[0]
            self.assertEqual(repaired['unavailable_teams'], [])
            self.assertEqual([row for row in repaired['rows'] if row['team_id'] == 102], old['rows'])
            remo = [row for row in repaired['rows'] if row['team_id'] == 5145]
            self.assertEqual(sum(row['goal'] for row in remo), 1)
            self.conn.execute('BEGIN IMMEDIATE')
            rankings.repair_missing_fixtures(self.conn, season); self.conn.commit()
            self.assertEqual(self.records()[0], repaired)

    def test_pending_old_opponent_rehydrates_native_ids_and_keeps_match_scope(self):
        self.active[rankings.CONTEXT].update(opponent_team_id=5145, opponent_roster=[])
        data_path = REPO / 'gateway/outputs/reference/canonical_rosters.json'
        native = json.loads(data_path.read_text(encoding='utf8'))
        pid = native['rosters']['5145'][0]
        request = self.request(); request['opp_goal_player_list'] = [pid, pid]
        request['opp_assist_player_list'] = []
        self.conn.execute('BEGIN IMMEDIATE'); self.record(request); self.conn.commit()
        self.assertNotIn('opponent', self.records()[0])
        owned_before = self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial').fetchall()
        with patch.object(rankings, 'CANONICAL_PATH', data_path):
            self.conn.execute('BEGIN IMMEDIATE')
            match_stats.replay_result(self.conn, protocol.SEASON, request); self.conn.commit()
        saved = self.records()[0]
        self.assertEqual(saved['opponent'], [dict(player_id=pid, team_id=5145,
                                                 is_com='YES', goal=2, assist=0)])
        context = json.loads(store.get_meta(self.conn, match_stats.state_key(protocol.SEASON, 100)))['active'][rankings.CONTEXT]
        for field in ('season', 'competition_id', 'team_id'):
            self.assertEqual(context[field], self.season[field])
        self.assertEqual(context['roster_source'], native['source']['cpk_sha256'])
        self.assertEqual(self.conn.execute('SELECT json FROM gameplayers ORDER BY id,serial').fetchall(), owned_before)

    def test_give_up_is_excluded_and_ai_mode_does_not_enter_season_tables(self):
        self.conn.execute('BEGIN IMMEDIATE'); self.record(resolved=dict(self.resolved, gave_up=True))
        self.conn.commit(); self.assertEqual(self.records(), [])
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.record_result(self.conn, protocol.AI, self.request(), self.active, self.resolved)
        self.conn.commit(); self.assertEqual(self.records(), [])


class NativeMyLeagueLeaderboardFlowTest(unittest.TestCase):
    """Exercise the shipped native templates through real season handlers."""
    def setUp(self):
        import test_match_myleague as base_tests
        base_tests.MyLeagueMatchTest.setUp(self)

    def select(self, *args, **kwargs):
        import test_match_myleague as base_tests
        return base_tests.MyLeagueMatchTest.select(self, *args, **kwargs)

    def test_finish_info_replay_and_restart_keep_both_tables_and_cpu_teams(self):
        import test_match_myleague as base_tests
        import my_league
        response = base_tests.MyLeagueMatchTest.start(self)
        self.assertEqual(response['result'], 'NOERR')
        active = my_league.state(self.conn)['active']
        context = active[rankings.CONTEXT]
        opponent = context['opponent_roster'][0]
        home, away = (3, 1) if active['side'] == 'HOME' else (1, 3)
        request = dict(msgid='CMD_GET_VSCOM_GAME_RESULT', rqid=60, user_id=7,
            match_result=dict(game_id=active['game_id'], gameend_reason='NORMAL_END',
                abnormalend_reason='NOERR', myteam_player_list=[
                    dict(gameplayer_id={'id': pid, 'serial': 1}, play_time=90,
                         is_starting_member='YES', is_join_game='NO')
                    for pid in (9000, 9001)]),
            score=[dict(side='HOME', score_1st=home), dict(side='AWAY', score_1st=away)],
            goal_player_list=[{'id': 9000, 'serial': 1}] * 3,
            assist_player_list=[{'id': 9001, 'serial': 1}] * 2,
            opp_goal_player_list=[opponent], opp_assist_player_list=[])
        ended = my_league.finish(self.conn, {}, request)
        self.assertEqual(ended['result'], 'NOERR')
        self.assertEqual(ended['game_end_result']['match_result']['game_res'], 'WIN')
        info = my_league.info(self.conn, ziru_data.season_info(), {})
        own_goals = [row for row in info['goal_ranking'] if row['is_com'] == 'NO']
        self.assertEqual(own_goals, [dict(rank=1, player_id=9000, team_id=101,
                                         value=3, is_com='NO')])
        own_assists = [row for row in info['assist_ranking'] if row['is_com'] == 'NO']
        self.assertEqual(len(own_assists), 1); self.assertEqual(own_assists[0]['value'], 2)
        self.assertIn(opponent, [row['player_id'] for row in info['goal_ranking']
                                if row['team_id'] == context['opponent_team_id']])
        saved = my_league.state(self.conn)
        self.assertEqual(saved['fixture'], 2)
        self.assertGreater(len({row['team_id'] for row in info['goal_ranking']}), 2)
        expected = {field: info[field] for field in ('goal_ranking', 'assist_ranking')}
        self.assertEqual(my_league.finish(self.conn, {}, request)['result'], 'NOERR')
        self.assertEqual(my_league.state(self.conn)['played'], saved['played'])
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        try:
            reopened_info = my_league.info(reopened, ziru_data.season_info(), {})
            self.assertEqual({field: reopened_info[field] for field in expected}, expected)
        finally:
            reopened.close()


if __name__ == '__main__':
    unittest.main()
