"""Player detail mutations: exact instances, native reply shapes, atomic retries."""
from __future__ import annotations

import hashlib
import copy
import json
import random
from fractions import Fraction

import club_store as store
import pes_tables
import player_skills

# Server-owned ability indices, established by PARAMETER_LAYOUT / assembler.
ABILITY_INDICES = set(range(14, 33)) | set(range(34, 41))

# Group membership follows pes_tables.CAREERPLAN_PARAM_LIST; the indices follow
# PARAMETER_LAYOUT.md and assemble_gate_player.ABILITY_AT (native 147 layout).
# GK2/GK3 pairing corrected 2026-10-15 against the user's own trained GK card
# (88045755829367: gkClearing[30] + gkReach[32] rose together by 9, gkCatching[29]
# + gkReflexes[31] rose together by 9) and eFootBase's game simulator, whose group
# table is {gk1: gkAwareness+jumping, gk2: parrying+reach, gk3: catching+reflexes}.
# The old pairing was (30,31)/(29,32).
AUTO_GROUP_INDICES = {
    'SHOOT': (20, 27, 28), 'PASS': (21, 22), 'DRIBLE': (17, 18, 19),
    'QUICKNESS': (14, 38, 36), 'STRENGTH': (37, 34, 40),
    'AERIAL': (23, 39, 35), 'DEFENCE': (15, 24, 25, 26),
    'GK1': (16, 39), 'GK2': (30, 32), 'GK3': (29, 31),
}
# Offline recovery policy, NOT claimed to reproduce the client's official OVR
# optimizer. Position order: GK CB LB RB DMF CMF LMF RMF AMF LWF RWF SS CF
# (main-position code order corrected 2026-10-15: 1=CB, 2=LB).
# Healthy native recommendations still use the existing allocation path.
AUTO_POSITION_WEIGHTS = (
    (0, 0, 0, 0, 1, 2, 0, 6, 6, 6),
    (0, 1, 0, 1, 3, 5, 7, 0, 0, 0),
    (0, 3, 2, 2, 5, 2, 5, 0, 0, 0),
    (0, 3, 2, 2, 5, 2, 5, 0, 0, 0),
    (1, 4, 2, 2, 4, 2, 6, 0, 0, 0),
    (2, 6, 4, 3, 4, 1, 3, 0, 0, 0),
    (2, 4, 5, 4, 5, 1, 1, 0, 0, 0),
    (2, 4, 5, 4, 5, 1, 1, 0, 0, 0),
    (3, 5, 6, 5, 3, 1, 0, 0, 0, 0),
    (4, 3, 6, 6, 5, 1, 0, 0, 0, 0),
    (4, 3, 6, 6, 5, 1, 0, 0, 0, 0),
    (6, 3, 5, 6, 4, 1, 0, 0, 0, 0),
    (7, 1, 3, 5, 4, 4, 0, 0, 0, 0),
)


# Wire/layout ceiling for one ability. The parameter slots are 7-bit and the
# official server itself ships owned cards above 99 (captured 2026-10-14,
# 094_gate_CMD_GET_MYCLUB_PLAYER_DATA_resp: goalkeeping 105, dribbling 105,
# header 102, catching 102), so allocation must not stop at 99. An earlier
# policy capped at 99 after one broken captured auto-recommendation saturated
# there; `career()` has always validated the full 0..127 range.
ABILITY_CEILING = 127


def training_auto_allocation(player, budget):
    """Build a position-weighted, exact-budget recommendation from saved base.

    Each category upgrade raises its abilities by one; its price increases
    every four upgrades. Greedy marginal benefit uses integer fractions so
    replays produce identical allocations. Shared jump contributions add up.
    Unspendable points stay available, and no base stat above the ceiling is
    lowered.
    """
    current = list(store.wire_player(player).get('parameters') or [])
    base = list(player.get('_careerplan_base_parameters') or current)
    if (type(budget) is not int or budget < 0 or len(current) != 147
            or len(base) != len(current) or type(base[0]) is not int
            or not 0 <= base[0] < len(AUTO_POSITION_WEIGHTS)
            or any(type(base[i]) is not int or not 0 <= base[i] <= ABILITY_CEILING
                   for i in ABILITY_INDICES)):
        raise ValueError('invalid automatic allocation base or budget')
    params = list(current)
    for index in ABILITY_INDICES:
        params[index] = base[index]
    groups = list(AUTO_GROUP_INDICES)
    counts = dict.fromkeys(groups, 0)
    spent = dict.fromkeys(groups, 0)
    remaining = budget
    weights = AUTO_POSITION_WEIGHTS[base[0]]
    while remaining:
        options = []
        for group, weight in zip(groups, weights):
            indices = AUTO_GROUP_INDICES[group]
            change = [i for i in indices if params[i] < ABILITY_CEILING]
            price = counts[group] // 4 + 1
            if weight and change and price <= remaining:
                score = Fraction(weight * len(change), len(indices) * price * (counts[group] + 1))
                options.append((score, -groups.index(group), group, change, price))
        if not options:
            break
        _, _, group, change, price = max(options)
        for index in change:
            params[index] += 1
        counts[group] += 1
        spent[group] += price
        remaining -= price
    return {g: value for g, value in spent.items() if value}, params


