"""Upgrade the original reserve gifts without recreating or retraining cards."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway/work/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_player_migration import card, database, policy
import club_store as store
import player_identity
import player_migration as migration
import player_release


def legacy_placement(conn):
    """Reproduce test.1/test.2 after the policy was already applied."""
    conn.execute('insert or ignore into warehouse(id,serial) '
                 'select g.id,g.serial from efootxi_reserve_gifts r join gameplayers g '
                 'on g.id=cast(r.card_id as integer) and g.serial=r.serial')
    conn.execute('delete from meta where key=?', (migration.PLACEMENT_MARKER,))
    conn.commit()


def table_rows(conn, table):
    return conn.execute('select * from "' + table + '" order by rowid').fetchall()


class GiftPlacementTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()
        migration.migrate(self.conn, policy())
        legacy_placement(self.conn)

    def tearDown(self):
        self.conn.close()

    def gift(self, pid=101):
        serial = self.conn.execute('select serial from efootxi_reserve_gifts where card_id=?',
                                   (str(pid),)).fetchone()[0]
        return {'id': pid, 'serial': serial}

    def test_same_policy_moves_exact_gifts_without_touching_saved_progress(self):
        gift = self.gift()
        row = store.player_by_key(self.conn, gift['id'], gift['serial'])
        row['parameters'][14] += 3
        row['additional_skill_id'] = [10, 20]
        row['additional_position'] = [1]
        row.update(games=12, goal=7, assist=9, is_lock='YES')
        self.conn.execute('update gameplayers set json=? where id=? and serial=?',
                          (json.dumps(row), gift['id'], gift['serial']))
        self.conn.execute('update careerplan set points=points+1 where id=? and serial=?',
                          (gift['id'], gift['serial']))
        # The user's ordinary copy of the same card must stay in reserve.
        self.conn.execute('insert into warehouse values(101,100)')
        self.conn.execute('create table saved_match_receipts(game_id text primary key,payload text)')
        self.conn.execute("insert into saved_match_receipts values('played','keep statistics receipt')")
        self.conn.commit()
        names = ['gameplayers', 'careerplan', 'efootxi_reserve_gifts', 'managed_cards',
                 'owned_identity_aliases', 'saved_match_receipts']
        before = {name: table_rows(self.conn, name) for name in names}
        result = migration.migrate(self.conn, policy())
        self.assertEqual(result['gifted'], 0)
        self.assertEqual(result['moved_to_main_list'], 2)
        self.assertFalse(result['already_applied'])
        self.assertTrue(migration.placement_applied(self.conn))
        self.assertEqual(table_rows(self.conn, 'warehouse'), [(101, 100)])
        self.assertEqual({name: table_rows(self.conn, name) for name in names}, before)
        actual = {tuple(p['gameplayer_id'].values()) for p in store.entry_roster(self.conn)}
        expected = {(int(pid), serial) for pid, serial in self.conn.execute(
            'select card_id,serial from efootxi_reserve_gifts')}
        self.assertEqual(actual, expected)
        after = list(self.conn.iterdump())
        self.assertTrue(migration.migrate(self.conn, policy())['already_applied'])
        self.assertEqual(list(self.conn.iterdump()), after)

    def test_released_gift_and_receipts_are_preserved_without_resurrection(self):
        gift = self.gift()
        player_release.release(self.conn, {'rqid': 'old-test-release', 'gameplayer_id': gift})
        protected = ['gameplayers', 'careerplan', 'efootxi_reserve_gifts',
                     'efootxi_release_receipts', 'efootxi_released_players']
        before = {name: table_rows(self.conn, name) for name in protected}
        result = migration.migrate(self.conn, policy())
        self.assertEqual((result['gifted'], result['moved_to_main_list']), (0, 1))
        self.assertEqual({name: table_rows(self.conn, name) for name in protected}, before)
        self.assertIsNone(store.player_by_key(self.conn, gift['id'], gift['serial']))
        self.assertTrue(player_release.is_released(self.conn, gift['id'], gift['serial']))
        updated = policy()
        updated['policy'] = 'later-policy'
        self.assertEqual(migration.migrate(self.conn, updated)['gifted'], 0)
        self.assertIsNone(store.player_by_key(self.conn, gift['id'], gift['serial']))

    def test_tombstoned_stale_row_is_not_promoted(self):
        gift = self.gift()
        self.conn.execute('create table efootxi_released_players('
                          'id integer,serial integer,released_at integer,primary key(id,serial))')
        self.conn.execute('insert into efootxi_released_players values(?,?,1)',
                          (gift['id'], gift['serial']))
        self.conn.commit()
        saved = table_rows(self.conn, 'gameplayers')
        result = migration.migrate(self.conn, policy())
        self.assertEqual(result['moved_to_main_list'], 1)
        self.assertTrue(store.in_warehouse(self.conn, gift['id'], gift['serial']))
        self.assertEqual(table_rows(self.conn, 'gameplayers'), saved)

    def test_manual_reserve_after_upgrade_stays_there_on_restart_and_policy_update(self):
        migration.migrate(self.conn, policy())
        gift = self.gift()
        self.conn.execute('insert into warehouse values(?,?)', (gift['id'], gift['serial']))
        self.conn.execute('insert into warehouse values(101,100)')
        self.conn.commit()
        self.assertTrue(migration.migrate(self.conn, policy())['already_applied'])
        updated = policy()
        updated['policy'] = 'new-definition-set'
        updated['cards']['105'] = ['Epic', 5, 1, 2, 1, 14, 0]
        self.conn.execute('insert into managed_cards values(?,?,?,?)',
                          ('105', json.dumps(card(105)), 'new', 'Epic'))
        self.conn.commit()
        before = table_rows(self.conn, 'warehouse')
        result = migration.migrate(self.conn, updated)
        self.assertEqual((result['gifted'], result['moved_to_main_list']), (1, 0))
        self.assertEqual(table_rows(self.conn, 'warehouse'), before)
        new_gift = self.gift(105)
        self.assertFalse(store.in_warehouse(self.conn, new_gift['id'], new_gift['serial']))

    def test_non_gift_copy_with_same_card_id_is_not_moved(self):
        self.conn.execute('insert into warehouse values(101,100)')
        self.conn.commit()
        original = store.player_by_key(self.conn, 101, 100)
        migration.migrate(self.conn, policy())
        self.assertTrue(store.in_warehouse(self.conn, 101, 100))
        self.assertEqual(store.player_by_key(self.conn, 101, 100), original)

    def test_receipt_with_different_acquisition_source_does_not_move_user_copy(self):
        gift = self.gift()
        self.conn.execute("update gameplayers set source='manual-import' where id=? and serial=?",
                          (gift['id'], gift['serial']))
        self.conn.commit()
        result = migration.migrate(self.conn, policy())
        self.assertEqual(result['moved_to_main_list'], 1)
        self.assertTrue(store.in_warehouse(self.conn, gift['id'], gift['serial']))

    def test_failed_move_rolls_back_positions_and_marker_then_can_retry(self):
        self.conn.execute("create trigger block_move before delete on warehouse when OLD.id=103 "
                          "begin select raise(ABORT,'simulated placement failure'); end")
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'placement failure'):
            migration.migrate(self.conn, policy())
        self.assertFalse(self.conn.in_transaction)
        self.assertFalse(migration.placement_applied(self.conn))
        self.assertEqual(list(self.conn.iterdump()), before)
        self.conn.execute('drop trigger block_move')
        result = migration.migrate(self.conn, policy())
        self.assertEqual((result['gifted'], result['moved_to_main_list']), (0, 2))

    def test_placement_survives_wal_close_reopen_without_regranting(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'club.sqlite3'
            conn = sqlite3.connect(path)
            try:
                self.conn.backup(conn)
                conn.execute('pragma journal_mode=wal')
                self.assertEqual(migration.migrate(conn, policy())['moved_to_main_list'], 2)
            finally:
                conn.close()
            conn = sqlite3.connect(path)
            try:
                snapshot = list(conn.iterdump())
                self.assertTrue(migration.migrate(conn, policy())['already_applied'])
                self.assertEqual(list(conn.iterdump()), snapshot)
                self.assertEqual((store.active_count(conn), store.warehouse_count(conn)), (3, 0))
            finally:
                conn.close()


class ActualGiftPlacementTests(unittest.TestCase):
    def test_full_actual_card_set_moves_without_changing_player_payloads(self):
        source = ROOT / 'build/gateway-fixture/outputs/efootball/local-club.sqlite3'
        if not source.is_file():
            self.skipTest('optional actual APK gateway fixture is unavailable')
        conn = sqlite3.connect(':memory:')
        original = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            original.backup(conn)
        finally:
            original.close()
        try:
            player_identity.migrate(conn)
            self.assertEqual(migration.migrate(conn)['gifted'], 6328)
            self.assertEqual((store.active_count(conn), store.warehouse_count(conn)), (6351, 0))
            legacy_placement(conn)
            protected = ['gameplayers', 'careerplan', 'efootxi_reserve_gifts', 'squads']
            def hashes():
                return {name: hashlib.sha256(json.dumps(table_rows(conn, name), ensure_ascii=False)
                                             .encode('utf-8')).hexdigest() for name in protected}
            before = hashes()
            result = migration.migrate(conn)
            self.assertEqual((result['gifted'], result['moved_to_main_list']), (0, 6328))
            self.assertEqual((len(store.entry_roster(conn)), store.warehouse_count(conn)), (6351, 0))
            self.assertEqual(hashes(), before)
            self.assertEqual(conn.execute('pragma integrity_check').fetchone()[0], 'ok')
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
