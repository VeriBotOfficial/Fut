package org.efootballrevival.bootstrap;

import java.io.*;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.*;
import org.json.*;

/** Host-side update protocol tests. Run with a real org.json JAR, never Android stub classes. */
public final class UpdateChannelTest {
    private static final String PACKAGE = "test.game";
    private static final long GAME = 311010101;
    private static final String BASE = "https://github.com/maxlzyver/efootxi-test/releases/download/resources-v2/";
    private static final String CHANNEL = "https://github.com/maxlzyver/efootxi-test/releases/download/updates/channel.json";
    private static int tests, assertions;

    public static void main(String[] args) throws Exception {
        freshRemoteSelectionIsDurableBeforeInstallation();
        offlineRestartRetainsDownloadedManifest();
        explicitOfflineModeUsesValidCacheWithoutTransport();
        explicitOfflineModeUsesEmbeddedWithoutTransport();
        pendingUpdateDoesNotReplaceInstalledOffline();
        completedPendingUpdateRecoversOfflineBeforeChannelCommit();
        legacyCacheRemainsReadableForMigration();
        explicitOfflineModeRejectsCorruptCacheWithoutTransport();
        corruptedCacheFallsBackAndCanRecover();
        lowerRevisionCannotRollBackSelection();
        futureGameResourcesKeepCurrentButOfferApk();
        manifestHashAndLengthAreMandatory();
        packageAndResourceIdentityMustMatch();
        manifestsRespectPathAndCountBounds();
        repositoryScopeAndSchemesAreBounded();
        explicitPackUrlsAndSubsetsSurviveParsing();
        System.out.println("PASS " + tests + " update-channel test groups; " + assertions + " assertions");
    }

    private static void freshRemoteSelectionIsDurableBeforeInstallation() throws Exception {
        Fixture f = fixture(); Transport transport = remote(f.channel, f.remoteBytes);
        UpdateChannel.Selection selected = client(f, transport).resolve(f.embedded, BASE);
        check(selected.revision == 2 && selected.manifest.resourceVersion.equals("v2"), "fresh manifest selected");
        check(selected.baseUrl.equals(BASE), "selected resource base is from verified channel");
        check(transport.urls.equals(Arrays.asList(CHANNEL, BASE + "manifest.json")), "only index and manifest fetched");
        check(transport.limits.get(0) == 128 * 1024 && transport.limits.get(1) == f.remoteBytes.length, "network reads are bounded by declared metadata sizes");
        check(cache(f, "pending-channel.json").isFile(), "channel pointer persisted before game installation starts");
        check(Arrays.equals(Files.readAllBytes(cache(f, "manifest-" + UpdateChannel.hash(f.remoteBytes) + ".json").toPath()), f.remoteBytes), "manifest bytes durable before pointer use");
        check(!new File(f.root, "Download/content.bin").exists(), "selection never modifies game content");
        check(!new File(f.root, "resource-bootstrap-state/complete.marker").exists(), "selection does not falsely mark resource installation complete"); tests++;
    }

    private static void offlineRestartRetainsDownloadedManifest() throws Exception {
        Fixture f = fixture(); client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        Transport offline = new Transport();
        UpdateChannel.Selection selected = client(f, offline).resolve(f.embedded, BASE);
        check(selected.revision == 2 && selected.manifest.resourceVersion.equals("v2"), "process restart uses cached update even before installation completed");
        check(offline.urls.equals(Collections.singletonList(CHANNEL)), "cached manifest is not re-fetched while offline");
        check(selected.manifest.identity.equals(UpdateChannel.parseManifest(f.remoteBytes).identity), "cached update retains exact resource identity"); tests++;
    }

    private static void explicitOfflineModeUsesValidCacheWithoutTransport() throws Exception {
        Fixture f = fixture(); UpdateChannel first = client(f, remote(f.channel, f.remoteBytes));
        first.commitInstalled(first.resolve(f.embedded, BASE));
        JSONObject newer = copy(f.channel).put("revision", 99);
        Transport network = remote(newer, f.remoteBytes);
        UpdateChannel.Selection selected = client(f, network).resolve(f.embedded, BASE, false);
        check(network.urls.isEmpty() && network.limits.isEmpty(), "explicit offline mode makes zero transport calls with a valid cache");
        check(selected.revision == 2 && selected.manifest.resourceVersion.equals("v2"), "offline mode retains verified cached selection without checking newer channel");
        check(selected.manifest.identity.equals(UpdateChannel.parseManifest(f.remoteBytes).identity), "offline cached manifest retains complete integrity-checked identity"); tests++;
    }