def _allocation_state_hash(wire):
    state = {'parameters': wire.get('parameters'), 'level_info': wire.get('level_info')}
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


def _latest_level_training_receipt(conn, req, key):
    if not conn.execute("select 1 from sqlite_master where type='table' "
                        "and name='level_training_receipts'").fetchone():
        return None
    for (raw,) in conn.execute('select response_json from level_training_receipts '
                              'where user_id=? order by rowid desc', (str(req.get('user_id', 0)),)):
        saved = json.loads(raw)
        trained = (saved.get('gameplayer_list') or [{}])[0]
        if store.player_key(trained) == key:
            return saved
    return None


def transact(conn, kind, req, apply):
    conn.execute('create table if not exists player_operation_receipts('
                 'kind text,user_id text,rqid text,fingerprint text,response text,'
                 'primary key(kind,user_id,rqid))')
    receipt_kind = kind
    # The native request counter can restart after a new login. A later skill
    # training must not be acknowledged by a receipt from an earlier session.
    # Keep the logical kind separate so same-session retries still reconcile
    # the returned player with the current saved, ordered skill slots.
    if kind in ('skill', 'delete_skill') and req.get('session_id') is not None:
        session = json.dumps(req['session_id'], sort_keys=True).encode()
        receipt_kind += '@' + hashlib.sha256(session).hexdigest()
    key = (receipt_kind, str(req.get('user_id', 0)), str(req.get('rqid')))
    payload = {k: v for k, v in req.items() if k not in ('rqid','session_id','s_keyword')}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    with conn:
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        if req.get('rqid') is not None:
            row = conn.execute('select fingerprint,response from player_operation_receipts '
                               'where kind=? and user_id=? and rqid=?', key).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError('request id reused for different operation')
                saved = json.loads(row[1])
                return _current_skill_receipt(conn, saved) if kind == 'skill' else saved
        result = apply()
        if req.get('rqid') is not None:
            conn.execute('insert into player_operation_receipts values(?,?,?,?,?)',
                         (*key, fingerprint, json.dumps(result, ensure_ascii=False)))
        return result


def _current_skill_receipt(conn, saved):
    """A retry must neither reroll nor overwrite the UI with an old snapshot.

    The database has always survived a replay, but returning the earlier player
    object after another slot was edited could make the client show old skills
    until the next refresh. Reconcile the response against the same exact saved
    instance and its current ordered slots.
    """
    result = store.player_identity.canonical_request(conn, copy.deepcopy(saved))
    for index, old in enumerate(result.get('gameplayer_list') or []):
        pid, serial = store.player_key(old)
        current = store.player_by_key(conn, pid, serial)
        if current is None:
            raise ValueError('skill retry refers to a player no longer owned')
        old_slots = player_skills.learned_skill_ids(old)
        current_slots = player_skills.learned_skill_ids(current)
        trained = (result.get('skill_training_result') or {}).get('additional_skill_id')
        if trained in old_slots:
            slot = old_slots.index(trained)
            present = slot < len(current_slots) and current_slots[slot] != player_skills.EMPTY_SKILL_ID
            result['skill_training_result'] = {
                'additional_skill_id': current_slots[slot] if present else player_skills.EMPTY_SKILL_ID,
                'is_success': 'YES' if present else 'NO',
            }
        result['gameplayer_list'][index] = store.wire_player(current, 'list')
    return result


def exact_player(conn, gid):
    if not isinstance(gid, dict) or type(gid.get('id')) is not int or type(gid.get('serial')) is not int:
        raise ValueError('exact player identity required')
    player = store.player_by_key(conn, gid['id'], gid['serial'])
    if player is None:
        raise ValueError('player not owned')
    return player


