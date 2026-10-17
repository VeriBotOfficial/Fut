package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.WindowManager;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.lang.ref.WeakReference;
import java.net.SocketTimeoutException;
import java.net.UnknownHostException;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ThreadFactory;
import org.json.JSONObject;

/** Intercepts the original gateway only while preparing local resources or an optional update. */
public final class ResourceBootstrap {
    private static final Coordinator COORDINATOR = new Coordinator();
    private ResourceBootstrap() { }

    public static boolean intercept(Activity activity) {
        if (Looper.myLooper() != Looper.getMainLooper()) throw new IllegalStateException("Main thread required");
        if (COORDINATOR.passNextLaunch) {
            COORDINATOR.passNextLaunch = false;
            activity.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            return false;
        }
        COORDINATOR.attach(activity);
        return true;
    }

    private enum Task { RESOURCES, APK }
    private static final class State {
        final BootstrapScreen.Stage stage;
        final String title, message, task, error;
        final long done, total;
        final BootstrapScreen.ProgressUnit unit;
        final double speed;
        final boolean complete;
        State(BootstrapScreen.Stage stage, String title, String message, String task, long done, long total,
              BootstrapScreen.ProgressUnit unit, double speed, String error, boolean complete) {
            this.stage = stage; this.title = title; this.message = message; this.task = task;
            this.done = done; this.total = total; this.unit = unit; this.speed = speed;
            this.error = error; this.complete = complete;
        }
        static State note(BootstrapScreen.Stage stage, String title, String message) {
            return new State(stage, title, message, "", 0, 0, BootstrapScreen.ProgressUnit.NONE, 0, "", false);
        }
    }

    private static final class Coordinator implements ResourceInstaller.Listener, BootstrapScreen.Listener {
        final Handler main = new Handler(Looper.getMainLooper());
        final ExecutorService worker = Executors.newSingleThreadExecutor(new ThreadFactory() {
            public Thread newThread(Runnable runnable) { return new Thread(runnable, "efootxi-resource-worker"); }
        });
        WeakReference<BootstrapScreen> screen = new WeakReference<BootstrapScreen>(null);
        WeakReference<Activity> activity = new WeakReference<Activity>(null);
        volatile State state = State.note(BootstrapScreen.Stage.CHECKING, "正在读取资源状态", "请稍候…");
        volatile State pausedFrom;
        volatile boolean running, resourcesReady, online, pauseRequested;
        boolean passNextLaunch, entering, started, enterAfterPause;
        volatile ResourceInstaller installer;
        volatile Task task = Task.RESOURCES;
        volatile String versionName = "1.2", resourceVersion = "";
        volatile long buildRevision = 1206;
        volatile ApkUpdater.Info offeredUpdate;
        volatile File downloadedApk;
        Context application;
        private boolean paintQueued;
        private long lastPaint, speedTime, speedBytes;
        private String speedTask = "";
        private double measuredSpeed;

        void attach(Activity owner) {
            activity = new WeakReference<Activity>(owner);
            screen = new WeakReference<BootstrapScreen>(new BootstrapScreen(owner, this));
            entering = false;
            paint();
            if (!started) { started = true; startResources(owner.getApplicationContext()); }
        }

        void startResources(final Context context) {
            if (running) return;
            application = context; task = Task.RESOURCES;
            running = true; pauseRequested = false; resourcesReady = false;
            installer = null; offeredUpdate = null; downloadedApk = null;
            resetSpeed();
            state = State.note(BootstrapScreen.Stage.CHECKING, "正在检查本地资源", "已完成的内容会保留"); paint();
            worker.execute(new Runnable() {
                public void run() {
                    State result;
                    UpdateChannel channel = null;
                    UpdateChannel.Selection installed = null, selection = null;
                    try {
                        ResourceInstaller.Manifest embedded = loadManifest(context);
                        PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
                        if (!embedded.packageName.equals(context.getPackageName()) || embedded.versionCode != info.getLongVersionCode())
                            throw new IOException("安装包与资源版本不一致");
                        JSONObject config = new JSONObject(readAsset(context, "resource-bootstrap/config.json"));
                        versionName = config.getString("versionName"); buildRevision = config.getLong("buildRevision");
                        resourceVersion = embedded.resourceVersion;
                        try { ApkUpdater.cleanupInstalled(context, buildRevision); }
                        catch (IOException ignored) { /* Optional cache maintenance must not prevent play. */ }
                        online = hasInternet(context);
                        channel = new UpdateChannel(context.getFilesDir(), context.getPackageName(), embedded.versionCode, config.getString("channelUrl"));
                        installed = channel.installedSelection(embedded, config.getString("baseUrl"));
                        if (online) progress("检查更新", "正在检查是否有新资源", 0, 0);
                        selection = channel.resolve(embedded, config.getString("baseUrl"), online);
                        resourceVersion = selection.manifest.resourceVersion;
                        offeredUpdate = newerApk(selection);
                        installer = createInstaller(context, selection, online);
                        if (pauseRequested) installer.requestPause();
                        installer.install();
                        resourcesReady = true;
                        try { channel.commitInstalled(selection); }
                        catch (IOException ignored) { /* Complete content remains usable if optional metadata commit fails. */ }
                        result = readyState();
                    } catch (ResourceInstaller.PausedException paused) {
                        resourcesReady = previousResourcesUsable(context, installed, selection);
                        result = pausedState("已暂停", "已完成的内容会保留，继续时从断点恢复");
                    } catch (Exception error) {
                        // A new update may be incomplete. Only offer old content when the installer verifies it is still usable.
                        resourcesReady = previousResourcesUsable(context, installed, selection);
                        result = failure(error);
                    }
                    finish(result);
                }
            });
        }

