package org.efootballrevival.bootstrap;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.util.ArrayList;
import org.json.*;

/** Small update index + hash-verified manifest cache. No game files are written here. */
public final class UpdateChannel {
    public interface Transport { byte[] read(URL address, int limit) throws IOException; }
    public static final class Selection {
        public final ResourceInstaller.Manifest manifest;
        public final String baseUrl;
        public final JSONObject apk;
        public final long revision;
        private final byte[] channelBytes;
        Selection(ResourceInstaller.Manifest manifest, String baseUrl, JSONObject apk, long revision) {
            this(manifest, baseUrl, apk, revision, null);
        }
        Selection(ResourceInstaller.Manifest manifest, String baseUrl, JSONObject apk, long revision, byte[] channelBytes) {
            this.manifest = manifest; this.baseUrl = baseUrl; this.apk = apk; this.revision = revision;
            this.channelBytes = channelBytes == null ? null : channelBytes.clone();
        }
    }
    private final File directory, completedMarker;
    private final String packageName, channelUrl;
    private final long gameVersionCode;
    private final Transport transport;

    public UpdateChannel(File filesDir, String packageName, long gameVersionCode, String channelUrl) throws IOException {
        this(filesDir, packageName, gameVersionCode, channelUrl, new HttpTransport());
    }
    public UpdateChannel(File filesDir, String packageName, long gameVersionCode, String channelUrl,
                         Transport transport) throws IOException {
        this.packageName = packageName; this.gameVersionCode = gameVersionCode;
        this.channelUrl = channelUrl; this.transport = transport;
        repositoryUrl(channelUrl);
        File root = filesDir.getCanonicalFile();
        directory = new File(root, "resource-bootstrap-state/updates");
        completedMarker = new File(root, "resource-bootstrap-state/complete.marker");
        File cursor = directory;
        while (!cursor.equals(root)) {
            if (Files.isSymbolicLink(cursor.toPath())) throw new IOException("Invalid update cache path");
            cursor = cursor.getParentFile();
        }
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory())
            throw new IOException("Unable to create update cache");
    }

    public Selection resolve(ResourceInstaller.Manifest embedded, String embeddedBaseUrl) throws IOException {
        return resolve(embedded, embeddedBaseUrl, true);
    }
    public Selection resolve(ResourceInstaller.Manifest embedded, String embeddedBaseUrl, boolean checkNetwork) throws IOException {
        if (transport instanceof HttpTransport) ((HttpTransport) transport).begin();
        Selection fallback = installedSelection(embedded, embeddedBaseUrl);
        if (!checkNetwork) return fallback;
        try {
            Selection pending = select(json(read(file("pending-channel.json"), 128 * 1024)), false);
            if (pending.revision >= fallback.revision) fallback = pending;
        } catch (Exception ignored) { /* Incomplete metadata never replaces usable installed selection. */ }
        try {
            byte[] raw = transport.read(repositoryUrl(channelUrl), 128 * 1024);
            JSONObject channel = json(raw);
            validateChannel(channel);
            if (channel.getLong("revision") < fallback.revision) return fallback;
            JSONObject resources = channel.getJSONObject("resources");
            if (resources.getLong("gameVersionCode") != gameVersionCode) {
                // A future game engine may need its new APK first; do not apply incompatible resources.
                return new Selection(fallback.manifest, fallback.baseUrl, channel.optJSONObject("apk"), fallback.revision, fallback.channelBytes);
            }
            Selection candidate = select(channel, true);
            atomicWrite(file("pending-channel.json"), raw); // Available for resume, not yet an installed release.
            return candidate;
        } catch (Exception unavailable) {
            // Network failure must not prevent already-installed resources from launching.
            return fallback;
        }
    }

    /** Only completed pending/legacy metadata may override the last installed selection, without network. */
    public Selection installedSelection(ResourceInstaller.Manifest embedded, String embeddedBaseUrl) {
        Selection installed = new Selection(embedded, embeddedBaseUrl, null, 0);
        try { installed = select(json(read(file("installed-channel.json"), 128 * 1024)), false); }
        catch (Exception ignored) { /* New installs use the APK baseline. */ }
        for (String name : new String[] {"pending-channel.json", "channel.json"}) {
            try {
                Selection candidate = select(json(read(file(name), 128 * 1024)), false);
                if (candidate.revision >= installed.revision && !Files.isSymbolicLink(completedMarker.toPath())
                        && new String(read(completedMarker, 256), StandardCharsets.UTF_8)
                            .equals("resource-bootstrap-v1\n" + candidate.manifest.identity + "\n")) installed = candidate;
            } catch (Exception ignored) { /* Downloaded metadata alone never proves installed content exists. */ }
        }
        return installed;
    }

    /** Caller must complete ResourceInstaller.install successfully before advancing this pointer. */
    public void commitInstalled(Selection installed) throws IOException {
        if (installed.channelBytes == null) return;
        try {
            Selection checked = select(json(installed.channelBytes), false);
            if (!checked.manifest.identity.equals(installed.manifest.identity)) throw new IOException("Installed resources do not match the manifest");
            atomicWrite(file("installed-channel.json"), installed.channelBytes);
        } catch (IOException error) { throw error; }
        catch (Exception invalid) { throw new IOException("Unable to save the installed resource version", invalid); }
    }

    private Selection select(JSONObject channel, boolean online) throws Exception {
        validateChannel(channel);
        JSONObject resource = channel.getJSONObject("resources");
        if (resource.getLong("gameVersionCode") != gameVersionCode) throw new IOException("Resources require a different game version");
        String sha = resource.getString("sha256");
        requireHash(sha);
        long bytes = resource.getLong("bytes");
        if (bytes <= 0 || bytes > 2 * 1024 * 1024) throw new IOException("Invalid update manifest size");
        URL address = repositoryUrl(resource.getString("manifestUrl"));
        String base = resource.getString("baseUrl");
        URL baseAddress = repositoryUrl(base);
        if (!baseAddress.getPath().endsWith("/")) throw new IOException("Invalid resource URL");
        File manifestFile = file("manifest-" + sha + ".json");
        byte[] payload = null;
        try {
            payload = read(manifestFile, (int) bytes);
            if (payload.length != bytes || !sha.equals(hash(payload))) payload = null;
        } catch (IOException ignored) { }
        if (payload == null) {
            if (!online) throw new IOException("Cached resource manifest is incomplete");
            payload = transport.read(address, (int) bytes);
            if (payload.length != bytes || !sha.equals(hash(payload))) throw new IOException("Resource manifest verification failed");
        }
        ResourceInstaller.Manifest manifest = parseManifest(payload);
        if (!packageName.equals(manifest.packageName) || manifest.versionCode != gameVersionCode
                || !resource.getString("resourceVersion").equals(manifest.resourceVersion))
            throw new IOException("Update manifest does not match the game");
        if (online) atomicWrite(manifestFile, payload);
        return new Selection(manifest, base, channel.optJSONObject("apk"), channel.getLong("revision"), channel.toString().getBytes(StandardCharsets.UTF_8));
    }

    private void validateChannel(JSONObject channel) throws Exception {
        if (channel.getInt("schemaVersion") != 1 || !packageName.equals(channel.getString("packageName"))
                || channel.getLong("revision") < 1) throw new IOException("Invalid update channel");
        channel.getJSONObject("resources");
    }

    public static ResourceInstaller.Manifest parseManifest(byte[] bytes) throws IOException {
        try {
            JSONObject root = json(bytes);
            if (root.getInt("schemaVersion") != 1) throw new IOException("Unsupported resource manifest version");
            JSONArray files = root.getJSONArray("files");
            if (files.length() < 1 || files.length() > 1000) throw new IOException("Invalid number of resource packages");
            ArrayList<ResourceInstaller.Pack> packs = new ArrayList<ResourceInstaller.Pack>();
            int memberCount = 0;
            for (int i = 0; i < files.length(); i++) {
                JSONObject pack = files.getJSONObject(i);
                JSONArray members = pack.getJSONArray("entries");
                if ((memberCount += members.length()) > 20000) throw new IOException("Too many resource files");
                ArrayList<ResourceInstaller.Entry> entries = new ArrayList<ResourceInstaller.Entry>();
                long expanded = 0;
                for (int j = 0; j < members.length(); j++) {
                    JSONObject member = members.getJSONObject(j);
                    ResourceInstaller.Entry entry = new ResourceInstaller.Entry(member.getString("path"),
                            member.getLong("bytes"), member.getString("sha256"));
                    entries.add(entry); expanded = Math.addExact(expanded, entry.bytes);
                }
                if (pack.getLong("expandedBytes") != expanded) throw new IOException("Resource manifest size mismatch");
                String url = pack.optString("url", "");
                if (!url.isEmpty()) repositoryUrl(url);
                packs.add(new ResourceInstaller.Pack(pack.getString("name"), pack.getLong("bytes"),
                        pack.getString("sha256"), entries, url.isEmpty() ? null : url, pack.optBoolean("allowSubset", false)));
            }
            return new ResourceInstaller.Manifest(root.getString("packageName"), root.getLong("versionCode"),
                    root.getString("resourceVersion"), packs);
        } catch (IOException error) { throw error; }
        catch (Exception malformed) { throw new IOException("Invalid resource manifest", malformed); }
    }

    static URL repositoryUrl(String value) throws IOException {
        URL url = new URL(value);
        ResourceInstaller.validateUrl(url);
        if (!"github.com".equalsIgnoreCase(url.getHost()) || url.getQuery() != null
                || !url.getPath().matches("/maxlzyver/(efootxi-test|efootxi|73efootball)/releases/download/[A-Za-z0-9][A-Za-z0-9._-]*/([A-Za-z0-9][A-Za-z0-9._-]*)?"))
            throw new IOException("Update URL must belong to an approved Fut release repository");
        return url;
    }
    private File file(String name) throws IOException {
        File result = new File(directory, name);
        if (Files.isSymbolicLink(result.toPath()) || !result.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile()))
            throw new IOException("Invalid update cache path");
        return result;
    }
    private static JSONObject json(byte[] bytes) throws JSONException { return new JSONObject(new String(bytes, StandardCharsets.UTF_8)); }
    static String hash(byte[] data) throws IOException {
        try { return ResourceInstaller.hex(MessageDigest.getInstance("SHA-256").digest(data)); }
        catch (Exception impossible) { throw new IOException(impossible); }
    }
    private static void requireHash(String value) throws IOException {
        if (!value.matches("[0-9a-f]{64}")) throw new IOException("Invalid update checksum");
    }
    private static byte[] read(File file, int limit) throws IOException {
        try (InputStream input = new FileInputStream(file)) { return bounded(input, limit); }
    }
    private static byte[] bounded(InputStream input, int limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192]; int count;
        while ((count = input.read(buffer)) != -1) {
            if (count > limit - output.size()) throw new IOException("Update manifest exceeds the size limit");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }
    private static void atomicWrite(File destination, byte[] bytes) throws IOException {
        File temporary = new File(destination.getParentFile(), destination.getName() + ".tmp");
        if (Files.isSymbolicLink(temporary.toPath())) throw new IOException("Invalid update cache path");
        try (FileOutputStream output = new FileOutputStream(temporary)) { output.write(bytes); output.getFD().sync(); }
        Files.move(temporary.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    private static final class HttpTransport implements Transport {
        private long deadline;
        void begin() { deadline = System.nanoTime() + 3_500_000_000L; }
        public byte[] read(URL address, int limit) throws IOException {
            for (int redirect = 0; redirect <= 5; redirect++) {
                ResourceInstaller.validateUrl(address);
                long remaining = (deadline - System.nanoTime()) / 1_000_000L;
                if (remaining <= 0) throw new IOException("Update check timed out");
                HttpURLConnection connection = (HttpURLConnection) address.openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout((int) Math.min(1200, remaining));
                connection.setReadTimeout((int) Math.min(1800, remaining));
                connection.setRequestProperty("Accept-Encoding", "identity");
                connection.setRequestProperty("User-Agent", "efootxi/1.1");
                connection.setRequestProperty("Cache-Control", "no-cache");
                try {
                    int code = connection.getResponseCode();
                    if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                        String location = connection.getHeaderField("Location");
                        if (location == null) throw new IOException("Invalid update redirect");
                        address = new URL(address, location); continue;
                    }
                    if (code != 200) throw new IOException("Update check HTTP " + code);
                    long length = connection.getContentLengthLong();
                    if (length > limit) throw new IOException("Update manifest exceeds the size limit");
                    try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                        byte[] buffer = new byte[8192]; int count;
                        while ((count = input.read(buffer)) != -1) {
                            if (System.nanoTime() >= deadline) throw new IOException("Update check timed out");
                            if (count > limit - output.size()) throw new IOException("Update manifest exceeds the size limit");
                            output.write(buffer, 0, count);
                        }
                        return output.toByteArray();
                    }
                } finally { connection.disconnect(); }
            }
            throw new IOException("Too many update redirects");
        }
    }
}
