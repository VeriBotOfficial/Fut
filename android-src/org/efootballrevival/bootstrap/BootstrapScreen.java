package org.efootballrevival.bootstrap;

import android.app.ActionBar;
import android.app.Activity;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Insets;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import java.util.Locale;

/** Native, scrollable presentation only. The coordinator owns every operation and permission. */
public final class BootstrapScreen {
    public interface Listener {
        void onPause();
        void onResume();
        void onRetry();
        void onDownloadUpdate();
        void onInstallUpdate();
        void onEnterGame();
    }

    public enum Stage {
        CHECKING, VERIFYING, DOWNLOADING, DEPLOYING, READY, PAUSED, ERROR,
        UPDATE_AVAILABLE, INSTALL_READY
    }

    public enum ProgressUnit { BYTES, FILES, NONE }

    public static final class Model {
        public final Stage stage;
        public final String versionName, resourceVersion, title, message, taskLabel;
        public final String details, errorDetails, enterGameLabel;
        public final long buildRevision, done, total;
        public final ProgressUnit unit;
        public final double bytesPerSecond;
        public final boolean online, resourcesReady;
        public final boolean canPause, canResume, canRetry, canDownloadUpdate, canInstallUpdate, canEnterGame;

        private Model(Builder b) {
            stage = b.stage; versionName = clean(b.versionName); buildRevision = b.buildRevision;
            resourceVersion = clean(b.resourceVersion); title = clean(b.title); message = clean(b.message);
            taskLabel = clean(b.taskLabel); details = clean(b.details); errorDetails = clean(b.errorDetails);
            enterGameLabel = clean(b.enterGameLabel); done = Math.max(0, b.done); total = b.total;
            unit = b.unit == null ? ProgressUnit.NONE : b.unit;
            bytesPerSecond = Double.isNaN(b.bytesPerSecond) || Double.isInfinite(b.bytesPerSecond)
                    ? 0 : Math.max(0, b.bytesPerSecond);
            online = b.online; resourcesReady = b.resourcesReady;
            canPause = b.canPause; canResume = b.canResume; canRetry = b.canRetry;
            canDownloadUpdate = b.canDownloadUpdate; canInstallUpdate = b.canInstallUpdate;
            canEnterGame = b.canEnterGame && b.resourcesReady;
        }

        public static final class Builder {
            private final Stage stage;
            private String versionName = "", resourceVersion = "", title = "", message = "", taskLabel = "";
            private String details = "", errorDetails = "", enterGameLabel = "进入游戏";
            private long buildRevision, done, total;
            private ProgressUnit unit = ProgressUnit.NONE;
            private double bytesPerSecond;
            private boolean online, resourcesReady, canPause, canResume, canRetry;
            private boolean canDownloadUpdate, canInstallUpdate, canEnterGame;

            public Builder(Stage stage) { this.stage = stage == null ? Stage.CHECKING : stage; }
            public Builder version(String name, long revision) { versionName = name; buildRevision = revision; return this; }
            public Builder resourceVersion(String value) { resourceVersion = value; return this; }
            public Builder network(boolean value) { online = value; return this; }
            public Builder resourcesReady(boolean value) { resourcesReady = value; return this; }
            public Builder text(String heading, String explanation) { title = heading; message = explanation; return this; }
            public Builder progress(String label, long completed, long expected, ProgressUnit progressUnit) {
                taskLabel = label; done = completed; total = expected; unit = progressUnit; return this;
            }
            public Builder speed(double value) { bytesPerSecond = value; return this; }
            public Builder details(String value) { details = value; return this; }
            public Builder errorDetails(String value) { errorDetails = value; return this; }
            public Builder canPause(boolean value) { canPause = value; return this; }
            public Builder canResume(boolean value) { canResume = value; return this; }
            public Builder canRetry(boolean value) { canRetry = value; return this; }
            public Builder canDownloadUpdate(boolean value) { canDownloadUpdate = value; return this; }
            public Builder canInstallUpdate(boolean value) { canInstallUpdate = value; return this; }
            public Builder canEnterGame(boolean value) { canEnterGame = value; return this; }
            public Builder enterGameLabel(String value) { enterGameLabel = value; return this; }
            public Model build() { return new Model(this); }
        }
    }

