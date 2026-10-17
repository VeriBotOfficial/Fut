import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_player_migration import card, database, policy
import player_identity
import player_migration
import player_release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()
        original = json.loads(self.conn.execute('select json from gameplayers where serial=100').fetchone()[0])
        original.update(sell_gp=50, is_lock='NO')
        self.conn.execute('update gameplayers set json=? where serial=100', (json.dumps(original),))
        extra = copy.deepcopy(original)
        extra['gameplayer_id']['serial'] = 200
        self.conn.execute('insert into gameplayers values(?,?,?,?,?)', (101, 200, json.dumps(extra), 'other-copy', 'old'))
        self.conn.execute('insert into warehouse values(101,100)')
        self.conn.execute('insert into warehouse values(101,200)')
        self.conn.execute("insert into meta values('gp','100')")
        self.conn.execute('create table rentals(id integer,serial integer)')
        self.conn.execute('insert into rentals values(101,100)')
        self.conn.execute('create table squads(squad_id integer primary key,json text)')
        squad = {'squad_data': {'player_list': [
            {'gameplayer_id': {'id': 101, 'serial': 100}, 'uniform_number': 10},
            {'gameplayer_id': {'id': 101, 'serial': 200}, 'uniform_number': 20}],
            'coach': {'coach_id': {'id': 101, 'serial': 100}}, 'squad_gameplan': {'keep': True}}}
        self.conn.execute('insert into squads values(1,?)', (json.dumps(squad),))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def req(self, serial=100, rqid=1):
        return {'rqid': rqid, 'user_id': 1, 'gameplayer_id_list': [{'id': 101, 'serial': serial}]}

    def test_exact_instance_release_preserves_other_copy_and_clears_dependents(self):
        response = player_release.release(self.conn, self.req())
        self.assertEqual(response['result'], 'NOERR')
        self.assertEqual(response['gp'], 150)
        self.assertEqual(response['warehouse_num'], 1)
        self.assertIsNone(self.conn.execute('select 1 from gameplayers where id=101 and serial=100').fetchone())
        self.assertIsNotNone(self.conn.execute('select 1 from gameplayers where id=101 and serial=200').fetchone())
        for table in ('warehouse', 'careerplan', 'rentals'):
            self.assertEqual(self.conn.execute('select count(*) from '+table+' where id=101 and serial=100').fetchone()[0], 0)
        self.assertTrue(player_release.is_released(self.conn, 101, 100))
        self.assertEqual(self.conn.execute('pragma integrity_check').fetchone()[0], 'ok')

    def test_squad_slots_are_cleared_without_touching_other_copy_or_coach(self):
        player_release.release(self.conn, self.req())
        plan = json.loads(self.conn.execute('select json from squads').fetchone()[0])['squad_data']
        self.assertEqual(len(plan['player_list']), 2)
        self.assertEqual(plan['player_list'][0]['gameplayer_id'], {'id': 0, 'serial': 0})
        self.assertEqual(plan['player_list'][1]['gameplayer_id'], {'id': 101, 'serial': 200})
        self.assertEqual(plan['coach']['coach_id'], {'id': 101, 'serial': 100})
        self.assertTrue(plan['squad_gameplan']['keep'])

    def test_retries_and_new_request_for_same_released_copy_do_not_award_twice(self):
        player_release.release(self.conn, self.req())
        self.assertEqual(player_release.release(self.conn, self.req())['gp'], 150)
        self.assertEqual(player_release.release(self.conn, self.req(rqid=2))['gp'], 150)
        with self.assertRaisesRegex(player_release.ReleaseError, 'reused'):
            player_release.release(self.conn, self.req(serial=200))
        self.assertIsNotNone(self.conn.execute('select 1 from gameplayers where serial=200').fetchone())

    def test_reward_is_from_stored_player_not_claimed_request(self):
        req = dict(self.req(), gp=99999999, sell_gp=999999999)
        self.assertEqual(player_release.release(self.conn, req)['gp'], 150)

    def test_unknown_identity_or_locked_card_rolls_back_whole_batch(self):
        req = self.req()
        req['gameplayer_id_list'].append({'id': 999, 'serial': 999})
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(player_release.ReleaseError, 'not owned'):
            player_release.release(self.conn, req)
        self.assertEqual(list(self.conn.iterdump()), before)
        extra = json.loads(self.conn.execute('select json from gameplayers where serial=200').fetchone()[0])
        extra['is_lock'] = 'YES'
        self.conn.execute('update gameplayers set json=? where serial=200', (json.dumps(extra),))
        self.conn.commit()
        before = list(self.conn.iterdump())
        req['gameplayer_id_list'][1] = {'id': 101, 'serial': 200}
        with self.assertRaisesRegex(player_release.ReleaseError, 'locked'):
            player_release.release(self.conn, req)
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_partial_sql_failure_rolls_back_deletes_currency_and_tombstones(self):
        self.conn.execute("create trigger block_delete before delete on gameplayers when OLD.serial=200 begin select raise(ABORT,'disk failure simulation'); end")
        before = list(self.conn.iterdump())
        req = self.req()
        req['gameplayer_id_list'].append({'id': 101, 'serial': 200})
        with self.assertRaises(sqlite3.IntegrityError):
            player_release.release(self.conn, req)
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)

    def test_serial_is_never_reused_after_releasing_latest_copy(self):
        player_release.release(self.conn, self.req(serial=200))
        with self.conn:
            self.assertEqual(player_identity.allocate(self.conn), 201)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(player_identity.allocate_many(self.conn, 2), [202, 203])
        self.conn.commit()

    def test_historical_identity_alias_is_resolved(self):
        self.conn.execute('insert into owned_identity_aliases values(101,999,100)')
        self.conn.commit()
        player_release.release(self.conn, self.req(serial=999))
        self.assertTrue(player_release.is_released(self.conn, 101, 100))
        self.assertIsNotNone(self.conn.execute('select 1 from owned_identity_aliases where old_serial=999').fetchone())

    def test_released_gift_is_not_regranted_after_policy_revision(self):
        player_migration.migrate(self.conn, policy())
        serial = self.conn.execute("select serial from efootxi_reserve_gifts where card_id='101'").fetchone()[0]
        player_release.release(self.conn, self.req(serial=serial))
        revised = policy()
        revised['policy'] = 'v2'
        self.assertEqual(player_migration.migrate(self.conn, revised)['gifted'], 0)
        self.assertIsNotNone(self.conn.execute("select 1 from efootxi_reserve_gifts where card_id='101'").fetchone())
        self.assertIsNone(self.conn.execute('select 1 from gameplayers where serial=?', (serial,)).fetchone())

    def test_committed_release_survives_close_and_reopen_with_wal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'club.sqlite3'
            disk = sqlite3.connect(path)
            self.conn.backup(disk)
            disk.execute('pragma journal_mode=wal')
            player_release.release(disk, self.req())
            disk.close()
            disk = sqlite3.connect(path)
            try:
                self.assertIsNone(disk.execute('select 1 from gameplayers where serial=100').fetchone())
                self.assertTrue(player_release.is_released(disk, 101, 100))
                self.assertEqual(disk.execute("select value from meta where key='gp'").fetchone()[0], '150')
            finally:
                disk.close()

    def test_missing_ambiguous_or_invalid_identity_is_rejected(self):
        requests = [{}, {'gameplayer_id_list': [101]},
                    {'gameplayer_id_list': [{'id': 101, 'serial': 0}]},
                    {'gameplayer_id_list': [{'id': 101, 'serial': True}]},
                    {'gameplayer_id_list': [{'id': 101, 'serial': 100}] * 2},
                    dict(self.req(), gameplayer_id={'id': 101, 'serial': 100})]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(player_release.ReleaseError):
                player_release.release(self.conn, request)

    def test_foreign_transaction_is_not_committed(self):
        self.conn.execute("insert into meta values('unsaved','1')")
        with self.assertRaisesRegex(player_release.ReleaseError, 'finish transaction'):
            player_release.release(self.conn, self.req())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()


if __name__ == '__main__':
    unittest.main()
