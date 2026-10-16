"""Small binary-AXML patch for the efootxi update installer; no resource rebuilding.

Existing string indices, node chunks and resource IDs retain their meaning. Only
the manifest versionName attribute and two declared nodes are changed.
"""

import struct


ANDROID_NS = "http://schemas.android.com/apk/res/android"
PERMISSION = "android.permission.REQUEST_INSTALL_PACKAGES"
PROVIDER = "org.efootballrevival.bootstrap.EfootxiUpdateProvider"
AUTHORITY = "com.efootball.revival.local.efootxi.update"
NO_INDEX = 0xFFFFFFFF
ATTRIBUTE_IDS = {
    "name": 0x01010003,
    "exported": 0x01010010,
    "authorities": 0x01010018,
    "grantUriPermissions": 0x0101001B,
    "versionName": 0x0101021C,
}


def _chunks(data):
    if len(data) < 8 or struct.unpack_from("<HHI", data) != (3, 8, len(data)):
        raise ValueError("Expected one complete Android binary XML document")
    result = []
    cursor = 8
    while cursor < len(data):
        if len(data) - cursor < 8:
            raise ValueError("Truncated XML chunk header")
        kind, header, size = struct.unpack_from("<HHI", data, cursor)
        if not 8 <= header <= size <= len(data) - cursor or size % 4:
            raise ValueError("Invalid XML chunk size")
        result.append((kind, header, bytes(data[cursor:cursor + size])))
        cursor += size
    return result


def _length(data, offset, utf8):
    if utf8:
        value = data[offset]
        if value & 0x80:
            return ((value & 0x7F) << 8) | data[offset + 1], offset + 2
        return value, offset + 1
    value = struct.unpack_from("<H", data, offset)[0]
    if value & 0x8000:
        return ((value & 0x7FFF) << 16) | struct.unpack_from("<H", data, offset + 2)[0], offset + 4
    return value, offset + 2


def _strings(chunk):
    kind, header, size = struct.unpack_from("<HHI", chunk)
    if kind != 1 or header < 28:
        raise ValueError("Unsupported Android string pool")
    count, styles, flags, start, styles_start = struct.unpack_from("<IIIII", chunk, 8)
    if header + 4 * (count + styles) > start or not start <= size:
        raise ValueError("Invalid string pool offsets")
    if (styles and not start <= styles_start <= size) or (not styles and styles_start):
        raise ValueError("Invalid style pool offsets")
    result = []
    utf8 = bool(flags & 0x100)
    for index in range(count):
        offset = start + struct.unpack_from("<I", chunk, header + 4 * index)[0]
        units, content = _length(chunk, offset, utf8)
        if utf8:
            length, content = _length(chunk, content, True)
            encoded = chunk[content:content + length]
            if chunk[content + length:content + length + 1] != b"\0":
                raise ValueError("Unterminated UTF-8 pool string")
            value = encoded.decode("utf-8")
        else:
            encoded = chunk[content:content + units * 2]
            if chunk[content + units * 2:content + units * 2 + 2] != b"\0\0":
                raise ValueError("Unterminated UTF-16 pool string")
            value = encoded.decode("utf-16le")
        if len(value.encode("utf-16le")) // 2 != units:
            raise ValueError("String pool length mismatch")
        result.append(value)
    return result


def _encode_length(value, utf8):
    if utf8:
        if not 0 <= value <= 0x7FFF:
            raise ValueError("UTF-8 string too long")
        return bytes([value]) if value < 0x80 else bytes([0x80 | (value >> 8), value & 255])
    if not 0 <= value <= 0x7FFFFFFF:
        raise ValueError("UTF-16 string too long")
    return struct.pack("<H", value) if value < 0x8000 else struct.pack("<HH", 0x8000 | (value >> 16), value & 0xFFFF)


