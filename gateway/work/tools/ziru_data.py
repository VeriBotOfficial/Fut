"""Ported gameplay data from the third-party offline package (ZIRU V27).

`work/tools/port_ziru_data.py` materialises the data into
`outputs/reference/ziru-data/` (tracked; the source package lives under the
gitignored `work/analysis/`). This module is the read side of that port: one
accessor per data set, plus the two builders the gate needs - an opponent squad
out of a match profile, and our own home squad out of the club store.

Design rules that come from earlier crashes, not from taste:

  * Match payloads are **self-contained**. `CMD_GET_VSCOM_OPPONENT` ships the
    opponent's 23 players as full gameplayer objects (parameters length 149,
    measured), so the client resolves the opponent's squad slots against that
    same reply. We therefore do NOT substitute our cards into an opponent
    profile - a rewritten id that no longer matches the accompanying list is
    exactly what SIGSEGV'd the client in E02/E17.
  * `CMD_START_GAME` carries OUR squad inline (`squad_data_home.player_list`,
    parameters length 145). Serving the ported file verbatim would make the
    client play with a foreign account's team, so the home side is always
    rebuilt from the club store.
  * Nothing here invents a player. If a data set is not ported, the accessor
    raises instead of returning an empty shape, so a missing port shows up as a
    loud failure rather than as a silently empty screen.

Usage:
  work\\venv\\Scripts\\python.exe work/tools/ziru_data.py --report
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "outputs" / "reference" / "ziru-data"


class NotPorted(RuntimeError):
    """The data set is absent - run port_ziru_data.py first."""


_cache: dict[str, object] = {}


def _json(rel: str):
    """Parsed data set (cached). Callers that mutate must use payload()."""
    if rel in _cache:
        return _cache[rel]
    p = DATA / rel
    if not p.is_file():
        raise NotPorted(f"{rel} not ported; run work/tools/port_ziru_data.py")
    d = json.loads(p.read_text(encoding="utf-8"))
    _cache[rel] = d
    return d


def payload(rel: str):
    """A private copy, safe to mutate and hand to the client."""
    return copy.deepcopy(_json(rel))


def manifest() -> dict:
    p = DATA / "manifest.json"
    if not p.is_file():
        raise NotPorted(f"no manifest at {p}; run work/tools/port_ziru_data.py")
    return json.loads(p.read_text(encoding="utf-8"))


def ported() -> bool:
    return (DATA / "manifest.json").is_file()


# --- opponents / teams ------------------------------------------------------
def match_profiles() -> list[dict]:
    return _json("match_profiles.json")["profiles"]


def profile(index: int) -> dict:
    """One of the 30 match profiles, wrapped around the index."""
    profiles = match_profiles()
    return payload("match_profiles.json")["profiles"][int(index) % len(profiles)]


def teams() -> dict[str, dict]:
    """base_team_id -> {team_name, team_name_short, kits, ...} (539 teams)."""
    return _json("match_teams.json")["teams"]


def team_names() -> dict[str, dict]:
    """base_team_id -> {name, short} (981 entries)."""
    return _json("team_names.json")


def team_label(team_id) -> str:
    key = str(int(team_id))
    for table in (teams(), team_names()):
        rec = table.get(key)
        if rec:
            return rec.get("team_name") or rec.get("name") or key
    return key


def rosters() -> dict[str, list[int]]:
    """team_id -> list of base player ids (415 teams / 12965 slots).

    The slots are base-player ids, not card ids, so they cannot be resolved
    against the club store directly - they are the raw material for building
    opponent teams, kept here so a later step does not have to re-unpack.
    """
    return _json("ml_rosters.json")


def opponent_pool() -> list[dict]:
    """The 30 division opponents: user_name + base_team_id + 23 player ids."""
    return _json("division_opponent_pool.json")["opponents"]


def division_roster_doc() -> list[dict]:
    """The 100 prepared squads, with per-slot position/max overall.

    Built next to the pool (`build_elite_division_100.py`). Used by the opponent
    squad builder as the slot metadata for the same player-id set, which is what
    makes a slot's fallback substitution pick the right position. Absent early in
    a fresh checkout, so callers treat None as "no slot metadata".
    """
    path = ROOT / "outputs" / "reference" / "division_opponents_100.json"
    if not path.is_file():
        return []
    if "division_opponents_100" not in _cache:
        _cache["division_opponents_100"] = json.loads(path.read_text(encoding="utf-8"))
    return _cache["division_opponents_100"].get("opponents") or []


def competition_registry() -> dict:
    return _json("ml_competition_registry.json")


# --- season / league --------------------------------------------------------
def division_rules() -> dict:
    return _json("division_rules_2027.json")


def schedules() -> dict[str, dict]:
    """name -> schedule payload (default, season_113 ... - 26 files)."""
    out: dict[str, dict] = {}
    for p in sorted((DATA / "schedules").glob("*.json")):
        out[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    if not out:
        raise NotPorted("no schedules ported")
    return out


def schedule_for_team(team_id) -> tuple[str, dict]:
    """The schedule whose first fixture contains `team_id`, else the default.

    The ported schedules are per-season snapshots (the file name carries the
    season id). Picking by team membership keeps a league table consistent with
    the fixtures the client is shown instead of pinning one arbitrary season.
    """
    want = int(team_id)
    allsch = schedules()
    for name, sch in allsch.items():
        for fixture in sch.get("schedule_list") or []:
            for m in fixture.get("match_list") or []:
                if want in (int(m.get("home_team_id") or 0), int(m.get("away_team_id") or 0)):
                    return name, payload(f"schedules/{name}.json")
    name = "ml_season_default"
    return name, payload(f"schedules/{name}.json")


def season_info(which: str = "f1") -> dict:
    """One season snapshot: fixture, opponent_team_id, ranking table, weather."""
    return payload(f"season_info/{which}.json")


def season_enter() -> dict:
    return payload("season_enter.json")


# --- campaign pass / exchange / rental --------------------------------------
def campaign_pass() -> dict:
    return payload("campaign_pass_reward.json")


def exchange_item() -> dict:
    return payload("exchange_item.json")


def exchange_rental() -> dict:
    return payload("exchange_rental.json")


def rental_list() -> dict:
    return payload("rental_list.json")


def point_item_list() -> dict:
    return payload("point_item_list.json")


# --- cards ------------------------------------------------------------------
def packs() -> dict:
    return _json("packs_config.json")


def player_records() -> dict[str, dict]:
    """card id -> {gameplayer, is_dummy} for the 1679 cards of the package."""
    return _json("player_records.json")


def random_boosters() -> dict:
    return _json("random_boosters.json")


def vscom_opponent() -> dict:
    return payload("vscom_opponent.json")


def ml_get_vscom_opponent() -> dict:
    return payload("ml_get_vscom_opponent.json")


def ml_game_result() -> dict:
    return payload("ml_game_result.json")


def start_game() -> dict:
    return payload("start_game.json")


def ml_start_game() -> dict:
    return payload("ml_start_game.json")


def wc_config() -> dict:
    return _json("wc_config.json")


def wc_special_pool() -> dict:
    return _json("wc_special_pool.json")


# --- squad builders ---------------------------------------------------------
def card_index(conn) -> dict[int, dict]:
    """Every card we can hand the client, by id: owned + draw pool."""
    out: dict[int, dict] = {}
    for table in ("gameplayers", "player_pool"):
        for gid, raw in conn.execute(f"select id, json from {table}"):
            try:
                out.setdefault(int(gid), json.loads(raw))
            except Exception:
                continue
    return out


def normalize_opponent_team(out: dict, team_id=None, user_name=None) -> dict:
    """Resolve a preset against asset-backed teams, including cached matches.

    The ported match-team set is only a subset of the native team registry.
    Preserve registered clubs/nations (including Bayern and China) even when
    they have no ported match profile. Unknown ids use the captured identity.
    Player identities and the match id stay intact. `user_name` overrides the
    display handle (the division presets carry their own opponent handles).
    """
    current = out.get('team_data') or {}
    tid = int(team_id if team_id is not None else current.get('base_team_id') or 0)
    rec = teams().get(str(tid))
    native = team_names().get(str(tid))
    if rec is None and native and native.get('name') and native.get('short'):
        rec = {'team_name': native['name'], 'team_name_short': native['short'],
               'base_team_license': current.get('base_team_license', 'UNKNOWN')}
    if not rec or not rec.get('team_name') or not rec.get('team_name_short'):
        tid = int(vscom_opponent()['team_data']['base_team_id'])
        rec = teams()[str(tid)]
    out['team_data'] = {
        'base_team_id': tid,
        'base_team_license': rec.get('base_team_license', 'UNKNOWN'),
        'team_name': rec['team_name'],
        'team_name_short': rec['team_name_short'],
    }
    # The preset squads carry their own opponent handle; the canned payload
    # carries the package author's. Prefer an explicit handle, then one the
    # payload already has, and only fall back to the club name.
    existing = next((ui.get('user_name') for ui in out.get('user_info') or []
                     if isinstance(ui, dict)), None)
    author = ((vscom_opponent().get('user_info') or [{}])[0] or {}).get('user_name')
    handle = str(user_name or (existing if existing and existing != author else rec['team_name']))[:20]
    for ui in out.get('user_info') or []:
        if isinstance(ui, dict):
            ui['user_name'] = handle
    return out


def opponent_squad(profile_index: int) -> dict:
    """The opponent side of a match profile, as the client receives it.

    Returned verbatim apart from the opponent's identity: the package ships the
    author's own handle (and, in the match result, an advert), which has no
    business travelling to our client.
    """
    prof = profile(profile_index)
    out = vscom_opponent()
    out["game_id"] = int(out.get("game_id") or 0) or _synthetic_game_id(prof)
    return normalize_opponent_team(out, prof.get('base_team_id'))


def _synthetic_game_id(prof: dict) -> int:
    """A stable game id per profile, so a replay of the same fixture matches."""
    return 2_600_000_000_000_000_00 + int(prof.get("profile_id") or 1)


# --- coverage ---------------------------------------------------------------
def coverage(conn) -> dict:
    """Resolution rates, so the port can be reported without hand-counting."""
    import club_store as store

    index = card_index(conn)
    out: dict[str, object] = {
        "ported": ported(),
        "cards_in_store": len(index),
        "profiles": len(match_profiles()),
        "teams": len(teams()),
        "rosters": len(rosters()),
        "roster_slots": sum(len(v) for v in rosters().values()),
        "schedules": len(schedules()),
        "season_points": int(store.get_meta(conn, "season_point", "0") or 0),
    }
    prof_ids = {int(i) for p in match_profiles() for i in p.get("player_ids") or []}
    out["profile_players"] = len(prof_ids)
    out["profile_players_resolvable"] = len(prof_ids & set(index))
    packs = _json("packs_config.json")
    pack_ids = {int(i) for p in packs["packs"] for i in p.get("player_ids") or []}
    out["pack_cards"] = len(pack_ids)
    out["pack_cards_resolvable"] = len(pack_ids & set(index))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT / "work" / "tools"))

    if not ported():
        print(f"not ported at {DATA}")
        return 2
    if a.report:
        import club_store as store

        conn = store.connect()
        cov = coverage(conn)
        for k, v in cov.items():
            print(f"{k:26s} {v}")
        m = manifest()
        print(f"{'manifest files':26s} {len(m['files'])}")
        return 0
    print(f"ported: {len(manifest()['files'])} files at {DATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
