"""Bounded command milestones for diagnosing local My League entry failures.

Only command/result enums, reviewed errors, side enums and array lengths are
retained. Logging owns a separate transaction and never commits business work.
"""
import json
import sqlite3

import club_store as store
import match_protocol as protocol

KIND = 'efootxi_myleague_trace'
KEEP = 64
COMMANDS = {
    'CMD_CHECK_MATCH_ENABLE': '检查开赛条件',
    'CMD_SET_SEASON_ENTER': '进入我的联赛',
    'CMD_GET_SEASON_INFO': '读取联赛信息',
    'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO': '保存联赛阵容',
    'CMD_GET_VSCOM_OPPONENT': '读取比赛对手',
    'CMD_GET_MYCLUB_VSCOM_OPPONENT': '读取比赛对手',
    'CMD_START_GAME': '返回开赛数据',
    'CMD_GET_VSCOM_GAME_RESULT': '提交比赛结果',
    'CMD_SET_MYCLUB_SEASON_UPDATE_INFO': '更新赛季信息',
    'CMD_SEND_MATCH_TEAM_INFO': '发送比赛队伍信息',
}
COUNT_LABELS = {
    'request_home': '请求主队', 'request_away': '请求客队',
    'special_squad': '保存阵容', 'participating': '上报球员',
    'goals': '己方进球事件', 'assists': '己方助攻事件',
    'opponent_goals': '对手进球事件', 'opponent_assists': '对手助攻事件',
    'response_home': '回复主队', 'response_away': '回复客队',
    'shared_card_ids': '共有卡 ID 数量', 'cache_only_card_ids': '缓存独有卡 ID 数量',
    'reply_only_card_ids': '回复独有卡 ID 数量', 'same_card_id_order': '卡 ID 顺序相同（1是/0否）',
}
RESULTS = frozenset(('NOERR', 'ERR_INVALID_PARAM', 'OTHER', 'ENTERED', 'EXCEPTION'))
EXCEPTIONS = frozenset(('MatchError', 'SQLiteError', 'ValueError', 'TypeError',
    'KeyError', 'IndexError', 'RuntimeError', 'OSError', 'MemoryError', 'Exception'))
ERRORS = protocol.DIAGNOSTIC_ERRORS | frozenset((
    'unowned_or_duplicate_match_squad_player', 'unclassified_request_error'))


def _season(conn, req):
    # A stored match ID is more precise than a menu or a conflicting category.
    try:
        gid = protocol.match_id(req, required=False)
        if gid is not None:
            return protocol.resolve_mode(conn, {'game_id': gid}) == protocol.SEASON
    except protocol.MatchError:
        pass
    try:
        category = protocol.category_mode(req)
    except protocol.MatchError:
        category = None
    if category == protocol.SEASON:
        return True
    if category in (protocol.AI, protocol.OTHER):
        return False
    return store.get_meta(conn, protocol.CONTEXT_KEY, '') == protocol.SEASON


def _card_ids(rows):
    """Read valid card-ID order without retaining IDs or inferring bad slots."""
    if not isinstance(rows, list) or len(rows) > 4096:
        return None
    result = []
    for row in rows:
        identity = row.get('gameplayer_id') if isinstance(row, dict) else None
        if not isinstance(identity, dict):
            return None
        pid, serial = identity.get('id'), identity.get('serial')
        if type(pid) is int and type(serial) is int and pid == serial == 0:
            continue
        if (type(pid) is not int or type(serial) is not int or
                not 0 < pid < 2**63 or not 0 < serial < 2**63):
            return None
        result.append(pid)
    return result