def _append_strings(chunk, values):
    if not values:
        return chunk
    _, header, size = struct.unpack_from("<HHI", chunk)
    count, styles, flags, start, styles_start = struct.unpack_from("<IIIII", chunk, 8)
    strings_end = styles_start if styles else size
    original_strings = chunk[start:strings_end]
    offsets = []
    additions = bytearray()
    utf8 = bool(flags & 0x100)
    for value in values:
        offsets.append(len(original_strings) + len(additions))
        utf16 = value.encode("utf-16le")
        additions.extend(_encode_length(len(utf16) // 2, utf8))
        if utf8:
            encoded = value.encode("utf-8")
            additions.extend(_encode_length(len(encoded), True))
            additions.extend(encoded + b"\0")
        else:
            additions.extend(utf16 + b"\0\0")
    while (len(original_strings) + len(additions)) % 4:
        additions.append(0)
    old_offsets_end = header + count * 4
    offsets_and_gap = (chunk[header:old_offsets_end] + struct.pack("<" + "I" * len(offsets), *offsets)
                       + chunk[old_offsets_end:start])
    new_start = start + len(values) * 4
    new_styles_start = new_start + len(original_strings) + len(additions) if styles else 0
    result = bytearray(chunk[:header] + offsets_and_gap + original_strings + additions + chunk[strings_end:])
    struct.pack_into("<I", result, 4, len(result))
    struct.pack_into("<IIIII", result, 8, count + len(values), styles, flags & ~1, new_start, new_styles_start)
    return bytes(result)


def _node(chunk, strings):
    kind, header, _ = struct.unpack_from("<HHI", chunk)
    if kind == 0x103:
        namespace, name = struct.unpack_from("<II", chunk, header)
        return strings[name], []
    if kind != 0x102:
        raise ValueError("Expected XML element")
    namespace, name, start, width, count = struct.unpack_from("<IIHHH", chunk, header)
    if width != 20 or header + start + width * count != len(chunk):
        raise ValueError("Unsupported XML attribute layout")
    attributes = []
    for index in range(count):
        offset = header + start + width * index
        ns, attr, raw, value_size, unused, value_type, value = struct.unpack_from("<IIIHBBI", chunk, offset)
        if value_size != 8 or unused:
            raise ValueError("Unsupported XML typed value")
        attributes.append({"namespace": None if ns == NO_INDEX else strings[ns], "name": strings[attr],
                           "raw": None if raw == NO_INDEX else strings[raw], "type": value_type,
                           "value": strings[value] if value_type == 3 else value, "offset": offset})
    return strings[name], attributes


def _element(name, attributes, indices, line):
    """Create a leaf element; Android framework attributes are sorted by resource ID."""
    encoded = []
    for attribute, kind, value in sorted(attributes, key=lambda item: ATTRIBUTE_IDS[item[0]]):
        raw = indices[value] if kind == 3 else NO_INDEX
        typed = indices[value] if kind == 3 else value
        encoded.append(struct.pack("<IIIHBBI", indices[ANDROID_NS], indices[attribute], raw, 8, 0, kind, typed))
    start = struct.pack("<HHIII", 0x102, 16, 36 + 20 * len(encoded), line, NO_INDEX)
    start += struct.pack("<IIHHHHHH", NO_INDEX, indices[name], 20, 20, len(encoded), 0, 0, 0) + b"".join(encoded)
    end = struct.pack("<HHIIIII", 0x103, 16, 24, line, NO_INDEX, NO_INDEX, indices[name])
    return start + end


def patch_manifest(data: bytes, version_name="1.1") -> bytes:
    """Return patched AXML. Repeating the same patch is byte-for-byte idempotent.

    A pre-existing provider with our class/authority must exactly match the
    expected private grant-only declaration. Conflicts are rejected.
    """
    if not isinstance(version_name, str) or not version_name or len(version_name) > 100 or "\0" in version_name:
        raise ValueError("Invalid application version name")
    chunks = _chunks(data)
    pools = [i for i, (kind, _, _) in enumerate(chunks) if kind == 1]
    if len(pools) != 1:
        raise ValueError("Expected exactly one string pool")
    pool_index = pools[0]
    strings = _strings(chunks[pool_index][2])
    indices = {value: i for i, value in reversed(list(enumerate(strings)))}
    for name in [ANDROID_NS, "manifest", "application", "uses-permission", "provider", *ATTRIBUTE_IDS]:
        if name not in indices:
            raise ValueError("Required existing XML name missing: " + name)
    resource_maps = [chunk for kind, _, chunk in chunks if kind == 0x180]
    if len(resource_maps) != 1:
        raise ValueError("Expected exactly one resource map")
    resource_map = resource_maps[0]
    map_header = struct.unpack_from("<H", resource_map, 2)[0]
    for name, expected in ATTRIBUTE_IDS.items():
        offset = map_header + indices[name] * 4
        if offset + 4 > len(resource_map) or struct.unpack_from("<I", resource_map, offset)[0] != expected:
            raise ValueError("Unexpected Android attribute resource ID: " + name)
    required = [version_name, PERMISSION, PROVIDER, AUTHORITY]
    additions = []
    for value in required:
        if value not in indices:
            indices[value] = len(strings) + len(additions)
            additions.append(value)

    stack = []
    manifest_index = application_index = application_end = version_offset = None
    permission_count = provider_count = 0
    for i, (kind, _, chunk) in enumerate(chunks):
        if kind == 0x102:
            name, attributes = _node(chunk, strings)
            android = {a["name"]: a for a in attributes if a["namespace"] == ANDROID_NS}
            if not stack:
                if name != "manifest" or manifest_index is not None:
                    raise ValueError("Expected one root manifest")
                manifest_index = i
                version = android.get("versionName")
                if version is None or version["type"] != 3:
                    raise ValueError("Expected a literal manifest versionName")
                version_offset = version["offset"]
            elif stack == ["manifest"] and name == "application":
                if application_index is not None:
                    raise ValueError("Multiple applications are unsupported")
                application_index = i
            elif stack == ["manifest"] and name == "uses-permission" and android.get("name", {}).get("value") == PERMISSION:
                if len(attributes) != 1:
                    raise ValueError("Conflicting installer permission declaration")
                permission_count += 1
            elif stack == ["manifest", "application"] and name == "provider":
                provider_name = android.get("name", {}).get("value")
                authority = android.get("authorities", {}).get("value")
                if provider_name == PROVIDER or authority == AUTHORITY:
                    expected = {"name": (3, PROVIDER), "exported": (18, 0), "authorities": (3, AUTHORITY), "grantUriPermissions": (18, NO_INDEX)}
                    actual = {a["name"]: (a["type"], a["value"]) for a in attributes if a["namespace"] == ANDROID_NS}
                    if len(attributes) != 4 or actual != expected:
                        raise ValueError("Conflicting APK update provider declaration")
                    provider_count += 1
            stack.append(name)
        elif kind == 0x103:
            name, _ = _node(chunk, strings)
            if not stack or stack.pop() != name:
                raise ValueError("Unbalanced XML elements")
            if name == "application" and stack == ["manifest"]:
                application_end = i
    if stack or None in (manifest_index, application_index, application_end, version_offset):
        raise ValueError("Missing or unbalanced manifest/application")
    if permission_count > 1 or provider_count > 1:
        raise ValueError("Duplicate update installer declaration")
    patched_root = bytearray(chunks[manifest_index][2])
    struct.pack_into("<I", patched_root, version_offset + 8, indices[version_name])
    struct.pack_into("<I", patched_root, version_offset + 16, indices[version_name])
    line = struct.unpack_from("<I", chunks[application_index][2], 8)[0]
    permission = _element("uses-permission", [("name", 3, PERMISSION)], indices, line)
    provider = _element("provider", [("name", 3, PROVIDER), ("authorities", 3, AUTHORITY),
                                     ("exported", 18, 0), ("grantUriPermissions", 18, NO_INDEX)], indices, line)
    output = bytearray(data[:8])
    for i, (_, _, chunk) in enumerate(chunks):
        if i == application_index and not permission_count:
            output.extend(permission)
        if i == application_end and not provider_count:
            output.extend(provider)
        if i == pool_index:
            output.extend(_append_strings(chunk, additions))
        elif i == manifest_index:
            output.extend(patched_root)
        else:
            output.extend(chunk)
    struct.pack_into("<I", output, 4, len(output))
    result = bytes(output)
    _chunks(result)
    return result
