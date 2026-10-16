package org.efootballrevival.bootstrap;

import java.io.*;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/** Android-independent resource installer. The selected manifest is the complete write allowlist. */
public final class ResourceInstaller {
    public interface Listener { void progress(String stage, String detail, long done, long total); }
    public interface ConnectionFactory { HttpURLConnection open(URL url) throws IOException; }
    public interface SpaceChecker { long available(File directory); }

    public static final class Entry {
        public final String path, sha256;
        public final long bytes;
        public Entry(String path, long bytes, String sha256) {
            validatePath(path);
            validateHash(sha256);
            if (bytes < 0) throw new IllegalArgumentException("资源大小无效");
            this.path = path; this.bytes = bytes; this.sha256 = sha256;
        }
    }

    public static final class Pack {
        public final String name, sha256, url;
        public final boolean allowSubset;
        public final long bytes;
        public final List<Entry> entries;
        public Pack(String name, long bytes, String sha256, List<Entry> entries) {
            this(name, bytes, sha256, entries, null, false);
        }
        public Pack(String name, long bytes, String sha256, List<Entry> entries, String url, boolean allowSubset) {
            if (name == null || !name.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,119}\\.zip"))
                throw new IllegalArgumentException("资源包名称无效");
            validateHash(sha256);
            if (bytes <= 0 || entries == null || entries.isEmpty())
                throw new IllegalArgumentException("资源包清单为空或大小无效");
            this.name = name; this.bytes = bytes; this.sha256 = sha256;
            this.url = url; this.allowSubset = allowSubset;
            this.entries = Collections.unmodifiableList(new ArrayList<Entry>(entries));
        }
    }

    public static final class Manifest {
        public final String packageName, resourceVersion, identity;
        public final long versionCode, expandedBytes, downloadBytes;
        public final List<Pack> packs;
        public final List<Entry> entries;
        public Manifest(String packageName, long versionCode, String resourceVersion, List<Pack> packs) {
            if (packageName == null || resourceVersion == null || resourceVersion.isEmpty()
                    || packs == null || packs.isEmpty()) throw new IllegalArgumentException("资源清单无效");
            this.packageName = packageName; this.versionCode = versionCode;
            this.resourceVersion = resourceVersion;
            this.packs = Collections.unmodifiableList(new ArrayList<Pack>(packs));
            ArrayList<Entry> all = new ArrayList<Entry>();
            HashSet<String> names = new HashSet<String>(), paths = new HashSet<String>();
            MessageDigest digest = digest();
            update(digest, packageName, Long.toString(versionCode), resourceVersion);
            long expanded = 0, downloaded = 0;
            for (Pack pack : packs) {
                if (!names.add(pack.name)) throw new IllegalArgumentException("重复资源包");
                downloaded = Math.addExact(downloaded, pack.bytes);
                update(digest, pack.name, Long.toString(pack.bytes), pack.sha256);
                for (Entry entry : pack.entries) {
                    if (!paths.add(entry.path)) throw new IllegalArgumentException("重复资源路径");
                    expanded = Math.addExact(expanded, entry.bytes);
                    update(digest, entry.path, Long.toString(entry.bytes), entry.sha256);
                    all.add(entry);
                }
            }
            entries = Collections.unmodifiableList(all);
            expandedBytes = expanded; downloadBytes = downloaded; identity = hex(digest.digest());
        }
    }

    private static final int BUFFER = 128 * 1024;
    private static final long RESERVE = 64L * 1024 * 1024;
    private static final Pattern RANGE = Pattern.compile("bytes (\\d+)-(\\d+)/(\\d+)");
    private static final Set<String> HOSTS = new HashSet<String>(Arrays.asList(
            "github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com",
            "github-releases.githubusercontent.com"));
    private final File root, state, marker;
    private final Manifest manifest;
    private final URL baseUrl;
    private final ConnectionFactory connections;
    private final SpaceChecker space;
    private final Listener listener;

