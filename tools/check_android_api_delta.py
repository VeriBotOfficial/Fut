"""Static DEX reference audit against the installed Android SDK API history.

This checks reference tables, not runtime reachability, reflection, or native APIs.
"""
import argparse
import collections
import hashlib
import json
import struct
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def dex_tables(data):
    def u32(offset):
        return struct.unpack_from('<I', data, offset)[0]

    def table(offset):
        count, start = struct.unpack_from('<II', data, offset)
        return count, start

    def string(offset):
        while data[offset] & 0x80:
            offset += 1
        offset += 1
        return data[offset:data.index(b'\0', offset)].decode('utf-8', errors='replace')

    assert data[:4] == b'dex\n' and data[7] == 0
    count, start = table(56)
    strings = [string(u32(start + i * 4)) for i in range(count)]
    count, start = table(64)
    types = [strings[u32(start + i * 4)] for i in range(count)]

    def type_list(offset):
        if not offset:
            return []
        return [types[struct.unpack_from('<H', data, offset + 4 + i * 2)[0]] for i in range(u32(offset))]

    count, start = table(72)
    protos = []
    for i in range(count):
        _, result, params = struct.unpack_from('<III', data, start + 12 * i)
        protos.append('(' + ''.join(type_list(params)) + ')' + types[result])
    count, start = table(80)
    fields = []
    for i in range(count):
        owner, _, name = struct.unpack_from('<HHI', data, start + 8 * i)
        fields.append((types[owner], strings[name]))
    count, start = table(88)
    methods = []
    for i in range(count):
        owner, proto, name = struct.unpack_from('<HHI', data, start + 8 * i)
        methods.append((types[owner], strings[name] + protos[proto]))
    count, start = table(96)
    hierarchy = {}
    for i in range(count):
        offset = start + i * 32
        cls, _, parent, interfaces = struct.unpack_from('<IIII', data, offset)
        hierarchy[types[cls]] = ([types[parent]] if parent != 0xffffffff else []) + type_list(interfaces)
    return types, fields, methods, hierarchy


def audit(apk, api_xml):
    classes = {}
    for cls in ET.parse(api_xml).getroot().findall('class'):
        since = int(cls.get('since', '1'))
        classes['L' + cls.get('name') + ';'] = {
            'since': since,
            'parents': ['L' + p.get('name') + ';' for tag in ('extends', 'implements') for p in cls.findall(tag)],
            'method': {m.get('name'): max(since, int(m.get('since', since))) for m in cls.findall('method')},
            'field': {m.get('name'): max(since, int(m.get('since', since))) for m in cls.findall('field')},
        }
    entries = []
    local_hierarchy = {}
    with zipfile.ZipFile(apk) as archive:
        for name in archive.namelist():
            if name.startswith('classes') and name.endswith('.dex') and '/' not in name:
                data = archive.read(name)
                types, fields, methods, hierarchy = dex_tables(data)
                entries.append((name, data[:8].decode('ascii').rstrip('\0'), types, fields, methods))
                local_hierarchy.update(hierarchy)

    def resolve(owner, kind, member, seen=None):
        seen = set() if seen is None else seen
        if owner in seen:
            return None
        seen.add(owner)
        definition = classes.get(owner)
        if definition and member in definition[kind]:
            return owner, definition[kind][member]
        if member.startswith('<init>'):
            return None
        parents = local_hierarchy.get(owner, definition['parents'] if definition else [])
        candidates = [found for parent in parents if (found := resolve(parent, kind, member, seen))]
        return min(candidates, key=lambda item: item[1]) if candidates else None

    findings = []
    stats = []
    for name, magic, types, fields, methods in entries:
        stats.append({'dex': name, 'magic': magic, 'types': len(types), 'fields': len(fields), 'methods': len(methods)})
        for descriptor in types:
            cls = descriptor.lstrip('[')
            if cls in classes and classes[cls]['since'] > 31:
                findings.append({'dex': name, 'kind': 'class', 'owner': cls, 'since': classes[cls]['since']})
        for kind, references in [('field', fields), ('method', methods)]:
            for owner, member in references:
                match = resolve(owner, kind, member)
                if match and match[1] > 31:
                    findings.append({'dex': name, 'kind': kind, 'owner': owner, 'declaredOwner': match[0], 'member': member, 'since': match[1]})
    return {
        'apk': str(apk.resolve()),
        'apiHistorySha256': hashlib.sha256(api_xml.read_bytes()).hexdigest(),
        'dexTables': stats,
        'newerApiReferenceCounts': dict(collections.Counter(str(f['since']) for f in findings)),
        'api32References': [f for f in findings if f['since'] == 32],
        'allNewerApiReferences': findings,
        'limitations': [
            'Reference-table audit, not control-flow or runtime execution.',
            'Newer API references may be correctly protected by SDK version guards.',
            'Public SDK history only; reflection, JNI, hidden APIs and dynamic loading are not covered.',
            'API33+ references also exist in the original API32 package and require separate guards; they do not alone identify a new API31 regression.',
        ],
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('apk', type=Path)
    parser.add_argument('--api-xml', type=Path, default=Path('.tools/android/platform/android-36/data/api-versions.xml'))
    parser.add_argument('--output', type=Path, default=Path('analysis/android12-dex-audit.json'))
    args = parser.parse_args()
    report = audit(args.apk, args.api_xml)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'allNewerApiReferences'}, indent=2))
