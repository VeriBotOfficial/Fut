#!/usr/bin/env python3
"""Build deterministic standalone resource ZIPs and verify every byte.

Usage (from the repository/workspace root):
    python tools/package_resources.py

Only the output directory is written. The source ZIP is opened read-only.
Each asset pack is kept together when possible; Download is another group.
ZIP members retain their original relative paths and use ZIP_STORED.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import zipfile
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath


CHUNK_BYTES = 1024 * 1024
FIXED_TIMESTAMP = (2024, 1, 1, 0, 0, 0)
DEFAULT_MAX_BYTES = 1_000_000_000


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def json_write(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def member_bytes(entry: dict) -> int:
    # Local header (30) + central directory header (46), no extras/comments.
    return entry['bytes'] + 76 + 2 * len(entry['path'].encode('utf-8'))


def validate_member_path(name: str) -> None:
    parsed = PurePosixPath(name)
    if not name or '\\' in name or parsed.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
        raise ValueError(f'Unsafe or noncanonical ZIP member path: {name!r}')
    if ':' in parsed.parts[0] or parsed.parts[0] not in ('assetpacks', 'Download'):
        raise ValueError(f'Unexpected resource root: {name!r}')


def group_key(entry: dict) -> str:
    parts = PurePosixPath(entry['path']).parts
    if parts[0] == 'assetpacks' and len(parts) >= 3:
        return '/'.join(parts[:2])
    return parts[0]


def partition(entries: list[dict], max_bytes: int) -> list[list[dict]]:
    grouped = defaultdict(list)
    for entry in entries:
        if member_bytes(entry) + 22 > max_bytes:
            raise ValueError(f"Member cannot fit within the package limit: {entry['path']}")
        grouped[group_key(entry)].append(entry)

    # Normally each logical group fits intact. Split only oversized groups.
    groups = []
    for name, group in sorted(grouped.items()):
        if sum(map(member_bytes, group)) + 22 <= max_bytes:
            groups.append((name, group))
        else:
            groups.extend((entry['path'], [entry]) for entry in sorted(group, key=lambda item: item['path']))
    groups.sort(key=lambda item: (-sum(map(member_bytes, item[1])), item[0]))

    packages: list[list[dict]] = []
    sizes: list[int] = []
    for _, group in groups:
        size = sum(map(member_bytes, group))
        for index, occupied in enumerate(sizes):
            if occupied + size <= max_bytes:
                packages[index].extend(group)
                sizes[index] += size
                break
        else:
            packages.append(list(group))
            sizes.append(22 + size)
    return [sorted(package, key=lambda item: item['path']) for package in packages]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('D:/ef/73efootball-resources.zip'))
    parser.add_argument('--source-manifest', type=Path, default=Path('config/source-resources.json'))
    parser.add_argument('--output', type=Path, default=Path('release/resources'))
    parser.add_argument('--max-bytes', type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if not 1 <= args.max_bytes <= DEFAULT_MAX_BYTES:
        parser.error('--max-bytes must be between 1 and 1,000,000,000')

    started = time.perf_counter()
    source_path = args.source.resolve(strict=True)
    source_manifest = json.loads(args.source_manifest.read_text(encoding='utf-8'))
    original_source_stat = source_path.stat()
    source_digest = file_sha256(source_path)
    if original_source_stat.st_size != source_manifest['archive']['bytes'] or source_digest != source_manifest['archive']['sha256']:
        raise ValueError('Source archive differs from the existing source manifest')
    print(f'Source archive SHA-256 verified: {source_digest}', flush=True)

    entries = []
    for entry in source_manifest['entries']:
        validate_member_path(entry['path'])
        if not isinstance(entry['bytes'], int) or entry['bytes'] < 0 or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256']):
            raise ValueError(f'Invalid source manifest entry: {entry!r}')
        entries.append({key: entry[key] for key in ('path', 'bytes', 'sha256')})
    expected_paths = Counter(entry['path'] for entry in entries)
    if any(count != 1 for count in expected_paths.values()):
        raise ValueError('Duplicate paths in source manifest')
    if sum(entry['bytes'] for entry in entries) != source_manifest['expanded_bytes']:
        raise ValueError('Expanded size does not match source manifest entries')

    packages = partition(entries, args.max_bytes)
    args.output.mkdir(parents=True, exist_ok=True)
    # Refuse to mix an old differently-sized build into this release directory.
    expected_zip_names = {f'resources-{index:02d}.zip' for index in range(1, len(packages) + 1)}
    unexpected_zips = [path.name for path in args.output.glob('*.zip') if path.name not in expected_zip_names]
    if unexpected_zips:
        raise ValueError(f'Output directory contains unrelated ZIP files: {unexpected_zips}')

    output_files = []
    verification_files = []
    observed_paths: Counter = Counter()
    with zipfile.ZipFile(source_path, 'r') as source:
        source_entries = [item for item in source.infolist() if not item.is_dir()]
        actual_paths = Counter(item.filename for item in source_entries)
        if actual_paths != expected_paths:
            raise ValueError('Source archive paths do not exactly match source manifest paths')
        source_info = {item.filename: item for item in source_entries}
        for entry in entries:
            if source_info[entry['path']].file_size != entry['bytes']:
                raise ValueError(f"Source member size mismatch: {entry['path']}")

        for index, package in enumerate(packages, start=1):
            name = f'resources-{index:02d}.zip'
            destination = args.output / name
            temporary = destination.with_suffix('.zip.tmp')
            predicted_bytes = 22 + sum(map(member_bytes, package))
            print(f'Building {name}: {len(package)} members; {predicted_bytes:,} bytes', flush=True)
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=False) as target:
                for entry in package:
                    info = zipfile.ZipInfo(entry['path'], date_time=FIXED_TIMESTAMP)
                    info.compress_type = zipfile.ZIP_STORED
                    info.create_system = 3
                    info.create_version = 20
                    info.extract_version = 20
                    info.external_attr = 0o100644 << 16
                    info.file_size = entry['bytes']
                    digest = hashlib.sha256()
                    size = 0
                    with source.open(entry['path'], 'r') as reader, target.open(info, 'w') as writer:
                        while chunk := reader.read(CHUNK_BYTES):
                            digest.update(chunk)
                            size += len(chunk)
                            writer.write(chunk)
                    if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                        raise ValueError(f"Source member content mismatch: {entry['path']}")
            archive_bytes = temporary.stat().st_size
            if archive_bytes != predicted_bytes or archive_bytes > args.max_bytes:
                raise ValueError(f'{name}: unexpected archive size {archive_bytes:,}, predicted {predicted_bytes:,}')

            with zipfile.ZipFile(temporary, 'r') as rebuilt:
                bad_file = rebuilt.testzip()
                if bad_file is not None:
                    raise ValueError(f'{name}: testzip failed for {bad_file}')
                rebuilt_paths = Counter(item.filename for item in rebuilt.infolist())
                if rebuilt_paths != Counter(entry['path'] for entry in package):
                    raise ValueError(f'{name}: member names differ from planned contents')
                for entry in package:
                    digest = hashlib.sha256()
                    size = 0
                    with rebuilt.open(entry['path'], 'r') as member:
                        while chunk := member.read(CHUNK_BYTES):
                            digest.update(chunk)
                            size += len(chunk)
                    if size != entry['bytes'] or digest.hexdigest() != entry['sha256']:
                        raise ValueError(f"{name}: rebuilt member SHA-256 mismatch: {entry['path']}")
                observed_paths.update(rebuilt_paths)

            archive_sha256 = file_sha256(temporary)
            temporary.replace(destination)
            output_files.append({
                'name': name,
                'bytes': archive_bytes,
                'sha256': archive_sha256,
                'expandedBytes': sum(entry['bytes'] for entry in package),
                'entries': package,
            })
            verification_files.append({
                'name': name,
                'bytes': archive_bytes,
                'sha256': archive_sha256,
                'testzip': 'passed',
                'sourceMemberSha256': 'passed',
                'rebuiltMemberSha256': 'passed',
                'entryCount': len(package),
                'withinSizeLimit': archive_bytes <= args.max_bytes,
                'logicalGroups': sorted({group_key(entry) for entry in package}),
            })
            print(f'Verified {name}: {archive_bytes:,} bytes; SHA-256 {archive_sha256}', flush=True)

    if observed_paths != expected_paths or any(count != 1 for count in observed_paths.values()):
        raise ValueError('Output packages have omitted or duplicated source entries')
    final_source_stat = source_path.stat()
    if (original_source_stat.st_size, original_source_stat.st_mtime_ns) != (final_source_stat.st_size, final_source_stat.st_mtime_ns):
        raise ValueError('Source archive changed during packaging')

    manifest = {
        'schemaVersion': 1,
        'packageName': 'com.efootball.revival.local',
        'versionCode': 311010101,
        'resourceVersion': '311010101-github-v1',
        'files': output_files,
    }
    verification = {
        'schemaVersion': 1,
        'status': 'passed',
        'source': str(source_path),
        'sourceBytes': original_source_stat.st_size,
        'sourceSha256': source_digest,
        'sourceArchiveSha256Verified': True,
        'sourceUnchangedDuringBuild': True,
        'sourceEntryCount': len(entries),
        'outputEntryCount': sum(observed_paths.values()),
        'missingEntries': [],
        'duplicateEntries': [],
        'maxPackageBytes': args.max_bytes,
        'totalPackageBytes': sum(item['bytes'] for item in output_files),
        'totalExpandedBytes': sum(item['expandedBytes'] for item in output_files),
        'zipMethod': 'STORE',
        'zipMemberTimestamp': list(FIXED_TIMESTAMP),
        'packageCount': len(output_files),
        'files': verification_files,
        'elapsedSeconds': round(time.perf_counter() - started, 3),
    }
    json_write(args.output / 'manifest.json', manifest)
    json_write(args.output / 'verification.json', verification)
    print(json.dumps({key: value for key, value in verification.items() if key != 'files'}, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