    public ResourceInstaller(File filesDir, Manifest manifest, String baseUrl, Listener listener)
            throws IOException {
        this(filesDir, manifest, baseUrl, listener, new ConnectionFactory() {
            public HttpURLConnection open(URL url) throws IOException {
                return (HttpURLConnection) url.openConnection();
            }
        }, new SpaceChecker() {
            public long available(File directory) { return directory.getUsableSpace(); }
        });
    }

    /** Connection/space injection changes transport only; production URL validation remains enforced. */
    public ResourceInstaller(File filesDir, Manifest manifest, String baseUrl, Listener listener,
            ConnectionFactory connections, SpaceChecker space) throws IOException {
        this.root = filesDir.getCanonicalFile();
        this.manifest = manifest; this.listener = listener;
        this.connections = connections; this.space = space;
        this.baseUrl = new URL(baseUrl);
        validateUrl(this.baseUrl);
        if (!this.baseUrl.getHost().equalsIgnoreCase("github.com")
                || !this.baseUrl.getPath().matches("/[^/]+/[^/]+/releases/download/[^/]+/"))
            throw new IOException("资源地址必须是固定版本的 GitHub Release");
        this.state = new File(root, "resource-bootstrap-state");
        ensureDirectory(state);
        this.marker = stateFile("complete.marker");
    }

    /** Only size/existence checks after a matching completion marker; never reads gigabytes on launch. */
    public boolean isReady() throws IOException {
        if (!marker.isFile() || marker.length() > 256) return false;
        if (!new String(readSmall(marker, 256), StandardCharsets.UTF_8).equals(markerText())) return false;
        for (Entry entry : manifest.entries) {
            File target = target(entry.path);
            if (!target.isFile() || target.length() != entry.bytes) return false;
        }
        return true;
    }

    public void install() throws IOException {
        install(false);
    }

    /** Full content verification is used for a resource update, never on every ordinary launch. */
    public void install(boolean forceVerify) throws IOException {
        report("检查", "正在检查已安装资源", 0, manifest.entries.size());
        if (!forceVerify && isReady()) { report("完成", "资源已就绪", 1, 1); return; }
        delete(marker);
        // A process kill bypasses extraction's finally block. Only reclaim staging files derived
        // from this embedded manifest; never enumerate or remove game data or saves.
        for (Entry entry : manifest.entries) delete(stateFile("stage-" + entry.sha256 + ".tmp"));
        Set<String> verified = new HashSet<String>();
        long checkedBytes = 0;
        for (Entry entry : manifest.entries) {
            File file = target(entry.path);
            final long checkedBefore = checkedBytes;
            if (file.isFile() && file.length() == entry.bytes
                    && entry.sha256.equals(hash(file, "校验", "核实已有资源", checkedBefore,
                            manifest.expandedBytes))) verified.add(entry.path);
            checkedBytes += entry.bytes;
            report("校验", "核实已有资源", checkedBytes, manifest.expandedBytes);
        }
        // Model the sequential download -> extraction -> archive deletion peak, relative to space
        // already occupied by resumable ZIPs. Counting a cached ZIP twice would block valid resumes.
        long allocated = 0, peak = 0;
        boolean needsDownload = false;
        for (Pack pack : manifest.packs) {
            if (allVerified(pack, verified)) { cleanupPack(pack); continue; }
            needsDownload = true;
            long missing = 0;
            for (Entry entry : pack.entries) if (!verified.contains(entry.path)) missing = Math.addExact(missing, entry.bytes);
            long cached = Math.min(pack.bytes, Math.max(stateFile(pack.name).length(), stateFile(pack.name + ".part").length()));
            peak = Math.max(peak, Math.addExact(Math.addExact(allocated, pack.bytes - cached), missing));
            allocated = Math.addExact(allocated, missing - cached);
        }
        long required = needsDownload ? Math.addExact(peak, RESERVE) : 0;
        if (needsDownload && space.available(root) < required)
            throw new IOException(String.format(Locale.ROOT, "空间不足：还需至少 %.2f GB 可用空间", required / 1e9));
        int index = 0;
        for (Pack pack : manifest.packs) {
            index++;
            if (allVerified(pack, verified)) { cleanupPack(pack); continue; }
            File archive = download(pack, index);
            extract(pack, archive, verified, index);
            cleanupPack(pack);
        }
        for (Entry entry : manifest.entries) {
            File file = target(entry.path);
            if (!verified.contains(entry.path) || !file.isFile() || file.length() != entry.bytes)
                throw new IOException("资源部署不完整：" + entry.path);
        }
        atomicWrite(marker, markerText().getBytes(StandardCharsets.UTF_8));
        report("完成", "资源已就绪", 1, 1);
    }

