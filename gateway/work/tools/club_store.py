"""Local club store for the eFootball revival (stage 02).

SQLite-backed state that the gate server serves from, so what the player does
in game (draws, squad edits, currency) survives restarts instead of being
replayed from a frozen capture.

Seeding pulls the account state out of the decrypted corpus
(outputs/reference/decrypted/*.txt), so the store starts from the real account
snapshot we captured.

Usage:
  python work/tools/club_store.py --init            # create + seed
  python work/tools/club_store.py --show            # print a summary
  python work/tools/club_store.py --reset           # delete and re-seed
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path

import msgpack
import player_identity
import player_skills
import project_info
from player_parameters import owned_parameters
from player_teams import split_base_teams

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "outputs" / "efootball" / "local-club.sqlite3"
CORPUS = ROOT / "outputs" / "reference" / "decrypted"
SPECIAL_COACHES = ROOT / "outputs" / "reference" / "SPECIAL_BOOSTER_COACHES.json"
# Official active-list capacity in the current corpus is 100000 (LOGIN and
# GET_MYCLUB_ENTRY_INFO: gameplayer_list_max / current_gameplayer_list_max /
# unlock_gameplayer_frame all 100000; the earlier 004 capture still had 900/500).
# The reserve side stays unlocked at the same value (E04/E37).
PLAYER_CAPACITY = 100000
# The client exposes a finite game-plan list.  Keep this independent from the
# player-card capacity: every slot is a complete, independently persisted
# squad document in SQLite.
GAMEPLAN_CAPACITY = 20

# Channel link. The client renders several text slots verbatim from our data
# (home banner note line, pack notice / description small print), and those
# slots exist to advertise the channel, so they all read this one constant.
# The gate and the pack installers / migrator / rewriter share it.
SOCIAL_LINK = project_info.PROJECT_URL


def pack_small_print(description: str) -> str:
    """Keep the pack's own blurb line, put the channel link on the small-print line.

    Idempotent: running it on an already rewritten description returns the same
    text, so installers, the migrator and the rewriter tool can all apply it.
    """
    head = str(description or '').split('\n')[0].strip()
    return f'{head}\n{SOCIAL_LINK}' if head else SOCIAL_LINK


def apply_player_capacity(conn, entry: dict) -> None:
    """Keep all three account schemas on the same unlocked player capacities."""
    for key in ("gameplayer_list_max", "current_gameplayer_list_max",
                "unlock_gameplayer_frame", "warehouse_player_max",
                "unlock_gameplayer_warehouse_frame", "current_gameplayer_warehouse_list_max"):
        entry[key] = PLAYER_CAPACITY
    entry["warehouse_player_num"] = warehouse_count(conn)
    for key in ("gameplayer_list_num", "total_get_player_num"):
        if key in entry:
            entry[key] = len(entry.get("gameplayer_list") or [])

SCHEMA = """
create table if not exists meta(key text primary key, value text);
create table if not exists gameplayers(
    id integer not null,
    serial integer not null,
    json text not null,
    source text,
    acquired_at text,
    primary key (id, serial)
);
create table if not exists agents(agent_id integer primary key, json text not null);
create table if not exists coaches(coach_id integer not null, serial integer not null, json text not null, primary key (coach_id, serial));
create table if not exists squads(squad_id integer primary key, json text not null);
create table if not exists stepup(kind text primary key, cleared text not null);
create table if not exists warehouse(id integer not null, serial integer not null, primary key (id, serial));
create table if not exists events(id integer primary key autoincrement, at text, kind text, detail text);

-- Ability-point allocation log (能力点分配 / CMD_USE_MYCLUB_CAREERPLAN).
-- The pool itself lives on the player (level_info.customize_point); this keeps
-- the history so the category -> parameter mapping can be pinned from real
-- allocations instead of guessed (TASKS R13).
create table if not exists careerplan(
    id integer not null,
    serial integer not null,
    category text,
    points integer,
    at text
);

-- Draw pool: every obtainable player, independent of what the club owns.
-- Populated from CMD_GET_MYCLUB_PROCURABLE_GAMEPLAYERLIST captures and from
-- any locally constructed catalogue. This is what makes a draw actually draw:
-- the old code copied a frozen 10-row list and never consulted a pool.
create table if not exists player_pool(
    id integer not null,
    serial integer not null,
    rarity integer not null default 1,
    json text not null,
    source text,
    primary key (id, serial)
);
create index if not exists idx_player_pool_rarity on player_pool(rarity);

-- Per-agent draw configuration, so each agent can weight its own rarities.
create table if not exists agent_pool(
    agent_id integer not null,
    rarity integer not null,
    weight integer not null default 1,
    primary key (agent_id, rarity)
);

