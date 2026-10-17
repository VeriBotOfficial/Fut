"""Native producer-shaped statistics requests; no synthetic lifetime deltas.

Shape was checked against build/match-stats-investigation/775915c.c: nested
match_result.myteam_player_list carries participation; root goal/assist lists
contain one exact player identity per event and may be omitted when empty.
These are decoder tests, not a claim of a phone/runtime capture.
"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))

import club_store as store
import match_protocol as protocol
import match_stats


class NativeMatchStatsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.executescript(store.SCHEMA)
        self.conn.execute('CREATE TABLE owned_identity_aliases('
                          'id INTEGER,old_serial INTEGER,new_serial INTEGER,'
                          'PRIMARY KEY(id,old_serial))')
        store.set_meta(self.conn, 'owned_identity_version', '1')
        self.identities = [(10, 1), (10, 2), (20, 3), (30, 4), (40, 5), (50, 6)]
        for pid, serial in self.identities:
            player = {'gameplayer_id': self.identity(pid, serial),
                      'games': 7, 'goal': 4, 'assist': 2}
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (pid, serial, json.dumps(player)))
        self.conn.commit()
        self.active = {'game_id': 100, 'side': 'HOME', 'started': True,
                       'eligible_player_instances': [list(value) for value in self.identities]}
        self.resolved = {'home': 3, 'away': 0, 'side': 'HOME', 'gave_up': False,
                         'result': 'WIN', 'score_source': 'explicit_row_side'}

    @staticmethod
    def identity(pid=10, serial=1):
        return {'id': pid, 'serial': serial}

    def player_row(self, pid, serial, play_time=90, starting='YES', joined='NO'):
        return {'gameplayer_id': self.identity(pid, serial), 'play_time': play_time,
                'is_starting_member': starting, 'is_join_game': joined}

    def request(self):
        return {'match_result': {'game_id': 100, 'gameend_reason': 'NORMAL_END',
                                'abnormalend_reason': 'NOERR', 'myteam_player_list': [
            self.player_row(10, 1, 90, 'YES', 'NO'),
            self.player_row(10, 2, 30, 'NO', 'YES'),
            self.player_row(20, 3, 0, 'NO', 'NO'),
            self.player_row(30, 4, 0, 'NO', 'YES'),
            self.player_row(40, 5, 0, 'YES', 'NO'),
            self.player_row(50, 6, 1, 'NO', 'NO')]}}

    def decode(self, req=None, resolved=None):
        return match_stats.decode_result(self.conn, self.request() if req is None else req,
                                         self.active, self.resolved if resolved is None else resolved)

    @staticmethod
    def counters(rows):
        return {(row['gameplayer_id']['id'], row['gameplayer_id']['serial']):
                {key: row[key] for key in ('games', 'goal', 'assist')} for row in rows}

    def test_starters_used_subs_unused_subs_and_zero_minute_appearance(self):
        rows = self.counters(self.decode())
        self.assertEqual(set(rows), set(self.identities))
        for identity in self.identities:
            self.assertEqual(rows[identity], {'games': int(identity != (20, 3)),
                                              'goal': 0, 'assist': 0}, identity)

    def test_events_count_repeated_same_identity_and_keep_same_card_copies_separate(self):
        req = self.request()
        req['goal_player_list'] = [self.identity(), self.identity(), self.identity()]
        req['assist_player_list'] = [self.identity(10, 2), self.identity(10, 2)]
        rows = self.counters(self.decode(req))
        self.assertEqual(rows[10, 1], {'games': 1, 'goal': 3, 'assist': 0})
        self.assertEqual(rows[10, 2], {'games': 1, 'goal': 0, 'assist': 2})

    def test_owned_aliases_are_resolved_for_participation_and_both_event_lists(self):
        self.conn.executemany('INSERT INTO owned_identity_aliases VALUES(?,?,?)',
                              [(10, 0, 1), (10, 99, 2)])
        self.conn.commit()
        req = self.request()
        req['match_result']['myteam_player_list'][0]['gameplayer_id']['serial'] = 0
        req['match_result']['myteam_player_list'][1]['gameplayer_id']['serial'] = 99
        req['goal_player_list'] = [self.identity(10, 0), self.identity(10, 99)]
        req['assist_player_list'] = [self.identity(10, 99)]
        rows = self.counters(self.decode(req))
        self.assertEqual(rows[10, 1], {'games': 1, 'goal': 1, 'assist': 0})
        self.assertEqual(rows[10, 2], {'games': 1, 'goal': 1, 'assist': 1})
        self.assertNotIn((10, 0), rows)
        self.assertNotIn((10, 99), rows)

    def test_zero_zero_with_omitted_goal_and_assist_keys_still_records_appearances(self):
        resolved = dict(self.resolved, home=0, away=0, result='DRAW')
        req = self.request()
        self.assertNotIn('goal_player_list', req)
        self.assertNotIn('assist_player_list', req)
        rows = self.counters(self.decode(req, resolved))
        self.assertEqual(sum(row['games'] for row in rows.values()), 5)
        self.assertEqual(sum(row['goal'] + row['assist'] for row in rows.values()), 0)

    def test_opponent_lists_are_never_added_to_our_counters(self):
        req = self.request()
        req['goal_player_list'] = [self.identity()]
        req['opp_goal_player_list'] = [self.identity()] * 7
        req['opp_assist_player_list'] = [self.identity(10, 2)] * 7
        rows = self.counters(self.decode(req))
        self.assertEqual(rows[10, 1]['goal'], 1)
        self.assertEqual(rows[10, 2]['assist'], 0)

    def test_unassisted_goals_and_opponent_own_goals_do_not_fabricate_player_events(self):
        req = self.request()
        req['goal_player_list'] = [self.identity()]
        rows = self.counters(self.decode(req, dict(self.resolved, home=2)))
        self.assertEqual(sum(row['goal'] for row in rows.values()), 1)
        self.assertEqual(sum(row['assist'] for row in rows.values()), 0)

    def test_away_match_uses_our_away_score_for_event_ceiling(self):
        req = self.request()
        req['goal_player_list'] = [self.identity(), self.identity()]
        resolved = dict(self.resolved, side='AWAY', home=0, away=2)
        self.active['side'] = 'AWAY'
        self.assertEqual(self.counters(self.decode(req, resolved))[10, 1]['goal'], 2)
        with self.assertRaises(protocol.MatchError):
            self.decode(req, dict(resolved, away=1))

    def test_decoder_does_not_mutate_request_active_or_database(self):
        req = self.request()
        req['goal_player_list'] = [self.identity()]
        before_req, before_active = copy.deepcopy(req), copy.deepcopy(self.active)
        before_db = list(self.conn.iterdump())
        self.decode(req)
        self.assertEqual(req, before_req)
        self.assertEqual(self.active, before_active)
        self.assertEqual(list(self.conn.iterdump()), before_db)

    def test_forfeit_returns_no_statistics_even_if_partial_events_are_present(self):
        req = self.request()
        req['goal_player_list'] = [self.identity()] * 5
        self.assertIsNone(self.decode(req, dict(self.resolved, gave_up=True, home=0, away=3)))

    def test_missing_or_invalid_participation_list_is_rejected(self):
        for value in ('missing', None, {}, 'players', [None]):
            with self.subTest(value=value):
                req = self.request()
                if value == 'missing':
                    del req['match_result']['myteam_player_list']
                else:
                    req['match_result']['myteam_player_list'] = value
                with self.assertRaises(protocol.MatchError):
                    self.decode(req)

    def test_invalid_identity_in_participation_or_events_is_rejected(self):
        invalid = [self.identity(999, 1), self.identity(10, 999),
                   self.identity(10, '1'), self.identity(10, True),
                   self.identity(30, 0), {'id': 10}, None, 10]
        for identity in invalid:
            for location in ('participation', 'goal_player_list', 'assist_player_list'):
                with self.subTest(identity=identity, location=location):
                    req = self.request()
                    if location == 'participation':
                        req['match_result']['myteam_player_list'][0]['gameplayer_id'] = identity
                    else:
                        req[location] = [identity]
                    with self.assertRaises(protocol.MatchError):
                        self.decode(req)

    def test_duplicate_participation_rows_are_rejected_including_alias_duplicates(self):
        req = self.request()
        req['match_result']['myteam_player_list'].append(copy.deepcopy(
            req['match_result']['myteam_player_list'][0]))
        with self.assertRaises(protocol.MatchError):
            self.decode(req)
        self.conn.execute('INSERT INTO owned_identity_aliases VALUES(10,0,1)')
        self.conn.commit()
        req['match_result']['myteam_player_list'][-1]['gameplayer_id']['serial'] = 0
        with self.assertRaises(protocol.MatchError):
            self.decode(req)

    def test_invalid_play_time_and_flags_are_rejected(self):
        for field, values in (
                ('play_time', (-1, '90', True, 1.5, None)),
                ('is_starting_member', ('yes', 'INVALID', 1, True, None)),
                ('is_join_game', ('no', 'INVALID', 0, False, None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    req = self.request()
                    req['match_result']['myteam_player_list'][0][field] = value
                    with self.assertRaises(protocol.MatchError):
                        self.decode(req)

    def test_event_totals_above_our_score_are_rejected(self):
        for field in ('goal_player_list', 'assist_player_list'):
            with self.subTest(field=field):
                req = self.request()
                req[field] = [self.identity()] * 4
                with self.assertRaises(protocol.MatchError):
                    self.decode(req)

    def test_unused_bench_cannot_receive_a_goal_or_assist(self):
        for field in ('goal_player_list', 'assist_player_list'):
            with self.subTest(field=field):
                req = self.request()
                req[field] = [self.identity(20, 3)]
                with self.assertRaises(protocol.MatchError):
                    self.decode(req)

    def test_invalid_event_list_container_is_not_silently_treated_as_empty(self):
        for field in ('goal_player_list', 'assist_player_list'):
            for value in (None, {}, 'events', 0):
                with self.subTest(field=field, value=value):
                    req = self.request()
                    req[field] = value
                    with self.assertRaises(protocol.MatchError):
                        self.decode(req)


if __name__ == '__main__':
    unittest.main()
