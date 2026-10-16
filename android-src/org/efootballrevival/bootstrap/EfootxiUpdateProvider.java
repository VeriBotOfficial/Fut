package org.efootballrevival.bootstrap;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.content.pm.ProviderInfo;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;

/** Grants the system installer read access to exactly one private, verified update APK. */
public final class EfootxiUpdateProvider extends ContentProvider {
    @Override public void attachInfo(Context context, ProviderInfo info) {
        if (info.exported || !info.grantUriPermissions
                || !(context.getPackageName() + ApkUpdater.AUTHORITY_SUFFIX).equals(info.authority))
            throw new SecurityException("更新文件提供程序配置无效");
        super.attachInfo(context, info);
    }

    @Override public boolean onCreate() { return true; }

    @Override public String getType(Uri uri) {
        validateUri(uri);
        return ApkUpdater.MIME;
    }

    @Override public Cursor query(Uri uri, String[] projection, String selection,
            String[] selectionArgs, String sortOrder) {
        File file;
        try { file = fileFor(uri); }
        catch (FileNotFoundException error) { throw new IllegalArgumentException(error.getMessage(), error); }
        String[] columns = projection == null
                ? new String[] { OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE } : projection;
        Object[] values = new Object[columns.length];
        for (int index = 0; index < columns.length; index++) {
            if (OpenableColumns.DISPLAY_NAME.equals(columns[index])) values[index] = "efootxi.apk";
            else if (OpenableColumns.SIZE.equals(columns[index])) values[index] = file.length();
        }
        MatrixCursor result = new MatrixCursor(columns, 1);
        result.addRow(values);
        return result;
    }

    @Override public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        if (!"r".equals(mode)) throw new SecurityException("更新文件只允许读取");
        return ParcelFileDescriptor.open(fileFor(uri), ParcelFileDescriptor.MODE_READ_ONLY);
    }

    @Override public Uri insert(Uri uri, ContentValues values) {
        throw new UnsupportedOperationException("更新文件只允许读取");
    }

    @Override public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("更新文件只允许读取");
    }

    @Override public int delete(Uri uri, String selection, String[] selectionArgs) {
        throw new UnsupportedOperationException("更新文件只允许读取");
    }

    private void validateUri(Uri uri) {
        if (uri == null || !"content".equals(uri.getScheme())
                || !(getContext().getPackageName() + ApkUpdater.AUTHORITY_SUFFIX).equals(uri.getAuthority())
                || !ApkUpdater.URI_PATH.equals(uri.getEncodedPath())
                || uri.getQuery() != null || uri.getFragment() != null)
            throw new SecurityException("无权读取此文件");
    }

    private File fileFor(Uri uri) throws FileNotFoundException {
        validateUri(uri);
        try {
            File file = ApkUpdater.updateFile(getContext());
            if (!file.isFile()) throw new FileNotFoundException("更新文件不存在");
            return file;
        } catch (IOException error) {
            FileNotFoundException missing = new FileNotFoundException("无法读取更新文件");
            missing.initCause(error);
            throw missing;
        }
    }
}