-- Season-point rentals (赛季租借). A rental grants a card that expires: the
-- client shows the remaining matches, so the expiry is part of the state and
-- not just a reply field.
create table if not exists rentals(
    id integer not null,
    serial integer not null,
    item_id integer,
    cost integer,
    expiration_date integer,
    rented_at text,
    primary key (id, serial)
);
"""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout + WAL: several writers hit this file at once (the gate, the pool
    # importers, the test suites). Without them a write that overlaps another
    # raises "database is locked", and the addon then answers with a truncated
    # reply - which is how a client gets a half-built account and dies.
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30.0)
    conn.execute("pragma journal_mode=wal")
    conn.execute("pragma busy_timeout=30000")
    conn.executescript(SCHEMA)
    return conn


def log_event(conn: sqlite3.Connection, kind: str, detail) -> None:
    conn.execute(
        "insert into events(at, kind, detail) values (?,?,?)",
        (now(), kind, json.dumps(detail, ensure_ascii=False)[:4000]),
    )


# --- corpus helpers --------------------------------------------------------
def _load(conn: sqlite3.Connection, name: str) -> dict | None:
    p = CORPUS / name
    if not p.is_file():
        return None
    try:
        return msgpack.unpackb(p.read_bytes(), raw=False, strict_map_key=False)
    except Exception:
        return None


def _first_response(prefix: str) -> dict | None:
    for p in sorted(CORPUS.glob(f"*_{prefix}_resp.txt")):
        try:
            return msgpack.unpackb(p.read_bytes(), raw=False, strict_map_key=False)
        except Exception:
            continue
    return None


# --- seeding ---------------------------------------------------------------
def seed(conn: sqlite3.Connection) -> dict:
    stats = {"players": 0, "agents": 0, "squads": 0}

    login = _first_response("gate_CMD_LOGIN")
    if login:
        entry = login.get("entry_info") or {}
        for sq in entry.get("squad_list") or []:
            if isinstance(sq, dict) and "squad_id" in sq:
                conn.execute(
                    "insert or replace into squads(squad_id, json) values (?,?)",
                    (sq["squad_id"], json.dumps(sq, ensure_ascii=False)),
                )
                stats["squads"] += 1
        set_meta(conn, "gp", str(login.get("gp", 0)))
        set_meta(conn, "coin", str((login.get("coin_info") or {}).get("coin", 0)))
        set_meta(conn, "user_id", str((login.get("pes_user_info") or {}).get("user_id", "")))
        set_meta(conn, "user_name", str((login.get("pes_user_info") or {}).get("user_name", "")))
        set_meta(conn, "team_name", str(entry.get("team_name", "")))
        set_meta(conn, "team_name_short", str(entry.get("team_name_short", "")))

    draw = _first_response("gate_CMD_USE_MYCLUB_AGENT")
    if draw:
        for gp in draw.get("gameplayer_list") or []:
            player = gp.get("gameplayer") if isinstance(gp, dict) else None
            if isinstance(player, dict) and isinstance(player.get("gameplayer_id"), dict):
                gid = player["gameplayer_id"]
                conn.execute(
                    "insert or ignore into gameplayers(id, serial, json, source, acquired_at) values (?,?,?,?,?)",
                    (int(gid.get("id", 0)), int(gid.get("serial", 0)), json.dumps(player, ensure_ascii=False), "seed-draw", now()),
                )
                stats["players"] += 1
        for ag in draw.get("agent_list") or []:
            if isinstance(ag, dict) and "agent_id" in ag:
                conn.execute(
                    "insert or replace into agents(agent_id, json) values (?,?)",
                    (ag["agent_id"], json.dumps(ag, ensure_ascii=False)),
                )
                stats["agents"] += 1

    agentlist = _first_response("gate_CMD_GET_MYCLUB_AGENTLIST")
    if agentlist:
        for ag in agentlist.get("agent_list") or []:
            if isinstance(ag, dict) and "agent_id" in ag:
                conn.execute(
                    "insert or replace into agents(agent_id, json) values (?,?)",
                    (ag["agent_id"], json.dumps(ag, ensure_ascii=False)),
                )
                stats["agents"] += 1
        set_meta(conn, "agent_price_list", json.dumps(agentlist.get("agent_price_list") or [], ensure_ascii=False))

    for kind in ("TEAM_SELECT", "SPECIAL_GACHA", "GAMEPLAN_SWAP", "PATHTOGLORY_1", "PATHTOGLORY_2"):
        conn.execute("insert or replace into stepup(kind, cleared) values (?, 'YES')", (kind,))

    conn.commit()
    log_event(conn, "seed", stats)
    return stats


# --- accessors -------------------------------------------------------------
def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("select value from meta where key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    if key in ("gp", "coin", "season_point"):
        fixed = get_meta(conn, "account_resource_fixed", "")
        if fixed:
            value = fixed
    conn.execute("insert or replace into meta(key, value) values (?,?)", (key, str(value)))


def players(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("select json from gameplayers order by id")]


def player_count(conn: sqlite3.Connection) -> int:
    return conn.execute("select count(*) from gameplayers").fetchone()[0]


def normalize_player(player: dict) -> dict:
    """Return a persistent copy with the game's booster slots normalized.

    Card importers may carry eFootHub/ZIRU numbering in ``parameters`` while
    also carrying the authoritative local ``initial_booster_id_list``.  The
    wire path already repairs this defensively; doing the same at the storage
    boundary prevents stale imported values from resurfacing through another
    command or after a restart.  ``owned_parameters`` preserves valid trained
    booster choices and unknown layouts.
    """
    out = json.loads(json.dumps(player, ensure_ascii=False))
    if isinstance(out.get("parameters"), list):
        out["parameters"] = owned_parameters(out)
    return out


def agents(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("select json from agents order by agent_id")]


def install_special_coaches(conn: sqlite3.Connection, source: Path = SPECIAL_COACHES) -> int:
    """Persist raw coach payloads from the authoritative reference catalog."""
    data = json.loads(source.read_text(encoding="utf-8"))
    catalog = data.get("coaches") or []
    if not catalog or data.get("total_coaches") != len(catalog):
        raise ValueError("coach catalog count mismatch")
    rows = []
    seen_ids, seen_serials = set(), set()
    for coach in catalog:
        if not isinstance(coach, dict) or not isinstance(coach.get("raw_payload"), dict):
            raise ValueError("invalid coach payload object")
        payload = coach.get("raw_payload")
        ident = payload.get("coach_id")
        if not isinstance(ident, dict):
            raise ValueError("invalid coach identity object")
        if (type(ident.get("id")) is not int or type(ident.get("serial")) is not int
                or ident["id"] <= 0 or ident["serial"] <= 0
                or coach.get("coach_id") != ident["id"] or coach.get("serial") != ident["serial"]
                or ident["id"] in seen_ids or ident["serial"] in seen_serials
                or not isinstance(payload.get("ability_id"), list)
                or len(payload["ability_id"]) != 4
                or any(type(a) is not int or a < 0 for a in payload["ability_id"])
                or any(type(payload.get(k)) is not int or not 0 <= payload[k] <= 100
                       for k in ("possession", "short_counter", "long_counter", "side_attack", "long_ball"))):
            raise ValueError("invalid or duplicate coach payload")
        seen_ids.add(ident["id"])
        seen_serials.add(ident["serial"])
        rows.append((ident["id"], ident["serial"],
                     json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    # Keep the existing current coach owned; installing a collection must not
    # invalidate the squad's original (id, serial) reference.
    login = _first_response("gate_CMD_LOGIN") or {}
    previous = (login.get("entry_info") or {}).get("coach_list") or []
    with conn:
        for payload in previous:
            ident = payload.get("coach_id") or {}
            if isinstance(ident, dict) and ident.get("id") and ident.get("serial"):
                conn.execute("insert or ignore into coaches values (?,?,?)", (
                    ident["id"], ident["serial"], json.dumps(payload, ensure_ascii=False)))
        conn.executemany("insert or replace into coaches(coach_id, serial, json) values (?,?,?)", rows)
    return len(rows)


def coaches(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("select json from coaches order by coach_id, serial")]


def squads(conn: sqlite3.Connection) -> list[dict]:
    return [json.loads(r[0]) for r in conn.execute("select json from squads order by squad_id")]


def ensure_gameplan_slots(conn: sqlite3.Connection, capacity: int = GAMEPLAN_CAPACITY) -> int:
    """Migrate the captured single plan to a persistent multi-plan store.

    The marker makes this idempotent: deleting a plan later is not undone by a
    restart.  Capacity is metadata, not 100 eagerly serialized LOGIN objects:
    the client accepts the native list of actually-created plans and creates a
    requested blank slot with CMD_CREATE_MYCLUB_SQUAD.
    """
    capacity = max(1, int(capacity))
    marker = get_meta(conn, "gameplan_slots_initialized", "")
    rows = squads(conn)
    if marker == str(capacity):
        return len(rows)
    if not rows:
        return 0
    set_meta(conn, "gameplan_slots_initialized", str(capacity))
    if not conn.in_transaction:
        conn.commit()
    return len(squads(conn))


def stepup(conn: sqlite3.Connection) -> list[dict]:
    return [{"kind": k, "cleared": c} for k, c in conn.execute("select kind, cleared from stepup")]


def add_player(conn: sqlite3.Connection, player: dict, source: str) -> bool:
    gid = player.get("gameplayer_id") or {}
    if player_identity.enabled(conn):
        # An acquisition creates an instance, unlike upsert (an existing card
        # update). Reserve under the caller's SQLite write transaction.
        if not conn.in_transaction:
            conn.execute('BEGIN IMMEDIATE')
        serial = int(gid.get('serial') or 0)
        if (serial <= 0 or conn.execute('select 1 from gameplayers where serial=?', (serial,)).fetchone()
                or conn.execute('select 1 from owned_identity_aliases where old_serial=? limit 1', (serial,)).fetchone()):
            gid = {**gid, 'serial': player_identity.allocate(conn)}
            player['gameplayer_id'] = gid
    key = (int(gid.get("id", 0)), int(gid.get("serial", 0)))
    # Normalize imported cards before they become persistent state.  External
    # databases use a different booster numbering scheme; keeping those values
    # in the game's parameter slots makes the lineup renderer ignore the card's
    # innate boosters even when initial_booster_id_list is correct.
    player = normalize_player(player)
    cur = conn.execute(
        "insert or ignore into gameplayers(id, serial, json, source, acquired_at) values (?,?,?,?,?)",
        (key[0], key[1], json.dumps(player, ensure_ascii=False), source, now()),
    )
    if cur.rowcount > 0:
        import player_training
        player_training.ensure_progression_ledger(conn, player)
    return cur.rowcount > 0


# --- draw pool -------------------------------------------------------------
def add_to_pool(conn: sqlite3.Connection, player: dict, source: str) -> bool:
    """Register a player as obtainable. Rarity comes from default_rarity."""
    gid = player.get("gameplayer_id") or {}
    if not isinstance(gid, dict) or "id" not in gid:
        return False
    player = normalize_player(player)
    rar = int(player.get("default_rarity", 1) or 1)
    cur = conn.execute(
        "insert or ignore into player_pool(id, serial, rarity, json, source) values (?,?,?,?,?)",
        (int(gid.get("id", 0)), int(gid.get("serial", 0)), rar,
         json.dumps(player, ensure_ascii=False), source),
    )
    return cur.rowcount > 0


def pool_size(conn: sqlite3.Connection) -> int:
    return conn.execute("select count(*) from player_pool").fetchone()[0]


def pool_by_rarity(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("select rarity, count(*) from player_pool group by rarity").fetchall()
    return {int(r): int(c) for r, c in rows}


def set_agent_weight(conn: sqlite3.Connection, agent_id: int, rarity: int, weight: int) -> None:
    conn.execute(
        "insert or replace into agent_pool(agent_id, rarity, weight) values (?,?,?)",
        (int(agent_id), int(rarity), int(weight)),
    )


def agent_weights(conn: sqlite3.Connection, agent_id: int) -> dict[int, int]:
    rows = conn.execute(
        "select rarity, weight from agent_pool where agent_id=?", (int(agent_id),)
    ).fetchall()
    return {int(r): int(w) for r, w in rows}


def draw_players(conn: sqlite3.Connection, agent_id: int, count: int) -> list[dict]:
    """Draw `count` distinct players from the pool for this agent.

    Selection is two-stage, mirroring how the real gacha works:
      1. pick a rarity using the agent's weights (uniform fallback)
      2. pick a uniformly random player of that rarity

    Distinctness is enforced across the whole draw so a single 10x pull never
    returns the same player twice. Without a populated pool this returns an
    empty list, which the caller must surface rather than fake.
    """
    import random

    if count <= 0:
        return []
    total = pool_size(conn)
    if total == 0:
        return []

    weights = agent_weights(conn, agent_id)
    if weights:
        # only keep rarities that actually have players in the pool
        available = pool_by_rarity(conn)
        weights = {r: w for r, w in weights.items() if available.get(r, 0) > 0}
    if not weights:
        # uniform across whatever rarities exist
        weights = {r: 1 for r in pool_by_rarity(conn)}
    if not weights:
        return []

    rarities = list(weights.keys())
    wlist = [max(0, weights[r]) for r in rarities]
    if sum(wlist) <= 0:
        wlist = [1] * len(rarities)

    chosen: list[dict] = []
    seen: set[tuple[int, int]] = set()
    max_attempts = count * 40

    for _ in range(max_attempts):
        if len(chosen) >= count:
            break
        rarity = random.choices(rarities, weights=wlist, k=1)[0]
        rows = conn.execute(
            "select id, serial, json from player_pool where rarity=? order by random() limit 8",
            (int(rarity),),
        ).fetchall()
        for rid, rser, rjson in rows:
            key = (int(rid), int(rser))
            if key in seen:
                continue
            seen.add(key)
            try:
                chosen.append(json.loads(rjson))
            except Exception:
                continue
            break

    # if the pool is smaller than the requested count, top up from anywhere
    if len(chosen) < count:
        rows = conn.execute(
            "select id, serial, json from player_pool order by random() limit ?",
            (count * 3,),
        ).fetchall()
        for rid, rser, rjson in rows:
            if len(chosen) >= count:
                break
            key = (int(rid), int(rser))
            if key in seen:
                continue
            seen.add(key)
            try:
                chosen.append(json.loads(rjson))
            except Exception:
                continue

    # a fresh copy each time, so the club store never aliases pool rows
    out: list[dict] = []
    for p in chosen[:count]:
        q = json.loads(json.dumps(p))
        q.setdefault("acquisition_date", int(dt.datetime.now().timestamp()))
        out.append(q)
    return out


def spend(conn: sqlite3.Connection, gp: int = 0, coin: int = 0) -> None:
    set_meta(conn, "gp", str(max(0, int(get_meta(conn, "gp", "0")) - gp)))
    set_meta(conn, "coin", str(max(0, int(get_meta(conn, "coin", "0")) - coin)))


# --- season points / rentals / campaign pass --------------------------------
# Season points (赛季点数) are their own currency: the exchange and the rental
# shop spend them, and they are not gp/coin. Kept in `meta` because there is one
# balance per account, with the rentals table holding what was bought.
def season_points(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "season_point", "0") or 0)


def set_season_points(conn: sqlite3.Connection, value: int) -> int:
    value = max(0, int(value))
    set_meta(conn, "season_point", str(value))
    return season_points(conn)


def spend_season_points(conn: sqlite3.Connection, cost: int) -> bool:
    """Deduct `cost`, or refuse (False) when the balance does not cover it.

    A refusal must not write anything: a half-applied exchange would hand the
    player the card without the charge, or the charge without the card.
    """
    cost = max(0, int(cost))
    have = season_points(conn)
    if have < cost:
        return False
    set_season_points(conn, have - cost)
    return True


def add_rental(conn: sqlite3.Connection, player: dict, item_id: int, cost: int,
               expiration_date: int) -> tuple[int, int]:
    """Register a rented player and return its identity key."""
    key = player_key(player)
    conn.execute(
        "insert or replace into rentals(id, serial, item_id, cost, expiration_date, rented_at) "
        "values (?,?,?,?,?,?)",
        (key[0], key[1], int(item_id), int(cost), int(expiration_date), now()),
    )
    return key


def rentals(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": int(i), "serial": int(s), "item_id": item_id, "cost": int(cost),
         "expiration_date": int(exp), "rented_at": at}
        for i, s, item_id, cost, exp, at in conn.execute(
            "select id, serial, item_id, cost, expiration_date, rented_at from rentals")
    ]


def campaign_pass_received(conn: sqlite3.Connection) -> int:
    """How many campaign-pass rewards have been claimed."""
    return int(get_meta(conn, "campaign_pass_received", "0") or 0)


def set_campaign_pass_received(conn: sqlite3.Connection, value: int) -> int:
    value = max(0, int(value))
    set_meta(conn, "campaign_pass_received", str(value))
    return value


def save_squad(conn: sqlite3.Connection, squad: dict) -> None:
    if "squad_id" in squad:
        conn.execute(
            "insert or replace into squads(squad_id, json) values (?,?)",
            (squad["squad_id"], json.dumps(squad, ensure_ascii=False)),
        )


def squad_with_owned_players(conn: sqlite3.Connection, squad: dict) -> dict:
    """Point the squad's player slots at the players we actually own.

    The captured squad references the placeholder players of the account it came
    from; the client renders those as generic cards. Mapping the slots onto the
    store's players makes the drawn players show up on the squad screen.
    Players parked in the warehouse are excluded (they are not squad candidates).
    """
    owned = squad_candidates(conn)
    if not owned:
        return squad
    out = json.loads(json.dumps(squad))
    pl = (out.get("squad_data") or {}).get("player_list") or []
    for i, slot in enumerate(pl):
        p = owned[i % len(owned)]
        slot["gameplayer_id"] = p.get("gameplayer_id")
        if not slot.get("uniform_number"):
            slot["uniform_number"] = p.get("uniform_number", i + 1)
    return out


# --- squad / player helpers (E13, E16) -------------------------------------
def player_key(player: dict) -> tuple[int, int]:
    """The identity the client uses for a player: (id, serial)."""
    gid = player.get("gameplayer_id") or {}
    return (int(gid.get("id", 0)), int(gid.get("serial", 0)))


def player_by_key(conn: sqlite3.Connection, gid: int, serial: int | None = None) -> dict | None:
    """Look up an owned player. serial=None matches on the id alone.

    The client addresses a player by the {id, serial} pair, but the serial it
    holds can differ from the stored one (captures use serial 0 for everything),
    so callers fall back to an id-only lookup.
    """
    if serial is None:
        row = conn.execute(
            "select json from gameplayers where id=? order by serial limit 1", (int(gid),)
        ).fetchone()
    else:
        row = conn.execute(
            "select json from gameplayers where id=? and serial=?", (int(gid), int(serial))
        ).fetchone()
    return json.loads(row[0]) if row else None


def upsert_player(conn: sqlite3.Connection, player: dict, source: str = "update") -> tuple[int, int]:
    """Write a modified player back, keeping (id, serial) as the identity."""
    key = player_key(player)
    if player_identity.enabled(conn) and key[1] <= 0:
        raise ValueError('update requires a canonical owned instance')
    conn.execute(
        "insert into gameplayers(id, serial, json, source, acquired_at) values (?,?,?,?,?) "
        "on conflict(id, serial) do update set json=excluded.json, source=excluded.source",
        (key[0], key[1], json.dumps(player, ensure_ascii=False), source, now()),
    )
    return key


# --- wire shape ------------------------------------------------------------
# Keys starting with "_" are ours (bookkeeping); they never go to the client.
INTERNAL_PREFIX = "_"

# The player-list commands carry two fields the LOGIN reply does not: measured
# 20 keys vs 18 (additional_position / additional_skill_id). They hold the extra
# positions and the extra skill a card can be trained into, empty until it is.
LIST_EXTRA_FIELDS: dict = {"additional_position": [], "additional_skill_id": []}


def wire_player(player: dict, shape: str = "login") -> dict:
    """The player object as the client expects it for a given command.

    The replies that carry players do NOT share one shape:
      LOGIN / ENTRY_INFO       18 keys
      PROCURABLE / WAREHOUSE   20 keys  (+ additional_position, additional_skill_id)
    Native 147-item parameters are preserved. Legacy 145 and identified ZIRU
    149-item layouts are converted to the 11.1.0 enum (E23); its four-zero
    extension is not part of the owned LOGIN schema. Unknown layouts and
    trained values are preserved. Never add invented client fields (E16).
    """
    p = {k: v for k, v in player.items() if not str(k).startswith(INTERNAL_PREFIX)}
    p = json.loads(json.dumps(p, ensure_ascii=False))
    team, national = split_base_teams(p.get("initial_base_team_id", 0),
                                      p.get("initial_base_national_team_id", 0))
    if team != p.get("initial_base_team_id", 0):
        p["initial_base_team_id"] = team
        p["initial_base_national_team_id"] = national
    if "parameters" in p:
        p["parameters"] = owned_parameters(p)
    if shape == "list":
        for key, default in LIST_EXTRA_FIELDS.items():
            p.setdefault(key, json.loads(json.dumps(default)))
    player_skills.apply_wire(p, shape)
    return p


def main_squad_id(conn: sqlite3.Connection) -> int:
    return int(get_meta(conn, "main_squad_id", "1") or 1)


def set_main_squad(conn: sqlite3.Connection, squad_id: int) -> None:
    squad_id = int(squad_id)
    if conn.execute("select 1 from squads where squad_id=?", (squad_id,)).fetchone() is None:
        raise ValueError("unknown squad")
    set_meta(conn, "main_squad_id", str(squad_id))


def set_squad_name(conn: sqlite3.Connection, squad_id: int, name: str) -> None:
    row = conn.execute("select json from squads where squad_id=?", (int(squad_id),)).fetchone()
    if row is None:
        raise ValueError("unknown squad")
    elem = json.loads(row[0])
    elem["name"] = str(name)
    save_squad(conn, elem)


def create_squad(conn: sqlite3.Connection, req: dict | None = None) -> dict:
    """Create the lowest free game-plan slot, cloning the active plan."""
    used = {int(r[0]) for r in conn.execute("select squad_id from squads")}
    capacity = int(get_meta(conn, "gameplan_slots_initialized", str(GAMEPLAN_CAPACITY)) or GAMEPLAN_CAPACITY)
    requested = (req or {}).get("squad_id")
    try:
        requested = int(requested) if requested is not None else None
    except (TypeError, ValueError):
        requested = None
    # The client sends the target slot when it creates a visible blank card.
    # Migration pre-populates those cards so LOGIN can advertise all 100, but
    # a requested in-range slot is still a valid create target and is replaced
    # with a fresh clone below.
    if requested is not None and not 1 <= requested <= capacity:
        raise ValueError("gameplan capacity reached")
    sid = requested
    if sid is None:
        sid = next((i for i in range(1, capacity + 1) if i not in used), None)
    if sid is None:
        raise ValueError("gameplan capacity reached")
    active = squads(conn)
    source_id = main_squad_id(conn)
    source = next((s for s in active if int(s.get("squad_id") or 0) == source_id), None)
    if source is None:
        source = active[0] if active else {"squad_id": 1, "name": "Match Plan", "squad_data": {}}
    clone = json.loads(json.dumps(source, ensure_ascii=False))
    clone["squad_id"] = sid
    requested_name = (req or {}).get("squad_name") or (req or {}).get("name")
    clone["name"] = str(requested_name) if isinstance(requested_name, str) and requested_name else f"Match Plan {sid}"
    save_squad(conn, clone)
    if not active or source_id not in used:
        set_meta(conn, "main_squad_id", str(sid))
    return clone


def delete_squad(conn: sqlite3.Connection, squad_id: int) -> int:
    """Delete one plan and move the active pointer if necessary."""
    squad_id = int(squad_id)
    if conn.execute("select 1 from squads where squad_id=?", (squad_id,)).fetchone() is None:
        raise ValueError("unknown squad")
    conn.execute("delete from squads where squad_id=?", (squad_id,))
    if main_squad_id(conn) == squad_id:
        row = conn.execute("select squad_id from squads order by squad_id limit 1").fetchone()
        if row:
            set_meta(conn, "main_squad_id", str(int(row[0])))
    return int(get_meta(conn, "main_squad_id", "1") or 1)


def save_squad_info(conn: sqlite3.Connection, req: dict) -> dict:
    """Persist a CMD_SET_MYCLUB_SQUAD_INFO request.

    The client sends `squad_id` and `squad_data` at the top level; the old
    handler looked for `squad_info` / `squad`, so nothing was ever written and
    every lineup edit was lost on the next login (E13).

    Careful with types: the request and the response do NOT share a schema for
    every field. `base_team` is an object in the request but a LIST in
    entry_info.squad_list - copying it through verbatim made the client's
    CMDDeserializer SIGSEGV on login (measured 2026-10-14 21:44).
    """
    squad_id = int(req.get("squad_id") or main_squad_id(conn))
    row = conn.execute("select json from squads where squad_id=?", (squad_id,)).fetchone()
    if row is None:
        raise ValueError("unknown squad")
    elem = json.loads(row[0])
    if isinstance(req.get("squad_data"), dict):
        if player_identity.enabled(conn):
            player_identity.validate_squad(conn, req['squad_data'])
        # ZIRU merges changed top-level game-plan fields so a partial client
        # update cannot erase the second formation or match-plan settings.
        patch = req["squad_data"]
        current = elem.setdefault("squad_data", {})
        for key, value in patch.items():
            current[key] = json.loads(json.dumps(value, ensure_ascii=False))
    elif isinstance(req.get("squad_info"), dict):
        if player_identity.enabled(conn):
            player_identity.validate_squad(conn, req['squad_info'])
        patch = req["squad_info"]
        current = elem.setdefault("squad_data", {})
        for key, value in patch.items():
            current[key] = json.loads(json.dumps(value, ensure_ascii=False))
    base_team = req.get("base_team")
    old_team = base_team_id_of(elem.get("base_team"))
    if isinstance(base_team, list):
        elem["base_team"] = base_team
    elif isinstance(base_team, dict):
        elem["base_team"] = [base_team]     # response schema is a list of objects
    name = req.get("squad_name") or req.get("name")
    if isinstance(name, str) and name:
        elem["name"] = name
    elem["squad_id"] = squad_id
    save_squad(conn, elem)
    retarget_uniforms(conn, old_team, base_team_id_of(elem.get("base_team")))
    return elem


def entry_roster(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Active cards for account snapshots; warehouse cards have their own list.

    E37 lifts the old 200-card snapshot limit to the unlocked 10000 capacity.
    An explicit limit remains available for diagnostics. Large snapshots need
    client regression testing (historical E18 asset-loading crash).
    """
    if limit is None:
        limit = PLAYER_CAPACITY
    all_players = squad_candidates(conn)
    active_keys = {player_key(p) for p in all_players}
    if limit <= 0 or len(all_players) <= limit:
        return all_players
    wanted: set[tuple[int, int]] = set()
    for sq in squads(conn):
        for slot in (sq.get("squad_data") or {}).get("player_list") or []:
            gid = (slot or {}).get("gameplayer_id") or {}
            if isinstance(gid, dict) and int(gid.get("id", 0) or 0) > 0:
                wanted.add((int(gid["id"]), int(gid.get("serial") or 0)))
    out: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for p in all_players:                       # squad members first
        k = player_key(p)
        if k in wanted and k not in seen:
            out.append(p)
            seen.add(k)
    recent = [json.loads(r[0]) for r in conn.execute(
        "select json from gameplayers order by acquired_at desc, rowid desc")]
    for p in recent:                            # include newly signed cards after relogin
        if len(out) >= limit:
            break
        k = player_key(p)
        if k not in seen and k in active_keys:
            out.append(p)
            seen.add(k)
    return out[:limit]


