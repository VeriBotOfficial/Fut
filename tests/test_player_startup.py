"""Repeat-start and interruption tests; all database writes are disposable."""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_player_migration import database, policy
import club_store as store
import player_identity
import player_migration
import player_release
import release_save


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'local-club.sqlite3'
        self.conn = store.connect(self.path)
        source = database()
        source.backup(self.conn)
        source.close()
        self.conn.executescript(store.SCHEMA)
        self.conn.execute('pragma journal_mode=wal')
        self.conn.execute('pragma wal_autocheckpoint=0')
        store.set_meta(self.conn, 'user_name', 'niuniu73')
        store.set_meta(self.conn, 'wal_saved_progress', 'keep-me')
        self.conn.execute('create table managed_draw_receipts(value text)')
        self.conn.execute("insert into managed_draw_receipts values('already consumed draw')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def restart(self):
        self.conn.close()
        self.conn = store.connect(self.path)
        return release_save.migrate(self.conn, policy())

    def test_full_startup_can_reopen_three_times_without_recreating_tables_or_cards(self):
        first = release_save.migrate(self.conn, policy())
        self.assertEqual(first['players']['gifted'], 2)
        self.assertEqual(store.get_meta(self.conn, 'user_name'), 'efootxi')
        snapshot = list(self.conn.iterdump())
        for _ in range(3):
            result = self.restart()
            self.assertTrue(result['players']['already_applied'])
            self.assertTrue(result['identities']['already_migrated'])
            self.assertFalse(self.conn.in_transaction)
            self.assertEqual(list(self.conn.iterdump()), snapshot)
            self.assertEqual(self.conn.execute('pragma integrity_check').fetchone()[0], 'ok')

    def test_backup_includes_committed_wal_and_is_not_overwritten_by_later_revision(self):
        self.assertTrue(Path(str(self.path) + '-wal').exists())
        before = list(self.conn.iterdump())
        result = release_save.migrate(self.conn, policy())
        backup = Path(result['backup'])
        saved = sqlite3.connect(backup.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            self.assertEqual(list(saved.iterdump()), before)
            self.assertEqual(store.get_meta(saved, 'wal_saved_progress'), 'keep-me')
        finally:
            saved.close()
        original_backup = backup.read_bytes()
        revised = policy()
        revised['policy'] = 'updated'
        release_save.migrate(self.conn, revised)
        self.assertEqual(backup.read_bytes(), original_backup)
        self.assertEqual(list(self.path.parent.glob('*.tmp')), [])

    def test_placement_upgrade_takes_current_save_backup_without_replacing_old_backup(self):
        first = release_save.migrate(self.conn, policy())
        old_backup = Path(first['backup'])
        old_bytes = old_backup.read_bytes()
        # Emulate an installed test.1/test.2 save with trained match progress.
        self.conn.execute('insert into warehouse(id,serial) '
                          'select cast(card_id as integer),serial from efootxi_reserve_gifts')
        self.conn.execute('delete from meta where key=?', (player_migration.PLACEMENT_MARKER,))
        pid, serial, raw = self.conn.execute('select id,serial,json from gameplayers '
                                              'where source=? limit 1', (player_migration.GIFT_SOURCE,)).fetchone()
        player = json.loads(raw)
        player.update(games=20, goal=13, assist=7, additional_skill_id=[10, 20])
        self.conn.execute('update gameplayers set json=? where id=? and serial=?',
                          (json.dumps(player), pid, serial))
        self.conn.commit()
        before = list(self.conn.iterdump())
        result = release_save.migrate(self.conn, policy())
        self.assertEqual((result['players']['gifted'], result['players']['moved_to_main_list']), (0, 2))
        backup = Path(result['backup'])
        self.assertEqual(backup.name, 'local-club.before-efootxi-main-list-v1.sqlite3')
        saved = sqlite3.connect(backup.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            self.assertEqual(list(saved.iterdump()), before)
        finally:
            saved.close()
        self.assertEqual(old_backup.read_bytes(), old_bytes)
        self.assertEqual(store.player_by_key(self.conn, pid, serial), player)
        self.assertEqual(store.warehouse_count(self.conn), 0)
        self.assertIsNone(self.restart()['backup'])

    def test_bad_placement_backup_blocks_move_and_keeps_current_save(self):
        release_save.migrate(self.conn, policy())
        self.conn.execute('insert into warehouse(id,serial) '
                          'select cast(card_id as integer),serial from efootxi_reserve_gifts')
        self.conn.execute('delete from meta where key=?', (player_migration.PLACEMENT_MARKER,))
        self.conn.commit()
        backup = self.path.with_name('local-club.before-efootxi-main-list-v1.sqlite3')
        backup.write_bytes(b'incomplete older placement backup')
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(release_save.StartupError, r'\[save backup\]'):
            release_save.migrate(self.conn, policy())
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_empty_roster_is_a_valid_save_and_released_gifts_stay_released(self):
        release_save.migrate(self.conn, policy())
        rows = [{'id': row[0], 'serial': row[1]} for row in self.conn.execute('select id,serial from gameplayers')]
        player_release.release(self.conn, {'rqid': 'release-all', 'gameplayer_id_list': rows})
        result = self.restart()
        self.assertEqual(result['players']['gifted'], 0)
        self.assertEqual(store.player_count(self.conn), 0)
        self.assertEqual(self.conn.execute('select count(*) from efootxi_reserve_gifts').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('select count(*) from efootxi_released_players').fetchone()[0], 3)
        self.assertEqual(self.conn.execute('select value from managed_draw_receipts').fetchone()[0], 'already consumed draw')

    def test_interrupted_player_phase_rolls_back_then_resumes_without_resetting_progress(self):
        self.conn.execute("create trigger fail_gift before insert on gameplayers when NEW.id=103 begin select raise(ABORT,'simulated disk write failure'); end")
        with self.assertRaisesRegex(release_save.StartupError, r'\[player migration\]'):
            release_save.migrate(self.conn, policy())
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(store.player_count(self.conn), 1)
        self.assertEqual(store.get_meta(self.conn, 'wal_saved_progress'), 'keep-me')
        self.assertEqual(store.get_meta(self.conn, 'efootxi_identity_v1'), '1')
        self.assertEqual(store.get_meta(self.conn, player_migration.MARKER), '')
        self.conn.execute('drop trigger fail_gift')
        self.assertEqual(self.restart()['players']['gifted'], 2)
        self.assertEqual(self.restart()['players']['gifted'], 0)

    def test_startup_refuses_to_commit_callers_unfinished_transaction(self):
        store.set_meta(self.conn, 'uncommitted', 'user-change')
        with self.assertRaisesRegex(release_save.StartupError, 'pending changes'):
            release_save.migrate(self.conn, policy())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(store.get_meta(self.conn, 'uncommitted'), '')

    def test_missing_policy_fails_before_any_save_mutation(self):
        empty = policy()
        empty['cards'] = {}
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(release_save.StartupError, r'\[card policy\]'):
            release_save.migrate(self.conn, empty)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(list(self.path.parent.glob('*before*')))

    def test_seed_cleanup_is_never_implicitly_allowed_on_live_start(self):
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, 'explicit staged'):
            release_save.clean_seed(self.conn)
        self.assertEqual(list(self.conn.iterdump()), before)
        release_save.migrate(self.conn, policy())
        self.assertEqual(self.conn.execute('select count(*) from managed_draw_receipts').fetchone()[0], 1)

    def test_actual_managed_agent_index_has_no_json_column(self):
        self.conn.execute('create table managed_agents(agent_id integer primary key,name text,'
                          'enabled integer,replacement integer,mode text)')
        self.conn.execute("insert into managed_agents values(1,'73efootball special',1,0,'finite')")
        self.conn.execute('insert into agents values(?,?)',
                          (1, json.dumps({'name': '73efootball', 'image': 'https://images.example/keep.png'})))
        self.conn.commit()
        release_save.migrate(self.conn, policy())
        self.assertEqual(self.conn.execute('select name,enabled,replacement,mode from managed_agents').fetchone(),
                         ('efootxi special', 1, 0, 'finite'))
        value = json.loads(self.conn.execute('select json from agents').fetchone()[0])
        self.assertEqual(value, {'name': 'efootxi', 'image': 'https://images.example/keep.png'})
        self.assertTrue(self.restart()['players']['already_applied'])

    def test_presentation_failure_rolls_back_its_partial_changes_and_can_resume(self):
        self.conn.execute("insert into agents values(1,'broken-json')")
        self.conn.commit()
        with self.assertRaisesRegex(release_save.StartupError, r'\[project presentation\]'):
            release_save.migrate(self.conn, policy())
        self.assertEqual(store.get_meta(self.conn, 'user_name'), 'niuniu73')
        self.assertEqual(store.get_meta(self.conn, 'efootxi_identity_v1'), '')
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute("update agents set json='{}' where agent_id=1")
        self.conn.commit()
        self.assertEqual(self.restart()['players']['gifted'], 2)

    def test_invalid_existing_backup_does_not_replace_or_reset_save(self):
        backup = self.path.with_name('local-club.before-efootxi-1200.sqlite3')
        backup.write_bytes(b'not sqlite')
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(release_save.StartupError, r'\[save backup\]'):
            release_save.migrate(self.conn, policy())
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertEqual(backup.read_bytes(), b'not sqlite')

    def test_competing_first_identity_startups_recheck_marker_after_writer_lock(self):
        self.conn.execute("delete from meta where key='owned_identity_version'")
        self.conn.execute('drop table owned_identity_aliases')
        self.conn.commit()
        rendezvous = threading.Barrier(2)
        actual_enabled = player_identity.enabled
        outcomes, errors = [], []

        def synchronized_enabled(conn):
            result = actual_enabled(conn)
            if not conn.in_transaction:
                rendezvous.wait(timeout=10)
            return result

        def worker():
            connection = sqlite3.connect(self.path, timeout=10)
            try:
                outcomes.append(player_identity.migrate(connection))
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        with mock.patch.object(player_identity, 'enabled', side_effect=synchronized_enabled):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(12)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(sum(bool(outcome.get('already_migrated')) for outcome in outcomes), 1)
        self.assertTrue(player_identity.enabled(self.conn))
        self.assertEqual(self.conn.execute('pragma integrity_check').fetchone()[0], 'ok')


if __name__ == '__main__':
    unittest.main()
