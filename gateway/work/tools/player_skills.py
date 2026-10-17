"""11.1.0 native skill-card ids (E30); do not confuse ids with parameter indices.
Registration-name source: outputs/runs/20261015-E30-special-skills/native-skill-registrations.json
(historical library SHA256 2ea6b0b56bb590420f967dc62b4ee24c9495b92a2bb67a71c7b270e79bebc82b).
The shipped engine SHA256 2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659
accepts native skill IDs 0..73 and uses 74 as an empty learned slot. A historical
registration name alone does not establish an available effect in this engine.
"""
from __future__ import annotations

# Tuple index is the native SKILL_CARD enum value.
SKILL_NAMES = (
    'SCISSORS_SKILLS',  # 0
    'DOUBLE_TOUCH',  # 1
    'FLIP_FLAP_SKILLS',  # 2
    'ROULETTE_SKILLS',  # 3
    'SOMBRERO',  # 4
    'EDGE_TURN',  # 5
    'QUICK_TOUCH',  # 6
    'PIVOT_FOOT_TOUCH',  # 7
    'TAP_FAKE',  # 8
    'SPRING_CUT',  # 9
    'SOLE_CONTROL',  # 10
    'MOMENTUM_DRIBBLING',  # 11
    'ACCEL_BURST',  # 12
    'BREAKTHROUGH_DRIBBLE',  # 13
    'AMAZON',  # 14
    'HEADER',  # 15
    'BULLET_HEADER',  # 16
    'LONG_RANGE_DRIVE',  # 17
    'BLITZ_CURLER',  # 18
    'CONTROL_LOOP_SHOT',  # 19
    'KNUCKLE_SHOT',  # 20
    'DROP_SHOT',  # 21
    'RISING_SHOT',  # 22
    'MIDDLE_SHOT',  # 23
    'LOW_IMPACT_SHOT',  # 24
    'INDUS',  # 25
    'TONE',  # 26
    'ACROBATIC_SHOT',  # 27
    'HEEL_TRICK',  # 28
    'ONE_TOUCH_SHOT',  # 29
    'IMPROVISER',  # 30
    'AGGRESSIVE_SHOOTER',  # 31
    'DIRECT_PASS',  # 32
    'THROUGH_PASS',  # 33
    'BACK_SPIN_LOB',  # 34
    'PIN_POINT_CROSS',  # 35
    'BLITZ_CROSSING',  # 36
    'OUT_SIDE_LONG',  # 37
    'RABONA',  # 38
    'NO_LOOK_PASS',  # 39
    'GAME_CHANGING_PASS',  # 40
    'VISIONARY_PASS',  # 41
    'PHENOMENAL_PASS',  # 42
    'MEKONG',  # 43
    'LOW_LOB',  # 44
    'LOW_PUNT_KICK',  # 45
    'LOW_HIGH_PUNT_KICK',  # 46
    'LONGTHROW',  # 47
    'GK_LONG_THROW',  # 48
    'PK_KICKER',  # 49
    'PK_STOPPER',  # 50
    'THAMES',  # 51
    'NILE',  # 52
    'GK_COACHING',  # 53
    'GK_ALERT',  # 54
    'MALICIA',  # 55
    'MAN_MARK',  # 56
    'CHASING',  # 57
    'INTERCEPT',  # 58
    'BLOCKER',  # 59
    'AIR_BATTLE',  # 60
    'SLIDING',  # 61
    'PHYSICAL_TACKLE',  # 62
    'GANGES',  # 63
    'FORTRESS',  # 64
    'ACROBATIC_CLEAR',  # 65
    'DF_SKY_HIGH',  # 66
    'SHADOW_GUARDIAN',  # 67
    'CONGO',  # 68
    'CAPTAINCY',  # 69
    'MAESTRO_SIGNAL',  # 70
    'SUPER_SUB',  # 71
    'COMBATIVE_SPIRIT',  # 72
    'BREAK_SPRINT',  # 73
    'STEAMROLLER',  # 74
)