    private boolean allVerified(Pack pack, Set<String> verified) {
        for (Entry entry : pack.entries) if (!verified.contains(entry.path)) return false;
        return true;
    }

    File download(Pack pack, int index) throws IOException {
        URL address = pack.url == null ? new URL(baseUrl, pack.name) : new URL(pack.url);
        return downloadContent(pack.name, pack.bytes, pack.sha256, address,
                index + "/" + manifest.packs.size() + " " + pack.name);
    }

    /** Downloads an APK with the same resumable transport and mandatory complete SHA-256 check. */
    public File downloadArtifact(String name, long bytes, String sha256, String url) throws IOException {
        if (name == null || !name.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,119}\\.apk") || bytes <= 0)
            throw new IOException("安装包名称或大小无效");
        try { validateHash(sha256); }
        catch (IllegalArgumentException invalid) { throw new IOException(invalid.getMessage(), invalid); }
        long cached = Math.min(bytes, Math.max(stateFile(name).length(), stateFile(name + ".part").length()));
        if (space.available(root) < bytes - cached + RESERVE) throw new IOException("下载更新所需空间不足");
        return downloadContent(name, bytes, sha256, new URL(url), "应用更新");
    }

    private File downloadContent(String name, long bytes, String sha256, URL address, String detail) throws IOException {
        validateUrl(address);
        File archive = stateFile(name), part = stateFile(name + ".part");
        File resume = stateFile(name + ".resume");
        if (archive.isFile()) {
            if (archive.length() == bytes && sha256.equals(hash(archive, "校验", detail, 0, bytes)))
                return archive;
            delete(archive);
        }
        String expected = "resource-content-v2:" + sha256 + ":" + bytes;
        Properties saved = readProperties(resume);
        String etag = saved.getProperty("etag", "");
        String previous = saved.getProperty("identity", "");
        // Old clients bound a partial to the whole manifest/URL. Only the archive content matters.
        boolean legacy = previous.matches("[0-9a-f]{64}:" + sha256 + ":" + bytes + ":https://.+");
        if ((!expected.equals(previous) && !legacy) || part.length() > bytes) {
            delete(part); delete(resume); etag = "";
        }
        if (part.isFile() && part.length() == bytes) {
            if (sha256.equals(hash(part, "校验", detail, 0, bytes))) {
                atomicMove(part, archive); delete(resume); return archive;
            }
            delete(part); delete(resume); etag = "";
        }
        // At most one restart for an unusable Range/validator response. Network failures retain valid partial data.
        for (int attempt = 0; attempt < 2; attempt++) {
            long offset = part.isFile() ? part.length() : 0;
            HttpURLConnection connection = open(address, offset, etag);
            try {
                int code = connection.getResponseCode();
                String responseEtag = connection.getHeaderField("ETag");
                boolean append = offset > 0 && code == 206
                        && (!strongEtag(etag) || !strongEtag(responseEtag) || etag.equals(responseEtag))
                        && validRange(connection.getHeaderField("Content-Range"), offset, bytes)
                        && expectedLength(connection, bytes - offset);
                if (offset > 0 && !append) {
                    // A 200 means If-Range failed or the server ignored Range; consume its complete representation.
                    if (code != 200) {
                        if (code != 206 && code != 416)
                            throw new IOException("下载暂时失败（HTTP " + code + "），已保留续传数据：" + name);
                        delete(part); delete(resume); etag = ""; continue;
                    }
                    offset = 0;
                }
                if ((!append && code != 200) || !expectedLength(connection, bytes - offset))
                    throw new IOException("下载响应异常（HTTP " + code + "）：" + name);
                String encoding = connection.getHeaderField("Content-Encoding");
                if (encoding != null && !encoding.isEmpty() && !encoding.equalsIgnoreCase("identity"))
                    throw new IOException("下载服务器返回了不支持的压缩传输");
                Properties metadata = new Properties();
                metadata.setProperty("identity", expected);
                metadata.setProperty("etag", strongEtag(responseEtag) ? responseEtag : "");
                // Truncate before publishing a new validator, so a crash can never combine old bytes with a new ETag.
                if (!append) { try (FileOutputStream ignored = new FileOutputStream(part, false)) { ignored.getFD().sync(); } }
                storeProperties(resume, metadata);
                long count = offset;
                try (InputStream input = connection.getInputStream(); FileOutputStream output = new FileOutputStream(part, true)) {
                    byte[] buffer = new byte[BUFFER];
                    report("下载", detail, count, bytes);
                    int read;
                    while ((read = input.read(buffer)) != -1) {
                        if (read > bytes - count) throw new IOException("下载数据超过清单大小：" + name);
                        output.write(buffer, 0, read); count += read;
                        report("下载", detail, count, bytes);
                    }
                    output.getFD().sync();
                }
                if (count != bytes) throw new IOException("下载中断，重试将继续下载：" + name);
                if (!sha256.equals(hash(part, "校验", detail, 0, bytes))) {
                    delete(part); delete(resume);
                    throw new IOException("资源包 SHA-256 校验失败，请重试：" + name);
                }
                atomicMove(part, archive); delete(resume); return archive;
            } finally { connection.disconnect(); }
        }
        throw new IOException("服务器不支持安全续传，请重试：" + name);
    }

