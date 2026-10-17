"""Real native 0..4 slot contract, including the released test.2 regression.

Use the actual delivered old function bodies for negative controls. We never
import or execute the reference APK; only the two selected Python function ASTs
are compiled with the fixture's audited module dependencies.
"""
import ast
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'gateway/work/tools'), str(ROOT/'tests'), str(ROOT/'build/host-deps')]
import club_store as store
import player_operations as operations
import player_skills as skills
import test_skills_gateway as gateway_fixture


class NativeSkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_fixture.SkillsGatewayTest.setUpClass()
        old_apk = ROOT/'release/v1.2.0-test.2/efootxi-test-1.2.0.apk'
        cls.old = None
        if old_apk.exists():
            with zipfile.ZipFile(old_apk) as z:
                source = z.read('assets/gateway-release/work/tools/player_operations.py')
            cls.old_source_sha256 = hashlib.sha256(source).hexdigest()
            tree = ast.parse(source)
            functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                         and n.name in ('transact', 'skill')]
            assert len(functions) == 2
            namespace = dict(vars(operations))
            exec(compile(ast.Module(body=functions, type_ignores=[]),
                         'released-test.2/player_operations.py', 'exec'), namespace)
            cls.old = {name: namespace[name] for name in ('transact', 'skill')}

    def setUp(self):
        self.fixture = gateway_fixture.SkillsGatewayTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        p = store.player_by_key(self.fixture.conn, self.fixture.pid, self.fixture.serial)
        self.ids = [sid for sid in skills.selectable_skill_ids(p)
                    if sid in skills.RANDOM_SPECIAL_SKILL_IDS][:5]
        self.assertEqual(len(self.ids), 5)

    def request(self, sid, index, rqid, session='native-login', random=False):
        req = {'msgid':'CMD_USE_MYCLUB_ITEM', 'user_id':0, 'rqid':rqid,
               'session_id':session, 'gameplayer_id_list':[self.fixture.identity()],
               'training_index':index, 'ability_id':sid,
               'item_list':[{'item':{'item_type':'SKILL_TRAINING_SELECT',
                                    'grade':0, 'effect':sid}, 'present_num':100}]}
        if random:
            req.pop('ability_id')
            req['item_list'][0]['item'].update(item_type='SKILL_TRAINING', effect=0)
        return req

    def send(self, req):
        reply = self.fixture.logic.handle(req['msgid'], req)
        self.assertEqual(reply['result'], 'NOERR', reply)
        return reply

    def saved(self):
        return store.player_by_key(self.fixture.conn, self.fixture.pid, self.fixture.serial)

    def assert_all_views(self, expected):
        f = self.fixture
        login = f.logic.handle('CMD_LOGIN', {'rqid':800})
        self.assertEqual(f.find(login['entry_info']['gameplayer_list'])['additional_skill_id'], expected)
        detail = f.logic.handle('CMD_GET_MYCLUB_PLAYER_DATA',
                                {'rqid':801, 'gameplayer_id':f.identity()})
        row = f.find(detail['gameplayer_list'])
        self.assertEqual(row['additional_skill_id'], expected)
        self.assertEqual(row['additional_skill'], expected)
        snapshot = f.logic.handle('CMD_GET_ENTRY_INFO', {'rqid':802})
        self.assertEqual(f.find(snapshot['entry_info']['gameplayer_list'])['additional_skill'], expected)
        match = f.logic._home_entries([{'gameplayer_id':f.identity()}])[0]
        for sid in expected:
            if sid != skills.EMPTY_SKILL_ID:
                self.assertEqual(match['parameters'][skills.PARAMETER_BY_SKILL[sid]], 1)
        self.assertEqual(skills.learned_skill_ids(self.saved()), expected)
        self.assertEqual(skills.learned_skill_ids(store.player_by_key(f.conn, f.pid, f.copy_serial)), [])

    def test_native_five_slots_survive_each_restart_and_all_response_shapes(self):
        for index, sid in enumerate(self.ids):
            reply = self.send(self.request(sid, index, index+1))
            self.assertEqual(self.fixture.find(reply['gameplayer_list'])['additional_skill_id'], self.ids[:index+1])
            self.fixture.restart()
            self.assert_all_views(self.ids[:index+1])

    def test_zero_based_replacements_leave_other_four_slots_unchanged(self):
        for index, sid in enumerate(self.ids):
            self.send(self.request(sid, index, index+1))
        expected = list(self.ids)
        replacements = [sid for sid in skills.selectable_skill_ids(self.saved())][:5]
        for index, sid in enumerate(replacements):
            self.send(self.request(sid, index, index+101))
            expected[index] = sid
            self.fixture.restart()
            self.assert_all_views(expected)

    def test_fifth_slot_can_be_filled_first_and_empty_positions_survive_restart(self):
        expected = [skills.EMPTY_SKILL_ID]*4+[self.ids[4]]
        self.send(self.request(self.ids[4], 4, 1))
        self.fixture.restart()
        self.assert_all_views(expected)
        for index in (2, 0, 3, 1):
            self.send(self.request(self.ids[index], index, 10+index))
            expected[index] = self.ids[index]
            self.fixture.restart()
            self.assert_all_views(expected)
        self.assertEqual(expected, self.ids)
        self.assertNotIn(skills.EMPTY_SKILL_ID, skills.SELECTABLE_SKILL_IDS)
        self.assertNotIn(skills.EMPTY_SKILL_ID, skills.PARAMETER_BY_SKILL)

    def test_old_retry_after_later_slots_filled_returns_current_five_positions(self):
        req = self.request(self.ids[4], 4, 1)
        self.send(req)
        for index in range(4):
            self.send(self.request(self.ids[index], index, 10+index))
        self.fixture.restart()
        reply = self.send(req)
        self.assertEqual(self.fixture.find(reply['gameplayer_list'])['additional_skill_id'], self.ids)
        self.assert_all_views(self.ids)

    def test_out_of_range_sixth_slot_is_rejected_without_losing_five(self):
        for index, sid in enumerate(self.ids):
            self.send(self.request(sid, index, index+1))
        before = copy.deepcopy(self.saved())
        req = self.request(skills.selectable_skill_ids(before)[0], 5, 106)
        reply = self.fixture.logic.handle(req['msgid'], req)
        self.assertNotEqual(reply['result'], 'NOERR')
        self.fixture.restart()
        self.assertEqual(self.saved(), before)

    def test_released_test2_reproduces_five_native_selections_saving_only_four(self):
        if self.old is None:
            self.skipTest('released test.2 APK unavailable')
        with mock.patch.object(operations, 'transact', self.old['transact']), \
             mock.patch.object(operations, 'skill', self.old['skill']):
            for index, sid in enumerate(self.ids):
                self.send(self.request(sid, index, index+1))
        self.fixture.restart()
        self.assertEqual(self.saved()['additional_skill_id'], self.ids[1:])
        self.assertEqual(len(self.saved()['additional_skill_id']), 4)

    def test_released_test2_reproduces_new_login_request_swallowed_as_old_success(self):
        if self.old is None:
            self.skipTest('released test.2 APK unavailable')
        generator = mock.Mock()
        generator.choice.side_effect = self.ids[:2]
        with mock.patch.object(operations, 'transact', self.old['transact']), \
             mock.patch.object(operations, 'skill', self.old['skill']), \
             mock.patch.object(operations.random, 'SystemRandom', return_value=generator):
            self.send(self.request(0, 0, 1, 'login-A', random=True))
            self.fixture.restart()
            self.send(self.request(0, 0, 1, 'login-B', random=True))
        self.assertEqual(generator.choice.call_count, 1)
        self.fixture.restart()
        self.assertEqual(self.saved()['additional_skill_id'], self.ids[:1])

    def test_new_login_reused_counter_performs_new_training_and_retry_does_not(self):
        generator = mock.Mock()
        generator.choice.side_effect = self.ids[:2]
        with mock.patch.object(operations.random, 'SystemRandom', return_value=generator):
            self.send(self.request(0, 0, 1, 'login-A', random=True))
            self.fixture.restart()
            req = self.request(0, 0, 1, 'login-B', random=True)
            self.send(req)
            self.assertEqual(generator.choice.call_count, 2)
            self.fixture.restart()
            self.send(req)
            self.assertEqual(generator.choice.call_count, 2)
        self.assert_all_views(self.ids[1:2])


if __name__ == '__main__':
    unittest.main()
