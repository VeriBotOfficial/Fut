"""Reproduce current league rosters from the user's resource ZIP, read-only.

Only dt870's small CPK and three pesdb tables are read. No original file is
written and no game or downloaded tool is executed. Format references:
https://github.com/Master-Antonio/Efootball-Mod-Studio/blob/c0778355a8153a014c8e1d9ae040f7ec55b4a350/ui/core/wesys.py
https://github.com/Master-Antonio/Efootball-Mod-Studio/blob/c0778355a8153a014c8e1d9ae040f7ec55b4a350/ui/core/pesdb.py
The implementation below is limited to the exact observed archive/layout and
checks all referential integrity before producing data used by the test app.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import struct
import zipfile
import zlib

CPK_MEMBER = 'Download/dt870_mobile_and.cpk'
CPK_SHA256 = '27472fe8f02de4346910996cdc6200940937efee22bfe6eeb1f5326644b032e8'
ASSIGNMENT_SHA256 = 'a789faf583475aa37ab0ce723256d5e6110f24c7eee9a07fb2ac2024506a56e0'
WESYS_KEYS = {1: (0x168EA000, 0x2E2AA6F2, 0x0CC8DCD3),
              2: (0xED5B2960, 0x4A523B4E, 0xF3A31BAD)}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def _utf(data):
    if data[:4] != b'@UTF':
        state, clear = 0x655f, bytearray()
        for value in data:
            clear.append(value ^ (state & 255))
            state = (state * 0x4115) & 0xffffffff
        data = bytes(clear)
    if data[:4] != b'@UTF':
        raise ValueError('CPK directory magic mismatch')
    size, rows, strings, _, name, columns, width, count = struct.unpack_from('>IIIIIHHI', data, 4)
    rows += 8; strings += 8
    if size + 8 > len(data) or count > 10000 or columns > 100:
        raise ValueError('CPK directory bounds mismatch')

    def text(offset):
        start = strings + offset
        return data[start:data.index(0, start)].decode('utf8')

    def read(kind, offset):
        fmt = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i',
               6: 'Q', 7: 'q', 8: 'f', 10: 'I', 11: 'II'}[kind]
        result = struct.unpack_from('>' + fmt, data, offset)
        result = result[0] if len(result) == 1 else result
        return (text(result) if kind == 10 else result), offset + struct.calcsize('>' + fmt)

    descriptors, offset = [], 32
    for _ in range(columns):
        flags = data[offset]
        column = text(struct.unpack_from('>I', data, offset + 1)[0])
        offset += 5
        constant = None
        if flags & 0xf0 == 0x30:
            constant, offset = read(flags & 15, offset)
        descriptors.append((column, flags, constant))
    result = []
    for index in range(count):
        offset, row = rows + index * width, {}
        for column, flags, constant in descriptors:
            if flags & 0xf0 == 0x50:
                row[column], offset = read(flags & 15, offset)
            elif flags & 0xf0 in (0x10, 0x30):
                row[column] = 0 if flags & 0xf0 == 0x10 else constant
            else:
                raise ValueError('Unsupported CPK column storage')
        result.append(row)
    return text(name), result


def _chunk(data, offset):
    length = struct.unpack_from('<Q', data, offset + 8)[0]
    if length > len(data) - offset - 16:
        raise ValueError('CPK chunk outside archive')
    return _utf(data[offset + 16:offset + 16 + length])


def cpk_tables(data):
    if data[:4] != b'CPK ':
        raise ValueError('Not a CPK')
    _, headers = _chunk(data, 0)
    header = headers[0]
    _, directory = _chunk(data, header['TocOffset'])
    base = min(header['ContentOffset'], header['TocOffset'])
    selected = {}
    for row in directory:
        if row['DirName'] != 'common/etc/pesdb' or row['FileName'] not in (
                'PlayerAssignment.bin', 'Player.bin', 'Team.bin'):
            continue
        start, length = base + row['FileOffset'], row['FileSize']
        if length != row['ExtractSize'] or start + length > len(data):
            raise ValueError('Unexpected compressed/invalid CPK table entry')
        selected[row['FileName']] = data[start:start + length]
    if len(selected) != 3:
        raise ValueError('Missing native roster tables')
    return selected


def unwrap_wesys(data):
    if data[:1] != b'\xff' or data[3:8] != b'WESYS' or data[2] != 0x83:
        raise ValueError('Unsupported WESYS wrapper')
    compressed, expected = struct.unpack_from('<II', data, 8)
    if compressed != len(data) - 16 or not 0 < expected <= 32 * 1024**2:
        raise ValueError('WESYS length mismatch')
    a, b, c = WESYS_KEYS[data[1] & 15]
    state = ((expected << 16) | compressed) & 0xffffffff
    stream = bytearray(data[16:])
    for offset in range(0, len(stream) - len(stream) % 4, 4):
        mixed = (a ^ (a << 11)) & 0xffffffff
        a, b, c = b, c, state
        state = (state ^ (((state >> 11) ^ mixed) >> 8) ^ mixed) & 0xffffffff
        struct.pack_into('<I', stream, offset,
                         struct.unpack_from('<I', stream, offset)[0] ^ state)
    decoder = zlib.decompressobj()
    clear = decoder.decompress(stream, expected + 1)
    if len(clear) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError('WESYS inflated size/stream mismatch')
    return clear


def parse_rosters(assignment, players, teams):
    if len(assignment) % 24 or len(players) % 400 or len(teams) % 1600:
        raise ValueError('Native table stride mismatch')
    player_ids, positions = [], {}
    for offset in range(0, len(players), 400):
        pid = struct.unpack_from('<Q', players, offset + 8)[0]
        position = players[offset + 69] >> 4
        if not 0 < pid < 2**63 or not 0 <= position <= 12:
            raise ValueError('Invalid native player/position')
        player_ids.append(pid); positions[str(pid)] = position
    if player_ids != sorted(set(player_ids)):
        raise ValueError('Native external player IDs are not strictly ordered')
    team_ids = [struct.unpack_from('<I', teams, offset + 12)[0]
                for offset in range(0, len(teams), 1600)]
    if len(team_ids) != len(set(team_ids)):
        raise ValueError('Duplicate native team ID')
    valid_players, valid_teams = set(player_ids), set(team_ids)
    rosters, record_ids, previous_team = defaultdict(list), set(), -1
    for offset in range(0, len(assignment), 24):
        record, reserved, pid, team = struct.unpack_from('<IIQI', assignment, offset)
        if reserved or assignment[offset + 23] or record in record_ids or team < previous_team:
            raise ValueError('Native assignment layout/order mismatch')
        if pid not in valid_players or team not in valid_teams:
            raise ValueError('Native roster references an absent player/team')
        if pid in rosters[str(team)]:
            raise ValueError('Duplicate player in native team roster')
        rosters[str(team)].append(pid); record_ids.add(record); previous_team = team
    if any(not 11 <= len(roster) <= 40 for roster in rosters.values()):
        raise ValueError('Native roster size outside observed 11..40')
    used = {pid for roster in rosters.values() for pid in roster}
    return (dict(sorted(rosters.items(), key=lambda item: int(item[0]))),
            {str(pid): positions[str(pid)] for pid in sorted(used)},
            dict(assignment_count=len(assignment) // 24, team_count=len(rosters),
                 unique_player_count=len(used), player_table_count=len(player_ids),
                 team_table_count=len(team_ids)))


def extract(archive):
    with zipfile.ZipFile(archive) as source:
        info = source.getinfo(CPK_MEMBER)
        if info.file_size > 10 * 1024**2:
            raise ValueError('Expected bounded small database CPK')
        cpk = source.read(info)
    if sha256(cpk) != CPK_SHA256:
        raise ValueError('Resource CPK differs from the reviewed source')
    raw = cpk_tables(cpk)
    if sha256(raw['PlayerAssignment.bin']) != ASSIGNMENT_SHA256:
        raise ValueError('Assignment source mismatch')
    decoded = {name: unwrap_wesys(data) for name, data in raw.items()}
    rosters, positions, validation = parse_rosters(decoded['PlayerAssignment.bin'],
                                                 decoded['Player.bin'], decoded['Team.bin'])
    return dict(schema=1, source=dict(archive_name=Path(archive).name,
        cpk_member=CPK_MEMBER, cpk_sha256=sha256(cpk),
        tables={name: dict(wrapped_sha256=sha256(raw[name]),
                           decoded_sha256=sha256(decoded[name]),
                           decoded_bytes=len(decoded[name])) for name in sorted(raw)},
        layout=dict(assignment_stride=24, assignment_player_offset=8,
                    assignment_team_offset=16, player_stride=400,
                    player_id_offset=8, position_byte=69, position_shift=4,
                    team_stride=1600, team_id_offset=12)),
        validation=validation, rosters=rosters, positions=positions)


def serialized(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + '\n'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = extract(args.archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(serialized(result).encode('utf8'))
    print(json.dumps(result['validation']))
