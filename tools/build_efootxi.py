"""Build efootxi 1.1 from the reviewed API31 package without rebuilding game code."""
import copy
import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path
from android_manifest import patch_manifest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'release/android12/xixixi-android12.apk'
SOURCE_SHA = '4a327fc814ff872283f7c8a7d0b8a17f2b786174c5e6d25febd5c8abac2024f4'
CERT_SHA = '7bc4e9ca40a17a15e32bfc0f20e55abc47b6c1e2cf701770f3be8bdbd16f93b4'
SDK = ROOT / '.tools/android/build-tools/android-16'
ANDROID_JAR = ROOT / '.tools/android/platform/android-36/android.jar'


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def run(*arguments):
    result = subprocess.run([str(arg) for arg in arguments], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    text = result.stdout.decode('utf-8', errors='replace')
    if text.strip():
        print(text, flush=True)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', default='1.1')
    parser.add_argument('--build-revision', type=int, default=1100)
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,2}', args.version) or args.build_revision < 1:
        parser.error('Use a numeric release version and positive build revision')
    version, revision = args.version, args.build_revision
    WORK = ROOT / ('build/efootxi-' + version)
    OUTPUT = ROOT / ('release/v' + version)
    assert sha256(SOURCE) == SOURCE_SHA
    WORK.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Every build uses fresh class/dex directories, avoiding stale classes or test code.
    import tempfile
    compile_dir = Path(tempfile.mkdtemp(prefix='compile-', dir=WORK))
    classes = compile_dir / 'classes'
    dex = compile_dir / 'dex'
    classes.mkdir(); dex.mkdir()
    sources = sorted(path for path in (ROOT / 'android-src').rglob('*.java') if not path.name.endswith('Test.java'))
    run('javac', '-encoding', 'UTF-8', '--release', '8', '-cp', ANDROID_JAR, '-d', classes, *sources)
    class_jar = compile_dir / 'bootstrap.jar'
    run('jar', 'cf', class_jar, '-C', classes, '.')
    run('java', '-cp', SDK / 'lib/d8.jar', 'com.android.tools.r8.D8', '--min-api', '31',
        '--lib', ANDROID_JAR, '--output', dex, class_jar)
    config = {
        'baseUrl': 'https://github.com/maxlzyver/efootxi/releases/download/resources-311010101-v1/',
        'channelUrl': 'https://github.com/maxlzyver/efootxi/releases/download/updates/channel.json',
        'buildRevision': revision,
        'versionName': version,
    }
    replacements = {
        'classes8.dex': (dex / 'classes.dex').read_bytes(),
        'assets/resource-bootstrap/config.json': json.dumps(config, ensure_ascii=False).encode('utf-8'),
    }
    original_hashes = {}
    unsigned = WORK / 'unsigned.apk'
    with zipfile.ZipFile(SOURCE) as source, zipfile.ZipFile(unsigned, 'w', allowZip64=False) as output:
        replacements['AndroidManifest.xml'] = patch_manifest(source.read('AndroidManifest.xml'), version_name=version)
        for info in source.infolist():
            target = copy.copy(info)
            if info.filename in replacements:
                original_hashes[info.filename] = hashlib.sha256(source.read(info)).hexdigest()
                output.writestr(target, replacements[info.filename])
            else:
                member_hash = hashlib.sha256()
                with source.open(info) as reader, output.open(target, 'w') as writer:
                    while block := reader.read(1024 * 1024):
                        member_hash.update(block); writer.write(block)
                original_hashes[info.filename] = member_hash.hexdigest()
    print('Game payload copied; compiling and manifest changes limited to bootstrap/update support', flush=True)
    aligned = WORK / 'aligned.apk'
    run(SDK / 'zipalign.exe', '-f', '-P', '16', '4', unsigned, aligned)
    apk = OUTPUT / ('efootxi-' + version + '.apk')
    run('java', '-jar', SDK / 'lib/apksigner.jar', 'sign',
        '--ks', ROOT / 'signing/release-signing.p12', '--ks-key-alias', '73efootball-bootstrap',
        '--ks-pass', 'file:' + str(ROOT / 'signing/signing-password.txt'), '--min-sdk-version', '31',
        '--v1-signing-enabled', 'false', '--v2-signing-enabled', 'true', '--v3-signing-enabled', 'true',
        '--v4-signing-enabled', 'false', '--out', apk, aligned)
    signature = run('java', '-jar', SDK / 'lib/apksigner.jar', 'verify', '--verbose', '--print-certs',
                    '--min-sdk-version', '31', apk)
    assert CERT_SHA in signature and 'Verified using v3 scheme (APK Signature Scheme v3): true' in signature
    run(SDK / 'zipalign.exe', '-c', '-P', '16', '4', apk)
    badging = run(SDK / 'aapt2.exe', 'dump', 'badging', apk)
    assert "versionCode='311010101' versionName='" + version + "'" in badging
    assert "minSdkVersion:'31'" in badging and "targetSdkVersion:'36'" in badging
    assert "application-label:'efootxi'" in badging
    assert 'android.permission.REQUEST_INSTALL_PACKAGES' in badging
    xml = run(SDK / 'aapt2.exe', 'dump', 'xmltree', apk, '--file', 'AndroidManifest.xml')
    assert 'org.efootballrevival.bootstrap.EfootxiUpdateProvider' in xml
    (OUTPUT / 'badging.txt').write_text(badging, encoding='utf-8')
    (OUTPUT / 'manifest-tree.txt').write_text(xml, encoding='utf-8')
    (OUTPUT / 'signature.txt').write_text(signature, encoding='utf-8')
    current_hashes = {}
    with zipfile.ZipFile(apk) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        for info in archive.infolist():
            with archive.open(info) as stream:
                current_hashes[info.filename] = hashlib.file_digest(stream, 'sha256').hexdigest()
    assert original_hashes.keys() == current_hashes.keys()
    changed = sorted(name for name in current_hashes if current_hashes[name] != original_hashes[name])
    assert changed == sorted(replacements)
    assert apk.stat().st_size < 2 * 1024 ** 3
    metadata = {
        'buildRevision': revision, 'versionCode': 311010101, 'versionName': version,
        'url': 'https://github.com/maxlzyver/efootxi/releases/download/v' + version + '/' + apk.name,
        'sha256': sha256(apk), 'bytes': apk.stat().st_size,
        'signerCertificateSha256': CERT_SHA, 'minSdk': 31, 'abi': 'arm64-v8a',
    }
    report = dict(metadata, apk=apk.name, sourceSha256=SOURCE_SHA, changedEntries=changed,
                  unchangedEntries=len(original_hashes) - len(changed),
                  originalGameDexNativeAndResourcesUnchanged=True, samePackageAndSigner=True,
                  signatureVerification='passed-v3', alignment='4-byte-and-16KiB-passed',
                  fullMemberCrcAndSha256='passed', androidDeviceTest='not-performed')
    for name, value in [('apk-update.json', metadata), ('verification.json', report)]:
        (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (OUTPUT / 'SHA256SUMS').write_text(metadata['sha256'] + '  ' + apk.name + '\n', encoding='utf-8')
    print(json.dumps(report, indent=2), flush=True)


if __name__ == '__main__':
    main()
