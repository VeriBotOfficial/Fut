import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
import player_identity
import player_migration as migration
import player_operations
import player_parameters
import recommended_allocation


def policy():
    return {'schema': 1, 'policy': 'test-v1', 'columns': list(player_parameters.POLICY_COLUMNS),
            'cards': {'101': ['Epic', 5, 1, 2, 1, 14, 0],
                      '102': ['Standard', 30, 0, 1, 1, 1, 0],
                      '103': ['Trending', 1, 1, 2, 1, 4, 0]}}


def card(pid=101, cap=5):
    params = [0] * 147
    params[0] = 12
    for i in migration.ABILITY_INDICES:
        params[i] = 60
    params[33], params[45], params[144] = 2, 1, 7
    return {'gameplayer_id': {'id': pid, 'serial': 0}, 'parameters': params,
            'play_style': [14, 0], 'initial_booster_id_list': [7, 0, 0],
            'level_info': {'level': 1, 'level_limit': cap, 'customize_point': 0,
                           'exp_total': 0, 'exp_current_level': 0, 'exp_to_next_level': 188},
            'additional_skill_id': [], 'additional_position': [], 'type': 'NORMAL'}


def database():
    conn = sqlite3.connect(':memory:')
    conn.executescript('''
        create table meta(key text primary key,value text);
        insert into meta values('owned_identity_version','1');
        create table owned_identity_aliases(id integer,old_serial integer,new_serial integer,primary key(id,old_serial));
        insert into owned_identity_aliases values(999,101,100);
        create table gameplayers(id integer,serial integer unique,json text,source text,acquired_at text,primary key(id,serial));
        create table warehouse(id integer,serial integer,primary key(id,serial));
        create table careerplan(id integer,serial integer,category text,points integer,at text);
        create table managed_cards(card_id text primary key,payload text,fingerprint text,card_type text);
        create table level_training_receipts(user_id text,rqid text,request_hash text,response_json text);
        create table events(id integer primary key,at text,kind text,detail text);
    ''')
    for pid, kind in ((101, 'Epic'), (102, 'Standard'), (103, 'Trending'), (104, 'UNKNOWN')):
        conn.execute('insert into managed_cards values(?,?,?,?)', (str(pid), json.dumps(card(pid)), 'original', kind))
    old = card()
    old['gameplayer_id']['serial'] = 100
    old['parameters'][20] += 2
    old['parameters'][80] = 1
    old['parameters'][145] = 8
    old['_careerplan_base_parameters'] = card()['parameters']
    old['level_info'].update(level=3, customize_point=2, exp_total=463)
    old['additional_skill_id'] = [10]
    old['games'] = 20
    conn.execute('insert into gameplayers values(?,?,?,?,?)', (101, 100, json.dumps(old), 'user-draw', 'old'))
    conn.execute('insert into careerplan values(?,?,?,?,?)', (101, 100, 'SHOOT', 2, 'old'))
    conn.commit()
    return conn


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database()

    def tearDown(self):
        self.conn.close()

    def saved(self, pid, serial):
        return json.loads(self.conn.execute('select json from gameplayers where id=? and serial=?', (pid, serial)).fetchone()[0])

    def test_gifts_specials_once_preserving_existing_training(self):
        old = self.saved(101, 100)
        journal = self.conn.execute('select * from careerplan where id=101 and serial=100').fetchall()
        result = migration.migrate(self.conn, policy())
        self.assertEqual(result['gifted'], 2)
        self.assertEqual(self.conn.execute('select count(*) from warehouse').fetchone()[0], 0)
        self.assertTrue(migration.placement_applied(self.conn))
        self.assertEqual(self.conn.execute('select count(*) from gameplayers where id=102').fetchone()[0], 0)
        after = self.saved(101, 100)
        for key in old:
            if key not in ('parameters', 'play_style'):
                self.assertEqual(after[key], old[key], key)
        unchanged = set(range(147)) - {43, 61, 62}
        self.assertEqual([after['parameters'][i] for i in unchanged],
                         [old['parameters'][i] for i in unchanged])
        self.assertEqual(after['parameters'][80], 1)
        self.assertEqual(after['parameters'][145], 8)
        self.assertEqual(self.conn.execute('select * from careerplan where id=101 and serial=100').fetchall(), journal)
        self.assertEqual(self.conn.execute('select serial from efootxi_reserve_gifts order by serial').fetchall(), [(102,), (103,)])
        snapshot = list(self.conn.iterdump())
        self.assertTrue(migration.migrate(self.conn, policy())['already_applied'])
        self.assertEqual(list(self.conn.iterdump()), snapshot)

    def test_gift_point_journal_base_and_experience_are_consistent(self):
        migration.migrate(self.conn, policy())
        for pid, serial in self.conn.execute('select card_id,serial from efootxi_reserve_gifts'):
            player = self.saved(int(pid), serial)
            info = player['level_info']
            self.assertEqual(info['level'], info['level_limit'])
            self.assertEqual((info['exp_current_level'], info['exp_to_next_level']), (0, 0))
            self.assertEqual(info['exp_total'], sum(player['_level_training_curve'][2:]))
            spent = self.conn.execute('select coalesce(sum(points),0) from careerplan where id=? and serial=?', (int(pid), serial)).fetchone()[0]
            self.assertEqual(spent + info['customize_point'], 2 * (info['level_limit'] - 1))
            for index in migration.ABILITY_INDICES:
                self.assertGreaterEqual(player['parameters'][index], player['_careerplan_base_parameters'][index])
            if pid == '103':
                self.assertEqual(info['level'], 1)
                self.assertEqual(spent, 0)

    def test_gift_can_be_reset_using_existing_manual_training_path(self):
        migration.migrate(self.conn, policy())
        serial = self.conn.execute("select serial from efootxi_reserve_gifts where card_id='101'").fetchone()[0]
        player = self.saved(101, serial)
        request = {'gameplayer_use_list': [{'gameplayer_id': player['gameplayer_id'],
                                           'use_list': [], 'parameters': list(player['parameters'])}]}
        with self.conn:
            player_operations.career(self.conn, request, lambda players: [])
        after = self.saved(101, serial)
        self.assertEqual(after['level_info']['customize_point'], 8)
        self.assertEqual(after['parameters'], player['_careerplan_base_parameters'])
        self.assertEqual(self.conn.execute('select count(*) from careerplan where id=101 and serial=?', (serial,)).fetchone()[0], 0)

    def test_moved_or_released_gifts_are_not_recreated_on_new_policy(self):
        migration.migrate(self.conn, policy())
        serial = self.conn.execute("select serial from efootxi_reserve_gifts where card_id='101'").fetchone()[0]
        self.conn.execute('delete from warehouse where id=101 and serial=?', (serial,))
        self.conn.execute('delete from gameplayers where id=101 and serial=?', (serial,))
        self.conn.commit()
        updated = policy()
        updated['policy'] = 'v2'
        self.assertEqual(migration.migrate(self.conn, updated)['gifted'], 0)
        self.assertIsNone(self.conn.execute('select 1 from gameplayers where id=101 and serial=?', (serial,)).fetchone())

    def test_new_card_revision_grants_only_new_card_id(self):
        migration.migrate(self.conn, policy())
        self.conn.execute('insert into managed_cards values(?,?,?,?)', ('105', json.dumps(card(105)), 'new', 'Epic'))
        self.conn.commit()
        updated = policy()
        updated['cards']['105'] = ['Epic', 5, 1, 2, 1, 14, 0]
        self.assertEqual(migration.migrate(self.conn, updated)['gifted'], 1)
        self.assertEqual(self.conn.execute('select count(*) from efootxi_reserve_gifts').fetchone()[0], 3)

    def test_failure_rolls_back_repairs_grants_and_receipts(self):
        self.conn.execute("create trigger fail_gift before insert on gameplayers when NEW.id=103 begin select raise(ABORT,'simulated failure'); end")
        before = list(self.conn.iterdump())
        with self.assertRaises(sqlite3.IntegrityError):
            migration.migrate(self.conn, policy())
        self.assertEqual(list(self.conn.iterdump()), before)
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute('drop trigger fail_gift')
        self.assertEqual(migration.migrate(self.conn, policy())['gifted'], 2)

    def test_missing_or_invalid_definition_fails_before_partial_success(self):
        document = policy()
        document['cards']['999'] = ['Epic', 5, 1, 2, 1, 14, 0]
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, 'missing special'):
            migration.migrate(self.conn, document)
        self.assertEqual(list(self.conn.iterdump()), before)
        broken = card()
        broken['parameters'][45] = 4
        self.conn.execute('update managed_cards set payload=? where card_id=?', (json.dumps(broken), '101'))
        self.conn.commit()
        before = list(self.conn.iterdump())
        with self.assertRaisesRegex(ValueError, 'weak-foot'):
            migration.migrate(self.conn, policy())
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_active_transaction_is_not_committed_by_migration(self):
        self.conn.execute("insert into meta values('unsaved','1')")
        with self.assertRaisesRegex(ValueError, 'finish transaction'):
            migration.migrate(self.conn, policy())
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_bulk_serials_skip_aliases_and_detect_exhaustion(self):
        with self.assertRaises(ValueError):
            player_identity.allocate_many(self.conn, 2)
        self.conn.execute('BEGIN IMMEDIATE')
        self.assertEqual(player_identity.allocate_many(self.conn, 3), [102, 103, 104])
        self.conn.rollback()
        self.conn.execute('update gameplayers set serial=2147483646')
        with self.assertRaisesRegex(ValueError, 'exhausted'):
            player_identity.allocate_many(self.conn, 1)
        self.conn.rollback()

    def test_recommendation_respects_budget_and_immutable_parameters(self):
        for position in range(13):
            for budget in (0, 1, 4, 20, 66, 120):
                with self.subTest(position=position, budget=budget):
                    original = card()['parameters']
                    original[0] = position
                    result, spent = recommended_allocation.apply_to_parameters(original, position, budget)
                    self.assertLessEqual(sum(spent.values()), budget)
                    self.assertTrue(all(type(v) is int and 0 <= v <= 127 for v in result))
                    self.assertTrue(all(result[i] == original[i] for i in range(147) if i not in migration.ABILITY_INDICES))


if __name__ == '__main__':
    unittest.main()
