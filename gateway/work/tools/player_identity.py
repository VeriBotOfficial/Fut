"""Owned-instance identity migration. Catalogue cards may still use serial zero."""
from __future__ import annotations

import json
from collections import Counter

HIGH_WATER_KEY = 'owned_serial_high_water'


def retain_high_water(conn, value):
    """Remember allocated/released short serials so old receipts cannot alias."""
    if type(value) is not int or not 0 <= value < 2147483647:
        raise ValueError('invalid owned serial high water')
    row = conn.execute('select value from meta where key=?', (HIGH_WATER_KEY,)).fetchone()
    previous = int(row[0]) if row else 0
    if value > previous:
        conn.execute('insert or replace into meta(key,value) values(?,?)', (HIGH_WATER_KEY, str(value)))


def _first_unused_serial(conn):
    largest = conn.execute('select coalesce(max(serial),0) from gameplayers '
                           'where serial between 1 and 2147483646').fetchone()[0]
    row = conn.execute('select value from meta where key=?', (HIGH_WATER_KEY,)).fetchone()
    return max(largest, int(row[0]) if row else 0) + 1


def enabled(conn):
    row = conn.execute("select value from meta where key='owned_identity_version'").fetchone()
    return bool(row and row[0] == '1')


def allocate(conn):
    # The native identity parser stores serial as int64. New local instances use
    # compact positive values; preserve existing unique official int64 serials.
    value = _first_unused_serial(conn)
    if enabled(conn):
        while conn.execute('select 1 from owned_identity_aliases where old_serial=? limit 1', (value,)).fetchone():
            value += 1
    if value >= 2147483647:
        raise ValueError('owned serial range exhausted')
    retain_high_water(conn, value)
    return value


def allocate_many(conn, count):
    """Reserve fresh serials inside the caller's writer transaction in O(n).

    The caller inserts all instances before committing. Legacy aliases remain
    reserved even if an old player has since been released.
    """
    if type(count) is not int or count < 0 or not conn.in_transaction:
        raise ValueError('bulk identity allocation requires a writer transaction')
    first = _first_unused_serial(conn)
    reserved = set()
    if enabled(conn):
        reserved = {row[0] for row in conn.execute(
            'select distinct old_serial from owned_identity_aliases where old_serial>=?', (first,))}
    result = []
    value = first
    while len(result) < count:
        if value >= 2147483647:
            raise ValueError('owned serial range exhausted')
        if value not in reserved:
            result.append(value)
        value += 1
    if result:
        retain_high_water(conn, result[-1])
    return result


def remap(value, mapping):
    if isinstance(value, list):
        return [remap(v, mapping) for v in value]
    if isinstance(value, dict):
        out = {k: remap(v, mapping) for k, v in value.items()}
        if type(out.get('id')) is int and type(out.get('serial')) is int:
            out['serial'] = mapping.get((out['id'], out['serial']), out['serial'])
        return out
    return value


def canonical_request(conn, req):
    if not enabled(conn):
        return req
    mapping = {(i, old): new for i, old, new in conn.execute(
        'select id,old_serial,new_serial from owned_identity_aliases')}
    return remap(req, mapping)


def migrate(conn):
    """Atomically fix zero/duplicate serials and every persisted owned reference.

    Historical receipts retain their old fingerprints and payloads for audit.
    The gate remaps receipt responses when returning them; migration is not a
    license to replay an already consumed training request.
    """
    if enabled(conn):
        return {'changed': 0, 'already_migrated': True}
    if conn.in_transaction:
        raise ValueError('finish transaction before identity migration')
    conn.execute('BEGIN IMMEDIATE')
    try:
        if enabled(conn):
            # Another startup may have completed while this writer waited.
            conn.rollback()
            return {'changed': 0, 'already_migrated': True}
        rows = conn.execute('select id,serial,json from gameplayers order by id,serial').fetchall()
        counts = Counter(s for _, s, _ in rows)
        # Never reuse ANY legacy positive serial: aliases must not shadow a new
        # canonical identity on the same card.
        used = {s for _, s, _ in rows if s > 0}
        candidate = 1
        mapping = {}
        for pid, old, _ in rows:
            if old > 0 and counts[old] == 1:
                continue
            while candidate in used:
                candidate += 1
            mapping[pid, old] = candidate
            used.add(candidate)
            candidate += 1
        conn.execute('create table owned_identity_aliases(id integer,old_serial integer,'
                     'new_serial integer,primary key(id,old_serial))')
        for (pid, old), new in mapping.items():
            conn.execute('insert into owned_identity_aliases values(?,?,?)', (pid, old, new))
        temporary_base = min([0] + [s for _, s, _ in rows]) - len(rows) - 1
        # Temporary serials avoid composite-key collisions on same-card copies.
        for n, (pid, old, raw) in enumerate(rows, 1):
            if (pid, old) in mapping:
                conn.execute('update gameplayers set serial=? where id=? and serial=?', (temporary_base-n, pid, old))
        for n, (pid, old, raw) in enumerate(rows, 1):
            if (pid, old) not in mapping:
                continue
            new = mapping[pid, old]
            player = remap(json.loads(raw), mapping)
            conn.execute('update gameplayers set serial=?,json=? where id=? and serial=?',
                         (new, json.dumps(player, ensure_ascii=False), pid, temporary_base-n))
        for table in ('warehouse', 'careerplan', 'rentals'):
            for (pid, old), new in mapping.items():
                conn.execute(f'update {table} set serial=? where id=? and serial=?', (new, pid, old))
        for sid, raw in conn.execute('select squad_id,json from squads').fetchall():
            conn.execute('update squads set json=? where squad_id=?',
                         (json.dumps(remap(json.loads(raw), mapping), ensure_ascii=False), sid))
        conn.execute('create unique index owned_serial_unique on gameplayers(serial)')
        conn.execute("create trigger owned_serial_positive_insert before insert on gameplayers "
                     "when NEW.serial<=0 begin select raise(ABORT,'owned serial must be positive'); end")
        conn.execute("create trigger owned_serial_positive_update before update of serial on gameplayers "
                     "when NEW.serial<=0 begin select raise(ABORT,'owned serial must be positive'); end")
        conn.execute("insert or replace into meta values('owned_identity_version','1')")
        conn.commit()
        return {'changed': len(mapping), 'total': len(rows)}
    except Exception:
        conn.rollback()
        raise


def validate_squad(conn, data):
    seen = set()
    for slot in data.get('player_list') or []:
        gid = slot.get('gameplayer_id') or {}
        pid, serial = int(gid.get('id') or 0), int(gid.get('serial') or 0)
        if not pid:
            continue
        if serial in seen or not conn.execute(
                'select 1 from gameplayers where id=? and serial=?', (pid, serial)).fetchone():
            raise ValueError('duplicate or unowned squad instance')
        seen.add(serial)
