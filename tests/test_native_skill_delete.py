"""Native delete_skill_index compacts its zero-based, persistent slots like the client."""
import copy
import unittest
from unittest import mock
import test_native_skill_contract as native


class NativeSkillDeleteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        native.gateway_fixture.SkillsGatewayTest.setUpClass()

    def setUp(self):
        self.case = native.NativeSkillContractTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        for index, sid in enumerate(self.case.ids):
            self.case.send(self.case.request(sid, index, index+1))
        self.initial = copy.deepcopy(self.case.saved())

    def request(self, slot, rqid=100, session='delete-session'):
        return {'msgid':'CMD_DELETE_MYCLUB_SKILL', 'user_id':0, 'rqid':rqid,
                'session_id':session, 'gameplayer_id':self.case.fixture.identity(),
                'delete_skill_index':slot}

    def test_delete_middle_preserves_other_skills_order_copy_and_innate_parameters(self):
        c = self.case
        reply = c.send(self.request(2))
        self.assertNotIn('gameplayer_list', reply)
        c.fixture.restart()
        expected = c.ids[:2] + c.ids[3:] + [native.skills.EMPTY_SKILL_ID]
        c.assert_all_views(expected)
        self.assertEqual(c.saved()['parameters'], self.initial['parameters'])
        match = c.fixture.logic._home_entries([{'gameplayer_id':c.fixture.identity()}])[0]
        self.assertEqual(match['parameters'][native.skills.PARAMETER_BY_SKILL[c.ids[2]]], 0)

    def test_delete_all_retains_empty_positions_and_sets_no_added_skill(self):
        c = self.case
        for index in range(4, -1, -1):
            c.send(self.request(index, 100+index))
        c.fixture.restart()
        c.assert_all_views([native.skills.EMPTY_SKILL_ID]*5)
        self.assertEqual(c.saved()['is_skill_added'], 'NO')
        detail = c.fixture.logic.handle('CMD_GET_MYCLUB_PLAYER_DATA',
                                      {'gameplayer_id':c.fixture.identity(), 'rqid':301})
        self.assertEqual(c.fixture.find(detail['gameplayer_list'])['is_skill_added'], 'NO')
        snapshot = c.fixture.logic.handle('CMD_GET_ENTRY_INFO', {'rqid':302})
        self.assertEqual(c.fixture.find(snapshot['entry_info']['gameplayer_list'])['is_skill_added'], 'NO')
        c.send(c.request(c.ids[4], 4, 303))
        c.fixture.restart()
        c.assert_all_views([native.skills.EMPTY_SKILL_ID]*4+[c.ids[4]])

    def test_retry_does_not_delete_retrained_slot_but_new_session_can(self):
        c = self.case
        req = self.request(1)
        c.send(req)
        c.send(c.request(c.ids[1], 4, 201))
        revision = c.saved()['_efootxi_skill_revision']
        c.fixture.restart()
        c.send(req)
        self.assertEqual(c.saved()['_efootxi_skill_revision'], revision)
        c.assert_all_views(c.ids[:1] + c.ids[2:] + [c.ids[1]])
        c.send(self.request(1, session='next-login'))
        c.fixture.restart()
        expected = c.ids[:1] + c.ids[3:] + [c.ids[1], native.skills.EMPTY_SKILL_ID]
        c.assert_all_views(expected)

    def test_invalid_identity_slot_and_counter_conflict_never_delete(self):
        c = self.case
        for index in (-1, 5, True, '1'):
            req = self.request(index)
            reply = c.fixture.logic.handle(req['msgid'], req)
            self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
            self.assertEqual(c.saved(), self.initial)
        req = self.request(1); req['gameplayer_id']['serial'] = 2147483000
        self.assertEqual(c.fixture.logic.handle(req['msgid'], req)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(c.saved(), self.initial)
        c.send(self.request(1))
        before = copy.deepcopy(c.saved())
        req = self.request(2)
        self.assertEqual(c.fixture.logic.handle(req['msgid'], req)['result'], 'ERR_INVALID_PARAM')
        self.assertEqual(c.saved(), before)

    def test_interrupted_delete_rolls_back_slot_and_receipt(self):
        c = self.case
        req = self.request(2)
        with mock.patch.object(native.store, 'log_event', side_effect=RuntimeError('interrupted write')):
            with self.assertRaises(RuntimeError):
                c.fixture.logic.handle(req['msgid'], req)
        c.fixture.restart()
        self.assertEqual(c.saved(), self.initial)
        c.send(req)
        expected = c.ids[:2] + c.ids[3:] + [native.skills.EMPTY_SKILL_ID]
        c.assert_all_views(expected)


if __name__ == '__main__':
    unittest.main()
