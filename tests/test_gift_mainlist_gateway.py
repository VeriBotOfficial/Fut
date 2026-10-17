"""Exercise the real account snapshots after upgrading the reserve-gift build."""
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

import test_skills_gateway as gateway_tests
import club_store as store
import gate_logic
import player_identity
import player_migration
import ziru_data

FIXTURE = gateway_tests.FIXTURE


class GiftMainListGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_tests.SkillsGatewayTest.setUpClass.__func__(cls)

    def test_full_gift_roster_survives_native_account_refresh_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'club.sqlite3'
            shutil.copy2(self.seed, path)
            with mock.patch.object(gate_logic, 'TEMPLATES', FIXTURE / 'work/templates'), \
                 mock.patch.object(gate_logic, 'LIVE_DATA_MANIFEST_FILE', FIXTURE / 'work/templates/CMD_LOGIN_LIVEDATA.json'), \
                 mock.patch.object(ziru_data, 'DATA', FIXTURE / 'outputs/reference/ziru-data'):
                conn = sqlite3.connect(path)
                try:
                    player_identity.migrate(conn)
                    self.assertEqual(player_migration.migrate(conn)['gifted'], 6328)
                    conn.execute('insert into warehouse select cast(card_id as integer),serial from efootxi_reserve_gifts')
                    conn.execute('delete from meta where key=?', (player_migration.PLACEMENT_MARKER,))
                    conn.commit()
                    self.assertEqual((store.active_count(conn), store.warehouse_count(conn)), (23, 6328))
                    self.assertEqual(player_migration.migrate(conn)['moved_to_main_list'], 6328)
                    expected = {(pid, serial) for pid, serial in conn.execute('select id,serial from gameplayers')}
                    self.assertEqual(len(expected), 6351)
                    logic = gate_logic.GateLogic(self.corpus, conn=conn)
                    def keys(players):
                        return {store.player_key(p) for p in players}
                    for mid in ('CMD_LOGIN', 'CMD_GET_ENTRY_INFO', 'CMD_GET_MYCLUB_ENTRY_INFO', 'CMD_SET_MYCLUB_ENTRY_INFO'):
                        reply = logic.handle(mid, {'rqid': 'main-list:' + mid})
                        self.assertEqual(reply['result'], 'NOERR', mid)
                        self.assertEqual(keys(reply['entry_info']['gameplayer_list']), expected, mid)
                        self.assertEqual(reply['entry_info']['warehouse_player_num'], 0, mid)
                    reply = logic.handle('CMD_GET_MYCLUB_PROCURABLE_GAMEPLAYERLIST', {'rqid': 'players'})
                    self.assertEqual(keys([row['gameplayer'] for row in reply['gameplayer_list']]), expected)
                    saved = conn.execute('select id,serial,json from gameplayers order by id,serial').fetchall()
                    conn.close()
                    conn = sqlite3.connect(path)
                    self.assertTrue(player_migration.migrate(conn)['already_applied'])
                    logic = gate_logic.GateLogic(self.corpus, conn=conn)
                    self.assertEqual(keys(logic.handle('CMD_LOGIN', {'rqid': 'after-restart'})['entry_info']['gameplayer_list']), expected)
                    self.assertEqual(conn.execute('select id,serial,json from gameplayers order by id,serial').fetchall(), saved)
                finally:
                    conn.close()


if __name__ == '__main__':
    unittest.main()
