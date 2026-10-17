package org.efootballrevival.bootstrap;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.util.Log;
import android.widget.Toast;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Proxy;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import org.json.JSONArray;
import org.json.JSONObject;

/** Repairs shipped support files only; never copies over an existing save/WAL. */
public final class GatewayGuard {
    private static boolean checked;
    private GatewayGuard() { }

    public static synchronized void prepare(Context context) throws Exception {
        if (checked) return;
        File root = new File(context.getFilesDir(), "embedded-gateway");
        // Fresh installations are installed by the original gateway atomically.
        if (!new File(root,"installed-release.txt").isFile()) return;
        JSONObject manifest;
        try (InputStream in=context.getAssets().open("gateway-release/release-manifest.json")) {
            manifest=new JSONObject(new String(read(in,4*1024*1024),StandardCharsets.UTF_8));
        }
        JSONArray files=manifest.getJSONArray("files");
        String prefix=root.getCanonicalPath()+File.separator;
        int repaired=0;
        for(int i=0;i<files.length();i++) {
            JSONObject item=files.getJSONObject(i); String relative=item.getString("path");
            // Persistent user data is exclusively handled by SQLite migrations.
            if(relative.equals("outputs/efootball/local-club.sqlite3")) continue;
            File target=new File(root,relative);
            if(!target.getCanonicalPath().startsWith(prefix)) throw new IllegalStateException("Unsafe support path");
            if(target.isFile() && target.length()==item.getLong("bytes")) continue;
            File parent=target.getParentFile();
            if(!parent.isDirectory() && !parent.mkdirs()) throw new IllegalStateException("无法创建本地资源目录");
            File temp=new File(parent,target.getName()+".repairing");
            MessageDigest digest=MessageDigest.getInstance("SHA-256");long size=0;
            try(InputStream input=context.getAssets().open("gateway-release/"+relative);FileOutputStream out=new FileOutputStream(temp)) {
                byte[] block=new byte[65536];int n;
                while((n=input.read(block))!=-1){out.write(block,0,n);digest.update(block,0,n);size+=n;}
                out.getFD().sync();
            }
            StringBuilder hex=new StringBuilder();for(byte value:digest.digest())hex.append(String.format(java.util.Locale.ROOT,"%02x",value&255));
            if(size!=item.getLong("bytes") || !hex.toString().equals(item.getString("sha256")))
                throw new IllegalStateException("本地支持文件校验失败："+relative);
            if(!temp.renameTo(target)) throw new IllegalStateException("本地支持文件写入失败："+relative);
            repaired++;
        }
        checked=true;
        Log.i("EFOOTXI_GATEWAY","support check complete; repaired="+repaired);
    }

    public static void health() throws Exception {
        HttpURLConnection c=(HttpURLConnection)new URL("http://127.0.0.1:"+port()+"/health").openConnection(Proxy.NO_PROXY);
        c.setConnectTimeout(3000);c.setReadTimeout(15000);c.setUseCaches(false);
        try {
            if(c.getResponseCode()!=200)throw new IllegalStateException("本地服务健康检查失败");
            try(InputStream in=c.getInputStream()) {new JSONObject(new String(read(in,65536),StandardCharsets.UTF_8));}
        } finally {c.disconnect();}
    }

    /** The OS binds a free loopback port; never probe or reuse another app's listener. */
    public static int port() throws Exception {
        Class<?> type=Class.forName("org.efootballrevival.runtimeprobe.EmbeddedGateway");
        java.lang.reflect.Field field=type.getDeclaredField("instance");field.setAccessible(true);
        Object instance=field.get(null);
        if(instance==null)throw new IllegalStateException("本地服务尚未启动");
        int value=((Number)type.getMethod("getListeningPort").invoke(instance)).intValue();
        if(value<1 || value>65535)throw new IllegalStateException("本地服务端口尚未就绪");
        return value;
    }

    private static byte[] read(InputStream in,int limit) throws Exception {
        ByteArrayOutputStream bytes=new ByteArrayOutputStream();byte[] block=new byte[8192];int n;
        while((n=in.read(block))!=-1){if(bytes.size()+n>limit)throw new IllegalStateException("Support response too large");bytes.write(block,0,n);}
        return bytes.toByteArray();
    }

    public static void showFailure(final Activity activity,Throwable error) {
        Throwable cause=error;for(int i=0;i<12&&cause.getCause()!=null;i++)cause=cause.getCause();
        final String report="efootxi 1.2 正式版\nAndroid "+android.os.Build.VERSION.RELEASE+" / "+android.os.Build.MANUFACTURER+" "+android.os.Build.MODEL+
                "\n"+Log.getStackTraceString(error);
        try(FileOutputStream out=new FileOutputStream(new File(activity.getFilesDir(),"efootxi-startup-error.txt"))){out.write(report.getBytes(StandardCharsets.UTF_8));out.getFD().sync();}
        catch(Exception ignored){ }
        Log.e("EFOOTXI_GATEWAY","startup failed; save retained",error);
        new AlertDialog.Builder(activity).setTitle("本地存档启动失败")
            .setMessage("已有存档已保留，请勿清除应用数据。\n\n"+cause.toString()+"\n\n请复制错误详情，发送至 "+FeedbackContact.EMAIL)
            .setPositiveButton("复制错误详情",new android.content.DialogInterface.OnClickListener(){public void onClick(android.content.DialogInterface dialog,int which){
                ((ClipboardManager)activity.getSystemService(Context.CLIPBOARD_SERVICE)).setPrimaryClip(ClipData.newPlainText("efootxi startup",report));
                Toast.makeText(activity,"错误详情已复制",Toast.LENGTH_LONG).show();activity.finish();
            }}).setNegativeButton("关闭",new android.content.DialogInterface.OnClickListener(){public void onClick(android.content.DialogInterface dialog,int which){activity.finish();}})
            .setNeutralButton("反馈邮箱",new android.content.DialogInterface.OnClickListener(){public void onClick(android.content.DialogInterface dialog,int which){FeedbackContact.open(activity);}}).setCancelable(false).show();
    }
}
