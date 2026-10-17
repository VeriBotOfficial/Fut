"""Durable exact-instance release; receipts never regrant a released card.

Native command name CMD_SELL_MYCLUB_GAMEPLAYER is present in the shipped UE4
library. Optional response fields keep the pre-existing generic-ACK shape.
Request identities must be explicit id/serial pairs; no matching by id alone.
"""
import hashlib
import json
import time

import player_identity

COMMAND = 'CMD_SELL_MYCLUB_GAMEPLAYER'


class ReleaseError(ValueError):
    pass


def _table(conn, name):
    return conn.execute("select 1 from sqlite_master where type='table' and name=?", (name,)).fetchone() is not None


def is_released(conn, gid, serial):
    return (_table(conn, 'efootxi_released_players') and conn.execute(
        'select 1 from efootxi_released_players where id=? and serial=?', (gid, serial)).fetchone() is not None)


def identities(req):
    keys = [key for key in ('gameplayer_id_list', 'gameplayer_id', 'gameplayer_list') if key in req]
    if len(keys) != 1:
        raise ReleaseError('one explicit player identity field is required')
    rows = req[keys[0]]
    if keys[0] == 'gameplayer_id':
        rows = [rows]
    if not isinstance(rows, list) or not rows or len(rows) > 100000:
        raise ReleaseError('invalid release list')
    result = []
    for value in rows:
        if isinstance(value, dict) and 'gameplayer_id' in value:
            value = value['gameplayer_id']
        if (not isinstance(value, dict) or type(value.get('id')) is not int
                or type(value.get('serial')) is not int or not 0 < value['id'] < 2**63
                or not 0 < value['serial'] < 2**63):
            raise ReleaseError('release requires exact positive id and serial')
        result.append((value['id'], value['serial']))
    if len(set(result)) != len(result):
        raise ReleaseError('duplicate release identity')
    return sorted(result)


def _scrub_squad(value, released):
    if isinstance(value, list):
        return [_scrub_squad(item, released) for item in value]
    if isinstance(value, dict):
        result = {key: _scrub_squad(item, released) for key, item in value.items()}
        identity = value.get('gameplayer_id')
        if (isinstance(identity, dict) and (identity.get('id'), identity.get('serial')) in released):
            # Keep the slot count and tactics/kit fields. The normal squad
            # resolver can fill an empty slot with another active owned card.
            result['gameplayer_id'] = {'id': 0, 'serial': 0}
        return result
    return value


def _response(conn, req):
    row = conn.execute("select value from meta where key='gp'").fetchone()
    return {'msgid': COMMAND, 'rqid': req.get('rqid'), 'result': 'NOERR',
            'gp': int(row[0]) if row else 0,
            'warehouse_num': conn.execute('select count(*) from warehouse w join gameplayers g '
                                           'on w.id=g.id and w.serial=g.serial').fetchone()[0]}


def release(conn, req):
    """Delete all requested owned copies atomically; retry gives no reward twice."""
    if conn.in_transaction:
        raise ReleaseError('finish transaction before releasing players')
    # The gate already canonicalizes aliases, but standalone callers should
    # receive the same protection for old captured serials.
    req = player_identity.canonical_request(conn, req)
    selected = identities(req)
    fingerprint = hashlib.sha256(json.dumps(selected, separators=(',', ':')).encode()).hexdigest()
    request_key = (str(req.get('user_id', 0)), str(req.get('rqid')))
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('create table if not exists efootxi_release_receipts('
                     'user_id text,rqid text,fingerprint text,reward_gp integer,released_at integer,'
                     'primary key(user_id,rqid))')
        conn.execute('create table if not exists efootxi_released_players('
                     'id integer,serial integer,released_at integer,primary key(id,serial))')
        if req.get('rqid') is not None:
            receipt = conn.execute('select fingerprint from efootxi_release_receipts where user_id=? and rqid=?', request_key).fetchone()
            if receipt:
                if receipt[0] != fingerprint:
                    raise ReleaseError('request id reused for different release')
                response = _response(conn, req)
                conn.commit()
                return response
        players, reward = [], 0
        for pid, serial in selected:
            row = conn.execute('select json from gameplayers where id=? and serial=?', (pid, serial)).fetchone()
            if row is None:
                if is_released(conn, pid, serial):
                    continue
                raise ReleaseError('player instance is not owned')
            player = json.loads(row[0])
            if player.get('gameplayer_id') != {'id': pid, 'serial': serial}:
                raise ReleaseError('stored player identity mismatch')
            if player.get('is_lock') == 'YES':
                raise ReleaseError('locked player cannot be released')
            gp = player.get('sell_gp', 0)
            if type(gp) is not int or gp < 0:
                raise ReleaseError('invalid stored release value')
            reward += gp
            players.append((pid, serial))
        timestamp = int(time.time())
        if players:
            # Persist the maximum before deleting it; serial allocation must
            # not reuse a released instance referenced by historical receipts.
            serials = [serial for _, serial in players if serial < 2147483647]
            if serials:
                player_identity.retain_high_water(conn, max(serials))
            for table in ('warehouse', 'careerplan', 'rentals'):
                if _table(conn, table):
                    conn.executemany('delete from ' + table + ' where id=? and serial=?', players)
            conn.executemany('delete from gameplayers where id=? and serial=?', players)
            conn.executemany('insert or ignore into efootxi_released_players values(?,?,?)',
                             [(pid, serial, timestamp) for pid, serial in players])
            if _table(conn, 'squads'):
                released = set(players)
                for squad_id, raw in conn.execute('select squad_id,json from squads').fetchall():
                    old = json.loads(raw)
                    new = _scrub_squad(old, released)
                    if new != old:
                        conn.execute('update squads set json=? where squad_id=?',
                                     (json.dumps(new, ensure_ascii=False), squad_id))
            row = conn.execute("select value from meta where key='gp'").fetchone()
            balance = int(row[0]) if row else 0
            conn.execute("insert or replace into meta(key,value) values('gp',?)",
                         (str(min(2147483647, balance + reward)),))
            if _table(conn, 'events'):
                conn.execute('insert into events(at,kind,detail) values(?,?,?)',
                             (str(timestamp), 'efootxi_players_released', json.dumps({'players': players, 'gp': reward})))
        # Never remove reserve gifts, acquisition receipts or serial aliases.
        if req.get('rqid') is not None:
            conn.execute('insert into efootxi_release_receipts values(?,?,?,?,?)',
                         (*request_key, fingerprint, reward, timestamp))
        response = _response(conn, req)
        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