    private static void explicitOfflineModeUsesEmbeddedWithoutTransport() throws Exception {
        Fixture f = fixture(); Transport network = remote(f.channel, f.remoteBytes);
        UpdateChannel.Selection selected = client(f, network).resolve(f.embedded, BASE, false);
        check(network.urls.isEmpty() && network.limits.isEmpty(), "explicit offline mode makes zero transport calls when no cache exists");
        check(selected.manifest == f.embedded && selected.revision == 0 && selected.baseUrl.equals(BASE), "offline mode selects embedded baseline without a cache");
        check(!cache(f, "pending-channel.json").exists(), "offline baseline selection does not create an unverified channel pointer"); tests++;
    }

    private static void explicitOfflineModeRejectsCorruptCacheWithoutTransport() throws Exception {
        Fixture f = fixture(); UpdateChannel first = client(f, remote(f.channel, f.remoteBytes));
        first.commitInstalled(first.resolve(f.embedded, BASE));
        Files.write(cache(f, "manifest-" + UpdateChannel.hash(f.remoteBytes) + ".json").toPath(), bytes("corrupt cache"));
        Transport network = remote(f.channel, f.remoteBytes);
        UpdateChannel.Selection selected = client(f, network).resolve(f.embedded, BASE, false);
        check(network.urls.isEmpty() && network.limits.isEmpty(), "corrupt cache does not trigger a network repair in explicit offline mode");
        check(selected.manifest == f.embedded && selected.revision == 0, "offline mode rejects corrupt cached content and uses embedded baseline"); tests++;
    }

    private static void pendingUpdateDoesNotReplaceInstalledOffline() throws Exception {
        Fixture f = fixture(); UpdateChannel first = client(f, remote(f.channel, f.remoteBytes));
        UpdateChannel.Selection pending = first.resolve(f.embedded, BASE);
        check(first.resolve(f.embedded, BASE, false).manifest == f.embedded, "uninstalled remote metadata cannot select pending resources offline");
        first.commitInstalled(pending);
        check(first.resolve(f.embedded, BASE, false).revision == 2, "successful install advances offline selection");
        byte[] nextRaw = bytes(manifest("v3", GAME).toString());
        JSONObject next = copy(f.channel).put("revision", 3).put("resources", resource(nextRaw, "v3", GAME));
        UpdateChannel newer = client(f, remote(next, nextRaw));
        UpdateChannel.Selection selected = newer.resolve(f.embedded, BASE);
        check(selected.revision == 3, "new update selected for preparation");
        Transport offline = new Transport();
        check(client(f, offline).resolve(f.embedded, BASE, false).revision == 2 && offline.urls.isEmpty(), "unfinished update preserves installed selection and never networks offline");
        newer.commitInstalled(selected);
        check(newer.resolve(f.embedded, BASE, false).revision == 3, "only successful newer installation commits new version"); tests++;
    }

    private static void legacyCacheRemainsReadableForMigration() throws Exception {
        Fixture f = fixture(); client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        Files.move(cache(f, "pending-channel.json").toPath(), cache(f, "channel.json").toPath());
        Transport offline = new Transport(); UpdateChannel old = client(f, offline);
        check(old.resolve(f.embedded, BASE, false).manifest == f.embedded, "uncompleted legacy pointer cannot displace embedded installed content offline");
        Files.write(new File(f.root, "resource-bootstrap-state/complete.marker").toPath(),
                bytes("resource-bootstrap-v1\n" + UpdateChannel.parseManifest(f.remoteBytes).identity + "\n"));
        UpdateChannel.Selection legacy = old.resolve(f.embedded, BASE, false);
        check(legacy.revision == 2 && offline.urls.isEmpty(), "v1.1 cached manifest remains readable without network");
        old.commitInstalled(legacy);
        check(cache(f, "installed-channel.json").isFile(), "validated legacy install can migrate its pointer"); tests++;
    }

