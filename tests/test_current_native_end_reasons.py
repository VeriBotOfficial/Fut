"""Current native terminal shape, response enums and old-receipt repair.

Static producer evidence: 775915c.c sends abnormalend_reason and root
intentional_give_up, not request gameend_reason. Native response evidence is
retained under build/native-settlement-audit; these are host regressions,
not a claim of having captured or replayed the failing phone's request.
"""
import copy
import json
import unittest

import test_native_stats_settlement as native_tests
import club_store as store
import match_protocol as protocol


class CurrentNativeEndReasonsTest(unittest.TestCase):
    # Reuse only setup/helpers, without rediscovering the entire source suite.
    setUp = native_tests.NativeStatsSettlementTest.setUp
    seed_season = native_tests.NativeStatsSettlementTest.seed_season
    request = native_tests.NativeStatsSettlementTest.request
    restart = native_tests.NativeStatsSettlementTest.restart
    start = native_tests.NativeStatsSettlementTest.start
    finish = native_tests.NativeStatsSettlementTest.finish
    counters = native_tests.NativeStatsSettlementTest.counters
    expected = native_tests.NativeStatsSettlementTest.expected
    progress = native_tests.NativeStatsSettlementTest.progress
    stats_state = native_tests.NativeStatsSettlementTest.stats_state

    def native_request(self, gid):
        req = native_tests.NativeStatsSettlementTest.native_request(self, gid)
        req['match_result'].pop('gameend_reason')
        req['intentional_give_up'] = 'NO'
        return req

    def legacy_receipt(self, mode, gid, reasons=('NORMAL', 'NORMAL')):
        key = protocol.receipt_key(mode, gid)
        response = json.loads(store.get_meta(self.conn, key))
        result = response['game_end_result']['match_result']
        result['gameend_reason'], result['abnormalend_reason'] = reasons
        store.set_meta(self.conn, key, json.dumps(response))
        self.conn.commit()
        return response

    def assert_normal(self, response):
        self.assertEqual(response['result'], 'NOERR')
        result = response['game_end_result']['match_result']
        self.assertEqual((result['gameend_reason'], result['abnormalend_reason']),
                         ('NORMAL_END', 'NOERR'))

    def test_native_ai_terminal_shape_keeps_win_draw_and_loss_normal(self):
        for home, away, expected in ((3, 1, 'WIN'), (0, 0, 'DRAW'), (0, 2, 'LOSE')):
            with self.subTest(result=expected):
                gid = self.start(protocol.AI)
                req = self.native_request(gid)
                req['score'] = [{'score_1st': home, 'score_2nd': 0},
                                {'score_1st': away, 'score_2nd': 0}]
                if not home:
                    req.pop('goal_player_list')
                    req.pop('assist_player_list')
                before = self.counters()
                response = self.finish(req)
                self.assert_normal(response)
                self.assertEqual(response['game_end_result']['match_result']['game_res'], expected)
                self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'applied')
                self.assertEqual(self.counters()[12, 1]['games'], before[12, 1]['games'] + 1)
                self.assertEqual(self.counters()[12, 1]['goal'], before[12, 1]['goal'] + (2 if home else 0))
                self.assertEqual(self.counters()[30, 4]['assist'], before[30, 4]['assist'] + (2 if home else 0))
                saved = self.counters()
                self.restart()
                self.assertEqual(self.counters(), saved)

    def test_old_normal_receipt_is_repaired_once_without_recounting(self):
        for mode in native_tests.MODES:
            with self.subTest(mode=mode):
                gid = self.start(mode)
                req = self.native_request(gid)
                self.assert_normal(self.finish(req))
                legacy = self.legacy_receipt(mode, gid)
                before, progress = self.counters(), self.progress()
                self.restart()
                req['rqid'] = 80
                response = self.finish(req)
                self.assert_normal(response)
                expected = copy.deepcopy(legacy)
                expected['game_end_result']['match_result'].update(
                    gameend_reason='NORMAL_END', abnormalend_reason='NOERR')
                expected['rqid'] = 80
                self.assertEqual(response, expected)
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.progress(), progress)
                stored = json.loads(store.get_meta(self.conn, protocol.receipt_key(mode, gid)))
                self.assert_normal(stored)
                req['rqid'] = 81
                self.assert_normal(self.finish(req))
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.progress(), progress)
                events = self.conn.execute("SELECT count(*) FROM events WHERE kind='efootxi_match_response_repaired'").fetchone()[0]
                self.assertEqual(events, native_tests.MODES.index(mode) + 1)

    def test_repair_of_pending_receipt_does_not_invent_stats_and_complete_retry_counts_once(self):
        gid = self.start(protocol.AI)
        complete = self.native_request(gid)
        incomplete = copy.deepcopy(complete)
        del incomplete['match_result']['myteam_player_list']
        before = self.counters()
        self.assert_normal(self.finish(incomplete))
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'pending')
        self.legacy_receipt(protocol.AI, gid)
        progress = self.progress()
        self.restart()
        self.assert_normal(self.finish(incomplete))
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'pending')
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.progress(), progress)
        self.assert_normal(self.finish(complete))
        self.assertEqual(self.counters(), self.expected(before))
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'applied')
        self.assertEqual(self.progress(), progress)
        self.assert_normal(self.finish(complete))
        self.assertEqual(self.counters(), self.expected(before))

    def test_explicit_forfeit_is_still_loss_and_excludes_partial_stats(self):
        gid = self.start(protocol.AI)
        req = self.native_request(gid)
        req['intentional_give_up'] = 'YES'
        req['match_result']['abnormalend_reason'] = 'NORMAL_GIVEUP'
        before = self.counters()
        response = self.finish(req)
        result = response['game_end_result']['match_result']
        self.assertEqual(result['game_res'], 'LOSE')
        self.assertEqual((result['gameend_reason'], result['abnormalend_reason']),
                         ('NORMAL_GIVEUP', 'NORMAL_GIVEUP'))
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'excluded_give_up')
        self.restart()
        self.assertEqual(self.finish(req)['game_end_result'], response['game_end_result'])
        self.assertEqual(self.counters(), before)

    def test_missing_fingerprint_or_unknown_old_reason_is_not_rewritten(self):
        for missing_metadata, pair in ((True, ('NORMAL', 'NORMAL')),
                                       (False, ('NORMAL_END', 'DISCONN_HB'))):
            with self.subTest(missing_metadata=missing_metadata):
                gid = self.start(protocol.AI)
                req = self.native_request(gid)
                self.finish(req)
                legacy = self.legacy_receipt(protocol.AI, gid, pair)
                if missing_metadata:
                    self.conn.execute('DELETE FROM meta WHERE key=?',
                                      (protocol._receipt_meta_key(protocol.AI, gid),))
                    self.conn.commit()
                before, progress = self.counters(), self.progress()
                response = self.finish(req)
                self.assertEqual(response, legacy)
                self.assertEqual(self.counters(), before)
                self.assertEqual(self.progress(), progress)

    def test_conflicting_retry_never_repairs_or_changes_old_match(self):
        gid = self.start(protocol.AI)
        req = self.native_request(gid)
        self.finish(req)
        legacy = self.legacy_receipt(protocol.AI, gid)
        before, progress = self.counters(), self.progress()
        req['score'][0]['score_1st'] += 1
        self.assertEqual(self.finish(req)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(json.loads(store.get_meta(self.conn, protocol.receipt_key(protocol.AI, gid))), legacy)
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.progress(), progress)


if __name__ == '__main__':
    unittest.main()
