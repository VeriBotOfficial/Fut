"""Build an APK with a resource bootstrap, preserving original game payloads."""
from pathlib import Path
import copy
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / '.tools' / 'android'
BUILD = ROOT / 'build'
SDK = TOOLS / 'build-tools' / 'android-16'
ANDROID_JAR = TOOLS / 'platform' / 'android-36' / 'android.jar'
SOURCE = Path('D:/ef/base.apk.1')
SOURCE_SHA256 = '6d34963dcc7d3038fe6bd6c1ee8730ed10ae3e90417e341c386c7e196f69cd2e'
OUTPUT = ROOT / 'release' / '73efootball-11.1.1-github-bootstrap.apk'
MANIFEST = ROOT / 'release' / 'resources' / 'manifest.json'
RESOURCE_URL = 'https://github.com/maxlzyver/73efootball/releases/download/resources-311010101-v1/'


def run(*args):
    subprocess.run([str(arg) for arg in args], check=True, cwd=ROOT)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while data := stream.read(8 * 1024 * 1024):
            digest.update(data)
    return digest.hexdigest()


def reset_compilation_directory(path):
    """Remove stale compiler outputs, only for the two known local build dirs."""
    root = ROOT.resolve()
    build_root = BUILD.resolve()
    resolved = path.resolve()
    if build_root != root / 'build' or resolved.parent != build_root or resolved.name not in ('java-classes', 'bootstrap-dex'):
        raise ValueError(f'Refusing to clear a directory outside the expected build outputs: {path}')
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError(f'Refusing to clear a linked build directory: {path}')
    if path.exists():
        if not path.is_dir():
            raise ValueError(f'Build directory is not a directory: {path}')
        shutil.rmtree(path)
    path.mkdir()