    private static void completedPendingUpdateRecoversOfflineBeforeChannelCommit() throws Exception {
        for (boolean hadCommittedRelease : new boolean[] {false, true}) {
            Fixture f = fixture();
            File content = new File(f.root, "Download/content.bin"); content.getParentFile().mkdirs();
            Files.write(content.toPath(), bytes("content"));
            ResourceInstaller.ConnectionFactory noNetwork = address -> {
                throw new AssertionError("completed resource recovery must not open a connection");
            };
            if (hadCommittedRelease) {
                byte[] oldRaw = bytes(manifest("v1", GAME).toString());
                JSONObject oldChannel = copy(f.channel).put("revision", 1)
                        .put("resources", resource(oldRaw, "v1", GAME));
                UpdateChannel old = client(f, remote(oldChannel, oldRaw));
                UpdateChannel.Selection installed = old.resolve(f.embedded, BASE);
                new ResourceInstaller(f.root, installed.manifest, installed.baseUrl, null, noNetwork,
                        directory -> Long.MAX_VALUE).setNetworkAllowed(false).install();
                old.commitInstalled(installed);
            }
            UpdateChannel beforeCrash = client(f, remote(f.channel, f.remoteBytes));
            UpdateChannel.Selection pending = beforeCrash.resolve(f.embedded, BASE);
            File committedPointer = cache(f, "installed-channel.json");
            byte[] previousPointer = committedPointer.isFile() ? Files.readAllBytes(committedPointer.toPath()) : null;
            check(cache(f, "pending-channel.json").isFile(), "pending channel is durable before resource completion");
            ResourceInstaller completed = new ResourceInstaller(f.root, pending.manifest, pending.baseUrl, null,
                    noNetwork, directory -> Long.MAX_VALUE).setNetworkAllowed(false);
            completed.install();
            check(completed.isReady(), "real installer completed pending content before simulated process death");
            check(new String(Files.readAllBytes(new File(f.root, "resource-bootstrap-state/complete.marker").toPath()),
                    StandardCharsets.UTF_8).equals("resource-bootstrap-v1\n" + pending.manifest.identity + "\n"),
                    "completion marker binds the pending manifest even before channel commit");
            // Simulate process death here: do not call beforeCrash.commitInstalled(pending).
            Transport offline = new Transport(); UpdateChannel restarted = client(f, offline);
            UpdateChannel.Selection recovered = restarted.resolve(f.embedded, BASE, false);
            check(recovered.revision == 2 && recovered.manifest.identity.equals(pending.manifest.identity),
                    "offline restart recovers completed pending selection over absent or older committed pointer");
            check(offline.urls.isEmpty() && offline.limits.isEmpty(), "crash recovery makes zero metadata transport calls");
            check(new ResourceInstaller(f.root, recovered.manifest, recovered.baseUrl, null, noNetwork,
                    directory -> Long.MAX_VALUE).setNetworkAllowed(false).isReady(),
                    "recovered selection still passes independent resource readiness checks");
            check(previousPointer == null ? !committedPointer.exists()
                    : Arrays.equals(previousPointer, Files.readAllBytes(committedPointer.toPath())),
                    "selecting completed pending metadata does not prematurely mutate the committed pointer");
            restarted.commitInstalled(recovered);
            Files.delete(cache(f, "pending-channel.json").toPath());
            check(client(f, offline).resolve(f.embedded, BASE, false).manifest.identity.equals(pending.manifest.identity),
                    "normal commit after crash recovery remains durable without pending metadata");
            check(offline.urls.isEmpty(), "commit and subsequent offline restart make zero metadata transport calls");
        }
        tests++;
    }

    private static void corruptedCacheFallsBackAndCanRecover() throws Exception {
        Fixture f = fixture(); client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        File manifest = cache(f, "manifest-" + UpdateChannel.hash(f.remoteBytes) + ".json");
        Files.write(manifest.toPath(), bytes("corrupt cache"));
        check(client(f, new Transport()).resolve(f.embedded, BASE).manifest == f.embedded, "corrupt manifest cache falls back to embedded baseline");
        UpdateChannel.Selection repaired = client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        check(repaired.revision == 2, "valid online manifest repairs corrupt cache");
        Files.write(cache(f, "pending-channel.json").toPath(), bytes("not JSON"));
        check(client(f, new Transport()).resolve(f.embedded, BASE).manifest == f.embedded, "corrupt pointer cannot select an unverified manifest"); tests++;
    }

