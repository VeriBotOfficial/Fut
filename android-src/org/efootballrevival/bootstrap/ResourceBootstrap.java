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
        volatile State state = State.note(BootstrapScreen.Stage.CHECKING, "Reading resource status", "Please wait…");
        volatile State pausedFrom;
        volatile boolean running, resourcesReady, online, pauseRequested;
        boolean passNextLaunch, entering, started, enterAfterPause;
        volatile ResourceInstaller installer;
        volatile Task task = Task.RESOURCES;
        volatile String versionName = "1.2", resourceVersion = "";
        volatile long buildRevision = 1206;
        volatile ApkUpdater.Info offeredUpdate;
        volatile File downloadedApk;
        volatile UpdateChannel activeChannel;
        volatile UpdateChannel.Selection activeSelection;
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
            state = State.note(BootstrapScreen.Stage.CHECKING, "Checking local resources", "Completed data will be kept"); paint();
            worker.execute(new Runnable() {
                public void run() {
                    State result;
                    UpdateChannel channel = null;
                    UpdateChannel.Selection installed = null, selection = null;
                    try {
                        ResourceInstaller.Manifest embedded = loadManifest(context);
                        PackageInfo info = context.getPackageManager().getPackageInfo(context.getPackageName(), 0);
                        if (!embedded.packageName.equals(context.getPackageName()) || embedded.versionCode != info.getLongVersionCode())
                            throw new IOException("The APK and resource versions do not match");
                        JSONObject config = new JSONObject(readAsset(context, "resource-bootstrap/config.json"));
                        versionName = config.getString("versionName"); buildRevision = config.getLong("buildRevision");
                        resourceVersion = embedded.resourceVersion;
                        try { ApkUpdater.cleanupInstalled(context, buildRevision); }
                        catch (IOException ignored) { /* Optional cache maintenance must not prevent play. */ }
                        online = hasInternet(context);
                        channel = new UpdateChannel(context.getFilesDir(), context.getPackageName(), embedded.versionCode, config.getString("channelUrl"));
                        installed = channel.installedSelection(embedded, config.getString("baseUrl"));
                        if (online) progress("Check for updates", "Checking for new resources", 0, 0);
                        selection = channel.resolve(embedded, config.getString("baseUrl"), online);
                        resourceVersion = selection.manifest.resourceVersion;
                        offeredUpdate = newerApk(selection);
                        installer = createInstaller(context, selection, online);
                        activeChannel = channel;
                        activeSelection = selection;
                        if (pauseRequested) installer.requestPause();

                        // First-time installs stop here and wait for one explicit Download tap.
                        // Existing installs are checked against their durable receipt, not re-downloaded.
                        boolean existingReady = installed != null
                                && createInstaller(context, installed, false).isReady();
                        if (!existingReady) {
                            resourcesReady = false;
                            result = State.note(BootstrapScreen.Stage.NEEDS_DOWNLOAD,
                                    "Ready to download", "Download the game data once. It will be kept on this device.");
                        } else {
                            if (!installed.manifest.identity.equals(selection.manifest.identity)) {
                                installer.install();
                            }
                            resourcesReady = true;
                            try { channel.commitInstalled(selection); }
                            catch (IOException ignored) { /* Verified game data remains usable. */ }
                            result = readyState();
                        }
                    } catch (ResourceInstaller.PausedException paused) {
                        resourcesReady = previousResourcesUsable(context, installed, selection);
                        result = pausedState("Paused", "Completed data will be kept. Resume from where you left off.");
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
            // App updates are optional and must never hold up the normal game launch.
            return new State(BootstrapScreen.Stage.READY, "Game data ready", "Starting game…", "", 1, 1,
                    BootstrapScreen.ProgressUnit.NONE, 0, "", true);
        }

        void downloadResources() {
            if (running || resourcesReady || installer == null) return;
            task = Task.RESOURCES;
            running = true;
            pauseRequested = false;
            online = hasInternet(application);
            installer.resume();
            installer.setNetworkAllowed(online);
            resetSpeed();
            state = State.note(BootstrapScreen.Stage.CHECKING, "Preparing download", "Your progress will be saved automatically");
            paint();
            worker.execute(new Runnable() {
                public void run() {
                    State result;
                    try {
                        installer.install();
                        resourcesReady = true;
                        UpdateChannel channel = activeChannel;
                        UpdateChannel.Selection selection = activeSelection;
                        if (channel != null && selection != null) {
                            try { channel.commitInstalled(selection); }
                            catch (IOException ignored) { }
                        }
                        result = readyState();
                    } catch (ResourceInstaller.PausedException paused) {
                        result = pausedState("Download paused", "Your downloaded data has been saved.");
                    } catch (Exception error) {
                        result = failure(error);
                    }
                    finish(result);
                }
            });
        }

        void downloadUpdate() {
            if (running || offeredUpdate == null || installer == null || !resourcesReady) return;
            task = Task.APK; running = true; pauseRequested = false; downloadedApk = null;
            online = hasInternet(application); installer.resume(); installer.setNetworkAllowed(online); resetSpeed();
            state = State.note(BootstrapScreen.Stage.CHECKING, "Preparing app update", "The update will replace the current app while keeping its data"); paint();
            worker.execute(new Runnable() {
                public void run() {
                    State result;
                    try {
                        downloadedApk = ApkUpdater.downloadAndVerify(application, installer, offeredUpdate);
                        result = State.note(BootstrapScreen.Stage.INSTALL_READY, "Update downloaded", "Tap Install Update and confirm in Android's installer");
                    } catch (ResourceInstaller.PausedException paused) {
                        result = pausedState("Update paused", "Download progress saved. You can resume or enter the game.");
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
                human = "You're offline and some required resources are still missing. Connect to a network to continue; completed downloads will be kept.";
            else if (error instanceof UnknownHostException || error instanceof SocketTimeoutException || message.contains("HTTP")
                    || message.contains("resolve host") || message.contains("connection") || message.contains("download"))
                human = "The download could not be completed. Check your connection and try again. Downloaded data will be kept.";
            else if (message.contains("storage")) human = message;
            else human = "Resource setup is incomplete, but existing files have been kept. Retry or view the details.";
            StringBuilder details = new StringBuilder(error.getClass().getSimpleName()).append(": ").append(message);
            ResourceInstaller active = installer;
            if (active != null) {
                List<String> diagnostics = active.diagnostics();
                for (String item : diagnostics) { if (details.length() > 12000) break; details.append('\n').append(item); }
            }
            return new State(BootstrapScreen.Stage.ERROR, task == Task.APK ? "App update incomplete" : "Resource setup incomplete",
                    human, "", 0, 0, BootstrapScreen.ProgressUnit.NONE, 0, details.toString(), false);
        }

        public void onPause() {
            if (!running || installer == null) return;
            pausedFrom = state; pauseRequested = true; installer.requestPause();
            state = State.note(BootstrapScreen.Stage.CHECKING, "Pausing", "Saving completed progress…"); paint();
        }
        public void onResume() { if (task == Task.APK) downloadUpdate(); else downloadResources(); }
        public void onRetry() {
            if (task == Task.APK) downloadUpdate();
            else if (installer != null) downloadResources();
            else startResources(application);
        }
        public void onDownloadResources() { downloadResources(); }
        public void onDownloadUpdate() { downloadUpdate(); }
        public void onInstallUpdate() {
            Activity owner = activity.get();
            if (running || downloadedApk == null || owner == null || owner.isFinishing()) return;
            try {
                boolean opened = ApkUpdater.requestInstall(owner, downloadedApk);
                state = State.note(BootstrapScreen.Stage.INSTALL_READY, "Install app update", opened
                        ? "Confirm installation in Android's installer. You can still enter the game if you cancel."
                        : "Allow this app to install updates, then return and tap Install Update again");
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
            state = new State(BootstrapScreen.Stage.READY, "Resources ready", "Launching game…", "", 1, 1,
                    BootstrapScreen.ProgressUnit.NONE, 0, "", true); paint();
        }

        public void progress(String stage, String detail, long done, long total) {
            BootstrapScreen.Stage visual;
            BootstrapScreen.ProgressUnit unit = BootstrapScreen.ProgressUnit.NONE;
            String title, message; double speed = 0;
            if ("Automatic retry".equals(stage)) {
                visual = BootstrapScreen.Stage.DOWNLOADING; title = "Connection lost. Retrying automatically";
                message = "Downloaded data will be kept. The download will resume when your connection returns, or you can pause it.";
                unit = BootstrapScreen.ProgressUnit.BYTES; resetSpeed();
            } else if ("Download".equals(stage)) {
                visual = BootstrapScreen.Stage.DOWNLOADING; title = task == Task.APK ? "Downloading app update" : "Downloading resources";
                message = "Completed data will be kept. You can resume after an interruption."; unit = BootstrapScreen.ProgressUnit.BYTES;
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
            } else if ("Verification".equals(stage)) {
                visual = BootstrapScreen.Stage.VERIFYING; title = task == Task.APK ? "Verifying app update" : "Verifying local resources";
                message = "Reading local files; no data usage"; unit = BootstrapScreen.ProgressUnit.BYTES;
            } else if (stage.equals("Setup") || stage.contains("Extracting") || stage.contains("Deploying") || stage.contains("Installing") || stage.contains("Importing")) {
                visual = BootstrapScreen.Stage.DEPLOYING; title = "Installing resources";
                message = "Saving verified files. Please wait."; unit = BootstrapScreen.ProgressUnit.BYTES;
            } else {
                visual = BootstrapScreen.Stage.CHECKING; title = "Complete".equals(stage) ? "Resource check complete" : stage.equals("Check for updates") ? "Checking for updates" : "Checking local resources";
                message = online ? "Verified resources will be reused" : "Using local resources offline; remote updates are not checked";
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
                    .details("App " + versionName + " · Build " + buildRevision + "\nResources " + resourceVersion)
                    .errorDetails(value.error).canPause(mayPause).canResume(paused && !running)
                    .canRetry(failed && !running && downloadedApk == null)
                    .canDownloadResources(!running && !resourcesReady && value.stage == BootstrapScreen.Stage.NEEDS_DOWNLOAD)
                    .canDownloadUpdate(optional && value.stage == BootstrapScreen.Stage.UPDATE_AVAILABLE && offeredUpdate != null && downloadedApk == null)
                    .canInstallUpdate(optional && downloadedApk != null)
                    .canEnterGame(resourcesReady && (!running || task == Task.APK))
                    .enterGameLabel(running ? "Pause update and enter game" : "Enter Game").build();
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
                            state = State.note(BootstrapScreen.Stage.ERROR, "Resources ready", "Please exit and reopen the game."); paint();
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
                if (output.size() + count > 512 * 1024) throw new IOException("Resource configuration file is too large");
                output.write(buffer, 0, count);
            }
            return new String(output.toByteArray(), StandardCharsets.UTF_8);
        }
    }
}
