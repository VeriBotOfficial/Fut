"""Read-only, bounded match feedback for the native notice page.

No account identifiers, player identities, names, network addresses or tokens
are emitted. Opening this report never starts, finishes or repairs a match.
"""
import json
import re
import myleague_trace


_MODES = {'ai_division': 'AI 组别', 'my_league': '我的联赛',
          'local_vscom': '本地电脑比赛'}
_STAGES = {'prepared': '已取得对手', 'started': '已返回开赛数据',
           'settled': '已完成本地结算', 'replayed': '已返回本场结算记录',
           'rejected': '请求未通过核对'}
_COMMANDS = {'CMD_START_GAME': '确认开赛', 'CMD_GET_VSCOM_GAME_RESULT': '提交比赛结果',
             'CMD_GET_VSCOM_OPPONENT': '取得对手', 'CMD_GET_MYCLUB_VSCOM_OPPONENT': '取得对手',
             'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO': '保存我的联赛阵容'}


def _code(value):
    if value is None or value == '':
        return '未提供'
    if type(value) is bool:
        return 'YES' if value else 'NO'
    if type(value) is int:
        return str(value) if abs(value) < 2**31 else '超出范围'
    if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', value):
        return value
    return '未识别'


def report_text(conn, version='1.2'):
    """Return at most three recent games as plain native-announcement text."""
    version = version if isinstance(version, str) and len(version) <= 80 else '1.2'
    lines = ['本地比赛反馈信息', '版本：' + version,
             '如果比赛转圈、正常踢完却判弃赛，或统计没有增加，请截取本页发给作者。',
             '本页仅查看本机记录，不会改变比分、存档或个人统计。', '']
    entries = []
    try:
        saved = conn.execute("SELECT detail FROM events WHERE kind='efootxi_match' "
                             'ORDER BY id DESC LIMIT 24').fetchall()
    except Exception:
        saved = []
    for (raw,) in saved:
        try:
            entry = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    groups = {}
    for entry in entries:
        active = entry.get('active') or {}
        key = entry.get('mode'), entry.get('game_id') or active.get('game_id')
        if key not in groups:
            if len(groups) == 3:
                continue
            groups[key] = entry
    if not groups:
        lines += ['目前还没有本机开赛或结算记录。',
                  '遇到问题后，请返回右上角通知，再打开本页。']
        lines.extend(myleague_trace.report_lines(conn))
        return '\n'.join(lines)
    for number, entry in enumerate(groups.values(), 1):
        lines.append('最近第 %d 场：%s' % (number, _MODES.get(entry.get('mode'), '模式未确认')))
        event = entry.get('event')
        lines.append('进度：' + _STAGES.get(event, _code(event)))
        if entry.get('command') in _COMMANDS:
            lines.append('请求步骤：' + _COMMANDS[entry['command']])
        roster = entry.get('start_roster') or {}
        if roster:
            lines.append('开赛名单：%s 个位置，其中空位 %s 个' % (
                _code(roster.get('count')), _code(roster.get('empty_slots'))))
        if event == 'started':
            lines.append('已向游戏回复开赛数据，尚未收到结算；是否进入球场需结合实际画面。')
        elif event == 'prepared':
            lines.append('已取得对手，尚未确认进入比赛。')
        lines.append('游戏上报结束原因：' + _code(entry.get('abnormalend_reason')))
        lines.append('游戏上报放弃标记：' + _code(entry.get('intentional_give_up')))
        resolved = entry.get('settlement') or {}
        if resolved:
            lines.append('本地结算：%s；比分 %s:%s；判定放弃 %s' % (
                _code(resolved.get('result')), _code(resolved.get('home')),
                _code(resolved.get('away')), _code(resolved.get('gave_up'))))
        response = entry.get('response') or {}
        if response:
            lines.append('返回游戏：%s / %s / %s' % (
                _code(response.get('game_res')), _code(response.get('gameend_reason')),
                _code(response.get('abnormalend_reason'))))
            lines.append('球员数据刷新：%s 人' % _code(response.get('player_update_count')))
        statistics = entry.get('statistics') or {}
        status = statistics.get('status')
        label = {'applied': '已保存', 'pending': '等待有效球员数据',
                 'excluded_give_up': '本场被识别为放弃，未计入'}.get(status, '尚未计入')
        lines.append('个人统计：' + label)
        if statistics.get('error'):
            lines.append('统计核对信息：' + _code(statistics['error']))
        participation = entry.get('participation') or {}
        lines.append('收到球员 %s 人，进球事件 %s 条，助攻事件 %s 条' % (
            _code(participation.get('player_count')),
            _code(participation.get('goal_player_list')),
            _code(participation.get('assist_player_list'))))
        for field, label in (('is_starting_member', '首发标记'), ('is_join_game', '上场标记')):
            values = participation.get(field) or []
            if isinstance(values, list):
                lines.append(label + '：' + '/'.join(_code(value) for value in values[:8]))
        if entry.get('error'):
            lines.append('请求核对信息：' + _code(entry['error']))
        lines.append('')
    lines.extend(myleague_trace.report_lines(conn))
    return '\n'.join(lines).rstrip()
