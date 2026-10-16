"""Make an API31/efootxi variant; preserve every payload except the manifest."""
import argparse
import copy
import hashlib
import json
import struct
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / '.tools/android/build-tools/android-16'
BASE_SHA = 'ceb866668bc9c6ae6c3d7588705d90f1e1117ad47c88e10a336ff378daf8ee45'
CERT_SHA = '7bc4e9ca40a17a15e32bfc0f20e55abc47b6c1e2cf701770f3be8bdbd16f93b4'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def patch_manifest(original):
    data = bytearray(original)
    assert struct.unpack_from('<HHI', data) == (3, 8, len(data)), 'Unexpected binary XML header'
    strings, slots = [], []
    changes = []
    cursor = 8
    pool_flags_offset = None

    def short_length(offset, utf8):
        if utf8:
            value = data[offset]
            return (((value & 127) << 8) | data[offset + 1], offset + 2) if value & 128 else (value, offset + 1)
        value = struct.unpack_from('<H', data, offset)[0]
        return (((value & 32767) << 16) | struct.unpack_from('<H', data, offset + 2)[0], offset + 4) if value & 32768 else (value, offset + 2)

    while cursor < len(data):
        kind, header_size, size = struct.unpack_from('<HHI', data, cursor)
        assert size >= header_size >= 8 and cursor + size <= len(data)
        if kind == 1:
            assert not strings, 'Multiple string pools are unsupported'
            count, style_count, flags, start, styles_start = struct.unpack_from('<IIIII', data, cursor + 8)
            utf8 = bool(flags & 256)
            pool_flags_offset = cursor + 16
            for i in range(count):
                offset = cursor + start + struct.unpack_from('<I', data, cursor + header_size + 4 * i)[0]
                _, content = short_length(offset, utf8)
                if utf8:
                    length, content = short_length(content, True)
                    strings.append(bytes(data[content:content + length]).decode('utf-8'))
                    end = content + length + 1
                else:
                    length, content = short_length(offset, False)
                    strings.append(bytes(data[content:content + length * 2]).decode('utf-16le'))
                    end = content + length * 2 + 2
                slots.append((offset, end, utf8))
        cursor += size
    assert cursor == len(data)
    label_ids = [i for i, value in enumerate(strings) if value == '73efootball']
    assert len(label_ids) == 1, 'Expected one shared literal application label'
    label_id = label_ids[0]
    label_references = []
    min_sdk_offsets = []
    cursor = 8
    while cursor < len(data):
        kind, header_size, size = struct.unpack_from('<HHI', data, cursor)
        if kind == 0x102:
            node = cursor + header_size
            namespace, name, attr_start, attr_size, attr_count = struct.unpack_from('<IIHHH', data, node)
            assert attr_size == 20
            element = strings[name]
            for i in range(attr_count):
                offset = node + attr_start + attr_size * i
                ns, attr_name, raw = struct.unpack_from('<III', data, offset)
                value_size, _, value_type, value = struct.unpack_from('<HBBI', data, offset + 12)
                assert value_size == 8
                attribute = strings[attr_name]
                if raw == label_id or (value_type == 3 and value == label_id):
                    assert attribute == 'label' and element in ('application', 'activity')
                    assert ns != 0xffffffff and strings[ns] == 'http://schemas.android.com/apk/res/android'
                    assert raw in (0xffffffff, label_id) and value_type == 3 and value == label_id
                    label_references.append(element)
                if element == 'uses-sdk' and attribute == 'minSdkVersion':
                    assert raw == 0xffffffff and value_type == 0x10 and value == 32
                    min_sdk_offsets.append(offset + 16)
        cursor += size
    assert len(min_sdk_offsets) == 1
    assert label_references.count('application') == 1 and len(label_references) == 4
    struct.pack_into('<I', data, min_sdk_offsets[0], 31)
    changes.append({'field': 'uses-sdk.minSdkVersion', 'before': 32, 'after': 31})
    offset, end, utf8 = slots[label_id]
    label = 'efootxi'
    encoded = bytes([len(label), len(label)]) + label.encode('utf-8') + b'\0' if utf8 else struct.pack('<H', len(label)) + label.encode('utf-16le') + b'\0\0'
    assert len(encoded) <= end - offset
    data[offset:end] = encoded + b'\0' * (end - offset - len(encoded))
    # Renaming a pooled value could invalidate the optional sorted-string flag.
    flags = struct.unpack_from('<I', data, pool_flags_offset)[0]
    struct.pack_into('<I', data, pool_flags_offset, flags & ~1)
    changes.append({'field': 'shared application/activity label', 'before': '73efootball', 'after': label, 'references': len(label_references)})
    assert len(data) == len(original)
    return bytes(data), changes


