"""Private in-process settings bridge. Called on the gateway's business executor."""
from __future__ import annotations
import json
import uuid
import club_store as store
import player_parameters
import player_operations
import player_skills
import training_settings
import announcements
import project_info


def _integer(value, name, minimum=0):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < minimum:
        raise ValueError(name + '必须是有效整数')
    return value


def _player(conn, req):
    pid = _integer(req.get('id'), '球员ID', 1)
    serial = _integer(req.get('serial'), '球员实例', 1)
    player = store.player_by_key(conn, pid, serial)
    if not player:
        raise ValueError('找不到这份球员，请返回列表刷新')
    return player


def _summary(player):
    gid = player['gameplayer_id']
    card = player_parameters.card_details(gid['id']) or {}
    return dict(id=gid['id'], serial=gid['serial'], name=card.get('name') or str(gid['id']),
                age=card.get('age'), kind=card.get('kind') or '',
                level=(player.get('level_info') or {}).get('level', 1),
                learned=player_skills.learned_skill_ids(player),
                games=int(player.get('games') or 0), goals=int(player.get('goal') or 0),
                assists=int(player.get('assist') or 0))


def handle(conn, command, req, *, match_running=False):
    if command == 'info':
        return dict(version=project_info.VERSION, author=project_info.AUTHOR,
                    project=project_info.PROJECT_URL, provenance=project_info.PROVENANCE,
                    training_mode=training_settings.get(conn), news=announcements.news(conn))
    if command == 'read_news':
        announcements.mark_read(conn, req)
        return {'saved': True}
    if command == 'players':
        query = str(req.get('query') or '').strip().casefold()[:100]
        scope = req.get('scope', 'active')
        offset = max(0, min(int(req.get('offset', 0)), 100000))
        rows = conn.execute('select g.id,g.serial,w.id from gameplayers g left join warehouse w '
                            'on g.id=w.id and g.serial=w.serial order by (w.id is not null),g.id,g.serial')
        selected = []
        matched = 0
        more = False
        for pid, serial, warehouse_id in rows:
            if scope == 'active' and warehouse_id is not None:
                continue
            if scope == 'warehouse' and warehouse_id is None:
                continue
            card = player_parameters.card_details(pid) or {}
            if query and query not in str(card.get('name', '')).casefold() and query not in str(pid):
                continue
            if matched < offset:
                matched += 1
                continue
            if len(selected) == 30:
                more = True
                break
            player = store.player_by_key(conn, pid, serial)
            item = _summary(player)
            item['warehouse'] = warehouse_id is not None
            selected.append(item)
        return dict(players=selected, offset=offset, next_offset=offset + len(selected), more=more)
    if command == 'player':
        player = _player(conn, req)
        return dict(player=_summary(player), skills=player_skills.catalog(player))
    if command == 'set_skill':
        # In-match changes cannot refresh the native engine safely at half-time.
        # A persisted unfinished match is not proof that this process has a
        # running native match. GateLogic supplies its explicit session flag.
        if match_running:
            raise ValueError('请先结束当前比赛，再调整附加技能')
        player = _player(conn, req)
        sid = _integer(req.get('skill'), '技能ID')
        slot = _integer(req.get('slot'), '技能槽位', 1)
        if sid not in player_skills.SELECTABLE_SKILL_IDS:
            raise ValueError('当前版本不支持该技能')
        # Learned/innate checks belong inside the transaction, after receipt
        # replay. Rejecting an already learned ID here made a successful
        # request's transport retry look like a failure before deduplication.
        if slot < 1 or slot > min(5, len(player_skills.learned_skill_ids(player)) + 1):
            raise ValueError('请选择已有槽位或下一个空槽位')
        request = dict(msgid='CMD_USE_MYCLUB_ITEM', user_id=int(store.get_meta(conn, 'user_id', '0')),
                       rqid='panel-' + str(req.get('request_id') or uuid.uuid4()),
                       gameplayer_id_list=[dict(player['gameplayer_id'])], training_index=slot - 1,
                       ability_id=sid,
                       item_list=[dict(item=dict(item_type='SKILL_TRAINING_SELECT', grade=0, effect=sid), present_num=1)])
        reply = player_operations.transact(conn, 'skill', request,
                                            lambda: player_operations.skill(conn, request))
        if (reply.get('skill_training_result') or {}).get('is_success') != 'YES':
            raise ValueError('技能未保存，请返回球员列表检查后重试')
        return dict(saved=True, player=_summary(_player(conn, req)),
                    message='技能已保存。返回游戏后重新打开球员详情或阵容页面以刷新显示。')
    if command == 'reset_progression':
        if match_running:
            raise ValueError('请先结束当前比赛，再重置加点')
        player = _player(conn, req)
        result = player_operations.reset_progression(conn, player['gameplayer_id'],
            request_id='panel-' + str(req.get('request_id') or uuid.uuid4()),
            user_id=int(store.get_meta(conn, 'user_id', '0')))
        return dict(saved=True, progression=result, player=_summary(_player(conn, req)),
                    message='加点已重置，当前手动/自动模式保持不变。重新打开游戏加点页面后可分配。')
    if command == 'training_mode':
        result = training_settings.handle(conn, {'msgid': 'CMD_SET_MYCLUB_CAREERPLAN_ALLOCATE_TYPE',
                                                'rqid': 'panel-' + str(uuid.uuid4()),
                                                'careerplan_allocate_type': req.get('mode')})
        if result.get('result') != 'NOERR':
            raise ValueError('加点模式无效')
        return dict(mode=training_settings.get(conn))
    if command == 'diagnostics':
        rows = conn.execute('select at,kind,detail from events where kind like ? or kind like ? '
                            'or kind in (?,?,?) order by id desc limit 25',
                            ('match_%', 'efootxi_%', 'start_game', 'player_data_miss', 'use_agent')).fetchall()
        return dict(version=project_info.VERSION, events=[dict(at=a, kind=k, detail=d) for a,k,d in rows])
    raise ValueError('不支持的设置操作')


def dispatch(command, request_json='{}'):
    try:
        import embedded_gateway
        logic = getattr(embedded_gateway, '_logic', None)
        if logic is None:
            raise ValueError('本地游戏服务尚未就绪，请进入游戏后重试')
        request = json.loads(str(request_json))
        if not isinstance(request, dict):
            raise ValueError('无效设置请求')
        result = handle(logic.conn, str(command), request,
                        match_running=bool(getattr(logic, 'session_match_running', False)))
        return json.dumps(dict(ok=True, data=result), ensure_ascii=False)
    except Exception as exc:
        return json.dumps(dict(ok=False, error=str(exc)), ensure_ascii=False)
