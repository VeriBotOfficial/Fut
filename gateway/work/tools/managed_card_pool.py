"""Opt-in explicit card pools; definitions, membership and owned instances differ.

Only agents installed by player_pool_workbench use this module. All writes for
one acquisition are serialized in BEGIN IMMEDIATE and committed together.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from pathlib import Path

DISPLAY_CONFIG = Path(__file__).resolve().parents[2] / 'outputs/reference/PACK_DISPLAY.json'

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_cards(
 card_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL,
 card_type TEXT NOT NULL, subtype TEXT, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS managed_agents(
 agent_id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
 replacement INTEGER NOT NULL, mode TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS managed_members(
 agent_id INTEGER NOT NULL REFERENCES managed_agents(agent_id),
 card_id TEXT NOT NULL REFERENCES managed_cards(card_id), weight INTEGER NOT NULL,
 PRIMARY KEY(agent_id,card_id));
CREATE TABLE IF NOT EXISTS managed_serials(card_id TEXT PRIMARY KEY,next_serial INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS managed_draw_receipts(
 user_id TEXT NOT NULL, rqid TEXT NOT NULL, fingerprint TEXT NOT NULL,
 players TEXT NOT NULL, PRIMARY KEY(user_id,rqid));
CREATE TABLE IF NOT EXISTS managed_consumed(
 agent_id INTEGER NOT NULL, card_id TEXT NOT NULL,
 PRIMARY KEY(agent_id,card_id));
"""


class PoolError(ValueError):
    pass


def can_reset(conn, agent_id: int) -> bool:
    """Whether a managed, non-replacement pool supports the native reset action."""
    if not is_managed(conn, agent_id):
        return False
    row = conn.execute("SELECT enabled,replacement FROM managed_agents WHERE agent_id=?",
                       (int(agent_id),)).fetchone()
    return bool(row and row[0] and not row[1])


def reset_agent(conn, agent_id: int, *, request=None) -> None:
    """Restore one finite pool to its original membership without touching owned cards."""
    if type(agent_id) is not int or agent_id <= 0:
        raise PoolError("invalid agent id")
    if conn.in_transaction:
        raise PoolError("caller must finish its transaction before resetting")
    request = request or {}
    key = (str(request.get('user_id', 0)), str(request.get('rqid')))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS managed_reset_receipts("
                     "user_id TEXT NOT NULL,rqid TEXT NOT NULL,agent_id INTEGER NOT NULL,"
                     "PRIMARY KEY(user_id,rqid))")
        if request.get('rqid') is not None:
            receipt = conn.execute("SELECT agent_id FROM managed_reset_receipts WHERE user_id=? AND rqid=?", key).fetchone()
            if receipt:
                if receipt[0] != agent_id:
                    raise PoolError("request id reused for a different reset")
                conn.commit()
                return
        if not can_reset(conn, agent_id):
            raise PoolError("agent does not support reset")
        conn.execute("CREATE TABLE IF NOT EXISTS managed_consumed("
                     "agent_id INTEGER NOT NULL,card_id TEXT NOT NULL,PRIMARY KEY(agent_id,card_id))")
        conn.execute("DELETE FROM managed_consumed WHERE agent_id=?", (int(agent_id),))
        # Retain draw receipts and serial counters: retries of an earlier draw
        # must not grant twice, and a new copy must get a fresh owned identity.
        if request.get('rqid') is not None:
            conn.execute("INSERT INTO managed_reset_receipts VALUES(?,?,?)", (*key, agent_id))
        import club_store as store
        store.log_event(conn, 'managed_pool_reset', {'agent_id': agent_id})
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def is_managed(conn, agent_id):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_agents'").fetchone()
    return bool(exists and conn.execute("SELECT 1 FROM managed_agents WHERE agent_id=?", (agent_id,)).fetchone())


# --- card library ----------------------------------------------------------
# managed_cards is the project's card registry: pool membership lives in
# managed_members. The 普通球员 (scout) list serves the whole registry, so a card
# stays signable even when it is in no gacha pool.

def library(conn) -> list[dict]:
    """Every registered card definition, in a stable order for paging."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_cards'").fetchone():
        return []
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM managed_cards ORDER BY card_id")]


def library_entry(conn, card_id) -> dict | None:
    """One registered card definition, or None when the id is not in the library."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_cards'").fetchone():
        return None
    row = conn.execute("SELECT payload FROM managed_cards WHERE card_id=?", (str(card_id),)).fetchone()
    return json.loads(row[0]) if row else None


