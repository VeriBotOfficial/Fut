package org.efootballrevival.bootstrap;

import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.*;
import java.util.zip.ZipEntry;
import java.util.zip.ZipOutputStream;

/** Standalone deterministic tests: javac --release 8 ResourceInstaller.java ResourceInstallerTest.java. */
public final class ResourceInstallerTest {
    private static final String BASE = "https://github.com/maxlzyver/73efootball/releases/download/resources-311010101-v1/";
    private static final long MARGIN = 64L * 1024 * 1024;
    private static int assertions, tests;

    public static void main(String[] args) throws Exception {
        normalInstallAndQuickReady();
        interruptedDownloadResumesAfterProcessRestart();
        changedEtagRestarts();
        ignoredRangeRestartsFromFullBody();
        invalidRangeRestarts();
        transientHttpErrorPreservesPartial();
        corruptArchiveRejected();
        corruptMemberRejectedAndPartialInstallRecovered();
        traversalAndUndeclaredEntriesRejected();
        changedManifestInvalidatesMarker();
        alreadyPresentResourcesNeedNoDownloadOrReserve();
        resumedSpaceAndKilledStageRecovery();
        hostAndRedirectBoundaries();
        incompleteZipRejected();
        System.out.println("PASS " + tests + " test groups; " + assertions + " assertions");
    }

    private static void normalInstallAndQuickReady() throws Exception {
        Fixture f = fixture(); FakeFactory factory = new FakeFactory(Reply.ok(f.zip));
        File save = new File(f.root, "SaveData/user.sav"); save.getParentFile().mkdirs(); write(save, bytes("KEEP"));
        ResourceInstaller installer = installer(f, factory);
        check(!installer.isReady(), "initial marker absent"); installer.install();
        check(installer.isReady(), "ready after complete install");
        check(Arrays.equals(read(new File(f.root, f.entries.get(0).path)), bytes("first-resource")), "exact bytes deployed");
        check(Arrays.equals(read(save), bytes("KEEP")), "save untouched");
        check(!new File(f.root, "resource-bootstrap-state/resources-01.zip").exists(), "archive removed after extraction");
        installer.install(); check(factory.requests.size() == 1, "ready path never downloads");
        write(new File(f.root, f.entries.get(0).path), bytes("short"));
        check(!installer.isReady(), "size change invalidates readiness"); tests++;
    }

    private static void interruptedDownloadResumesAfterProcessRestart() throws Exception {
        Fixture f = fixture(); int prefix = 31;
        FakeFactory first = new FakeFactory(Reply.broken(f.zip, prefix));
        expectIo(() -> installer(f, first).install(), "interrupted body rejected");
        check(part(f).length() == prefix, "partial bytes retained");
        FakeFactory next = new FakeFactory(Reply.range(f.zip, prefix, "\"v1\""));
        ResourceInstaller resumed = installer(f, next); resumed.install();
        check(("bytes=" + prefix + "-").equals(next.requests.get(0).getRequestProperty("Range")), "Range resumes exact offset");
        check("\"v1\"".equals(next.requests.get(0).getRequestProperty("If-Range")), "strong ETag is supplied");
        check(resumed.isReady(), "new installer instance recovers process death"); tests++;
    }

    private static void changedEtagRestarts() throws Exception {
        Fixture f = fixture(); preparePartial(f, 25);
        FakeFactory factory = new FakeFactory(Reply.range(f.zip, 25, "\"changed\""), Reply.ok(f.zip));
        installer(f, factory).install();
        check(factory.requests.size() == 2, "validator change triggers one fresh request");
        check(factory.requests.get(1).getRequestProperty("Range") == null, "fresh request has no Range"); tests++;
    }

    private static void ignoredRangeRestartsFromFullBody() throws Exception {
        Fixture f = fixture(); preparePartial(f, 25);
        FakeFactory factory = new FakeFactory(Reply.ok(f.zip)); installer(f, factory).install();
        check(factory.requests.size() == 1, "200 full response can be reused");
        check(new File(f.root, f.entries.get(1).path).length() == f.entries.get(1).bytes, "200 response truncates old partial"); tests++;
    }

    private static void invalidRangeRestarts() throws Exception {
        Fixture f = fixture(); preparePartial(f, 25);
        Reply bad = Reply.range(f.zip, 25, "\"v1\""); bad.headers.put("content-range", "bytes 24-100/200");
        FakeFactory factory = new FakeFactory(bad, Reply.ok(f.zip)); installer(f, factory).install();
        check(factory.requests.size() == 2, "mismatched range must restart");
        check(!ResourceInstaller.validRange("bytes 0-9/*", 0, 10), "unknown total rejected");
        check(!ResourceInstaller.validRange("bytes 999999999999999999999-9/10", 0, 10), "overflow range rejected"); tests++;
    }

