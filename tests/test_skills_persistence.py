"""Real SQLite lifecycle checks for ordered skills and progression settings."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway/work/tools'))
import club_store as store
import player_operations as operations
import player_skills as skills
import player_training
import training_settings


def player(pid=100, serial=1):
    parameters = [0] * 147
    parameters[0] = 12
    for i in operations.ABILITY_INDICES:
        parameters[i] = 60
    parameters[skills.PARAMETER_BY_SKILL[1]] = 1
    return {'gameplayer_id': {'id': pid, 'serial': serial}, 'parameters': parameters,
            'additional_skill_id': [], 'initial_booster_id_list': [0, 0, 0],
            'level_info': {'level': 1, 'level_limit': 3, 'customize_point': 0,
                           'exp_total': 0, 'exp_current_level': 0, 'exp_to_next_level': 188}}


class SkillsPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'club.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(store.SCHEMA)
        self.conn.execute('create table owned_identity_aliases(id integer,old_serial integer,new_serial integer)')
        store.set_meta(self.conn, 'owned_identity_version', '1')
        for serial in (1, 2):
            p = player(serial=serial)
            self.conn.execute('insert into gameplayers values(?,?,?,?,?)',
                              (100, serial, json.dumps(p), 'test', 'now'))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.path)

    def request(self, sid, slot=None, rqid=None, serial=1):
        if slot is None:
            slot = len(skills.learned_skill_ids(self.saved(serial)))
        return {'msgid': 'CMD_USE_MYCLUB_ITEM', 'user_id': 0,
                'rqid': rqid if rqid is not None else f'{sid}:{slot}:{serial}',
                'gameplayer_id_list': [{'id': 100, 'serial': serial}],
                'training_index': slot, 'ability_id': sid,
                'item_list': [{'item': {'item_type': 'SKILL_TRAINING_SELECT',
                                       'grade': 0, 'effect': sid}, 'present_num': 1}]}

    def apply(self, req):
        return operations.transact(self.conn, 'skill', req,
                                   lambda: operations.skill(self.conn, req))

    def saved(self, serial=1):
        return store.player_by_key(self.conn, 100, serial)

    def test_selected_regular_and_st_survive_real_database_reopen(self):
        self.apply(self.request(32))
        self.apply(self.request(18))
        self.restart()
        self.assertEqual(skills.learned_skill_ids(self.saved()), [32, 18])
        self.assertEqual(skills.learned_skill_ids(self.saved(2)), [])
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 2)

    def test_explicit_replacement_preserves_other_slots_and_order(self):
        for sid in [32, 18, 53, 64, 73]:
            self.apply(self.request(sid))
        self.apply(self.request(11, slot=1))
        self.restart()
        self.assertEqual(skills.learned_skill_ids(self.saved()), [32, 11, 53, 64, 73])
        projected = skills.match_parameters(self.saved(), self.saved()['parameters'])
        self.assertEqual(projected[skills.PARAMETER_BY_SKILL[18]], 0)
        self.assertEqual(projected[skills.PARAMETER_BY_SKILL[11]], 1)

    def test_out_of_range_sixth_slot_does_not_replace_existing(self):
        for sid in [32, 18, 53, 64, 73]:
            self.apply(self.request(sid))
        before = self.saved()
        with self.assertRaisesRegex(ValueError, 'invalid skill slot'):
            self.apply(self.request(11))
        self.restart()
        self.assertEqual(self.saved(), before)

    def test_receipt_retry_after_restart_never_rerolls_or_reorders(self):
        req = self.request(18, rqid='retry')
        self.apply(req)
        self.restart()
        with mock.patch.object(operations, 'skill', side_effect=AssertionError('must not execute twice')):
            reply = self.apply(req)
        self.assertEqual(reply['gameplayer_list'][0]['additional_skill_id'], [18])
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 1)

    def test_legacy_random_request_is_drawn_once_then_stable_after_restart(self):
        req = self.request(18, rqid='legacy-random')
        req.pop('ability_id')
        req['item_list'][0]['item'].update(item_type='SKILL_TRAINING', effect=0)
        generator = mock.Mock()
        generator.choice.return_value = 18
        with mock.patch.object(operations.random, 'SystemRandom', return_value=generator):
            self.apply(req)
        self.assertEqual(generator.choice.call_count, 1)
        self.restart()
        with mock.patch.object(operations.random, 'SystemRandom', side_effect=AssertionError('reroll')):
            reply = self.apply(req)
        self.assertEqual(reply['skill_training_result']['additional_skill_id'], 18)
        self.assertEqual(self.saved()['additional_skill_id'], [18])

    def test_old_receipt_returns_current_state_after_later_slot_edits(self):
        req = self.request(18, rqid='old')
        self.apply(req)
        self.apply(self.request(32))
        self.apply(self.request(11, slot=0, rqid='replacement'))
        self.restart()
        reply = self.apply(req)
        self.assertEqual(reply['gameplayer_list'][0]['additional_skill_id'], [11, 32])
        self.assertEqual(reply['skill_training_result']['additional_skill_id'], 11)
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 3)

    def test_reused_receipt_for_different_choice_fails_without_mutation(self):
        self.apply(self.request(18, rqid='same'))
        with self.assertRaisesRegex(ValueError, 'request id reused'):
            self.apply(self.request(32, rqid='same'))
        self.restart()
        self.assertEqual(self.saved()['additional_skill_id'], [18])

    def test_same_skill_in_same_slot_is_noop(self):
        self.apply(self.request(18))
        self.apply(self.request(18, slot=0, rqid='confirm-existing'))
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 1)

    def test_new_session_can_reuse_native_counter_without_old_receipt(self):
        first = dict(self.request(18, rqid=1), session_id='login-one')
        self.apply(first)
        self.restart()
        second = dict(self.request(32, rqid=1), session_id='login-two')
        self.apply(second)
        self.restart()
        self.assertEqual(self.saved()['additional_skill_id'], [18, 32])
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 2)
        with mock.patch.object(operations, 'skill', side_effect=AssertionError('must not replay')):
            reply = self.apply(second)
        self.assertEqual(reply['gameplayer_list'][0]['additional_skill_id'], [18, 32])

    def test_session_scoped_old_retry_returns_current_five_slots(self):
        first = dict(self.request(18, rqid=1), session_id='same-login')
        self.apply(first)
        for index, sid in enumerate([32, 53, 64, 73], 2):
            self.apply(dict(self.request(sid, rqid=index), session_id='same-login'))
        self.restart()
        reply = self.apply(first)
        self.assertEqual(reply['gameplayer_list'][0]['additional_skill_id'], [18, 32, 53, 64, 73])
        self.assertEqual(self.saved()['_efootxi_skill_revision'], 5)

    def test_innate_and_duplicate_skills_rejected(self):
        with self.assertRaisesRegex(ValueError, 'already learned or innate'):
            self.apply(self.request(1))
        self.apply(self.request(18))
        with self.assertRaisesRegex(ValueError, 'already learned or innate'):
            self.apply(self.request(18, rqid='duplicate'))
        self.assertEqual(self.saved()['additional_skill_id'], [18])

    def test_bad_slots_types_and_unknown_skills_leave_database_unchanged(self):
        before = self.saved()
        for req in [self.request(18, slot=5), self.request(74), self.request(14),
                    self.request(True), self.request(18, slot=True)]:
            with self.subTest(req=req), self.assertRaises(ValueError):
                self.apply(req)
        self.assertEqual(self.saved(), before)

    def test_transaction_failure_rolls_back_player_and_receipt(self):
        with mock.patch.object(store, 'log_event', side_effect=RuntimeError('write interrupted')):
            with self.assertRaises(RuntimeError):
                self.apply(self.request(18))
        self.restart()
        self.assertEqual(self.saved()['additional_skill_id'], [])
        self.assertEqual(self.conn.execute('select count(*) from player_operation_receipts').fetchone()[0], 0)

    def test_canonical_empty_slots_override_stale_legacy_alias(self):
        p = self.saved()
        p.update(additional_skill=[18, 32], is_skill_added='YES')
        skills.apply_wire(p, 'detail')
        self.assertEqual(p['additional_skill_id'], [])
        self.assertEqual(p['additional_skill'], [])
        self.assertEqual(p['is_skill_added'], 'NO')

    def test_detail_list_and_match_share_same_order_without_mutating_base(self):
        self.apply(self.request(64))
        self.apply(self.request(18))
        self.restart()
        saved = self.saved()
        baseline = copy.deepcopy(saved)
        detail = skills.apply_wire(copy.deepcopy(saved), 'detail')
        listing = skills.apply_wire(copy.deepcopy(saved), 'list')
        match = skills.match_parameters(saved, saved['parameters'])
        self.assertEqual(detail['additional_skill'], listing['additional_skill_id'])
        self.assertEqual(detail['is_skill_added'], 'YES')
        for sid in [1, 18, 64]:
            self.assertEqual(match[skills.PARAMETER_BY_SKILL[sid]], 1)
        self.assertEqual(saved, baseline)
        self.assertEqual(saved['parameters'][skills.PARAMETER_BY_SKILL[64]], 0)

    def test_skill_zero_is_valid_not_treated_as_empty(self):
        self.apply(self.request(0))
        self.restart()
        self.assertEqual(self.saved()['additional_skill_id'], [0])
        self.assertEqual(skills.apply_wire(copy.deepcopy(self.saved()), 'detail')['is_skill_added'], 'YES')

    def test_level_training_preserves_selected_slots(self):
        self.apply(self.request(18))
        trained, *_ = player_training.train(self.saved(), 1000)
        self.assertEqual(trained['additional_skill_id'], [18])
        self.assertEqual(trained['_efootxi_skill_revision'], 1)

    def test_catalog_is_mapped_named_and_excludes_innate_and_learned(self):
        self.apply(self.request(18))
        catalog = skills.catalog(self.saved())
        ids = {x['id'] for x in catalog}
        self.assertEqual(ids, skills.SELECTABLE_SKILL_IDS)
        self.assertEqual(len(ids), 63)
        self.assertTrue({9, 31, 70, 74}.isdisjoint(ids))
        self.assertTrue(all(x['display_name'] and x['parameter_index'] < 147 for x in catalog))
        self.assertEqual(sum(x['special'] for x in catalog), 18)
        self.assertNotIn(18, skills.selectable_skill_ids(self.saved()))
        self.assertNotIn(1, skills.selectable_skill_ids(self.saved()))

    def test_manual_setting_roundtrip_does_not_touch_player(self):
        before = self.saved()
        reply = training_settings.handle(self.conn, {'rqid': 3, 'careerplan_allocate_type': 'MANUAL'})
        self.assertEqual(reply['msgid'], training_settings.COMMAND)
        self.restart()
        response = training_settings.apply(self.conn, {'entry_info': {'careerplan_allocate_type': 'AUTO'}})
        self.assertEqual(response['entry_info']['careerplan_allocate_type'], 'MANUAL')
        self.assertEqual(self.saved(), before)
        training_settings.handle(self.conn, {'rqid': 4, 'careerplan_allocate_type': 'AUTO'})
        self.restart()
        self.assertEqual(training_settings.get(self.conn), 'AUTO')

    def test_invalid_setting_does_not_replace_valid_mode(self):
        training_settings.handle(self.conn, {'careerplan_allocate_type': 'MANUAL'})
        for value in ('UNKNOWN', 0, None, True):
            reply = training_settings.handle(self.conn, {'careerplan_allocate_type': value})
            self.assertEqual(reply['result'], 'ERR_INVALID_PARAM')
        self.restart()
        self.assertEqual(training_settings.get(self.conn), 'MANUAL')

    def test_manual_allocation_works_after_training_without_switching_mode(self):
        training_settings.handle(self.conn, {'careerplan_allocate_type': 'MANUAL'})
        trained, *_ = player_training.train(self.saved(), 1000)
        operations.persist(self.conn, trained)
        self.conn.commit()
        parameters = list(trained['parameters'])
        for index in operations.AUTO_GROUP_INDICES['SHOOT']:
            parameters[index] += 1
        request = {'rqid': 'manual-allocation', 'user_id': 0,
                   'careerplan_allocate_type': 'MANUAL', 'use_auto_allocate_point': 'NO',
                   'gameplayer_use_list': [{'gameplayer_id': {'id': 100, 'serial': 1},
                                            'use_list': [{'type': 'PARAMETER', 'parameter_type': 'SHOOT', 'point': 1}],
                                            'parameters': parameters}]}
        operations.transact(self.conn, 'career', request,
                            lambda: operations.career(self.conn, request, lambda players: []))
        self.restart()
        self.assertEqual(training_settings.get(self.conn), 'MANUAL')
        self.assertEqual(self.saved()['parameters'], parameters)
        self.assertEqual(self.saved()['level_info']['customize_point'], 3)
        self.assertEqual(store.careerplan_counts(self.conn, 100, 1), {'SHOOT': 1})


if __name__ == '__main__':
    unittest.main()
