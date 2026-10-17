"""START caches never replace the saved My League roster or create cards."""
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_match_myleague as base_tests
import club_store as store
import gate_logic
import match_diagnostics
import match_protocol as protocol
import my_league


EMPTY = {'gameplayer_id': {'id': 0, 'serial': 0}}


class MyLeagueStartSlotsTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueMatchTest.setUp(self)
        store.player_identity.migrate(self.conn)
        self.refs = copy.deepcopy(store.squads(self.conn)[0]['squad_data']['player_list'])
        self.assertEqual(my_league.select(self.conn, {}, dict(
            select_team_id=101, select_compe_unit_id=0, rqid=1))['result'], 'NOERR')
        self.prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(
            match_category='MYLEAGUE', rqid=2))

    def start(self, refs, gate=None):
        return (gate or self.gate).handle('CMD_START_GAME', dict(
            game_id=self.prepared['game_id'], match_category='MYLEAGUE',
            player_list_home=copy.deepcopy(refs), player_list_away=[],
            side=my_league.state(self.conn)['active']['side'], rqid=3))

    def latest(self):
        return json.loads(self.conn.execute("SELECT detail FROM events WHERE "
            "kind='efootxi_match' ORDER BY id DESC LIMIT 1").fetchone()[0])

    def test_exact_empty_sentinels_use_saved_plan_with_identity_checks_enabled(self):
        out = self.start([EMPTY] * 40)
        self.assertEqual(out['result'], 'NOERR')
        self.assertEqual([x['gameplayer_id'] for x in out['squad_data_home']['player_list']],
                         [x['gameplayer_id'] for x in self.refs])
        self.assertTrue(my_league.state(self.conn)['active']['started'])

    def test_mixed_cached_empty_bench_slots_do_not_replace_saved_order(self):
        selected = list(reversed(self.refs))
        refs = [EMPTY] + selected[:11] + [EMPTY] * 8 + selected[11:] + [EMPTY] * 8
        self.assertEqual(len(refs), 40)
        out = self.start(refs)
        self.assertEqual(out['result'], 'NOERR')
        self.assertEqual([x['gameplayer_id'] for x in out['squad_data_home']['player_list']],
                         [x['gameplayer_id'] for x in self.refs])
        self.assertEqual(len(my_league.state(self.conn)['active']['eligible_player_instances']), 23)

    def test_unknown_or_duplicate_cached_cards_do_not_select_players(self):
        for refs in ([self.refs[0], self.refs[0], EMPTY],
                     [dict(gameplayer_id=dict(id=999999, serial=999999)), EMPTY]):
            with self.subTest(refs=refs):
                out = self.start(refs)
                self.assertEqual(out['result'], 'NOERR')
                self.assertEqual([x['gameplayer_id'] for x in out['squad_data_home']['player_list']],
                                 [x['gameplayer_id'] for x in self.refs])
        self.assertIsNone(store.player_by_key(self.conn, 999999, 999999))
        self.assertEqual(my_league.state(self.conn)['played'], {})

    def test_ignored_cache_contents_cannot_change_the_saved_roster(self):
        for refs in ([dict(gameplayer_id=dict(id=0, serial=1))],
                     [dict(gameplayer_id=dict(id=9000, serial=0))],
                     [dict(gameplayer_id=dict(id=False, serial=0))],
                     [dict(gameplayer_id={})], [None], [EMPTY] * 41):
            with self.subTest(refs=refs):
                self.assertEqual(self.start(refs)['result'], 'NOERR')
                self.assertEqual(my_league.start_refs(self.conn, {'player_list_home': refs}), self.refs)

    def test_present_cache_must_still_be_an_array(self):
        before = my_league.state(self.conn)
        for refs in ({}, None, 'invalid'):
            with self.subTest(refs=refs):
                self.assertEqual(self.start(refs)['result'], 'ERR_INVALID_PARAM')
                self.assertEqual(my_league.state(self.conn), before)
                self.assertEqual(self.latest()['error'], 'invalid_match_squad')

    def test_empty_slot_start_survives_database_reopen(self):
        reopened = sqlite3.connect(Path(self.temp.name) / 'club.sqlite3')
        self.addCleanup(reopened.close)
        gate = gate_logic.GateLogic({}, conn=reopened)
        out = self.start(self.refs + [EMPTY], gate)
        self.assertEqual(out['result'], 'NOERR')
        self.assertTrue(my_league.state(reopened)['active']['started'])

    def test_old_boundary_reproduces_prepared_only_failure_and_new_report_explains_it(self):
        with patch.object(my_league, 'start_refs', return_value=self.refs + [EMPTY]):
            self.assertEqual(self.start(self.refs + [EMPTY])['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(my_league.state(self.conn)['active']['started'])
        self.assertEqual(self.latest()['error'], 'unowned_or_duplicate_match_squad_player')
        self.assertEqual(self.start(self.refs + [EMPTY])['result'], 'NOERR')

    def test_unknown_exception_text_is_not_disclosed_in_feedback(self):
        for error in (ValueError('private-token path user@example.com'),
                      ValueError('private_token_abcdefgh'),
                      protocol.MatchError('private_token_abcdefgh')):
            protocol.reject(self.conn, 'CMD_START_GAME', dict(
                game_id=self.prepared['game_id']), error, protocol.SEASON)
            self.assertEqual(self.latest()['error'], 'unclassified_request_error')
            report = match_diagnostics.report_text(self.conn)
            self.assertNotIn('private-token', report)
            self.assertNotIn('private_token', report)
            self.assertNotIn('user@example.com', report)


if __name__ == '__main__':
    unittest.main()
