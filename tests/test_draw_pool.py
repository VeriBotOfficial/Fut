"""Finite-pool reset integration against a temporary copy of the shipped DB."""
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
import msgpack
import club_store as store
import gate_logic
import managed_card_pool as pools
import player_release
import ziru_data

FIXTURE = REPO / 'build/gateway-fixture'


class DrawPoolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = FIXTURE / 'outputs/efootball/local-club.sqlite3'
        if not cls.seed.is_file():
            raise unittest.SkipTest('unpack shipped gateway fixture')
        cls.corpus = {}
        for path in (FIXTURE / 'outputs/reference/decrypted').glob('*.txt'):
            obj = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
            if isinstance(obj, dict) and obj.get('msgid'):
                cls.corpus[obj['msgid']] = obj

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'club.sqlite3'
        shutil.copy2(self.seed, self.path)
        self.conn = sqlite3.connect(self.path)
        self.patches = [mock.patch.object(gate_logic, 'TEMPLATES', FIXTURE / 'work/templates'),
                        mock.patch.object(ziru_data, 'DATA', FIXTURE / 'outputs/reference/ziru-data'),
                        mock.patch.object(pools, 'DISPLAY_CONFIG', FIXTURE / 'outputs/reference/PACK_DISPLAY.json')]
        for patch in self.patches:
            patch.start()
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)
        self.a, self.b = [row[0] for row in self.conn.execute(
            'select agent_id from managed_agents where enabled=1 and replacement=0 order by agent_id limit 2')]
        pools.reset_agent(self.conn, self.a)
        pools.reset_agent(self.conn, self.b)

    def tearDown(self):
        self.conn.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)

    def draw(self, aid, rqid):
        return pools.draw_and_grant(self.conn, aid, 1, request={'user_id': 0, 'rqid': rqid})[0]

    def test_reset_one_pool_keeps_owned_cards_other_pool_and_serials(self):
        first = self.draw(self.a, 'a1')
        other = self.draw(self.b, 'b1')
        owned = self.conn.execute('select id,serial,json from gameplayers order by id,serial').fetchall()
        serials = self.conn.execute('select * from managed_serials order by card_id').fetchall()
        before_b = pools.counts(self.conn, self.b)
        pools.reset_agent(self.conn, self.a, request={'rqid': 'reset-a'})
        self.restart()
        self.assertEqual(pools.counts(self.conn, self.a), pools.counts(self.conn, self.a, remaining=False))
        self.assertEqual(pools.counts(self.conn, self.b), before_b)
        self.assertEqual(self.conn.execute('select id,serial,json from gameplayers order by id,serial').fetchall(), owned)
        self.assertEqual(self.conn.execute('select * from managed_serials order by card_id').fetchall(), serials)
        self.assertEqual(self.draw(self.a, 'a1'), first)  # old draw retry cannot grant twice
        self.assertIsNotNone(store.player_by_key(self.conn, *store.player_key(other)))

    def test_retry_old_reset_after_new_draw_does_not_reset_new_draw(self):
        request = {'user_id': 0, 'rqid': 'reset-once'}
        self.draw(self.a, 'before')
        pools.reset_agent(self.conn, self.a, request=request)
        self.draw(self.a, 'after')
        remaining = pools.counts(self.conn, self.a)
        self.restart()
        pools.reset_agent(self.conn, self.a, request=request)
        self.assertEqual(pools.counts(self.conn, self.a), remaining)
        with self.assertRaises(pools.PoolError):
            pools.reset_agent(self.conn, self.b, request=request)

    def test_failed_reset_rolls_back_consumption_and_receipt(self):
        self.draw(self.a, 'draw')
        before = pools.counts(self.conn, self.a)
        with mock.patch.object(store, 'log_event', side_effect=RuntimeError('disk failure')):
            with self.assertRaises(RuntimeError):
                pools.reset_agent(self.conn, self.a, request={'rqid': 'failed'})
        self.restart()
        self.assertEqual(pools.counts(self.conn, self.a), before)
        self.assertIsNone(self.conn.execute('select 1 from managed_reset_receipts where rqid=?', ('failed',)).fetchone())

    def test_native_commands_enable_reset_and_refresh_box_and_list(self):
        self.draw(self.a, 'draw')
        self.assertEqual(self.logic.handle('CMD_GET_MYCLUB_AGENT_BOX_INFO', {'agent_id': self.a})['can_reset'], 'YES')
        reply = self.logic.handle('CMD_RESET_MYCLUB_AGENT', {'agent_id': self.a, 'rqid': 990})
        self.assertEqual(reply['result'], 'NOERR')
        self.assertEqual(reply['msgid'], 'CMD_RESET_MYCLUB_AGENT')
        box = self.logic.handle('CMD_GET_MYCLUB_AGENT_BOX_INFO', {'agent_id': self.a})
        self.assertEqual(box['remaining_player_count'], box['base_player_count'])
        listing = self.logic.handle('CMD_GET_MYCLUB_AGENTLIST', {'rqid': 991})
        agent = next(a for a in listing['agent_list'] if a['agent_id'] == self.a)
        self.assertEqual(agent['can_reset'], 'YES')
        self.assertEqual(agent['use_count'], 0)
        self.assertEqual(agent['remaining_player_count'], agent['base_player_count'])
        self.assertEqual(agent['acquired_list'], [])

    def test_replacement_disabled_unknown_and_invalid_pools_rejected(self):
        self.conn.execute('update managed_agents set replacement=1 where agent_id=?', (self.a,))
        self.conn.execute('update managed_agents set enabled=0 where agent_id=?', (self.b,))
        self.conn.commit()
        for aid in [self.a, self.b, 999999, True, 0, '7000']:
            with self.subTest(agent=aid), self.assertRaises(pools.PoolError):
                pools.reset_agent(self.conn, aid, request={'rqid': f'invalid:{aid}'})

    def test_released_draw_receipt_is_retained_but_cannot_redisplay_or_regrant(self):
        card = self.draw(self.a, 'draw-release')
        gid = card['gameplayer_id']
        player_release.release(self.conn, {'rqid': 'release', 'gameplayer_id_list': [gid]})
        self.restart()
        count = store.player_count(self.conn)
        with self.assertRaisesRegex(pools.PoolError, 'released player'):
            self.draw(self.a, 'draw-release')
        self.assertEqual(store.player_count(self.conn), count)
        self.assertIsNone(store.player_by_key(self.conn, gid['id'], gid['serial']))
        self.assertIsNotNone(self.conn.execute('select 1 from managed_draw_receipts where rqid=?', ('draw-release',)).fetchone())


if __name__ == '__main__':
    unittest.main()