def preview(conn, agent_id, *, include_consumed=False):
    config = conn.execute("SELECT enabled,replacement,mode,name FROM managed_agents WHERE agent_id=?", (agent_id,)).fetchone()
    if not config:
        raise PoolError("unknown managed agent")
    available = ''
    if not include_consumed and not config[1] and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_consumed'").fetchone():
        available = ' AND NOT EXISTS(SELECT 1 FROM managed_consumed x WHERE x.agent_id=m.agent_id AND x.card_id=m.card_id)'
    members = conn.execute("SELECT c.card_id,m.weight,c.payload,c.card_type,c.subtype FROM managed_members m JOIN managed_cards c ON c.card_id=m.card_id WHERE m.agent_id=? AND m.weight>0" + available + " ORDER BY c.card_id", (agent_id,)).fetchall()
    return {"agent_id": agent_id, "enabled": bool(config[0]), "replacement": bool(config[1]),
            "mode": config[2], "name": config[3],
            "members": [{"card_id": pid, "weight": weight, "gameplayer": json.loads(payload),
                         "card_type": kind, "subtype": subtype} for pid, weight, payload, kind, subtype in members]}


def counts(conn, agent_id, *, remaining=True):
    """Return live member counts in the response's five rarity buckets."""
    available = ''
    config = conn.execute('SELECT replacement FROM managed_agents WHERE agent_id=?', (agent_id,)).fetchone()
    if remaining and config and not config[0] and conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='managed_consumed'").fetchone():
        available = ' AND NOT EXISTS(SELECT 1 FROM managed_consumed x WHERE x.agent_id=m.agent_id AND x.card_id=m.card_id)'
    rows = conn.execute(
        "SELECT COALESCE(json_extract(c.payload, '$.default_rarity'),1),count(*) "
        "FROM managed_members m JOIN managed_cards c ON c.card_id=m.card_id "
        "WHERE m.agent_id=? AND m.weight>0" + available + " GROUP BY 1", (agent_id,)).fetchall()
    out = [0, 0, 0, 0, 0]
    for rarity, n in rows:
        if 1 <= int(rarity) <= 5:
            out[int(rarity) - 1] = int(n)
    return out


def draw_and_grant(conn, agent_id, count, *, rng=None, request=None):
    """Grant atomically; zero weights excluded; insufficient pools change nothing."""
    import club_store as store
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise PoolError("draw count must be an integer in 1..100")
    if conn.in_transaction:
        raise PoolError("caller must finish its transaction before drawing")
    conn.execute("CREATE TABLE IF NOT EXISTS managed_draw_receipts("
                 "user_id TEXT NOT NULL,rqid TEXT NOT NULL,fingerprint TEXT NOT NULL,"
                 "players TEXT NOT NULL,PRIMARY KEY(user_id,rqid))")
    conn.execute("CREATE TABLE IF NOT EXISTS managed_consumed("
                 "agent_id INTEGER NOT NULL,card_id TEXT NOT NULL,PRIMARY KEY(agent_id,card_id))")
    request = request or {}
    receipt_key = (str(request.get("user_id", 0)), str(request.get("rqid")))
    fingerprint = hashlib.sha256(json.dumps(
        [agent_id, count, request.get("buy_type")], separators=(",", ":")).encode()).hexdigest()
    rng = rng or random.SystemRandom()
    conn.execute("BEGIN IMMEDIATE")
    try:
        if request.get("rqid") is not None:
            row = conn.execute("SELECT fingerprint,players FROM managed_draw_receipts "
                               "WHERE user_id=? AND rqid=?", receipt_key).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise PoolError("request id reused for a different draw")
                import player_release
                saved = store.player_identity.canonical_request(conn, json.loads(row[1]))
                current = []
                for card in saved:
                    pid, serial = store.player_key(card)
                    if player_release.is_released(conn, pid, serial):
                        raise PoolError("draw receipt refers to a released player")
                    owned = store.player_by_key(conn, pid, serial)
                    if owned is None:
                        raise PoolError("draw receipt refers to a missing owned instance")
                    current.append(owned)
                conn.commit()
                return current
        pool = preview(conn, agent_id)
        if not pool["enabled"]:
            raise PoolError("managed agent disabled")
        candidates = list(pool["members"])
        if not candidates:
            raise PoolError("managed pool has no positive-weight members")
        if not pool["replacement"] and count > len(candidates):
            raise PoolError("count exceeds pool size without replacement")
        if pool["mode"] == "LEAGUE_POSITION":
            if count not in (1, 10):
                raise PoolError("league packs support single or ten draws")
            if request.get("buy_type", "BUY_BY_COIN") != "BUY_BY_COIN":
                raise PoolError("unsupported payment type")
            cost = 100 if count == 1 else 900
            balance = int(store.get_meta(conn, "coin", "0"))
            if balance < cost:
                raise PoolError("insufficient coin")
            store.set_meta(conn, "coin", str(balance - cost))
        drawn = []
        for _ in range(count):
            chosen = rng.choices(candidates, weights=[p["weight"] for p in candidates], k=1)[0]
            if not pool["replacement"]:
                candidates.remove(chosen)
            pid = int(chosen["card_id"])
            maximum = conn.execute("SELECT COALESCE(MAX(serial),0) FROM gameplayers WHERE id=?", (pid,)).fetchone()[0]
            next_row = conn.execute("SELECT next_serial FROM managed_serials WHERE card_id=?", (str(pid),)).fetchone()
            serial = (store.player_identity.allocate(conn) if store.player_identity.enabled(conn)
                      else max(maximum + 1, next_row[0] if next_row else 1, 1))
            if serial >= 2147483647:
                raise PoolError("serial range exhausted")
            conn.execute("INSERT INTO managed_serials VALUES(?,?) ON CONFLICT(card_id) DO UPDATE SET next_serial=excluded.next_serial", (str(pid), serial + 1))
            player = copy.deepcopy(chosen["gameplayer"])
            player["gameplayer_id"] = {"id": pid, "serial": serial}
            for field in ("games", "goal", "assist", "red_card"):
                player[field] = 0
            player["acquisition_date"] = int(time.time())
            player["is_lock"] = "NO"
            if not store.add_player(conn, player, f"managed_draw:{agent_id}"):
                raise PoolError("owned instance conflict")
            if not pool['replacement']:
                conn.execute('INSERT INTO managed_consumed VALUES(?,?)', (agent_id, str(pid)))
            # add_player normalizes a persistent copy; return that exact instance.
            drawn.append(store.player_by_key(conn, pid, player["gameplayer_id"]["serial"]))
        store.log_event(conn, "draw", {"agent_id": agent_id, "count": count, "returned": len(drawn),
            "added": len(drawn), "pool": len(pool["members"]), "degraded": False,
            "managed": True, "mode": pool["mode"], "replacement": pool["replacement"]})
        if request.get("rqid") is not None:
            conn.execute("INSERT INTO managed_draw_receipts VALUES(?,?,?,?)",
                         (*receipt_key, fingerprint, json.dumps(drawn, ensure_ascii=False)))
        conn.commit()
        return drawn
    except Exception:
        conn.rollback()
        raise


