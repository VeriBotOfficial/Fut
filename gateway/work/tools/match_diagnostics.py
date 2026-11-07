"""Read-only, bounded match feedback for the native notice page.

No account identifiers, player identities, names, network addresses or tokens
are emitted. Opening this report never starts, finishes or repairs a match.
"""
import json
import re
import myleague_trace


_MODES = {'ai_division': 'AI Division', 'my_league': 'My League',
          'local_vscom': 'Local vs COM'}
_STAGES = {'prepared': 'Opponent received', 'started': 'Match-start data returned',
           'settled': 'Local settlement completed', 'replayed': 'Match result returned',
           'rejected': 'Request validation failed'}
_COMMANDS = {'CMD_START_GAME': 'Start match', 'CMD_GET_VSCOM_GAME_RESULT': 'Submit match result',
             'CMD_GET_VSCOM_OPPONENT': 'Get opponent', 'CMD_GET_MYCLUB_VSCOM_OPPONENT': 'Get opponent',
             'CMD_SET_MYCLUB_SPECIAL_DREAM_SQUAD_INFO': '保存My League阵容'}


def _code(value):
    if value is None or value == '':
        return 'Not provided'
    if type(value) is bool:
        return 'YES' if value else 'NO'
    if type(value) is int:
        return str(value) if abs(value) < 2**31 else 'Out of range'
    if isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_]{1,80}', value):
        return value
    return 'Unrecognized'


def report_text(conn, version='1.2'):
    """Return at most three recent games as plain native-announcement text."""
    version = version if isinstance(version, str) and len(version) <= 80 else '1.2'
    lines = ['Local Match Diagnostics', 'Version: ' + version,
             'If a match gets stuck, a normally completed match is marked as a forfeit, or statistics do not increase, capture this page and send it to the maintainer.',
             'This page only displays local records. It does not change scores, saves, or player statistics.', '']
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
        lines += ['There are no local match-start or settlement records yet.',
                  'If you encounter an issue, return to Notifications in the top-right corner and open this page again.']
        lines.extend(myleague_trace.report_lines(conn))
        return '\n'.join(lines)
    for number, entry in enumerate(groups.values(), 1):
        lines.append('Recent match %d: %s' % (number, _MODES.get(entry.get('mode'), 'Mode not confirmed')))
        event = entry.get('event')
        lines.append('Progress: ' + _STAGES.get(event, _code(event)))
        if entry.get('command') in _COMMANDS:
            lines.append('Request step: ' + _COMMANDS[entry['command']])
        roster = entry.get('start_roster') or {}
        if roster:
            lines.append('Starting lineup: %s slots, including %s empty slots' % (
                _code(roster.get('count')), _code(roster.get('empty_slots'))))
        if event == 'started':
            lines.append('Match-start data was returned to the game, but no result has been received yet. Check the actual game screen to confirm whether the match loaded.')
        elif event == 'prepared':
            lines.append('Opponent received，尚未确认进入比赛。')
        lines.append('Game-reported end reason: ' + _code(entry.get('abnormalend_reason')))
        lines.append('Game-reported forfeit flag: ' + _code(entry.get('intentional_give_up')))
        resolved = entry.get('settlement') or {}
        if resolved:
            lines.append('Local settlement: %s; score %s–%s; marked as forfeit: %s' % (
                _code(resolved.get('result')), _code(resolved.get('home')),
                _code(resolved.get('away')), _code(resolved.get('gave_up'))))
        response = entry.get('response') or {}
        if response:
            lines.append('Game response: %s / %s / %s' % (
                _code(response.get('game_res')), _code(response.get('gameend_reason')),
                _code(response.get('abnormalend_reason'))))
            lines.append('Player records refreshed: %s' % _code(response.get('player_update_count')))
        statistics = entry.get('statistics') or {}
        status = statistics.get('status')
        label = {'applied': 'Saved', 'pending': 'Waiting for valid player data',
                 'excluded_give_up': 'This match was identified as a forfeit and was not counted'}.get(status, 'Not counted')
        lines.append('Player statistics: ' + label)
        if statistics.get('error'):
            lines.append('Statistics validation: ' + _code(statistics['error']))
        participation = entry.get('participation') or {}
        lines.append('Received %s players, %s goal events, and %s assist events' % (
            _code(participation.get('player_count')),
            _code(participation.get('goal_player_list')),
            _code(participation.get('assist_player_list'))))
        for field, label in (('is_starting_member', 'Starting-player flag'), ('is_join_game', 'Played-in-match flag')):
            values = participation.get(field) or []
            if isinstance(values, list):
                lines.append(label + '：' + '/'.join(_code(value) for value in values[:8]))
        if entry.get('error'):
            lines.append('Request validation: ' + _code(entry['error']))
        lines.append('')
    lines.extend(myleague_trace.report_lines(conn))
    return '\n'.join(lines).rstrip()
