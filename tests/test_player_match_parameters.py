"""Real-APK fixture checks of all 147 fields, not a native-engine simulation."""
import copy
import json
from pathlib import Path
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
import player_identity
import player_operations
import player_parameters
import player_skills
import release_save
import ziru_data

FIXTURE = REPO / 'build/gateway-fixture'
MESSI_19_BIG_TIME = 89136409091415


class MatchParameterPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = FIXTURE / 'outputs/efootball/local-club.sqlite3'
        if not source.is_file():
            raise unittest.SkipTest('optional actual APK gateway fixture is unavailable')
        cls.temp_seed = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp_seed.cleanup)
        cls.seed = Path(cls.temp_seed.name) / 'migrated.sqlite3'
        original = sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)
        staged = store.connect(cls.seed)
        try:
            original.backup(staged)
            result = release_save.migrate(staged)
            if result['players']['gifted'] != 6328:
                raise AssertionError('unexpected original fixture gift count')
        finally:
            staged.close()
            original.close()
        cls.corpus = {}
        for path in sorted((FIXTURE / 'outputs/reference/decrypted').glob('*.txt')):
            value = msgpack.unpackb(path.read_bytes(), raw=False, strict_map_key=False)
            if isinstance(value, dict) and value.get('msgid'):
                cls.corpus[value['msgid']] = value

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'club.sqlite3'
        source = sqlite3.connect(self.seed.resolve().as_uri() + '?mode=ro', uri=True)
        self.conn = store.connect(self.path)
        source.backup(self.conn)
        source.close()
        self.addCleanup(lambda: self.conn.close())
        for patch in (mock.patch.object(gate_logic, 'TEMPLATES', FIXTURE / 'work/templates'),
                      mock.patch.object(gate_logic, 'LIVE_DATA_MANIFEST_FILE', FIXTURE / 'work/templates/CMD_LOGIN_LIVEDATA.json'),
                      mock.patch.object(ziru_data, 'DATA', FIXTURE / 'outputs/reference/ziru-data'),
                      mock.patch.object(ziru_data, '_cache', {})):
            patch.start()
            self.addCleanup(patch.stop)
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)
        serial = self.conn.execute('select serial from efootxi_reserve_gifts where card_id=?',
                                   (str(MESSI_19_BIG_TIME),)).fetchone()[0]
        original = store.player_by_key(self.conn, MESSI_19_BIG_TIME, serial)
        self.first = dict(original['gameplayer_id'])
        self.conn.execute('delete from warehouse where id=? and serial=?', (MESSI_19_BIG_TIME, serial))
        duplicate = copy.deepcopy(original)
        duplicate['gameplayer_id']['serial'] = player_identity.allocate(self.conn)
        self.second = dict(duplicate['gameplayer_id'])
        store.add_player(self.conn, duplicate, 'test-same-card-copy')
        self.conn.execute('insert into careerplan(id,serial,category,points,at) '
                          'select id,?,category,points,at from careerplan where id=? and serial=?',
                          (self.second['serial'], MESSI_19_BIG_TIME, serial))
        self.conn.commit()
        # Both copies are changed through the actual native careerplan handler.
        self.manual_allocate(self.first, 'PASS', 4, 'first')
        self.manual_allocate(self.second, 'STRENGTH', 4, 'second')
        choices = [sid for sid in player_skills.selectable_skill_ids(self.saved(self.first))
                   if sid in player_skills.RANDOM_SPECIAL_SKILL_IDS][:2]
        self.assertEqual(len(choices), 2)
        for identity, sid in zip((self.first, self.second), choices):
            response = self.logic.handle('CMD_USE_MYCLUB_ITEM', {
                'rqid': 'skill:' + str(identity['serial']), 'user_id': 7,
                'gameplayer_id_list': [identity], 'training_index': 0, 'ability_id': sid,
                'item_list': [{'item': {'item_type': 'SKILL_TRAINING_SELECT', 'grade': 0, 'effect': sid},
                               'present_num': 1}]})
            self.assertEqual(response['result'], 'NOERR', response)
        squad = store.squads(self.conn)[0]
        middle = (squad['squad_data']['player_list'])[:21]
        self.refs = [{'gameplayer_id': self.first}] + middle + [{'gameplayer_id': self.second}]
        self.assertEqual(len(self.refs), 23)
        squad['squad_data']['player_list'] = copy.deepcopy(self.refs)
        # Preserve the actual fixture's coach and tactical shape.
        self.squad_id = squad['squad_id']
        self.conn.execute('update squads set json=? where squad_id=?', (json.dumps(squad), self.squad_id))
        self.conn.commit()
        self.before = {key['serial']: self.saved(key) for key in (self.first, self.second)}
        self.assertNotEqual(self.before[self.first['serial']]['parameters'],
                            self.before[self.second['serial']]['parameters'])

    def saved(self, identity):
        return store.player_by_key(self.conn, identity['id'], identity['serial'])

    def manual_allocate(self, identity, group, points, label):
        current = self.saved(identity)
        reset = {'rqid': label + ':reset', 'user_id': 7, 'gameplayer_use_list': [
            {'gameplayer_id': identity, 'parameters': list(current['parameters']), 'use_list': []}]}
        self.assertEqual(self.logic.handle('CMD_USE_MYCLUB_CAREERPLAN', reset)['result'], 'NOERR')
        current = self.saved(identity)
        desired = list(current['parameters'])
        for index in player_operations.AUTO_GROUP_INDICES[group]:
            desired[index] = min(127, desired[index] + points)
        request = {'rqid': label + ':manual', 'user_id': 7,
                   'careerplan_allocate_type': 'MANUAL', 'use_auto_allocate_point': 'NO',
                   'gameplayer_use_list': [{'gameplayer_id': identity, 'parameters': desired,
                       'use_list': [{'type': 'PARAMETER', 'parameter_type': group, 'point': points}]}]}
        response = self.logic.handle('CMD_USE_MYCLUB_CAREERPLAN', request)
        self.assertEqual(response['result'], 'NOERR', response)
        stored = self.saved(identity)
        self.assertEqual(stored['parameters'], desired)
        self.assertNotEqual(stored['parameters'], stored['_careerplan_base_parameters'])

    def expected(self, identity, match=False):
        source = self.before[identity['serial']]
        expected = list(source['parameters'])
        facts = player_parameters.card_details(identity['id'])
        expected[43], expected[61], expected[62] = facts['foot'], facts['attack_style'], facts['defence_style']
        boosters = source['initial_booster_id_list']
        for index, limit in ((0, 255), (1, 1023)):
            if not 0 < expected[144 + index] <= limit:
                expected[144 + index] = boosters[index]
        if match:
            for sid in source['additional_skill_id']:
                expected[player_skills.PARAMETER_BY_SKILL[sid]] = 1
        return expected

    def assert_parameters(self, actual, identity, *, match=False, path=''):
        expected = self.expected(identity, match)
        self.assertEqual(len(actual), 147, path)
        categories = {
            'position': set(range(14)),
            'trained abilities': set(range(14, 33)) | set(range(34, 41)),
            'weak foot': {33, 45}, 'foot and styles': {43, 61, 62},
            'skill flags': set(player_skills.PARAMETER_BY_SKILL.values()),
            'boosters': {144, 145, 146}}
        covered = set().union(*categories.values())
        categories['other immutable fields'] = set(range(147)) - covered
        self.assertEqual(set().union(*categories.values()), set(range(147)))
        for category, indices in categories.items():
            with self.subTest(path=path, serial=identity['serial'], category=category):
                self.assertEqual({i: actual[i] for i in indices}, {i: expected[i] for i in indices})

    def assert_match_rows(self, rows, references, label):
        self.assertEqual([row['gameplayer_id'] for row in rows], [row['gameplayer_id'] for row in references])
        for identity in (self.first, self.second):
            row = next(row for row in rows if row['gameplayer_id'] == identity)
            self.assert_parameters(row['parameters'], identity, match=True, path=label)
            self.assertEqual(self.saved(identity), self.before[identity['serial']])

    def start(self, mode, *, reversed_order=False):
        refs = list(reversed(self.refs)) if reversed_order else self.refs
        request = {'rqid': 'start:' + mode, 'user_id': 7, 'squad_id': self.squad_id, 'player_list_home': refs}
        if mode == 'my_league':
            selected = self.logic.handle('CMD_SET_SEASON_ENTER', {
                'rqid': 'select', 'user_id': 7, 'select_team_id': 101, 'select_compe_unit_id': 0})
            self.assertEqual(selected['result'], 'NOERR', selected)
            request['match_category'] = 'MYLEAGUE'
        elif mode == 'ai_division':
            request['match_category'] = 'LEAGUE_COM'
        else:
            request['match_category'] = 'FRIEND'
        if mode != 'ordinary':
            prepared = self.logic.handle('CMD_GET_VSCOM_OPPONENT', {
                'rqid': 'prepare:' + mode, 'user_id': 7, 'match_category': request['match_category']})
            self.assertEqual(prepared['result'], 'NOERR', prepared)
            request['game_id'] = prepared['game_id']
        response = self.logic.handle('CMD_START_GAME', request)
        self.assertEqual(response['result'], 'NOERR', response)
        expected_refs = self.refs if mode == 'my_league' else refs
        self.assert_match_rows(response['squad_data_home']['player_list'], expected_refs, mode)

    def test_detail_entry_and_direct_match_preserve_all_trained_fields(self):
        for identity in (self.first, self.second):
            response = self.logic.handle('CMD_GET_MYCLUB_PLAYER_DATA', {'rqid': 'detail', 'gameplayer_id': identity})
            row = next(row for row in response['gameplayer_list'] if row['gameplayer_id'] == identity)
            self.assert_parameters(row['parameters'], identity, path='detail')
        for command in ('CMD_GET_ENTRY_INFO', 'CMD_GET_MYCLUB_ENTRY_INFO', 'CMD_LOGIN'):
            response = self.logic.handle(command, {'rqid': command})
            for identity in (self.first, self.second):
                row = next(row for row in response['entry_info']['gameplayer_list'] if row['gameplayer_id'] == identity)
                self.assert_parameters(row['parameters'], identity, path=command)
        self.assert_match_rows(self.logic._home_entries(self.refs), self.refs, 'direct home entries')

    def test_ordinary_start_keeps_manual_training_and_bench_order(self):
        self.start('ordinary', reversed_order=True)

    def test_ai_division_start_keeps_manual_training_and_bench_order(self):
        self.start('ai_division', reversed_order=True)

    def test_my_league_start_keeps_manual_training_and_bench_order(self):
        self.start('my_league', reversed_order=True)

    def test_restart_then_reorder_never_substitutes_other_copy_or_reverts_to_base(self):
        self.conn.close()
        self.conn = store.connect(self.path)
        result = release_save.migrate(self.conn)
        self.assertTrue(result['players']['already_applied'])
        self.logic = gate_logic.GateLogic(self.corpus, conn=self.conn)
        reversed_refs = list(reversed(self.refs))
        self.assert_match_rows(self.logic._home_entries(reversed_refs), reversed_refs, 'reordered after restart')
        self.assert_match_rows(self.logic._home_entries(self.refs), self.refs, 'original after restart')


if __name__ == '__main__':
    unittest.main()
