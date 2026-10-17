"""Persisted offline resource grant and own-account presentation (E41)."""
import copy
import json
import club_store as store
import player_training

RESOURCE_COUNT = 99999
VIEIRA_ID = 17592186179005


def item_key(item):
    return ':'.join(str(item.get(k, 0)) for k in ('item_type', 'grade', 'effect'))


def configure(conn, login):
    """Explicit persistent fixed-balance policy requested by the player."""
    rows = player_training.inventory((login.get('entry_info') or {}).get('item_list'))
    saved = {item_key(r['item']): dict(item=r['item'], inventory_num=RESOURCE_COUNT) for r in rows}
    store.set_meta(conn, 'account_inventory', json.dumps(list(saved.values())))
    store.set_meta(conn, 'account_resource_fixed', str(RESOURCE_COUNT))
    for k in ('gp', 'coin', 'season_point'):
        store.set_meta(conn, k, str(RESOURCE_COUNT))
    store.set_meta(conn, 'user_name', 'efootxi')
    avatar = copy.deepcopy((login.get('pes_user_info') or {}).get('avatar') or {})
    avatar.update(is_already_set='YES', player_id=VIEIRA_ID, special_id=0,
                  background_id=1, special_id_background=0, is_enable_player='YES',
                  is_enable_background='YES', material_key_player='MATERIAL_KEY_NONE',
                  material_key_background='MATERIAL_KEY_NONE')
    store.set_meta(conn, 'avatar', json.dumps(avatar))


def inventory(conn, rows, flat=False):
    saved = json.loads(store.get_meta(conn, 'account_inventory', '[]'))
    if not saved:
        return rows
    flat = flat or bool(rows and 'item' not in rows[0] and 'item_type' in rows[0])
    out = copy.deepcopy(rows or [])
    known = {item_key(r['item']): r for r in saved}
    seen = set()
    for r in out:
        item = r.get('item') or r
        key = item_key(item)
        seen.add(key)
        # Only inventory counters are touched, never present_num or prices.
        if key in known:
            r['count' if flat else 'inventory_num'] = known[key]['inventory_num']
    for key, r in known.items():
        if key in seen:
            continue
        if flat:
            out.append(dict(r['item'], count=r['inventory_num'], pes_point_price=0))
        else:
            out.append(copy.deepcopy(r))
    return out


def apply(conn, resp, mid, req):
    entry = resp.get('entry_info')
    if isinstance(entry, dict):
        for field, meta in [('gp','gp'), ('coin','coin'), ('coin_value','coin'), ('myclub_coin','coin')]:
            if field in entry:
                entry[field] = int(store.get_meta(conn, meta, '0'))
        if 'user_name' in entry:
            entry['user_name'] = store.get_meta(conn, 'user_name', '')
        if 'item_list' in entry:
            entry['item_list'] = inventory(conn, entry['item_list'], mid == 'CMD_SET_MYCLUB_ENTRY_INFO')
            if 'item_list_num' in entry:
                entry['item_list_num'] = len(entry['item_list'])
    if mid in ('CMD_USE_MYCLUB_ITEM', 'CMD_GET_GPSHOP_ITEM_LIST'):
        resp['item_list'] = inventory(conn, resp.get('item_list', []))
    own = resp.get('pes_user_info') if mid == 'CMD_LOGIN' else None
    if mid == 'CMD_GET_MYCLUB_USER_DETAIL':
        target = req.get('target_user_id', req.get('user_id'))
        if target is None or str(target) == store.get_meta(conn, 'user_id', ''):
            own = resp.get('user_detail')
    if isinstance(own, dict):
        own['user_name'] = store.get_meta(conn, 'user_name', own.get('user_name',''))
        saved = store.get_meta(conn, 'avatar', '')
        if saved:
            own['avatar'] = json.loads(saved)
    if mid == 'CMD_LOGIN' and store.get_meta(conn, 'avatar', ''):
        pid = json.loads(store.get_meta(conn, 'avatar'))['player_id']
        icons = resp.setdefault('avatar_icon_info_id_list', [])
        if pid not in icons:
            icons.append(pid)
    if 'myleague_point' in resp and store.get_meta(conn, 'account_inventory', ''):
        resp['myleague_point'] = store.season_points(conn)


def save_avatar(conn, req, resp):
    old = json.loads(store.get_meta(conn, 'avatar', '{}'))
    supplied = req.get('avatar') or req.get('avatar_info') or req
    keys = ('player_id','special_id','background_id','special_id_background')
    values = {k:supplied[k] for k in keys if k in supplied}
    if not values or any(type(v) is not int or v < 0 for v in values.values()):
        return {'msgid':'CMD_SET_AVATAR_INFO','rqid':req.get('rqid'),'result':'ERR_INVALID_PARAM'}
    old.update(values, is_already_set='YES')
    store.set_meta(conn,'avatar',json.dumps(old));conn.commit()
    if 'avatar' in resp:resp['avatar']=copy.deepcopy(old)
    resp['result']='NOERR'
    return resp
