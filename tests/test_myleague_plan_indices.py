"""START roster compaction must preserve saved tactical player references."""
import copy
import json
import unittest

import test_myleague_start_slots as base_tests
import club_store as store
import match_protocol as protocol
import my_league
import myleague_plan


def ref(number):
    return {'gameplayer_id': {'id': 9000 + number, 'serial': 1}}


def squad():
    return {'player_list': [ref(i) for i in range(23)], 'squad_gameplan': {
        'match_plan_settings': {'order': list(range(23)) + [255] * 17,
            'captain': 0, 'set_piece_taker': [1, 2, 3, 4, 5, 6],
            'df_overlap_setplay': [10, 22, 255], 'auto_member_change': 2},
        'formation': [{'team_style': 3, 'formation_offensive': [{'role': 0}],
            'team_tactics': [{'data': [255, 0], 'value_id': [0, 0]}],
            'coach_link_data': [{'is_link_on': 'YES', 'key_member_Id': 5,
                                 'link_member_Id': 6}]}]}}


class MyLeaguePlanIndexTest(unittest.TestCase):
    def test_identity_map_preserves_every_original_field_and_input(self):
        data = squad(); before = copy.deepcopy(data)
        out = myleague_plan.gameplan_for_entries(data, data['player_list'])
        self.assertEqual(out, data['squad_gameplan'])
        self.assertEqual(data, before)
        self.assertIsNot(out, data['squad_gameplan'])

    def test_reordered_roster_retains_each_saved_player_assignment(self):
        data = squad(); entries = list(reversed(data['player_list']))
        out = myleague_plan.gameplan_for_entries(data, entries)
        settings = out['match_plan_settings']; source = data['squad_gameplan']['match_plan_settings']
        for name in ('order', 'set_piece_taker', 'df_overlap_setplay'):
            for old, new in zip(source[name], settings[name]):
                if old != 255:
                    self.assertEqual(entries[new]['gameplayer_id'], data['player_list'][old]['gameplayer_id'])
                else:
                    self.assertEqual(new, 255)
        self.assertEqual(settings['captain'], 22)
        self.assertEqual(out['formation'][0]['coach_link_data'][0], {
            'is_link_on': 'YES', 'key_member_Id': 17, 'link_member_Id': 16})
        self.assertEqual(out['formation'][0]['team_tactics'], data['squad_gameplan']['formation'][0]['team_tactics'])

    def test_sparse_unused_bench_compaction_remaps_later_references(self):
        data = squad(); data['player_list'][15] = copy.deepcopy(base_tests.EMPTY)
        entries = [slot for slot in data['player_list'] if slot != base_tests.EMPTY]
        out = myleague_plan.gameplan_for_entries(data, entries)
        order = out['match_plan_settings']['order']
        self.assertEqual(order[15], 255)
        self.assertEqual(order[22], 21)
        self.assertEqual(out['match_plan_settings']['df_overlap_setplay'], [10, 21, 255])
        self.assertTrue(all(index == 255 or 0 <= index < len(entries) for index in order))
        self.assertEqual(len(set(order[:11])), 11)

    def test_missing_starting_player_is_rejected_instead_of_promoting_bench(self):
        data = squad(); data['player_list'][2] = copy.deepcopy(base_tests.EMPTY)
        entries = [slot for slot in data['player_list'] if slot != base_tests.EMPTY]
        with self.assertRaisesRegex(protocol.MatchError, 'missing_match_starting_player'):
            myleague_plan.gameplan_for_entries(data, entries)

    def test_captured_enabled_unset_links_and_unset_captain_are_preserved(self):
        data = squad()
        data['squad_gameplan']['match_plan_settings']['captain'] = 255
        links = [{'is_link_on': 'YES', 'key_member_Id': 255, 'link_member_Id': 255}
                 for _ in range(4)]
        data['squad_gameplan']['formation'][0]['coach_link_data'] = copy.deepcopy(links)
        out = myleague_plan.gameplan_for_entries(data, list(reversed(data['player_list'])))
        self.assertEqual(out['match_plan_settings']['captain'], 255)
        self.assertEqual(out['formation'][0]['coach_link_data'], links)

    def test_explicit_unset_first_eleven_still_rejects(self):
        data = squad()
        data['squad_gameplan']['match_plan_settings']['order'][2] = 255
        with self.assertRaisesRegex(protocol.MatchError, 'missing_match_starting_player'):
            myleague_plan.gameplan_for_entries(data, data['player_list'])

    def test_active_unknown_tactics_are_preserved_for_identity_map_but_not_guessed_for_reorder(self):
        data = squad(); data['squad_gameplan']['formation'][0]['team_tactics'][0]['value_id'][0] = 4
        self.assertEqual(myleague_plan.gameplan_for_entries(data, data['player_list']), data['squad_gameplan'])
        with self.assertRaisesRegex(protocol.MatchError, 'unverified_match_tactics_reindex'):
            myleague_plan.gameplan_for_entries(data, list(reversed(data['player_list'])))

    def test_unknown_identity_duplicate_starting_order_and_bad_indexes_reject(self):
        for problem in ('unknown_identity', 'duplicate_starter', 'bad_index', 'short_roster'):
            with self.subTest(problem=problem):
                data = squad(); entries = copy.deepcopy(data['player_list'])
                if problem == 'unknown_identity': entries[-1] = ref(99)
                elif problem == 'duplicate_starter': data['squad_gameplan']['match_plan_settings']['order'][1] = 0
                elif problem == 'bad_index': data['squad_gameplan']['match_plan_settings']['captain'] = 40
                else: entries = entries[:10]
                with self.assertRaises(protocol.MatchError):
                    myleague_plan.gameplan_for_entries(data, entries)


