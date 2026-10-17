"""Change only Android application identity and labels, preserving resource IDs."""
import struct
from android_manifest import _chunks, _strings, _node, _append_strings, patch_manifest

OLD = 'com.efootball.revival.local'
PACKAGE = 'com.efootball.revival.test'
LABEL = 'efootxi 测试'


def manifest(data, version='1.2.0'):
    data = patch_manifest(data, version_name=version)
    chunks = _chunks(data)
    pool_i = next(i for i,c in enumerate(chunks) if c[0] == 1)
    strings = _strings(chunks[pool_i][2])
    added, edits = [], {}
    def index(value):
        if value in strings: return strings.index(value)
        if value not in added: added.append(value)
        return len(strings) + added.index(value)
    for i, (kind, _, raw) in enumerate(chunks):
        if kind != 0x102: continue
        element, attrs = _node(raw, strings)
        out = bytearray(raw)
        for a in attrs:
            if a['type'] != 3: continue
            old = a['value']; value = old
            if a['name'] == 'package' and element == 'manifest':
                if old != OLD: raise ValueError('Unexpected source application identity')
                value = PACKAGE
            elif a['name'] == 'label' and old in ('efootxi', '73efootball'):
                value = LABEL
            elif old.startswith(OLD) and a['name'] in ('authorities','permission','name','readPermission','writePermission'):
                # Runtime classes have org.efootballrevival / com.epicgames names.
                value = PACKAGE + old[len(OLD):]
            elif a['name'] in ('name','targetActivity','backupAgent','appComponentFactory') and old.startswith('.'):
                value = OLD + old
            if value != old:
                idx = index(value)
                struct.pack_into('<I',out,a['offset']+8,idx)
                struct.pack_into('<I',out,a['offset']+16,idx)
        edits[i] = bytes(out)
    result = bytearray(data[:8])
    for i, (_,_,raw) in enumerate(chunks):
        result.extend(_append_strings(raw,added) if i == pool_i else edits.get(i,raw))
    struct.pack_into('<I',result,4,len(result))
    _chunks(result)
    return bytes(result)


def resource_table(data):
    out = bytearray(data)
    kind, header, size = struct.unpack_from('<HHI',out)
    if (kind, size) != (2,len(out)): raise ValueError('Invalid resource table')
    offset, changed = header, 0
    while offset < len(out):
        kind, head, size = struct.unpack_from('<HHI',out,offset)
        if size < head or size < 8 or offset + size > len(out): raise ValueError('Invalid resource chunk')
        if kind == 0x200:
            name = bytes(out[offset+12:offset+268]).decode('utf-16le').split('\0')[0]
            if name == OLD:
                encoded = PACKAGE.encode('utf-16le')
                out[offset+12:offset+268] = encoded + bytes(256-len(encoded)); changed += 1
        offset += size
    if changed != 1: raise ValueError('Expected one resource application package')
    return bytes(out)
