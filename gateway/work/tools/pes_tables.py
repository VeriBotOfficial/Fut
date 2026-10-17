"""Static gate tables that are not part of the captured corpus.

`CMD_GET_MYCLUB_PLAYER_DATA` is the data source for the player detail / ability
point (能力点分配) screen, and the client needs more than the player object:

  careerplan_list       which parameter groups the allocation screen shows
                        (SHOOT / PASS / DRIBLE / QUICKNESS / STRENGTH / AERIAL /
                        DEFENCE / GK1..3) - without it the screen has no rows
                        and therefore no allocate buttons (E14)
  original_parameters   the player's pre-allocation base values
  experience_list       level -> required experience
  skillcard_id_list     skill cards the player may be given
  booster_training_info booster item counts

Only the first is in our own corpus (LOGIN's `common_careerplan_info`); most other
tables were taken from the third-party offline package's own
PLAYER_DATA response (work/analysis/ziru27/.../get_myclub_player_data.json),
which is itself a capture of the official server. Provenance kept here so the
values can be re-checked instead of trusted blindly.
The skill list is now the full current native enum (player_skills, E30), rather
than the captured player's eligible subset.
"""
from __future__ import annotations

from player_skills import SKILL_NAMES

# level -> experience required to reach it (official table, levels 2..35)
EXPERIENCE_LIST: list[tuple[int, int]] = [
    (2, 188), (3, 275), (4, 367), (5, 464), (6, 566), (7, 674), (8, 787),
    (9, 906), (10, 1031), (11, 1163), (12, 1301), (13, 1446), (14, 1598),
    (15, 1758), (16, 1926), (17, 2101), (18, 2285), (19, 2478), (20, 2680),
    (21, 2891), (22, 3112), (23, 3343), (24, 3585), (25, 3837), (26, 4102),
    (27, 4378), (28, 4666), (29, 4967), (30, 5282), (31, 5610), (32, 5953),
    (33, 6310), (34, 6683), (35, 7073),
]

# The captured list was one player's eligible subset, not the skill enum.
# Offline training supports every current native id, including IMPROVISER (30)
# and ACCEL_BURST (12). Keep ids distinct from parameters[114] and [124].
SKILLCARD_ID_LIST: list[int] = list(range(len(SKILL_NAMES)))

BOOSTER_TRAINING_INFO: dict = {
    "use_item_num_list_random": [0, 1],
    "use_item_num_list_select": [0, 100],
}

# Ability-point allocation groups: which concrete abilities each category raises.
# The allocation screen renders one row per group, and expands the row from this
# list - without it the row exists but has no allocatable abilities under it
# (audit B2). Values are the official server's own response (ZIRU27 package,
# get_myclub_player_data.json), which is a capture of the live server.
CAREERPLAN_PARAM_LIST: dict[str, list[str]] = {
    "SHOOT": ["ABILITY_SHOT", "ABILITY_PLACE_KICKING", "ABILITY_BALL_SPIN_CONTROL"],
    "PASS": ["ABILITY_SHORT_PASS", "ABILITY_LONG_PASS"],
    "DRIBLE": ["ABILITY_DRIBBLE", "ABILITY_BALL_CONTROL", "ABILITY_BALL_TOUCH"],
    "QUICKNESS": ["ABILITY_OFFENSE_DECISION", "ABILITY_EXPLOSIVE_POWER", "ABILITY_BODY_CONTROL"],
    "STRENGTH": ["ABILITY_KICKING_POWER", "ABILITY_SPEED", "ABILITY_STAMINA"],
    "AERIAL": ["ABILITY_HEADING", "ABILITY_JUMP", "ABILITY_BODY_BALANCE"],
    "DEFENCE": ["ABILITY_DEFENSE_DECISION", "ABILITY_INTERCEPT",
                "ABILITY_AGGRESSIVENESS", "ABILITY_CONSCIOUS_DEFENSE"],
    "GK1": ["ABILITY_GK_DECISION", "ABILITY_JUMP"],
    "GK2": ["ABILITY_CLEARANCES", "ABILITY_DEFLECTING"],
    "GK3": ["ABILITY_CATCHING", "ABILITY_COLLAPSING"],
}


def experience_list() -> list[dict]:
    return [{"level": lv, "experience": exp} for lv, exp in EXPERIENCE_LIST]
