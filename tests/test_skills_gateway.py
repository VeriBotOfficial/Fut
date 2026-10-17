"""Gateway integration with a temporary copy of the actual shipped seed.

This verifies command/serialization behavior. It does not simulate the native
engine's halftime UI or prove a particular phone's reported symptom is fixed.
"""
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
import player_skills as skills
import ziru_data

FIXTURE = REPO / 'build/gateway-fixture'


class SkillsGatewayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = FIXTURE / 'outputs/efootball/local-club.sqlite3'
        if not cls.seed.is_file():
            raise unittest.SkipTest('unpack the shipped gateway fixture before integration tests')
        cls.corpus = {}
        for source in sorted((FIXTURE / 'outputs/reference/decrypted').glob('*.txt')):
            value = msgpack.unpackb(source.read_bytes(), raw=False, strict_map_key=False)
            if isinstance(value, dict) and value.get('msgid'):
                cls.corpus[value['msgid']] = value

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'club.sqlite3'
        shutil.copy2(self.seed, self.path)
        self.patches = [mock.patch.object(gate_logic, 'TEMPLATES', FIXTURE / 'work/templates'),
                        mock.patch.object(gate_logic, 'LIVE_DATA_MANIFEST_FILE', FIXTURE / 'work/templates/CMD_LOGIN_LIVEDATA.json'),
                        mock.patch.object(ziru_data, 'DATA', FIXTURE / 'outputs/reference/ziru-data')]
        for patch in self.patches:
            patch.start()
        self.conn = sqlite3.connect(self.path)
        p = json.loads(self.conn.execute('select json from gameplayers order by serial limit 1').fetchone()[0])
        p['additional_skill_id'] = []
        p.pop('additional_skill', None)
        self.pid, self.serial = store.player_key(p)
        self.sid1, self.sid2 = [sid for sid in skills.selectable_skill_ids(p)
                                if sid in skills.RANDOM_SPECIAL_SKILL_IDS][:2]
        self.conn.execute('update gameplayers set json=? where id=? and serial=?',
                          (json.dumps(p), self.pid, self.serial))
        self.copy_serial = store.player_identity.allocate(self.conn)
        second = copy.deepcopy(p)
        second['gameplayer_id']['serial'] = self.copy_serial
        store.add_player(self.conn, second, 'test-same-card-copy')
        self.conn.commit()
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)

    def tearDown(self):
        self.conn.close()
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)

    def identity(self, serial=None):
        return {'id': self.pid, 'serial': self.serial if serial is None else serial}

    def teach(self, sid, serial=None):
        gid = self.identity(serial)
        req = {'msgid': 'CMD_USE_MYCLUB_ITEM', 'user_id': 0, 'rqid': f'select:{gid}:{sid}',
               'gameplayer_id_list': [gid], 'training_index': 0, 'ability_id': sid,
               'item_list': [{'item': {'item_type': 'SKILL_TRAINING_SELECT', 'grade': 0, 'effect': sid},
                              'present_num': 1}]}
        result = self.logic.handle(req['msgid'], req)
        self.assertEqual(result['result'], 'NOERR', result)

    def find(self, rows, serial=None):
        ident = self.identity(serial)
        return next(p for p in rows if p['gameplayer_id'] == ident)

    def test_native_training_reply_login_detail_snapshot_and_match_agree_after_restart(self):
        self.teach(self.sid1)
        self.restart()
        login = self.logic.handle('CMD_LOGIN', {'rqid': 10})
        owned = self.find(login['entry_info']['gameplayer_list'])
        self.assertEqual(owned['additional_skill_id'], [self.sid1])
        detail = self.logic.handle('CMD_GET_MYCLUB_PLAYER_DATA',
                                  {'rqid': 11, 'gameplayer_id': self.identity()})
        current = self.find(detail['gameplayer_list'])
        self.assertEqual(current['additional_skill_id'], [self.sid1])
        self.assertEqual(current['additional_skill'], [self.sid1])
        self.assertEqual(current['is_skill_added'], 'YES')
        snapshot = self.logic.handle('CMD_GET_ENTRY_INFO', {'rqid': 12})
        current = self.find(snapshot['entry_info']['gameplayer_list'])
        self.assertEqual(current['additional_skill'], [self.sid1])
        self.assertEqual(current['is_skill_added'], 'YES')
        self.assertNotIn(self.sid1, detail['skillcard_id_list'])
        self.assertNotIn(74, detail['skillcard_id_list'])
        projected = self.logic._home_entries([{'gameplayer_id': self.identity()}])[0]
        self.assertEqual(projected['parameters'][skills.PARAMETER_BY_SKILL[self.sid1]], 1)

    def test_reordered_first_team_and_bench_keep_same_card_instances_skills(self):
        self.teach(self.sid1)
        self.teach(self.sid2, self.copy_serial)
        refs = [{'gameplayer_id': self.identity()}, {'gameplayer_id': self.identity(self.copy_serial)}]
        before = self.logic._home_entries(refs)
        reverse = self.logic._home_entries(list(reversed(refs)))
        self.assertEqual(before[0], reverse[1])
        self.assertEqual(before[1], reverse[0])
        for entry, expected, absent in [(before[0], self.sid1, self.sid2), (before[1], self.sid2, self.sid1)]:
            self.assertEqual(entry['parameters'][skills.PARAMETER_BY_SKILL[expected]], 1)
            self.assertEqual(entry['parameters'][skills.PARAMETER_BY_SKILL[absent]], 0)
        # Exercise the actual known squad-save command. This is not a claim
        # that the native client necessarily sends it at halftime.
        squad = store.squads(self.conn)[0]
        request = {'rqid': 25, 'squad_id': squad['squad_id'],
                   'squad_data': {'player_list': list(reversed(refs))}}
        reply = self.logic.handle('CMD_SET_MYCLUB_SQUAD_INFO', request)
        self.assertEqual(reply['result'], 'NOERR')
        self.restart()
        after = self.logic._home_entries(list(reversed(refs)))
        self.assertEqual(after, reverse)
        self.assertEqual(skills.learned_skill_ids(store.player_by_key(self.conn, self.pid, self.serial)), [self.sid1])
        self.assertEqual(skills.learned_skill_ids(store.player_by_key(self.conn, self.pid, self.copy_serial)), [self.sid2])

    def test_missing_and_duplicate_instances_do_not_fall_back_to_another_copy(self):
        with self.assertRaises(ValueError):
            self.logic._home_entries([{'gameplayer_id': self.identity(2147483000)}])
        with self.assertRaises(ValueError):
            self.logic._home_entries([{'gameplayer_id': self.identity()}, {'gameplayer_id': self.identity()}])

    def test_mode_command_echoes_correct_command_and_login_remembers_manual(self):
        mid = 'CMD_SET_MYCLUB_CAREERPLAN_ALLOCATE_TYPE'
        reply = self.logic.handle(mid, {'msgid': mid, 'rqid': 71, 'careerplan_allocate_type': 'MANUAL'})
        self.assertEqual(reply['msgid'], mid)
        self.assertEqual(reply['result'], 'NOERR')
        self.restart()
        login = self.logic.handle('CMD_LOGIN', {'rqid': 72})
        snapshot = self.logic.handle('CMD_GET_ENTRY_INFO', {'rqid': 73})
        self.assertEqual(login['entry_info']['careerplan_allocate_type'], 'MANUAL')
        self.assertEqual(snapshot['entry_info']['careerplan_allocate_type'], 'MANUAL')

    def test_match_projection_does_not_persist_skills_as_innate(self):
        self.teach(self.sid1)
        before = self.conn.execute('select json from gameplayers where id=? and serial=?',
                                   (self.pid, self.serial)).fetchone()[0]
        for _ in range(3):
            self.logic._home_entries([{'gameplayer_id': self.identity()}])
        after = self.conn.execute('select json from gameplayers where id=? and serial=?',
                                  (self.pid, self.serial)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(json.loads(after)['parameters'][skills.PARAMETER_BY_SKILL[self.sid1]], 0)


if __name__ == '__main__':
    unittest.main()