def resolve_squad(conn: sqlite3.Connection, squad: dict, pool: list[dict] | None = None) -> dict:
    """Return a squad whose every slot points at a player from `pool`.

    The seeded squad came from a captured account, so its slots reference that
    account's placeholder players and the client renders blank cards (E02). The
    fix used to be "remap every slot by index", which is wrong once the user can
    edit the lineup: it would overwrite the saved arrangement on every login.

    So a slot we own keeps its player (the serial is normalised to the stored
    one, which is what the client looks up), and only unresolvable slots fall
    back to the next unused player. Identity is the (id, serial) pair, not the
    id: the same card can be owned twice with different serials.

    `pool` is the roster being served alongside (see entry_roster) - a slot may
    only reference a player the client can actually find in that list.
    """
    out = json.loads(json.dumps(squad, ensure_ascii=False))
    # Guard the list-typed fields: a bare object here (a request-shape value that
    # slipped into the store) makes the client's deserializer SIGSEGV on login.
    for key in ("base_team", "coach"):
        if isinstance(out.get(key), dict):
            out[key] = [out[key]]
    owned = pool if pool is not None else players(conn)
    if not owned:
        return out
    by_key: dict[tuple[int, int], dict] = {}
    by_id: dict[int, dict] = {}
    for p in owned:
        k = player_key(p)
        by_key.setdefault(k, p)
        by_id.setdefault(k[0], p)
    pl = (out.get("squad_data") or {}).get("player_list") or []
    taken: set[tuple[int, int]] = set()
    for i, slot in enumerate(pl):
        if not isinstance(slot, dict):
            continue
        gid = slot.get("gameplayer_id")
        if not isinstance(gid, dict):
            continue
        key = (int(gid.get("id", 0) or 0), int(gid.get("serial") or 0))
        if key[0] <= 0:
            continue            # the client's empty slot is id 0; leave it empty
        p = by_key.get(key)
        if p is not None and player_key(p) in taken:
            p = None
        if p is None:
            p = by_id.get(key[0])           # the serial can differ from the stored one
            if p is not None and player_key(p) in taken:
                p = None
        if p is None:
            p = next((q for q in owned if player_key(q) not in taken), None)
        if p is None:
            continue
        taken.add(player_key(p))
        slot["gameplayer_id"] = p.get("gameplayer_id")
        if not slot.get("uniform_number"):
            slot["uniform_number"] = p.get("uniform_number", i + 1)
    return out


