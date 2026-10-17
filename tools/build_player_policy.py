"""Extract a compact, reviewable card policy from a supplied base APK.

Only reads the archive and its SQLite snapshot; never imports game modules.
Unknown card definitions are excluded. Trending cards are fixed-level cards.
The other existing level caps are preserved, including documented overrides.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import zipfile

PREFIX = 'assets/gateway-release/'
KINDS = {'Standard', 'Legendary', 'Featured', 'Epic', 'Highlight', 'Trending'}
COLUMNS = ['kind', 'cap', 'foot', 'weak_accuracy', 'weak_usage', 'attack_style', 'defence_style']


def build(apk, output):
    cards, labels, excluded, cap_rules = {}, {}, [], []
    with zipfile.ZipFile(apk) as archive, tempfile.TemporaryDirectory(prefix='efootxi-policy-') as temp:
        raw = archive.read(PREFIX + 'outputs/efootball/local-club.sqlite3')
        db = Path(temp) / 'source.sqlite3'
        db.write_bytes(raw)
        connection = sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)
        connection.execute('PRAGMA query_only=ON')
        try:
            for pid, kind, payload in connection.execute('select card_id,card_type,payload from managed_cards order by card_id'):
                player = json.loads(payload)
                if kind not in KINDS:
                    excluded.append({'card_id': pid, 'reason': 'unverified card classification'})
                    continue
                source = json.loads(archive.read(PREFIX + 'outputs/reference/external-attrs/' + pid + '.json'))
                external = source.get('efhub', {}).get('player', {})
                foot = external.get('preferredFoot')
                accuracy, usage = external.get('weakFootAccuracy'), external.get('weakFootUsage')
                style = player.get('play_style')
                cap = (player.get('level_info') or {}).get('level_limit')
                params = player.get('parameters') or []
                if (foot not in ('Left', 'Right') or type(accuracy) is not int or not 0 <= accuracy <= 3
                        or type(usage) is not int or not 0 <= usage <= 3
                        or not isinstance(style, list) or len(style) != 2
                        or any(type(s) is not int or not 0 <= s <= 35 for s in style)
                        or type(cap) is not int or not 1 <= cap <= 1000 or len(params) != 147
                        or params[33] != accuracy or params[45] != usage):
                    raise ValueError('unverified card fields: ' + pid)
                if kind == 'Trending' and cap != 1:
                    cap_rules.append({'card_id': pid, 'from': cap, 'to': 1, 'reason': 'Trending has no progression'})
                    cap = 1
                elif external.get('levelCap') != cap:
                    cap_rules.append({'card_id': pid, 'from': cap, 'to': cap,
                                      'external': external.get('levelCap'),
                                      'reason': 'preserve existing local cap; external value is not a verified correction'})
                cards[pid] = [kind, cap, int(foot == 'Left'), accuracy, usage, *style]
                name = external.get('nameZh') or external.get('name') or pid
                if '\ufffd' in name:
                    name = external.get('name') or pid
                age = external.get('age')
                labels[pid] = [name, age if type(age) is int else None]
        finally:
            connection.close()
    result = {'schema': 1, 'policy': 'efootxi-test-1.2.0-players-v1',
              'source_db_sha256': hashlib.sha256(raw).hexdigest(),
              'layout_status': '147-slot static evidence; foot/style await Android gameplay regression',
              'weak_foot_policy': 'preserve native 0..3 values; no ordinal offset',
              'columns': COLUMNS, 'excluded': excluded, 'cap_rules': cap_rules, 'cards': cards, 'labels': labels}
    output.parent.mkdir(parents=True, exist_ok=True)
    # One card per line keeps diffs reviewable without carrying large caches.
    head = {k: v for k, v in result.items() if k not in ('cards', 'labels')}
    text = json.dumps(head, ensure_ascii=False, indent=2)[:-2] + ',\n  "cards": {\n'
    text += ',\n'.join('    ' + json.dumps(pid) + ':' + json.dumps(row, ensure_ascii=False, separators=(',', ':'))
                       for pid, row in cards.items()) + '\n  },\n  "labels": {\n'
    text += ',\n'.join('    ' + json.dumps(pid) + ':' + json.dumps(row, ensure_ascii=False, separators=(',', ':'))
                       for pid, row in labels.items()) + '\n  }\n}\n'
    # Fail before writing if the hand-formatted object is malformed.
    assert json.loads(text) == result
    output.write_text(text, encoding='utf-8')
    print(json.dumps({'cards': len(cards), 'types': dict(collections.Counter(r[0] for r in cards.values())),
                      'excluded': excluded, 'cap_rules': len(cap_rules), 'bytes': output.stat().st_size}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apk', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'gateway/outputs/reference/efootxi-card-policy.json')
    arguments = parser.parse_args()
    build(arguments.apk, arguments.output)
