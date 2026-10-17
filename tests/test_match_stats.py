import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
HOST_DEPS = REPO.parents[1] / 'analysis/v120-investigation/draw-skills/host-deps'
if HOST_DEPS.is_dir():
    sys.path.insert(0, str(HOST_DEPS))

import club_store as store
import match_protocol as protocol
import match_stats


class MatchStatsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.executescript(store.SCHEMA)
        self.addCleanup(self.conn.close)
        for pid, serial in ((10, 1), (10, 2), (20, 3)):
            player = {'gameplayer_id': {'id': pid, 'serial': serial}, 'games': 5, 'goal': 2,
                      'assist': 3, 'level_info': {'level': 25}}
            self.conn.execute('INSERT INTO gameplayers(id,serial,json) VALUES(?,?,?)',
                              (pid, serial, json.dumps(player)))
        self.conn.commit()
        self.active = {'side': 'HOME', 'eligible_player_instances': [[10, 1], [20, 3]]}
        self.resolved = {'home': 2, 'away': 0, 'side': 'HOME', 'gave_up': False}

    def row(self, pid=10, serial=1, games=1, goal=1, assist=0):
        return {'gameplayer_id': {'id': pid, 'serial': serial}, 'games': games, 'goal': goal, 'assist': assist}

    def apply(self, rows, game_id=100):
        return match_stats.apply_confirmed_deltas(self.conn, protocol.AI, game_id, rows,
                                                  self.active, self.resolved, source='test_confirmed_schema')

    def player(self, pid=10, serial=1):
        return json.loads(self.conn.execute('SELECT json FROM gameplayers WHERE id=? AND serial=?',
                                             (pid, serial)).fetchone()[0])

    def test_updates_exact_instance_and_does_not_count_unused_bench(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.apply([self.row(), self.row(20, 3, games=0, goal=0)]))
        self.conn.commit()
        self.assertEqual(self.player()['games'], 6)
        self.assertEqual(self.player()['goal'], 3)
        self.assertEqual(self.player()['assist'], 3)
        self.assertEqual(self.player(10, 2)['games'], 5)
        self.assertEqual(self.player(20, 3)['games'], 5)
        self.assertEqual(self.player()['level_info'], {'level': 25})

    def test_repeated_stats_do_not_double_count_and_conflicting_retry_rejected(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.apply([self.row()]))
        self.conn.commit()
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertFalse(self.apply([self.row()]))
        with self.assertRaises(protocol.MatchError):
            self.apply([self.row(goal=2)])
        self.conn.rollback()
        self.assertEqual(self.player()['goal'], 3)

    def test_game_ids_keep_matches_separate(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.apply([self.row()], 100)
        self.apply([self.row()], 101)
        self.conn.commit()
        self.assertEqual(self.player()['games'], 7)

    def test_rollback_removes_counter_changes_and_receipt_together(self):
        self.conn.execute('BEGIN IMMEDIATE')
        self.apply([self.row()])
        self.conn.rollback()
        self.assertEqual(self.player()['games'], 5)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertTrue(self.apply([self.row()]))
        self.conn.commit()
        self.assertEqual(self.player()['games'], 6)

    def test_unknown_identity_wrong_instance_and_duplicate_rows_rejected_before_write(self):
        for rows in ([self.row(), self.row(99, 99)], [self.row(serial=2)], [self.row(), self.row()]):
            with self.subTest(rows=rows):
                self.conn.execute('BEGIN IMMEDIATE')
                with self.assertRaises(protocol.MatchError):
                    self.apply(rows)
                self.conn.rollback()
                self.assertEqual(self.player()['games'], 5)

    def test_invalid_deltas_or_impossible_goal_totals_are_not_guessed(self):
        for row in (self.row(games=23), self.row(games=0, goal=1), self.row(goal=-1),
                    self.row(goal='1'), self.row(goal=3), self.row(assist=3)):
            with self.subTest(row=row):
                self.conn.execute('BEGIN IMMEDIATE')
                with self.assertRaises(protocol.MatchError):
                    self.apply([row])
                self.conn.rollback()
        self.assertEqual(self.player()['games'], 5)

    def test_unmapped_native_request_logs_shape_without_updating_stats_or_leaking_values(self):
        req = {'game_id': 100, 'token': 'very-secret', 'player_result_list': [
            {'gameplayer_id': {'id': 10, 'serial': 1}, 'mystery_goal_counter': 1000}]}
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.observe_unmapped(self.conn, protocol.AI, req)
        match_stats.observe_unmapped(self.conn, protocol.AI, req)
        self.conn.commit()
        self.assertEqual(self.player()['games'], 5)
        rows = self.conn.execute("SELECT detail FROM events WHERE kind='efootxi_match_stats'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn('very-secret', rows[0][0])
        self.assertIn('mystery_goal_counter', rows[0][0])
        self.assertEqual(json.loads(rows[0][0])['status'], 'awaiting_native_stats_schema')

    def test_roster_snapshot_does_not_assume_anyone_appeared(self):
        active = {}
        match_stats.snapshot_roster(active, [self.row(), self.row(20, 3), self.row()])
        self.assertEqual(active['eligible_player_instances'], [[10, 1], [20, 3]])
        self.assertEqual(self.player()['games'], 5)

    def test_stats_reject_missing_transaction_or_missing_roster_evidence(self):
        with self.assertRaises(protocol.MatchError):
            self.apply([self.row()])
        self.conn.execute('BEGIN IMMEDIATE')
        self.active = {'side': 'HOME'}
        with self.assertRaises(protocol.MatchError):
            self.apply([self.row()])


if __name__ == '__main__':
    unittest.main()