def persist(conn, player):
    pid, serial = store.player_key(player)
    conn.execute('update gameplayers set json=? where id=? and serial=?',
                 (json.dumps(player, ensure_ascii=False), pid, serial))


def skill(conn, req):
    ids = req.get('gameplayer_id_list')
    items = req.get('item_list')
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(items, list) or len(items) != 1:
        raise ValueError('one player and one skill item required')
    player = exact_player(conn, ids[0])
    item = items[0].get('item') or {}
    count = items[0].get('present_num')
    if type(count) is not int or not 1 <= count <= 999:
        raise ValueError('invalid skill item quantity')
    skills = player_skills.learned_skill_ids(player)
    index = req.get('training_index', 0)
    # Verified native chain: row click 0x35dd1c8 -> context 0x74521f8 ->
    # request 0x7943be8 -> response application 0x794447c. Every step uses the
    # same zero-based slot, 0..4. Treating zero as append and subtracting one
    # from the other slots made the native screen and saved player disagree.
    if type(index) is not int or not 0 <= index < player_skills.MAX_ADDITIONAL_SKILLS:
        raise ValueError('invalid skill slot')
    slot = index
    innate = player_skills.innate_skill_ids(store.wire_player(player).get('parameters') or [])
    # Replacing a slot must award a new skill, including when that slot is the
    # only copy of its skill. Innate skills also never consume training slots.
    excluded = innate | set(skills)
    if item.get('item_type') == 'SKILL_TRAINING_SELECT':
        ability = req.get('ability_id')
        if (type(ability) is not int or ability not in player_skills.SELECTABLE_SKILL_IDS
                or type(item.get('effect', 0)) is not int or item.get('effect', 0) not in (0, ability)):
            raise ValueError('invalid selected skill')
    elif item.get('item_type') in ('SKILL_TRAINING', 'SKILL_TRAINING_CATEGORY'):
        pool = [s for s in pes_tables.SKILLCARD_ID_LIST
                if s in player_skills.RANDOM_SPECIAL_SKILL_IDS
                and s in player_skills.SELECTABLE_SKILL_IDS]
        if item.get('item_type') == 'SKILL_TRAINING_CATEGORY':
            match = player_skills.category_skill_ids(req.get('skill_training_category_type'), item)
            pool = [s for s in pool if s in match]
        pool = [s for s in pool if s not in excluded]
        if not pool:
            raise ValueError('no available additional skill')
        ability = random.SystemRandom().choice(pool)
    else:
        raise ValueError('unsupported skill training item')
    if slot < len(skills) and skills[slot] == ability:
        return {'skill_training_result': {'additional_skill_id': ability, 'is_success': 'YES'},
                'gameplayer_list': [store.wire_player(player, 'list')]}
    if ability in excluded:
        raise ValueError('skill already learned or innate')
    skills.extend([player_skills.EMPTY_SKILL_ID] * max(0, slot + 1 - len(skills)))
    skills[slot] = ability
    player['additional_skill_id'] = skills
    player.pop('additional_skill', None)  # avoid a stale alternate snapshot field
    player['is_skill_added'] = 'YES' if skills else 'NO'
    player['_efootxi_skill_revision'] = int(player.get('_efootxi_skill_revision') or 0) + 1
    persist(conn, player)
    store.log_event(conn, 'skill_training', {'player':ids[0],'slot':slot,'skill':ability})
    return {'skill_training_result': {'additional_skill_id':ability,'is_success':'YES'},
            'gameplayer_list':[store.wire_player(player, 'list')]}


def delete_skill(conn, req):
    """Delete the native zero-based slot using the client's exact compaction.

    Producer 0x78c2bac sends gameplayer_id:{id,serial} and the uint32 field
    delete_skill_index. Its reply handler 0x78c29d0 calls 0x7a88288, which shifts
    the following slots left and writes empty marker 74 into the fifth slot.
    Keep the other skills and their relative order, matching the visible UI.
    """
    player = exact_player(conn, req.get('gameplayer_id'))
    index = req.get('delete_skill_index')
    if type(index) is not int or not 0 <= index < player_skills.MAX_ADDITIONAL_SKILLS:
        raise ValueError('invalid learned skill deletion index')
    skills = player_skills.learned_skill_ids(player)
    if index >= len(skills):
        return {}
    removed = skills[index]
    skills.extend([player_skills.EMPTY_SKILL_ID] * (player_skills.MAX_ADDITIONAL_SKILLS - len(skills)))
    skills.pop(index)
    skills.append(player_skills.EMPTY_SKILL_ID)
    player['additional_skill_id'] = skills
    player.pop('additional_skill', None)
    player['is_skill_added'] = 'YES' if any(s != player_skills.EMPTY_SKILL_ID for s in skills) else 'NO'
    player['_efootxi_skill_revision'] = int(player.get('_efootxi_skill_revision') or 0) + 1
    persist(conn, player)
    store.log_event(conn, 'skill_deleted', {
        'player': player['gameplayer_id'], 'slot': index, 'skill': removed})
    return {}


