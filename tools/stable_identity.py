"""Preserve the released Android identity while updating formal version/label.

Resource IDs, provider authorities, permissions and package-derived private
paths retain the 1.1.1 application's identity. The resource table is checked,
never rewritten.
"""
import struct

from android_manifest import _chunks, _strings, _node, _append_strings, patch_manifest

PACKAGE = 'com.efootball.revival.local'
LABEL = 'efootxi'
VERSION_CODE = 311010101


def manifest(data, version='1.2'):
    chunks = _chunks(data)
    pool_i = next(i for i, chunk in enumerate(chunks) if chunk[0] == 1)
    strings = _strings(chunks[pool_i][2])
    roots = [_node(raw, strings)[1] for kind, _, raw in chunks
             if kind == 0x102 and _node(raw, strings)[0] == 'manifest']
    if len(roots) != 1:
        raise ValueError('Expected one formal application manifest')
    attrs = {a['name']: a for a in roots[0]}
    if attrs.get('package', {}).get('value') != PACKAGE:
        raise ValueError('Unexpected source application identity')
    if attrs.get('versionCode', {}).get('value') != VERSION_CODE:
        raise ValueError('Unexpected source game version code')
    data = patch_manifest(data, version_name=version)
    chunks = _chunks(data)
    pool_i = next(i for i, chunk in enumerate(chunks) if chunk[0] == 1)
    strings = _strings(chunks[pool_i][2])
    added, edits = [], {}
    found = 0
    for i, (kind, _, raw) in enumerate(chunks):
        if kind != 0x102:
            continue
        element, attrs = _node(raw, strings)
        if element != 'application':
            continue
        found += 1
        labels = [a for a in attrs if a['name'] == 'label']
        if len(labels) != 1 or labels[0]['type'] != 3:
            raise ValueError('Expected a literal application label')
        label = labels[0]
        if label['value'] not in (LABEL, '73efootball'):
            raise ValueError('Unexpected source application label')
        if label['value'] != LABEL:
            if LABEL in strings:
                index = strings.index(LABEL)
            else:
                added.append(LABEL)
                index = len(strings)
            out = bytearray(raw)
            struct.pack_into('<I', out, label['offset'] + 8, index)
            struct.pack_into('<I', out, label['offset'] + 16, index)
            edits[i] = bytes(out)
    if found != 1:
        raise ValueError('Expected one application label')
    if not edits:
        return data
    result = bytearray(data[:8])
    for i, (_, _, raw) in enumerate(chunks):
        result.extend(_append_strings(raw, added) if i == pool_i else edits.get(i, raw))
    struct.pack_into('<I', result, 4, len(result))
    _chunks(result)
    return bytes(result)


def resource_table(data):
    """Validate the existing formal resource package and return the same bytes."""
    if len(data) < 12:
        raise ValueError('Invalid resource table')
    kind, header, size = struct.unpack_from('<HHI', data)
    if kind != 2 or size != len(data) or not 12 <= header <= size:
        raise ValueError('Invalid resource table')
    offset, names = header, []
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError('Truncated resource chunk')
        kind, head, size = struct.unpack_from('<HHI', data, offset)
        if not 8 <= head <= size <= len(data) - offset:
            raise ValueError('Invalid resource chunk')
        if kind == 0x200:
            if head < 268:
                raise ValueError('Truncated resource application package')
            names.append(data[offset + 12:offset + 268].decode('utf-16le').split('\0')[0])
        offset += size
    if names != [PACKAGE]:
        raise ValueError('Expected the released formal resource package')
    return data
