"""Regression for the actual test.3 classifier defect, not invented wins.

The shipped native constructor defaults intentional_give_up to YES. The
user-supplied 73 reference explicitly documents normal NOERR + YES. These
producer-shaped tests exercise the full gateway and durable SQLite state.
"""
import copy
import json
import unittest

import test_native_stats_settlement as native_tests
import club_store as store
import match_diagnostics
import match_protocol as protocol
import match_stats


class TerminalReasonPriorityTest(unittest.TestCase):
    setUp = native_tests.NativeStatsSettlementTest.setUp
    seed_season = native_tests.NativeStatsSettlementTest.seed_season
    request = native_tests.NativeStatsSettlementTest.request
    restart = native_tests.NativeStatsSettlementTest.restart
    start = native_tests.NativeStatsSettlementTest.start
    finish = native_tests.NativeStatsSettlementTest.finish
    counters = native_tests.NativeStatsSettlementTest.counters
    progress = native_tests.NativeStatsSettlementTest.progress
    stats_state = native_tests.NativeStatsSettlementTest.stats_state

    def terminal(self, gid, home=11, away=0, side='HOME'):
        req = native_tests.NativeStatsSettlementTest.native_request(self, gid, side)
        req['match_result'].pop('gameend_reason')  # not emitted by native 775915c
        req['intentional_give_up'] = 'YES'
        req['score'] = [{'score_1st': home, 'score_2nd': 0},
                        {'score_1st': away, 'score_2nd': 0}]
        own_score = home if side == 'HOME' else away
        req['goal_player_list'] = [dict(self.refs[0]['gameplayer_id']) for _ in range(own_score)]
        req['assist_player_list'] = [dict(self.refs[3]['gameplayer_id'])
                                     for _ in range(max(0, own_score-1))]
        return req

    def test_noerr_yes_eleven_zero_records_win_and_native_total_refresh_after_restart(self):
        gid = self.start(protocol.AI)
        before = self.counters()
        req = self.terminal(gid)
        out = self.finish(req)
        result = out['game_end_result']['match_result']
        self.assertEqual(result['game_res'], 'WIN')
        self.assertEqual(result['score_data'], {'home_score': 11, 'away_score': 0})
        self.assertEqual((result['gameend_reason'], result['abnormalend_reason']),
                         ('NORMAL_END', 'NOERR'))
        self.assertEqual(self.counters()[12, 1]['goal'], before[12, 1]['goal'] + 11)
        self.assertEqual(self.counters()[30, 4]['assist'], before[30, 4]['assist'] + 10)
        self.assertEqual(self.counters()[20, 3]['games'], before[20, 3]['games'])
        updates = out['game_end_result']['extra_match_result']['gameplayer_update_info']
        self.assertEqual(len(updates), 4)
        for row in updates:
            key = store.player_key(row)
            for field in ('games', 'goal', 'assist'):
                self.assertEqual(row[field], [self.counters()[key][field]])
        saved, progress = self.counters(), self.progress()
        self.restart()
        req['rqid'] += 1
        again = self.finish(req)
        self.assertEqual(again['game_end_result'], out['game_end_result'])
        self.assertEqual(self.counters(), saved)
        self.assertEqual(self.progress(), progress)

    def test_noerr_yes_keeps_real_wins_draws_and_losses_on_either_side(self):
        for side in ('HOME', 'AWAY'):
            for home, away in ((2, 0), (0, 0), (0, 2)):
                with self.subTest(side=side, score=(home, away)):
                    gid = self.start(protocol.AI, side)
                    req = self.terminal(gid, home, away, side)
                    before = self.counters()
                    out = self.finish(req)
                    ours, theirs = (home, away) if side == 'HOME' else (away, home)
                    expected = 'WIN' if ours > theirs else 'DRAW' if ours == theirs else 'LOSE'
                    self.assertEqual(out['game_end_result']['match_result']['game_res'], expected)
                    self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'applied')
                    self.assertEqual(self.counters()[12, 1]['games'], before[12, 1]['games'] + 1)
                    self.assertEqual(self.counters()[12, 1]['goal'], before[12, 1]['goal'] + ours)

    def test_explicit_terminal_giveup_still_overrides_a_leading_score_and_no_flag(self):
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        req['intentional_give_up'] = 'NO'
        req['match_result']['abnormalend_reason'] = 'NORMAL_GIVEUP'
        before = self.counters()
        out = self.finish(req)
        self.assertEqual(out['game_end_result']['match_result']['game_res'], 'LOSE')
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'excluded_give_up')
        self.assertEqual(out['game_end_result']['extra_match_result']['gameplayer_update_info'], [])

    def test_yes_is_only_a_fallback_when_terminal_reason_is_absent(self):
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        req['match_result'].pop('abnormalend_reason')
        req.pop('score')
        before = self.counters()
        self.assertEqual(self.finish(req)['game_end_result']['match_result']['game_res'], 'LOSE')
        self.assertEqual(self.counters(), before)
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        req['match_result']['abnormalend_reason'] = 'CHEAT'
        self.assertEqual(self.finish(req)['result'], 'ERR_INVALID_PARAM')
        self.assertIsNotNone(protocol.active_match(self.conn, protocol.AI))
        self.assertEqual(self.counters(), before)

    def test_explicit_normal_reason_without_scores_stays_pending_then_can_complete(self):
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        incomplete = copy.deepcopy(req)
        incomplete.pop('score')
        before = self.counters()
        self.assertEqual(self.finish(incomplete)['result'], 'ERR_INVALID_PARAM')
        self.assertIsNotNone(protocol.active_match(self.conn, protocol.AI))
        self.assertEqual(self.counters(), before)
        self.assertEqual(self.finish(req)['game_end_result']['match_result']['game_res'], 'WIN')
        self.assertEqual(self.counters()[12, 1]['goal'], before[12, 1]['goal'] + 11)

    def test_historical_result_refresh_uses_current_totals_without_double_stats(self):
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        self.finish(req)
        state = self.stats_state(protocol.AI, gid)
        self.conn.execute('BEGIN IMMEDIATE')
        match_stats.apply_confirmed_deltas(self.conn, protocol.AI, gid+1, [
            {'gameplayer_id': {'id': 12, 'serial': 1}, 'games': 1, 'goal': 2, 'assist': 0}],
            state['active'], dict(state['resolved'], home=2, away=0), source='confirmed-next-match')
        self.conn.commit()
        before = self.counters()
        out = self.finish(req)
        updates = out['game_end_result']['extra_match_result']['gameplayer_update_info']
        row = next(row for row in updates if store.player_key(row) == (12, 1))
        self.assertEqual(row['goal'], [before[12, 1]['goal']])
        self.assertEqual(self.counters(), before)

    def test_feedback_report_is_read_only_and_reports_real_pending_reason_without_private_data(self):
        gid = self.start(protocol.AI)
        req = self.terminal(gid)
        req['user_name'] = 'PRIVATE_PLAYER_NAME'
        req['session_token'] = 'PRIVATE_SESSION_TOKEN'
        req['match_result']['myteam_player_list'][0]['is_join_game'] = 'unverified'
        out = self.finish(req)
        self.assertEqual(out['game_end_result']['match_result']['game_res'], 'WIN')
        self.assertEqual(self.stats_state(protocol.AI, gid)['status'], 'pending')
        self.assertEqual(out['game_end_result']['extra_match_result']['gameplayer_update_info'], [])
        before = list(self.conn.iterdump())
        text = match_diagnostics.report_text(self.conn, '1.2.0-test.4')
        self.assertIn('游戏上报结束原因：NOERR', text)
        self.assertIn('游戏上报放弃标记：YES', text)
        self.assertIn('WIN / NORMAL_END / NOERR', text)
        self.assertIn('unknown_native_participation_flag', text)
        self.assertNotIn('PRIVATE_PLAYER_NAME', text)
        self.assertNotIn('PRIVATE_SESSION_TOKEN', text)
        self.assertNotIn(str(gid), text)
        self.assertEqual(list(self.conn.iterdump()), before)
        for (raw,) in self.conn.execute("SELECT detail FROM events WHERE kind='efootxi_match'"):
            self.assertIsInstance(json.loads(raw), dict)


if __name__ == '__main__':
    unittest.main()
