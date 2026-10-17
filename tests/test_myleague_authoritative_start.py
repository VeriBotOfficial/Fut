"""A native saved season plan is the authoritative START source.

The legacy negative controls reproduce server-side rejection, not phone or
native-engine execution. A successful reply is not proof of entry to the pitch.
"""
import copy
import json
import unittest
from unittest.mock import patch

import test_myleague_native_plan as native_tests
import club_store as store
import my_league


class MyLeagueAuthoritativeStartTest(unittest.TestCase):
    def setUp(self):
        # Reuse a full 23-player fixture, not a settlement-only gameplan mock.
        native_tests.MyLeagueNativePlanTest.setUp(self)
        store.player_identity.migrate(self.conn)
        self.refs = store.player_identity.canonical_request(self.conn, self.refs)
        self.assertEqual(self.save_special()['result'], 'NOERR')
        self.prepared = self.gate.handle('CMD_GET_VSCOM_OPPONENT', dict(
            match_category='SEASON_MODE', rqid=20, user_id=7))

    select = native_tests.MyLeagueNativePlanTest.select
    request = native_tests.MyLeagueNativePlanTest.request
    save_special = native_tests.MyLeagueNativePlanTest.save_special

    def start(self, **extra):
        request = dict(game_id=self.prepared['game_id'], match_category='SEASON_MODE',
                       player_list_home=[], player_list_away=[], side='HOME', rqid=21)
        request.update(extra)
        return self.gate.handle('CMD_START_GAME', request)

    def assert_saved_plan(self, reply):
        self.assertEqual(reply['result'], 'NOERR')
        home = reply['squad_data_home']
        expected = my_league.season_special(my_league.state(self.conn))['squad_data']
        self.assertEqual([x['gameplayer_id'] for x in home['player_list']],
                         [x['gameplayer_id'] for x in expected['player_list']])
        self.assertEqual(home['squad_gameplan'], expected['squad_gameplan'])
        self.assertEqual(home['coach']['coach_id']['id'], 82)
        self.assertEqual(home['player_list'][0]['parameters'][107], 1)
        self.assertTrue(all(len(x['parameters']) == 147 for x in home['player_list']))

    def test_test5_cached_unknown_instance_rejects_but_same_saved_plan_now_starts(self):
        cache = [dict(gameplayer_id=dict(id=999999999, serial=999999999))]
        before = my_league.state(self.conn)
        # test.5 preferred any populated request roster, sending it straight
        # into exact-owned resolution before the saved season plan was used.
        with patch.object(my_league, 'start_refs', return_value=cache):
            legacy = self.start(player_list_home=cache)
        self.assertEqual(legacy['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), before)
        self.assert_saved_plan(self.start(player_list_home=cache))
        self.assertIsNone(store.player_by_key(self.conn, 999999999, 999999999))
        self.assertEqual(my_league.state(self.conn)['played'], {})

    def test_conflicting_ordinary_selection_and_cache_cannot_mix_two_plans(self):
        # squad_id is not present in the native START serializer. An ordinary
        # plan cannot override the season's already persisted special plan.
        before = copy.deepcopy(store.squads(self.conn))
        self.assert_saved_plan(self.start(squad_id=999999, player_list_home=self.refs,
                                         side='untrusted-cache-value'))
        self.assertEqual(store.squads(self.conn), before)
        active = my_league.state(self.conn)['active']
        self.assertIsNone(active['request_side'])
        self.assertIn(active['side'], ('HOME', 'AWAY'))

    def test_corrupt_saved_identity_or_duplicate_never_falls_back_to_request_or_random_card(self):
        original = my_league.state(self.conn)
        cases = [dict(id=0, serial=1), dict(id=9000, serial=0),
                 dict(id=False, serial=0), dict(id=999999999, serial=1),
                 original['special_squad']['data']['squad_data']['player_list'][1]['gameplayer_id']]
        owned = list(self.conn.execute('SELECT id,serial,json FROM gameplayers ORDER BY id,serial'))
        for identity in cases:
            with self.subTest(identity=identity):
                damaged = copy.deepcopy(original)
                damaged['special_squad']['data']['squad_data']['player_list'][0]['gameplayer_id'] = identity
                my_league.save(self.conn, damaged); self.conn.commit()
                self.assertEqual(self.start(player_list_home=self.refs)['result'], 'ERR_INVALID_PARAM')
                self.assertEqual(my_league.state(self.conn), damaged)
                self.assertEqual(list(self.conn.execute(
                    'SELECT id,serial,json FROM gameplayers ORDER BY id,serial')), owned)

    def test_explicitly_missing_saved_starter_is_not_promoted_from_bench(self):
        damaged = my_league.state(self.conn)
        damaged['special_squad']['data']['squad_data']['player_list'][0]['gameplayer_id'] = dict(id=0, serial=0)
        my_league.save(self.conn, damaged); self.conn.commit()
        self.assertEqual(self.start(player_list_home=self.refs)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), damaged)
        detail = json.loads(self.conn.execute("SELECT detail FROM events WHERE kind='efootxi_match' "
            'ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual(detail['error'], 'missing_match_starting_player')

    def test_source_ownership_is_enforced_even_without_migrated_identity_policy(self):
        # The legacy _home_entries path substitutes cards when identity policy
        # is disabled. The My League saved source must fail before that path.
        damaged = my_league.state(self.conn)
        damaged['special_squad']['data']['squad_data']['player_list'][0]['gameplayer_id'] = dict(id=999999999, serial=1)
        my_league.save(self.conn, damaged); self.conn.commit()
        with patch.object(store.player_identity, 'enabled', return_value=False):
            with patch.object(self.gate, '_home_entries', wraps=self.gate._home_entries) as hydrate:
                self.assertEqual(self.start()['result'], 'ERR_INVALID_PARAM')
                hydrate.assert_not_called()
        self.assertEqual(my_league.state(self.conn), damaged)

    def release_saved_slot(self, index):
        source = my_league.season_special(my_league.state(self.conn))['squad_data']
        identity = copy.deepcopy(source['player_list'][index]['gameplayer_id'])
        out = self.gate.handle('CMD_SELL_MYCLUB_GAMEPLAYER', dict(
            gameplayer_id_list=[identity], rqid=99, user_id=7))
        self.assertEqual(out['result'], 'NOERR')
        self.assertIsNone(store.player_by_key(self.conn, identity['id'], identity['serial']))
        # No SET_SPECIAL resave: release scrubs ordinary plans but leaves this
        # old special source. Its durable tombstone permits an empty position.
        self.assertEqual(my_league.season_special(my_league.state(self.conn))[
            'squad_data']['player_list'][index]['gameplayer_id'], identity)
        return identity

    def test_released_optional_bench_does_not_require_resaving_season_plan(self):
        sd = my_league.season_special(my_league.state(self.conn))['squad_data']
        settings = sd['squad_gameplan']['match_plan_settings']
        required = set(settings['order'][:11]) | {settings['captain']}
        for formation in sd['squad_gameplan']['formation']:
            for link in formation.get('coach_link_data') or []:
                if link.get('is_link_on') == 'YES':
                    required.update(link.get(field, 255) for field in ('key_member_Id', 'link_member_Id'))
        index = next(i for i in range(len(sd['player_list'])) if i not in required)
        removed = self.release_saved_slot(index)
        response = self.start(player_list_home=self.refs)
        self.assertEqual(response['result'], 'NOERR')
        home = response['squad_data_home']
        self.assertEqual(len(home['player_list']), 22)
        self.assertNotIn(removed, [row['gameplayer_id'] for row in home['player_list']])
        self.assertEqual(len(set(home['squad_gameplan']['match_plan_settings']['order'][:11])), 11)
        self.assertNotIn(255, home['squad_gameplan']['match_plan_settings']['order'][:11])

    def test_released_saved_starter_still_rejects_instead_of_promoting_bench(self):
        sd = my_league.season_special(my_league.state(self.conn))['squad_data']
        index = sd['squad_gameplan']['match_plan_settings']['order'][0]
        removed = self.release_saved_slot(index)
        before = my_league.state(self.conn)
        self.assertEqual(self.start(player_list_home=self.refs)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), before)
        self.assertFalse(before['active']['started'])
        self.assertIsNone(store.player_by_key(self.conn, removed['id'], removed['serial']))

    def test_legacy_saved_aliases_display_and_start_as_the_same_exact_owned_instances(self):
        original = my_league.state(self.conn)
        legacy = copy.deepcopy(original)
        aliases = {(pid, new): old for pid, old, new in self.conn.execute(
            'SELECT id,old_serial,new_serial FROM owned_identity_aliases')}
        changed = 0
        for slot in legacy['special_squad']['data']['squad_data']['player_list']:
            identity = slot['gameplayer_id']; key = identity['id'], identity['serial']
            if key in aliases:
                identity['serial'] = aliases[key]; changed += 1
        self.assertEqual(changed, 23)
        my_league.save(self.conn, legacy); self.conn.commit()
        display = my_league.own_special(self.conn, {}, 101)
        expected = original['special_squad']['data']['squad_data']
        self.assertEqual(display['squad_data']['player_list'], expected['player_list'])
        self.assertEqual(display['squad_data']['coach'], expected['coach'])
        self.assertEqual(my_league.state(self.conn), legacy)
        reply = self.start(player_list_home=legacy['special_squad']['data']['squad_data']['player_list'])
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual([row['gameplayer_id'] for row in reply['squad_data_home']['player_list']],
                         [row['gameplayer_id'] for row in display['squad_data']['player_list']])
        self.assertEqual(reply['squad_data_home']['squad_gameplan'], expected['squad_gameplan'])
        self.assertEqual(my_league.state(self.conn)['special_squad'], legacy['special_squad'])

    def test_unknown_zero_serial_is_not_guessed_from_an_owned_card(self):
        damaged = my_league.state(self.conn)
        gid = damaged['special_squad']['data']['squad_data']['player_list'][0]['gameplayer_id']
        self.assertIsNone(self.conn.execute('SELECT 1 FROM owned_identity_aliases '
            'WHERE id=? AND old_serial=0', (gid['id'],)).fetchone())
        gid['serial'] = 0
        my_league.save(self.conn, damaged); self.conn.commit()
        self.assertEqual(self.start()['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(my_league.state(self.conn), damaged)


if __name__ == '__main__':
    unittest.main()
