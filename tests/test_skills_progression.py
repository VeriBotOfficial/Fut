"""Acquisition -> development -> manual allocation across four real code paths."""
import copy
import json
import unittest
from unittest import mock

import test_skills_gateway as gateway_tests
FIXTURE = gateway_tests.FIXTURE
import club_store as store
import club_pack
import managed_card_pool
import player_migration
import player_operations as operations
import player_parameters
import player_training
import training_settings
import developed_preview


class ProgressionGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_tests.SkillsGatewayTest.setUpClass.__func__(cls)

    def setUp(self):
        gateway_tests.SkillsGatewayTest.setUp(self)
        self.pack_patch = mock.patch.object(club_pack, 'PACKS_CONFIG_FILE', FIXTURE / 'outputs/reference/top10_club_packs.json')
        self.pack_patch.start()
        club_pack._CACHED_PACKS = None
        club_pack.enable(self.conn)
        training_settings.handle(self.conn, {'careerplan_allocate_type': 'MANUAL'})
        self.card = next(json.loads(raw) for (raw,) in self.conn.execute('select payload from managed_cards')
                         if (json.loads(raw).get('level_info') or {}).get('level_limit', 1) > 1
                         and str(json.loads(raw)['gameplayer_id']['id']) in player_parameters.card_policy()['cards']
                         and player_parameters.card_policy()['cards'][str(json.loads(raw)['gameplayer_id']['id'])][0] != 'Standard')

    def tearDown(self):
        self.pack_patch.stop()
        club_pack._CACHED_PACKS = None
        gateway_tests.SkillsGatewayTest.tearDown(self)

    restart = gateway_tests.SkillsGatewayTest.restart

    def owned(self, gid):
        return store.player_by_key(self.conn, gid['id'], gid['serial'])

    def acquire(self, source):
        if source == 'draw':
            aid = self.conn.execute('select agent_id from managed_members where card_id=? limit 1',
                                    (str(self.card['gameplayer_id']['id']),)).fetchone()[0]
            managed_card_pool.reset_agent(self.conn, aid)
            chosen = next(p for p in managed_card_pool.preview(self.conn, aid)['members']
                          if p['card_id'] == str(self.card['gameplayer_id']['id']))
            rng = mock.Mock()
            rng.choices.return_value = [chosen]
            return managed_card_pool.draw_and_grant(self.conn, aid, 1, rng=rng, request={'rqid': 'progress-draw'})[0]
        if source == 'gp':
            reply = self.logic.handle('CMD_BUY_MYCLUB_SCOUT_PLAYER',
                                      {'rqid': 'progress-gp', 'target_player': {'id': self.card['gameplayer_id']['id']}})
            self.assertEqual(reply['result'], 'NOERR', reply)
            return self.owned(reply['gameplayer']['gameplayer']['gameplayer_id'])
        if source == 'pack':
            pack = club_pack.load_packs_config()[0]
            reply = self.logic.handle('CMD_BUY_MYCLUB_PACK',
                                      {'rqid': 'progress-pack', 'pack_id': pack['pack_id'], 'buy_type': 'BUY_BY_GP'})
            self.assertEqual(reply['result'], 'NOERR', reply)
            return self.owned(next(x['player_info']['gameplayer']['gameplayer_id']
                                   for x in reply['added_present_items_list'] if x['present_type'] == 'PLAYER'))
        if source == 'gift':
            serial = store.player_identity.allocate(self.conn)
            player, spent = player_migration.prepare_gift(self.card, serial, player_parameters.card_policy(), 1234)
            store.add_player(self.conn, player, player_migration.GIFT_SOURCE)
            # Same production gift journal; add_player may already have
            # reconstructed the exactly recognized full-development snapshot.
            if not store.careerplan_counts(self.conn, *store.player_key(player)):
                for group, points in spent.items():
                    store.record_careerplan(self.conn, *store.player_key(player), group, points)
            self.conn.commit()
            return self.owned(player['gameplayer_id'])
        raise AssertionError(source)

    def save_one_manual_point(self, gid, rqid):
        player = self.owned(gid)
        params = list(player.get('_careerplan_base_parameters') or player['parameters'])
        for index in operations.AUTO_GROUP_INDICES['SHOOT']:
            params[index] += 1
        req = {'rqid': rqid, 'careerplan_allocate_type': 'MANUAL', 'use_auto_allocate_point': 'NO',
               'gameplayer_use_list': [{'gameplayer_id': gid, 'parameters': params,
                                        'use_list': [{'type': 'PARAMETER', 'parameter_type': 'SHOOT', 'point': 1}]}]}
        reply = self.logic.handle('CMD_USE_MYCLUB_CAREERPLAN', req)
        self.assertEqual(reply['result'], 'NOERR', reply)

    def test_four_sources_allow_manual_at_max_level_without_auto_toggle(self):
        for source in ('draw', 'gp', 'pack', 'gift'):
            with self.subTest(source=source):
                player = self.acquire(source)
                gid = player['gameplayer_id']
                if player['level_info']['level'] < player['level_info']['level_limit']:
                    saved_before = copy.deepcopy(player)
                    preview = developed_preview.developed(player)
                    self.assertEqual(preview['level_info']['level'], player['level_info']['level_limit'])
                    self.assertEqual(self.owned(gid), saved_before)  # display-only max level cannot spend points
                    reply = self.logic.handle('CMD_USE_MYCLUB_ITEM', {
                        'rqid': 'level-' + source, 'gameplayer_id_list': [gid],
                        'item_list': [{'item': {'item_type': 'LEVEL_TRAINING', 'grade': 4, 'effect': 10000}, 'present_num': 999}]})
                    self.assertEqual(reply['result'], 'NOERR')
                player = self.owned(gid)
                self.assertEqual(player['level_info']['level'], player['level_info']['level_limit'])
                budget = 2 * (player['level_info']['level'] - 1)
                self.assertEqual(operations.progression_view(self.conn, gid)['budget'], budget)
                # Direct manual replacement works with either unspent points
                # or an existing full auto journal; no AUTO setting is sent.
                self.save_one_manual_point(gid, 'manual-' + source)
                self.assertEqual(self.owned(gid)['level_info']['customize_point'], budget - 1)
                self.assertEqual(training_settings.get(self.conn), 'MANUAL')
                state = operations.reset_progression(self.conn, gid, request_id='reset-' + source)
                self.assertEqual(state['available'], budget)
                self.assertEqual(state['spent'], 0)
                self.restart()
                self.assertEqual(operations.progression_view(self.conn, gid)['available'], budget)
                self.assertEqual(training_settings.get(self.conn), 'MANUAL')

    def test_old_pack_snapshot_missing_ledger_is_recovered_without_ability_change(self):
        player = self.acquire('pack')
        gid = player['gameplayer_id']
        self.conn.execute('delete from careerplan where id=? and serial=?', store.player_key(player))
        self.conn.commit()
        self.assertTrue(player_training.ensure_progression_ledger(self.conn, player))
        self.assertEqual(self.owned(gid), player)
        self.conn.commit()
        self.assertFalse(player_training.ensure_progression_ledger(self.conn, player))
        state = operations.reset_progression(self.conn, gid, request_id='old-pack-reset')
        self.assertEqual(state['available'], 2 * (player['level_info']['level'] - 1))

    def test_unknown_manual_build_is_not_guessed_and_reset_retry_does_not_clear_later_edits(self):
        player = self.acquire('pack')
        gid = player['gameplayer_id']
        operations.reset_progression(self.conn, gid, request_id='reset-once')
        self.save_one_manual_point(gid, 'new-manual')
        state = operations.reset_progression(self.conn, gid, request_id='reset-once')
        self.assertEqual(state['spent'], 1)
        snapshot = self.owned(gid)
        self.conn.execute('delete from careerplan where id=? and serial=?', store.player_key(snapshot))
        self.conn.commit()
        self.assertFalse(player_training.ensure_progression_ledger(self.conn, snapshot))
        self.assertEqual(self.owned(gid), snapshot)


if __name__ == '__main__':
    unittest.main()
