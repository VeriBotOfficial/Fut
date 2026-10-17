"""Current native mailbox shape, explicit read receipts and restart persistence."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gateway/work/tools'))
HOST_DEPS = ROOT.parents[1] / 'analysis/v120-investigation/draw-skills/host-deps'
if HOST_DEPS.is_dir():
    sys.path.insert(0, str(HOST_DEPS))

import announcements
import club_store as store
import test_skills_gateway as gateway_tests


class NativeAnnouncementsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'club.sqlite3'
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(store.SCHEMA)
        self.addCleanup(lambda: self.conn.close())
        self.items = [dict(newsId=120000, title='来源说明', body='已经获得修改许可',
                           date=1792166400, priority=0, readFlag='0'),
                      dict(newsId=120100, title='更新内容', body='出场、进球和助攻',
                           date=1792166400, priority=1, readFlag='0')]
        patcher = patch.object(announcements, 'records', side_effect=lambda: copy.deepcopy(self.items))
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, command, body=None, response=None):
        req = {'rqid': 2, **(body or {})}
        resp = {'msgid': command, 'rqid': 2, 'result': 'NOERR', **(response or {})}
        return announcements.apply(self.conn, command, req, resp)

    def restart(self):
        self.conn.close()
        self.conn = sqlite3.connect(self.path)

    def unread(self):
        return self.request('CMD_GET_MAINMENU_INFO')['unread_information_count']

    def test_local_feedback_report_refreshes_only_on_mailbox_and_preserves_read_state(self):
        nid = announcements.LOCAL_DIAGNOSTICS_NEWS_ID
        self.items.append(dict(newsId=nid, title='本地测试反馈信息', body='只在打开时读取',
                               date=1792166400, priority=99, readFlag='0'))
        with patch('match_diagnostics.report_text', side_effect=['本地测试：尚未比赛', '本地测试：正常结算']) as report:
            self.assertEqual(self.unread(), 3)
            report.assert_not_called()
            first = self.request('CMD_GET_MAILBOX_INFO')['news_list'][-1]
            self.assertEqual(first['body'], '本地测试：尚未比赛')
            self.assertEqual(first['frequency_type'], 'NONE')
            self.request('CMD_SET_NEWSLIST', {'news_list': [nid]})
            self.assertEqual(self.unread(), 2)
            second = self.request('CMD_GET_MAILBOX_INFO')['news_list'][-1]
            self.assertEqual(second['body'], '本地测试：正常结算')
            self.assertEqual(second['readFlag'], 'YES')
            self.assertEqual(report.call_count, 2)

    def test_local_feedback_report_failure_never_discloses_exception_or_blocks_mailbox(self):
        self.items.append(dict(newsId=announcements.LOCAL_DIAGNOSTICS_NEWS_ID,
                               title='本地测试反馈信息', body='', date=1792166400, priority=99))
        with patch('match_diagnostics.report_text', side_effect=RuntimeError('private-account-token')):
            out = self.request('CMD_GET_MAILBOX_INFO')
        self.assertEqual(out['result'], 'NOERR')
        body = out['news_list'][-1]['body']
        self.assertIn('暂时无法读取', body)
        self.assertNotIn('private-account-token', body)

    def test_current_mailbox_uses_native_names_types_and_never_popup_frequency(self):
        out = self.request('CMD_GET_MAILBOX_INFO', response={'present_list': [{'present_id': 9}]})
        self.assertEqual(out['present_list'], [{'present_id': 9}])
        rows = out['news_list']
        self.assertEqual([n['news_id'] for n in rows], [120000, 120100])
        self.assertEqual(rows[0]['category'], 'NEWS_CATEGORY_IMPORTANT')
        self.assertEqual(rows[1]['category'], 'NEWS_CATEGORY_UPDATE')
        for item in rows:
            self.assertEqual(set(item), {'news_id', 'title', 'date', 'body', 'readFlag', 'type',
                'priority', 'frequency_type', 'category', 'banner_name', 'link_list', 'surveyFlag'})
            self.assertEqual(item['type'], 'NEWS_TYPE_GLOBAL')
            self.assertEqual(item['readFlag'], 'NO')
            self.assertEqual(item['surveyFlag'], 'NO')
            self.assertEqual(item['frequency_type'], 'NONE')
            self.assertEqual(item['link_list'], [])

    def test_all_home_refreshes_keep_unread_count_but_disable_automatic_popup(self):
        for command in announcements.HOME_COMMANDS:
            with self.subTest(command=command):
                out = self.request(command, response={'is_news_popup': 'YES', 'unread_information_count': 999})
                self.assertEqual(out['is_news_popup'], 'NO')
                self.assertEqual(out['unread_information_count'], 2)
        self.assertEqual(self.request('OTHER', response={'is_news_popup': 'YES'})['is_news_popup'], 'NO')

    def test_gets_never_mark_read_even_when_request_contains_ids(self):
        for command in ('CMD_GET_MAILBOX_INFO', 'CMD_GET_MYCLUB_NEWSLIST', *announcements.HOME_COMMANDS):
            self.request(command, {'news_list': [120000, 120100], 'newsId': 120000})
            self.assertEqual(announcements.read_ids(self.conn), set())
            self.assertEqual(self.unread(), 2)

    def test_native_read_one_survives_restart_preserves_other_and_read_all_clears_count(self):
        ack = self.request('CMD_SET_NEWSLIST', {'news_list': [120000]})
        self.assertEqual(ack['result'], 'NOERR')
        self.restart()
        self.assertEqual(self.unread(), 1)
        self.assertEqual({n['news_id']: n['readFlag'] for n in self.request('CMD_GET_MAILBOX_INFO')['news_list']},
                         {120000: 'YES', 120100: 'NO'})
        self.request('CMD_SET_NEWSLIST', {'news_list': [120100]})
        self.restart()
        self.assertEqual(self.unread(), 0)
        self.assertTrue(all(n['readFlag'] == 'YES' for n in self.request('CMD_GET_MAILBOX_INFO')['news_list']))
        self.assertEqual(self.request('CMD_LOGIN')['is_news_popup'], 'NO')

    def test_new_release_id_is_unread_without_forgetting_existing_read_ids(self):
        self.request('CMD_SET_NEWSLIST', {'news_list': [120000, 120100]})
        self.restart()
        self.items.append(dict(newsId=120200, title='下一轮更新', body='新内容',
                               date=1792252800, priority=2, readFlag='0'))
        self.assertEqual(self.unread(), 1)
        self.assertEqual(announcements.read_ids(self.conn), {120000, 120100})
        self.assertEqual(self.request('CMD_GET_MAILBOX_INFO')['news_list'][-1]['readFlag'], 'NO')

    def test_duplicate_unknown_and_empty_native_ids_do_not_mark_other_notices(self):
        self.assertEqual(self.request('CMD_SET_NEWSLIST', {'news_list': []})['result'], 'NOERR')
        self.request('CMD_SET_NEWSLIST', {'news_list': [120000, 120000, 99999]})
        self.assertEqual(announcements.read_ids(self.conn), {120000})
        self.assertEqual(self.unread(), 1)

    def test_unconfirmed_aliases_nested_rows_and_invalid_ids_cannot_acknowledge(self):
        invalid = [{}, {'newsId': 120000}, {'news_id_list': [120000]},
                   {'news_list': [{'news_id': 120000}]}, {'news_list': ['120000']},
                   {'news_list': [True]}, {'news_list': [120000, False]},
                   {'news_list': [0]}, {'news_list': [-1]}, {'news_list': [2**31]},
                   {'news_list': [120000.0]}, {'news_list': 120000}, {'news_list': None}]
        for body in invalid:
            with self.subTest(body=body):
                self.assertEqual(self.request('CMD_SET_NEWSLIST', body)['result'], 'ERR_INVALID_PARAM')
                self.assertEqual(announcements.read_ids(self.conn), set())
        self.request('CMD_SET_MYCLUB_NEWSLIST', {'news_list': [120000, 120100]})
        self.assertEqual(announcements.read_ids(self.conn), set())

    def test_old_compatibility_list_and_internal_ack_remain_separate(self):
        announcements.mark_read(self.conn, {'newsId': 120000})
        old = self.request('CMD_GET_MYCLUB_NEWSLIST')
        self.assertEqual(old['news_list_num'], 2)
        self.assertEqual([row['readFlag'] for row in old['news_list']], ['1', '0'])
        self.assertEqual([row['readFlag'] for row in self.request('CMD_GET_MAILBOX_INFO')['news_list']], ['YES', 'NO'])

    def test_bad_stored_read_list_cannot_mark_everything_read_or_break_login(self):
        for value in ('not JSON', 'null', '{}', '"120000"', '[true, "120000", -1, 120100]'):
            store.set_meta(self.conn, announcements.READ_KEY, value)
            self.conn.commit()
            expected = 1 if value.startswith('[true') else 2
            self.assertEqual(self.unread(), expected)


class NativeAnnouncementsGatewayTest(unittest.TestCase):
    """Use actual captured responses and a disposable copy of the shipped DB."""
    @classmethod
    def setUpClass(cls):
        gateway_tests.SkillsGatewayTest.setUpClass.__func__(cls)

    setUp = gateway_tests.SkillsGatewayTest.setUp
    tearDown = gateway_tests.SkillsGatewayTest.tearDown
    restart = gateway_tests.SkillsGatewayTest.restart

    def test_native_mailbox_matches_captured_shape_and_roundtrips_read_state(self):
        command = 'CMD_GET_MAILBOX_INFO'
        capture = self.corpus[command]
        self.assertTrue(capture['news_list'])
        response = self.logic.handle(command, {'rqid': 800})
        self.assertEqual(response['msgid'], command)
        self.assertEqual(response['rqid'], 800)
        self.assertEqual(response['present_list'], capture['present_list'])
        self.assertEqual(response['news_list'], announcements.native_news(self.conn))
        expected = announcements.records()
        self.assertEqual(len(response['news_list']), len(expected))
        for supplied, original in zip(response['news_list'], expected):
            self.assertEqual(set(supplied), set(capture['news_list'][0]))
            if supplied['news_id'] == announcements.LOCAL_DIAGNOSTICS_NEWS_ID:
                import match_diagnostics
                import project_info
                self.assertEqual(supplied['body'], match_diagnostics.report_text(
                    self.conn, version=project_info.BUILD_LABEL))
                self.assertIn(project_info.BUILD_LABEL, supplied['body'])
            else:
                self.assertEqual(supplied['body'], original['body'])
            self.assertEqual(supplied['title'], original['title'])
            self.assertEqual(supplied['readFlag'], 'NO')
            for key in supplied:
                self.assertIs(type(supplied[key]), type(capture['news_list'][0][key]))
        menu = self.logic.handle('CMD_GET_MAINMENU_INFO', {'rqid': 801})
        self.assertEqual(menu['is_news_popup'], 'NO')
        self.assertEqual(menu['unread_information_count'], len(expected))
        ack = self.logic.handle('CMD_SET_NEWSLIST', {
            'rqid': 802, 'news_list': [row['news_id'] for row in response['news_list']]})
        self.assertEqual(ack['result'], 'NOERR')
        self.restart()
        menu = self.logic.handle('CMD_GET_MAINMENU_INFO', {'rqid': 803})
        self.assertEqual(menu['unread_information_count'], 0)
        self.assertEqual(menu['is_news_popup'], 'NO')
        viewed = self.logic.handle(command, {'rqid': 804})['news_list']
        self.assertTrue(all(row['readFlag'] == 'YES' for row in viewed))


if __name__ == '__main__':
    unittest.main()
