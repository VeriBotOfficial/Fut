"""Native card affiliation IDs, independent of country/filter ID numbering."""
from __future__ import annotations

# National teams present in the shipped team_names table. Clubs start at 100;
# do not treat reserved/unknown IDs (25, 58, 60..99) as national teams.
NATIONAL_TEAM_IDS = frozenset(range(1, 60)) - {25, 58}


def split_base_teams(team_id: int, national_id: int) -> tuple[int, int]:
    """Move a known national affiliation out of the club-emblem field.

    Local record +16 stores the card's affiliation, which can be a club OR a
    national team. Existing cards imported using that word need the same repair
    at response time. An explicit national field takes precedence; countryId
    and _nation_id belong to another numbering space and must never be used here.
    """
    if type(team_id) is int and team_id in NATIONAL_TEAM_IDS:
        return 0, national_id or team_id
    return team_id, national_id