# --- ability points (能力点分配) -------------------------------------------
def record_careerplan(conn: sqlite3.Connection, gid: int, serial: int, category, points: int) -> None:
    conn.execute(
        "insert into careerplan(id, serial, category, points, at) values (?,?,?,?,?)",
        (int(gid), int(serial or 0), str(category), int(points), now()),
    )


def careerplan_log(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "select id, serial, category, points, at from careerplan order by rowid"
    ).fetchall()
    return [{"id": r[0], "serial": r[1], "category": r[2], "points": r[3], "at": r[4]} for r in rows]


def careerplan_counts(conn: sqlite3.Connection, gid: int, serial: int | None = None) -> dict[str, int]:
    """Points allocated per category for one player (raw category strings)."""
    if serial is None:
        rows = conn.execute('select category,sum(points) from careerplan where id=? group by category', (int(gid),)).fetchall()
    else:
        rows = conn.execute('select category,sum(points) from careerplan where id=? and serial=? group by category', (int(gid),int(serial))).fetchall()
    return {str(r[0]): int(r[1] or 0) for r in rows}


def enable_owned_identity(conn):
    return player_identity.migrate(conn)


# --- warehouse (储备球员 / 更衣室) -----------------------------------------
def move_to_warehouse(conn: sqlite3.Connection, gid: int, serial: int) -> bool:
    cur = conn.execute("insert or ignore into warehouse(id, serial) values (?,?)", (gid, serial))
    return cur.rowcount > 0


