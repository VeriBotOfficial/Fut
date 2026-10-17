"""Visible project identity for the official efootxi application."""
NAME = 'efootxi'
VERSION = '1.2'
BUILD_REVISION = 1206
BUILD_LABEL = '1.2'
CHANNEL = 'stable'
AUTHOR = 'maxlzyver'
PROJECT_URL = 'https://github.com/maxlzyver/efootxi'
STABLE_URL = 'https://github.com/maxlzyver/efootxi'
PROVENANCE = 'efootxi 第一版基于 73@7othree 版本开发，已获得修改许可。后续版本由 maxlzyver 持续修改与维护，具体变更以各版本更新公告为准。'
FEEDBACK_EMAIL = 'lzyuan666666@gmail.com'
NETWORK_NOTE = '资源托管在 GitHub。下载慢或失败时，请更换可用、稳定的梯子（网络代理）和更快的网络后重试；下载速度仍取决于实际连接。资源完整后可离线游玩。'


def replace_legacy_link(value):
    """Replace known promotional text, never arbitrary CDN or resource URLs."""
    if isinstance(value, dict):
        return {k: replace_legacy_link(v) for k, v in value.items()}
    if isinstance(value, list):
        return [replace_legacy_link(v) for v in value]
    if isinstance(value, str):
        for old in ('https://t.me/efootball_73', 'http://t.me/efootball_73', 't.me/efootball_73'):
            value = value.replace(old, PROJECT_URL)
        value = value.replace('73efootball', NAME)
    return value


def migrate(conn):
    """Change inherited presentation defaults once; preserve user-chosen names."""
    import club_store as store
    import json
    marker = 'efootxi_identity_v1'
    if store.get_meta(conn, marker, '') == '1':
        return False
    if conn.in_transaction:
        raise ValueError('finish transaction before project identity migration')
    conn.execute('BEGIN IMMEDIATE')
    try:
        if store.get_meta(conn, marker, '') == '1':
            conn.rollback()
            return False
        if store.get_meta(conn, 'user_name', '') == 'niuniu73':
            store.set_meta(conn, 'user_name', NAME)
        for table in ('agents', 'managed_agents'):
            columns = {row[1] for row in conn.execute('pragma table_info(' + table + ')')}
            # The actual seed's managed_agents is a name/enabled/mode index,
            # unlike agents.json. Do not assume both tables store JSON blobs.
            if not {'agent_id', 'json'} <= columns:
                if {'agent_id', 'name'} <= columns:
                    for aid, name in conn.execute('select agent_id,name from ' + table).fetchall():
                        updated = replace_legacy_link(name)
                        if updated != name:
                            conn.execute('update ' + table + ' set name=? where agent_id=?', (updated, aid))
                continue
            for aid, raw in conn.execute('select agent_id,json from ' + table).fetchall():
                updated = replace_legacy_link(json.loads(raw))
                text = json.dumps(updated, ensure_ascii=False)
                if text != raw:
                    conn.execute('update ' + table + ' set json=? where agent_id=?', (text, aid))
        store.set_meta(conn, marker, '1')
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
