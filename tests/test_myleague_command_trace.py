"""Privacy and transaction boundaries of actual gate command milestones."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_match_myleague as base_tests
import club_store as store
import match_diagnostics
import match_protocol as protocol
import my_league
import myleague_trace as trace


class MyLeagueCommandTraceTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueMatchTest.setUp(self)
        store.player_identity.migrate(self.conn)
        self.refs = store.player_identity.canonical_request(self.conn, self.refs)

    def select(self):
        return self.gate.handle('CMD_SET_SEASON_ENTER', dict(
            select_team_id=101, select_compe_unit_id=0, rqid=1, user_id=7))

    def prepare(self):
        return self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(
            match_category='MYLEAGUE', rqid=2, user_id=7))

    def raw(self):
        return [row[0] for row in self.conn.execute(
            'SELECT detail FROM events WHERE kind=? ORDER BY id', (trace.KIND,))]

    def test_actual_gate_sequence_records_responses_in_order_without_changing_state(self):
        self.assertEqual(self.select()['result'], 'NOERR')
        self.gate.handle('CMD_CHECK_MATCH_ENABLE', dict(rqid=2))
        before = my_league.state(self.conn)
        self.gate.handle('CMD_GET_SEASON_INFO', dict(rqid=3))
        self.assertEqual(my_league.state(self.conn), before)
        prepared = self.prepare()
        result = self.gate.handle('CMD_START_GAME', dict(game_id=prepared['game_id'],
            match_category='MYLEAGUE', player_list_home=[], player_list_away=[],
            side='AWAY', rqid=4))
        self.assertEqual(result['result'], 'NOERR')
        steps = trace.recent(self.conn)
        self.assertEqual([step['command'] for step in steps], [
            'CMD_SET_SEASON_ENTER', 'CMD_CHECK_MATCH_ENABLE', 'CMD_GET_SEASON_INFO',
            'CMD_GET_VSCOM_OPPONENT', 'CMD_START_GAME'])
        self.assertEqual(steps[-1]['counts'], dict(request_home=0, request_away=0, response_home=23,
            shared_card_ids=0, cache_only_card_ids=0, reply_only_card_ids=23, same_card_id_order=0))
        self.assertEqual(steps[-1]['request_side'], 'AWAY')
        self.assertEqual(my_league.state(self.conn)['played'], {})

    def test_real_start_rejection_retains_reviewed_error_without_advancing_fixture(self):
        self.select(); prepared = self.prepare()
        before = my_league.state(self.conn)
        reply = self.gate.handle('CMD_START_GAME', dict(game_id=prepared['game_id'],
            match_category='MYLEAGUE', player_list_home={}, rqid=4))
        self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        last = trace.recent(self.conn)[-1]
        self.assertEqual(last['error'], 'invalid_match_squad')
        self.assertNotIn('request_home', last.get('counts', {}))
        self.assertEqual(my_league.state(self.conn), before)

    def test_ai_categories_and_identified_ai_matches_are_not_traced(self):
        self.select(); before = self.raw()
        self.gate.handle('CMD_CHECK_MATCH_ENABLE', dict(match_category='LEAGUE_COM'))
        self.assertEqual(self.raw(), before)
        store.set_meta(self.conn, 'ai_division_active_match', json.dumps(dict(game_id=123, started=True)))
        self.conn.commit()
        self.gate.handle('CMD_GET_VSCOM_GAME_RESULT', dict(game_id=123, match_category='MYLEAGUE'))
        self.assertEqual(self.raw(), before)
        protocol.remember_mode(self.conn, protocol.AI)
        self.gate.handle('CMD_CHECK_MATCH_ENABLE', {})
        self.assertEqual(self.raw(), before)

    def test_caller_transaction_is_never_committed_or_rolled_back_by_trace(self):
        self.select(); before = self.raw()
        store.set_meta(self.conn, 'uncommitted_business_value', 'pending')
        self.assertTrue(self.conn.in_transaction)
        self.assertFalse(trace.record(self.conn, 'CMD_START_GAME',
            dict(match_category='MYLEAGUE'), dict(result='ERR_INVALID_PARAM')))
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(store.get_meta(self.conn, 'uncommitted_business_value'), 'pending')
        self.conn.rollback()
        self.assertEqual(store.get_meta(self.conn, 'uncommitted_business_value'), '')
        self.assertEqual(self.raw(), before)

    def test_trace_storage_failure_cannot_replace_successful_business_response(self):
        self.select()
        self.conn.execute("CREATE TRIGGER fail_trace BEFORE INSERT ON events "
            "WHEN NEW.kind='efootxi_myleague_trace' BEGIN SELECT RAISE(ABORT,'trace unavailable'); END")
        self.conn.commit()
        reply = self.gate.handle('CMD_CHECK_MATCH_ENABLE', {})
        self.assertEqual(reply['result'], 'NOERR')
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(my_league.state(self.conn)['fixture'], 1)

    def test_unexpected_business_exception_still_propagates(self):
        self.select(); prepared = self.prepare(); before = self.raw()
        with patch.object(self.gate, '_home_entries', side_effect=RuntimeError('business failure')):
            with self.assertRaisesRegex(RuntimeError, 'business failure'):
                self.gate.handle('CMD_START_GAME', dict(game_id=prepared['game_id'],
                    match_category='MYLEAGUE', player_list_home=[], rqid=4))
        self.assertEqual(len(self.raw()), len(before) + 1)
        last = trace.recent(self.conn)[-1]
        self.assertEqual(last['command'], 'CMD_START_GAME')
        self.assertEqual(last['result'], 'EXCEPTION')
        self.assertEqual(last['exception'], 'RuntimeError')
        self.assertNotIn('business failure', '\n'.join(self.raw()))
        self.assertFalse(my_league.state(self.conn)['active']['started'])

    def test_exception_with_open_business_transaction_keeps_entry_receipt_without_committing(self):
        self.select(); before = len(self.raw())

        def fail(mid, request):
            store.set_meta(self.conn, 'unfinished_business', 'must remain uncommitted')
            raise RuntimeError('private-account-token')

        with patch.object(self.gate, '_handle', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'private-account-token'):
                self.gate.handle('CMD_CHECK_MATCH_ENABLE', {})
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(store.get_meta(self.conn, 'unfinished_business'), 'must remain uncommitted')
        self.conn.rollback()
        self.assertEqual(store.get_meta(self.conn, 'unfinished_business'), '')
        self.assertEqual(len(self.raw()), before + 1)
        self.assertEqual(trace.recent(self.conn)[-1]['result'], 'ENTERED')
        self.assertNotIn('private-account-token', '\n'.join(self.raw()))

    def test_response_finishes_the_same_row_and_exception_records_only_reviewed_codes(self):
        self.select(); before = len(self.raw())
        request = dict(match_category='MYLEAGUE', player_list_home=[])
        trace_id = trace.begin_entry(self.conn, 'CMD_START_GAME', request)
        self.assertEqual(len(self.raw()), before + 1)
        self.assertEqual(trace.recent(self.conn)[-1]['result'], 'ENTERED')
        self.assertTrue(trace.finish_entry(self.conn, trace_id, 'CMD_START_GAME', request,
            error=protocol.MatchError('unknown_match_coach')))
        self.assertEqual(len(self.raw()), before + 1)
        last = trace.recent(self.conn)[-1]
        self.assertEqual((last['result'], last['exception'], last['error']),
                         ('EXCEPTION', 'MatchError', 'unknown_match_coach'))
        self.assertFalse(trace.finish_entry(self.conn, trace_id, 'CMD_START_GAME', request,
                                           dict(result='NOERR')))

    def test_trace_never_retains_account_fields_identities_or_arbitrary_error_text(self):
        self.select()
        sensitive = dict(password='private-password', email='private@example.com',
            token='private-token', user_name='private-name', user_id=987654321,
            side='private-side', player_list_home=[dict(gameplayer_id=dict(id=987654322, serial=987654323))])
        self.gate.responses['CMD_CHECK_MATCH_ENABLE'] = dict(result='private-result',
            msgid='CMD_CHECK_MATCH_ENABLE', token='private-response-token')
        self.gate.handle('CMD_CHECK_MATCH_ENABLE', sensitive)
        raw = '\n'.join(self.raw())
        report = match_diagnostics.report_text(self.conn)
        for private in ('private-', 'private@example.com', '987654321', '987654322', '987654323'):
            self.assertNotIn(private, raw)
            self.assertNotIn(private, report)
        self.assertEqual(trace.recent(self.conn)[-1], dict(command='CMD_CHECK_MATCH_ENABLE',
            result='OTHER', counts=dict(request_home=1)))

    def test_retention_is_bounded_and_report_reads_do_not_change_database(self):
        self.select()
        for count in range(80):
            self.assertTrue(trace.record(self.conn, 'CMD_SEND_MATCH_TEAM_INFO',
                dict(player_list_home=[None] * count), dict(result='NOERR')))
        self.assertEqual(len(self.raw()), trace.KEEP)
        rows = trace.recent(self.conn)
        self.assertEqual([row['counts']['request_home'] for row in rows], list(range(72, 80)))
        before = self.conn.total_changes
        report = match_diagnostics.report_text(self.conn)
        self.assertIn('我的联赛最近步骤（从早到晚）', report)
        self.assertIn('不能证明已经进入球场', report)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(len(self.raw()), trace.KEEP)

    def test_report_sanitizes_malformed_retained_trace(self):
        self.select()
        for value in (dict(command='CMD_START_GAME', result=['private-token'],
                           error=['private-token'], counts=['private-token']),
                      dict(command=['private-token'], result='NOERR'),
                      dict(command='CMD_START_GAME', result='NOERR',
                           counts=dict(request_home=True, token='private-token'))):
            store.log_event(self.conn, trace.KIND, value)
        self.conn.commit()
        report = match_diagnostics.report_text(self.conn)
        self.assertNotIn('private-token', report)
        self.assertNotIn('True', report)

    def test_previous_rejection_is_not_reused_for_a_new_failure_without_protocol_event(self):
        self.select()
        protocol.reject(self.conn, 'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO', {},
                        protocol.MatchError('unknown_match_coach'), protocol.SEASON)
        reply = self.gate.handle('CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO',
                                 dict(event_id='invalid', rqid=99))
        self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        last = trace.recent(self.conn)[-1]
        self.assertEqual(last['command'], 'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO')
        self.assertNotIn('error', last)

    def test_classification_failure_does_not_block_handler_or_touch_caller_transaction(self):
        for error in (sqlite3.OperationalError('trace read failed'), ValueError('bad stored JSON'),
                      TypeError('bad trace metadata'), KeyError('missing classification')):
            with self.subTest(error=type(error).__name__):
                with patch.object(trace, '_season', side_effect=error):
                    with patch.object(self.gate, '_handle', return_value=dict(result='NOERR')) as handler:
                        self.assertEqual(self.gate.handle('CMD_START_GAME', {})['result'], 'NOERR')
                        handler.assert_called_once()
                self.assertFalse(self.conn.in_transaction)
        store.set_meta(self.conn, 'pending_business', 'keep')
        with patch.object(trace, '_season', side_effect=sqlite3.OperationalError('trace failed')) as classify:
            with patch.object(self.gate, '_handle', return_value=dict(result='NOERR')) as handler:
                self.assertEqual(self.gate.handle('CMD_START_GAME', {})['result'], 'NOERR')
                handler.assert_called_once(); classify.assert_not_called()
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(store.get_meta(self.conn, 'pending_business'), 'keep')
        self.conn.rollback()
        self.assertEqual(store.get_meta(self.conn, 'pending_business'), '')

    def test_card_id_cache_counts_distinguish_same_reordered_and_disjoint(self):
        self.select()
        ids = [dict(gameplayer_id=dict(id=910001+i, serial=810001+i)) for i in range(3)]
        scenarios = [(ids, 3, 0, 0, 1), (list(reversed(ids)), 3, 0, 0, 0),
                     ([dict(gameplayer_id=dict(id=920001+i, serial=820001+i)) for i in range(3)],
                      0, 3, 3, 0)]
        for cache, shared, cache_only, reply_only, same_order in scenarios:
            with self.subTest(shared=shared, same_order=same_order):
                self.assertTrue(trace.record(self.conn, 'CMD_START_GAME',
                    dict(player_list_home=cache), dict(result='NOERR', squad_data_home=dict(player_list=ids))))
                counts = trace.recent(self.conn)[-1]['counts']
                self.assertEqual({key: counts[key] for key in (
                    'shared_card_ids', 'cache_only_card_ids', 'reply_only_card_ids', 'same_card_id_order')},
                    dict(shared_card_ids=shared, cache_only_card_ids=cache_only,
                         reply_only_card_ids=reply_only, same_card_id_order=same_order))

    def test_card_comparison_retains_no_ids_and_does_not_infer_from_malformed_cache(self):
        self.select()
        cache = [dict(gameplayer_id=dict(id=987654321, serial=987654322)),
                 dict(gameplayer_id=dict(id=987654321, serial=987654323)),
                 dict(gameplayer_id=dict(id=987654324, serial=987654325))]
        reply = [cache[0], dict(gameplayer_id=dict(id=987654326, serial=987654327))]
        self.assertTrue(trace.record(self.conn, 'CMD_START_GAME', dict(player_list_home=cache),
            dict(result='NOERR', squad_data_home=dict(player_list=reply))))
        counts = trace.recent(self.conn)[-1]['counts']
        self.assertEqual(counts['shared_card_ids'], 1)
        self.assertEqual(counts['cache_only_card_ids'], 1)
        self.assertEqual(counts['reply_only_card_ids'], 1)
        for value in range(987654321, 987654328):
            self.assertNotIn(str(value), '\n'.join(self.raw()))
            self.assertNotIn(str(value), match_diagnostics.report_text(self.conn))
        for malformed in ([None], cache + [dict(gameplayer_id=dict(id=0, serial=1))]):
            self.assertTrue(trace.record(self.conn, 'CMD_START_GAME', dict(player_list_home=malformed),
                dict(result='NOERR', squad_data_home=dict(player_list=reply))))
            self.assertNotIn('shared_card_ids', trace.recent(self.conn)[-1]['counts'])


if __name__ == '__main__':
    unittest.main()