# Native position enum order inside parameters[1:14].
POSITION_ENUMS = ('CF','ST','LWG','RWG','OMF','DMF','CMF','LMF','RMF','CB','LSB','RSB','GK')

# The client's position training arrives as POSITION_TRAINING_SELECT with the
# chosen position in `position_id` (captured 2026-10-15, rqid 3167156823); the
# plain POSITION_TRAINING card carries no target and keeps the offline scan.
POSITION_ITEM_TYPES = ('POSITION_TRAINING', 'POSITION_TRAINING_SELECT')


def position(conn, req):
    ids, items = req.get('gameplayer_id_list'), req.get('item_list')
    if not isinstance(ids, list) or len(ids) != 1 or not isinstance(items, list) or len(items) != 1:
        raise ValueError('one player and one position item required')
    row = items[0]
    item = row.get('item') or {}
    count = row.get('present_num')
    # The captured SELECT request reports the item's stack size (100), so the
    # quantity is only checked for being a positive count, not for being one.
    if item.get('item_type') not in POSITION_ITEM_TYPES or type(count) is not int or count < 1:
        raise ValueError('one position training card required')
    slot = req.get('training_index', 0)
    if type(slot) is not int or slot not in (0,1):
        raise ValueError('invalid position slot')
    player = exact_player(conn, ids[0])
    params = list(store.wire_player(player).get('parameters') or [])
    if len(params) < 14 or params[0] == 0:
        raise ValueError('goalkeeper or invalid player')
    base = list(player.get('_position_training_base_aptitudes') or params[1:14])
    slots = list(player.get('additional_position') or [])
    if len(base) != 13 or len(slots) > 2 or any(p not in POSITION_ENUMS[:12] and p != '' for p in slots):
        raise ValueError('invalid position state')
    # Fixed offline selection, not a claim to reproduce the official RNG.
    # Replacement does not accumulate permanent aptitude outside the two slots,
    # so the scan skips every position the card already holds.
    excluded = set(slots)
    requested = req.get('position_id')
    if requested in (None, ''):
        order = list(range(6,12))+list(range(6)) if slot == 1 else list(range(12))
        choices = [i for i in order if base[i] < 2 and POSITION_ENUMS[i] not in excluded]
        if not choices:
            raise ValueError('no available additional position')
        learned = POSITION_ENUMS[choices[0]]
    else:
        # The client named the position its screen showed; teach exactly that.
        # Re-teaching the position already in this slot is a no-op, not an error
        # (the client's list is driven by the served aptitudes).
        picked = [p for index, p in enumerate(slots) if p and index != slot]
        if (requested not in POSITION_ENUMS[:12] or base[POSITION_ENUMS.index(requested)] >= 2
                or requested in picked):
            raise ValueError('requested position is not trainable')
        learned = requested
    while len(slots) <= slot:
        slots.append('')
    slots[slot] = learned
    params[1:14] = base
    for pos in slots:
        if pos:
            params[1 + POSITION_ENUMS.index(pos)] = 2
    player.update(parameters=params, additional_position=slots, _position_training_base_aptitudes=base)
    persist(conn, player)
    store.log_event(conn, 'position_training', {'player':ids[0],'slot':slot,'position':learned,
                                                'item_type':item.get('item_type')})
    # Native CMD_USE_MYCLUB_ITEM replies carry achievement_notification next to
    # the training result (captured 2026-10-15); the client reads both.
    return {'position_training_result': {'additional_position':learned,'is_success':'YES'},
            'achievement_notification': [],
            'gameplayer_list':[store.wire_player(player, 'list')]}