        boolean previousResourcesUsable(Context context, UpdateChannel.Selection installed, UpdateChannel.Selection selection) {
            if (installed == null || selection == null || installed.manifest.identity.equals(selection.manifest.identity)) return false;
            try { return createInstaller(context, installed, false).isReady(); }
            catch (IOException ignored) { return false; }
        }

        State pausedState(String title, String message) {
            State previous = pausedFrom;
            return new State(BootstrapScreen.Stage.PAUSED, title, message, previous == null ? "" : previous.task,
                    previous == null ? 0 : previous.done, previous == null ? 0 : previous.total,
                    previous == null ? BootstrapScreen.ProgressUnit.NONE : previous.unit, 0, "", false);
        }

        ResourceInstaller createInstaller(final Context context, UpdateChannel.Selection selection, boolean allowNetwork) throws IOException {
            return new ResourceInstaller(context.getFilesDir(), selection.manifest, selection.baseUrl, this)
                    .setNetworkAllowed(allowNetwork)
                    .setSeedSource(new ResourceInstaller.SeedSource() {
                        public InputStream open(String path) throws IOException {
                            return context.getAssets().open("resource-bootstrap/seeds/" + path);
                        }
                    });
        }

        ApkUpdater.Info newerApk(UpdateChannel.Selection selection) {
            if (!online || selection.apk == null) return null;
            try {
                ApkUpdater.Info candidate = new ApkUpdater.Info(selection.apk);
                return candidate.buildRevision > buildRevision ? candidate : null;
            } catch (IOException malformed) { return null; }
        }

        State readyState() {
            if (offeredUpdate != null) return State.note(BootstrapScreen.Stage.UPDATE_AVAILABLE, "发现应用更新",
                    "可更新至 " + offeredUpdate.versionName + "，也可以先进入游戏");
            return new State(BootstrapScreen.Stage.READY, "资源已就绪", "正在进入游戏…", "", 1, 1,
                    BootstrapScreen.ProgressUnit.NONE, 0, "", true);
        }

        void downloadUpdate() {
            if (running || offeredUpdate == null || installer == null || !resourcesReady) return;
            task = Task.APK; running = true; pauseRequested = false; downloadedApk = null;
            online = hasInternet(application); installer.resume(); installer.setNetworkAllowed(online); resetSpeed();
            state = State.note(BootstrapScreen.Stage.CHECKING, "正在准备应用更新", "更新会覆盖当前应用并保留数据"); paint();
            worker.execute(new Runnable() {
                public void run() {
                    State result;
                    try {
                        downloadedApk = ApkUpdater.downloadAndVerify(application, installer, offeredUpdate);
                        result = State.note(BootstrapScreen.Stage.INSTALL_READY, "更新已下载", "点击安装更新，在系统页面确认覆盖安装");
                    } catch (ResourceInstaller.PausedException paused) {
                        result = pausedState("更新已暂停", "已保留下载进度，可以继续下载或先进入游戏");
                    } catch (Exception error) { result = failure(error); }
                    finish(result);
                }
            });
        }

        void finish(final State result) {
            main.post(new Runnable() {
                public void run() {
                    running = false; state = result;
                    if (enterAfterPause && resourcesReady) { enterAfterPause = false; enterGame(); }
                    else paint();
                }
            });
        }

