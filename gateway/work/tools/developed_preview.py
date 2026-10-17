"""Full-development preview for card lists (卡池 / 普通球员签约列表).

Those surfaces display the stored card *definition*, which is a level-1 card.
This module renders the card the way the account plays it: the game's own
recommended ability allocation (能力点分配・默认推荐, see recommended_allocation)
at the card's level cap, plus the equipped coach's stat boosts.

Display-only: the caller sends the returned payload and never writes it back.
An acquisition still creates the level-1 instance. Switch the preview off with
meta `developed_preview = 0`; switch the coach boost with `preview_coach_boost = 0`.
"""
from __future__ import annotations

import copy
import json

import club_store as store
import recommended_allocation

META_KEY = "developed_preview"
COACH_META_KEY = "preview_coach_boost"
POINTS_PER_LEVEL = 2
BOOST_MAP = json.loads(
    (store.ROOT / "outputs" / "reference" / "coach_boost_map.json").read_text("utf-8")
) if (store.ROOT / "outputs" / "reference" / "coach_boost_map.json").exists() else {}


def enabled(conn) -> bool:
    """List previews default to ON; `developed_preview = 0` restores level-1 rows."""
    return str(store.get_meta(conn, META_KEY, "1") or "1") != "0"


def coach_boost_enabled(conn) -> bool:
    return str(store.get_meta(conn, COACH_META_KEY, "1") or "1") != "0"


def cap_of(player: dict) -> int:
    li = player.get("level_info") or {}
    try:
        return int(li.get("level_limit") or 1)
    except (TypeError, ValueError):
        return 1


def budget_of(cap: int) -> int:
    """Free allocation points: two per level gained (level 1 -> cap)."""
    return POINTS_PER_LEVEL * max(0, cap - 1)


def developed_to(player: dict, level: int, boosts: dict[int, int] | None = None) -> dict:
    """The card developed to `level` (1 = the stored card, cap = full development).

    Same allocation policy as `developed`, scaled to the levels gained, so the
    list previews and the graded AI-division opponents cannot drift apart. The
    card's own level cap is kept in level_info: an official opponent sample at
    any COM level arrives with its own level_limit and level 1.
    """
    cap = cap_of(player)
    level = max(1, min(int(level or 1), max(1, cap)))
    out = copy.deepcopy(player)
    base = list(out.get("parameters") or [])
    if level > 1 and len(base) == 147:
        params, _spent = recommended_allocation.apply_to_parameters(
            base, int(base[0]), POINTS_PER_LEVEL * (level - 1), recommended_allocation.GROUP_INDICES)
        out["parameters"] = params
    if boosts:
        params = list(out["parameters"])
        for index, amount in boosts.items():
            params[index] = min(127, params[index] + amount)
        out["parameters"] = params
    out["_developed_preview"] = level
    out["level_info"] = dict(player.get("level_info") or {}, level=level, level_limit=cap,
                             exp_total=0, exp_current_level=0, exp_to_next_level=0, customize_point=0)
    return out


def coach_boosts(conn) -> dict[int, int]:
    """+1 per ability for the coach the squad plays with (its ability_id list)."""
    boost: dict[int, int] = {}
    if not coach_boost_enabled(conn):
        return boost
    squads = store.squads(conn)
    coach_id = None
    for sq in squads:
        coach = sq.get("coach") or (sq.get("squad_data") or {}).get("coach")
        for entry in (coach or []):
            ident = (entry or {}).get("coach_id") or {}
            if isinstance(ident, dict) and ident.get("id"):
                coach_id = int(ident["id"])
                break
        if coach_id:
            break
    for coach in store.coaches(conn):
        ident = coach.get("coach_id") or {}
        if isinstance(ident, dict) and int(ident.get("id") or 0) == coach_id:
            for ability in coach.get("ability_id") or []:
                for index in BOOST_MAP.get(str(ability), []):
                    boost[int(index)] = boost.get(int(index), 0) + 1
            break
    return boost


def developed(player: dict, boosts: dict[int, int] | None = None) -> dict:
    """The card as displayed after full development (and the coach's boosts).

    Returns a display copy; the stored payload is never modified. A card we
    cannot develop (no level cap) is returned unchanged, so a list row can never
    fail on this path. The result carries an internal marker so applying the
    preview twice cannot allocate the same points twice (`wire_player` strips
    `_`-prefixed keys before the client sees them).
    """
    if player.get("_developed_preview"):
        return player
    cap = cap_of(player)
    if cap <= 1 and not boosts:
        return player
    base = list(player.get("parameters") or [])
    if len(base) != 147:
        return player
    out = copy.deepcopy(player)
    if cap > 1:
        params, _spent = recommended_allocation.apply_to_parameters(
            base, int(base[0]), budget_of(cap), recommended_allocation.GROUP_INDICES)
        out["parameters"] = params
    if boosts:
        params = list(out["parameters"])
        for index, amount in boosts.items():
            params[index] = min(127, params[index] + amount)
        out["parameters"] = params
    out["_developed_preview"] = cap
    out["level_info"] = dict(player.get("level_info") or {},
                             level=cap, level_limit=cap, exp_total=0,
                             exp_current_level=0, exp_to_next_level=0, customize_point=0)
    return out
