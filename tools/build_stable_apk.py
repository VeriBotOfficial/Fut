"""Build formal efootxi 1.2 while retaining the released 1.1.1 app identity.

Original DEX1-6/packed game content are copied unchanged. Reviewed fixed-size
native UI patches, gateway and bootstrap match the reviewed test.6 behavior.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from stable_identity import PACKAGE, LABEL, VERSION_CODE, manifest, resource_table
import native_ui_patch

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT.parents[1]
SOURCE = INPUTS / 'release/v1.1.1/efootxi-1.1.1.apk'
SOURCE_SHA = '0a612a8153843c8bfe5fb515eac5b983a6ec377990eaf8635c03034b1f5ae255'
CERT = '7bc4e9ca40a17a15e32bfc0f20e55abc47b6c1e2cf701770f3be8bdbd16f93b4'
JDK = Path.home() / '.codex/tools/jdk-21.0.12.1+1/bin'
ANDROID = INPUTS / '.tools/android'
SDK = ANDROID / 'build-tools/android-16'
WORK = ROOT / 'build/stable-apk'
OUTPUT = ROOT / 'release/v1.2'
PREFIX = 'assets/gateway-release/'
VERSION = '1.2'
REVISION = 1206
TAG = 'v1.2'
EXPECTED_NATIVE_SHA = '8446ee04589a54f8d92cd6d486d4227fbbacb05253d8949bd505c1482e59ef3f'


def digest(path, algorithm='sha256'):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, algorithm).hexdigest()


def run(*args):
    r = subprocess.run([str(x) for x in args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    text = r.stdout.decode('utf-8',errors='replace')
    if r.returncode:
        print(text,flush=True)
        r.check_returncode()
    return text


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def code(source):
    compile_dir=Path(tempfile.mkdtemp(prefix='code-',dir=WORK))
    classes=compile_dir/'classes';dex=compile_dir/'dex';classes.mkdir();dex.mkdir()
    sources=sorted(p for p in (ROOT/'android-src').rglob('*.java') if not p.name.endswith('Test.java'))
    run(JDK/'javac.exe','-encoding','UTF-8','--release','8','-cp',ANDROID/'platform/android-36/android.jar','-d',classes,*sources)
    jar=compile_dir/'bootstrap.jar'
    run(JDK/'jar.exe','cf',jar,'-C',classes,'.')
    run(JDK/'java.exe','-cp',SDK/'lib/d8.jar','com.android.tools.r8.D8','--min-api','31',
        '--lib',ANDROID/'platform/android-36/android.jar','--output',dex,jar)
    patch_input=compile_dir/'patch-input.apk'
    with zipfile.ZipFile(patch_input,'w') as z:
        z.writestr('classes.dex',source.read('classes7.dex'))
        z.writestr('AndroidManifest.xml',source.read('AndroidManifest.xml'))
    patch_project=compile_dir/'patch-project'
    run(JDK/'java.exe','-jar',ANDROID/'apktool.jar','d','--no-res','--no-debug-info','--output',patch_project,patch_input)
    smali=patch_project/'smali'
    gateway=smali/'org/efootballrevival/runtimeprobe/EmbeddedGateway.smali'
    raw=gateway.read_text(encoding='utf-8')
    if raw.count('0x1f95') != 2: raise ValueError('Unexpected gateway port layout')
    raw=raw.replace('const/16 v1, 0x1f95','const/16 v1, 0x0')
    raw=raw.replace('const/16 v6, 0x1f95','invoke-static {}, Lorg/efootballrevival/bootstrap/GatewayGuard;->port()I\n\n    move-result v6')
    if '0x1f95' in raw: raise ValueError('An unpatched fixed gateway port remains')
    gateway.write_text(raw,encoding='utf-8')
    boot=smali/'org/efootballrevival/runtimeprobe/GatewayBootActivity.smali'
    raw=boot.read_text(encoding='utf-8')
    start='    invoke-static {p0}, Lorg/efootballrevival/runtimeprobe/EmbeddedGateway;->ensureStarted(Landroid/content/Context;)V'
    if raw.count(start)!=1: raise ValueError('Unexpected boot entry')
    raw=raw.replace(start,'    invoke-static {p0}, Lorg/efootballrevival/bootstrap/GatewayGuard;->prepare(Landroid/content/Context;)V\n\n'+start+
        '\n\n    invoke-static {}, Lorg/efootballrevival/bootstrap/GatewayGuard;->health()V')
    pattern=r'(\.method synthetic lambda\$onCreate\$2\$[^\n]*\(Ljava/lang/Throwable;\)V)\n.*?\.end method'
    raw,count=re.subn(pattern,lambda m:m[1]+'\n    .locals 0\n    invoke-static {p0, p1}, Lorg/efootballrevival/bootstrap/GatewayGuard;->showFailure(Landroid/app/Activity;Ljava/lang/Throwable;)V\n    return-void\n.end method',raw,flags=re.S)
    if count!=1: raise ValueError('Unexpected failure handler')
    # Ensure the published bootstrap hook remains in place.
    if 'ResourceBootstrap;->intercept' not in raw: raise ValueError('Missing resource bootstrap hook')
    boot.write_text(raw,encoding='utf-8')
    output=compile_dir/'gateway.apk'
    run(JDK/'java.exe','-jar',ANDROID/'apktool.jar','b','--output',output,patch_project)
    with zipfile.ZipFile(output) as z: gateway_dex=z.read('classes.dex')
    print('Java compiled, bootstrap DEX and gateway DEX with an OS-assigned listener assembled',flush=True)
    return {'classes8.dex':(dex/'classes.dex').read_bytes(),'classes7.dex':gateway_dex}


def gateway_payload(source):
    stage=Path(tempfile.mkdtemp(prefix='gateway-',dir=WORK))
    for info in source.infolist():
        if not info.filename.startswith(PREFIX) or info.is_dir():continue
        relative=info.filename[len(PREFIX):]
        if '..' in Path(relative).parts:raise ValueError('Invalid gateway path')
        dest=stage/relative;dest.parent.mkdir(parents=True,exist_ok=True)
        with source.open(info) as src,dest.open('wb') as dst:shutil.copyfileobj(src,dst,1024*1024)
    for p in (ROOT/'gateway').rglob('*'):
        if not p.is_file() or '__pycache__' in p.parts or p.suffix=='.pyc':continue
        dest=stage/p.relative_to(ROOT/'gateway');dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
    # Runs only our reviewed Python sources on an isolated build copy, never the APK.
    env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join([str(stage/'work/tools'),str(ROOT/'build/host-deps')])
    child='import sys,sqlite3; from pathlib import Path; import club_store,release_save,match_protocol; p=Path(sys.argv[1]); c=club_store.connect(p); release_save.clean_seed(c,new_test_seed=True); match_protocol.initialize_test_seed(c,new_test_seed=True); release_save.migrate(c); assert c.execute("pragma integrity_check").fetchone()[0]=="ok"; c.execute("pragma wal_checkpoint(TRUNCATE)"); c.execute("pragma journal_mode=DELETE"); c.close()'
    subprocess.run([sys.executable,'-c',child,str(stage/'outputs/efootball/local-club.sqlite3')],env=env,check=True)
    release=json.loads((stage/'release-manifest.json').read_text(encoding='utf-8'))
    release.update(release_id='efootxi-'+VERSION+'-r'+str(REVISION),modules=len(list((stage/'work/tools').glob('*.py'))))
    release['files']=[]
    for p in sorted(stage.rglob('*')):
        if not p.is_file() or '__pycache__' in p.parts or p.name=='release-manifest.json' or p.suffix in ('.pyc',) or p.name.endswith(('-wal','-shm','.backup')):continue
        # Migration backups are user recovery artifacts, never bundled into a seed.
        if 'backups' in p.parts or '.before-efootxi-' in p.name:continue
        relative=p.relative_to(stage).as_posix()
        release['files'].append(dict(path=relative,bytes=p.stat().st_size,sha256=digest(p)))
    write_json(stage/'release-manifest.json',release)
    result={PREFIX+entry['path']:stage/entry['path'] for entry in release['files']}
    result[PREFIX+'release-manifest.json']=stage/'release-manifest.json'
    print('Reviewed gateway staged and isolated seed migrated',flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--code-only',action='store_true')
    parser.add_argument('--local-only',action='store_true',help='Build a local formal APK without a published update URL')
    args=parser.parse_args()
    WORK.mkdir(parents=True,exist_ok=True);OUTPUT.mkdir(parents=True,exist_ok=True)
    if digest(SOURCE)!=SOURCE_SHA:raise ValueError('Unexpected base APK SHA-256')
    with zipfile.ZipFile(SOURCE) as source:
        replacements=code(source)
        if args.code_only:
            print('Code verification complete');return
        native_bytes, native_report = native_ui_patch.patch_library(source.read(native_ui_patch.LIBRARY))
        if native_report['patchedSha256'] != EXPECTED_NATIVE_SHA:
            raise ValueError('Formal engine differs from the reviewed test.6 UI patch')
        replacements[native_ui_patch.LIBRARY] = native_bytes
        write_json(OUTPUT/'native-ui-patch.json', native_report)
        # Keep original Python bytecode. gateway_runtime adapts only its
        # generated live-data URLs to the OS-selected listener at runtime.
        replacements['AndroidManifest.xml']=manifest(source.read('AndroidManifest.xml'), version=VERSION)
        resource_table(source.read('resources.arsc'))  # Validate; keep the original bytes outside replacements.
        config=json.loads(source.read('assets/resource-bootstrap/config.json'))
        config.update(channelUrl='https://github.com/maxlzyver/efootxi/releases/download/updates/channel.json',buildRevision=REVISION,versionName=VERSION)
        replacements['assets/resource-bootstrap/config.json']=json.dumps(config,ensure_ascii=False).encode()
        resources=json.loads(source.read('assets/resource-bootstrap/manifest.json'));resources['packageName']=PACKAGE
        replacements['assets/resource-bootstrap/manifest.json']=json.dumps(resources,ensure_ascii=False,indent=2).encode()
        write_json(OUTPUT/'resource-manifest.json',resources)
        replacements.update(gateway_payload(source))
        original={};newnames=set(replacements)-set(source.namelist());unsigned=WORK/'unsigned.apk'
        with zipfile.ZipFile(unsigned,'w',allowZip64=False) as target:
            for info in source.infolist():
                name=info.filename
                if name in replacements:
                    original[name]=hashlib.sha256(source.read(info)).hexdigest()
                    value=replacements[name];data=value.read_bytes() if isinstance(value,Path) else value
                    target.writestr(copy.copy(info),data)
                else:
                    h=hashlib.sha256()
                    with source.open(info) as src,target.open(copy.copy(info),'w') as dst:
                        while block:=src.read(1024*1024):h.update(block);dst.write(block)
                    original[name]=h.hexdigest()
            for name in sorted(newnames):
                value=replacements[name];data=value.read_bytes() if isinstance(value,Path) else value
                target.writestr(name,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    print('APK payload assembled; aligning and signing formal package',flush=True)
    aligned=WORK/'aligned.apk';run(SDK/'zipalign.exe','-f','-P','16','4',unsigned,aligned)
    apk=OUTPUT/'efootxi-1.2.apk'
    run(JDK/'java.exe','-jar',SDK/'lib/apksigner.jar','sign','--ks',INPUTS/'signing/release-signing.p12',
        '--ks-key-alias','73efootball-bootstrap','--ks-pass','file:'+str(INPUTS/'signing/signing-password.txt'),
        '--min-sdk-version','31','--v1-signing-enabled','false','--v2-signing-enabled','true','--v3-signing-enabled','true',
        '--v4-signing-enabled','false','--out',apk,aligned)
    signature=run(JDK/'java.exe','-jar',SDK/'lib/apksigner.jar','verify','--verbose','--print-certs','--min-sdk-version','31',apk)
    assert CERT in signature and 'Verified using v3 scheme (APK Signature Scheme v3): true' in signature
    run(SDK/'zipalign.exe','-c','-P','16','4',apk)
    badging=run(SDK/'aapt2.exe','dump','badging',apk)
    assert "package: name='"+PACKAGE+"'" in badging and "versionCode='"+str(VERSION_CODE)+"' versionName='"+VERSION+"'" in badging
    assert "minSdkVersion:'31'" in badging and "targetSdkVersion:'36'" in badging and "application-label:'"+LABEL+"'" in badging
    xml=run(SDK/'aapt2.exe','dump','xmltree',apk,'--file','AndroidManifest.xml')
    assert PACKAGE+'.efootxi.update' in xml and 'org.efootballrevival.bootstrap.EfootxiUpdateProvider' in xml
    for filename,data in [('badging.txt',badging),('signature.txt',signature),('manifest-tree.txt',xml)]: (OUTPUT/filename).write_text(data,encoding='utf-8')
    current={}
    with zipfile.ZipFile(apk) as z:
        assert len(z.namelist())==len(set(z.namelist()))
        for info in z.infolist():
            with z.open(info) as f:current[info.filename]=hashlib.file_digest(f,'sha256').hexdigest()
    assert set(current)==set(original)|newnames
    changes=sorted(n for n in original if current[n]!=original[n])
    assert set(changes)<=set(replacements)
    for name,value in replacements.items():
        expected=digest(value) if isinstance(value,Path) else hashlib.sha256(value).hexdigest()
        assert current[name]==expected,name
    for name in current:
        if name == native_ui_patch.LIBRARY:
            assert original[name] == native_report['sourceSha256'],name
            assert current[name] == native_report['patchedSha256'],name
            assert native_report['unmodifiedRangesVerified'],name
        elif name.startswith('lib/') or re.fullmatch(r'classes(?:[2-6])?\.dex',name):
            assert current[name]==original[name],name
    assert current['resources.arsc']==original['resources.arsc']
    assert apk.stat().st_size<2*1024**3
    metadata=dict(buildRevision=REVISION,versionCode=VERSION_CODE,versionName=VERSION,packageName=PACKAGE,
        sha256=digest(apk),bytes=apk.stat().st_size,signerCertificateSha256=CERT,minSdk=31,abi='arm64-v8a')
    if args.local_only:
        metadata['distribution']='local-only'
    else:
        metadata['url']='https://github.com/maxlzyver/efootxi/releases/download/'+TAG+'/'+apk.name
    report=dict(metadata,sourceSha256=SOURCE_SHA,changedEntries=changes,addedEntries=sorted(newnames),
        signatureVerification='passed-v3',alignment='4-byte-and-16KiB-passed',memberHashes='all-checked',
        originalGameDexUnchanged=True,otherNativeLibrariesUnchanged=True,
        nativeUiPatch=dict(sourceSha256=native_report['sourceSha256'],patchedSha256=native_report['patchedSha256'],
                           ranges=len(native_report['ranges']),unmodifiedRangesVerified=True,report='native-ui-patch.json'),
        samePackageAndSigner=True,resourcesArscUnchanged=True,independentSave=False,
        updateIdentity='same-as-stable-1.1.1',localPort='OS-assigned loopback port',androidDeviceTest='not-performed')
    write_json(OUTPUT/'apk-update.json',metadata);write_json(OUTPUT/'verification.json',report)
    (OUTPUT/'SHA256SUMS').write_text(metadata['sha256']+'  '+apk.name+'\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('changedEntries','addedEntries')},indent=2),flush=True)


if __name__=='__main__':main()

