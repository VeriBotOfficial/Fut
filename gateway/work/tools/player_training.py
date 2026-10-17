"""Offline level-training state and replenished training inventory (E24).

The captured EXP table is the default curve, NOT a verified universal curve.
Native cards with a positive current EXP requirement anchor its scale. The
unobserved >35 tail uses a documented offline continuation of the last delta.
Client preview and server execution always use the same cached per-card curve.
"""
from __future__ import annotations

import copy

import pes_tables

TRAINING_COUNT = 999
INVENTORY_COUNT = 99999
LEVEL_ITEM_EFFECTS = {1: 100, 2: 1000, 3: 4000, 4: 10000, 99: 4000}
TRAINING_TYPES = {
    'LEVEL_TRAINING', 'SKILL_TRAINING', 'POSITION_TRAINING',
    'POSITION_TRAINING_SELECT',
    'ABILITY_TRAINING_RANDOM', 'ABILITY_TRAINING_SELECT',
    'SKILL_TRAINING_CATEGORY', 'SKILL_TRAINING_SELECT',
}


def integer(value, default=0):
    return value if type(value) is int else default


def inventory(items, *, flat: bool = False) -> list[dict]:
    """Replenish training cards only; never touch currencies, rewards or tickets."""
    out = copy.deepcopy(items) if isinstance(items, list) else []
    flat = flat or bool(out and isinstance(out[0], dict) and 'item' not in out[0] and 'item_type' in out[0])
    for entry in out:
        if isinstance(entry, dict) and (entry.get('item') or entry).get('item_type') in TRAINING_TYPES:
            entry['count' if flat else 'inventory_num'] = INVENTORY_COUNT
    defaults = [dict(item_type='LEVEL_TRAINING', grade=g, effect=e) for g, e in LEVEL_ITEM_EFFECTS.items()]
    defaults += [dict(item_type=k, grade=0, effect=0) for k in sorted(TRAINING_TYPES - {'LEVEL_TRAINING'})]
    keys = {(e.get('item') or e).get('item_type', '') + ':' + str((e.get('item') or e).get('grade', 0))
            for e in out if isinstance(e, dict)}
    for item in defaults:
        if item['item_type'] + ':' + str(item['grade']) not in keys:
            if flat:
                entry = copy.deepcopy(out[0]) if out else {'pes_point_price': 0}
                entry.update(item, count=INVENTORY_COUNT)
                for key, value in [('item_info', item['item_type']), ('use_item', item['item_type']),
                                   ('item_grade', item['grade']), ('item_effect', item['effect'])]:
                    if key in entry:
                        entry[key] = value
                out.append(entry)
            else:
                out.append({'item': item, 'inventory_num': INVENTORY_COUNT})
    return out


def lock_reply_inventory(resp: dict, mid: str) -> None:
    inventory_commands = {'CMD_USE_MYCLUB_ITEM', 'CMD_GET_GPSHOP_ITEM_LIST'}
    if mid in inventory_commands or isinstance(resp.get('item_list'), list):
        items = resp.get('item_list')
        # Rewards with present_num are not the account's inventory.
        if mid in inventory_commands or not items or any('inventory_num' in e for e in items if isinstance(e, dict)):
            resp['item_list'] = inventory(items)
    entry = resp.get('entry_info')
    if isinstance(entry, dict):
        entry['item_list'] = inventory(entry.get('item_list'), flat=mid == 'CMD_SET_MYCLUB_ENTRY_INFO')
        if 'item_list_num' in entry:
            entry['item_list_num'] = len(entry['item_list'])


