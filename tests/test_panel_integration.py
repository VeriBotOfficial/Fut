"""New private panel and gateway wiring on a temporary real release database.

Dispatch uses a stub only for the embedded module's reference to the real
GateLogic/SQLite connection; all operations below use actual business code.
This is a host integration check, not Android UI or native protocol evidence.
"""
import json
import sys
import types
import unittest
from unittest import mock

import test_skills_gateway as gateway_tests
import announcements
import club_store as store
import efootxi_panel as panel
import player_operations
import player_release
import player_skills
import training_settings


class PanelIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        gateway_tests.SkillsGatewayTest.setUpClass.__func__(cls)

    setUp = gateway_tests.SkillsGatewayTest.setUp
    tearDown = gateway_tests.SkillsGatewayTest.tearDown
    restart = gateway_tests.SkillsGatewayTest.restart

    def dispatch(self, command, req=None):
        bridge = types.SimpleNamespace(_logic=self.logic)
        with mock.patch.dict(sys.modules, {'embedded_gateway': bridge}):
            return json.loads(panel.dispatch(command, json.dumps(req or {})))

    def identity(self):
        return {'id': self.pid, 'serial': self.serial}

    def skill_request(self, skill=None, request_id='panel-skill-first'):
        return dict(self.identity(), skill=self.sid1 if skill is None else skill,
                    slot=1, request_id=request_id)

    def test_set_skill_dispatch_success_replay_and_same_slot_noop(self):
        req = self.skill_request()
        result = self.dispatch('set_skill', req)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['data']['saved'])
        self.assertEqual(result['data']['player']['learned'], [self.sid1])
        self.restart()
        retried = self.dispatch('set_skill', req)
        self.assertTrue(retried['ok'], retried)
        self.assertEqual(retried['data']['player']['learned'], [self.sid1])
        noop = self.dispatch('set_skill', self.skill_request(request_id='same-slot-new-request'))
        self.assertTrue(noop['ok'], noop)
        self.assertEqual(store.player_by_key(self.conn, self.pid, self.serial)['_efootxi_skill_revision'], 1)

    def test_old_skill_replay_returns_current_slot_and_changed_request_is_rejected(self):
        old = self.skill_request()
        self.assertTrue(self.dispatch('set_skill', old)['ok'])
        current = self.skill_request(self.sid2, 'panel-skill-second')
        self.assertTrue(self.dispatch('set_skill', current)['ok'])
        replay = self.dispatch('set_skill', old)
        self.assertTrue(replay['ok'], replay)
        self.assertEqual(replay['data']['player']['learned'], [self.sid2])
        mismatch = self.dispatch('set_skill', self.skill_request(self.sid2))
        self.assertFalse(mismatch['ok'], mismatch)
        self.assertEqual(player_skills.learned_skill_ids(store.player_by_key(self.conn, self.pid, self.serial)), [self.sid2])

    def test_reset_progression_keeps_manual_mode_and_persists_via_dispatch(self):
        self.assertEqual(self.dispatch('training_mode', {'mode': 'MANUAL'})['data']['mode'], 'MANUAL')
        player = store.player_by_key(self.conn, self.pid, self.serial)
        self.assertGreater(player_operations.progression_view(self.conn, player['gameplayer_id'])['spent'], 0)
        before = player['level_info']
        req = dict(self.identity(), request_id='manual-reset')
        result = self.dispatch('reset_progression', req)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['data']['progression']['spent'], 0)
        self.assertGreater(result['data']['progression']['available'], 0)
        self.restart()
        self.assertEqual(training_settings.get(self.conn), 'MANUAL')
        after = store.player_by_key(self.conn, self.pid, self.serial)['level_info']
        self.assertEqual(after['level'], before['level'])
        self.assertEqual(after['exp_total'], before['exp_total'])
        self.assertTrue(self.dispatch('reset_progression', req)['ok'])
        login = self.logic.handle('CMD_LOGIN', {'rqid': 'login-after-panel-reset'})
        self.assertEqual(login['entry_info']['careerplan_allocate_type'], 'MANUAL')

    def test_announcements_read_one_preserves_other_and_survives_restart(self):
        news = self.dispatch('info')['data']['news']
        self.assertGreaterEqual(len(news), 2)
        one, two = news[0]['newsId'], news[1]['newsId']
        self.assertTrue(self.dispatch('read_news', {'newsId': one})['ok'])
        self.assertTrue(self.dispatch('read_news', {'unexpected_key': two})['ok'])
        self.restart()
        states = {n['newsId']: n['readFlag'] for n in self.dispatch('info')['data']['news']}
        self.assertEqual(states[one], '1')
        self.assertEqual(states[two], '0')
        reply = self.logic.handle('CMD_GET_MYCLUB_NEWSLIST', {'rqid': 811})
        self.assertEqual(reply['news_list_num'], len(news))
        self.assertEqual({n['newsId']: n['readFlag'] for n in reply['news_list']}, states)
        ack = self.logic.handle('CMD_SET_NEWSLIST', {'rqid': 812, 'news_list': [n['newsId'] for n in news if n['newsId'] != one]})
        self.assertEqual(ack['result'], 'NOERR')
        self.assertTrue(all(n['readFlag'] == '1' for n in self.dispatch('info')['data']['news']))

    def test_diagnostics_real_schema_and_full_player_request_responses(self):
        for index in range(30):
            store.log_event(self.conn, 'efootxi_panel_test', {'index': index})
        self.conn.commit()
        result = self.dispatch('diagnostics')
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['data']['events']), 25)
        self.assertEqual(json.loads(result['data']['events'][0]['detail'])['index'], 29)
        listing = self.dispatch('players', {'query': str(self.pid), 'scope': 'active', 'offset': 0})
        self.assertTrue(listing['ok'], listing)
        self.assertTrue(any(p['id'] == self.pid for p in listing['data']['players']))
        detail = self.dispatch('player', self.identity())
        self.assertTrue(detail['ok'], detail)
        self.assertEqual(detail['data']['player']['serial'], self.serial)
        self.assertEqual(len(detail['data']['skills']), 63)

    def test_gate_release_hides_exact_instance_from_panel_and_retry_does_not_restore(self):
        player = store.player_by_key(self.conn, self.pid, self.serial)
        player['is_lock'] = 'NO'
        player_operations.persist(self.conn, player)
        self.conn.commit()
        request = {'msgid': player_release.COMMAND, 'rqid': 'gate-release', 'gameplayer_id_list': [self.identity()]}
        result = self.logic.handle(player_release.COMMAND, request)
        self.assertEqual(result['result'], 'NOERR', result)
        self.assertEqual(result['msgid'], player_release.COMMAND)
        self.assertFalse(self.dispatch('player', self.identity())['ok'])
        self.restart()
        gp = store.get_meta(self.conn, 'gp')
        self.assertEqual(self.logic.handle(player_release.COMMAND, request)['result'], 'NOERR')
        self.assertEqual(store.get_meta(self.conn, 'gp'), gp)
        self.assertIsNone(store.player_by_key(self.conn, self.pid, self.serial))
        self.assertIsNotNone(store.player_by_key(self.conn, self.pid, self.copy_serial))
        self.assertTrue(self.dispatch('diagnostics')['ok'])

    def test_in_match_mutations_and_invalid_numeric_inputs_return_json_error_without_changes(self):
        before = store.player_by_key(self.conn, self.pid, self.serial)
        for field, value in [('skill', True), ('skill', self.sid1 + 0.5), ('slot', True), ('serial', True)]:
            request = dict(self.skill_request(), **{field: value})
            self.assertFalse(self.dispatch('set_skill', request)['ok'])
        store.set_meta(self.conn, 'ai_division_active_match', json.dumps({'game_id': 444, 'started': True}))
        self.conn.commit()
        self.logic.session_match_running = True
        self.assertFalse(self.dispatch('set_skill', self.skill_request())['ok'])
        self.assertFalse(self.dispatch('reset_progression', self.identity())['ok'])
        self.assertEqual(store.player_by_key(self.conn, self.pid, self.serial), before)

    def test_persisted_unfinished_match_does_not_lock_new_session_training(self):
        store.set_meta(self.conn, 'ai_division_active_match', json.dumps({'game_id': 444, 'started': True}))
        self.conn.commit()
        self.logic.session_match_running = True
        self.assertFalse(self.dispatch('set_skill', self.skill_request())['ok'])
        self.restart()
        result = self.dispatch('set_skill', self.skill_request())
        self.assertTrue(result['ok'], result)


if __name__ == '__main__':
    unittest.main()