    private static void transientHttpErrorPreservesPartial() throws Exception {
        Fixture f = fixture(); preparePartial(f, 25);
        for (int code : new int[] {429, 500, 503, 403}) {
            FakeFactory factory = new FakeFactory(new Reply(code, new byte[0]));
            expectIo(() -> installer(f, factory).install(), "HTTP " + code + " is retryable");
            check(part(f).length() == 25, "HTTP " + code + " retains partial bytes");
            check(new File(f.root, "resource-bootstrap-state/resources-01.zip.resume").isFile(), "resume validator preserved");
        }
        FakeFactory good = new FakeFactory(Reply.range(f.zip, 25, "\"v1\"")); installer(f, good).install();
        check(good.requests.get(0).getRequestProperty("Range") != null, "resume after server recovery"); tests++;
    }

    private static void corruptArchiveRejected() throws Exception {
        Fixture f = fixture(); byte[] bad = f.zip.clone(); bad[bad.length / 2] ^= 1;
        FakeFactory factory = new FakeFactory(Reply.ok(bad));
        expectIo(() -> installer(f, factory).install(), "bad archive SHA rejected");
        check(!part(f).exists(), "corrupt archive partial discarded");
        check(!new File(f.root, f.entries.get(0).path).exists(), "archive checked before extraction");
        check(!installer(f, new FakeFactory()).isReady(), "no marker after hash error"); tests++;
    }

    private static void corruptMemberRejectedAndPartialInstallRecovered() throws Exception {
        Fixture f = fixture();
        List<ResourceInstaller.Entry> badEntries = Arrays.asList(f.entries.get(0),
                new ResourceInstaller.Entry(f.entries.get(1).path, f.entries.get(1).bytes, sha(bytes("wrong"))));
        ResourceInstaller.Pack badPack = new ResourceInstaller.Pack("resources-01.zip", f.zip.length, sha(f.zip), badEntries);
        ResourceInstaller.Manifest badManifest = manifest("v1", badPack);
        FakeFactory factory = new FakeFactory(Reply.ok(f.zip));
        ResourceInstaller broken = new ResourceInstaller(f.root, badManifest, BASE, null, factory, dir -> Long.MAX_VALUE);
        expectIo(() -> broken.install(), "member hash mismatch rejected");
        check(new File(f.root, f.entries.get(0).path).isFile(), "fully verified earlier member survives");
        check(!new File(f.root, f.entries.get(1).path).exists(), "failed member never published");
        check(!broken.isReady(), "partial install has no completion marker");
        ResourceInstaller recovered = installer(f, new FakeFactory()); recovered.install();
        check(recovered.isReady(), "correct manifest recovers using verified cached ZIP"); tests++;
    }

    private static void traversalAndUndeclaredEntriesRejected() throws Exception {
        for (String badPath : new String[] {"../escape", "assetpacks/../../escape", "Download/unknown", "/absolute", "Download\\..\\escape"}) {
            Fixture f = fixture(); LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
            contents.put(badPath, bytes("malicious")); byte[] zip = zip(contents);
            ResourceInstaller.Pack pack = new ResourceInstaller.Pack("resources-01.zip", zip.length, sha(zip), f.entries);
            ResourceInstaller i = new ResourceInstaller(f.root, manifest("v1", pack), BASE, null,
                    new FakeFactory(Reply.ok(zip)), dir -> Long.MAX_VALUE);
            expectIo(() -> i.install(), "undeclared ZIP path rejected: " + badPath);
            check(!new File(f.root, "escape").exists(), "traversal wrote no file");
        }
        try { new ResourceInstaller.Entry("Download/../save", 0, sha(new byte[0])); throw new AssertionError("unsafe manifest accepted"); }
        catch (IllegalArgumentException expected) { assertions++; }
        tests++;
    }

    private static void changedManifestInvalidatesMarker() throws Exception {
        Fixture f = fixture(); installer(f, new FakeFactory(Reply.ok(f.zip))).install();
        ResourceInstaller changed = new ResourceInstaller(f.root, manifest("v2", f.pack), BASE, null,
                new FakeFactory(), dir -> 0);
        check(!changed.isReady(), "resourceVersion change invalidates old marker");
        changed.install(); check(changed.isReady(), "unchanged file contents reverified and marker updated");
        File marker = new File(f.root, "resource-bootstrap-state/complete.marker"); write(marker, bytes("invalid"));
        check(!changed.isReady(), "corrupt marker invalidates readiness"); tests++;
    }

    private static void alreadyPresentResourcesNeedNoDownloadOrReserve() throws Exception {
        Fixture f = fixture();
        writeResource(f, 0, bytes("first-resource")); writeResource(f, 1, bytes("second-resource-data"));
        ResourceInstaller i = new ResourceInstaller(f.root, f.manifest, BASE, null, new FakeFactory(), dir -> 0);
        i.install(); check(i.isReady(), "existing files validated with no download and zero free-space report"); tests++;
    }

