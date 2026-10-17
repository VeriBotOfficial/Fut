# -*- coding: utf-8 -*-
"""Club Pack (Player Pack / 组合包) management and protocol handling.

Supports 10 elite club packs (23 players per club, 0 standard cards, priority to Epic/Showtime/BigTime):
- Arsenal (101)
- Real Madrid (109)
- FC Barcelona (108)
- Manchester United (100)
- Manchester City (173)
- Liverpool (103)
- Chelsea (102)
- AC Milan (121)
- Inter Milan (119)
- Juventus (120)
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

import club_store as store

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PACKS_CONFIG_FILE = ROOT / "outputs" / "reference" / "top10_club_packs.json"
CONTRACT_PACK_BANNER_B = "/banner/priority/pack/201791712e451e810cc207d2f7771fdf50f1bee15145d9c869d1a2fddfcdd6fa"

_CACHED_PACKS: list[dict] | None = None


def load_packs_config() -> list[dict]:
    """Load pre-generated 10 club packs configuration."""
    global _CACHED_PACKS
    if _CACHED_PACKS is not None:
        return _CACHED_PACKS
    if PACKS_CONFIG_FILE.exists():
        try:
            with open(PACKS_CONFIG_FILE, "r", encoding="utf-8") as f:
                _CACHED_PACKS = json.load(f)
                return _CACHED_PACKS
        except Exception:
            logger.exception("Failed to load top10_club_packs.json")
    return []


def is_enabled(conn: sqlite3.Connection) -> bool:
    """Returns True if the club pack feature is enabled in meta."""
    return store.get_meta(conn, "club_pack_enabled", "0") == "1"


def enable(conn: sqlite3.Connection) -> None:
    """Enables club pack in the database."""
    store.set_meta(conn, "club_pack_enabled", "1")
    conn.commit()


def disable(conn: sqlite3.Connection) -> None:
    """Disables club pack in the database."""
    store.set_meta(conn, "club_pack_enabled", "0")
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS pack_purchase_receipts ("
        "user_id TEXT NOT NULL,"
        "rqid TEXT NOT NULL,"
        "pack_id INTEGER NOT NULL,"
        "presents_json TEXT NOT NULL,"
        "PRIMARY KEY(user_id, rqid))"
    )
    columns = {row[1] for row in conn.execute('PRAGMA table_info(pack_purchase_receipts)')}
    if 'request_hash' not in columns:
        conn.execute('ALTER TABLE pack_purchase_receipts ADD COLUMN request_hash TEXT')


def _current_presents(conn, presents):
    """A retry returns current exact instances, never stale or released cards."""
    import player_release
    rows = store.player_identity.canonical_request(conn, copy.deepcopy(presents))
    for index, present in enumerate(rows, start=1):
        present['present_id'] = index
        if present.get('present_type') != 'PLAYER':
            continue
        info = present.get('player_info') or {}
        pid, serial = store.player_key(info.get('gameplayer') or {})
        if player_release.is_released(conn, pid, serial):
            raise ValueError('purchase refers to a released player')
        player = store.player_by_key(conn, pid, serial)
        if player is None:
            raise ValueError('purchase refers to a missing owned instance')
        info['gameplayer'] = store.wire_player(player, 'login')
        info['is_warehouse'] = 'YES' if store.in_warehouse(conn, pid, serial) else 'NO'
        present['player_info'] = info
    return rows


def get_pack_list(conn: sqlite3.Connection) -> list[dict]:
    """Returns all 10 club packs with live purchase status."""
    packs = load_packs_config()
    out = []
    repeatable = store.get_meta(conn, "pack_repeatable", "1") == "1"
    for p in packs:
        p_copy = copy.deepcopy(p)
        pack_id = p_copy["pack_id"]
        purchased = store.get_meta(conn, f"pack_{pack_id}_purchased", "0") == "1"
        p_copy["purchase_able_num"] = 1 if (repeatable or not purchased) else 0
        # remove summary helper field from wire payload
        p_copy.pop("player_roster_summary", None)
        p_copy.pop("team_id", None)
        out.append(p_copy)
    return out


def apply_contract_info(conn: sqlite3.Connection, resp: dict) -> dict:
    """Enables the pack tab in CMD_GET_MYCLUB_CONTRACT_INFO if enabled."""
    if is_enabled(conn):
        packs = get_pack_list(conn)
        resp["playerpack_num"] = len(packs)
        resp["banner_playerpack_b_name"] = CONTRACT_PACK_BANNER_B
        resp["_custom_pack"] = True
    else:
        resp["playerpack_num"] = 0
        resp["banner_playerpack_b_name"] = ""
    return resp


def apply_pack_list(conn: sqlite3.Connection, resp: dict, req: dict) -> dict:
    """Populates pack_list in CMD_GET_MYCLUB_PACK_LIST if enabled."""
    if is_enabled(conn):
        resp["pack_list"] = get_pack_list(conn)
        resp["note_text"] = ""
        resp["_custom_pack"] = True
    else:
        resp["pack_list"] = []
        resp["note_text"] = ""
    return resp


def handle_buy_pack(conn: sqlite3.Connection, resp: dict, req: dict) -> dict:
    """Grant a pack once and return its already committed current instances.

    Keep the existing added_present_items_list response surface. The bundled
    corpus has no purchase/receive-present capture, so do not invent new native
    player-list or mailbox fields to force a UI refresh.
    """
    import player_training
    resp.pop('added_present_items_list', None)
    if conn.in_transaction:
        raise ValueError('finish transaction before buying pack')
    _ensure_schema(conn)
    try:
        raw_id = req.get('pack_id')
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
            raise ValueError('invalid pack id')
        pack_id = int(raw_id)
        buy_type = str(req.get('buy_type') or 'BUY_BY_GP')
        if buy_type not in ('BUY_BY_GP', 'BUY_BY_COIN'):
            raise ValueError('invalid payment type')
        user_id = str(req.get('user_id', 0))
        rqid = str(req.get('rqid')) if req.get('rqid') is not None else None
        key = (user_id, rqid)
        fingerprint = hashlib.sha256(json.dumps([pack_id, buy_type]).encode()).hexdigest()
        conn.execute('BEGIN IMMEDIATE')
        if rqid is not None:
            receipt = conn.execute(
                'SELECT pack_id,presents_json,request_hash FROM pack_purchase_receipts '
                'WHERE user_id=? AND rqid=?', key).fetchone()
            if receipt:
                if receipt[0] != pack_id or (receipt[2] and receipt[2] != fingerprint):
                    raise ValueError('request id reused for a different purchase')
                presents = _current_presents(conn, json.loads(receipt[1]))
                conn.commit()
                resp.update(result='NOERR', added_present_items_list=presents,
                            gp=int(store.get_meta(conn, 'gp', '0')),
                            coin_info={'coin': int(store.get_meta(conn, 'coin', '0'))},
                            warehouse_num=store.warehouse_count(conn))
                return resp
        if not is_enabled(conn):
            raise ValueError('pack feature disabled')
        target = next((p for p in load_packs_config() if p['pack_id'] == pack_id), None)
        if target is None:
            raise ValueError('unknown pack')
        if (store.get_meta(conn, f'pack_{pack_id}_purchased', '0') == '1'
                and store.get_meta(conn, 'pack_repeatable', '1') != '1'):
            raise ValueError('pack already purchased')
        price = int(target['price']['coin_price' if buy_type == 'BUY_BY_COIN' else 'gp_price'])
        currency = 'coin' if buy_type == 'BUY_BY_COIN' else 'gp'
        if price < 0:
            raise ValueError('invalid pack price')
        if int(store.get_meta(conn, currency, '0')) < price:
            resp['result'] = 'ERR_INSUFFICIENT_COIN' if currency == 'coin' else 'ERR_INSUFFICIENT_GP'
            conn.rollback()
            return resp
        store.spend(conn, **{currency: price})
        added = []
        now_ts = int(time.time())
        for index, present in enumerate(target.get('present_info', []), start=1):
            item = copy.deepcopy(present)
            item['present_id'] = index
            if item.get('present_type') == 'PLAYER':
                card = copy.deepcopy((item.get('player_info') or {}).get('gameplayer') or {})
                cid = int((card.get('gameplayer_id') or {}).get('id') or 0)
                if cid <= 0:
                    raise ValueError('invalid pack card')
                serial = store.player_identity.allocate(conn)
                card['gameplayer_id'] = {'id': cid, 'serial': serial}
                card.update(acquisition_date=now_ts, is_lock='NO')
                for field in ('games', 'goal', 'assist', 'red_card'):
                    card[field] = 0
                if not store.add_player(conn, card, f'pack:{pack_id}:{cid}'):
                    raise ValueError('duplicate or invalid player')
                saved = store.player_by_key(conn, cid, serial)
                # A full-development JSON without its separate journal has no
                # resettable points. Recover only an exact known build.
                player_training.ensure_progression_ledger(conn, saved)
                reserved = store.active_count(conn) > store.PLAYER_CAPACITY
                if reserved:
                    store.move_to_warehouse(conn, cid, serial)
                item['player_info']['gameplayer'] = store.wire_player(saved, 'login')
                item['player_info']['is_warehouse'] = 'YES' if reserved else 'NO'
            added.append(item)
        store.set_meta(conn, f'pack_{pack_id}_purchased', '1')
        if rqid is not None:
            conn.execute('INSERT INTO pack_purchase_receipts '
                         '(user_id,rqid,pack_id,presents_json,request_hash) VALUES(?,?,?,?,?)',
                         (*key, pack_id, json.dumps(added), fingerprint))
        store.log_event(conn, 'pack_buy', {'pack_id': pack_id, 'buy_type': buy_type,
                                         'cost': price, 'presents_count': len(added)})
        conn.commit()
        resp.update(result='NOERR', added_present_items_list=added,
                    gp=int(store.get_meta(conn, 'gp', '0')),
                    coin_info={'coin': int(store.get_meta(conn, 'coin', '0'))},
                    warehouse_num=store.warehouse_count(conn))
        return resp
    except (ValueError, TypeError, KeyError):
        conn.rollback()
        resp['result'] = 'ERR_INVALID_PARAM'
        return resp
    except Exception:
        conn.rollback()
        logger.exception('pack purchase failed')
        resp['result'] = 'ERR_INVALID_PARAM'
        return resp
