"""Offline native mailbox announcements and durable explicit read receipts.

11.1.1 uses CMD_GET_MAILBOX_INFO and CMD_SET_NEWSLIST.news_list (int32 IDs).
The older MYCLUB capture/internal panel uses a different, compatibility shape.
Reading a list never acknowledges it, and announcements never request a popup.
"""
from __future__ import annotations
import copy
import json
from pathlib import Path
import club_store as store

NOTICE_FILE = Path(__file__).resolve().parents[2] / 'outputs/reference/efootxi-notices.json'
READ_KEY = 'efootxi_news_read_v1'
LOCAL_DIAGNOSTICS_NEWS_ID = 120399
HOME_COMMANDS = frozenset(('CMD_LOGIN', 'CMD_GET_MAINMENU_INFO',
                           'CMD_GET_MYCLUB_MAINMENU_INFO'))


def valid_id(value):
    return type(value) is int and 0 < value < 2**31


def records():
    data = json.loads(NOTICE_FILE.read_text(encoding='utf-8'))
    return copy.deepcopy(data['news'])


def read_ids(conn):
    try:
        values = json.loads(store.get_meta(conn, READ_KEY, '[]'))
        return {n for n in values if valid_id(n)} if isinstance(values, list) else set()
    except (ValueError, TypeError):
        return set()


def news(conn):
    """Compatibility view for the existing internal API, not the native mailbox."""
    seen = read_ids(conn)
    out = records()
    for item in out:
        item['readFlag'] = '1' if item['newsId'] in seen else '0'
    return out


def native_news(conn):
    """Use the current native decoder/captured mailbox's names and enum types."""
    seen = read_ids(conn)
    result = []
    for item in records():
        nid = item['newsId']
        if not valid_id(nid):
            raise ValueError('invalid_announcement_id')
        body = item['body']
        if nid == LOCAL_DIAGNOSTICS_NEWS_ID:
            # A passive report inside the existing notification screen. It is
            # refreshed only when the player opens the mailbox, never a popup.
            try:
                import match_diagnostics
                import project_info
                body = match_diagnostics.report_text(conn, version=project_info.BUILD_LABEL)
            except Exception:
                body = '暂时无法读取本地反馈信息。游戏存档已保留，请反馈机型、游戏版本和复现步骤。'
        result.append({
            'news_id': nid, 'title': item['title'], 'date': item['date'],
            'body': body, 'readFlag': 'YES' if nid in seen else 'NO',
            'type': 'NEWS_TYPE_GLOBAL', 'priority': item.get('priority', 5),
            # NONE is present in the shipped mailbox capture. ONCE_POPUP_ONLY
            # and automatic-news flags are deliberately never used here.
            'frequency_type': 'NONE',
            'category': ('NEWS_CATEGORY_IMPORTANT' if item.get('priority') == 0
                         else 'NEWS_CATEGORY_UPDATE'),
            'banner_name': '', 'link_list': [], 'surveyFlag': 'NO',
        })
    return result


def _save_read_ids(conn, supplied):
    allowed = {n['newsId'] for n in records()}
    seen = read_ids(conn) | (set(supplied) & allowed)
    store.set_meta(conn, READ_KEY, json.dumps(sorted(seen)))
    conn.commit()


def mark_read(conn, req):
    """Known private API retained for existing callers; not a native command."""
    value = req.get('newsId')
    _save_read_ids(conn, [value] if valid_id(value) else [])


def acknowledge_native(conn, req):
    """Native JSON and MessagePack serializers both send news_list: int32[]."""
    supplied = req.get('news_list')
    if not isinstance(supplied, list) or any(not valid_id(n) for n in supplied):
        return False
    _save_read_ids(conn, supplied)
    return True


def apply(conn, mid, req, resp):
    if mid == 'CMD_SET_NEWSLIST':
        accepted = acknowledge_native(conn, req)
        resp.update(msgid=mid, rqid=req.get('rqid'),
                    result='NOERR' if accepted else 'ERR_INVALID_PARAM')
    # CMD_SET_MYCLUB_NEWSLIST only has a captured response, no proven current
    # request producer. It must not be treated as an implicit read-all receipt.
    items = news(conn)
    unread = sum(item['readFlag'] == '0' for item in items)
    if mid == 'CMD_GET_MYCLUB_NEWSLIST':
        resp.update(news_list=items, news_list_num=len(items), result='NOERR')
    elif mid == 'CMD_GET_MAILBOX_INFO':
        resp.update(news_list=native_news(conn), result='NOERR')
    elif 'news_list' in resp:
        # A native response containing this field uses the same NewsInfo
        # decoder. Do not leak the Nyan/private newsId/readFlag=0/1 template.
        resp['news_list'] = native_news(conn)
        if 'news_list_num' in resp:
            resp['news_list_num'] = len(items)
    if mid in HOME_COMMANDS:
        resp['is_news_popup'] = 'NO'
        resp['unread_information_count'] = unread
    elif 'is_news_popup' in resp:
        resp['is_news_popup'] = 'NO'
    return resp
