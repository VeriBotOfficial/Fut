"""Build a CMD_SET_MYCLUB_ENTRY_INFO reply from the local club store.

Why this module exists: the game speaks THREE different `entry_info` schemas,
and they are not interchangeable.

  CMD_LOGIN                  entry_info: 27 keys, player objects: 18 keys
  CMD_GET_MYCLUB_ENTRY_INFO  entry_info: 43 keys, player objects: 46 keys
  CMD_SET_MYCLUB_ENTRY_INFO  entry_info: 38 keys, player objects: 23 keys

The captured reply for SET_MYCLUB_ENTRY_INFO belongs to a different account
("FC Barcelona" / "Offline Barca", 28 players of its own). Replaying it made the
client rebuild its whole account state - team, roster, squad - from a stranger's
data and SIGSEGV in CMDDeserializer (measured 2026-10-14 23:11, right after this
command). So this command must answer with *our* account, in *this* command's
shape.

The shape is taken from the captured reply (the template) and only the fields we
actually own are overwritten: identity, roster, squad, counters, currency. The
template guarantees every key and value type the client expects is present, which
is the part that keeps this from crashing again.
"""
from __future__ import annotations

import copy
import player_skills
import csv
import json
from pathlib import Path

import club_store as store

ROOT = Path(__file__).resolve().parents[2]


def _card_classes() -> dict[int, tuple[str, int]]:
    """Return the client card-class labels known from the local records."""
    path = ROOT / "outputs" / "reference" / "card-class-table.csv"
    out: dict[int, tuple[str, int]] = {}
    if not path.is_file():
        return out
    try:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    code = int(row.get("class_value") or 0)
                    pid = int(row.get("player_id") or 0)
                except (TypeError, ValueError):
                    continue
                if pid and code:
                    out.setdefault(pid, (str(row.get("card_class") or "").upper(), code))
    except OSError:
        return {}
    return out


CARD_CLASSES = _card_classes()
NATIVE_CARD_TYPE = {
    "STANDARD": 1,
    "LEGENDARY": 2,
    "ICONIC_MOMENT": 3,
    "FEATURED": 4,
    "EPIC": 5,
    "BIGTIME": 6,
    "SHOWTIME": 7,
}

# values that belong to the captured account and must never be served as ours
FOREIGN_STRINGS = {
    "team_name": "-",
    "team_name_short": "-",
    "user_name": "",
    "home_town_name": "Soccer City",
    "home_stadium_name": "Soccer Stadium",
}


def _int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def to_entry_player(src: dict, tmpl: dict) -> dict:
    """Map one stored player into this command's 23-key player shape."""
    p = copy.deepcopy(tmpl)
    gid = src.get("gameplayer_id") or {}
    p["gameplayer_id"] = {"id": _int(gid.get("id")), "serial": _int(gid.get("serial"))}
    p["uniform_number"] = _int(src.get("uniform_number"))
    p["is_lock"] = src.get("is_lock") or p.get("is_lock") or "NO"
    p["initial_base_team_id"] = _int(store.wire_player(src).get("initial_base_team_id"))
    p["sell_gp"] = _int(src.get("sell_gp"))
    p["contract_gp"] = _int(src.get("contract_gp"))
    p["contract_pes_point"] = _int(src.get("contract_pes_point"))
    for key in ("games", "goal", "assist", "red_card"):
        p[key] = _int(src.get(key))

    # here `play_style` is a string; the other schemas use a list
    ps = src.get("play_style")
    if isinstance(ps, str) and ps:
        p["play_style"] = ps
    elif isinstance(ps, (list, tuple)) and ps:
        p["play_style"] = str(_int(ps[0]))

    li = dict(p.get("level_info") or {})
    src_li = src.get("level_info") or {}
    level = _int(src_li.get("level"), 1) or 1
    limit = _int(src_li.get("level_limit"), _int(li.get("level_limit"), 1)) or 1
    li["level"] = level
    li["level_limit"] = limit
    li["level_limit_max"] = _int(li.get("level_limit_max"), limit) or limit
    li["exp_total"] = _int(src_li.get("exp_total"))
    li["exp_current_level"] = _int(src_li.get("exp_current_level"))
    li["exp_to_next_level"] = _int(src_li.get("exp_to_next_level"))
    li["customize_point"] = _int(src_li.get("customize_point"))
    p["level_info"] = li
    return p