    private static void lowerRevisionCannotRollBackSelection() throws Exception {
        Fixture f = fixture(); client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        JSONObject stale = copy(f.channel).put("revision", 1);
        byte[] staleManifest = bytes(manifest("older", GAME).toString());
        stale.put("resources", resource(staleManifest, "older", GAME));
        Transport transport = remote(stale, staleManifest);
        UpdateChannel.Selection selected = client(f, transport).resolve(f.embedded, BASE);
        check(selected.revision == 2 && selected.manifest.resourceVersion.equals("v2"), "older channel revision cannot roll resources back");
        check(transport.urls.size() == 1, "older manifest is not downloaded");
        check(new JSONObject(new String(Files.readAllBytes(cache(f, "pending-channel.json").toPath()), StandardCharsets.UTF_8)).getLong("revision") == 2, "rollback does not replace durable channel pointer"); tests++;
    }

    private static void futureGameResourcesKeepCurrentButOfferApk() throws Exception {
        Fixture f = fixture(); client(f, remote(f.channel, f.remoteBytes)).resolve(f.embedded, BASE);
        JSONObject next = copy(f.channel).put("revision", 3);
        next.getJSONObject("resources").put("gameVersionCode", GAME + 100);
        JSONObject apk = new JSONObject().put("buildRevision", 9).put("name", "efootxi.apk"); next.put("apk", apk);
        Transport transport = remote(next, new byte[0]);
        UpdateChannel.Selection selected = client(f, transport).resolve(f.embedded, BASE);
        check(selected.manifest.resourceVersion.equals("v2") && selected.revision == 2, "incompatible game data does not replace usable resource selection");
        check(selected.apk != null && selected.apk.getLong("buildRevision") == 9, "future game version still offers its APK metadata");
        check(transport.urls.size() == 1, "incompatible resource manifest is not fetched");
        check(client(f, new Transport()).resolve(f.embedded, BASE).manifest.resourceVersion.equals("v2"), "offline restart after incompatible offer retains compatible resources"); tests++;
    }

    private static void manifestHashAndLengthAreMandatory() throws Exception {
        Fixture f = fixture(); byte[] tampered = f.remoteBytes.clone(); tampered[tampered.length - 3] ^= 1;
        check(client(f, remote(f.channel, tampered)).resolve(f.embedded, BASE).manifest == f.embedded, "manifest SHA mismatch rejected");
        check(!cache(f, "pending-channel.json").exists(), "failed SHA cannot commit pointer");
        check(client(f, remote(f.channel, Arrays.copyOf(f.remoteBytes, f.remoteBytes.length - 1))).resolve(f.embedded, BASE).manifest == f.embedded, "truncated manifest rejected");
        JSONObject excessive = copy(f.channel); excessive.getJSONObject("resources").put("bytes", 2 * 1024 * 1024 + 1);
        Transport transport = remote(excessive, f.remoteBytes);
        check(client(f, transport).resolve(f.embedded, BASE).manifest == f.embedded && transport.urls.size() == 1, "oversized manifest declaration rejected before download");
        JSONObject invalidHash = copy(f.channel); invalidHash.getJSONObject("resources").put("sha256", "../escape");
        check(client(f, remote(invalidHash, f.remoteBytes)).resolve(f.embedded, BASE).manifest == f.embedded, "invalid hash cannot become cache filename"); tests++;
    }

