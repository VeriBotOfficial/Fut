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
        Selection(ResourceInstaller.Manifest manifest, String baseUrl, JSONObject apk, long revision) {
            this.manifest = manifest; this.baseUrl = baseUrl; this.apk = apk; this.revision = revision;
        }
    }
    private final File directory;
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
        File cursor = directory;
        while (!cursor.equals(root)) {
            if (Files.isSymbolicLink(cursor.toPath())) throw new IOException("更新缓存路径无效");
            cursor = cursor.getParentFile();
        }
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory())
            throw new IOException("无法创建更新缓存");
    }

    public Selection resolve(ResourceInstaller.Manifest embedded, String embeddedBaseUrl) throws IOException {
        return resolve(embedded, embeddedBaseUrl, true);
    }
    public Selection resolve(ResourceInstaller.Manifest embedded, String embeddedBaseUrl, boolean checkNetwork) throws IOException {
        if (transport instanceof HttpTransport) ((HttpTransport) transport).begin();
        Selection fallback = new Selection(embedded, embeddedBaseUrl, null, 0);
        try {
            JSONObject cached = json(read(file("channel.json"), 128 * 1024));
            fallback = select(cached, false);
        } catch (Exception ignored) { /* Initial launch or invalid cache uses the embedded baseline. */ }
        if (!checkNetwork) return fallback;
        try {
            byte[] raw = transport.read(repositoryUrl(channelUrl), 128 * 1024);
            JSONObject channel = json(raw);
            validateChannel(channel);
            if (channel.getLong("revision") < fallback.revision) return fallback;
            JSONObject resources = channel.getJSONObject("resources");
            if (resources.getLong("gameVersionCode") != gameVersionCode) {
                // A future game engine may need its new APK first; do not apply incompatible resources.
                return new Selection(fallback.manifest, fallback.baseUrl, channel.optJSONObject("apk"), fallback.revision);
            }
            Selection candidate = select(channel, true);
            atomicWrite(file("channel.json"), raw); // Manifest is durable before the pointer is committed.
            return candidate;
        } catch (Exception unavailable) {
            // Network failure must not prevent already-installed resources from launching.
            return fallback;
        }
    }

    private Selection select(JSONObject channel, boolean online) throws Exception {
        validateChannel(channel);
        JSONObject resource = channel.getJSONObject("resources");
        if (resource.getLong("gameVersionCode") != gameVersionCode) throw new IOException("资源需要不同游戏版本");
        String sha = resource.getString("sha256");
        requireHash(sha);
        long bytes = resource.getLong("bytes");
        if (bytes <= 0 || bytes > 2 * 1024 * 1024) throw new IOException("更新清单大小无效");
        URL address = repositoryUrl(resource.getString("manifestUrl"));
        String base = resource.getString("baseUrl");
        URL baseAddress = repositoryUrl(base);
        if (!baseAddress.getPath().endsWith("/")) throw new IOException("资源地址无效");
        File manifestFile = file("manifest-" + sha + ".json");
        byte[] payload = null;
        try {
            payload = read(manifestFile, (int) bytes);
            if (payload.length != bytes || !sha.equals(hash(payload))) payload = null;
        } catch (IOException ignored) { }
        if (payload == null) {
            if (!online) throw new IOException("缓存的资源清单不完整");
            payload = transport.read(address, (int) bytes);
            if (payload.length != bytes || !sha.equals(hash(payload))) throw new IOException("资源清单校验失败");
        }
        ResourceInstaller.Manifest manifest = parseManifest(payload);
        if (!packageName.equals(manifest.packageName) || manifest.versionCode != gameVersionCode
                || !resource.getString("resourceVersion").equals(manifest.resourceVersion))
            throw new IOException("更新清单与游戏不匹配");
        if (online) atomicWrite(manifestFile, payload);
        return new Selection(manifest, base, channel.optJSONObject("apk"), channel.getLong("revision"));
    }

    private void validateChannel(JSONObject channel) throws Exception {
        if (channel.getInt("schemaVersion") != 1 || !packageName.equals(channel.getString("packageName"))
                || channel.getLong("revision") < 1) throw new IOException("更新频道无效");
        channel.getJSONObject("resources");
    }

    public static ResourceInstaller.Manifest parseManifest(byte[] bytes) throws IOException {
        try {
            JSONObject root = json(bytes);
            if (root.getInt("schemaVersion") != 1) throw new IOException("不支持的资源清单版本");
            JSONArray files = root.getJSONArray("files");
            if (files.length() < 1 || files.length() > 1000) throw new IOException("资源包数量无效");
            ArrayList<ResourceInstaller.Pack> packs = new ArrayList<ResourceInstaller.Pack>();
            int memberCount = 0;
            for (int i = 0; i < files.length(); i++) {
                JSONObject pack = files.getJSONObject(i);
                JSONArray members = pack.getJSONArray("entries");
                if ((memberCount += members.length()) > 20000) throw new IOException("资源文件数量过多");
                ArrayList<ResourceInstaller.Entry> entries = new ArrayList<ResourceInstaller.Entry>();
                long expanded = 0;
                for (int j = 0; j < members.length(); j++) {
                    JSONObject member = members.getJSONObject(j);
                    ResourceInstaller.Entry entry = new ResourceInstaller.Entry(member.getString("path"),
                            member.getLong("bytes"), member.getString("sha256"));
                    entries.add(entry); expanded = Math.addExact(expanded, entry.bytes);
                }
                if (pack.getLong("expandedBytes") != expanded) throw new IOException("资源清单大小不一致");
                String url = pack.optString("url", "");
                if (!url.isEmpty()) repositoryUrl(url);
                packs.add(new ResourceInstaller.Pack(pack.getString("name"), pack.getLong("bytes"),
                        pack.getString("sha256"), entries, url.isEmpty() ? null : url, pack.optBoolean("allowSubset", false)));
            }
            return new ResourceInstaller.Manifest(root.getString("packageName"), root.getLong("versionCode"),
                    root.getString("resourceVersion"), packs);
        } catch (IOException error) { throw error; }
        catch (Exception malformed) { throw new IOException("资源清单无效", malformed); }
    }

    static URL repositoryUrl(String value) throws IOException {
        URL url = new URL(value);
        ResourceInstaller.validateUrl(url);
        if (!"github.com".equalsIgnoreCase(url.getHost()) || url.getQuery() != null
                || !url.getPath().matches("/maxlzyver/(efootxi|73efootball)/releases/download/[A-Za-z0-9][A-Za-z0-9._-]*/([A-Za-z0-9][A-Za-z0-9._-]*)?"))
            throw new IOException("更新地址必须属于 efootxi 发布仓库");
        return url;
    }
    private File file(String name) throws IOException {
        File result = new File(directory, name);
        if (Files.isSymbolicLink(result.toPath()) || !result.getCanonicalFile().getParentFile().equals(directory.getCanonicalFile()))
            throw new IOException("更新缓存路径无效");
        return result;
    }
    private static JSONObject json(byte[] bytes) throws JSONException { return new JSONObject(new String(bytes, StandardCharsets.UTF_8)); }
    static String hash(byte[] data) throws IOException {
        try { return ResourceInstaller.hex(MessageDigest.getInstance("SHA-256").digest(data)); }
        catch (Exception impossible) { throw new IOException(impossible); }
    }
    private static void requireHash(String value) throws IOException {
        if (!value.matches("[0-9a-f]{64}")) throw new IOException("更新校验值无效");
    }
    private static byte[] read(File file, int limit) throws IOException {
        try (InputStream input = new FileInputStream(file)) { return bounded(input, limit); }
    }
    private static byte[] bounded(InputStream input, int limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[8192]; int count;
        while ((count = input.read(buffer)) != -1) {
            if (count > limit - output.size()) throw new IOException("更新清单超过大小限制");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }
    private static void atomicWrite(File destination, byte[] bytes) throws IOException {
        File temporary = new File(destination.getParentFile(), destination.getName() + ".tmp");
        if (Files.isSymbolicLink(temporary.toPath())) throw new IOException("更新缓存路径无效");
        try (FileOutputStream output = new FileOutputStream(temporary)) { output.write(bytes); output.getFD().sync(); }
        Files.move(temporary.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }

    private static final class HttpTransport implements Transport {
        private long deadline;
        void begin() { deadline = System.nanoTime() + 8_000_000_000L; }
        public byte[] read(URL address, int limit) throws IOException {
            for (int redirect = 0; redirect <= 5; redirect++) {
                ResourceInstaller.validateUrl(address);
                long remaining = (deadline - System.nanoTime()) / 1_000_000L;
                if (remaining <= 0) throw new IOException("更新检查超时");
                HttpURLConnection connection = (HttpURLConnection) address.openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setConnectTimeout((int) Math.min(2000, remaining));
                connection.setReadTimeout((int) Math.min(2500, remaining));
                connection.setRequestProperty("Accept-Encoding", "identity");
                connection.setRequestProperty("User-Agent", "efootxi/1.1");
                connection.setRequestProperty("Cache-Control", "no-cache");
                try {
                    int code = connection.getResponseCode();
                    if (code == 301 || code == 302 || code == 303 || code == 307 || code == 308) {
                        String location = connection.getHeaderField("Location");
                        if (location == null) throw new IOException("更新重定向无效");
                        address = new URL(address, location); continue;
                    }
                    if (code != 200) throw new IOException("更新检查 HTTP " + code);
                    long length = connection.getContentLengthLong();
                    if (length > limit) throw new IOException("更新清单超过大小限制");
                    try (InputStream input = connection.getInputStream(); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                        byte[] buffer = new byte[8192]; int count;
                        while ((count = input.read(buffer)) != -1) {
                            if (System.nanoTime() >= deadline) throw new IOException("更新检查超时");
                            if (count > limit - output.size()) throw new IOException("更新清单超过大小限制");
                            output.write(buffer, 0, count);
                        }
                        return output.toByteArray();
                    }
                } finally { connection.disconnect(); }
            }
            throw new IOException("更新重定向次数过多");
        }
    }
}
