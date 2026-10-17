"""Keep saved My League player references aligned with the START roster.

The saved plan refers to its player container by zero-based member indices.
START can reorder that container or omit empty bench slots. Rebase only the
known member-reference fields, without changing positions, styles or tactics.
"""
from __future__ import annotations

import copy

import match_protocol as protocol

UNSET = 255


def _identity(slot):
    value = slot.get('gameplayer_id') if isinstance(slot, dict) else None
    if not isinstance(value, dict):
        raise protocol.MatchError('invalid_saved_match_squad')
    pid, serial = value.get('id'), value.get('serial')
    if type(pid) is int and type(serial) is int and pid == serial == 0:
        return None
    try:
        return protocol.positive_id(pid), protocol.positive_id(serial)
    except protocol.MatchError:
        raise protocol.MatchError('invalid_saved_match_squad')


def _active_unverified_tactics(plan):
    """Tactical target data lacks a verified reindexing contract in this build."""
    for formation in plan.get('formation') or []:
        if not isinstance(formation, dict):
            raise protocol.MatchError('invalid_match_gameplan')
        for tactic in formation.get('team_tactics') or []:
            if not isinstance(tactic, dict):
                raise protocol.MatchError('invalid_match_gameplan')
            values = tactic.get('value_id')
            if values is not None and (not isinstance(values, list) or
                                       any(type(v) is not int for v in values)):
                raise protocol.MatchError('invalid_match_gameplan')
            if any(v != 0 for v in values or []):
                return True
    return False


def gameplan_for_entries(saved_squad, entries):
    """Return a private, consistent plan or reject unsafe/missing starters.

No saved roster, player, or plan is modified. Unknown response identities do
not acquire someone else's tactical references. Native index 255 remains the
unset value; missing optional bench references become unset as well.
"""
    source = saved_squad.get('player_list') if isinstance(saved_squad, dict) else None
    original = saved_squad.get('squad_gameplan') if isinstance(saved_squad, dict) else None
    if not isinstance(source, list) or not 11 <= len(source) <= 40:
        raise protocol.MatchError('invalid_saved_match_squad')
    if not isinstance(entries, list) or not 11 <= len(entries) <= 40:
        raise protocol.MatchError('incomplete_match_starting_eleven')
    if not isinstance(original, dict) or not original:
        raise protocol.MatchError('missing_match_gameplan')
    plan = copy.deepcopy(original)
    target = {}
    for index, entry in enumerate(entries):
        identity = _identity(entry)
        if identity is None or identity in target:
            raise protocol.MatchError('invalid_match_squad_identity')
        target[identity] = index
    mapping, source_ids = {}, set()
    for index, slot in enumerate(source):
        identity = _identity(slot)
        if identity is None:
            continue
        if identity in source_ids:
            raise protocol.MatchError('invalid_saved_match_squad')
        source_ids.add(identity)
        if identity in target:
            mapping[index] = target[identity]
    if set(target) - source_ids:
        raise protocol.MatchError('match_squad_plan_identity_mismatch')
    changed = any(old != new for old, new in mapping.items())
    if changed and _active_unverified_tactics(plan):
        # Do not guess whether team_tactics.data contains roster indices or
        # formation-position indices. An explicit failure is safer than moving
        # an active individual instruction to a different player.
        raise protocol.MatchError('unverified_match_tactics_reindex')

    def member(value, *, required=False, allow_unset=True):
        if type(value) is not int or (value != UNSET and not 0 <= value < len(source)):
            raise protocol.MatchError('invalid_match_plan_member_index')
        result = UNSET if value == UNSET else mapping.get(value, UNSET)
        if required and result == UNSET and (value != UNSET or not allow_unset):
            raise protocol.MatchError('missing_match_starting_player')
        return result

    settings = plan.get('match_plan_settings')
    if not isinstance(settings, dict):
        raise protocol.MatchError('invalid_match_gameplan')
    order = settings.get('order')
    if not isinstance(order, list) or not 11 <= len(order) <= 40:
        raise protocol.MatchError('invalid_match_plan_order')
    settings['order'] = [member(value, required=index < 11, allow_unset=index >= 11)
                         for index, value in enumerate(order)]
    if len(set(settings['order'][:11])) != 11:
        raise protocol.MatchError('duplicate_match_starting_player')
    if 'captain' in settings:
        settings['captain'] = member(settings['captain'], required=True)
    for name in ('set_piece_taker', 'df_overlap_setplay'):
        if name in settings:
            values = settings[name]
            if not isinstance(values, list):
                raise protocol.MatchError('invalid_match_gameplan')
            settings[name] = [member(value) for value in values]
    for formation in plan.get('formation') or []:
        if not isinstance(formation, dict):
            raise protocol.MatchError('invalid_match_gameplan')
        links = formation.get('coach_link_data') or []
        if not isinstance(links, list):
            raise protocol.MatchError('invalid_match_gameplan')
        for link in links:
            if not isinstance(link, dict):
                raise protocol.MatchError('invalid_match_gameplan')
            for name in ('key_member_Id', 'link_member_Id'):
                if name in link:
                    link[name] = member(link[name], required=link.get('is_link_on') == 'YES')
    return plan