def run(*args):
    result = subprocess.run([str(a) for a in args], check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.stdout.decode('utf-8', errors='replace')


def build(source):
    assert digest(source) == BASE_SHA, 'Reviewed source APK identity mismatch'
    output_dir = ROOT / 'release/android12'
    work = ROOT / 'build/android12'
    output_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    source_badging = run(SDK / 'aapt2.exe', 'dump', 'badging', source)
    source_signature = run('java', '-jar', SDK / 'lib/apksigner.jar', 'verify', '--print-certs', source)
    assert CERT_SHA in source_signature, 'Unexpected source signing certificate'
    unsigned = work / 'unsigned.apk'
    source_entries = {}
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(unsigned, 'w', allowZip64=False) as dst:
        assert len(src.namelist()) == len(set(src.namelist())), 'Duplicate archive member'
        manifest, changes = patch_manifest(src.read('AndroidManifest.xml'))
        (work / 'AndroidManifest.xml').write_bytes(manifest)
        for info in src.infolist():
            if info.filename.upper().startswith('META-INF/') and info.filename.upper().endswith(('.SF', '.RSA', '.DSA', '.EC', 'MANIFEST.MF')):
                raise ValueError('Unexpected legacy signature member')
            target = copy.copy(info)
            if info.filename == 'AndroidManifest.xml':
                source_entries[info.filename] = hashlib.sha256(src.read(info)).hexdigest()
                dst.writestr(target, manifest)
            else:
                member_hash = hashlib.sha256()
                with src.open(info) as reader, dst.open(target, 'w') as writer:
                    while block := reader.read(1024 * 1024):
                        member_hash.update(block)
                        writer.write(block)
                source_entries[info.filename] = member_hash.hexdigest()
    print('Repacked: only manifest content changed', flush=True)
    aligned = work / 'aligned.apk'
    run(SDK / 'zipalign.exe', '-f', '-P', '16', '4', unsigned, aligned)
    output = output_dir / 'xixixi-android12.apk'
    run('java', '-jar', SDK / 'lib/apksigner.jar', 'sign',
        '--ks', ROOT / 'signing/release-signing.p12', '--ks-key-alias', '73efootball-bootstrap',
        '--ks-pass', 'file:' + str(ROOT / 'signing/signing-password.txt'), '--min-sdk-version', '31',
        '--v1-signing-enabled', 'false', '--v2-signing-enabled', 'true',
        '--v3-signing-enabled', 'true', '--v4-signing-enabled', 'false', '--out', output, aligned)
    signature = run('java', '-jar', SDK / 'lib/apksigner.jar', 'verify', '--verbose', '--print-certs',
                    '--min-sdk-version', '31', '--max-sdk-version', '31', output)
    assert CERT_SHA in signature, 'Signing certificate changed'
    assert 'Verified using v3 scheme (APK Signature Scheme v3): true' in signature
    run(SDK / 'zipalign.exe', '-c', '-P', '16', '4', output)
    badging = run(SDK / 'aapt2.exe', 'dump', 'badging', output)
    expected_badging = source_badging.replace("minSdkVersion:'32'", "minSdkVersion:'31'").replace('73efootball', 'efootxi')
    assert badging == expected_badging, 'Unexpected manifest badging difference'
    (output_dir / 'badging.txt').write_text(badging, encoding='utf-8')
    (output_dir / 'signature-verification.txt').write_text(signature, encoding='utf-8')
    verified_entries = {}
    with zipfile.ZipFile(output) as archive:
        for info in archive.infolist():
            with archive.open(info) as stream:
                verified_entries[info.filename] = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert source_entries.keys() == verified_entries.keys()
    changed = [name for name, value in source_entries.items() if verified_entries[name] != value]
    assert changed == ['AndroidManifest.xml'], changed
    assert output.stat().st_size < 2 * 1024 ** 3
    output_hash = digest(output)
    report = {
        'apk': output.name, 'displayName': 'xixixi实况破解版（Android 12兼容测试版）.apk',
        'launcherLabel': 'efootxi', 'bytes': output.stat().st_size, 'sha256': output_hash,
        'sourceSha256': BASE_SHA, 'package': 'com.efootball.revival.local',
        'versionCode': 311010101, 'minSdk': 31, 'targetSdk': 36, 'abi': 'arm64-v8a',
        'manifestChanges': changes, 'changedEntries': changed,
        'unchangedEntries': len(source_entries) - 1,
        'allDexNativeAndResourcePayloadsIdentical': True,
        'signatureVerifiedForApi31': True, 'signatureScheme': 'v3', 'signerCertificateSha256': CERT_SHA,
        'sameSignerAsPreviousRelease': True, 'zipAlignment4And16KiB': 'passed',
        'fullMemberCrcAndSha256Comparison': 'passed',
        'androidDeviceTest': 'not-performed-no-device-connected',
        'performanceBenchmark': 'not-performed',
    }
    (output_dir / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (output_dir / 'SHA256SUMS').write_text(f'{output_hash}  {output.name}\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    build(args.source.resolve())