    private static void resumedSpaceAndKilledStageRecovery() throws Exception {
        Fixture f = fixture(); int prefix = 100; preparePartial(f, prefix);
        File stale = new File(f.root, "resource-bootstrap-state/stage-" + f.entries.get(0).sha256 + ".tmp"); write(stale, bytes("orphan-from-kill"));
        long needed = f.manifest.expandedBytes + f.zip.length - prefix + MARGIN;
        ResourceInstaller i = new ResourceInstaller(f.root, f.manifest, BASE, null,
                new FakeFactory(Reply.range(f.zip, prefix, "\"v1\"")), dir -> {
                    check(!stale.exists(), "abandoned stage reclaimed before space check"); return needed;
                });
        i.install(); check(i.isReady(), "space calculation credits existing partial");
        Fixture insufficient = fixture();
        ResourceInstaller small = new ResourceInstaller(insufficient.root, insufficient.manifest, BASE, null,
                new FakeFactory(), dir -> 0);
        expectIo(() -> small.install(), "insufficient space fails before network"); tests++;
    }

    private static void hostAndRedirectBoundaries() throws Exception {
        for (String address : new String[] {"http://github.com/x", "https://github.com.evil.example/x", "https://user@github.com/x", "https://github.com:444/x", "https://example.com/x"})
            expectIo(() -> ResourceInstaller.validateUrl(new URL(address)), "unsafe URL rejected");
        ResourceInstaller.validateUrl(new URL("https://release-assets.githubusercontent.com/x?sig=public")); assertions++;
        Fixture f = fixture(); Reply redirect = new Reply(302, new byte[0]); redirect.headers.put("location", "https://release-assets.githubusercontent.com/file");
        FakeFactory factory = new FakeFactory(redirect, Reply.ok(f.zip)); installer(f, factory).install();
        check(factory.requests.size() == 2, "GitHub asset redirect followed");
        Fixture blocked = fixture(); Reply escape = new Reply(302, new byte[0]); escape.headers.put("location", "http://github.com/x");
        FakeFactory rejected = new FakeFactory(escape);
        expectIo(() -> installer(blocked, rejected).install(), "HTTPS downgrade rejected");
        check(rejected.requests.size() == 1, "blocked redirect never connected");
        Fixture loop = fixture(); Reply again = new Reply(302, new byte[0]); again.headers.put("location", "https://github.com/again");
        FakeFactory looping = new FakeFactory(again, again, again, again, again, again, again);
        expectIo(() -> installer(loop, looping).install(), "redirect bound enforced");
        check(looping.requests.size() == 6, "maximum five redirect hops"); tests++;
    }

    private static void incompleteZipRejected() throws Exception {
        Fixture f = fixture(); LinkedHashMap<String, byte[]> one = new LinkedHashMap<String, byte[]>();
        one.put(f.entries.get(0).path, bytes("first-resource")); byte[] zip = zip(one);
        ResourceInstaller.Pack pack = new ResourceInstaller.Pack("resources-01.zip", zip.length, sha(zip), f.entries);
        ResourceInstaller installer = new ResourceInstaller(f.root, manifest("v1", pack), BASE, null,
                new FakeFactory(Reply.ok(zip)), dir -> Long.MAX_VALUE);
        expectIo(() -> installer.install(), "ZIP missing declared member rejected");
        check(!installer.isReady(), "missing member cannot complete"); tests++;
    }

