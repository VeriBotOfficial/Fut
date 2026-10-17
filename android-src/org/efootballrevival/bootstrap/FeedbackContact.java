package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.widget.Toast;

/** Explicit feedback action on the startup error screen; no game overlay. */
public final class FeedbackContact {
    public static final String EMAIL = "lzyuan666666@gmail.com";
    private FeedbackContact() { }

    public static void open(Activity activity) {
        try {
            activity.startActivity(new Intent(Intent.ACTION_SENDTO, Uri.parse(
                    "mailto:" + EMAIL + "?subject=" + Uri.encode("efootxi 1.2 正式版反馈"))));
        } catch (android.content.ActivityNotFoundException unavailable) {
            ClipboardManager clipboard = (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
            if (clipboard != null) clipboard.setPrimaryClip(ClipData.newPlainText("efootxi feedback", EMAIL));
            Toast.makeText(activity, "未找到邮件应用，反馈邮箱已复制：" + EMAIL, Toast.LENGTH_LONG).show();
        }
    }
}
