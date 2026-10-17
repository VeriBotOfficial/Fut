"""AI-division opponent squads, built from the 100 division presets (E56).

Before this module the served opponent came from the third-party package's
single canned `vscom_opponent` payload: only the team identity was swapped per
profile, so every match fielded the same 23 players. The player list is now
built from the profile the gate actually advanced to — the 100 squads prepared
for this division (`match_profiles.json`, mirrored by `division_opponent_pool.json`
and `division_opponents_100.json`).

Each preset slot is a card id from our own card library, so the opponent is
developed with the same max-level policy the presets were computed with
(`developed_preview`, two points per level). Slots whose card is not in the
library fall back to the closest library card of the same position, chosen
deterministically, and the substitution is reported through `build()['substituted']`.

Everything else in the reply (layout wrappers, formation, uniforms, coach,
game id) stays as the known-good canned payload, so only the players vary.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

import club_store as store
import developed_preview
import ziru_data

DATA_DIR = Path(__file__).resolve().parents[2] / "outputs" / "reference" / "ziru-data"
ATTRS_DIR = Path(__file__).resolve().parents[2] / "outputs" / "reference" / "external-attrs"
_CAPS_CACHE: dict[int, int] | None = None

# Main-position names. The client uses two encodings and they are NOT the same
# table: a formation slot's `role` puts 1=CB / 2=LB (measured: role 1 sits at the
# centre of the back line, role 2 on the left), and a card's parameters[0] uses
# the same order (1=CB / 2=LB) - corrected 2026-10-15 from the official corpus
# (326 cards with a single aptitude-2 all read [0]=1 in the CB slot); the earlier
# 1=LB/2=CB reading was circular (it came from our own assembler's table).
# Assignment still goes through the position NAMES.
FORMATION_ROLE_NAMES = {
    0: "GK", 1: "CB", 2: "LB", 3: "RB", 4: "DMF", 5: "CMF", 6: "LMF",
    7: "RMF", 8: "AMF", 9: "LWF", 10: "RWF", 11: "SS", 12: "CF",
}
FORMATION_ROLE_CODES = {name: code for code, name in FORMATION_ROLE_NAMES.items()}
CARD_POSITION_NAMES = {
    0: "GK", 1: "CB", 2: "LB", 3: "RB", 4: "DMF", 5: "CMF", 6: "LMF",
    7: "RMF", 8: "AMF", 9: "LWF", 10: "RWF", 11: "SS", 12: "CF",
}
# Kept for the substitution fallback, which matches library cards by their own
# parameters[0] code (the card encoding above).
POSITION_CODES = {
    "GK": 0, "CB": 1, "LB": 2, "RB": 3, "DMF": 4, "CMF": 5, "LMF": 6,
    "RMF": 7, "AMF": 8, "LWF": 9, "RWF": 10, "SS": 11, "CF": 12,
}
# Where each role stands, in the client's own coordinate convention (x = depth
# from own goal, y = left to right; values taken from the ported match profiles).
# Extra entries cover the duplicate roles a preset can field (three centre-backs,
# two holding mids, ...); the first entry is the canonical spot.
# Slot coordinates, taken from ZIRU's own opponent payload - the two plan variants
# between them cover every outfield role: variant 0 [GK, CB, LB, RB, CB, CMF, CMF,
# RWF, LWF, AMF, CF] and variant 1 [GK, CB, CB, RB, LB, DMF, DMF, RMF, LMF, AMF,
# CF]. Nothing here is invented except the midpoints marked `# mid`, which are
# needed for shapes ZIRU does not ship (a lone holding mid, a second striker).
# First entry of a role is its canonical spot, so the first N read as a line.
ROLE_COORDS = {
    "GK": [(3, 52)],                                   # both variants
    "CB": [(11, 45), (11, 60), (11, 52)],              # v0 pair (+ mid for a third)
    "LB": [(13, 16), (15, 26)],                        # v1 / v0-adjacent
    "RB": [(13, 88), (15, 76)],                        # v1 / v0-adjacent
    "DMF": [(22, 64), (22, 40), (22, 52)],             # v1 pair (+ mid for a lone one)
    "CMF": [(24, 65), (24, 39), (24, 52)],             # v0 pair (+ mid for a lone one)
    "LMF": [(31, 16), (29, 26)],                       # v1
    "RMF": [(31, 88), (29, 78)],                       # v1
    "AMF": [(32, 52), (34, 53)],                       # v1 / v0
    "LWF": [(42, 18), (41, 28)],                       # v0
    "RWF": [(43, 84), (41, 74)],                       # v0
    "SS": [(40, 52), (40, 40)],                        # mid  # not in ZIRU
    "CF": [(47, 52), (45, 38), (45, 66)],              # v0 (+ mid pair for two strikers)
}
# Slots are written in this order so a formation reads back to front; `slot_order`
# still decides who stands where, this only fixes the array the client reads.
ROLE_LINE = ("GK", "CB", "LB", "RB", "LMF", "RMF", "DMF", "CMF", "AMF",
             "LWF", "RWF", "SS", "CF")
# How well a player of one position covers a slot of another, for the leftovers
# after the exact matches. 1.0 = his own position; a full back covers the other
# flank, a holding mid covers the box-to-box slot, and so on. Offline policy.
AFFINITY = {
    "GK": {"GK": 1.0},
    "CB": {"CB": 1.0, "DMF": 0.7, "LB": 0.6, "RB": 0.6, "CMF": 0.2, "CF": 0.2},
    "LB": {"LB": 1.0, "RB": 0.8, "CB": 0.6, "LMF": 0.6},
    "RB": {"RB": 1.0, "LB": 0.8, "CB": 0.6, "RMF": 0.6},
    "DMF": {"DMF": 1.0, "CMF": 0.9, "CB": 0.6, "CF": 0.3, "LWF": 0.3, "RWF": 0.3},
    "CMF": {"CMF": 1.0, "DMF": 0.8, "AMF": 0.8, "LMF": 0.6, "RMF": 0.6, "CF": 0.4},
    "LMF": {"LMF": 1.0, "LWF": 0.8, "CMF": 0.6, "LB": 0.5},
    "RMF": {"RMF": 1.0, "RWF": 0.8, "CMF": 0.6, "RB": 0.5, "CF": 0.6},
    "AMF": {"AMF": 1.0, "CMF": 0.8, "SS": 0.8, "CF": 0.6},
    "LWF": {"LWF": 1.0, "LMF": 0.8, "CF": 0.7, "SS": 0.7, "RWF": 0.6},
    "RWF": {"RWF": 1.0, "RMF": 0.8, "CF": 0.7, "SS": 0.7, "LWF": 0.6},
    "SS": {"SS": 1.0, "CF": 0.9, "AMF": 0.8, "LWF": 0.6, "RWF": 0.6},
    "CF": {"CF": 1.0, "SS": 0.9, "LWF": 0.7, "RWF": 0.7, "AMF": 0.6,
           "LMF": 0.6, "RMF": 0.6, "CMF": 0.4, "DMF": 0.3, "CB": 0.2},
}
STARTERS = 11
SERIAL_BASE = 96_000_000_000_000_000
OPPONENT_PARAM_LEN = 149          # the length the canned opponent payload used
_CACHE: dict[tuple, dict] = {}    # (library signature, profile index) -> built squad
_LAST_ERROR = ""                  # why the last build() fell back, for the event log


def _system_formation(template: dict) -> tuple[list[str], list[dict]]:
    """The roles and coordinates of the system's own formation, in slot order.

    The package ships two plan variants: a multi-formation pair and a single
    standard shape (`use_multi_formation='NO'`, the 4-2-3-1 with two holding
    mids). Serving that shape keeps the opponent lined up like any normal team -
    a preset's own role list is not a formation - and the coordinates are the
    client's own, never something we invent. The first variant is the fallback
    when the single one is missing.
    """
    formation = ((template.get("squad_data") or {}).get("squad_gameplan") or {}).get("formation") or []
    variants = [v for v in formation if isinstance(v, dict)]
    chosen = next((v for v in variants if str(v.get("use_multi_formation")) == "NO"), None)
    if chosen is None and variants:
        chosen = variants[0]
    slots = (chosen or {}).get("formation_offensive") or []
    roles = [FORMATION_ROLE_NAMES.get(int(s.get("role"))) for s in slots if isinstance(s, dict)]
    roles = [r for r in roles if r]
    if len(roles) != STARTERS:
        return formation_for_roles()
    return roles, [{"role": int(s["role"]), "position_x": int(s["position_x"]),
                    "position_y": int(s["position_y"])} for s in slots]


def formation_for_roles(roles: list[str] | None = None) -> tuple[list[str], list[dict]]:
    """Coordinates for the roles a squad actually fields.

    The generated squads each name their own eleven roles (4-3-3, 3-5-2, ...);
    each of them gets a slot at the client-native position for that role, extra
    occurrences of a role spread out so nobody stacks on the same spot.
    """
    chosen = [str(r) for r in (roles or []) if r in ROLE_COORDS]
    if len(chosen) != STARTERS:
        chosen = list(ROLE_LINE[:STARTERS])
    used: dict[str, int] = {}
    coords = []
    for role in chosen:
        options = ROLE_COORDS[role]
        index = used.get(role, 0)
        used[role] = index + 1
        x, y = options[index] if index < len(options) else options[-1]
        coords.append({"role": FORMATION_ROLE_CODES[role], "position_x": x, "position_y": y})
    return chosen, coords


def slot_order(slot_roles: list[str], player_positions: list[str],
               starter_count: int | None = None) -> tuple[list[int], list[dict]]:
    """Which players stand in the formation slots, and who was swapped in.

    A preset's own eleven is not a formation - several of them field three left
    backs, four centre backs or two centre forwards - so the lineup is compiled
    from the WHOLE squad (starters and bench), the way a manager fills his
    formation: an out-of-position starter is replaced by the bench player who
    actually plays that role. Exact fits first, then position affinity, and the
    preset's own eleven wins ties so its intent is kept wherever it can be.

    Deterministic: (fit desc, bench-after-starter, slot order, list order).
    Returns (slot -> player index, swaps) with swaps reported for the event log.
    """
    total = len(player_positions)
    starters = total if starter_count is None else min(starter_count, total)
    placed: list[int | None] = [None] * len(slot_roles)
    used: set[int] = set()

    # 1. Exact primary-position matches, maximised (Kuhn's augmenting paths): a
    #    player whose primary position is the slot's role is always correct, so
    #    the lineup takes as many of those as the roster allows.
    def assign(slot_index: int, visited: set) -> bool:
        for player_index in range(total):
            if player_index in visited or player_positions[player_index] != slot_roles[slot_index]:
                continue
            visited.add(player_index)
            holder = next((s for s, p in enumerate(placed) if p == player_index), None)
            if holder is None or assign(holder, visited):
                placed[slot_index] = player_index
                if holder is not None:
                    placed[holder] = None
                return True
        return False

    for slot_index in range(len(slot_roles)):
        assign(slot_index, set())
    used = {p for p in placed if p is not None}

    # 2. The remaining slots take the best available affinity, the preset's own
    #    eleven winning ties so its intent is kept wherever it can be.
    candidates: list[tuple] = []
    for slot_index, role in enumerate(slot_roles):
        if placed[slot_index] is not None:
            continue
        for player_index, position in enumerate(player_positions):
            if player_index in used:
                continue
            fit = AFFINITY.get(role, {}).get(position, 0.0)
            candidates.append((-fit, player_index >= starters, slot_index, player_index))
    candidates.sort()
    for _, _, slot_index, player_index in candidates:
        if placed[slot_index] is not None or player_index in used:
            continue
        placed[slot_index] = player_index
        used.add(player_index)
    order = [p for p in placed if p is not None]
    promoted = [(index, player_index) for index, player_index in enumerate(order)
                if player_index >= starters]
    benched = [i for i in range(starters) if i not in set(order)]
    swaps = [{"slot": index, "role": slot_roles[index],
              "out": benched[n] if n < len(benched) else None, "in": player_index}
             for n, (index, player_index) in enumerate(promoted)]
    return order, swaps


class PresetError(ValueError):
    """The preset data for this index is missing or malformed."""


def _by_id_set(rows, ids):
    want = frozenset(int(i) for i in ids)
    for row in rows:
        if frozenset(int(i) for i in row.get("player_ids") or []) == want:
            return row
    return None


def preset(conn, index: int) -> dict:
    """One opponent squad: the prepared roster plus the display identity to use.

    Preferred source is `division_squads.json` (build_division_squads.py): the
    player's design, N formations x M squads with every slot drawn from the card
    pool by its own position, so a starter is never out of position. The earlier
    match-profile presets stay as the fallback for a checkout without that file.
    """
    manual = manual_squads()
    if manual:
        entry = manual[int(index) % len(manual)]
        slots = [dict(x) for x in (entry.get('slots') or [])]
        ids = [int(x['gameplayer_id']['id']) for x in entry['player_list']]
        return {'index': int(index) % len(manual), 'profile': None, 'player_ids': ids,
                'slots': slots, 'user_name': entry.get('name') or f'Manual DV {index+1}',
                'team_name': (entry.get('base_team') or [{}])[0].get('team_name'),
                'formation': None, 'formation_roles': None,
                'team_id': (entry.get('base_team') or [{}])[0].get('base_team_id'),
                'squad_id': entry.get('squad_id'), 'coach': entry.get('coach'),
                'manual_squad_data': entry.get('squad_data')}
    squads = division_squads()
    if squads:
        entry = squads[int(index) % len(squads)]
        slots = [dict(s) for s in (entry.get('slots') or []) + (entry.get('substitutes') or [])]
        ids = [int(s['card_id']) for s in slots if s.get('card_id') is not None]
        if len(ids) >= 11:
            return {"index": int(index) % len(squads), "profile": None, "player_ids": ids,
                    "slots": slots, "user_name": entry.get('user_name'),
                    "team_name": entry.get('team_name'),
                    "formation": entry.get('formation'),
                    "formation_roles": [s.get('role') for s in entry.get('slots') or []],
                    "team_id": entry.get('base_team_id'), "squad_id": entry.get('squad_id')}
    profiles = ziru_data.match_profiles()
    if not profiles:
        raise PresetError("no match profiles")
    prof = profiles[int(index) % len(profiles)]
    ids = [int(i) for i in prof.get("player_ids") or []]
    if len(ids) < 11:
        raise PresetError("profile has no roster")
    out = {"index": int(index) % len(profiles), "profile": prof, "player_ids": ids,
           "user_name": None, "slots": None, "formation": None, "formation_roles": None,
           "team_id": prof.get('base_team_id')}
    try:
        out["user_name"] = (_by_id_set(ziru_data.opponent_pool(), ids) or {}).get("user_name")
    except Exception:
        out["user_name"] = None
    doc = ziru_data.division_roster_doc()
    if doc:
        entry = _by_id_set(doc, ids)
        if entry:
            out["slots"] = [dict(s) for s in (entry.get("starting_xi") or []) + (entry.get("substitutes") or [])]
            out["team_name"] = entry.get("team_name")
            out["formation"] = entry.get("formation")
    return out


DIVISION_SQUADS_FILE = DATA_DIR / "division_squads.json"
MANUAL_SQUADS_FILE = DATA_DIR / "manual_dv_squads.json"
_SQUADS_CACHE: list | None = None
_MANUAL_CACHE: list | None = None

def manual_squads() -> list[dict]:
    global _MANUAL_CACHE
    if _MANUAL_CACHE is None:
        try:
            payload = json.loads(MANUAL_SQUADS_FILE.read_text(encoding='utf-8'))
            _MANUAL_CACHE = payload.get('squads') or []
        except Exception:
            _MANUAL_CACHE = []
    return _MANUAL_CACHE


def division_squads() -> list[dict]:
    """The generated division squads (empty when the builder has not run yet)."""
    global _SQUADS_CACHE
    if _SQUADS_CACHE is None:
        try:
            payload = json.loads(DIVISION_SQUADS_FILE.read_text(encoding="utf-8"))
            _SQUADS_CACHE = payload.get("squads") or []
        except Exception:                        # noqa: BLE001 - fallback source
            _SQUADS_CACHE = []
    return _SQUADS_CACHE


def _library_card(conn: sqlite3.Connection, card_id: int) -> dict | None:
    row = conn.execute("select payload from managed_cards where card_id=?",
                       (str(card_id),)).fetchone()
    if row is None:
        row = conn.execute("select payload from managed_cards where card_id=?",
                           (card_id,)).fetchone()
    if row is not None:
        return json.loads(row[0])
    for table in ("player_pool", "gameplayers"):
        row = conn.execute(f"select json from {table} where id=?", (card_id,)).fetchone()
        if row is not None:
            return json.loads(row[0])
    return None


def _substitute(conn: sqlite3.Connection, position: str | None, overall: int | None,
                exclude: frozenset = frozenset()) -> dict | None:
    """Closest library card for a slot whose preset card we do not have.

    Deterministic: same position first, then the smallest |overall - target|,
    then the smallest card id, so a replay of the same fixture is identical.
    `exclude` carries the cards already standing in this squad, so a substitution
    can never duplicate a preset player or another substitute.
    """
    code = POSITION_CODES.get(str(position or "").upper())
    target = overall if isinstance(overall, int) else 80
    best = None
    for card_id, payload in conn.execute("select cast(card_id as text), payload from managed_cards"):
        try:
            card = json.loads(payload)
        except Exception:
            continue
        params = card.get("parameters") or []
        if len(params) < 14:
            continue
        if int(card_id) in exclude:
            continue
        if code is not None and params[0] != code:
            continue
        distance = abs(int(card.get("_overall") or 0) - target)
        key = (distance, int(card_id) if str(card_id).isdigit() else 0)
        if best is None or key < best[0]:
            best = (key, card)
    return best[1] if best else None


def external_caps() -> dict[int, int]:
    """card id -> the level cap eFootHub reports for it.

    Our library payloads were assembled from local records, and a good number of
    them carry `level_limit = 1` where the card's own data says 29-35. A cap-1
    card cannot be developed at all, so the client shows a legend at his base
    rating (the "why is everyone 65-78" screen). The opponent path takes the
    authoritative cap from the same external source the rest of the pipeline
    uses - never higher than the card's own data, only raised to it.
    """
    global _CAPS_CACHE
    if _CAPS_CACHE is not None:
        return _CAPS_CACHE
    caps: dict[int, int] = {}
    for path in ATTRS_DIR.glob('*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        hub = data.get('efhub') or {}
        card = hub.get('card') or {}
        player = hub.get('player') or {}
        cap = card.get('initialLevelCap') or player.get('levelCap')
        if type(cap) is int and cap > 0:
            caps[int(path.stem)] = cap
    _CAPS_CACHE = caps
    return caps


def player_entry(conn: sqlite3.Connection, card_id: int, uniform: int, serial: int,
                 slot: dict | None = None, exclude: frozenset = frozenset(),
                 division: int | None = None) -> tuple[dict, bool]:
    """One opponent player, developed the way the opponent policy calls for.

    Default is the preset's own intent - every card at its level cap, so a served
    opponent looks like the elite squad the preset describes. Passing a division
    grades the development instead (the opt-in difficulty policy).

    Returns (player, substituted). The parameter array keeps the card's own native
    layout and is zero-padded to the length the canned opponent payload used (149),
    so a client branch keyed on the array length behaves exactly as it did before
    this module existed.
    """
    card = _library_card(conn, card_id)
    substituted = False
    if card is None:
        card = _substitute(conn, (slot or {}).get("position"), (slot or {}).get("max_ovr"), exclude)
        substituted = True
    if card is None:
        raise PresetError(f"cannot resolve opponent card {card_id}")
    # A card whose stored cap is below the cap its own data reports (the library
    # has plenty of `level_limit = 1` legends) is raised to the external cap
    # before development - otherwise the client shows him at his base rating.
    external = external_caps().get(int(card_id))
    level_info = dict(card.get('level_info') or {})
    if external and int(level_info.get('level_limit') or 0) < external:
        level_info['level_limit'] = external
        level_info.setdefault('level', 1)
        card['level_info'] = level_info
    if division is None and opponent_development(conn) == "graded":
        division = current_division(conn)
    developed = (graded_development(copy.deepcopy(card), division) if division is not None
                 else developed_preview.developed(copy.deepcopy(card)))
    wire = store.wire_player(developed, "list")
    params = list(wire.get("parameters") or [])
    # A card is proficient at its own position: the aptitude block
    # (parameters[1:14]) carries 2 on the own slot in the native payload - the
    # ported opponent reference reads [0]*13 + [2] for its GK - while our library
    # cards arrive with 0 there. Without this the client treats every preset
    # player as out of position and the overall ratings collapse (measured: the
    # user's opponent screen showed 55-73 for cards whose data is 90+).
    main = params[0] if params else None
    if type(main) is int and 0 <= main < 13 and len(params) > 1 + main:
        if type(params[1 + main]) is not int or params[1 + main] < 2:
            params[1 + main] = 2
    wire["parameters"] = params
    if 0 < len(params) < OPPONENT_PARAM_LEN:
        wire["parameters"] = params + [0] * (OPPONENT_PARAM_LEN - len(params))
    wire["gameplayer_id"] = {"id": int(wire.get("gameplayer_id", {}).get("id") or card_id),
                             "serial": int(serial)}
    wire["uniform_number"] = int(uniform)
    wire["rental_match_left"] = 0
    return wire, substituted


def build(conn: sqlite3.Connection, index: int, division: int | None = None) -> dict | None:
    """The opponent squad for profile `index`, or None when it cannot be built.

    Returns {player_list, squad_data, team_id, user_name, substituted}. None
    means "keep the canned payload" (closed fallback: never invent a squad) and
    the reason is available from `last_error()` for the caller's event log.

    Development: the presets' own level caps by default (`opponent_development`
    meta), or graded by `division` when that policy is on - or when a caller
    passes a division explicitly.
    """
    global _LAST_ERROR
    try:
        if division is None and opponent_development(conn) == "graded":
            division = current_division(conn)
        out = _build(conn, index, division)
        _LAST_ERROR = "" if out is not None else f"profile {index} has no preset data"
        return out
    except Exception as exc:                     # noqa: BLE001 - reported, not raised
        _LAST_ERROR = f"{type(exc).__name__}: {exc}"
        return None


def last_error() -> str:
    return _LAST_ERROR


# --- opponent development policy -------------------------------------------
# The player's call (2026-10-15): the opponents ARE the prepared presets, so they
# are served at the presets' own development - every card at its level cap - and
# the squad looks like the elite team the preset describes. The division-graded
# development below stays as an opt-in policy (`meta.opponent_development = graded`),
# kept because the official corpus shows low-grade opponents arriving undeveloped
# (053/099: level 1, no allocated points), which is a real Konami behaviour for
# low COM levels - just not what this account's division squads should look like.
POLICY_KEY = "opponent_development"


def opponent_development(conn) -> str:
    """`max` (default) serves the presets fully developed; `graded` scales them."""
    try:
        value = str(store.get_meta(conn, POLICY_KEY, "max") or "max").strip().lower()
    except Exception:                            # noqa: BLE001 - default policy
        return "max"
    return "graded" if value == "graded" else "max"


def progress_for_division(division) -> float:
    try:
        div = int(division)
    except (TypeError, ValueError):
        div = 10
    return (10 - min(10, max(1, div))) / 9


def graded_development(card: dict, division) -> dict:
    """The opponent card developed to what `division` calls for."""
    cap = developed_preview.cap_of(card)
    level = 1 + int(round(progress_for_division(division) * max(0, cap - 1)))
    return developed_preview.developed_to(card, level)


def current_division(conn: sqlite3.Connection) -> int:
    """The AI-division level in play (10 when nothing is known yet)."""
    try:
        import ai_division
        return int(ai_division.state(conn).get("division") or 10)
    except Exception:                            # noqa: BLE001 - easiest band
        return 10


def _library_signature(conn: sqlite3.Connection) -> tuple:
    """Cheap identity of the card library a connection serves.

    The built squads are cached per (library, profile): the same profile can be
    asked of a fixture store in tests and of the live store, and those must not
    share a cache entry.
    """
    try:
        row = conn.execute("select count(*), coalesce(max(rowid), 0) from managed_cards").fetchone()
        return (int(row[0]), int(row[1]))
    except sqlite3.Error:
        return (-1, -1)


def _slot_role(template: dict, index: int):
    """The role code of formation slot `index` in the payload the client renders.

    The observed opponent screen matches the first (offensive) variant's roles,
    so that variant drives the placement; the defensive variant keeps its own
    coordinates from the template.
    """
    try:
        formation = (template.get("squad_data") or {}).get("squad_gameplan", {}).get("formation") or []
        variant = formation[0] if formation else {}
        slot = (variant.get("formation_offensive") or [])[index]
        return int(slot.get("role"))
    except Exception:
        return None


def _slot_position(slots: list, index: int) -> str | None:
    if index < len(slots) and isinstance(slots[index], dict):
        return str(slots[index].get("position") or "").upper() or None
    return None


def _card_position(player: dict) -> str | None:
    params = player.get("parameters") or []
    if len(params) < 1 or type(params[0]) is not int:
        return None
    return CARD_POSITION_NAMES.get(params[0])


def _slot_role_raw(player: dict) -> int:
    """The card's own parameters[0], for tests and diagnostics."""
    params = player.get("parameters") or []
    return params[0] if params and type(params[0]) is int else -1