# Innate skills remain in parameters; learned skills stay in additional_skill_id.
# Only verified native flag mappings participate in duplicate detection.
PARAMETER_BY_SKILL = {
    0: 70,  # SCISSORS_SKILLS
    1: 107,  # DOUBLE_TOUCH
    2: 71,  # FLIP_FLAP_SKILLS
    3: 72,  # ROULETTE_SKILLS
    4: 73,  # SOMBRERO
    5: 108,  # EDGE_TURN
    6: 74,  # QUICK_TOUCH
    7: 75,  # PIVOT_FOOT_TOUCH
    8: 135,  # TAP_FAKE
    9: 136,  # SPRING_CUT
    10: 102,  # SOLE_CONTROL
    11: 116,  # MOMENTUM_DRIBBLING
    12: 124,  # ACCEL_BURST
    13: 130,  # BREAKTHROUGH_DRIBBLE
    14: 140,  # AMAZON
    15: 76,  # HEADER
    16: 121,  # BULLET_HEADER
    17: 77,  # LONG_RANGE_DRIVE
    18: 120,  # BLITZ_CURLER
    19: 101,  # CONTROL_LOOP_SHOT
    20: 78,  # KNUCKLE_SHOT
    21: 103,  # DROP_SHOT
    22: 104,  # RISING_SHOT
    23: 109,  # MIDDLE_SHOT
    24: 127,  # LOW_IMPACT_SHOT
    25: 143,  # INDUS
    26: 134,  # TONE
    27: 79,  # ACROBATIC_SHOT
    28: 80,  # HEEL_TRICK
    29: 81,  # ONE_TOUCH_SHOT
    30: 114,  # IMPROVISER
    31: 128,  # AGGRESSIVE_SHOOTER
    32: 82,  # DIRECT_PASS
    33: 110,  # THROUGH_PASS
    34: 83,  # BACK_SPIN_LOB
    35: 84,  # PIN_POINT_CROSS
    36: 119,  # BLITZ_CROSSING
    37: 85,  # OUT_SIDE_LONG
    38: 86,  # RABONA
    39: 98,  # NO_LOOK_PASS
    40: 117,  # GAME_CHANGING_PASS
    41: 118,  # VISIONARY_PASS
    42: 122,  # PHENOMENAL_PASS
    43: 141,  # MEKONG
    44: 87,  # LOW_LOB
    45: 88,  # LOW_PUNT_KICK
    46: 105,  # LOW_HIGH_PUNT_KICK
    47: 89,  # LONGTHROW
    48: 90,  # GK_LONG_THROW
    49: 99,  # PK_KICKER
    50: 100,  # PK_STOPPER
    51: 138,  # THAMES
    52: 139,  # NILE
    53: 126,  # GK_COACHING
    54: 129,  # GK_ALERT
    55: 91,  # MALICIA
    56: 92,  # MAN_MARK
    57: 93,  # CHASING
    58: 106,  # INTERCEPT
    59: 111,  # BLOCKER
    60: 112,  # AIR_BATTLE
    61: 113,  # SLIDING
    62: 125,  # PHYSICAL_TACKLE
    63: 137,  # GANGES
    64: 115,  # FORTRESS
    65: 94,  # ACROBATIC_CLEAR
    66: 123,  # DF_SKY_HIGH
    67: 132,  # SHADOW_GUARDIAN
    68: 142,  # CONGO
    69: 95,  # CAPTAINCY
    70: 131,  # MAESTRO_SIGNAL
    71: 96,  # SUPER_SUB
    72: 97,  # COMBATIVE_SPIRIT
    73: 133,  # BREAK_SPRINT
}

def innate_skill_ids(parameters: list) -> set[int]:
    return {sid for sid, index in PARAMETER_BY_SKILL.items()
            if index < len(parameters) and parameters[index] == 1}


# Category grouping follows the current native skill order, rather than the
# older ZIRU client's ranges (e.g. 31 is AGGRESSIVE_SHOOTER, 44 is LOW_LOB).
CATEGORY_SKILLS = {
    'SHOOT': set(range(15, 32)),
    'PASS': set(range(32, 45)),
    'DRIBBLE': set(range(15)),
    'DEFENCE': set(range(56, 69)),
    'OTHER': set(range(45, 56)) | set(range(69, 75)),
}

# Conservative pool of named special skills with verified match-parameter flags.
# River-code entries (AMAZON/INDUS/etc.) and STEAMROLLER are intentionally absent:
# enum registration alone does not establish a usable localized skill.
RANDOM_SPECIAL_SKILL_IDS = frozenset({
    11, 12, 13, 16, 18, 24, 30, 31, 36, 40, 41, 42,
    53, 54, 62, 64, 66, 67, 70, 73,
})


def category_skill_ids(category, item: dict) -> set[int]:
    """Resolve native string/integer categories, then item effect/grade."""
    names = ('SHOOT', 'PASS', 'DRIBBLE', 'DEFENCE', 'OTHER', 'OTHER')
    if type(category) is int:
        if 0 <= category < len(names):
            return CATEGORY_SKILLS[names[category]]
        raise ValueError('unknown skill category')
    if isinstance(category, str) and category.strip():
        name = category.upper()
        for word, resolved in (('SHOOT','SHOOT'), ('FINISH','SHOOT'), ('PASS','PASS'),
                               ('DRIB','DRIBBLE'), ('DEF','DEFENCE'), ('OTHER','OTHER')):
            if word in name:
                return CATEGORY_SKILLS[resolved]
        raise ValueError('unknown skill category')
    # Category item metadata uses 1..5, while the request enum uses 0..4.
    for key in ('effect', 'grade'):
        value = item.get(key)
        if type(value) is int and 1 <= value <= 5:
            return CATEGORY_SKILLS[names[value-1]]
    raise ValueError('skill category required')