class MyLeaguePlanGateTest(unittest.TestCase):
    def setUp(self):
        base_tests.MyLeagueStartSlotsTest.setUp(self)

    start = base_tests.MyLeagueStartSlotsTest.start

    def assert_consistent(self, out):
        self.assertEqual(out['result'], 'NOERR')
        home = out['squad_data_home']; n = len(home['player_list'])
        settings = home['squad_gameplan']['match_plan_settings']
        self.assertTrue(all(i == 255 or 0 <= i < n for i in settings['order']))
        self.assertEqual(len(set(settings['order'][:11])), 11)
        self.assertNotIn(255, settings['order'][:11])

    def test_filtering_middle_empty_request_still_maps_plan_by_populated_identity(self):
        entries = list(reversed(self.refs))
        out = self.start([base_tests.EMPTY] + entries[:15] + [base_tests.EMPTY] + entries[15:])
        self.assert_consistent(out)
        saved = store.squads(self.conn)[0]['squad_data']
        home = out['squad_data_home']
        for old, new in zip(saved['squad_gameplan']['match_plan_settings']['order'],
                            home['squad_gameplan']['match_plan_settings']['order']):
            if old != 255:
                self.assertEqual(saved['player_list'][old]['gameplayer_id'], home['player_list'][new]['gameplayer_id'])

    def test_missing_saved_starter_rejects_before_started_or_fixture_writes(self):
        ordinary = store.squads(self.conn)[0]
        sd = ordinary['squad_data']
        index = sd['squad_gameplan']['match_plan_settings']['order'][0]
        sd['player_list'][index] = copy.deepcopy(base_tests.EMPTY)
        self.conn.execute('UPDATE squads SET json=? WHERE squad_id=?', (json.dumps(ordinary), ordinary['squad_id']))
        self.conn.commit()
        before = my_league.state(self.conn)
        out = self.start(self.refs)
        self.assertEqual(out['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), before)

    def test_sparse_saved_bench_retains_remaining_identity_order_and_plan_bounds(self):
        ordinary = store.squads(self.conn)[0]
        # In the retained real plan, index15 starts. Use an unused bench index
        # for this test so no required XI assignment is silently discarded.
        used = set(ordinary['squad_data']['squad_gameplan']['match_plan_settings']['order'][:11])
        index = next(i for i in range(11, 22) if i not in used)
        ordinary['squad_data']['player_list'][index] = copy.deepcopy(base_tests.EMPTY)
        self.conn.execute('UPDATE squads SET json=? WHERE squad_id=?', (json.dumps(ordinary), ordinary['squad_id']))
        self.conn.commit()
        refs = copy.deepcopy(ordinary['squad_data']['player_list'])
        out = self.start(refs)
        self.assert_consistent(out)
        self.assertEqual([p['gameplayer_id'] for p in out['squad_data_home']['player_list']],
                         [p['gameplayer_id'] for p in refs if p != base_tests.EMPTY])


if __name__ == '__main__':
    unittest.main()