    private HttpURLConnection open(URL address, long offset, String etag) throws IOException {
        for (int redirects = 0; redirects <= 5; redirects++) {
            validateUrl(address);
            HttpURLConnection connection = connections.open(address);
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(20000); connection.setReadTimeout(30000);
            connection.setRequestProperty("Accept-Encoding", "identity");
            connection.setRequestProperty("User-Agent", "eFootball-ResourceBootstrap/1");
            if (offset > 0) {
                connection.setRequestProperty("Range", "bytes=" + offset + "-");
                if (strongEtag(etag)) connection.setRequestProperty("If-Range", etag);
            }
            int code;
            try { code = connection.getResponseCode(); }
            catch (IOException error) { connection.disconnect(); throw error; }
            if (code != 301 && code != 302 && code != 303 && code != 307 && code != 308) return connection;
            String location = connection.getHeaderField("Location");
            connection.disconnect();
            if (location == null || redirects == 5) throw new IOException("资源下载重定向异常");
            address = new URL(address, location);
        }
        throw new IOException("资源下载重定向次数过多");
    }

    static boolean validRange(String value, long offset, long total) {
        if (value == null) return false;
        Matcher match = RANGE.matcher(value);
        if (!match.matches()) return false;
        try { return Long.parseLong(match.group(1)) == offset
                && Long.parseLong(match.group(2)) == total - 1 && Long.parseLong(match.group(3)) == total; }
        catch (NumberFormatException bad) { return false; }
    }

    private static boolean expectedLength(HttpURLConnection connection, long expected) {
        String value = connection.getHeaderField("Content-Length");
        if (value == null) return true; // Chunked transfer is bounded and checked after reading.
        try { return Long.parseLong(value) == expected; }
        catch (NumberFormatException invalid) { return false; }
    }

    private static boolean strongEtag(String value) {
        return value != null && value.length() >= 2 && value.startsWith("\"") && value.endsWith("\"")
                && value.indexOf('\n') < 0 && value.indexOf('\r') < 0;
    }