        State failure(Exception error) {
            String message = error.getMessage();
            if (message == null) message = error.getClass().getSimpleName();
            String human;
            if (error instanceof ResourceInstaller.OfflineRequiredException)
                human = "当前没有网络，仍有必需资源未准备好。连接网络后可继续，已完成内容会保留。";
            else if (error instanceof UnknownHostException || error instanceof SocketTimeoutException || message.contains("HTTP")
                    || message.contains("resolve host") || message.contains("连接") || message.contains("下载"))
                human = "暂时无法完成下载，请检查网络后重试。已下载内容会保留。";
            else if (message.contains("空间")) human = message;
            else human = "资源准备暂未完成，已保留现有文件。可重试或查看详细原因。";
            StringBuilder details = new StringBuilder(error.getClass().getSimpleName()).append(": ").append(message);
            ResourceInstaller active = installer;
            if (active != null) {
                List<String> diagnostics = active.diagnostics();
                for (String item : diagnostics) { if (details.length() > 12000) break; details.append('\n').append(item); }
            }
            return new State(BootstrapScreen.Stage.ERROR, task == Task.APK ? "应用更新未完成" : "资源准备未完成",
                    human, "", 0, 0, BootstrapScreen.ProgressUnit.NONE, 0, details.toString(), false);
        }

        public void onPause() {
            if (!running || installer == null) return;
            pausedFrom = state; pauseRequested = true; installer.requestPause();
            state = State.note(BootstrapScreen.Stage.CHECKING, "正在暂停", "正在保存已完成的进度…"); paint();
        }
        public void onResume() { if (task == Task.APK) downloadUpdate(); else startResources(application); }
        public void onRetry() { onResume(); }
        public void onDownloadUpdate() { downloadUpdate(); }
        public void onInstallUpdate() {
            Activity owner = activity.get();
            if (running || downloadedApk == null || owner == null || owner.isFinishing()) return;
            try {
                boolean opened = ApkUpdater.requestInstall(owner, downloadedApk);
                state = State.note(BootstrapScreen.Stage.INSTALL_READY, "安装应用更新", opened
                        ? "请在系统页面确认安装，取消后仍可进入游戏"
                        : "请允许此应用安装更新，返回后再次点击安装更新");
            } catch (Exception error) { state = failure(error); }
            paint();
        }
        public void onEnterGame() {
            if (!resourcesReady) return;
            if (running) { enterAfterPause = true; onPause(); }
            else enterGame();
        }
        void enterGame() {
            if (!resourcesReady || running) return;
            state = new State(BootstrapScreen.Stage.READY, "资源已就绪", "正在进入游戏…", "", 1, 1,
                    BootstrapScreen.ProgressUnit.NONE, 0, "", true); paint();
        }

        public void progress(String stage, String detail, long done, long total) {
            BootstrapScreen.Stage visual;
            BootstrapScreen.ProgressUnit unit = BootstrapScreen.ProgressUnit.NONE;
            String title, message; double speed = 0;
            if ("自动重试".equals(stage)) {
                visual = BootstrapScreen.Stage.DOWNLOADING; title = "连接中断，正在自动重试";
                message = "已下载的内容会保留。网络恢复后会自动继续，也可以先暂停下载。";
                unit = BootstrapScreen.ProgressUnit.BYTES; resetSpeed();
            } else if ("下载".equals(stage)) {
                visual = BootstrapScreen.Stage.DOWNLOADING; title = task == Task.APK ? "正在下载应用更新" : "正在下载资源";
                message = "已完成的内容会保留，中断后可以继续下载"; unit = BootstrapScreen.ProgressUnit.BYTES;
                long now = SystemClock.elapsedRealtime();
                synchronized (this) {
                    if (!detail.equals(speedTask) || done < speedBytes || speedTime == 0) {
                        speedTask = detail; speedTime = now; speedBytes = done; measuredSpeed = 0;
                    } else if (now - speedTime >= 600) {
                        measuredSpeed = (done - speedBytes) * 1000d / (now - speedTime);
                        speedTime = now; speedBytes = done;
                    }
                    speed = measuredSpeed;
                }
            } else if ("校验".equals(stage)) {
                visual = BootstrapScreen.Stage.VERIFYING; title = task == Task.APK ? "正在校验应用更新" : "正在校验本地资源";
                message = "正在读取本地文件，不消耗流量"; unit = BootstrapScreen.ProgressUnit.BYTES;
            } else if (stage.equals("准备") || stage.contains("解压") || stage.contains("部署") || stage.contains("安装") || stage.contains("导入")) {
                visual = BootstrapScreen.Stage.DEPLOYING; title = "正在安装资源";
                message = "正在保存已验证的文件，请稍候"; unit = BootstrapScreen.ProgressUnit.BYTES;
            } else {
                visual = BootstrapScreen.Stage.CHECKING; title = "完成".equals(stage) ? "资源检查完成" : stage.equals("检查更新") ? "正在检查更新" : "正在检查本地资源";
                message = online ? "完整资源会继续复用" : "离线使用本地资源，不检查远程更新";
                if (total > 0) unit = BootstrapScreen.ProgressUnit.FILES;
            }
            if (pauseRequested) return;
            state = new State(visual, title, message, detail, done, total, unit, speed, "", false);
            synchronized (this) {
                if (paintQueued) return; paintQueued = true;
                long delay = Math.max(0, 160 - (SystemClock.uptimeMillis() - lastPaint));
                main.postDelayed(new Runnable() {
                    public void run() {
                        synchronized (Coordinator.this) { paintQueued = false; lastPaint = SystemClock.uptimeMillis(); }
                        paint();
                    }
                }, delay);
            }
        }
        synchronized void resetSpeed() { speedTime = 0; speedBytes = 0; speedTask = ""; measuredSpeed = 0; }