    private static void packageAndResourceIdentityMustMatch() throws Exception {
        Fixture f = fixture(); JSONObject wrongChannel = copy(f.channel).put("packageName", "other.game");
        Transport rejected = remote(wrongChannel, f.remoteBytes);
        check(client(f, rejected).resolve(f.embedded, BASE).manifest == f.embedded && rejected.urls.size() == 1, "other package channel rejected before fetching manifest");
        JSONObject wrongManifest = manifest("v2", GAME).put("packageName", "other.game"); byte[] raw = bytes(wrongManifest.toString());
        JSONObject matchingHash = copy(f.channel).put("resources", resource(raw, "v2", GAME));
        check(client(f, remote(matchingHash, raw)).resolve(f.embedded, BASE).manifest == f.embedded, "valid hash cannot authorize another package resources");
        JSONObject wrongVersion = manifest("v2", GAME + 1); raw = bytes(wrongVersion.toString());
        matchingHash = copy(f.channel).put("resources", resource(raw, "v2", GAME));
        check(client(f, remote(matchingHash, raw)).resolve(f.embedded, BASE).manifest == f.embedded, "manifest engine version must match channel and installed game");
        JSONObject differentVersion = copy(f.channel); differentVersion.getJSONObject("resources").put("resourceVersion", "other-version");
        check(client(f, remote(differentVersion, f.remoteBytes)).resolve(f.embedded, BASE).manifest == f.embedded, "resource version must match verified manifest"); tests++;
    }

    private static void manifestsRespectPathAndCountBounds() throws Exception {
        for (String path : new String[] {"Download/../../save", "assetpacks/../save", "SaveData/user.sav", "Download\\escape"}) {
            JSONObject value = manifest("v2", GAME);
            value.getJSONArray("files").getJSONObject(0).getJSONArray("entries").getJSONObject(0).put("path", path);
            expectIo(() -> UpdateChannel.parseManifest(bytes(value.toString())), "unsafe or out-of-scope content path rejected");
        }
        JSONObject tooMany = manifest("v2", GAME); JSONObject pack = tooMany.getJSONArray("files").getJSONObject(0);
        JSONArray packs = new JSONArray(); for (int n = 0; n < 1001; n++) packs.put(pack); tooMany.put("files", packs);
        expectIo(() -> UpdateChannel.parseManifest(bytes(tooMany.toString())), "pack count is bounded");
        JSONObject badExpanded = manifest("v2", GAME); badExpanded.getJSONArray("files").getJSONObject(0).put("expandedBytes", 99);
        expectIo(() -> UpdateChannel.parseManifest(bytes(badExpanded.toString())), "expanded-size mismatch rejected");
        JSONObject duplicates = manifest("v2", GAME); JSONObject duplicatePack = copy(duplicates.getJSONArray("files").getJSONObject(0)).put("name", "another.zip");
        duplicates.getJSONArray("files").put(duplicatePack);
        expectIo(() -> UpdateChannel.parseManifest(bytes(duplicates.toString())), "same path cannot be owned by two update archives"); tests++;
    }

    private static void repositoryScopeAndSchemesAreBounded() throws Exception {
        for (String address : new String[] {
                "http://github.com/maxlzyver/efootxi/releases/download/v1/file.json",
                "https://github.com/other/efootxi/releases/download/v1/file.json",
                "https://github.com/maxlzyver/other/releases/download/v1/file.json",
                "https://github.com/maxlzyver/efootxi/releases/download/v1/file.json?redirect=other",
                "https://github.com/maxlzyver/efootxi/releases/download/v1/../../../../other/file.json",
                "https://github.com/maxlzyver/efootxi/releases/download/v1/%2e%2e/%2e%2e/file.json",
                "https://github.com/maxlzyver/efootxi/releases/download/v1/nested%2ffile.json"
        }) expectIo(() -> UpdateChannel.repositoryUrl(address), "update address cannot escape repository release assets");
        check(UpdateChannel.repositoryUrl(BASE).toString().equals(BASE), "resource base directory accepted");
        check(UpdateChannel.repositoryUrl("https://github.com/maxlzyver/efootxi/releases/download/resources-311010101-v1/resources-01.zip") != null, "stable repository immutable assets remain readable");
        check(UpdateChannel.repositoryUrl("https://github.com/maxlzyver/73efootball/releases/download/resources-311010101-v1/resources-01.zip") != null, "original repository asset links retained for migration"); tests++;
    }