def _training_continue_overflow_receipt(conn, req, use, player):
    """Recognize a broken native auto preview after a committed level training.

    The captured Continue request contains a uint32-wrapped negative point
    together with over-budget positive points and abilities saturated at 99.
    It is not a valid allocation or a general-purpose sentinel. A matching
    training receipt lets us rebuild the recommendation from the saved base
    and earned budget instead of trusting the malformed client abilities.
    Manual requests and auto requests after any subsequent player change keep
    their existing strict validation. XP, earned points and boosters stay intact.
    """
    if (req.get('use_auto_allocate_point') != 'YES'
            or req.get('careerplan_allocate_type') != 'AUTO'):
        return False
    rows = use.get('use_list')
    if not isinstance(rows, list) or len(rows) != len(pes_tables.CAREERPLAN_PARAM_LIST):
        return False
    groups, overflow = set(), False
    for row in rows:
        if not isinstance(row, dict):
            return False
        group, points = row.get('parameter_type'), row.get('point')
        if (row.get('type') != 'PARAMETER' or group not in pes_tables.CAREERPLAN_PARAM_LIST
                or group in groups or type(points) is not int or not 0 <= points <= 0xffffffff):
            return False
        groups.add(group)
        overflow |= points >= 0x80000000
    if not overflow:
        return False
    wire = store.wire_player(player)
    current, candidate = wire.get('parameters'), use.get('parameters')
    if (not isinstance(current, list) or len(current) != 147
            or not isinstance(candidate, list) or len(candidate) != len(current)
            or any(type(candidate[i]) is not int or not 0 <= candidate[i] <= 127
                   for i in ABILITY_INDICES)):
        return False
    key = store.player_key(player)
    saved = _latest_level_training_receipt(conn, req, key)
    if saved:
        trained = (saved.get('gameplayer_list') or [{}])[0]
        # Match a direct Continue or our already persisted auto result. The
        # marker is tied to both training and current state, so a later manual
        # save invalidates it; transport retries never spend points twice.
        marker = player.get('_training_auto_allocation') or {}
        if marker.get('invalidated') and marker.get('training_rqid') == saved.get('rqid'):
            return None
        matched = (trained.get('parameters') == current
                   and trained.get('level_info') == wire.get('level_info'))
        repeated = (marker.get('training_rqid') == saved.get('rqid')
                    and marker.get('training_sha256') == _allocation_state_hash(trained)
                    and marker.get('state_sha256') == _allocation_state_hash(wire))
        return saved if saved.get('result') == 'NOERR' and (matched or repeated) else None
    return False


