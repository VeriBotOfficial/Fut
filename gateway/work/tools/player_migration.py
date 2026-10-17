"""Transactional identity repair and one-time gifts in the main player list.

Never replaces a live database or resets an existing player's development.
Only immutable foot/style fields are repaired on existing instances. Gift
receipts survive moves/releases: reopening the app cannot regenerate a gift.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time

import player_identity
import player_parameters
import player_training
import recommended_allocation

MARKER = 'efootxi_player_policy_applied'
# Kept separate from the card-policy digest: installed test builds already
# applied the same policy while putting every gift into the reserve list.
PLACEMENT_MARKER = 'efootxi_gifts_main_list_v1'
# Historical identity strings remain stable so released gifts never reappear.
GIFT_SOURCE = 'efootxi:reserve-gift:v1'
ABILITY_INDICES = set(range(14, 33)) | set(range(34, 41))
SPECIAL_TYPES = player_parameters.KNOWN_KINDS - {'Standard'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _signature(policy):
    return hashlib.sha256(_json(policy).encode('utf-8')).hexdigest()


def _marker(conn):
    row = conn.execute('select value from meta where key=?', (MARKER,)).fetchone()
    return row[0] if row else None


def placement_applied(conn):
    row = conn.execute('select value from meta where key=?', (PLACEMENT_MARKER,)).fetchone()
    return row is not None and row[0] == '1'


def _move_legacy_gifts_to_main_list(conn):
    """Move only surviving gift instances, once, without rewriting any player.

    The receipt identifies the exact owned copy, not every copy of that card.
    Ordinary manually stored players are left alone. After this migration a
    player can manually store a gift again; subsequent boots respect that.
    """
    if placement_applied(conn):
        return 0
    released = conn.execute("select 1 from sqlite_master where type='table' "
                            "and name='efootxi_released_players'").fetchone()
    exclude_released = (" and not exists(select 1 from efootxi_released_players t "
                        "where t.id=g.id and t.serial=g.serial)") if released else ''
    moved = conn.execute(
        'delete from warehouse where exists('
        'select 1 from efootxi_reserve_gifts r join gameplayers g '
        'on g.id=cast(r.card_id as integer) and g.serial=r.serial '
        'where warehouse.id=g.id and warehouse.serial=g.serial and g.source=?'
        + exclude_released + ')', (GIFT_SOURCE,)).rowcount
    conn.execute('insert or replace into meta(key,value) values(?,?)', (PLACEMENT_MARKER, '1'))
    return moved


def _repair(player, policy, *, existing_instance=False):
    """Return a copy; abilities and all training bookkeeping remain untouched."""
    facts = player_parameters.policy_for(player, policy)
    if facts is None:
        return player
    out = copy.deepcopy(player)
    params = player_parameters.owned_parameters(out, policy)
    if len(params) != 147:
        raise ValueError('unsupported owned parameter layout: ' + str(out.get('gameplayer_id')))
    player_parameters.validate_weak_foot(params)
    if existing_instance and len(out.get('parameters') or []) == 147:
        # Serializer booster normalization predates this migration. Do not
        # materialize any such unrelated change into an existing trained save.
        params = list(out['parameters'])
        params[43], params[61], params[62] = facts[2], facts[5], facts[6]
    out['parameters'] = params
    out['play_style'] = [facts[5], facts[6]]
    return out


def _valid_definition(card, expected_id):
    if (card.get('gameplayer_id') or {}).get('id') != int(expected_id):
        raise ValueError('card definition identity mismatch: ' + expected_id)
    params = card.get('parameters') or []
    player_parameters.validate_weak_foot(params)
    if (type(params[0]) is not int or not 0 <= params[0] <= 12
            or any(type(params[i]) is not int or not 0 <= params[i] <= 127 for i in ABILITY_INDICES)
            or (card.get('level_info') or {}).get('level') != 1):
        raise ValueError('invalid level-one card definition: ' + expected_id)


def prepare_gift(card, serial, policy, acquired_at):
    """Build a full-development instance and its exact spent-point journal.

    Uses the existing fitted recommendation policy, not a claim of universal
    official OVR optimality. The original base never includes coach boosts.
    """
    out = _repair(card, policy)
    facts = player_parameters.policy_for(out, policy)
    if facts is None or facts[0] not in SPECIAL_TYPES:
        raise ValueError('only verified special definitions can receive gifts')
    pid = str(out['gameplayer_id']['id'])
    _valid_definition(out, pid)
    out['gameplayer_id'] = {'id': int(pid), 'serial': serial}
    out['acquisition_date'] = int(acquired_at)
    out['is_lock'] = 'NO'
    for key in ('games', 'goal', 'assist', 'red_card'):
        out[key] = 0
    out['additional_skill_id'] = []
    out['additional_position'] = []
    out.pop('additional_skill', None)
    for key in ('_developed_preview', '_training_auto_allocation', '_level_training_curve',
                '_careerplan_base_parameters', '_position_training_base_aptitudes'):
        out.pop(key, None)
    cap = facts[1]
    info = dict(out.get('level_info') or {})
    info.update(level=1, level_limit=cap, exp_total=0, exp_current_level=0, customize_point=0)
    if 'level_limit_max' in info:
        info['level_limit_max'] = cap
    out['level_info'] = info
    curve = player_training.level_curve(out)
    if cap > 1:
        out, _, _, _ = player_training.train(out, sum(curve[2:]))
    else:
        out['level_info'].update(exp_current_level=0, exp_to_next_level=0)
    base = list(out['parameters'])
    budget = 2 * (cap - 1)
    params, spent = recommended_allocation.apply_to_parameters(base, base[0], budget)
    if sum(spent.values()) > budget:
        raise ValueError('gift allocation exceeds earned budget')
    out['parameters'] = params
    out['_careerplan_base_parameters'] = base
    out['_level_training_curve'] = curve
    out['_efootxi_allocation_policy'] = 'recommended_allocation_v1'
    out['_efootxi_gift'] = GIFT_SOURCE
    out['level_info']['customize_point'] = budget - sum(spent.values())
    return out, spent


def migrate(conn, policy=None):
    """Run after owned-identity migration and before serving game requests.

    New policy revisions may add card IDs. Previously gifted cards are never
    re-gifted, and existing trained copies are never replaced or levelled.
    A failure rolls back definition repairs, existing-card repairs and gifts.
    """
    policy = player_parameters.validate_policy(player_parameters.card_policy() if policy is None else policy)
    if not policy['cards']:
        raise ValueError('efootxi card policy is missing or empty')
    if conn.in_transaction:
        raise ValueError('finish transaction before player migration')
    if not player_identity.enabled(conn):
        raise ValueError('run owned identity migration first')
    signature = _signature(policy)
    if _marker(conn) == signature and placement_applied(conn):
        return {'already_applied': True, 'gifted': 0, 'moved_to_main_list': 0,
                'policy_sha256': signature}
    conn.execute('BEGIN IMMEDIATE')
    try:
        if _marker(conn) == signature and placement_applied(conn):
            conn.rollback()
            return {'already_applied': True, 'gifted': 0, 'moved_to_main_list': 0,
                    'policy_sha256': signature}
        conn.execute('CREATE TABLE IF NOT EXISTS efootxi_reserve_gifts('
                     'card_id TEXT PRIMARY KEY,serial INTEGER NOT NULL,policy_sha256 TEXT NOT NULL,'
                     'granted_at INTEGER NOT NULL)')
        moved = _move_legacy_gifts_to_main_list(conn)
        if _marker(conn) == signature:
            # A placement-only upgrade must leave even the serialized JSON
            # byte-for-byte unchanged, including skills, training and stats.
            conn.commit()
            return {'already_applied': False, 'gifted': 0, 'moved_to_main_list': moved,
                    'policy_sha256': signature}
        gifted = {row[0] for row in conn.execute('select card_id from efootxi_reserve_gifts')}
        definitions = {pid: json.loads(raw) for pid, raw in conn.execute('select card_id,payload from managed_cards')}
        required = {pid for pid, row in policy['cards'].items() if row[0] in SPECIAL_TYPES}
        missing = sorted(required - definitions.keys())
        if missing:
            raise ValueError('missing special card definitions: ' + ','.join(missing[:5]))
        changed_definitions = changed_owned = 0
        for pid, card in definitions.items():
            facts = policy['cards'].get(pid)
            if facts is None:
                continue
            normalized = _repair(card, policy)
            _valid_definition(normalized, pid)
            normalized['level_info']['level_limit'] = facts[1]
            if 'level_limit_max' in normalized['level_info']:
                normalized['level_info']['level_limit_max'] = facts[1]
            if facts[1] == 1:
                normalized['level_info'].update(customize_point=0, exp_current_level=0, exp_to_next_level=0)
            if normalized != card:
                encoded = _json(normalized)
                conn.execute('update managed_cards set payload=?,fingerprint=? where card_id=?',
                             (encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest(), pid))
                definitions[pid] = normalized
                changed_definitions += 1
        for pid, serial, raw in conn.execute('select id,serial,json from gameplayers').fetchall():
            old = json.loads(raw)
            if (old.get('gameplayer_id') or {}) != {'id': pid, 'serial': serial}:
                raise ValueError('owned card identity mismatch')
            normalized = _repair(old, policy, existing_instance=True)
            if normalized != old:
                conn.execute('update gameplayers set json=? where id=? and serial=?', (_json(normalized), pid, serial))
                changed_owned += 1
        pending = sorted(required - gifted, key=int)
        serials = player_identity.allocate_many(conn, len(pending))
        timestamp = int(time.time())
        journal_rows = 0
        for pid, serial in zip(pending, serials):
            player, spent = prepare_gift(definitions[pid], serial, policy, timestamp)
            conn.execute('insert into gameplayers(id,serial,json,source,acquired_at) values(?,?,?,?,?)',
                         (int(pid), serial, _json(player), GIFT_SOURCE, str(timestamp)))
            conn.executemany('insert into careerplan(id,serial,category,points,at) values(?,?,?,?,?)',
                             [(int(pid), serial, group, points, str(timestamp)) for group, points in spent.items()])
            journal_rows += len(spent)
            conn.execute('insert into efootxi_reserve_gifts values(?,?,?,?)', (pid, serial, signature, timestamp))
        conn.execute('insert or replace into meta(key,value) values(?,?)', (MARKER, signature))
        conn.commit()
        return {'already_applied': False, 'gifted': len(pending), 'repaired_definitions': changed_definitions,
                'repaired_owned': changed_owned, 'journal_rows': journal_rows,
                'moved_to_main_list': moved, 'policy_sha256': signature}
    except Exception:
        conn.rollback()
        raise