def build():
    BUILD.mkdir(exist_ok=True)
    OUTPUT.parent.mkdir(exist_ok=True)
    if sha256(SOURCE) != SOURCE_SHA256:
        raise ValueError('Original APK identity changed; refusing to patch.')
    signing = ROOT / 'signing'
    signing.mkdir(exist_ok=True)
    password_path = signing / 'signing-password.txt'
    keystore = signing / 'release-signing.p12'
    if not keystore.exists():
        if password_path.exists():
            raise ValueError('Signing password exists without keystore; inspect before retrying.')
        password_path.write_text(secrets.token_urlsafe(32), encoding='ascii')
        run('keytool', '-genkeypair', '-keystore', keystore, '-storetype', 'PKCS12',
            '-storepass:file', password_path, '-keypass:file', password_path,
            '-alias', '73efootball-bootstrap', '-keyalg', 'RSA', '-keysize', '3072',
            '-validity', '10000', '-dname', 'CN=73efootball Bootstrap, O=maxlzyver, C=CN')
    if not password_path.is_file():
        raise ValueError('Signing password file is missing.')

    patch_input = BUILD / 'patch-input.apk'
    patch_project = BUILD / 'patch-project'
    if not patch_project.exists():
        with zipfile.ZipFile(SOURCE) as src, zipfile.ZipFile(patch_input, 'w') as dst:
            for name in ('AndroidManifest.xml', 'resources.arsc'):
                dst.writestr(name, src.read(name))
            dst.writestr('classes.dex', src.read('classes7.dex'))
        run('java', '-jar', TOOLS / 'apktool.jar', 'd', '--no-res', '--no-debug-info',
            '--output', patch_project, patch_input)

    activity_path = patch_project / 'smali/org/efootballrevival/runtimeprobe/GatewayBootActivity.smali'
    activity = activity_path.read_text(encoding='utf-8')
    call = '    invoke-super {p0, p1}, Landroid/app/Activity;->onCreate(Landroid/os/Bundle;)V'
    hook = '\n'.join([
        call, '',
        '    invoke-static {p0}, Lorg/efootballrevival/bootstrap/ResourceBootstrap;->intercept(Landroid/app/Activity;)Z',
        '    move-result v0',
        '    if-eqz v0, :resource_bootstrap_complete',
        '    return-void',
        '    :resource_bootstrap_complete',
    ])
    if 'Lorg/efootballrevival/bootstrap/ResourceBootstrap;' not in activity:
        if activity.count(call) != 1:
            raise ValueError('Expected exactly one bootstrap injection point.')
        activity = activity.replace(call, hook)
        activity_path.write_text(activity, encoding='utf-8')
    if activity.count(hook) != 1:
        raise ValueError('Existing bootstrap patch differs from the expected hook; inspect the patch project.')
    patched_apk = BUILD / 'patched-dex.apk'
    run('java', '-jar', TOOLS / 'apktool.jar', 'b', '--output', patched_apk, patch_project)

    classes = BUILD / 'java-classes'
    dex = BUILD / 'bootstrap-dex'
    reset_compilation_directory(classes)
    reset_compilation_directory(dex)
    sources = sorted(p for p in (ROOT / 'android-src').rglob('*.java') if not p.name.endswith('Test.java'))
    if not sources:
        raise ValueError('Bootstrap Java source is missing.')
    run('javac', '-encoding', 'UTF-8', '--release', '8', '-cp', ANDROID_JAR,
        '-d', classes, *sources)
    class_jar = BUILD / 'bootstrap-classes.jar'
    run('jar', 'cf', class_jar, '-C', classes, '.')
    run('java', '-cp', SDK / 'lib/d8.jar', 'com.android.tools.r8.D8',
        '--min-api', '32', '--lib', ANDROID_JAR, '--output', dex, class_jar)

    unsigned = BUILD / 'unsigned.apk'
    config = json.dumps({'baseUrl': RESOURCE_URL}, ensure_ascii=False).encode('utf-8')
    with zipfile.ZipFile(patched_apk) as patched:
        patched_dex = patched.read('classes.dex')
    with zipfile.ZipFile(SOURCE) as src, zipfile.ZipFile(unsigned, 'w', allowZip64=False) as dst:
        for info in src.infolist():
            name = info.filename
            # ZipFile writes mutate ZipInfo offsets, CRC and sizes. Keep the
            # source metadata independent even though its reader opens first.
            target_info = copy.copy(info)
            if name == 'classes7.dex':
                dst.writestr(target_info, patched_dex)
                continue
            if name == 'classes8.dex' or name.startswith('assets/resource-bootstrap/'):
                raise ValueError(f'Unexpected existing bootstrap entry {name}')
            if name.upper().startswith('META-INF/') and name.upper().endswith(('.SF', '.RSA', '.DSA', '.EC', 'MANIFEST.MF')):
                continue
            with src.open(info) as reader, dst.open(target_info, 'w') as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
        dst.write(dex / 'classes.dex', 'classes8.dex', compress_type=zipfile.ZIP_STORED)
        dst.writestr('assets/resource-bootstrap/manifest.json', MANIFEST.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
        dst.writestr('assets/resource-bootstrap/config.json', config, compress_type=zipfile.ZIP_DEFLATED)

    aligned = BUILD / 'aligned.apk'
    run(SDK / 'zipalign.exe', '-f', '-P', '16', '4', unsigned, aligned)
    run('java', '-jar', SDK / 'lib/apksigner.jar', 'sign', '--ks', keystore,
        '--ks-key-alias', '73efootball-bootstrap', '--ks-pass', 'file:' + str(password_path),
        '--min-sdk-version', '32',
        '--v1-signing-enabled', 'false', '--v2-signing-enabled', 'true',
        '--v3-signing-enabled', 'true', '--v4-signing-enabled', 'false',
        '--out', OUTPUT, aligned)
    run('java', '-jar', SDK / 'lib/apksigner.jar', 'verify', '--verbose', '--print-certs', OUTPUT)
    run(SDK / 'zipalign.exe', '-c', '-P', '16', '4', OUTPUT)
    if OUTPUT.stat().st_size >= 2 * 1024 ** 3:
        raise ValueError('APK exceeds a GitHub Releases attachment limit.')
    record = {
        'sourceSha256': SOURCE_SHA256,
        'apk': OUTPUT.name,
        'bytes': OUTPUT.stat().st_size,
        'sha256': sha256(OUTPUT),
        'resourceBaseUrl': RESOURCE_URL,
        'manifestSha256': sha256(MANIFEST),
        'signature': 'new-local-signing-key',
        'verification': ['apksigner-v2-v3', 'zipalign-4-and-16KiB-native'],
        'deviceTest': 'not-performed-no-connected-device',
    }
    (OUTPUT.parent / 'apk-build.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    build()