    private static final int BLUE = Color.rgb(39, 101, 220);
    private static final int INK = Color.rgb(24, 43, 74);
    private static final int MUTED = Color.rgb(83, 104, 135);
    private static final int LINE = Color.rgb(214, 227, 246);
    private static final int PALE = Color.rgb(239, 246, 255);
    private final Activity activity;
    private final Listener listener;
    private final LinearLayout root, content, card, actions;
    private final TextView version, network, resourceState, stageLabel, heading, message;
    private final TextView task, percentage, amount, speed, notes, resourceVersion, technicalDetails, footer;
    private final ProgressBar progress;
    private final Button errorToggle, pause, resume, retry, downloadUpdate, installUpdate, enterGame;
    private Model model;
    private boolean detailsExpanded;
    private String lastError = "";

    public BootstrapScreen(Activity activity, Listener listener) {
        if (activity == null || listener == null) throw new IllegalArgumentException("Activity and listener are required");
        this.activity = activity; this.listener = listener;
        try { activity.requestWindowFeature(Window.FEATURE_NO_TITLE); }
        catch (RuntimeException contentAlreadyInstalled) { /* A themed ActionBar can still be hidden below. */ }
        ActionBar actionBar = activity.getActionBar();
        if (actionBar != null) actionBar.hide();
        Window window = activity.getWindow();
        window.clearFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN);
        window.setDecorFitsSystemWindows(false);
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.WHITE);
        WindowInsetsController controller = window.getInsetsController();
        if (controller != null) {
            controller.show(WindowInsets.Type.systemBars());
            controller.setSystemBarsAppearance(WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS,
                    WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS
                            | WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
        }
        root = column();
        root.setBackground(new GradientDrawable(GradientDrawable.Orientation.TOP_BOTTOM,
                new int[] {Color.rgb(229, 240, 255), Color.rgb(248, 251, 255), Color.WHITE}));
        root.setOnApplyWindowInsetsListener(new View.OnApplyWindowInsetsListener() {
            @Override public WindowInsets onApplyWindowInsets(View view, WindowInsets insets) {
                Insets bars = insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout());
                root.setPadding(bars.left, bars.top, bars.right, bars.bottom);
                return WindowInsets.CONSUMED;
            }
        });

        ScrollView scroll = new ScrollView(activity);
        scroll.setFillViewport(true);
        scroll.setClipToPadding(false);
        scroll.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        scroll.setVerticalScrollBarEnabled(true);
        root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1f));
        content = column();
        content.setPadding(dp(22), dp(24), dp(22), dp(24));
        scroll.addView(content, new ScrollView.LayoutParams(-1, -2));

        TextView brand = text("Fut", 32, BLUE, true);
        content.addView(brand, margin(0));
        version = text("", 16, MUTED, false);
        content.addView(version, margin(6));
        network = text("", 16, BLUE, false);
        network.setPadding(dp(12), dp(7), dp(12), dp(7));
        network.setBackground(shape(Color.WHITE, 10, LINE));
        LinearLayout.LayoutParams networkLayout = margin(16);
        networkLayout.width = -2;
        content.addView(network, networkLayout);

        card = column();
        card.setPadding(dp(20), dp(22), dp(20), dp(22));
        card.setBackground(shape(Color.WHITE, 22, LINE));
        card.setElevation(dp(2));
        content.addView(card, margin(22));
        stageLabel = text("", 16, BLUE, true);
        card.addView(stageLabel, margin(0));
        heading = text("", 28, INK, true);
        heading.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
        card.addView(heading, margin(12));
        message = text("", 18, MUTED, false);
        card.addView(message, margin(12));
        task = text("", 16, MUTED, true);
        card.addView(task, margin(24));
        percentage = text("", 52, BLUE, true);
        card.addView(percentage, margin(8));
        progress = new ProgressBar(activity, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(1000);
        progress.setProgressTintList(ColorStateList.valueOf(BLUE));
        progress.setProgressBackgroundTintList(ColorStateList.valueOf(LINE));
        progress.setIndeterminateTintList(ColorStateList.valueOf(BLUE));
        LinearLayout.LayoutParams progressLayout = new LinearLayout.LayoutParams(-1, dp(9));
        progressLayout.topMargin = dp(14);
        card.addView(progress, progressLayout);
        amount = text("", 18, INK, true);
        card.addView(amount, margin(12));
        speed = text("", 16, MUTED, false);
        card.addView(speed, margin(10));
        resourceState = text("", 16, MUTED, false);
        card.addView(resourceState, margin(20));

        notes = text("", 18, MUTED, false);
        notes.setTextIsSelectable(true);
        content.addView(notes, margin(24));
        resourceVersion = text("", 16, MUTED, false);
        resourceVersion.setTextIsSelectable(true);
        content.addView(resourceVersion, margin(16));
        TextView projectNote = text("资源托管在 GitHub。下载慢或失败时，请更换可用、稳定的梯子（网络代理）和更快的网络后重试；下载速度仍取决于实际连接。资源完整后可离线游玩。\n问题反馈：lzyuan666666@gmail.com", 18, MUTED, false);
        content.addView(projectNote, margin(18));
        TextView provenance = text("Fut 由 Doruk Çavdar 维护。项目包含经许可使用的上游工作，来源说明见项目仓库。", 16, MUTED, false);
        android.text.SpannableString creditedSource = new android.text.SpannableString(provenance.getText());
        int authorStart = creditedSource.toString().indexOf("7othree");
        creditedSource.setSpan(new android.text.style.StyleSpan(Typeface.BOLD), authorStart, authorStart + 7,
                android.text.Spanned.SPAN_EXCLUSIVE_EXCLUSIVE);
        provenance.setText(creditedSource);
        content.addView(provenance, margin(12));
        TextView projectAddress = text("项目地址：https://github.com/VeriBotOfficial/Fut\n更新公告请进入游戏，点击右上角通知图标查看。", 18, MUTED, false);
        projectAddress.setTextIsSelectable(true);
        android.text.util.Linkify.addLinks(projectAddress, android.text.util.Linkify.WEB_URLS);
        content.addView(projectAddress, margin(16));
        errorToggle = button("查看错误详情");
        styleButton(errorToggle, false);
        errorToggle.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View view) {
                detailsExpanded = !detailsExpanded;
                updateErrorDetails();
            }
        });
        content.addView(errorToggle, margin(18));
        technicalDetails = text("", 16, MUTED, false);
        technicalDetails.setTypeface(Typeface.MONOSPACE);
        technicalDetails.setTextIsSelectable(true);
        technicalDetails.setPadding(dp(16), dp(16), dp(16), dp(16));
        technicalDetails.setBackground(shape(PALE, 12, LINE));
        content.addView(technicalDetails, margin(10));
        TextView scrollHint = text("上下滑动可查看完整信息", 16, MUTED, false);
        scrollHint.setGravity(Gravity.CENTER);
        content.addView(scrollHint, margin(24));

        // This region is outside ScrollView, so no scrollable text can be hidden behind a button.
        actions = column();
        actions.setPadding(dp(22), dp(14), dp(22), dp(16));
        actions.setBackgroundColor(Color.WHITE);
        actions.setElevation(dp(4));
        root.addView(actions, new LinearLayout.LayoutParams(-1, -2));
        enterGame = action("进入游戏", new Runnable() {
            @Override public void run() { if (model != null && model.canEnterGame) BootstrapScreen.this.listener.onEnterGame(); }
        });
        installUpdate = action("安装应用更新", new Runnable() {
            @Override public void run() { if (model != null && model.canInstallUpdate) BootstrapScreen.this.listener.onInstallUpdate(); }
        });
        downloadUpdate = action("下载应用更新", new Runnable() {
            @Override public void run() { if (model != null && model.canDownloadUpdate) BootstrapScreen.this.listener.onDownloadUpdate(); }
        });
        resume = action("继续下载", new Runnable() {
            @Override public void run() { if (model != null && model.canResume) BootstrapScreen.this.listener.onResume(); }
        });
        retry = action("重试", new Runnable() {
            @Override public void run() { if (model != null && model.canRetry) BootstrapScreen.this.listener.onRetry(); }
        });
        pause = action("暂停下载", new Runnable() {
            @Override public void run() { if (model != null && model.canPause) BootstrapScreen.this.listener.onPause(); }
        });
        footer = text("", 16, MUTED, false);
        footer.setGravity(Gravity.CENTER);
        actions.addView(footer, margin(12));
        activity.setContentView(root);
        root.requestApplyInsets();
        render(new Model.Builder(Stage.CHECKING).build());
    }

    public boolean alive() { return !activity.isFinishing() && !activity.isDestroyed(); }

    public void render(Model next) {
        if (!alive() || next == null) return;
        model = next;
        version.setText("应用 " + (next.versionName.isEmpty() ? "—" : next.versionName)
                + "  ·  构建 " + (next.buildRevision > 0 ? String.valueOf(next.buildRevision) : "—"));
        network.setText(next.online ? "网络已连接" : "当前离线");
        setTextIfChanged(stageLabel, stageName(next.stage));
        setTextIfChanged(heading, next.title.isEmpty() ? defaultTitle(next.stage) : next.title);
        setTextIfChanged(message, next.message.isEmpty() ? defaultMessage(next.stage) : next.message);
        resourceState.setText(next.resourcesReady ? "本地资源：完整可用"
                : next.stage == Stage.ERROR ? "本地资源：尚未确认完整"
                : next.stage == Stage.PAUSED ? "本地资源：准备未完成"
                : next.stage == Stage.CHECKING || next.stage == Stage.VERIFYING ? "本地资源：正在确认"
                : "本地资源：准备中");
        resourceState.setTextColor(next.resourcesReady ? Color.rgb(40, 117, 82) : MUTED);
        boolean active = next.stage == Stage.CHECKING || next.stage == Stage.VERIFYING
                || next.stage == Stage.DOWNLOADING || next.stage == Stage.DEPLOYING;
        boolean showProgress = active || (next.stage == Stage.PAUSED && next.total > 0);
        task.setVisibility(showProgress ? View.VISIBLE : View.GONE);
        task.setText(next.taskLabel.isEmpty() ? stageName(next.stage) + "进度" : next.taskLabel);
        progress.setVisibility(showProgress ? View.VISIBLE : View.GONE);
        amount.setVisibility(showProgress ? View.VISIBLE : View.GONE);
        percentage.setVisibility(showProgress && next.total > 0 ? View.VISIBLE : View.GONE);
        progress.setIndeterminate(active && next.total <= 0);
        if (next.total > 0) {
            double ratio = Math.min(1d, (double) next.done / (double) next.total);
            progress.setProgress((int) (ratio * 1000d));
            String percent = String.format(Locale.SIMPLIFIED_CHINESE, "%.1f%%", ratio * 100d);
            percentage.setText(percent);
            if (next.unit == ProgressUnit.BYTES)
                amount.setText(bytes(next.done) + " / " + bytes(next.total));
            else if (next.unit == ProgressUnit.FILES)
                amount.setText(next.done + " / " + next.total + " 个文件");
            else amount.setText(percent);
            progress.setContentDescription(task.getText() + "，" + amount.getText());
        } else {
            amount.setText("正在处理，请稍候");
            progress.setContentDescription(task.getText() + "，总量待确认");
        }
        boolean showSpeed = next.stage == Stage.DOWNLOADING && next.bytesPerSecond > 0;
        speed.setVisibility(showSpeed ? View.VISIBLE : View.GONE);
        if (showSpeed) speed.setText("下载速度  " + bytes((long) next.bytesPerSecond) + "/s");
        notes.setText(next.details);
        notes.setVisibility(next.details.isEmpty() ? View.GONE : View.VISIBLE);
        resourceVersion.setText("资源版本  " + next.resourceVersion);
        resourceVersion.setVisibility(next.resourceVersion.isEmpty() ? View.GONE : View.VISIBLE);
        if (!next.errorDetails.equals(lastError)) { lastError = next.errorDetails; detailsExpanded = false; }
        technicalDetails.setText(next.errorDetails);
        updateErrorDetails();

        enterGame.setText(next.enterGameLabel.isEmpty() ? "进入游戏" : next.enterGameLabel);
        pause.setText(next.stage == Stage.DOWNLOADING ? "暂停下载" : next.stage == Stage.VERIFYING ? "暂停校验" : "暂停准备");
        resume.setText(next.title.contains("更新") ? "继续更新" : "继续准备");
        Button[] buttons = { enterGame, installUpdate, downloadUpdate, resume, retry, pause };
        boolean[] allowed = { next.canEnterGame, next.canInstallUpdate, next.canDownloadUpdate,
                next.canResume, next.canRetry, next.canPause };
        boolean hasButton = false;
        for (int index = 0; index < buttons.length; index++) {
            Button button = buttons[index];
            button.setVisibility(allowed[index] ? View.VISIBLE : View.GONE);
            if (allowed[index]) {
                styleButton(button, !hasButton);
                LinearLayout.LayoutParams params = (LinearLayout.LayoutParams) button.getLayoutParams();
                int topMargin = hasButton ? dp(10) : 0;
                if (params.topMargin != topMargin) {
                    params.topMargin = topMargin;
                    button.setLayoutParams(params);
                }
                hasButton = true;
            }
        }
        String footerText = next.stage == Stage.PAUSED ? "已下载的内容会保留"
                : next.canInstallUpdate ? "请在系统安装界面确认更新"
                : next.canPause ? "暂停后会保留已完成内容"
                : next.resourcesReady && !next.online ? "使用本地资源，无需等待联网"
                : "";
        footer.setText(footerText);
        footer.setVisibility(footerText.isEmpty() ? View.GONE : View.VISIBLE);
        actions.setVisibility(hasButton ? View.VISIBLE : View.GONE);
    }

    private void updateErrorDetails() {
        boolean available = model != null && !model.errorDetails.isEmpty();
        errorToggle.setVisibility(available ? View.VISIBLE : View.GONE);
        errorToggle.setText(detailsExpanded ? "收起错误详情" : "查看错误详情");
        errorToggle.setContentDescription(detailsExpanded ? "收起错误详情，当前已展开" : "查看错误详情，当前已收起");
        technicalDetails.setVisibility(available && detailsExpanded ? View.VISIBLE : View.GONE);
    }

    private Button action(String label, final Runnable callback) {
        Button button = button(label);
        button.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View view) { if (alive()) callback.run(); }
        });
        actions.addView(button, margin(10));
        return button;
    }

    private Button button(String label) {
        Button button = new Button(activity);
        button.setText(label);
        button.setAllCaps(false);
        button.setSingleLine(false);
        button.setTextSize(20);
        button.setTypeface(Typeface.DEFAULT);
        button.setGravity(Gravity.CENTER);
        button.setMinHeight(dp(60));
        button.setMinimumHeight(dp(60));
        button.setPadding(dp(16), dp(12), dp(16), dp(12));
        button.setStateListAnimator(null);
        return button;
    }

    private void styleButton(Button button, boolean primary) {
        if (Boolean.valueOf(primary).equals(button.getTag())) return;
        button.setTag(Boolean.valueOf(primary));
        button.setTextColor(primary ? Color.WHITE : BLUE);
        button.setBackgroundTintList(null);
        GradientDrawable background = shape(primary ? BLUE : Color.WHITE, 14, primary ? BLUE : LINE);
        GradientDrawable mask = shape(Color.WHITE, 14, Color.WHITE);
        button.setBackground(new RippleDrawable(ColorStateList.valueOf(primary
                ? Color.argb(45, 255, 255, 255) : Color.argb(24, 39, 101, 220)), background, mask));
    }

    private LinearLayout column() {
        LinearLayout layout = new LinearLayout(activity);
        layout.setOrientation(LinearLayout.VERTICAL);
        return layout;
    }

    private TextView text(String value, float size, int color, boolean medium) {
        TextView view = new TextView(activity);
        view.setText(value); view.setTextSize(size); view.setTextColor(color);
        view.setTypeface(Typeface.DEFAULT, medium ? Typeface.BOLD : Typeface.NORMAL);
        view.setLineSpacing(dp(3), 1f);
        view.setBreakStrategy(1);
        view.setHyphenationFrequency(0);
        return view;
    }

    private GradientDrawable shape(int color, int radius, int stroke) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color); drawable.setCornerRadius(dp(radius)); drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private LinearLayout.LayoutParams margin(int top) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(-1, -2);
        params.topMargin = dp(top); return params;
    }

    private int dp(int value) { return (int) (value * activity.getResources().getDisplayMetrics().density + 0.5f); }
    private static String clean(String value) { return value == null ? "" : value.trim(); }
    private static void setTextIfChanged(TextView view, String value) {
        if (!value.contentEquals(view.getText())) view.setText(value);
    }
    private static String bytes(long value) {
        if (value >= 1000000000L) return String.format(Locale.SIMPLIFIED_CHINESE, "%.2f GB", value / 1e9);
        if (value >= 1000000L) return String.format(Locale.SIMPLIFIED_CHINESE, "%.1f MB", value / 1e6);
        if (value >= 1000L) return String.format(Locale.SIMPLIFIED_CHINESE, "%.1f KB", value / 1e3);
        return value + " B";
    }

    private static String stageName(Stage stage) {
        switch (stage) {
            case VERIFYING: return "本地校验";
            case DOWNLOADING: return "正在下载";
            case DEPLOYING: return "正在部署";
            case READY: return "准备完成";
            case PAUSED: return "已暂停";
            case ERROR: return "需要处理";
            case UPDATE_AVAILABLE: return "应用更新";
            case INSTALL_READY: return "等待安装";
            default: return "检查状态";
        }
    }

    private static String defaultTitle(Stage stage) {
        switch (stage) {
            case VERIFYING: return "检查本地资源";
            case DOWNLOADING: return "正在下载资源";
            case DEPLOYING: return "正在准备游戏资源";
            case READY: return "准备好，开场吧";
            case PAUSED: return "下载已暂停";
            case ERROR: return "准备暂时中断";
            case UPDATE_AVAILABLE: return "有新的应用版本";
            case INSTALL_READY: return "更新已下载完成";
            default: return "正在检查资源状态";
        }
    }

    private static String defaultMessage(Stage stage) {
        switch (stage) {
            case VERIFYING: return "正在读取本地文件，不消耗流量。";
            case DOWNLOADING: return "已下载的完整内容会保留。";
            case DEPLOYING: return "正在整理本地资源，请稍候。";
            case READY: return "所需资源已准备完整，可以进入游戏。";
            case PAUSED: return "已保存下载进度，可继续完成准备。";
            case ERROR: return "暂时无法完成准备，请查看提示后重试。";
            case UPDATE_AVAILABLE: return "可以先进入游戏，稍后再更新应用。";
            case INSTALL_READY: return "安装时请在系统界面确认更新。";
            default: return "正在读取已安装资源与版本信息。";
        }
    }
}
