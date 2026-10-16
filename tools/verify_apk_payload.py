#!/usr/bin/env python3
"""Read-only comparison of the source APK and the resource-bootstrap APK.

Every ZIP member is read in full, checked against its ZIP CRC, and hashed with
SHA-256. Only classes7.dex may change; classes8.dex and the two bootstrap JSON
assets must be added. The report is written even when verification fails.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA256 = '6d34963dcc7d3038fe6bd6c1ee8730ed10ae3e90417e341c386c7e196f69cd2e'
ALLOWED_CHANGED = {'classes7.dex'}
REQUIRED_ADDED = {
    'classes8.dex',
    'assets/resource-bootstrap/manifest.json',
    'assets/resource-bootstrap/config.json',
}
CHUNK_BYTES = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_archive(path: Path) -> dict:
    members = {}
    with zipfile.ZipFile(path, 'r') as archive:
        names = Counter(info.filename for info in archive.infolist())
        duplicates = sorted(name for name, count in names.items() if count > 1)
        if duplicates:
            raise ValueError(f'{path.name} has duplicate ZIP member paths: {duplicates}')
        for info in archive.infolist():
            digest = hashlib.sha256()
            crc = 0
            size = 0
            with archive.open(info, 'r') as member:
                while chunk := member.read(CHUNK_BYTES):
                    digest.update(chunk)
                    crc = zlib.crc32(chunk, crc)
                    size += len(chunk)
            if size != info.file_size or crc & 0xffffffff != info.CRC:
                raise ValueError(f'{path.name}: CRC or expanded-size mismatch in {info.filename}')
            members[info.filename] = {
                'bytes': size,
                'sha256': digest.hexdigest(),
                'crc32': f'{crc & 0xffffffff:08x}',
                'compressionMethod': info.compress_type,
                'crcVerified': True,
            }
    return {'bytes': path.stat().st_size, 'sha256': file_sha256(path), 'entryCount': len(members), 'entries': members}


def verify(source_path: Path, output_path: Path, manifest_path: Path) -> dict:
    source = inspect_archive(source_path)
    if source['sha256'] != SOURCE_SHA256:
        raise ValueError('Source APK identity differs from the reviewed original')
    output = inspect_archive(output_path)
    if output['bytes'] >= 2 * 1024 ** 3:
        raise ValueError('APK is not below 2 GiB')
    source_entries, output_entries = source['entries'], output['entries']
    original_paths, final_paths = set(source_entries), set(output_entries)
    added = final_paths - original_paths
    removed = original_paths - final_paths
    changed = {
        name for name in original_paths & final_paths
        if source_entries[name]['sha256'] != output_entries[name]['sha256']
        or source_entries[name]['bytes'] != output_entries[name]['bytes']
    }
    if added != REQUIRED_ADDED or removed or changed != ALLOWED_CHANGED:
        raise ValueError(json.dumps({
            'unexpectedAdded': sorted(added - REQUIRED_ADDED),
            'missingRequiredAdded': sorted(REQUIRED_ADDED - added),
            'removed': sorted(removed),
            'unexpectedChanged': sorted(changed - ALLOWED_CHANGED),
            'expectedPatchMissing': sorted(ALLOWED_CHANGED - changed),
        }))

    with zipfile.ZipFile(output_path, 'r') as archive:
        packaged_manifest_bytes = archive.read('assets/resource-bootstrap/manifest.json')
        if packaged_manifest_bytes != manifest_path.read_bytes():
            raise ValueError('Packaged resource manifest differs from release/resources/manifest.json')
        manifest = json.loads(packaged_manifest_bytes)
        if (manifest.get('schemaVersion'), manifest.get('packageName'), manifest.get('versionCode'), manifest.get('resourceVersion')) != (
            1, 'com.efootball.revival.local', 311010101, '311010101-github-v1'
        ):
            raise ValueError('Packaged resource manifest identity is incorrect')
        config = json.loads(archive.read('assets/resource-bootstrap/config.json'))
        if set(config) != {'baseUrl'}:
            raise ValueError('Bootstrap configuration must contain only the public baseUrl')
        parsed = urlsplit(config['baseUrl'])
        if parsed.scheme != 'https' or parsed.hostname != 'github.com' or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Bootstrap URL must be a public HTTPS GitHub URL without credentials')
        if not parsed.path.endswith('/') or '/releases/download/' not in parsed.path:
            raise ValueError('Bootstrap URL does not identify a GitHub Releases download directory')
        for name in ('classes7.dex', 'classes8.dex'):
            with archive.open(name) as dex:
                if not dex.read(8).startswith(b'dex\n'):
                    raise ValueError(f'{name} is not a DEX payload')

    comparisons = []
    for name in sorted(original_paths | final_paths):
        item = {'path': name, 'status': 'added' if name in added else 'changed' if name in changed else 'identical'}
        if name in source_entries:
            item['source'] = source_entries[name]
        if name in output_entries:
            item['output'] = output_entries[name]
        comparisons.append(item)
    return {
        'source': {'path': str(source_path), **{key: value for key, value in source.items() if key != 'entries'}},
        'apk': {'path': str(output_path), **{key: value for key, value in output.items() if key != 'entries'}},
        'sourceIdentityVerified': True,
        'allSourceMemberCrcVerified': True,
        'allOutputMemberCrcVerified': True,
        'allMembersReadAndSha256Computed': True,
        'addedEntries': sorted(added),
        'changedEntries': sorted(changed),
        'removedEntries': sorted(removed),
        'identicalEntryCount': len(original_paths) - len(changed),
        'packagedResourceManifestMatchesRelease': True,
        'publicResourceBaseUrl': config['baseUrl'],
        'signingSecretEntriesAdded': False,
        'below2GiB': True,
        'signatureAndAlignment': 'Validate separately with apksigner and zipalign; this verifier compares payloads only.',
        'entries': comparisons,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path('D:/ef/base.apk.1'))
    parser.add_argument('--apk', type=Path, default=ROOT / 'release/73efootball-11.1.1-github-bootstrap.apk')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'release/resources/manifest.json')
    parser.add_argument('--report', type=Path, default=ROOT / 'release/apk-payload-verification.json')
    args = parser.parse_args()
    started = time.perf_counter()
    report = {'schemaVersion': 1, 'status': 'failed'}
    try:
        report.update(verify(args.source.resolve(strict=True), args.apk.resolve(strict=True), args.manifest.resolve(strict=True)))
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = f'{type(error).__name__}: {error}'
    report['elapsedSeconds'] = round(time.perf_counter() - started, 3)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_suffix(args.report.suffix + '.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(args.report)
    print(json.dumps({key: value for key, value in report.items() if key != 'entries'}, ensure_ascii=False, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