def _counts(mid, req, resp):
    mr = req.get('match_result')
    mr = mr if isinstance(mr, dict) else {}
    special = req.get('special_squad_data')
    special = special if isinstance(special, dict) else {}
    squad = special.get('squad_data')
    squad = squad if isinstance(squad, dict) else {}
    home, away = resp.get('squad_data_home'), resp.get('squad_data_away')
    candidates = {
        'request_home': req.get('player_list_home'),
        'request_away': req.get('player_list_away'),
        'special_squad': squad.get('player_list'),
        'participating': mr.get('myteam_player_list'),
        'goals': req.get('goal_player_list'), 'assists': req.get('assist_player_list'),
        'opponent_goals': req.get('opp_goal_player_list'),
        'opponent_assists': req.get('opp_assist_player_list'),
        'response_home': home.get('player_list') if isinstance(home, dict) else None,
        'response_away': away.get('player_list') if isinstance(away, dict) else None,
    }
    counts = {key: min(len(value), 4096) for key, value in candidates.items()
              if isinstance(value, list)}
    if mid == 'CMD_START_GAME' and resp.get('result') == 'NOERR':
        cache_ids = _card_ids(req.get('player_list_home'))
        reply_ids = _card_ids(home.get('player_list') if isinstance(home, dict) else None)
        # An invalid cache cannot establish a real mismatch. Omit the whole
        # comparison instead of treating a malformed slot as a missing card.
        if cache_ids is not None and reply_ids is not None:
            cached, replied = set(cache_ids), set(reply_ids)
            counts.update(shared_card_ids=len(cached & replied),
                cache_only_card_ids=len(cached - replied),
                reply_only_card_ids=len(replied - cached),
                same_card_id_order=int(cache_ids == reply_ids))
    return counts


def _known_error(conn, mid, req, *, after_id=0):
    row = conn.execute("SELECT detail FROM events WHERE kind='efootxi_match' "
                       'AND id>? ORDER BY id DESC LIMIT 1', (after_id,)).fetchone()
    if not row:
        return None
    try:
        event = json.loads(row[0])
        gid = protocol.match_id(req, required=False)
    except (ValueError, TypeError):
        return None
    if not isinstance(event, dict) or event.get('event') != 'rejected':
        return None
    if event.get('command') != mid or event.get('game_id') != gid:
        return None
    error = event.get('error')
    return error if isinstance(error, str) and error in ERRORS else None


def begin_entry(conn, mid, req):
    """Retain receipt before a handler can throw, using one row per command."""
    return record(conn, mid, req, {'result': 'ENTERED'}, _entry=True)


def finish_entry(conn, trace_id, mid, req, resp=None, *, error=None):
    if type(trace_id) is not int or trace_id <= 0:
        return False
    response = {'result': 'EXCEPTION'} if error is not None else resp
    return record(conn, mid, req, response, _trace_id=trace_id, _exception=error)