def level_curve(player: dict) -> list[int]:
    """Index is destination level; value is the XP cost from the previous one."""
    li = player.get('level_info') or {}
    level = max(1, integer(li.get('level'), 1))
    cap = max(level, integer(li.get('level_limit'), level))
    if cap > 1000:
        raise ValueError('invalid level limit')
    saved = player.get('_level_training_curve')
    if (isinstance(saved, list) and len(saved) == cap + 1
            and all(type(v) is int and v > 0 for v in saved[2:])):
        return list(saved)
    costs = [0, 0] + [exp for _, exp in pes_tables.EXPERIENCE_LIST]
    while len(costs) <= cap:
        delta = (costs[-1] - costs[-2]) * 105 // 100
        costs.append(costs[-1] + max(1, delta))
    costs = costs[:cap+1]
    # Preserve a native card's observed remaining-XP boundary exactly.
    remaining = integer(li.get('exp_to_next_level'))
    current = max(0, integer(li.get('exp_current_level')))
    if level < cap and remaining > 0:
        observed = current + remaining
        denominator = costs[level+1]
        costs = [0, 0] + [max(1, (x * observed + denominator // 2) // denominator) for x in costs[2:]]
    return costs


def experience_table(player: dict) -> list[dict]:
    costs = level_curve(player)
    return [{'level': lv, 'experience': costs[lv]} for lv in range(2, len(costs))] + [
        {'level': len(costs), 'experience': 0}]


def level_info_view(player: dict, costs: list[int] | None = None) -> dict:
    li = copy.deepcopy(player.get('level_info') or {})
    level = max(1, integer(li.get('level'), 1))
    cap = max(level, integer(li.get('level_limit'), level))
    li['level'], li['level_limit'] = level, cap
    li['customize_point'] = max(0, integer(li.get('customize_point')))
    li['exp_total'] = max(0, integer(li.get('exp_total')))
    if level >= cap:
        li['exp_current_level'] = li['exp_to_next_level'] = 0
    else:
        costs = costs if costs is not None else level_curve(player)
        current = max(0, min(integer(li.get('exp_current_level')), costs[level+1]-1))
        li['exp_current_level'] = current
        li['exp_to_next_level'] = costs[level+1] - current
    return li


def item_experience(items) -> int:
    """Read the captured request field present_num; distrust claimed effects."""
    if not isinstance(items, list) or not items:
        raise ValueError('missing training items')
    total = 0
    counts = {}
    for row in items:
        if not isinstance(row, dict) or not isinstance(row.get('item'), dict):
            raise ValueError('invalid item')
        item, count = row['item'], row.get('present_num')
        grade = item.get('grade')
        if (item.get('item_type') != 'LEVEL_TRAINING' or type(grade) is not int
                or grade not in LEVEL_ITEM_EFFECTS or type(count) is not int or count <= 0):
            raise ValueError('invalid training quantity or grade')
        if type(item.get('effect')) is not int or item['effect'] != LEVEL_ITEM_EFFECTS[grade]:
            raise ValueError('training effect mismatch')
        counts[grade] = counts.get(grade, 0) + count
        if counts[grade] > TRAINING_COUNT:
            raise ValueError('training quantity exceeds inventory')
        total += LEVEL_ITEM_EFFECTS[grade] * count
    return total


def train(player: dict, experience: int) -> tuple[dict, dict, dict, int]:
    if type(experience) is not int or experience <= 0:
        raise ValueError('invalid experience')
    out = copy.deepcopy(player)
    costs = level_curve(out)
    before = level_info_view(out, costs)
    after = copy.deepcopy(before)
    level, cap, current = before['level'], before['level_limit'], before['exp_current_level']
    left = experience
    while level < cap and left:
        used = min(left, costs[level+1] - current)
        current += used
        left -= used
        if current == costs[level+1]:
            level += 1
            current = 0
    applied = experience - left
    after.update(level=level, exp_total=before['exp_total'] + applied,
                 exp_current_level=current, exp_to_next_level=costs[level+1]-current if level < cap else 0,
                 customize_point=before['customize_point'] + 2 * (level-before['level']))
    out['level_info'] = after
    out['_level_training_curve'] = costs
    return out, before, after, applied


def restore_use_item_experience(response: dict, identity: dict) -> bool:
    """Repair our saved training reply from its own post-training snapshot.

    Never train again or substitute the player's later database state. The
    command decoder needs the six-field level-info object at root experience;
    old replies saved it only inside the otherwise unused match EXP list.
    """
    rows = response.get('gameplayer_exp_list')
    if (response.get('result') != 'NOERR' or not isinstance(rows, list) or len(rows) != 1
            or not isinstance(rows[0], dict) or rows[0].get('gameplayer_id') != identity):
        return False
    after = rows[0].get('exp_info_postmatch')
    fields = ('level', 'level_limit', 'exp_total', 'exp_current_level',
              'exp_to_next_level', 'customize_point')
    if (not isinstance(after, dict) or any(type(after.get(key)) is not int
            or not 0 <= after[key] < 2**31 for key in fields)
            or not 1 <= after['level'] <= after['level_limit'] <= 1000):
        return False
    if response.get('experience') == after:
        return False
    response['experience'] = copy.deepcopy(after)
    return True


def ensure_progression_ledger(conn, player: dict) -> bool:
    """Restore the omitted journal of an exactly recognized developed snapshot.

    Pack catalogue cards carry max-level stats and their original base, but
    the old acquisition copied only JSON, losing the separate careerplan rows.
    Reconstruct only when the bundled recommendation reproduces every ability
    and unspent point exactly. Unknown manual builds are never guessed, and an
    existing journal is never replaced. Caller owns the enclosing transaction.
    """
    import club_store as store
    import recommended_allocation
    from player_operations import ABILITY_INDICES
    pid, serial = store.player_key(player)
    if conn.execute('select 1 from careerplan where id=? and serial=? limit 1', (pid, serial)).fetchone():
        return False
    base = player.get('_careerplan_base_parameters')
    params = player.get('parameters')
    info = player.get('level_info') or {}
    level = info.get('level')
    points = info.get('customize_point')
    if (not isinstance(base, list) or len(base) != 147
            or not isinstance(params, list) or len(params) != 147
            or type(level) is not int or not 1 < level <= 1000
            or type(points) is not int or points < 0
            or type(base[0]) is not int or not 0 <= base[0] <= 12
            or any(type(base[i]) is not int or not 0 <= base[i] <= 127 for i in ABILITY_INDICES)):
        return False
    budget = 2 * (level - 1)
    expected, allocation = recommended_allocation.apply_to_parameters(base, base[0], budget)
    if (not allocation or points != budget - sum(allocation.values())
            or any(params[i] != expected[i] for i in ABILITY_INDICES)):
        return False
    for group, spent in allocation.items():
        if spent:
            store.record_careerplan(conn, pid, serial, group, spent)
    store.log_event(conn, 'efootxi_progression_ledger_recovered',
                    {'id': pid, 'serial': serial, 'spent': sum(allocation.values()),
                     'reason': 'exact bundled recommendation and saved base match'})
    return True
