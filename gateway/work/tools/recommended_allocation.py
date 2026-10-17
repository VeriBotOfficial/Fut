"""The game's own recommended ability allocation (能力点分配・默认推荐).

Learned from the client's own auto-allocations, captured 2026-10-15:
  * CMD_USE_MYCLUB_CAREERPLAN requests (the client computes the recommendation
    locally and sends the resulting distribution), and
  * the club's recorded careerplan rows for cards the account developed.

The shape every sample shares: a small fixed bundle per position plus the
remainder poured into that position's primary group(s), then any leftover point
spent on the cheapest affordable upgrade. Two CF cards with the same budget can
differ by where the last 1-3 points land - that is the client's own tie-break,
not a different rule.

Evidence samples (card id -> group upgrades):
  CF  89139630444046 QUICK 12 SHOOT 11 STR 8 DRI 4 AER 4 GK1 1     (budget 66)
  CF  88046292730790 QUICK 12 SHOOT 10 STR 8 DRI 4 AER 4           (62)
  CF  89138556678270 QUICK 12 SHOOT  9 STR 8 DRI 5 AER 4 DEF 1     (62)
  CF  88046292831745 QUICK 12 SHOOT  8 STR 8 DRI 4 AER 4           (56)
  RWF 89136409091415 DRI 12 QUICK 11 STR 8 SHOOT 4 PASS 4 AER 1    (66)
  SS  89138556575063 DRI 12 SHOOT 8 QUICK 8 STR 7 PASS 4           (62)
  CMF 89138019805599 PASS 12 DRI 8 STR 8 DEF 7 QUICK 4             (62)
  CB  88045755960770 STR 11 DEF 8 PASS 4 DRI 4 QUICK 4 AER 4 SHOOT 1 (50)
  GK  88045755829367 GK1 9 GK2 9 GK3 9 STR 4 AER 4 DRI 1           (54)
"""
from __future__ import annotations

import copy

from player_operations import AUTO_GROUP_INDICES

# group -> the parameter slots one upgrade raises (single source: player_operations)
GROUP_INDICES = AUTO_GROUP_INDICES

# position index -> (fixed bundle {group: upgrades}, primary groups in order)
RECOMMENDED = {
    12: ({"QUICKNESS": 12, "STRENGTH": 8, "DRIBLE": 4, "AERIAL": 4}, ["SHOOT"]),  # CF
    10: ({"DRIBLE": 12, "STRENGTH": 8, "SHOOT": 4, "PASS": 4}, ["QUICKNESS"]),   # RWF
    9:  ({"DRIBLE": 12, "STRENGTH": 8, "SHOOT": 4, "PASS": 4}, ["QUICKNESS"]),   # LWF (镜像 RWF, 有官方卡实证)
    11: ({"SHOOT": 8, "QUICKNESS": 8, "STRENGTH": 7, "PASS": 4}, ["DRIBLE"]),    # SS
    8:  ({"PASS": 8, "QUICKNESS": 8, "SHOOT": 4}, ["DRIBLE"]),                  # AMF
    5:  ({"DRIBLE": 8, "STRENGTH": 8, "DEFENCE": 7, "QUICKNESS": 4}, ["PASS"]),  # CMF
    6:  ({"PASS": 8, "DRIBLE": 8, "QUICKNESS": 8}, ["STRENGTH"]),                # LMF
    7:  ({"PASS": 8, "DRIBLE": 8, "QUICKNESS": 8}, ["STRENGTH"]),                # RMF (镜像 LMF, 依 LWF↔RWF 先例)
    4:  ({"STRENGTH": 8, "PASS": 4, "DRIBLE": 4, "QUICKNESS": 4, "AERIAL": 4}, ["DEFENCE"]),  # DMF
    1:  ({"DEFENCE": 8, "PASS": 4, "DRIBLE": 4, "QUICKNESS": 4, "AERIAL": 4}, ["STRENGTH"]),  # CB
    2:  ({"DEFENCE": 8, "QUICKNESS": 8, "DRIBLE": 4, "AERIAL": 4, "PASS": 4}, ["STRENGTH"]),  # LB
    3:  ({"DEFENCE": 8, "QUICKNESS": 8, "DRIBLE": 4, "AERIAL": 4}, ["STRENGTH"]),  # RB
    0:  ({"GK1": 9, "GK2": 9, "GK3": 9, "STRENGTH": 4, "AERIAL": 4}, []),        # GK
}
# Every position now has either an in-game capture or an official-account card.
# LWF = RWF's template (left/right mirror): verified by official-account card
# 89068226588798 (level 31, 60 points) whose own allocation - derived from the
# card's stats minus ZIRU's level-1 base for the same card id - is exactly
# {DRIBLE 12, QUICKNESS 9, STRENGTH 8, SHOOT 4, PASS 4, AERIAL 1}. RMF = LMF's
# mirror on the same precedent (no direct sample yet; either a capture or a
# mirrored official card would confirm it).
# Captures (2026-10-15, budgets = 2*(level-1) points):
#   RB 89138288138433 (62/64)  DEFENCE 12 QUICKNESS 12 DRIBLE 4 AERIAL 4 => STRENGTH, PASS fill
#   DMF 105716056708070 (56)   STRENGTH 8 PASS 4 DRIBLE 4 QUICKNESS 4 AERIAL 4 => DEFENCE, PASS fill
#   LB  (64)                   DEFENCE 12 QUICKNESS 12 DRIBLE 4 AERIAL 4 PASS 4 => STRENGTH
#   LMF (48)                   PASS 12 DRIBLE 12 QUICKNESS 12 => STRENGTH
#   AMF (58)                   PASS 12 QUICKNESS 12 SHOOT 4 => DRIBLE, STRENGTH fill
# The last 1-5 points jitter between runs (client-side), see RECOMMENDED_ALLOCATION.md.