def record(conn, mid, req, resp, *, _entry=False, _trace_id=None, _exception=None):
    """Observe a completed gate response without changing its semantics.

    An uncommitted caller transaction is deliberately skipped, even on errors.
    Only failures of this isolated logging transaction are handled here; no
    business command is executed or its exception caught by this helper.
    """
    if mid not in COMMANDS or conn.in_transaction or not isinstance(req, dict) or not isinstance(resp, dict):
        return False
    # ENTER is intrinsically a My League command, before it has selected the
    # menu context. A final update belongs only to the already admitted row.
    try:
        if _trace_id is None and mid != 'CMD_SET_SEASON_ENTER' and not _season(conn, req):
            return False
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        # Only trace classification failed; do not stop the business handler
        # or touch a transaction owned by its caller.
        return False
    result = resp.get('result')
    detail = {'schema': 1, 'command': mid,
              'result': result if isinstance(result, str) and result in RESULTS else 'OTHER',
              'counts': _counts(mid, req, resp)}
    if req.get('side') in ('HOME', 'AWAY'):
        detail['request_side'] = req['side']
    if resp.get('side') in ('HOME', 'AWAY'):
        detail['response_side'] = resp['side']
    if _exception is not None:
        if isinstance(_exception, protocol.MatchError):
            name = 'MatchError'
            code = str(_exception)
            if code in ERRORS:
                detail['error'] = code
        elif isinstance(_exception, sqlite3.Error):
            name = 'SQLiteError'
        else:
            name = type(_exception).__name__
            if name not in EXCEPTIONS:
                name = 'Exception'
        detail['exception'] = name
    started = False
    try:
        conn.execute('BEGIN IMMEDIATE')
        started = True
        if _trace_id is not None:
            previous = conn.execute('SELECT detail FROM events WHERE id=? AND kind=?',
                                    (_trace_id, KIND)).fetchone()
            try:
                previous = json.loads(previous[0]) if previous else None
            except (ValueError, TypeError):
                previous = None
            if (not isinstance(previous, dict) or previous.get('command') != mid
                    or previous.get('result') != 'ENTERED'):
                conn.rollback()
                return False
        if detail['result'] == 'ERR_INVALID_PARAM':
            error = _known_error(conn, mid, req, after_id=_trace_id or 0)
            if error:
                detail['error'] = error
        if _trace_id is None:
            store.log_event(conn, KIND, detail)
            row_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            conn.execute('UPDATE events SET detail=? WHERE id=? AND kind=?',
                         (json.dumps(detail, ensure_ascii=False), _trace_id, KIND))
            row_id = _trace_id
        conn.execute('DELETE FROM events WHERE kind=? AND id NOT IN '
                     '(SELECT id FROM events WHERE kind=? ORDER BY id DESC LIMIT ?)',
                     (KIND, KIND, KEEP))
        conn.commit()
    except sqlite3.Error:
        if started:
            conn.rollback()
        return False
    return row_id if _entry else True


def recent(conn):
    """Read at most eight sanitized milestones, oldest first, with no writes."""
    try:
        rows = conn.execute('SELECT detail FROM events WHERE kind=? '
                            'ORDER BY id DESC LIMIT 8', (KIND,)).fetchall()
    except sqlite3.Error:
        return []
    entries = []
    for (raw,) in reversed(rows):
        try:
            item = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if (not isinstance(item, dict) or not isinstance(item.get('command'), str)
                or item['command'] not in COMMANDS):
            continue
        result = item.get('result')
        entry = {'command': item['command'],
                 'result': result if isinstance(result, str) and result in RESULTS else 'OTHER'}
        for field in ('request_side', 'response_side'):
            if item.get(field) in ('HOME', 'AWAY'):
                entry[field] = item[field]
        if isinstance(item.get('error'), str) and item['error'] in ERRORS:
            entry['error'] = item['error']
        if isinstance(item.get('exception'), str) and item['exception'] in EXCEPTIONS:
            entry['exception'] = item['exception']
        values = item.get('counts')
        values = values if isinstance(values, dict) else {}
        entry['counts'] = {key: value for key, value in values.items()
                           if key in COUNT_LABELS and type(value) is int and 0 <= value <= 4096}
        entries.append(entry)
    return entries


def report_lines(conn):
    entries = recent(conn)
    if not entries:
        return []
    lines = ['', '我的联赛最近步骤（从早到晚）',
             '“返回开赛数据”表示已回复游戏，不能证明已经进入球场。']
    for number, entry in enumerate(entries, 1):
        status = {'ENTERED': '已收到请求，尚未完成回复',
                  'EXCEPTION': '处理时发生异常'}.get(entry['result'], entry['result'])
        text = '%d. %s：%s' % (number, COMMANDS[entry['command']], status)
        if entry.get('request_side'):
            text += '；请求侧 ' + entry['request_side']
        if entry.get('response_side'):
            text += '；回复侧 ' + entry['response_side']
        lines.append(text)
        if entry.get('counts'):
            lines.append('；'.join(COUNT_LABELS[key] + ' ' + str(value)
                                  for key, value in entry['counts'].items()))
        if entry.get('error'):
            lines.append('核对信息：' + entry['error'])
        if entry.get('exception'):
            lines.append('异常类别：' + entry['exception'])
    return lines