def _build(conn: sqlite3.Connection, index: int, division: int | None) -> dict | None:
    entry = preset(conn, index)
    template = ziru_data.vscom_opponent()
    key = (_library_signature(conn), entry["index"],
           "max" if division is None else int(division))
    cached = _CACHE.get(key)
    if cached is not None:
        return copy.deepcopy(cached)

    slots = entry["slots"] or []
    templates = template.get("player_list") or []
    uniforms = [int(p.get("uniform_number") or n + 1) for n, p in enumerate(templates)]
    while len(uniforms) < len(entry["player_ids"]):
        uniforms.append(len(uniforms) + 1)

    # Resolve every preset player first, then decide where each one stands.
    resolved, substituted = [], []
    used: set[int] = set(entry["player_ids"])
    for n, card_id in enumerate(entry["player_ids"]):
        slot = slots[n] if n < len(slots) else None
        serial = SERIAL_BASE + entry["index"] * 1000 + n
        player, swapped = player_entry(conn, card_id, uniforms[n], serial, slot,
                                       frozenset(used - {card_id}), division)
        resolved.append(player)
        if swapped:
            served_id = int(player["gameplayer_id"]["id"])
            used.discard(card_id)
            used.add(served_id)
            substituted.append({"slot": n, "card_id": card_id, "served": served_id})

    order = list(range(len(resolved)))
    manual_plan = entry.get('manual_squad_data')
    # Copy the complete native plan, never reconstruct coordinates by role.
    native_variants = template['squad_data']['squad_gameplan']['formation']
    native_plan = next(v for v in native_variants if v.get('use_multi_formation') == 'NO')
    roles, coords = _system_formation(template)
    swaps: list[dict] = []
    if len(resolved) > STARTERS and not manual_plan:
        # Fill the formation from the whole squad, not just the preset's eleven:
        # a bench player who actually plays the vacant role comes in for an
        # out-of-position starter, so the client shows nobody in the wrong slot.
        positions = [(_slot_position(slots, n) or _card_position(resolved[n]) or "")
                     for n in range(len(resolved))]
        starting, swaps = slot_order(roles, positions, STARTERS)
        if len(starting) == STARTERS and len(set(starting)) == STARTERS:
            order = starting + [n for n in range(len(resolved)) if n not in starting]
        else:
            swaps = []
    players = resolved if manual_plan else [resolved[n] for n in order]
    for n, player in enumerate(players):
        player["uniform_number"] = uniforms[n]
    # The client rates a card by its aptitude for the SLOT it stands in. Tier-1
    # names the guide lists for one position sometimes carry their cards under a
    # neighbouring position (Figo as RMF, his cards read RWF), so the slot's own
    # aptitude is stamped as well - that is the field the rating is computed from.
    for slot_index, role in enumerate(roles[:STARTERS]):
        code = FORMATION_ROLE_CODES.get(role)
        if code is None or slot_index >= len(players):
            continue
        params = list(players[slot_index].get("parameters") or [])
        if len(params) > 1 + code and (type(params[1 + code]) is not int or params[1 + code] < 2):
            params[1 + code] = 2
            players[slot_index]["parameters"] = params

    squad = copy.deepcopy(template.get("squad_data") or {})
    if isinstance(squad, dict):
        squad["player_list"] = [{"gameplayer_id": dict(p["gameplayer_id"]),
                                 "uniform_number": p["uniform_number"]} for p in players]
        if entry.get('coach'):
            squad['coach'] = copy.deepcopy(entry['coach'])
        # The formation stays the system's own standard shape; we only decide who
        # stands where, by matching each preset player to a slot of his position.
        # Preserve every native field, including offensive/defensive coordinates
        # and tactic wrappers, by copying the complete known-good single plan.
        if manual_plan and isinstance(manual_plan.get('squad_gameplan'), dict):
            squad['squad_gameplan'] = copy.deepcopy(manual_plan['squad_gameplan'])
        else:
            plans = squad['squad_gameplan']['formation']
            squad['squad_gameplan']['formation'] = [copy.deepcopy(native_plan) for _ in plans]
    out = {"player_list": players, "squad_data": squad, "user_name": entry.get("user_name"),
           "team_id": entry.get("team_id"),
           "substituted": substituted, "profile_index": entry["index"],
           "formation": entry.get("formation"),
           "slot_order": order[:STARTERS], "formation_roles": roles, "xi_swaps": swaps}
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[key] = copy.deepcopy(out)
    return out