# Registered placeholders do not establish a named, usable skill. Keep old
# saved IDs intact, but never offer these as new choices. In particular 74 has
# no match-parameter mapping in this client version.
UNVERIFIED_SKILL_IDS = frozenset({9, 14, 25, 26, 31, 43, 51, 52, 63, 68, 70, 74})
SELECTABLE_SKILL_IDS = frozenset(PARAMETER_BY_SKILL) - UNVERIFIED_SKILL_IDS
MAX_ADDITIONAL_SKILLS = 5
# In this delivered engine, 0x765b97c maps wire IDs 0..73 identically and
# everything else to 74; the native five-slot array tests 74 as its empty marker.
# Keep it only as a position placeholder, never as an offered/trained effect.
EMPTY_SKILL_ID = 74


def learned_skill_ids(player: dict) -> list[int]:
    """The saved ordered slots are authoritative, including an empty list.

    Some inherited detail snapshots carry all native skills in
    ``additional_skill`` (up to ten entries). That field must never replace an
    existing ``additional_skill_id`` list. No sorting or set conversion is
    allowed: each list position is a particular training slot. Empty slots use
    the native marker 74 and may occur more than once without being skills.
    """
    if 'additional_skill_id' in player:
        values = player['additional_skill_id']
    else:
        legacy = player.get('additional_skill', [])
        values = legacy if isinstance(legacy, list) and len(legacy) <= MAX_ADDITIONAL_SKILLS else []
    if (not isinstance(values, list) or len(values) > MAX_ADDITIONAL_SKILLS
            or any(type(v) is not int or not 0 <= v < len(SKILL_NAMES) for v in values)
            or len(set(v for v in values if v != EMPTY_SKILL_ID)) !=
               sum(v != EMPTY_SKILL_ID for v in values)):
        raise ValueError('invalid saved additional skill slots')
    return list(values)


def apply_wire(player: dict, shape: str = 'login') -> dict:
    """Synchronize aliases in an already-copied wire object, never the save."""
    values = learned_skill_ids(player)
    if 'additional_skill_id' in player or shape in ('list', 'detail'):
        player['additional_skill_id'] = list(values)
    if 'additional_skill' in player or shape == 'detail':
        player['additional_skill'] = list(values)
    if 'is_skill_added' in player or shape == 'detail':
        player['is_skill_added'] = 'YES' if any(v != EMPTY_SKILL_ID for v in values) else 'NO'
    return player


def match_parameters(player: dict, parameters: list) -> list:
    """Overlay learned flags on a copy; never bake them into innate skills."""
    out = list(parameters)
    for skill_id in learned_skill_ids(player):
        index = PARAMETER_BY_SKILL.get(skill_id)
        if index is not None and index < len(out):
            out[index] = 1
    return out


def selectable_skill_ids(player: dict | None = None) -> list[int]:
    excluded = set()
    if player is not None:
        from player_parameters import owned_parameters
        excluded = innate_skill_ids(owned_parameters(player)) | set(learned_skill_ids(player))
    return sorted(SELECTABLE_SKILL_IDS - excluded)


def catalog(player: dict | None = None) -> list[dict]:
    """Stable choice data for the local selector; IDs are NOT parameter indices.

    A mapping establishes the gateway's flag support, not a claim that every
    native animation/trigger has been verified on a physical device.
    """
    from skill_names import CHINESE_NAMES
    learned = set(learned_skill_ids(player)) if player is not None else set()
    innate = set()
    if player is not None:
        from player_parameters import owned_parameters
        innate = innate_skill_ids(owned_parameters(player))
    return [dict(id=sid, native_name=SKILL_NAMES[sid],
                 display_name=CHINESE_NAMES.get(sid, SKILL_NAMES[sid].replace('_', ' ').title()),
                 special=sid in RANDOM_SPECIAL_SKILL_IDS,
                 category=next((k for k, ids in CATEGORY_SKILLS.items() if sid in ids), 'OTHER'),
                 learned=sid in learned, innate=sid in innate,
                 eligible=sid not in learned | innate, parameter_index=PARAMETER_BY_SKILL[sid])
            for sid in sorted(SELECTABLE_SKILL_IDS)]