def to_entry_squad(squad: dict, players_by_key: dict, tmpl: dict) -> dict:
    """Map a stored squad into this command's squad shape.

    `member_list` is what the client renders, and every member must exist in the
    roster we serve alongside it - that self-consistency is the point.
    """
    out = copy.deepcopy(tmpl)
    out["squad_id"] = _int(squad.get("squad_id"), 1) or 1
    name = squad.get("name")
    out["name"] = name if isinstance(name, str) and name else out.get("name") or "Squad"

    members = []
    for slot in (squad.get("squad_data") or {}).get("player_list") or []:
        if not isinstance(slot, dict):
            continue
        gid = slot.get("gameplayer_id")
        if not isinstance(gid, dict) or _int(gid.get("id")) <= 0:
            continue                      # the client's empty slot
        key = (_int(gid.get("id")), _int(gid.get("serial")))
        player = players_by_key.get(key) or players_by_key.get((key[0], None))
        members.append({
            "gameplayer_id": {"id": key[0], "serial": _int(player["gameplayer_id"]["serial"]) if player else key[1]},
            "uniform_number": _int(slot.get("uniform_number")),
            "player_level": _int((player or {}).get("level_info", {}).get("level"), 1) or 1,
        })
    out["member_list"] = members
    out["member_num"] = len(members)
    coaches = (squad.get("squad_data") or {}).get("coach") or []
    if isinstance(coaches, dict):
        coaches = [coaches]
    identities = [c["coach_id"] for c in coaches if isinstance(c, dict)
                  and isinstance(c.get("coach_id"), dict)]
    if identities:
        if "coach_id" in out:
            out["coach_id"] = copy.deepcopy(identities[0])
        if "coach_id_list" in out:
            out["coach_id_list"] = [c["id"] for c in identities]
    modern = ((squad.get("squad_data") or {}).get("squad_gameplan") or {})
    legacy = out.get("squad_gameplan")
    if isinstance(modern, dict) and isinstance(legacy, dict):
        forms = modern.get("formation") or []
        roles = legacy.get("role")
        if isinstance(roles, list) and forms:
            for i, role in enumerate(roles):
                form = forms[min(i, len(forms) - 1)] or {}
                for dst, src in (("kickoff", "formation_offensive"),
                                 ("offence", "formation_offensive"),
                                 ("defence", "formation_defensive")):
                    if isinstance(role, dict) and isinstance(form.get(src), list):
                        role[dst] = [int(x.get("role", 0)) if isinstance(x, dict) else int(x)
                                     for x in form[src]]
        settings = modern.get("match_plan_settings") or {}
        for key in ("order", "captain", "set_piece_taker", "df_overlap_setplay",
                    "auto_member_change", "auto_offside_trap"):
            if key in settings and key in legacy:
                legacy[key] = copy.deepcopy(settings[key])
    return out


def to_snapshot_player(src: dict, tmpl: dict) -> dict:
    """Map an owned card into the 46-key GET_ENTRY_INFO player schema.

    Keep the endpoint's object shape, but not the old template's 108-value
    parameter prefix: that predates the current native enum and drops boosters
    when the account refreshes after the detail/training screen (E23).
    """
    out = copy.deepcopy(tmpl)
    wire = store.wire_player(src, "list")
    gid = src.get("gameplayer_id") or {}
    pid = _int(gid.get("id"))
    level_info = dict(out.get("level_info") or {})
    source_level = dict(src.get("level_info") or {})

    for key in ("games", "goal", "assist", "red_card", "uniform_number", "sell_gp",
                "is_lock", "play_style", "initial_base_team_id", "default_rarity",
                "type", "acquisition_date"):
        if key in src and key in out:
            out[key] = copy.deepcopy(src[key])
    out["gameplayer_id"] = {"id": pid, "serial": _int(gid.get("serial"))}
    out["level"] = _int(source_level.get("level"), _int(out.get("level"), 1))
    out["level_no_self_conscious"] = out["level"]
    out["gameplayer_level"] = out["level"]
    out["level_info"] = level_info
    for key in ("level", "level_limit", "level_limit_max", "exp_total",
                "exp_current_level", "exp_to_next_level", "customize_point"):
        if key in source_level:
            level_info[key] = source_level[key]

    params = list(wire.get("parameters") or [])
    out["parameters"] = params
    skills = player_skills.learned_skill_ids(src)
    out["additional_skill"] = list(skills)
    out["is_skill_added"] = "YES" if any(s != player_skills.EMPTY_SKILL_ID for s in skills) else "NO"
    if "additional_position" in out:
        out["additional_position"] = copy.deepcopy(src.get("additional_position") or [])
    out["initial_base_team_id"] = _int(wire.get("initial_base_team_id"))
    out["original_nation_id"] = _int(src.get("_nation_id"),
                                      _int(src.get("initial_base_national_team_id")))
    out["club_team_id"] = out["initial_base_team_id"]
    out["nationality_id"] = out["original_nation_id"]

    # These fields drive the border/card renderer in the lineup view.
    cls, class_code = CARD_CLASSES.get(pid, ("", 0))
    if not cls:
        cls = str(out.get("offline_card_class") or "BASE")
    out["offline_card_class"] = cls
    out["offline_card_variation"] = _int(out.get("offline_card_variation"))
    out["offline_base_player_id"] = _int(src.get("_base_player_id"), pid) or pid
    out["offline_native_card_type"] = NATIVE_CARD_TYPE.get(cls, _int(out.get("offline_native_card_type"), 1))
    overall = _int(src.get("_overall"), _int(out.get("default_overall"), 0))
    if overall:
        out["default_overall"] = overall
        out["overall"] = overall
    out["is_contract_end"] = out.get("is_contract_end") or "NO"
    return out