    private static void explicitPackUrlsAndSubsetsSurviveParsing() throws Exception {
        JSONObject value = manifest("v2", GAME); String oldUrl = "https://github.com/maxlzyver/73efootball/releases/download/old/old-pack.zip";
        value.getJSONArray("files").getJSONObject(0).put("url", oldUrl).put("allowSubset", true);
        ResourceInstaller.Manifest parsed = UpdateChannel.parseManifest(bytes(value.toString()));
        check(parsed.packs.get(0).allowSubset && parsed.packs.get(0).url.equals(oldUrl), "incremental manifest preserves previous immutable archive URL and subset mode");
        JSONObject invalid = copy(value); invalid.getJSONArray("files").getJSONObject(0).put("url", "https://github.com/other/efootxi/releases/download/tag/archive.zip");
        expectIo(() -> UpdateChannel.parseManifest(bytes(invalid.toString())), "per-pack URL is repository-scoped too"); tests++;
    }

    private interface Action { void run() throws Exception; }
    private static void expectIo(Action action, String message) throws Exception {
        try { action.run(); throw new AssertionError(message); }
        catch (IOException expected) { assertions++; }
    }
    private static void check(boolean condition, String message) { assertions++; if (!condition) throw new AssertionError(message); }
    private static byte[] bytes(String value) { return value.getBytes(StandardCharsets.UTF_8); }
    private static JSONObject copy(JSONObject value) { return new JSONObject(value.toString()); }
    private static File cache(Fixture f, String name) { return new File(f.root, "resource-bootstrap-state/updates/" + name); }
    private static UpdateChannel client(Fixture f, Transport transport) throws IOException { return new UpdateChannel(f.root, PACKAGE, GAME, CHANNEL, transport); }
    private static Transport remote(JSONObject channel, byte[] manifest) { return new Transport().with(CHANNEL, bytes(channel.toString())).with(BASE + "manifest.json", manifest); }
    private static JSONObject manifest(String version, long gameVersion) throws IOException {
        String contentHash = UpdateChannel.hash(bytes("content"));
        JSONObject entry = new JSONObject().put("path", "Download/content.bin").put("bytes", 7).put("sha256", contentHash);
        JSONObject pack = new JSONObject().put("name", "content.zip").put("bytes", 100).put("sha256", contentHash).put("expandedBytes", 7).put("entries", new JSONArray().put(entry));
        return new JSONObject().put("schemaVersion", 1).put("packageName", PACKAGE).put("versionCode", gameVersion).put("resourceVersion", version).put("files", new JSONArray().put(pack));
    }
    private static JSONObject resource(byte[] raw, String version, long gameVersion) throws IOException {
        return new JSONObject().put("gameVersionCode", gameVersion).put("resourceVersion", version).put("manifestUrl", BASE + "manifest.json").put("baseUrl", BASE).put("bytes", raw.length).put("sha256", UpdateChannel.hash(raw));
    }
    private static Fixture fixture() throws Exception {
        File parent = new File("android-build/update-channel-tests"); parent.mkdirs();
        File root = Files.createTempDirectory(parent.toPath(), "case-").toFile();
        ResourceInstaller.Manifest embedded = UpdateChannel.parseManifest(bytes(manifest("v1", GAME).toString()));
        byte[] raw = bytes(manifest("v2", GAME).toString());
        JSONObject channel = new JSONObject().put("schemaVersion", 1).put("packageName", PACKAGE).put("revision", 2).put("resources", resource(raw, "v2", GAME));
        return new Fixture(root, embedded, raw, channel);
    }
    private static final class Fixture {
        final File root; final ResourceInstaller.Manifest embedded; final byte[] remoteBytes; final JSONObject channel;
        Fixture(File root, ResourceInstaller.Manifest embedded, byte[] remoteBytes, JSONObject channel) {
            this.root = root; this.embedded = embedded; this.remoteBytes = remoteBytes; this.channel = channel;
        }
    }
    private static final class Transport implements UpdateChannel.Transport {
        final Map<String, byte[]> responses = new HashMap<String, byte[]>();
        final List<String> urls = new ArrayList<String>(); final List<Integer> limits = new ArrayList<Integer>();
        Transport with(String address, byte[] value) { responses.put(address, value); return this; }
        public byte[] read(URL url, int limit) throws IOException {
            urls.add(url.toString()); limits.add(limit); byte[] response = responses.get(url.toString());
            if (response == null) throw new IOException("simulated offline response");
            if (response.length > limit) throw new IOException("simulated bounded transport rejects oversized body");
            return response;
        }
    }
}