def move_from_warehouse(conn: sqlite3.Connection, gid: int, serial: int) -> bool:
    cur = conn.execute("delete from warehouse where id=? and serial=?", (gid, serial))
    return cur.rowcount > 0


def warehouse_players(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for gid, serial in conn.execute("select id, serial from warehouse order by id"):
        row = conn.execute("select json from gameplayers where id=? and serial=?", (gid, serial)).fetchone()
        if row:
            out.append(json.loads(row[0]))
    return out


def warehouse_count(conn: sqlite3.Connection) -> int:
    return conn.execute("select count(*) from warehouse w join gameplayers g "
                        "on g.id=w.id and g.serial=w.serial").fetchone()[0]


def in_warehouse(conn: sqlite3.Connection, gid: int, serial: int) -> bool:
    """Whether this owned instance is parked in the reserve slots."""
    return conn.execute("select 1 from warehouse where id=? and serial=?",
                        (int(gid), int(serial))).fetchone() is not None


# --- club kits (CMD_SET_MYCLUB_BRING_UNIFORM / CMD_START_GAME) -------------
# A kit id encodes its base team: id = base_team_id * UNIFORM_STRIDE + slot,
# slot 0 = home, 1 = away, 2 = goalkeeper. Derived from the corpora: captured
# uniform_list_home [2064384, 2064385, 2064386] / 16384 = 126 (that club's team),
# the account's set_uniform_list [1769472, 1769473, ...] / 16384 = 108, and the
# client's own picks in the live session (1687552 = 103 * 16384).
UNIFORM_STRIDE = 16384


def base_team_id_of(base_team) -> int:
    """The base_team_id out of a squad's base_team (list of objects) or object."""
    if isinstance(base_team, list) and base_team and isinstance(base_team[0], dict):
        return int(base_team[0].get("base_team_id") or 0)
    if isinstance(base_team, dict):
        return int(base_team.get("base_team_id") or 0)
    return 0


def club_team_id(conn: sqlite3.Connection) -> int:
    """The club's current base team: the saved squad's, else the recorded meta."""
    for sq in squads(conn):
        tid = base_team_id_of(sq.get("base_team"))
        if tid:
            return tid
    return int(get_meta(conn, "base_team_id", "0") or 0)


def default_uniforms(team: int) -> list[int]:
    """The home/away kit pair of a base team."""
    return [team * UNIFORM_STRIDE, team * UNIFORM_STRIDE + 1]


def set_uniforms(conn: sqlite3.Connection, ids: list[int]) -> None:
    set_meta(conn, "set_uniform_list", json.dumps([int(i) for i in ids]))


def stored_uniforms(conn: sqlite3.Connection) -> list[int] | None:
    raw = get_meta(conn, "set_uniform_list", "")
    try:
        ids = json.loads(raw) if raw else None
    except ValueError:
        return None
    if isinstance(ids, list) and ids and all(type(i) is int and i > 0 for i in ids):
        return ids
    return None


def equipped_uniforms(conn: sqlite3.Connection) -> list[int]:
    """The kits the club wears: the saved selection, else the team's own pair."""
    ids = stored_uniforms(conn)
    if ids:
        return ids
    team = club_team_id(conn)
    return default_uniforms(team) if team else []


def retarget_uniforms(conn: sqlite3.Connection, old_team: int, new_team: int) -> None:
    """A new base team wears its own kits until the player picks again.

    Mirrors the reference offline server (z27TeamChange resets the saved kits
    to the new team's ``kits``); without this a team change kept the previous
    team's selection.
    """
    if new_team and new_team != old_team:
        set_uniforms(conn, default_uniforms(new_team))


def apply_uniform_selection(conn: sqlite3.Connection, entry: dict) -> None:
    """Keep an entry_info snapshot's equipped-kit fields on our selection.

    Only fields the schema already carries are touched: the LOGIN snapshot has
    `set_uniform_list`/`is_set_invalid_uniform`, the SET_MYCLUB_ENTRY_INFO
    schema has neither (measured against the captured replies).
    """
    ids = equipped_uniforms(conn)
    if not ids:
        return
    if "set_uniform_list" in entry:
        entry["set_uniform_list"] = list(ids)
    if "is_set_invalid_uniform" in entry:
        entry["is_set_invalid_uniform"] = "NO"



def active_count(conn: sqlite3.Connection) -> int:
    """Owned instances that are NOT parked in the reserve slots.

    This is the number the client's capacity fields apply to; purchases stay
    active while it is within PLAYER_CAPACITY.
    """
    return conn.execute("select count(*) from gameplayers g where not exists "
                        "(select 1 from warehouse w where w.id=g.id and w.serial=g.serial)").fetchone()[0]


def squad_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Owned players that are not parked in the warehouse."""
    in_wh = {(r[0], r[1]) for r in conn.execute("select id, serial from warehouse")}
    return [p for p in players(conn)
            if (p["gameplayer_id"]["id"], p["gameplayer_id"]["serial"]) not in in_wh]


def summary(conn: sqlite3.Connection) -> str:
    return (
        f"gp={get_meta(conn,'gp','0')} coin={get_meta(conn,'coin','0')} "
        f"players={player_count(conn)} agents={len(agents(conn))} squads={len(squads(conn))} "
        f"user={get_meta(conn,'user_name')} team={get_meta(conn,'team_name')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    db = Path(a.db)
    if a.reset and db.exists():
        db.unlink()
        print(f"removed {db}")
    conn = connect(db)
    if a.init or a.reset:
        print("seeded:", seed(conn))
    if a.show or a.init or a.reset:
        print(summary(conn))
        for p in players(conn)[:5]:
            gid = p.get("gameplayer_id") or {}
            print(f"  player id={gid.get('id')} serial={gid.get('serial')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