GROUPS = ("SHOOT", "PASS", "DRIBLE", "QUICKNESS", "STRENGTH", "AERIAL",
          "DEFENCE", "GK1", "GK2", "GK3")


def price(upgrades_in_group: int) -> int:
    """Points for the next upgrade in a group (price rises every four)."""
    return upgrades_in_group // 4 + 1


def cost(upgrades_in_group: int) -> int:
    return sum(i // 4 + 1 for i in range(upgrades_in_group))


def recommended(position: int, budget: int) -> dict[str, int]:
    """Points per group the game's recommendation would spend.

    Fixed bundle first (in table order), then one upgrade at a time cycling the
    primary groups, then any leftover point on the cheapest affordable upgrade.
    """
    spec = RECOMMENDED.get(int(position))
    if spec is None or budget <= 0:
        return {}
    bundle, primaries = spec
    counts = dict.fromkeys(GROUPS, 0)
    remaining = int(budget)

    def buy(group: str) -> bool:
        nonlocal remaining
        p = price(counts[group])
        if p > remaining:
            return False
        counts[group] += 1
        remaining -= p
        return True

    for group, n in bundle.items():
        while n and buy(group):
            n -= 1
    while primaries:
        bought = False
        for group in primaries:
            bought = buy(group) or bought
        if not bought:
            break
    while remaining:
        options = [g for g in GROUPS if price(counts[g]) <= remaining]
        if not options:
            break
        buy(min(options, key=lambda g: (price(counts[g]), GROUPS.index(g))))
    return {g: v for g, v in ((g, cost(counts[g])) for g in GROUPS) if v}


def apply_to_parameters(base: list, position: int, budget: int,
                        ability_indices: dict | None = None) -> tuple[list, dict]:
    """Return (parameters with the recommended upgrades, points spent per group)."""
    ability_indices = ability_indices or GROUP_INDICES
    spent = recommended(position, budget)
    params = list(base)
    for group, points in spent.items():
        n = 0
        while points >= price(n):
            n += 1
            points -= price(n - 1)
        for index in ability_indices[group]:
            params[index] = min(127, params[index] + n)
    return params, spent
