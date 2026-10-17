"""Idempotent embedded-gateway startup; never reset or replace a live club."""
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import club_store as store
import player_identity
import player_migration
import player_parameters
import project_info

MARKER = 'release_seed_defaults_20261016_v2'
INHERITED_GAME_ID = 1792089875562211
PROJECT_MARKER = 'efootxi_identity_v1'


class StartupError(RuntimeError):
    """A named startup phase failed; the original save remains in place."""


def _backup_before_migration(conn, *, revision='1200'):
    """Snapshot committed SQLite state including WAL before changing a save.

    This intentionally does not copy the database file: committed changes may
    still live in its WAL. A completed backup is published atomically and is
    never overwritten by subsequent attempts or concurrent first starts.
    """
    location = next((row[2] for row in conn.execute('pragma database_list') if row[1] == 'main'), '')
    if not location:  # In-memory fixtures have no persistent save to protect.
        return None
    source = Path(location)
    backup = source.with_name(source.stem + '.before-efootxi-' + revision + source.suffix)
    if backup.exists():
        # An existing completed snapshot belongs to the first upgrade attempt.
        check = sqlite3.connect(backup.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            if check.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('existing migration backup failed integrity check')
        finally:
            check.close()
        return str(backup)
    descriptor, temporary_name = tempfile.mkstemp(prefix=backup.name + '.', suffix='.tmp', dir=backup.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        target = sqlite3.connect(temporary)
        try:
            conn.backup(target)
            target.execute('pragma journal_mode=delete')
            if target.execute('pragma quick_check').fetchone()[0] != 'ok':
                raise ValueError('migration backup failed integrity check')
        finally:
            target.close()
        # link is atomic and refuses an existing destination on both Linux
        # (Android private app data) and NTFS. The staging file is then unlinked.
        try:
            os.link(temporary, backup)
        except FileExistsError:
            pass
        return str(backup)
    finally:
        temporary.unlink(missing_ok=True)


def reset_pools(conn):
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    for table in ('managed_consumed', 'managed_draw_receipts', 'draw_receipts'):
        if table in tables:
            conn.execute('delete from "' + table + '"')
    for table in ('agents', 'managed_agents'):
        if not conn.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone():
            continue
        columns = {r[1] for r in conn.execute('pragma table_info("' + table + '")')}
        if 'json' not in columns:
            continue
        for agent_id, raw in list(conn.execute('select agent_id,json from "' + table + '"')):
            agent = json.loads(raw)
            for key, value in (('use_count', 0), ('acquired_list', [])):
                if key in agent:
                    agent[key] = value
            conn.execute('update "' + table + '" set json=? where agent_id=?', (json.dumps(agent), agent_id))


def _migrate_legacy(conn):
    """Repair presentation defaults once, preserving live acquisition receipts."""
    if store.get_meta(conn, MARKER, '') == '1':
        return False
    conn.execute('BEGIN IMMEDIATE')
    try:
        # Recheck after the writer lock so concurrent first starts cannot reset twice.
        if store.get_meta(conn, MARKER, '') == '1':
            conn.rollback()
            return False
        # Pool/receipt cleanup belongs to a staged seed, never a live boot.
        if store.club_team_id(conn) == 101 and store.stored_uniforms(conn) == store.default_uniforms(50):
            store.set_uniforms(conn, store.default_uniforms(101))
        active = json.loads(store.get_meta(conn, 'ai_division_active_match', 'null'))
        if active and active.get('game_id') == INHERITED_GAME_ID:
            store.set_meta(conn, 'ai_division_active_match', 'null')
        store.set_meta(conn, MARKER, '1')
        store.log_event(conn, 'release_defaults_repaired', {'version': 2})
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def migrate(conn, policy=None):
    """Entry point called by the existing embedded_gateway.initialize bridge.

    Each phase commits its own marker in the same transaction as its changes.
    If a later phase fails, a second boot resumes it without replaying earlier
    gifts or identity migration. Never infer a new save from an empty roster.
    """
    if conn.in_transaction:
        raise StartupError('startup requires a clean connection; pending changes were not committed')
    stage = 'card policy'
    try:
        document = player_parameters.validate_policy(
            player_parameters.card_policy() if policy is None else policy)
        if not document['cards']:
            raise ValueError('efootxi card policy is missing or empty')
        signature = player_migration._signature(document)
        placement_pending = not player_migration.placement_applied(conn)
        pending = (not player_identity.enabled(conn)
                   or store.get_meta(conn, MARKER, '') != '1'
                   or store.get_meta(conn, PROJECT_MARKER, '') != '1'
                   or store.get_meta(conn, player_migration.MARKER, '') != signature
                   or placement_pending)
        stage = 'save backup'
        # Existing test saves may have trained/played for days since the old
        # 1200 snapshot. Preserve a fresh snapshot of that progress before
        # moving the original gift copies, without overwriting the old backup.
        revision = ('main-list-v1' if placement_pending
                    and store.get_meta(conn, player_migration.MARKER, '') else '1200')
        backup = _backup_before_migration(conn, revision=revision) if pending else None
        stage = 'owned identities'
        identities = player_identity.migrate(conn)
        stage = 'inherited defaults'
        legacy = _migrate_legacy(conn)
        stage = 'project presentation'
        project_info.migrate(conn)
        stage = 'player migration'
        players = player_migration.migrate(conn, document)
        if conn.in_transaction:
            raise ValueError('startup migration left a pending transaction')
        stage = 'local gateway addresses'
        import gateway_runtime
        gateway_runtime.install()
        return {'backup': backup, 'identities': identities, 'legacy': legacy, 'players': players}
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        raise StartupError('efootxi startup [' + stage + ']: ' + str(exc)) from exc


def clean_seed(conn, *, new_test_seed=False):
    """Run only on a staged release copy, never on a production save."""
    if not new_test_seed:
        raise ValueError('seed cleanup requires an explicit staged test copy')
    if conn.in_transaction:
        raise ValueError('finish transaction before seed cleanup')
    reset_pools(conn)
    tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    for table in tables:
        if table == 'events' or table.endswith('_receipts'):
            conn.execute('delete from "' + table + '"')
    for (key,) in list(conn.execute('select key from meta')):
        if key.startswith(('ai_division_', 'pack_')) and (key.startswith('ai_division_') or key.endswith('_purchased')):
            conn.execute('delete from meta where key=?', (key,))
    squad = next((s for s in store.squads(conn)
                  if s.get('squad_id') == store.main_squad_id(conn)), {})
    team = store.base_team_id_of(squad.get('base_team'))
    if team:
        store.set_uniforms(conn, store.default_uniforms(team))
        base = squad['base_team']
        store.set_meta(conn, 'base_team', json.dumps(base[0] if isinstance(base, list) else base))
        store.set_meta(conn, 'base_team_id', str(team))
    store.set_meta(conn, MARKER, '1')
    conn.commit()
