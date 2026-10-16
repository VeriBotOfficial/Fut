"""Exercise the production Java installer against the smallest public resource pack."""
from pathlib import Path
import json
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / 'build' / 'network-smoke'
BASE = 'https://github.com/maxlzyver/73efootball/releases/download/resources-311010101-v1/'


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    BUILD.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((ROOT / 'release/resources/manifest.json').read_text(encoding='utf-8'))
    pack = min(manifest['files'], key=lambda item: item['bytes'])
    entries = '\n'.join(
        '        entries.add(new ResourceInstaller.Entry(%s, %dL, %s));' %
        (json.dumps(item['path']), item['bytes'], json.dumps(item['sha256']))
        for item in pack['entries']
    )
    source = '''
import java.io.*;
import java.net.*;
import java.util.*;
import org.efootballrevival.bootstrap.ResourceInstaller;
public class NetworkSmoke {
    public static void main(String[] args) throws Exception {
        List<ResourceInstaller.Entry> entries = new ArrayList<>();
%s
        ResourceInstaller.Pack pack = new ResourceInstaller.Pack(%s, %dL, %s, entries);
        ResourceInstaller.Manifest manifest = new ResourceInstaller.Manifest(%s, %dL, %s, Arrays.asList(pack));
        File root = new File(args[0]); root.mkdirs();
        ResourceInstaller.Listener listener = new ResourceInstaller.Listener() {
            private String stage = ""; private long last;
            public void progress(String next, String detail, long done, long total) {
                long now = System.currentTimeMillis();
                if (!stage.equals(next) || now - last > 3000 || done == total) {
                    System.out.println(next + " " + done + "/" + total); stage = next; last = now;
                }
            }
        };
        ResourceInstaller first = new ResourceInstaller(root, manifest, %s, listener);
        first.install();
        if (!first.isReady()) throw new AssertionError("First install was not ready");
        ResourceInstaller second = new ResourceInstaller(root, manifest, %s, listener,
            new ResourceInstaller.ConnectionFactory() {
                public HttpURLConnection open(URL url) { throw new AssertionError("Ready launch touched network"); }
            }, new ResourceInstaller.SpaceChecker() {
                public long available(File directory) { return 0; }
            });
        second.install();
        if (!second.isReady()) throw new AssertionError("Second launch was not ready");
        System.out.println("NETWORK_SMOKE_PASS");
    }
}
''' % (entries, json.dumps(pack['name']), pack['bytes'], json.dumps(pack['sha256']),
       json.dumps(manifest['packageName']), manifest['versionCode'],
       json.dumps(manifest['resourceVersion'] + '-smallest-pack-smoke'), json.dumps(BASE), json.dumps(BASE))
    java = BUILD / 'NetworkSmoke.java'
    java.write_text(source, encoding='utf-8')
    classes = BUILD / 'classes'
    classes.mkdir(exist_ok=True)
    subprocess.run(['javac', '-encoding', 'UTF-8', '--release', '8', '-d', str(classes),
                    str(ROOT / 'android-src/org/efootballrevival/bootstrap/ResourceInstaller.java'), str(java)], check=True)
    started = time.monotonic()
    result = subprocess.run(['java', '-Djava.net.useSystemProxies=true', '-Dstdout.encoding=UTF-8', '-Dstderr.encoding=UTF-8', '-cp', str(classes),
                             'NetworkSmoke', str(BUILD / ('app-files-' + str(time.time_ns())))], text=True, encoding='utf-8',
                            errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (BUILD / 'output.log').write_text(result.stdout, encoding='utf-8')
    print(result.stdout)
    result.check_returncode()
    record = {'passed': 'NETWORK_SMOKE_PASS' in result.stdout, 'pack': pack['name'],
              'bytes': pack['bytes'], 'entries': len(pack['entries']),
              'seconds': round(time.monotonic() - started, 2),
              'scope': 'Real public HTTPS download, package and member hashes, extraction, ready marker, second launch without network',
              'runtime': 'Windows Java 21 with system proxy discovery; not an Android device test'}
    (ROOT / 'release/network-smoke-verification.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