    void extract(Pack pack, File archive, Set<String> verified, int index) throws IOException {
        Map<String, Entry> allowed = new HashMap<String, Entry>();
        long total = 0, done = 0;
        for (Entry entry : pack.entries) { allowed.put(entry.path, entry); total += entry.bytes; }
        Set<String> seen = new HashSet<String>();
        String detail = index + "/" + manifest.packs.size() + " " + pack.name;
        try (ZipInputStream input = new ZipInputStream(new BufferedInputStream(new FileInputStream(archive), BUFFER))) {
            ZipEntry member;
            byte[] buffer = new byte[BUFFER];
            while ((member = input.getNextEntry()) != null) {
                String path = member.getName();
                Entry entry = allowed.get(path);
                try { validatePath(path); }
                catch (IllegalArgumentException invalid) { throw new IOException("资源包包含非法路径：" + path, invalid); }
                if (member.isDirectory() || !seen.add(path))
                    throw new IOException("资源包包含未声明或重复的路径：" + path);
                if (entry == null) {
                    if (!pack.allowSubset) throw new IOException("资源包包含未声明的路径：" + path);
                    // A prior immutable archive may also contain files superseded by a newer pack.
                    // Its complete SHA was checked before extraction; omitted members never write.
                    input.closeEntry(); continue;
                }
                File destination = target(path);
                if (member.getSize() != -1 && member.getSize() != entry.bytes)
                    throw new IOException("资源成员大小不符：" + path);
                boolean reuse = verified.contains(path);
                File temporary = stateFile("stage-" + entry.sha256 + ".tmp");
                MessageDigest sha = digest();
                long written = 0;
                try {
                    try (FileOutputStream output = reuse ? null : new FileOutputStream(temporary, false)) {
                        int read;
                        while ((read = input.read(buffer)) != -1) {
                            if (read > entry.bytes - written) throw new IOException("资源成员超过清单大小：" + path);
                            sha.update(buffer, 0, read);
                            if (output != null) output.write(buffer, 0, read);
                            written += read;
                            report("解压", detail, done + written, total);
                        }
                        if (output != null) output.getFD().sync();
                    }
                    if (written != entry.bytes || !entry.sha256.equals(hex(sha.digest())))
                        throw new IOException("资源成员校验失败：" + path);
                    if (!reuse) {
                        ensureDirectory(destination.getParentFile());
                        destination = target(path); // Recheck canonical containment after creating parent directories.
                        atomicMove(temporary, destination);
                    }
                    verified.add(path); done += written; input.closeEntry();
                } finally { delete(temporary); }
            }
        }
        if (!seen.containsAll(allowed.keySet())) throw new IOException("资源包缺少清单成员：" + pack.name);
        report("解压", detail, total, total);
    }

    private String hash(File file, String stage, String detail, long offset, long total) throws IOException {
        MessageDigest sha = digest(); long readBytes = 0;
        try (InputStream input = new BufferedInputStream(new FileInputStream(file), BUFFER)) {
            byte[] buffer = new byte[BUFFER]; int read;
            while ((read = input.read(buffer)) != -1) {
                sha.update(buffer, 0, read); readBytes += read;
                report(stage, detail, offset + readBytes, total);
            }
        }
        return hex(sha.digest());
    }

    File target(String path) throws IOException {
        try { validatePath(path); }
        catch (IllegalArgumentException invalid) { throw new IOException(invalid.getMessage(), invalid); }
        File result = new File(root, path);
        checkInside(result);
        return result;
    }

    private File stateFile(String name) throws IOException {
        File result = new File(state, name); checkInside(result); return result;
    }

    private void checkInside(File file) throws IOException {
        File canonical = file.getCanonicalFile();
        if (!canonical.getPath().startsWith(root.getPath() + File.separator))
            throw new IOException("拒绝访问资源目录之外的文件");
        File cursor = file;
        while (cursor != null && !cursor.equals(root)) {
            if (Files.isSymbolicLink(cursor.toPath())) throw new IOException("资源路径包含符号链接");
            cursor = cursor.getParentFile();
        }
    }