    private interface IoAction { void run() throws Exception; }
    private static void expectIo(IoAction action, String message) throws Exception {
        try { action.run(); throw new AssertionError(message); }
        catch (IOException expected) { assertions++; }
    }
    private static void check(boolean success, String message) { assertions++; if (!success) throw new AssertionError(message); }
    private static byte[] bytes(String value) { return value.getBytes(StandardCharsets.UTF_8); }
    private static String sha(byte[] value) throws Exception { return ResourceInstaller.hex(MessageDigest.getInstance("SHA-256").digest(value)); }
    private static void write(File file, byte[] value) throws IOException { Files.write(file.toPath(), value); }
    private static byte[] read(File file) throws IOException { return Files.readAllBytes(file.toPath()); }
    private static File part(Fixture f) { return new File(f.root, "resource-bootstrap-state/resources-01.zip.part"); }
    private static void writeResource(Fixture f, int index, byte[] bytes) throws IOException {
        File file = new File(f.root, f.entries.get(index).path); file.getParentFile().mkdirs(); write(file, bytes);
    }
    private static void preparePartial(Fixture f, int prefix) throws Exception {
        expectIo(() -> installer(f, new FakeFactory(Reply.broken(f.zip, prefix))).install(), "prepare interrupted download");
    }
    private static ResourceInstaller installer(Fixture f, FakeFactory factory) throws IOException {
        return new ResourceInstaller(f.root, f.manifest, BASE, null, factory, dir -> Long.MAX_VALUE);
    }
    private static ResourceInstaller.Manifest manifest(String version, ResourceInstaller.Pack pack) {
        return new ResourceInstaller.Manifest("test.game", 311010101, version, Collections.singletonList(pack));
    }
    private static Fixture fixture() throws Exception {
        File parent = new File("android-build/resource-installer-tests"); parent.mkdirs();
        File root = Files.createTempDirectory(parent.toPath(), "case-").toFile();
        LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
        contents.put("assetpacks/pad_ff_01/311010101/assets/test.bin", bytes("first-resource"));
        contents.put("Download/test.bin", bytes("second-resource-data"));
        ArrayList<ResourceInstaller.Entry> entries = new ArrayList<ResourceInstaller.Entry>();
        for (Map.Entry<String, byte[]> item : contents.entrySet()) entries.add(new ResourceInstaller.Entry(item.getKey(), item.getValue().length, sha(item.getValue())));
        byte[] zip = zip(contents);
        ResourceInstaller.Pack pack = new ResourceInstaller.Pack("resources-01.zip", zip.length, sha(zip), entries);
        return new Fixture(root, zip, entries, pack, manifest("v1", pack));
    }
    private static byte[] zip(LinkedHashMap<String, byte[]> contents) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (ZipOutputStream output = new ZipOutputStream(bytes)) {
            for (Map.Entry<String, byte[]> item : contents.entrySet()) {
                output.putNextEntry(new ZipEntry(item.getKey())); output.write(item.getValue()); output.closeEntry();
            }
        }
        return bytes.toByteArray();
    }
    private static final class Fixture {
        final File root; final byte[] zip; final List<ResourceInstaller.Entry> entries;
        final ResourceInstaller.Pack pack; final ResourceInstaller.Manifest manifest;
        Fixture(File root, byte[] zip, List<ResourceInstaller.Entry> entries, ResourceInstaller.Pack pack, ResourceInstaller.Manifest manifest) {
            this.root = root; this.zip = zip; this.entries = entries; this.pack = pack; this.manifest = manifest;
        }
    }
    private static final class Reply {
        final int code; final byte[] body; final Map<String, String> headers = new HashMap<String, String>(); int failAfter = -1;
        Reply(int code, byte[] body) { this.code = code; this.body = body; headers.put("content-length", Integer.toString(body.length)); }
        static Reply ok(byte[] bytes) { Reply reply = new Reply(200, bytes); reply.headers.put("etag", "\"v1\""); return reply; }
        static Reply broken(byte[] bytes, int prefix) { Reply reply = ok(bytes); reply.failAfter = prefix; return reply; }
        static Reply range(byte[] bytes, int offset, String etag) {
            Reply reply = new Reply(206, Arrays.copyOfRange(bytes, offset, bytes.length));
            reply.headers.put("etag", etag); reply.headers.put("content-range", "bytes " + offset + "-" + (bytes.length - 1) + "/" + bytes.length); return reply;
        }
    }
    private static final class FakeFactory implements ResourceInstaller.ConnectionFactory {
        final Deque<Reply> replies = new ArrayDeque<Reply>(); final List<FakeConnection> requests = new ArrayList<FakeConnection>();
        FakeFactory(Reply... replies) { this.replies.addAll(Arrays.asList(replies)); }
        public HttpURLConnection open(URL url) {
            if (replies.isEmpty()) throw new AssertionError("Unexpected network request: " + url);
            FakeConnection result = new FakeConnection(url, replies.remove()); requests.add(result); return result;
        }
    }
    private static final class FakeConnection extends HttpURLConnection {
        final Reply reply;
        FakeConnection(URL url, Reply reply) { super(url); this.reply = reply; }
        public void connect() { }
        public void disconnect() { }
        public boolean usingProxy() { return false; }
        public int getResponseCode() { return reply.code; }
        public String getHeaderField(String name) { return reply.headers.get(name.toLowerCase(Locale.ROOT)); }
        public InputStream getInputStream() {
            if (reply.failAfter < 0) return new ByteArrayInputStream(reply.body);
            return new InputStream() {
                int position;
                public int read() throws IOException {
                    if (position >= reply.failAfter) throw new IOException("simulated connection reset");
                    return reply.body[position++] & 255;
                }
                public int read(byte[] buffer, int offset, int length) throws IOException {
                    if (position >= reply.failAfter) throw new IOException("simulated connection reset");
                    int count = Math.min(length, reply.failAfter - position);
                    System.arraycopy(reply.body, position, buffer, offset, count); position += count; return count;
                }
            };
        }
    }
}