def decorate_agents(conn, agents):
    """Use the exact positive membership for managed pickup previews."""
    import club_store as store
    import developed_preview
    display = json.loads(DISPLAY_CONFIG.read_text(encoding='utf8')) if DISPLAY_CONFIG.exists() else {}
    configured = display.get('packs', {})
    result = copy.deepcopy(agents)
    preview_on = developed_preview.enabled(conn)
    boosts = developed_preview.coach_boosts(conn) if preview_on else {}
    for agent in result:
        agent_id = int(agent.get("agent_id") or 0)
        if is_managed(conn, agent_id):
            pool = preview(conn, agent_id)
            members = pool["members"] if pool["enabled"] else []
            buckets = counts(conn, agent_id) if pool["enabled"] else [0] * 5
            ranked = sorted(members, key=lambda p: (
                -(p["gameplayer"].get("_overall") or 0), -int(p["card_id"])))
            by_id = {str(p['card_id']): p for p in members}
            chosen = configured.get(str(agent_id), [])
            featured = [by_id[str(pid)] for pid in chosen if str(pid) in by_id][:3]
            featured += [p for p in ranked if p not in featured][:3-len(featured)]
            agent["pickup_list"] = [
                store.wire_player(developed_preview.developed(p["gameplayer"], boosts) if preview_on
                                  else p["gameplayer"], "list")
                for p in featured]
            agent["banner_a_pickup_list"] = copy.deepcopy(agent["pickup_list"][:3])
            agent["banner_c_pickup_list"] = copy.deepcopy(agent["pickup_list"][:3])
            agent["pickup_cardtype_list"] = []
            base = counts(conn, agent_id, remaining=False)
            agent["player_list_total"] = sum(base)
            agent["remaining_player_count"] = list(buckets)
            agent["base_player_count"] = base
            agent["use_count"] = sum(base) - sum(buckets)
            # Captured acquired_list belongs to the original account. Never
            # retain its stale display rows after a reset (or a fresh save).
            # Actual finite-pool availability is authoritative in consumed.
            agent["acquired_list"] = []
            agent["can_reset"] = "YES" if can_reset(conn, agent_id) else "NO"
            agent["probability"] = "0" if not members else f"{100.0 / len(members):.4f}"
            agent["title"] = pool["name"]
    return result
