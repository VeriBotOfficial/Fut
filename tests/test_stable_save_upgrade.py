"""Real formal 1.1.1 SQLite + committed WAL -> current formal 1.2 startup.

Only a disposable copy of the released seed is written. No APK is executed,
live-save cleanup is forbidden, and existing instance identity is respected.
"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'gateway/work/tools'))
HOST_DEPS = REPO.parents[1] / 'analysis/v120-investigation/draw-skills/host-deps'
if HOST_DEPS.is_dir():
    sys.path.insert(0, str(HOST_DEPS))
import club_store as store
import player_identity
import player_migration
import player_release
import player_skills
import release_save

SOURCE = REPO.parents[1] / 'release/v1.1.1/efootxi-1.1.1.apk'
MEMBER = 'assets/gateway-release/outputs/efootball/local-club.sqlite3'


def dump_digest(conn):
    result = hashlib.sha256()
    for line in conn.iterdump():
        result.update(line.encode('utf8')); result.update(b'\n')
    return result.hexdigest()


class StableSaveUpgradeTest(unittest.TestCase):
    def setUp(self):
        if not SOURCE.is_file():
            self.skipTest('Released formal 1.1.1 APK unavailable')
        self.temp = tempfile.TemporaryDirectory(prefix='efootxi-formal-save-upgrade-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'local-club.sqlite3'
        with zipfile.ZipFile(SOURCE) as archive:
            raw = archive.read(MEMBER)
        self.seed_sha256 = hashlib.sha256(raw).hexdigest()
        self.path.write_bytes(raw)
        self.conn = store.connect(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.conn.execute('PRAGMA wal_autocheckpoint=0')

    def test_formal_111_live_progress_backup_migration_and_second_boot(self):
        conn = self.conn
        self.assertTrue(player_identity.enabled(conn))  # actual 1.1.1 state
        aliases = list(conn.execute('SELECT * FROM owned_identity_aliases ORDER BY id,old_serial'))
        self.assertTrue(aliases)
        pid, old_serial, serial = aliases[0]
        trained = copy.deepcopy(store.player_by_key(conn, pid, serial))
        self.assertIsNotNone(trained)
        self.assertEqual(len(trained['parameters']), 147)
        trained['parameters'][20] = min(127, trained['parameters'][20] + 1)
        trained['parameters'][80] = 1
        trained.update(games=37, goal=51, assist=19, additional_skill_id=[0, 12, 69, 74, 73])
        self.assertEqual(player_skills.learned_skill_ids(trained), [0, 12, 69, 74, 73])
        conn.execute('UPDATE gameplayers SET json=? WHERE id=? AND serial=?',
                     (json.dumps(trained), pid, serial))
        conn.execute('INSERT INTO warehouse(id,serial) VALUES(?,?)', (pid, serial))

        # A genuine already-released extra acquired copy has its own tombstone,
        # receipt and high-water mark; the original trained copy remains owned.
        acquired = copy.deepcopy(trained)
        released_serial = player_identity.allocate(conn)
        acquired['gameplayer_id'] = dict(id=pid, serial=released_serial)
        acquired['is_lock'] = 'NO'
        conn.execute('INSERT INTO gameplayers VALUES(?,?,?,?,?)',
                     (pid, released_serial, json.dumps(acquired), 'user-draw', 'before-upgrade'))
        conn.commit()
        player_release.release(conn, dict(user_id='formal-upgrade-fixture', rqid='already-released',
            gameplayer_id_list=[dict(id=pid, serial=released_serial)]))

        # Historical transport receipts intentionally retain their old alias
        # payload/fingerprint; normal gateway replay canonicalizes on reading.
        alias_response = json.dumps(dict(gameplayer_id=dict(id=pid, serial=old_serial),
                                         additional_skill_id=trained['additional_skill_id']))
        conn.execute('INSERT INTO level_training_receipts VALUES(?,?,?,?)',
                     ('formal-upgrade-fixture', 'trained', 'original-training-fingerprint', alias_response))
        conn.execute('INSERT INTO player_operation_receipts VALUES(?,?,?,?,?)',
                     ('skill', 'formal-upgrade-fixture', 'learned', 'original-skill-fingerprint', alias_response))
        conn.execute('INSERT INTO managed_draw_receipts VALUES(?,?,?,?)',
                     ('formal-upgrade-fixture', 'acquired', 'original-draw-fingerprint', json.dumps([acquired])))
        pool = conn.execute('SELECT agent_id,card_id FROM managed_members LIMIT 1').fetchone()
        self.assertIsNotNone(pool)
        conn.execute('INSERT OR IGNORE INTO managed_consumed VALUES(?,?)', pool)
        agent_id, agent_raw = conn.execute('SELECT agent_id,json FROM agents LIMIT 1').fetchone()
        agent = json.loads(agent_raw); agent.update(use_count=7, acquired_list=[pid])
        conn.execute('UPDATE agents SET json=? WHERE agent_id=?', (json.dumps(agent), agent_id))
        store.set_meta(conn, 'team_name', '正式版存档球队')
        store.set_meta(conn, 'user_name', '正式版存档玩家')
        store.set_meta(conn, 'wal_upgrade_fixture_marker', 'committed-WAL-must-survive')
        sid = store.main_squad_id(conn)
        squad = next(value for value in store.squads(conn) if value['squad_id'] == sid)
        squad['name'] = '我的正式版阵容'
        squad['base_team'] = [{'base_team_id': 108}]
        conn.execute('UPDATE squads SET json=? WHERE squad_id=?', (json.dumps(squad), sid))
        store.set_meta(conn, 'base_team_id', '108')
        store.set_uniforms(conn, store.default_uniforms(108))
        conn.commit()

        original_players = {(pid, serial): json.loads(raw) for pid, serial, raw in
                            conn.execute('SELECT id,serial,json FROM gameplayers')}
        self.assertEqual(len(original_players), 23)
        preserved_tables = ('owned_identity_aliases', 'warehouse', 'managed_draw_receipts',
            'level_training_receipts', 'player_operation_receipts', 'efootxi_release_receipts',
            'efootxi_released_players', 'managed_consumed', 'managed_serials')
        snapshots = {table: list(conn.execute('SELECT * FROM ' + table)) for table in preserved_tables}
        original_journal = list(conn.execute('SELECT * FROM careerplan ORDER BY id,serial,category'))
        original_squad = list(conn.execute('SELECT * FROM squads'))
        original_gp = store.get_meta(conn, 'gp')
        before = dump_digest(conn)
        self.assertGreater(Path(str(self.path) + '-wal').stat().st_size, 0)
        main_only = sqlite3.connect(self.path.resolve().as_uri() + '?immutable=1', uri=True)
        try:
            self.assertEqual(store.get_meta(main_only, 'wal_upgrade_fixture_marker'), '')
        finally:
            main_only.close()

        with patch.object(release_save, 'clean_seed', side_effect=AssertionError('Live startup must not clean seed')):
            first = release_save.migrate(conn)
        self.assertEqual(first['players']['gifted'], 6328)
        self.assertTrue(first['identities']['already_migrated'])
        self.assertEqual(store.player_count(conn), len(original_players) + 6328)
        self.assertEqual(conn.execute('SELECT count(*) FROM efootxi_reserve_gifts').fetchone()[0], 6328)
        self.assertGreater(conn.execute('SELECT min(serial) FROM efootxi_reserve_gifts').fetchone()[0], released_serial)
        self.assertEqual(conn.execute('SELECT count(*) FROM warehouse w JOIN efootxi_reserve_gifts g '
            'ON w.id=cast(g.card_id as integer) AND w.serial=g.serial').fetchone()[0], 0)
        backup = Path(first['backup'])
        saved = sqlite3.connect(backup.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            self.assertEqual(dump_digest(saved), before)
            self.assertEqual(store.get_meta(saved, 'wal_upgrade_fixture_marker'), 'committed-WAL-must-survive')
        finally:
            saved.close()

        for identity, original in original_players.items():
            actual = store.player_by_key(conn, *identity)
            self.assertIsNotNone(actual, identity)
            for field in set(original) - {'parameters', 'play_style'}:
                self.assertEqual(actual[field], original[field], (identity, field))
            for index in set(range(147)) - {43, 61, 62}:
                self.assertEqual(actual['parameters'][index], original['parameters'][index], (identity, index))
        for table, rows in snapshots.items():
            self.assertEqual(list(conn.execute('SELECT * FROM ' + table)), rows, table)
        self.assertEqual(list(conn.execute('SELECT * FROM squads')), original_squad)
        for row in original_journal:
            self.assertIn(row, conn.execute('SELECT * FROM careerplan WHERE id=? AND serial=?', row[:2]).fetchall())
        self.assertEqual(store.get_meta(conn, 'team_name'), '正式版存档球队')
        self.assertEqual(store.get_meta(conn, 'user_name'), '正式版存档玩家')
        self.assertEqual(store.get_meta(conn, 'gp'), original_gp)
        self.assertEqual(store.stored_uniforms(conn), store.default_uniforms(108))
        current_agent = json.loads(conn.execute('SELECT json FROM agents WHERE agent_id=?', (agent_id,)).fetchone()[0])
        self.assertEqual((current_agent['use_count'], current_agent['acquired_list']), (7, [pid]))
        self.assertIsNone(store.player_by_key(conn, pid, released_serial))
        self.assertTrue(player_release.is_released(conn, pid, released_serial))
        remapped = player_identity.canonical_request(conn, json.loads(alias_response))
        self.assertEqual(remapped['gameplayer_id'], dict(id=pid, serial=serial))

        after = dump_digest(conn)
        backup_hash = hashlib.sha256(backup.read_bytes()).hexdigest()
        conn.close(); self.conn = store.connect(self.path); conn = self.conn
        with patch.object(release_save, 'clean_seed', side_effect=AssertionError('Live startup must not clean seed')):
            second = release_save.migrate(conn)
        self.assertTrue(second['players']['already_applied'])
        self.assertEqual(second['players']['gifted'], 0)
        self.assertIsNone(second['backup'])
        self.assertEqual(dump_digest(conn), after)
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), backup_hash)
        self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        report = REPO / 'build/stable-save-upgrade-verification.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(dict(status='passed', source=str(SOURCE), seed_sha256=self.seed_sha256,
            owned_before=23, gifts_added=6328, owned_after=6351, alias_count=len(aliases),
            wal_backup_included=True, existing_training_skills_stats_preserved=True,
            release_and_acquisition_receipts_preserved=True, pool_state_preserved=True,
            second_boot_database_unchanged=True), indent=2) + '\n', encoding='utf8')


if __name__ == '__main__':
    unittest.main()
