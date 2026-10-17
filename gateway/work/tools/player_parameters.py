"""Versioned player parameter layouts (E19/E23).

11.0.1 owned replies contain 145 items; 11.1.0 uses 147. ZIRU's static table
adds four zero items to the old layout. Current owned responses insert the two
new native skill fields before ABILITY1..3; abilities and training stay intact.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

# Test-client 147-slot layout. The bundled legal START_GAME capture carries
# these slots; foot/style repairs are additionally supported by the card-table
# versus owned-array audit. Engine behaviour still requires device regression.
# Weak-foot values deliberately remain the observed native 0..3 enumeration.
IDENTITY_SLOTS = {'weak_accuracy': 33, 'foot': 43, 'weak_usage': 45,
                  'attack_style': 61, 'defence_style': 62}
POLICY_FILE = Path(__file__).resolve().parents[2] / 'outputs/reference/efootxi-card-policy.json'
POLICY_COLUMNS = ('kind', 'cap', 'foot', 'weak_accuracy', 'weak_usage', 'attack_style', 'defence_style')
KNOWN_KINDS = {'Standard', 'Legendary', 'Featured', 'Epic', 'Highlight', 'Trending'}


def validate_policy(document):
    if (not isinstance(document, dict) or document.get('schema') != 1
            or document.get('columns') != list(POLICY_COLUMNS)
            or not isinstance(document.get('cards'), dict)):
        raise ValueError('invalid efootxi card policy')
    for pid, row in document['cards'].items():
        if (not isinstance(pid, str) or not pid.isdigit() or int(pid) <= 0
                or not isinstance(row, list) or len(row) != len(POLICY_COLUMNS)
                or row[0] not in KNOWN_KINDS
                or any(type(value) is not int for value in row[1:])
                or not 1 <= row[1] <= 1000 or row[2] not in (0, 1)
                or not 0 <= row[3] <= 3 or not 0 <= row[4] <= 3
                or not 0 <= row[5] <= 35 or not 0 <= row[6] <= 35
                or (row[0] == 'Trending' and row[1] != 1)):
            raise ValueError('invalid card policy row: ' + str(pid))
    return document


@functools.lru_cache(maxsize=1)
def card_policy():
    """Small, versioned facts, independent of the large external card cache."""
    if not POLICY_FILE.is_file():
        return {'schema': 1, 'columns': list(POLICY_COLUMNS), 'cards': {}}
    return validate_policy(json.loads(POLICY_FILE.read_text(encoding='utf-8')))


def policy_for(player, policy=None):
    cards = (card_policy() if policy is None else policy)['cards']
    return cards.get(str((player.get('gameplayer_id') or {}).get('id')))


def card_details(card_id, policy=None):
    """Stable small UI API; callers paginate owned cards before resolving it."""
    document = card_policy() if policy is None else policy
    pid = str(card_id)
    row = document['cards'].get(pid)
    if row is None:
        return None
    label = document.get('labels', {}).get(pid, [pid, None])
    return dict(zip(POLICY_COLUMNS, row), card_id=pid, name=label[0], age=label[1])


def validate_weak_foot(parameters):
    """Validate, never shift 0..3 to the UI's displayed ordinal 1..4."""
    if len(parameters) != 147:
        raise ValueError('expected native 147-parameter player layout')
    if any(type(parameters[index]) is not int or not 0 <= parameters[index] <= 3
           for index in (33, 45)):
        raise ValueError('invalid weak-foot enumeration')

BOOSTER_OFFSET = {145: 142, 147: 144, 149: 142}
# Local record field widths: slot0 bits308..315, slot1 bits288..297.
BOOSTER_LIMITS = (255, 1023)


def owned_parameters(player: dict, policy=None) -> list:
    """Return a copy suitable for owned-card commands.

    Preserve unknown layouts, nonzero extensions and trained booster values.
    A length alone is insufficient evidence that an array is a static record.
    """
    params = list(player.get('parameters') or [])
    boosters = player.get('initial_booster_id_list')
    offset = BOOSTER_OFFSET.get(len(params))
    known = len(params) in (145, 147) or (len(params) == 149 and params[145:] == [0, 0, 0, 0])
    if (known and offset is not None and isinstance(boosters, list) and len(boosters) == 3
            and all(type(v) is int and 0 <= v <= limit
                    for v, limit in zip(boosters[:2], BOOSTER_LIMITS))):
        for n, limit in enumerate(BOOSTER_LIMITS):
            value = params[offset+n]
            # Fill missing innate boosters and replace the out-of-range IDs
            # left by the external importer. A valid nonzero trained choice
            # may differ from the innate list and must survive serialization.
            if type(value) is int and (value == 0 or value < 0 or value > limit):
                params[offset+n] = boosters[n]
    if len(params) == 149 and params[145:] == [0, 0, 0, 0] and isinstance(boosters, list) and len(boosters) == 3:
        params = params[:145]
    # 11.1.0's native enum adds CONGO/INDUS at 142/143. ABILITY1..3
    # move from 142..144 to 144..146 (E23 native registration evidence).
    # Convert before returning any owned response, including trained cards.
    if len(params) == 145:
        params = params[:142] + [0, 0] + params[142:145]
    # Match replies have no separate play_style or preferredFoot fields.
    # Apply immutable, card-specific facts at the common serialization boundary
    # so new draws, GP purchases, owned cards and opponents use the same values.
    # Do not change abilities, weak-foot grades, learned skills or boosters.
    facts = policy_for(player, policy) if len(params) == 147 else None
    if facts is not None:
        params[43], params[61], params[62] = facts[2], facts[5], facts[6]
    return params


def write_initial_boosters(parameters: list, boosters: list) -> None:
    """Write an assembled card's innate boosters in its known layout."""
    offset = BOOSTER_OFFSET.get(len(parameters))
    if offset is None:
        raise ValueError(f'unknown player parameter layout: {len(parameters)}')
    parameters[offset:offset+2] = boosters[:2]