def build_snapshot(conn, template_entry: dict) -> dict:
    """Build the account-owned 43-key GET_MYCLUB_ENTRY_INFO entry."""
    entry = copy.deepcopy(template_entry)
    tmpl_player = (entry.get("gameplayer_list") or [{}])[0]
    if not isinstance(tmpl_player, dict):
        tmpl_player = {}
    owned = store.entry_roster(conn)
    roster = [to_snapshot_player(p, tmpl_player) for p in owned]
    by_key = {}
    for p in roster:
        gid = p.get("gameplayer_id") or {}
        mapped = (_int(gid.get("id")), _int(gid.get("serial")))
        by_key[mapped] = p
        by_key.setdefault((mapped[0], None), p)
    tmpl_squad = (entry.get("squad_list") or [{}])[0]
    if not isinstance(tmpl_squad, dict):
        tmpl_squad = {}
    entry["gameplayer_list"] = roster
    entry["total_get_player_num"] = len(roster)
    store.apply_player_capacity(conn, entry)
    store.apply_uniform_selection(conn, entry)
    entry["gp"] = _int(store.get_meta(conn, "gp", "0"))
    entry["squad_list"] = [
        to_entry_squad(store.resolve_squad(conn, squad, owned), by_key, tmpl_squad)
        for squad in store.squads(conn)
    ]
    entry["main_squad_id"] = store.main_squad_id(conn)
    entry["squad_id"] = entry["main_squad_id"]
    if not entry["squad_list"]:
        blank = copy.deepcopy(tmpl_squad)
        blank["member_list"] = []
        blank["member_num"] = 0
        entry["squad_list"] = [blank]
    return entry


def build(conn, req: dict, template_entry: dict) -> dict:
    """Return the entry_info to send for CMD_SET_MYCLUB_ENTRY_INFO."""
    entry = copy.deepcopy(template_entry)

    # --- identity: whatever the client just submitted wins ------------------
    base_team = req.get("base_team")
    if isinstance(base_team, dict):
        for key in ("base_team_id", "base_team_license", "team_name", "team_name_short"):
            if key in base_team:
                entry[key] = base_team[key]
    else:
        # nothing submitted: fall back to what the squad element remembers
        squads = store.squads(conn)
        bt = (squads[0].get("base_team") if squads else None) or []
        if isinstance(bt, list) and bt and isinstance(bt[0], dict):
            for key in ("base_team_id", "base_team_license", "team_name", "team_name_short"):
                if key in bt[0]:
                    entry[key] = bt[0][key]

    for key, fallback in FOREIGN_STRINGS.items():
        if key in ("team_name", "team_name_short"):
            continue                      # already handled above
        value = store.get_meta(conn, key, "")
        entry[key] = value or fallback

    # --- roster: our players, in this command's shape -----------------------
    tmpl_player = (entry.get("gameplayer_list") or [{}])[0]
    if not isinstance(tmpl_player, dict):
        tmpl_player = {}
    owned = store.entry_roster(conn)
    players_by_key: dict = {}
    roster = []
    for src in owned:
        gid = src.get("gameplayer_id") or {}
        key = (_int(gid.get("id")), _int(gid.get("serial")))
        mapped = to_entry_player(src, tmpl_player)
        players_by_key[key] = mapped
        players_by_key.setdefault((key[0], None), mapped)
        roster.append(mapped)
    entry["gameplayer_list"] = roster
    entry["gameplayer_list_num"] = len(roster)

    # --- squad: our squads, every member resolvable inside the roster -------
    tmpl_squad = (entry.get("squad_list") or [{}])[0]
    if not isinstance(tmpl_squad, dict):
        tmpl_squad = {}
    squads = [to_entry_squad(store.resolve_squad(conn, s, owned), players_by_key, tmpl_squad)
              for s in store.squads(conn)]
    if not squads:
        squads = [copy.deepcopy(tmpl_squad)]
        squads[0]["member_list"] = []
        squads[0]["member_num"] = 0
    entry["squad_list"] = squads
    entry["squad_num"] = len(squads)
    entry["main_squad_id"] = store.main_squad_id(conn)

    # --- counters / currency we own ----------------------------------------
    entry["gp"] = _int(store.get_meta(conn, "gp", "0"))
    entry["total_get_player_num"] = len(roster)
    store.apply_player_capacity(conn, entry)
    store.apply_uniform_selection(conn, entry)
    entry["coach_list_num"] = len(entry.get("coach_list") or [])
    entry["default_coach_list_num"] = len(entry.get("default_coach_list") or [])
    entry["item_list_num"] = len(entry.get("item_list") or [])
    return entry


def squad_name(req: dict) -> str:
    name = req.get("squad_name")
    return name if isinstance(name, str) and name else ""


def summary(entry: dict) -> dict:
    """Small log payload (never log the whole 30 KB entry_info)."""
    return {
        "team_name": entry.get("team_name"),
        "base_team_id": entry.get("base_team_id"),
        "user_name": entry.get("user_name"),
        "players": entry.get("gameplayer_list_num"),
        "squads": entry.get("squad_num"),
        "members": [s.get("member_num") for s in entry.get("squad_list") or []],
        "gp": entry.get("gp"),
    }


def _unused(o) -> str:            # keep json import meaningful for debugging
    return json.dumps(o, ensure_ascii=False)