def career(conn, req, careerplan_list):
    uses = req.get('gameplayer_use_list')
    if not isinstance(uses, list) or not uses or len(uses)>23:
        raise ValueError('gameplayer_use_list required')
    result, seen = [], set()
    for use in uses:
        player = exact_player(conn, use.get('gameplayer_id'))
        import player_training
        player_training.ensure_progression_ledger(conn, player)
        key = store.player_key(player)
        if key in seen:
            raise ValueError('duplicate player allocation')
        seen.add(key)
        training = _training_continue_overflow_receipt(conn, req, use, player)
        if training:
            previous = store.careerplan_counts(conn, *key)
            budget = int((player.get('level_info') or {}).get('customize_point') or 0) + sum(previous.values())
            base = list(player.get('_careerplan_base_parameters') or store.wire_player(player)['parameters'])
            if (type(budget) is not int or budget < 0 or len(base) != 147
                    or type(base[0]) is not int or not 0 <= base[0] <= 12):
                raise ValueError('invalid automatic allocation base or budget')
            # E60: rebuild with the same recommendation the allocation screen
            # shows - the fitted model of the client's own auto-allocation - so a
            # player levelled to the cap matches the plan the player saw there
            # (user report 2026-10-15). Imported here because
            # recommended_allocation reads AUTO_GROUP_INDICES from this module.
            import recommended_allocation
            params, allocation = recommended_allocation.apply_to_parameters(
                base, int(base[0]), budget, recommended_allocation.GROUP_INDICES)
            player['_careerplan_base_parameters'] = base
            player['parameters'] = params
            player.setdefault('level_info', {})['customize_point'] = budget - sum(allocation.values())
            conn.execute('delete from careerplan where id=? and serial=?', key)
            for group, points in allocation.items():
                store.record_careerplan(conn, *key, group, points)
            player['_training_auto_allocation'] = {
                'policy': 'recommended_allocation_v1', 'training_rqid': training.get('rqid'),
                'training_sha256': _allocation_state_hash(training['gameplayer_list'][0]),
                'state_sha256': _allocation_state_hash(store.wire_player(player)),
            }
            persist(conn, player)
            store.log_event(conn, 'level_training_auto_allocated', {
                'id': key[0], 'serial': key[1],
                'reason': 'auto recommendation uint32 overflow; server recommendation rebuilt',
                'budget': budget, 'spent': sum(allocation.values()), 'allocation': allocation})
            result.append({'gameplayer': store.wire_player(player),
                           'careerplan_list': careerplan_list([player])})
            continue
        allocation = {}
        for row in use.get('use_list') or []:
            group, points = row.get('parameter_type'), row.get('point')
            if (row.get('type') != 'PARAMETER' or group not in pes_tables.CAREERPLAN_PARAM_LIST
                    or group in allocation or type(points) is not int or not 0 <= points <= 1000):
                raise ValueError('invalid allocation, including unsigned overflow')
            allocation[group] = points
        previous = store.careerplan_counts(conn, *key)
        budget = int((player.get('level_info') or {}).get('customize_point') or 0)+sum(previous.values())
        spent = sum(allocation.values())
        if spent > budget:
            raise ValueError('allocation exceeds earned points')
        current = list(store.wire_player(player).get('parameters') or [])
        base = list(player.get('_careerplan_base_parameters') or current)
        candidate = use.get('parameters')
        if not isinstance(candidate, list) or len(candidate) != len(current) or len(current)!=147:
            raise ValueError('invalid parameter layout')
        # Only numeric ability values may change. Native identity, positions,
        # skills and boosters remain server-owned even when the client sends a
        # complete parameter array. A reset restores saved pre-allocation stats.
        updated = list(current)
        for i in ABILITY_INDICES:
            value = candidate[i] if spent else base[i]
            if type(value) is not int or not 0 <= value <= 127 or not base[i] <= value <= min(127,base[i]+spent):
                raise ValueError('invalid allocated ability value')
            updated[i] = value
        player['_careerplan_base_parameters'] = base
        player.pop('_training_auto_allocation', None)
        latest_training = _latest_level_training_receipt(conn, req, key)
        if latest_training:
            # Even a reset identical to the training snapshot is a deliberate
            # save. Reject an old broken Continue until a NEW training occurs.
            player['_training_auto_allocation'] = {
                'training_rqid': latest_training.get('rqid'), 'invalidated': True}
        player['parameters'] = updated
        player.setdefault('level_info', {})['customize_point'] = budget-spent
        conn.execute('delete from careerplan where id=? and serial=?', key)
        for group, points in allocation.items():
            if points:
                store.record_careerplan(conn, *key, group, points)
        persist(conn, player)
        result.append({'gameplayer':store.wire_player(player),
                       'careerplan_list':careerplan_list([player])})
    store.log_event(conn,'careerplan_saved',{'players':[list(k) for k in seen]})
    return {'careerplan_result_list':result, 'achievement_notification':[]}


def progression_view(conn, gid):
    """Server-owned manual/reset state; no dependency on the AUTO setting."""
    player = exact_player(conn, gid)
    spent = store.careerplan_counts(conn, *store.player_key(player))
    available = int((player.get('level_info') or {}).get('customize_point') or 0)
    return {'available': available, 'spent': sum(spent.values()),
            'budget': available + sum(spent.values()), 'allocation': spent,
            'can_reset': any(spent.values()),
            'level': int((player.get('level_info') or {}).get('level') or 1)}


def reset_progression(conn, gid, *, request_id, user_id=0, careerplan_list=None):
    """Reset the exact owned instance through the same atomic native operation.

    The local settings UI uses this when a native screen hides manual reset.
    It preserves the user's MANUAL/AUTO preference, level, XP, skills, boosters
    and identity. No fabricated points are awarded to untrainable level-1 cards.
    """
    player = exact_player(conn, gid)
    req = {'msgid': 'CMD_USE_MYCLUB_CAREERPLAN', 'rqid': request_id,
           'user_id': user_id, 'careerplan_allocate_type': 'MANUAL',
           'use_auto_allocate_point': 'NO',
           'gameplayer_use_list': [{'gameplayer_id': dict(gid), 'use_list': [],
                                    'parameters': list(store.wire_player(player)['parameters'])}]}
    # The incoming full parameter array does not identify a reset: the empty
    # use_list does. Exclude current stats from its idempotency fingerprint so
    # a repeated panel request after another edit cannot perform a second reset.
    receipt_req = {'msgid': req['msgid'], 'rqid': request_id, 'user_id': user_id,
                   'gameplayer_id': dict(gid)}
    transact(conn, 'career-reset', receipt_req,
             lambda: career(conn, req, careerplan_list or (lambda players: [])))
    return progression_view(conn, gid)
