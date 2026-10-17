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
        resourceUpdateDownloadsOnlyChangedPack();
        forceVerifyRepairsSameLengthCorruption();
        partialSurvivesManifestAndRepositoryUpdate();
        legacyPartialMetadataSurvivesUpdate();
        resumeWithoutEtagStillRequiresFinalHash();
        changedArchiveDiscardsUnrelatedPartial();
        subsetArchiveNeverInstallsSupersededFiles();
        subsetArchiveRejectsUnsafePaths();
        apkDownloadResumesAndVerifies();
        exactSeedPolicyHasNoDirectoryWildcard();
        runtimeSeedChangesDoNotRedownload();
        offlineMissingContentFailsBeforeHashOrNetwork();
        offlineCorruptContentNamesTheFailedFile();
        lostLegacyMarkerMigratesWithoutDownload();
        existingLegacyMarkerMigratesSeedReceipts();
        partialInstallCannotClaimReady();
        bundledSeedHashMustMatchBeforeDeployment();
        failedRepairKeepsLastCompletionRecord();
        pauseDownloadDisconnectsAndResumesExactOffset();
        pauseHashPreservesReadyRecord();
        pausedExtractionResumesOfflineFromCachedArchive();
        offlineApkDownloadUsesNoTransport();
        resourceUpdatePreservesRuntimeSeeds();
        pauseInterruptsBlockedReadAndKeepsPrefix();
        lostMarkerAndDamagedContentCannotOverwriteRuntimeState();
        transientFailuresAutomaticallyResumeWithoutButton();
        timeoutAndShortBodyAutomaticallyRecover();
        progressRenewsConsecutiveRetryBudget();
        expiredGithubRedirectIsRefreshedFromRelease();
        permanentDownloadFailuresDoNotRetry();
        exhaustedRetryBudgetPreservesProgress();
        pauseInterruptsRetryBackoff();
        automaticApkRetryUsesSameVerifiedTransport();
        connectionLostAfterLastByteFinishesByHash();
        networkReturnsDuringAutomaticRetry();
        signedLinkBudgetRenewsAfterRealResponse();
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

    private static void resourceUpdateDownloadsOnlyChangedPack() throws Exception {
        Fixture f = fixture(); installer(f, new FakeFactory(Reply.ok(f.zip))).install();
        byte[] replacement = bytes("newer-second-resource-data");
        LinkedHashMap<String, byte[]> changes = new LinkedHashMap<String, byte[]>();
        changes.put(f.entries.get(1).path, replacement); byte[] newZip = zip(changes);
        ResourceInstaller.Entry changed = new ResourceInstaller.Entry(f.entries.get(1).path, replacement.length, sha(replacement));
        ResourceInstaller.Pack oldSubset = new ResourceInstaller.Pack(f.pack.name, f.pack.bytes, f.pack.sha256,
                Collections.singletonList(f.entries.get(0)), BASE + f.pack.name, true);
        ResourceInstaller.Pack patch = new ResourceInstaller.Pack("changed-resource.zip", newZip.length, sha(newZip),
                Collections.singletonList(changed), "https://github.com/maxlzyver/efootxi/releases/download/resources-v2/changed-resource.zip", false);
        ResourceInstaller.Manifest update = new ResourceInstaller.Manifest("test.game", 311010101, "v2", Arrays.asList(oldSubset, patch));
        FakeFactory factory = new FakeFactory(Reply.ok(newZip));
        ResourceInstaller next = new ResourceInstaller(f.root, update, BASE, null, factory, dir -> Long.MAX_VALUE);
        check(!next.isReady(), "updated resource manifest requires integrity check"); next.install();
        check(factory.requests.size() == 1, "unchanged archive is not fetched for existing verified files");
        check(factory.requests.get(0).getURL().toString().equals(patch.url), "new content uses explicit release URL");
        check(Arrays.equals(read(new File(f.root, changed.path)), replacement), "only new resource replaces old bytes");
        check(Arrays.equals(read(new File(f.root, f.entries.get(0).path)), bytes("first-resource")), "unchanged resource retained");
        check(next.isReady(), "new resource manifest committed"); tests++;
    }

    private static void forceVerifyRepairsSameLengthCorruption() throws Exception {
        Fixture f = fixture(); installer(f, new FakeFactory(Reply.ok(f.zip))).install();
        byte[] corruption = bytes("first-resource"); corruption[0] ^= 1; writeResource(f, 0, corruption);
        bumpModified(new File(f.root, f.entries.get(0).path));
        FakeFactory factory = new FakeFactory(Reply.ok(f.zip)); ResourceInstaller next = installer(f, factory);
        check(!next.isReady(), "changed modification time triggers a content hash despite unchanged length");
        next.install(true);
        check(factory.requests.size() == 1, "explicit integrity check repairs same-length damage");
        check(Arrays.equals(read(new File(f.root, f.entries.get(0).path)), bytes("first-resource")), "damaged file restored"); tests++;
    }

    private static void partialSurvivesManifestAndRepositoryUpdate() throws Exception {
        Fixture f = fixture(); preparePartial(f, 27);
        FakeFactory factory = new FakeFactory(Reply.range(f.zip, 27, "\"v1\""));
        ResourceInstaller next = new ResourceInstaller(f.root, manifest("v2", f.pack),
                "https://github.com/maxlzyver/efootxi/releases/download/resources-v2/", null, factory, dir -> Long.MAX_VALUE);
        next.install();
        check("bytes=27-".equals(factory.requests.get(0).getRequestProperty("Range")), "unchanged content resumes across manifest and repository changes");
        check(next.isReady(), "resumed content is fully verified and deployed"); tests++;
    }

    private static void legacyPartialMetadataSurvivesUpdate() throws Exception {
        Fixture f = fixture(); preparePartial(f, 29);
        Properties old = new Properties(); old.setProperty("identity", f.manifest.identity + ":" + f.pack.sha256 + ":" + f.pack.bytes + ":" + BASE + f.pack.name);
        old.setProperty("etag", "\"v1\"");
        try (OutputStream output = new FileOutputStream(new File(f.root, "resource-bootstrap-state/resources-01.zip.resume"))) { old.store(output, "legacy"); }
        FakeFactory factory = new FakeFactory(Reply.range(f.zip, 29, "\"v1\""));
        new ResourceInstaller(f.root, manifest("v2", f.pack), BASE, null, factory, dir -> Long.MAX_VALUE).install();
        check("bytes=29-".equals(factory.requests.get(0).getRequestProperty("Range")), "old client partial survives upgrade without restarting"); tests++;
    }

    private static void resumeWithoutEtagStillRequiresFinalHash() throws Exception {
        Fixture f = fixture(); Reply broken = Reply.broken(f.zip, 32); broken.headers.remove("etag");
        expectIo(() -> installer(f, new FakeFactory(broken)).install(), "unvalidated transfer interruption retained");
        Reply range = Reply.range(f.zip, 32, "unused"); range.headers.remove("etag");
        FakeFactory factory = new FakeFactory(range); installer(f, factory).install();
        check("bytes=32-".equals(factory.requests.get(0).getRequestProperty("Range")), "hash-bound transfer resumes without ETag");
        check(factory.requests.get(0).getRequestProperty("If-Range") == null, "no invalid If-Range header sent");
        Fixture corrupt = fixture(); Reply brokenAgain = Reply.broken(corrupt.zip, 32); brokenAgain.headers.remove("etag");
        expectIo(() -> installer(corrupt, new FakeFactory(brokenAgain)).install(), "prepare second interrupted transfer");
        byte[] mixed = corrupt.zip.clone(); mixed[40] ^= 1;
        Reply badRange = Reply.range(mixed, 32, "unused"); badRange.headers.remove("etag");
        expectIo(() -> installer(corrupt, new FakeFactory(badRange)).install(), "mixed representation fails complete content hash");
        check(!part(corrupt).exists(), "failed complete content is discarded"); tests++;
    }

    private static void changedArchiveDiscardsUnrelatedPartial() throws Exception {
        Fixture f = fixture(); preparePartial(f, 24);
        LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
        byte[] changed = bytes("replacement"); contents.put(f.entries.get(0).path, changed); byte[] zip = zip(contents);
        ResourceInstaller.Entry entry = new ResourceInstaller.Entry(f.entries.get(0).path, changed.length, sha(changed));
        ResourceInstaller.Pack replacement = new ResourceInstaller.Pack(f.pack.name, zip.length, sha(zip), Collections.singletonList(entry));
        FakeFactory factory = new FakeFactory(Reply.ok(zip));
        new ResourceInstaller(f.root, manifest("v2", replacement), BASE, null, factory, dir -> Long.MAX_VALUE).install();
        check(factory.requests.get(0).getRequestProperty("Range") == null, "changed archive hash cannot reuse old bytes"); tests++;
    }

    private static void subsetArchiveNeverInstallsSupersededFiles() throws Exception {
        Fixture f = fixture();
        ResourceInstaller.Pack subset = new ResourceInstaller.Pack(f.pack.name, f.pack.bytes, f.pack.sha256,
                Collections.singletonList(f.entries.get(0)), BASE + f.pack.name, true);
        ResourceInstaller next = new ResourceInstaller(f.root, manifest("v2", subset), BASE, null,
                new FakeFactory(Reply.ok(f.zip)), dir -> Long.MAX_VALUE);
        next.install();
        check(new File(f.root, f.entries.get(0).path).isFile(), "subset installs required unchanged content");
        check(!new File(f.root, f.entries.get(1).path).exists(), "omitted obsolete member never written");
        check(next.isReady(), "subset finishes after all declared members verified");
        Fixture missing = fixture(); ResourceInstaller.Entry absent = new ResourceInstaller.Entry("Download/absent.bin", 1, sha(bytes("x")));
        ResourceInstaller.Pack badSubset = new ResourceInstaller.Pack(missing.pack.name, missing.pack.bytes, missing.pack.sha256,
                Collections.singletonList(absent), BASE + missing.pack.name, true);
        expectIo(() -> new ResourceInstaller(missing.root, manifest("v2", badSubset), BASE, null,
                new FakeFactory(Reply.ok(missing.zip)), dir -> Long.MAX_VALUE).install(), "subset still requires declared member presence"); tests++;
    }

    private static void subsetArchiveRejectsUnsafePaths() throws Exception {
        Fixture f = fixture(); LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
        contents.put("assetpacks/../../outside", bytes("bad")); contents.put(f.entries.get(0).path, bytes("first-resource"));
        byte[] zip = zip(contents); ResourceInstaller.Pack unsafe = new ResourceInstaller.Pack(f.pack.name, zip.length, sha(zip),
                Collections.singletonList(f.entries.get(0)), BASE + f.pack.name, true);
        expectIo(() -> new ResourceInstaller(f.root, manifest("v2", unsafe), BASE, null,
                new FakeFactory(Reply.ok(zip)), dir -> Long.MAX_VALUE).install(), "skipped members still reject path traversal");
        check(!new File(f.root, f.entries.get(0).path).exists(), "unsafe archive cannot deploy later members"); tests++;
    }

    private static void apkDownloadResumesAndVerifies() throws Exception {
        Fixture f = fixture(); byte[] apk = bytes("APK-content-signed-elsewhere"); String hash = sha(apk);
        String url = "https://github.com/maxlzyver/efootxi/releases/download/app-v3/efootxi.apk";
        expectIo(() -> installer(f, new FakeFactory(Reply.broken(apk, 9))).downloadArtifact("update.apk", apk.length, hash, url), "APK interrupted download retained");
        FakeFactory factory = new FakeFactory(Reply.range(apk, 9, "\"v1\""));
        ResourceInstaller next = installer(f, factory); File result = next.downloadArtifact("update.apk", apk.length, hash, url);
        check("bytes=9-".equals(factory.requests.get(0).getRequestProperty("Range")), "APK uses persistent range resume");
        check(Arrays.equals(read(result), apk), "downloaded APK hash matches manifest");
        next.downloadArtifact("update.apk", apk.length, hash, url);
        check(factory.requests.size() == 1, "verified cached APK reused");
        check(!next.isReady(), "downloading APK alone does not mark game resources ready");
        expectIo(() -> next.downloadArtifact("../outside.apk", apk.length, hash, url), "APK filename cannot escape state directory"); tests++;
    }

    private static final String SEED_PATH = "assetpacks/pad_ff_00/311010101/311010101/_metadata/properties.dat";
    private static final byte[] SEED_BYTES = bytes("#Thu Oct 15 16:01:46 GMT+08:00 2026\nmoduleVersionTag=311010101\n");

    private static void exactSeedPolicyHasNoDirectoryWildcard() throws Exception {
        check(ResourcePolicy.seedCount() == 125, "baseline seed policy contains exactly the reviewed 125 identities");
        check(ResourcePolicy.isSeed(new ResourceInstaller.Entry(SEED_PATH, SEED_BYTES.length, sha(SEED_BYTES))), "reviewed baseline metadata identity recognized");
        check(!ResourcePolicy.isSeed(new ResourceInstaller.Entry(SEED_PATH, SEED_BYTES.length, sha(bytes("different")))), "changed seed identity does not weaken integrity automatically");
        check(!ResourcePolicy.isSeed(new ResourceInstaller.Entry("assetpacks/unknown/_metadata/properties.dat", SEED_BYTES.length, sha(SEED_BYTES))), "metadata directory is not a wildcard");
        check(!ResourcePolicy.isSeed(new ResourceInstaller.Entry("Download/user.bin", 1, sha(new byte[] {0}))), "Download directory is not a wildcard"); tests++;
    }

    private static void runtimeSeedChangesDoNotRedownload() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        byte[] changed = bytes("game-maintained-properties-now-a-different-size"); write(new File(f.root, SEED_PATH), changed);
        Files.delete(new File(f.root, "Download/dummy").toPath());
        FakeFactory offline = new FakeFactory(); final int[] checks = {0};
        ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE, (stage, detail, done, total) -> {
            if (stage.equals("检查") && detail.equals("核实已有资源")) checks[0]++;
        }, offline, dir -> Long.MAX_VALUE).setSeedSource(ResourceInstallerTest::seedStream).setNetworkAllowed(false);
        next.install();
        check(offline.requests.isEmpty(), "runtime seed changes make zero network requests");
        check(checks[0] == f.entries.size(), "unchanged content uses persisted verification receipt without re-reading payload bytes");
        check(Arrays.equals(read(new File(f.root, SEED_PATH)), changed), "runtime metadata is preserved");
        check(!new File(f.root, "Download/dummy").exists(), "consumed seed is not recreated after recorded installation");
        check(next.isReady(), "game-managed seed deletion remains ready while content is intact"); tests++;
    }

    private static void offlineMissingContentFailsBeforeHashOrNetwork() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        byte[] marker = read(marker(f)); Files.delete(new File(f.root, f.entries.get(0).path).toPath());
        FakeFactory offline = new FakeFactory(); final int[] hashProgress = {0};
        ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE, (stage, detail, done, total) -> {
            if (stage.equals("校验")) hashProgress[0]++;
        }, offline, dir -> Long.MAX_VALUE).setSeedSource(ResourceInstallerTest::seedStream).setNetworkAllowed(false);
        try { next.install(); throw new AssertionError("missing content accepted"); }
        catch (ResourceInstaller.OfflineRequiredException expected) { check(expected.getMessage().contains(f.entries.get(0).path), "offline error identifies missing necessary file"); }
        check(hashProgress[0] == 0 && offline.requests.isEmpty(), "offline preflight fails before large-file checks and transport");
        check(Arrays.equals(read(marker(f)), marker), "offline failure preserves last successful record");
        check(!next.diagnostics().isEmpty() && next.diagnostics().get(0).contains("文件缺失"), "missing-file diagnostics retained"); tests++;
    }

    private static void offlineCorruptContentNamesTheFailedFile() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        File content = new File(f.root, f.entries.get(0).path); byte[] corruption = read(content); corruption[0] ^= 1; write(content, corruption); bumpModified(content);
        FakeFactory offline = new FakeFactory(); ResourceInstaller next = seeded(f, offline).setNetworkAllowed(false);
        try { next.install(); throw new AssertionError("corrupt content accepted"); }
        catch (ResourceInstaller.OfflineRequiredException expected) { check(expected.getMessage().contains("SHA-256"), "offline hash failure explains actual mismatch"); }
        check(offline.requests.isEmpty(), "corrupt offline file never triggers DNS or download");
        check(!next.isReady(), "known corruption invalidates cached verification receipt");
        check(marker(f).isFile(), "last completion record survives corrupt-file diagnosis"); tests++;
    }

    private static void lostLegacyMarkerMigratesWithoutDownload() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        Files.delete(marker(f).toPath()); Files.delete(receipt(f).toPath());
        byte[] runtime = bytes("changed-by-the-game"); write(new File(f.root, SEED_PATH), runtime);
        Files.delete(new File(f.root, "Download/dummy").toPath());
        FakeFactory offline = new FakeFactory(); ResourceInstaller next = seeded(f, offline).setNetworkAllowed(false); next.install();
        check(next.isReady() && offline.requests.isEmpty(), "lost old marker migrates locally after content SHA verification");
        check(Arrays.equals(read(new File(f.root, SEED_PATH)), runtime), "migration retains existing game-managed metadata");
        check(Arrays.equals(read(new File(f.root, "Download/dummy")), new byte[] {0}), "missing seed restored from verified embedded bytes only once");
        Files.delete(new File(f.root, "Download/dummy").toPath());
        seeded(f, offline).setNetworkAllowed(false).install();
        check(!new File(f.root, "Download/dummy").exists(), "new receipt prevents repeating seed restoration"); tests++;
    }

    private static void existingLegacyMarkerMigratesSeedReceipts() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        Files.delete(receipt(f).toPath()); write(new File(f.root, SEED_PATH), bytes("runtime state"));
        Files.delete(new File(f.root, "Download/dummy").toPath());
        ResourceInstaller next = installer(f, new FakeFactory()).setNetworkAllowed(false); next.install();
        check(next.isReady(), "matching legacy complete marker migrates exact seed receipts");
        check(!new File(f.root, "Download/dummy").exists(), "legacy proven deployment does not resurrect consumed seed"); tests++;
    }

    private static void partialInstallCannotClaimReady() throws Exception {
        Fixture f = seedFixture(); File seed = new File(f.root, SEED_PATH); seed.getParentFile().mkdirs(); write(seed, SEED_BYTES);
        ResourceInstaller next = seeded(f, new FakeFactory()).setNetworkAllowed(false);
        check(!next.isReady(), "seed presence cannot replace content installation");
        try { next.install(); throw new AssertionError("partial install accepted"); }
        catch (ResourceInstaller.OfflineRequiredException expected) { assertions++; }
        check(!marker(f).exists(), "partial installation cannot create a completion marker"); tests++;
    }

    private static void bundledSeedHashMustMatchBeforeDeployment() throws Exception {
        Fixture f = seedFixture(); File content = new File(f.root, f.entries.get(0).path); content.getParentFile().mkdirs(); write(content, bytes("first-resource"));
        ResourceInstaller next = installer(f, new FakeFactory()).setNetworkAllowed(false).setSeedSource(path -> new ByteArrayInputStream(bytes("corrupt")));
        expectIo(() -> next.install(), "corrupt bundled seed fails before publication");
        check(!new File(f.root, SEED_PATH).exists() && !marker(f).exists(), "invalid bundled seed cannot be installed or marked complete"); tests++;
    }

    private static void failedRepairKeepsLastCompletionRecord() throws Exception {
        Fixture f = fixture(); installer(f, new FakeFactory(Reply.ok(f.zip))).install(); byte[] last = read(marker(f));
        writeResource(f, 0, bytes("damaged"));
        ResourceInstaller next = installer(f, new FakeFactory(new Reply(503, new byte[0])));
        expectIo(() -> next.install(), "failed repair reports transport error");
        check(Arrays.equals(read(marker(f)), last), "repair never erases previous completion record prematurely");
        check(!next.isReady(), "last record alone cannot authorize damaged content"); tests++;
    }

    private static void pauseDownloadDisconnectsAndResumesExactOffset() throws Exception {
        Fixture f = largeFixture(); int offset = 128 * 1024;
        FakeFactory factory = new FakeFactory(Reply.ok(f.zip), Reply.range(f.zip, offset, "\"v1\""));
        final ResourceInstaller[] holder = new ResourceInstaller[1]; final boolean[] requested = {false};
        holder[0] = new ResourceInstaller(f.root, f.manifest, BASE, (stage, detail, done, total) -> {
            if (stage.equals("下载") && done >= offset && !requested[0]) { requested[0] = true; holder[0].requestPause(); }
        }, factory, dir -> Long.MAX_VALUE);
        try { holder[0].install(); throw new AssertionError("pause not observed"); }
        catch (ResourceInstaller.PausedException expected) { assertions++; }
        check(part(f).length() == offset, "pause persists exact downloaded prefix");
        check(factory.requests.get(0).disconnected, "pause disconnects active HTTP connection");
        check(!marker(f).exists(), "paused first installation is not marked complete");
        holder[0].resume(); holder[0].install();
        check(("bytes=" + offset + "-").equals(factory.requests.get(1).getRequestProperty("Range")), "continue resumes from exact saved byte offset");
        check(holder[0].isReady(), "resumed archive and contents pass complete verification"); tests++;
    }

    private static void pauseHashPreservesReadyRecord() throws Exception {
        Fixture f = largeFixture(); installer(f, new FakeFactory(Reply.ok(f.zip))).install(); byte[] completed = read(marker(f));
        final ResourceInstaller[] holder = new ResourceInstaller[1]; final boolean[] requested = {false};
        holder[0] = new ResourceInstaller(f.root, f.manifest, BASE, (stage, detail, done, total) -> {
            if (stage.equals("校验") && done > 0 && !requested[0]) { requested[0] = true; holder[0].requestPause(); }
        }, new FakeFactory(), dir -> Long.MAX_VALUE);
        try { holder[0].install(true); throw new AssertionError("hash pause ignored"); }
        catch (ResourceInstaller.PausedException expected) { assertions++; }
        check(Arrays.equals(read(marker(f)), completed), "pausing verification retains successful-install record");
        holder[0].resume(); holder[0].install(true); check(holder[0].isReady(), "verification can resume locally"); tests++;
    }

    private static void pausedExtractionResumesOfflineFromCachedArchive() throws Exception {
        Fixture f = largeFixture(); FakeFactory factory = new FakeFactory(Reply.ok(f.zip));
        final ResourceInstaller[] holder = new ResourceInstaller[1]; final boolean[] requested = {false};
        holder[0] = new ResourceInstaller(f.root, f.manifest, BASE, (stage, detail, done, total) -> {
            if (stage.equals("解压") && done > 0 && !requested[0]) { requested[0] = true; holder[0].requestPause(); }
        }, factory, dir -> Long.MAX_VALUE);
        try { holder[0].install(); throw new AssertionError("extraction pause ignored"); }
        catch (ResourceInstaller.PausedException expected) { assertions++; }
        check(new File(f.root, "resource-bootstrap-state/resources-01.zip").isFile(), "verified complete archive survives extraction pause");
        check(!new File(f.root, f.entries.get(0).path).exists(), "partial extracted file is never published");
        holder[0].resume(); holder[0].setNetworkAllowed(false).install();
        check(factory.requests.size() == 1 && holder[0].isReady(), "cached archive finishes extraction offline without another request"); tests++;
    }

    private static void offlineApkDownloadUsesNoTransport() throws Exception {
        Fixture f = fixture(); byte[] apk = bytes("verified-update-package"); String hash = sha(apk);
        String url = "https://github.com/maxlzyver/efootxi/releases/download/app/update.apk";
        FakeFactory offline = new FakeFactory(); ResourceInstaller unavailable = installer(f, offline).setNetworkAllowed(false);
        try { unavailable.downloadArtifact("update.apk", apk.length, hash, url); throw new AssertionError("offline APK request allowed"); }
        catch (ResourceInstaller.OfflineRequiredException expected) { assertions++; }
        check(offline.requests.isEmpty(), "APK transport also obeys offline guard");
        installer(f, new FakeFactory(Reply.ok(apk))).downloadArtifact("update.apk", apk.length, hash, url);
        File cached = unavailable.downloadArtifact("update.apk", apk.length, hash, url);
        check(Arrays.equals(read(cached), apk) && offline.requests.isEmpty(), "verified downloaded APK can be reused offline"); tests++;
    }

    private static void resourceUpdatePreservesRuntimeSeeds() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        byte[] runtime = bytes("runtime metadata changed during game"); write(new File(f.root, SEED_PATH), runtime);
        Files.delete(new File(f.root, "Download/dummy").toPath());
        byte[] newer = bytes("new-content-version"); LinkedHashMap<String, byte[]> content = new LinkedHashMap<String, byte[]>();
        content.put(f.entries.get(0).path, newer); byte[] archive = zip(content);
        ResourceInstaller.Entry changed = new ResourceInstaller.Entry(f.entries.get(0).path, newer.length, sha(newer));
        ResourceInstaller.Pack seeds = new ResourceInstaller.Pack(f.pack.name, f.pack.bytes, f.pack.sha256,
                f.entries.subList(1, f.entries.size()), BASE + f.pack.name, true);
        ResourceInstaller.Pack patch = new ResourceInstaller.Pack("content-update.zip", archive.length, sha(archive), Collections.singletonList(changed));
        ResourceInstaller.Manifest update = new ResourceInstaller.Manifest("test.game", 311010101, "v2", Arrays.asList(seeds, patch));
        FakeFactory factory = new FakeFactory(Reply.ok(archive));
        ResourceInstaller next = new ResourceInstaller(f.root, update, BASE, null, factory, dir -> Long.MAX_VALUE)
                .setSeedSource(ResourceInstallerTest::seedStream);
        next.install();
        check(factory.requests.size() == 1 && factory.requests.get(0).getURL().getPath().endsWith("/content-update.zip"), "resource update downloads only changed content pack");
        check(Arrays.equals(read(new File(f.root, SEED_PATH)), runtime), "resource revision does not overwrite game-maintained metadata");
        check(!new File(f.root, "Download/dummy").exists(), "resource revision does not recreate consumed unchanged seed");
        check(next.isReady(), "updated content and retained seeds form ready installation"); tests++;
    }

    private static void pauseInterruptsBlockedReadAndKeepsPrefix() throws Exception {
        Fixture f = fixture(); int prefix = 32;
        final java.util.concurrent.CountDownLatch blocked = new java.util.concurrent.CountDownLatch(1);
        final java.util.concurrent.CountDownLatch disconnected = new java.util.concurrent.CountDownLatch(1);
        ResourceInstaller.ConnectionFactory factory = url -> new HttpURLConnection(url) {
            public void connect() { }
            public boolean usingProxy() { return false; }
            public int getResponseCode() { return 200; }
            public void disconnect() { disconnected.countDown(); }
            public String getHeaderField(String key) {
                if ("Content-Length".equalsIgnoreCase(key)) return Integer.toString(f.zip.length);
                if ("ETag".equalsIgnoreCase(key)) return "\"v1\"";
                return null;
            }
            public InputStream getInputStream() {
                return new InputStream() {
                    boolean first = true;
                    public int read() throws IOException { throw new IOException("unused scalar read"); }
                    public int read(byte[] buffer, int offset, int length) throws IOException {
                        if (first) { first = false; System.arraycopy(f.zip, 0, buffer, offset, prefix); return prefix; }
                        blocked.countDown();
                        try {
                            if (!disconnected.await(5, java.util.concurrent.TimeUnit.SECONDS)) throw new IOException("test disconnect timed out");
                        } catch (InterruptedException interrupted) { Thread.currentThread().interrupt(); throw new IOException(interrupted); }
                        throw new IOException("connection closed by pause");
                    }
                };
            }
        };
        final ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE, null, factory, dir -> Long.MAX_VALUE);
        final java.util.concurrent.atomic.AtomicReference<Throwable> failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
        Thread worker = new Thread(() -> { try { next.install(); } catch (Throwable error) { failure.set(error); } }); worker.start();
        check(blocked.await(5, java.util.concurrent.TimeUnit.SECONDS), "download reaches blocking socket read");
        next.requestPause(); worker.join(5000);
        check(!worker.isAlive() && failure.get() instanceof ResourceInstaller.PausedException, "disconnect wakes blocking read and produces pause state instead of network error");
        check(part(f).length() == prefix, "blocking-read pause keeps completed prefix");
        FakeFactory resumed = new FakeFactory(Reply.range(f.zip, prefix, "\"v1\"")); installer(f, resumed).install();
        check(("bytes=" + prefix + "-").equals(resumed.requests.get(0).getRequestProperty("Range")), "new process resumes prefix after blocked-read pause"); tests++;
    }

    private static void lostMarkerAndDamagedContentCannotOverwriteRuntimeState() throws Exception {
        Fixture f = seedFixture(); seeded(f, new FakeFactory(Reply.ok(f.zip))).install();
        Files.delete(marker(f).toPath()); Files.delete(receipt(f).toPath());
        byte[] runtime = bytes("game state newer than original snapshot"); write(new File(f.root, SEED_PATH), runtime);
        write(new File(f.root, f.entries.get(0).path), bytes("broken"));
        ResourceInstaller next = seeded(f, new FakeFactory(new Reply(503, new byte[0])));
        expectIo(() -> next.install(), "content repair fails after missing-marker migration");
        check(Arrays.equals(read(new File(f.root, SEED_PATH)), runtime), "absence of marker plus damaged content never authorizes rollback of existing runtime state");
        check(!marker(f).exists() && !next.isReady(), "failed content repair remains incomplete despite accepted runtime state"); tests++;
    }

    private static void transientFailuresAutomaticallyResumeWithoutButton() throws Exception {
        Fixture f = fixture(); int prefix = 31;
        Reply limited = new Reply(429, new byte[0]); limited.headers.put("retry-after", "5");
        FakeFactory factory = new FakeFactory(Reply.broken(f.zip, prefix), new Reply(503, new byte[0]), limited,
                Reply.range(f.zip, prefix, "\"v1\""));
        List<Long> waits = new ArrayList<Long>(); List<String> stages = new ArrayList<String>();
        ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE,
                (stage, detail, done, total) -> stages.add(stage), factory, dir -> Long.MAX_VALUE, 3, waits::add);
        next.install();
        check(next.isReady(), "connection reset, HTTP 503 and rate limiting recover in one install call");
        check(waits.equals(Arrays.asList(1000L, 2000L, 5000L)), "backoff increases and observes bounded Retry-After");
        for (int i = 1; i < factory.requests.size(); i++) {
            check(("bytes=" + prefix + "-").equals(factory.requests.get(i).getRequestProperty("Range")), "every automatic request retains exact partial prefix");
            check("\"v1\"".equals(factory.requests.get(i).getRequestProperty("If-Range")), "ETag written before interrupted body is reused immediately");
        }
        check(Collections.frequency(stages, "自动重试") == 3, "player sees automatic reconnect state for each wait");
        for (FakeConnection connection : factory.requests) check(connection.disconnected, "failed and completed sockets close");
        tests++;
    }

    private static void timeoutAndShortBodyAutomaticallyRecover() throws Exception {
        Fixture f = fixture(); int prefix = 23;
        Reply timeout = Reply.ok(f.zip); timeout.headerFailure = new java.net.SocketTimeoutException("simulated connect timeout");
        Reply shortBody = new Reply(200, Arrays.copyOf(f.zip, prefix));
        shortBody.headers.put("content-length", Integer.toString(f.zip.length)); shortBody.headers.put("etag", "\"v1\"");
        FakeFactory factory = new FakeFactory(timeout, shortBody, Reply.range(f.zip, prefix, "\"v1\""));
        List<Long> waits = new ArrayList<Long>();
        ResourceInstaller next = retrying(f, factory, 2, waits); next.install();
        check(next.isReady() && factory.requests.size() == 3, "connect timeout and clean but truncated EOF both recover");
        check(factory.requests.get(0).disconnected, "header timeout closes its socket before retry");
        check(("bytes=" + prefix + "-").equals(factory.requests.get(2).getRequestProperty("Range")), "truncated EOF keeps verified-identity prefix");
        check(waits.equals(Arrays.asList(1000L, 1000L)), "newly received bytes renew no-progress retry budget"); tests++;
    }

    private static void progressRenewsConsecutiveRetryBudget() throws Exception {
        Fixture f = fixture(); int first = 19, second = 41;
        Reply advancing = Reply.range(f.zip, first, "\"v1\""); advancing.failAfter = second - first;
        FakeFactory factory = new FakeFactory(Reply.broken(f.zip, first), new Reply(503, new byte[0]),
                advancing, new Reply(503, new byte[0]), Reply.range(f.zip, second, "\"v1\""));
        List<Long> waits = new ArrayList<Long>(); ResourceInstaller next = retrying(f, factory, 2, waits);
        next.install();
        check(next.isReady() && factory.requests.size() == 5, "large transfer is not limited by cumulative transient errors");
        check(waits.equals(Arrays.asList(1000L, 2000L, 1000L, 2000L)), "any new bytes reset consecutive-failure backoff");
        check(("bytes=" + second + "-").equals(factory.requests.get(4).getRequestProperty("Range")), "later reconnect resumes newest prefix"); tests++;
    }

    private static void expiredGithubRedirectIsRefreshedFromRelease() throws Exception {
        Fixture f = fixture(); preparePartial(f, 29);
        Reply expired = new Reply(302, new byte[0]); expired.headers.put("location", "https://release-assets.githubusercontent.com/release/old?token=expired");
        Reply refreshed = new Reply(302, new byte[0]); refreshed.headers.put("location", "https://release-assets.githubusercontent.com/release/new?token=fresh");
        FakeFactory factory = new FakeFactory(expired, new Reply(403, new byte[0]), refreshed, Reply.range(f.zip, 29, "\"v1\""));
        ResourceInstaller next = retrying(f, factory, 2, new ArrayList<Long>()); next.install();
        check(next.isReady() && factory.requests.size() == 4, "expired GitHub signed redirect renews and finishes");
        URL restarted = factory.requests.get(2).getURL();
        check("github.com".equals(restarted.getHost()) && restarted.getPath().equals(new URL(BASE + f.pack.name).getPath()), "expired asset returns to immutable Release URL");
        check(restarted.getQuery() != null && restarted.getQuery().startsWith("efootxi_retry="), "expired redirect refresh bypasses stale signed-URL cache");
        for (FakeConnection connection : factory.requests) {
            check("bytes=29-".equals(connection.getRequestProperty("Range")), "redirect renewal never discards downloaded prefix");
            check("no-cache".equals(connection.getRequestProperty("Cache-Control")) && !connection.getUseCaches(), "reconnect disables URL connection cache");
        }
        Fixture denied = fixture();
        FakeFactory repeated = new FakeFactory(expired, new Reply(403, new byte[0]), expired, new Reply(403, new byte[0]), expired, new Reply(403, new byte[0]));
        expectIo(() -> retrying(denied, repeated, 12, new ArrayList<Long>()).install(), "repeated signed-link rejection is bounded");
        check(repeated.requests.size() == 6, "signed-link rejection stops after original and two refreshed links"); tests++;
    }

    private static void permanentDownloadFailuresDoNotRetry() throws Exception {
        for (int status : new int[] {400, 401, 403, 404, 410, 501, 505}) {
            Fixture f = fixture(); FakeFactory factory = new FakeFactory(new Reply(status, new byte[0]));
            List<Long> waits = new ArrayList<Long>();
            expectIo(() -> retrying(f, factory, 12, waits).install(), "permanent HTTP status does not spin: " + status);
            check(factory.requests.size() == 1 && waits.isEmpty(), "permanent HTTP error stops immediately: " + status);
        }
        Fixture corrupt = fixture(); byte[] bad = corrupt.zip.clone(); bad[40] ^= 1;
        FakeFactory badFactory = new FakeFactory(Reply.ok(bad)); List<Long> waits = new ArrayList<Long>();
        expectIo(() -> retrying(corrupt, badFactory, 12, waits).install(), "hash mismatch cannot trigger repeated corrupt downloads");
        check(badFactory.requests.size() == 1 && waits.isEmpty() && !part(corrupt).exists(), "failed SHA discards corrupt data and remains an explicit failure");
        Fixture disk = fixture(); FakeFactory diskFactory = new FakeFactory(Reply.ok(disk.zip));
        ResourceInstaller diskInstaller = retrying(disk, diskFactory, 12, waits);
        check(part(disk).mkdir(), "prepare unusable local output directory");
        write(new File(part(disk), "blocker"), bytes("cannot replace local directory"));
        expectIo(() -> diskInstaller.install(), "local disk/path failure is not transport retryable");
        check(diskFactory.requests.size() <= 1 && waits.isEmpty(), "unwritable local output does not repeat network requests");
        Fixture tls = fixture(); Reply rejected = Reply.ok(tls.zip);
        rejected.headerFailure = new javax.net.ssl.SSLHandshakeException("certificate rejected");
        FakeFactory tlsFactory = new FakeFactory(rejected);
        expectIo(() -> retrying(tls, tlsFactory, 12, waits).install(), "certificate failure does not weaken or retry TLS");
        check(tlsFactory.requests.size() == 1 && waits.isEmpty(), "TLS identity failure fails immediately"); tests++;
    }

    private static void exhaustedRetryBudgetPreservesProgress() throws Exception {
        Fixture f = fixture(); preparePartial(f, 37);
        FakeFactory factory = new FakeFactory(new Reply(503, new byte[0]), new Reply(503, new byte[0]), new Reply(503, new byte[0]));
        List<Long> waits = new ArrayList<Long>();
        expectIo(() -> retrying(f, factory, 2, waits).install(), "continuous no-progress failures eventually give actionable error");
        check(factory.requests.size() == 3 && waits.size() == 2, "retry budget counts failures without progress");
        check(part(f).length() == 37 && !marker(f).exists(), "exhausted attempts retain prefix without declaring ready");
        FakeFactory recovered = new FakeFactory(Reply.range(f.zip, 37, "\"v1\""));
        ResourceInstaller next = retrying(f, recovered, 2, new ArrayList<Long>()); next.install();
        check(next.isReady() && "bytes=37-".equals(recovered.requests.get(0).getRequestProperty("Range")), "later manual recovery resumes exact old prefix"); tests++;
    }

    private static void pauseInterruptsRetryBackoff() throws Exception {
        Fixture f = fixture(); preparePartial(f, 27);
        Reply longWait = new Reply(429, new byte[0]); longWait.headers.put("retry-after", "60");
        FakeFactory factory = new FakeFactory(longWait);
        java.util.concurrent.CountDownLatch waiting = new java.util.concurrent.CountDownLatch(1);
        ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE,
                (stage, detail, done, total) -> { if (stage.equals("自动重试")) waiting.countDown(); }, factory, dir -> Long.MAX_VALUE);
        java.util.concurrent.atomic.AtomicReference<Throwable> failure = new java.util.concurrent.atomic.AtomicReference<Throwable>();
        Thread worker = new Thread(() -> { try { next.install(); } catch (Throwable error) { failure.set(error); } }); worker.start();
        check(waiting.await(5, java.util.concurrent.TimeUnit.SECONDS), "production path enters a 60-second server backoff");
        long start = System.nanoTime(); next.requestPause(); worker.join(2000);
        check(!worker.isAlive() && failure.get() instanceof ResourceInstaller.PausedException, "pause wakes production retry wait immediately");
        check((System.nanoTime() - start) < 2000000000L && part(f).length() == 27, "pause preserves prefix without waiting for backoff");
        check(factory.requests.size() == 1 && factory.requests.get(0).disconnected, "paused wait has no active socket or late reconnect"); tests++;
    }

    private static void automaticApkRetryUsesSameVerifiedTransport() throws Exception {
        Fixture f = fixture(); byte[] apk = bytes("apk-content-automatic-resume");
        FakeFactory factory = new FakeFactory(Reply.broken(apk, 8), Reply.range(apk, 8, "\"v1\""));
        ResourceInstaller next = retrying(f, factory, 2, new ArrayList<Long>());
        File downloaded = next.downloadArtifact("update.apk", apk.length, sha(apk), "https://github.com/maxlzyver/efootxi-test/releases/download/test/update.apk");
        check(Arrays.equals(apk, read(downloaded)) && "bytes=8-".equals(factory.requests.get(1).getRequestProperty("Range")), "APK updates automatically resume and retain full SHA verification");
        check(!next.isReady(), "APK transport never claims game resource readiness"); tests++;
    }

    private static ResourceInstaller retrying(Fixture f, FakeFactory factory, int retries, List<Long> waits) throws IOException {
        return new ResourceInstaller(f.root, f.manifest, BASE, null, factory, dir -> Long.MAX_VALUE, retries, waits::add);
    }

    private static void connectionLostAfterLastByteFinishesByHash() throws Exception {
        Fixture f = fixture(); FakeFactory factory = new FakeFactory(Reply.broken(f.zip, f.zip.length));
        List<Long> waits = new ArrayList<Long>(); ResourceInstaller next = retrying(f, factory, 2, waits); next.install();
        check(next.isReady() && factory.requests.size() == 1 && waits.isEmpty(), "connection reset after final expected byte validates full SHA without downloading again");
        Fixture corrupt = fixture(); byte[] bad = corrupt.zip.clone(); bad[40] ^= 1;
        FakeFactory badFactory = new FakeFactory(Reply.broken(bad, bad.length));
        expectIo(() -> retrying(corrupt, badFactory, 2, new ArrayList<Long>()).install(), "last-byte connection failure still requires valid full SHA");
        check(!part(corrupt).exists() && !marker(corrupt).exists(), "corrupt full partial is discarded instead of being accepted or resumed"); tests++;
    }

    private static void networkReturnsDuringAutomaticRetry() throws Exception {
        Fixture f = fixture(); FakeFactory restored = new FakeFactory(Reply.ok(f.zip)); int[] opens = {0};
        List<Long> waits = new ArrayList<Long>();
        ResourceInstaller.ConnectionFactory outage = address -> {
            opens[0]++;
            if (opens[0] == 1) throw new java.net.UnknownHostException("network offline");
            if (opens[0] == 2) throw new java.net.NoRouteToHostException("network changing");
            return restored.open(address);
        };
        ResourceInstaller next = new ResourceInstaller(f.root, f.manifest, BASE, null, outage,
                dir -> Long.MAX_VALUE, 2, waits::add); next.install();
        check(next.isReady() && opens[0] == 3, "network returning after DNS/routing outage continues same installation automatically");
        check(waits.equals(Arrays.asList(1000L, 2000L)), "offline connection attempts follow bounded backoff"); tests++;
    }

    private static void signedLinkBudgetRenewsAfterRealResponse() throws Exception {
        Fixture f = fixture(); Reply link = new Reply(302, new byte[0]);
        link.headers.put("location", "https://release-assets.githubusercontent.com/release/asset?token=value");
        int first = 21, second = 43;
        Reply firstBody = Reply.broken(f.zip, first);
        Reply secondBody = Reply.range(f.zip, first, "\"v1\""); secondBody.failAfter = second - first;
        FakeFactory factory = new FakeFactory(link, new Reply(403, new byte[0]), link, firstBody,
                link, new Reply(403, new byte[0]), link, secondBody,
                link, new Reply(403, new byte[0]), link, Reply.range(f.zip, second, "\"v1\""));
        ResourceInstaller next = retrying(f, factory, 12, new ArrayList<Long>()); next.install();
        check(next.isReady() && factory.requests.size() == 12, "three separate temporary-link expiries across advancing transfer each recover");
        check(("bytes=" + second + "-").equals(factory.requests.get(11).getRequestProperty("Range")), "valid signed response renews link-error budget without losing latest bytes"); tests++;
    }

    private static File marker(Fixture f) { return new File(f.root, "resource-bootstrap-state/complete.marker"); }
    private static File receipt(Fixture f) { return new File(f.root, "resource-bootstrap-state/verified-files.properties"); }
    private static void bumpModified(File file) throws IOException {
        if (!file.setLastModified(file.lastModified() + 5000)) throw new IOException("cannot set test timestamp");
    }
    private static InputStream seedStream(String path) throws IOException {
        if (path.equals(SEED_PATH)) return new ByteArrayInputStream(SEED_BYTES);
        if (path.equals("Download/dummy")) return new ByteArrayInputStream(new byte[] {0});
        throw new FileNotFoundException(path);
    }
    private static ResourceInstaller seeded(Fixture f, FakeFactory factory) throws IOException {
        return installer(f, factory).setSeedSource(ResourceInstallerTest::seedStream);
    }
    private static Fixture seedFixture() throws Exception {
        LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
        contents.put("assetpacks/pad_ff_01/311010101/assets/test.bin", bytes("first-resource"));
        contents.put(SEED_PATH, SEED_BYTES); contents.put("Download/dummy", new byte[] {0}); return fixture(contents);
    }
    private static Fixture largeFixture() throws Exception {
        byte[] content = new byte[512 * 1024]; new Random(48235).nextBytes(content);
        LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>(); contents.put("Download/content.ucas", content);
        return fixture(contents);
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
        // Existing recovery tests deliberately simulate a process stopping at its first failure.
        return new ResourceInstaller(f.root, f.manifest, BASE, null, factory, dir -> Long.MAX_VALUE, 0, delay -> { });
    }
    private static ResourceInstaller.Manifest manifest(String version, ResourceInstaller.Pack pack) {
        return new ResourceInstaller.Manifest("test.game", 311010101, version, Collections.singletonList(pack));
    }
    private static Fixture fixture() throws Exception {
        LinkedHashMap<String, byte[]> contents = new LinkedHashMap<String, byte[]>();
        contents.put("assetpacks/pad_ff_01/311010101/assets/test.bin", bytes("first-resource"));
        contents.put("Download/test.bin", bytes("second-resource-data"));
        return fixture(contents);
    }
    private static Fixture fixture(LinkedHashMap<String, byte[]> contents) throws Exception {
        File parent = new File("android-build/resource-installer-tests"); parent.mkdirs();
        File root = Files.createTempDirectory(parent.toPath(), "case-").toFile();
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
        IOException headerFailure;
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
        boolean disconnected;
        FakeConnection(URL url, Reply reply) { super(url); this.reply = reply; }
        public void connect() { }
        public void disconnect() { disconnected = true; }
        public boolean usingProxy() { return false; }
        public int getResponseCode() throws IOException { if (reply.headerFailure != null) throw reply.headerFailure; return reply.code; }
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