        void paint() {
            final BootstrapScreen current = screen.get(); final Activity owner = activity.get();
            if (current == null || !current.alive() || owner == null) return;
            State value = state;
            boolean paused = value.stage == BootstrapScreen.Stage.PAUSED;
            boolean failed = value.stage == BootstrapScreen.Stage.ERROR;
            boolean optional = resourcesReady && !running;
            boolean mayPause = running && installer != null && !pauseRequested;
            BootstrapScreen.Model model = new BootstrapScreen.Model.Builder(value.stage)
                    .version(versionName, buildRevision).resourceVersion(resourceVersion).network(online)
                    .resourcesReady(resourcesReady).text(value.title, value.message)
                    .progress(value.task, value.done, value.total, value.unit).speed(value.speed)
                    .details("应用 " + versionName + " · 构建 " + buildRevision + "\n资源 " + resourceVersion)
                    .errorDetails(value.error).canPause(mayPause).canResume(paused && !running)
                    .canRetry(failed && !running && downloadedApk == null)
                    .canDownloadUpdate(optional && value.stage == BootstrapScreen.Stage.UPDATE_AVAILABLE && offeredUpdate != null && downloadedApk == null)
                    .canInstallUpdate(optional && downloadedApk != null)
                    .canEnterGame(resourcesReady && (!running || task == Task.APK))
                    .enterGameLabel(running ? "暂停更新并进入游戏" : "进入游戏").build();
            current.render(model);
            if (running) owner.getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            else owner.getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            if (value.complete && resourcesReady && !running && !entering) {
                entering = true;
                main.post(new Runnable() {
                    public void run() {
                        if (screen.get() != current || activity.get() != owner || !current.alive()) { entering = false; return; }
                        passNextLaunch = true;
                        try { owner.recreate(); }
                        catch (RuntimeException error) {
                            passNextLaunch = false; entering = false;
                            state = State.note(BootstrapScreen.Stage.ERROR, "资源已就绪", "请退出后重新打开游戏"); paint();
                        }
                    }
                });
            }
        }
    }

    private static ResourceInstaller.Manifest loadManifest(Context context) throws Exception {
        return UpdateChannel.parseManifest(readAsset(context, "resource-bootstrap/manifest.json").getBytes(StandardCharsets.UTF_8));
    }
    private static boolean hasInternet(Context context) {
        try {
            ConnectivityManager manager = (ConnectivityManager) context.getSystemService(Context.CONNECTIVITY_SERVICE);
            Network network = manager == null ? null : manager.getActiveNetwork();
            NetworkCapabilities capabilities = network == null ? null : manager.getNetworkCapabilities(network);
            // A failed Android validation probe does not prove the GitHub download is unreachable.
            // A missing usable network disables every resource transport; connected networks may try.
            return capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
        } catch (RuntimeException unavailable) { return false; }
    }
    private static String readAsset(Context context, String name) throws IOException {
        try (InputStream input = context.getAssets().open(name); ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192]; int count;
            while ((count = input.read(buffer)) != -1) {
                if (output.size() + count > 512 * 1024) throw new IOException("资源配置文件过大");
                output.write(buffer, 0, count);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