    private void ensureDirectory(File directory) throws IOException {
        if (!directory.equals(root)) checkInside(directory);
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory())
            throw new IOException("无法创建资源目录");
    }

    private String markerText() { return "resource-bootstrap-v1\n" + manifest.identity + "\n"; }
    private void cleanupPack(Pack pack) throws IOException {
        delete(stateFile(pack.name)); delete(stateFile(pack.name + ".part")); delete(stateFile(pack.name + ".resume"));
    }
    private void report(String stage, String detail, long done, long total) {
        if (listener != null) listener.progress(stage, detail, done, total);
    }

    static void validateUrl(URL url) throws IOException {
        if (!"https".equalsIgnoreCase(url.getProtocol()) || !HOSTS.contains(url.getHost().toLowerCase(Locale.ROOT))
                || (url.getPort() != -1 && url.getPort() != 443) || url.getUserInfo() != null || url.getRef() != null)
            throw new IOException("资源下载地址不在允许的 HTTPS 域名内");
    }
    private static void validatePath(String path) {
        if (path == null || !(path.startsWith("assetpacks/") || path.startsWith("Download/"))
                || path.indexOf('\\') >= 0 || path.indexOf(':') >= 0 || path.indexOf('\0') >= 0)
            throw new IllegalArgumentException("非法资源路径");
        for (String part : path.split("/", -1))
            if (part.isEmpty() || part.equals(".") || part.equals("..")) throw new IllegalArgumentException("非法资源路径");
    }
    private static void validateHash(String sha) {
        if (sha == null || !sha.matches("[0-9a-f]{64}")) throw new IllegalArgumentException("SHA-256 格式无效");
    }
    private static MessageDigest digest() {
        try { return MessageDigest.getInstance("SHA-256"); }
        catch (NoSuchAlgorithmException impossible) { throw new AssertionError(impossible); }
    }
    private static void update(MessageDigest digest, String... strings) {
        for (String value : strings) { digest.update(value.getBytes(StandardCharsets.UTF_8)); digest.update((byte) 0); }
    }
    static String hex(byte[] bytes) {
        char[] out = new char[bytes.length * 2], alphabet = "0123456789abcdef".toCharArray();
        for (int i = 0; i < bytes.length; i++) { out[i * 2] = alphabet[(bytes[i] & 255) >>> 4]; out[i * 2 + 1] = alphabet[bytes[i] & 15]; }
        return new String(out);
    }
    private static byte[] readSmall(File file, int maximum) throws IOException {
        try (InputStream input = new FileInputStream(file); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[1024]; int read;
            while ((read = input.read(buffer)) != -1) {
                if (output.size() + read > maximum) throw new IOException("本地资源状态文件过大");
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }
    private static Properties readProperties(File file) {
        Properties result = new Properties();
        try { if (file.isFile()) result.load(new ByteArrayInputStream(readSmall(file, 16384))); }
        catch (IOException | IllegalArgumentException ignored) { result.clear(); }
        return result;
    }
    private static void storeProperties(File file, Properties properties) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        properties.store(output, "Resource bootstrap resume metadata"); atomicWrite(file, output.toByteArray());
    }
    private static void atomicWrite(File destination, byte[] bytes) throws IOException {
        File temporary = new File(destination.getParentFile(), destination.getName() + ".tmp");
        if (Files.isSymbolicLink(destination.toPath()) || Files.isSymbolicLink(temporary.toPath()))
            throw new IOException("资源状态路径包含符号链接");
        try {
            try (FileOutputStream output = new FileOutputStream(temporary)) { output.write(bytes); output.getFD().sync(); }
            atomicMove(temporary, destination);
        } finally { delete(temporary); }
    }
    private static void atomicMove(File source, File destination) throws IOException {
        Files.move(source.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    }
    private static void delete(File file) throws IOException {
        if (file.exists() && !file.delete()) throw new IOException("无法清理资源临时文件：" + file.getName());
    }
}
