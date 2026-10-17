"""Pack atomicity and immediate account snapshots using the real catalogue.

There is no bundled BUY/RECEIVE_PRESENT capture. Tests do not claim to emulate
the native result dialog; they validate the existing handler response surface
and the known GET_ENTRY_INFO / GET_MYCLUB_PLAYER_DATA commands.
"""
import json
import unittest
from unittest import mock

import test_skills_gateway as gateway_tests
import club_pack
import club_store as store
import player_operations
import player_release
import player_skills


class ClubPackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_tests.SkillsGatewayTest.setUpClass.__func__(cls)

    def setUp(self):
        gateway_tests.SkillsGatewayTest.setUp(self)
        self.pack_patch = mock.patch.object(club_pack, 'PACKS_CONFIG_FILE',
                                            gateway_tests.FIXTURE / 'outputs/reference/top10_club_packs.json')
        self.pack_patch.start()
        club_pack._CACHED_PACKS = None
        club_pack.enable(self.conn)
        self.pack = club_pack.load_packs_config()[0]
        self.request = {'msgid': 'CMD_BUY_MYCLUB_PACK', 'user_id': 0, 'rqid': 'pack-first',
                        'pack_id': self.pack['pack_id'], 'buy_type': 'BUY_BY_GP'}

    def tearDown(self):
        self.pack_patch.stop()
        club_pack._CACHED_PACKS = None
        gateway_tests.SkillsGatewayTest.tearDown(self)

    restart = gateway_tests.SkillsGatewayTest.restart

    def buy(self, **changes):
        request = dict(self.request, **changes)
        return self.logic.handle(request['msgid'], request)

    def cards(self, reply):
        return [present['player_info']['gameplayer'] for present in reply.get('added_present_items_list', [])
                if present['present_type'] == 'PLAYER']

    def test_all_cards_immediately_visible_in_existing_snapshot_and_detail_commands(self):
        before = store.player_count(self.conn)
        reply = self.buy()
        self.assertEqual(reply['result'], 'NOERR')
        expected = sum(x['present_type'] == 'PLAYER' for x in self.pack['present_info'])
        cards = self.cards(reply)
        self.assertEqual(len(cards), expected)
        self.assertEqual(store.player_count(self.conn), before + expected)
        self.assertEqual(len({x['present_id'] for x in reply['added_present_items_list']}), len(reply['added_present_items_list']))
        self.assertEqual(len({p['gameplayer_id']['serial'] for p in cards}), expected)
        snapshot = self.logic.handle('CMD_GET_ENTRY_INFO', {'rqid': 'immediate-snapshot'})
        active = {store.player_key(p) for p in snapshot['entry_info']['gameplayer_list']}
        for card in cards:
            key = store.player_key(card)
            self.assertIn(key, active)
            gid = card['gameplayer_id']
            detail = self.logic.handle('CMD_GET_MYCLUB_PLAYER_DATA', {'rqid': 'detail-' + str(key), 'gameplayer_id': gid})
            self.assertEqual(detail['gameplayer_list'][0]['gameplayer_id'], gid)
            self.assertGreater(player_operations.progression_view(self.conn, gid)['spent'], 0)
            self.assertEqual(player_operations.progression_view(self.conn, gid)['budget'],
                             2 * (card['level_info']['level'] - 1))

    def test_restart_retry_keeps_money_count_and_current_skill_snapshot(self):
        card = self.cards(self.buy())[0]
        gid = card['gameplayer_id']
        sid = player_skills.selectable_skill_ids(card)[0]
        request = {'rqid': 'train-pack-card', 'gameplayer_id_list': [gid], 'ability_id': sid, 'training_index': 0,
                   'item_list': [{'item': {'item_type': 'SKILL_TRAINING_SELECT', 'effect': sid}, 'present_num': 1}]}
        player_operations.transact(self.conn, 'skill', request, lambda: player_operations.skill(self.conn, request))
        count, gp = store.player_count(self.conn), store.get_meta(self.conn, 'gp')
        self.restart()
        reply = self.buy()
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual(store.player_count(self.conn), count)
        self.assertEqual(store.get_meta(self.conn, 'gp'), gp)
        retried = next(p for p in self.cards(reply) if p['gameplayer_id'] == gid)
        self.assertEqual(retried['additional_skill_id'], [sid])

    def test_release_does_not_allow_old_purchase_to_redisplay_or_regrant_card(self):
        card = self.cards(self.buy())[0]
        gid = card['gameplayer_id']
        player_release.release(self.conn, {'rqid': 'release-pack', 'gameplayer_id_list': [gid]})
        count = store.player_count(self.conn)
        self.restart()
        reply = self.buy()
        self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        self.assertFalse(reply.get('added_present_items_list'))
        self.assertEqual(store.player_count(self.conn), count)
        self.assertIsNone(store.player_by_key(self.conn, gid['id'], gid['serial']))
        self.assertIsNotNone(self.conn.execute('select 1 from pack_purchase_receipts where rqid=?', ('pack-first',)).fetchone())

    def test_same_receipt_cannot_switch_pack_or_payment_and_fresh_purchase_has_new_instances(self):
        first = self.cards(self.buy())
        count, gp = store.player_count(self.conn), store.get_meta(self.conn, 'gp')
        other = club_pack.load_packs_config()[1]['pack_id']
        self.assertEqual(self.buy(pack_id=other)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(self.buy(buy_type='BUY_BY_COIN')['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(store.player_count(self.conn), count)
        self.assertEqual(store.get_meta(self.conn, 'gp'), gp)
        second = self.cards(self.buy(rqid='pack-second'))
        self.assertTrue({store.player_key(p) for p in first}.isdisjoint({store.player_key(p) for p in second}))

    def test_mid_purchase_failure_rolls_back_money_instances_journal_and_serial_counter(self):
        count, gp = store.player_count(self.conn), store.get_meta(self.conn, 'gp')
        journal = self.conn.execute('select count(*) from careerplan').fetchone()[0]
        allocator = store.get_meta(self.conn, 'owned_identity_next_serial')
        original = store.add_player
        calls = 0
        def fail_second(conn, player, source):
            nonlocal calls
            calls += 1
            return original(conn, player, source) if calls == 1 else False
        with mock.patch.object(store, 'add_player', side_effect=fail_second):
            self.assertEqual(self.buy()['result'], 'ERR_INVALID_PARAM')
        self.restart()
        self.assertEqual(store.player_count(self.conn), count)
        self.assertEqual(store.get_meta(self.conn, 'gp'), gp)
        self.assertEqual(self.conn.execute('select count(*) from careerplan').fetchone()[0], journal)
        self.assertEqual(store.get_meta(self.conn, 'owned_identity_next_serial'), allocator)
        self.assertIsNone(self.conn.execute('select 1 from pack_purchase_receipts where rqid=?', ('pack-first',)).fetchone())

    def test_disabled_and_invalid_payment_never_grant(self):
        count = store.player_count(self.conn)
        self.assertEqual(self.buy(buy_type='UNKNOWN')['result'], 'ERR_INVALID_PARAM')
        club_pack.disable(self.conn)
        self.assertEqual(self.buy()['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(store.player_count(self.conn), count)


if __name__ == '__main__':
    unittest.main()
