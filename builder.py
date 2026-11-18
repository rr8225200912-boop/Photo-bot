import asyncio
import hashlib
import re
import shutil
import zipfile
from pathlib import Path


class BuildError(Exception):
    pass


# ---- Android WebView shell (written to disk at build time) ----
TEMPLATE = {
    "settings.gradle": """pluginManagement {
    repositories { google(); mavenCentral(); gradlePluginPortal() }
}
dependencyResolutionManagement {
    repositories { google(); mavenCentral() }
}
rootProject.name = 'ApkShell'
include ':app'
""",
    "build.gradle": "plugins { id 'com.android.application' version '8.3.2' apply false }\n",
    "gradle.properties": "org.gradle.jvmargs=-Xmx1024m -Dfile.encoding=UTF-8\norg.gradle.daemon=false\n",
    "app/build.gradle": """plugins { id 'com.android.application' }
android {
    namespace 'com.apkbot.shell'
    compileSdk 34
    defaultConfig {
        applicationId "__APPID__"
        minSdk 24
        targetSdk 34
        versionCode 1
        versionName "1.0"
    }
    compileOptions {
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }
}
""",
    "app/src/main/AndroidManifest.xml": """<?xml version="1.0" encoding="utf-8"?>
<manifest xmlns:android="http://schemas.android.com/apk/res/android">
    <uses-permission android:name="android.permission.INTERNET"/>
    <application
        android:label="@string/app_name"
        __ICON__
        android:allowBackup="true"
        android:theme="@android:style/Theme.Material.NoActionBar">
        <activity
            android:name=".MainActivity"
            android:exported="true"
            android:configChanges="orientation|screenSize|keyboardHidden">
            <intent-filter>
                <action android:name="android.intent.action.MAIN"/>
                <category android:name="android.intent.category.LAUNCHER"/>
            </intent-filter>
        </activity>
    </application>
</manifest>
""",
    "app/src/main/java/com/apkbot/shell/MainActivity.java": """package com.apkbot.shell;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.webkit.JsResult;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

public class MainActivity extends Activity {
    private WebView w;

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        int bar = getColor(R.color.bar);
        getWindow().setStatusBarColor(bar);
        getWindow().setNavigationBarColor(bar);
        w = new WebView(this);
        setContentView(w);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        w.setWebViewClient(new WebViewClient());
        w.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onJsAlert(WebView v, String u, String m, final JsResult r) {
                new AlertDialog.Builder(MainActivity.this).setMessage(m)
                    .setPositiveButton("OK", (d, i) -> r.confirm())
                    .setOnCancelListener(d -> r.cancel()).show();
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView v, String u, String m, final JsResult r) {
                new AlertDialog.Builder(MainActivity.this).setMessage(m)
                    .setPositiveButton("OK", (d, i) -> r.confirm())
                    .setNegativeButton("Cancel", (d, i) -> r.cancel())
                    .setOnCancelListener(d -> r.cancel()).show();
                return true;
            }
        });
        w.loadUrl("file:///android_asset/www/index.html");
    }

    @Override
    public void onBackPressed() {
        if (w.canGoBack()) w.goBack(); else super.onBackPressed();
    }
}
""",
    "app/src/main/res/values/strings.xml": '<resources><string name="app_name">__APPNAME__</string></resources>\n',
    "app/src/main/res/values/colors.xml": '<resources><color name="bar">__BAR__</color></resources>\n',
}


def _xml_text(s):
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("'", "\\'").replace('"', '\\"')
    if s[:1] in "@?":
        s = "\\" + s
    return s


def _make_icon(w, name, key):
    try:
        from PIL import Image, ImageDraw, ImageFont
        cols = ["#e8b04a", "#4a90d9", "#86c291", "#e5735c", "#9b7fd4", "#3fb8af"]
        im = Image.new("RGBA", (192, 192), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.ellipse((6, 6, 186, 186), fill=cols[int(key, 16) % len(cols)])
        ch = next((c for c in name if c.isascii() and c.isalpha()), "A").upper()
        d.text((96, 96), ch, fill="#1a1a1a", font=ImageFont.load_default(size=100), anchor="mm")
        out = w / "app/src/main/res/mipmap-xxxhdpi"
        out.mkdir(parents=True, exist_ok=True)
        im.save(out / "ic_launcher.png")
        return True
    except Exception:
        return False


def prepare_project(w, name, web_dir):
    """Create the WebView Android project in w, with web_dir as the app content."""
    h = hashlib.md5(name.encode()).hexdigest()
    alnum = re.sub(r"[^a-z0-9]", "", name.lower()) or "app"
    if alnum[0].isdigit():
        alnum = "a" + alnum
    app_id = "com.apkbot." + alnum[:20] + h[:4]
    bar = "#111111"
    try:
        m = re.search(r"<meta[^>]+name=[\"']theme-color[\"'][^>]+content=[\"'](#[0-9a-fA-F]{6})",
                      (web_dir / "index.html").read_text(errors="ignore"))
        if m:
            bar = m.group(1)
    except Exception:
        pass
    icon = _make_icon(w, name, h)
    for rel, text in TEMPLATE.items():
        text = (text.replace("__APPID__", app_id)
                    .replace("__APPNAME__", _xml_text(name))
                    .replace("__BAR__", bar)
                    .replace("__ICON__", 'android:icon="@mipmap/ic_launcher"' if icon else ""))
        p = w / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    www = w / "app/src/main/assets/www"
    shutil.copytree(web_dir, www)


def _gradle_root(base):
    c = [p for p in base.rglob("settings.gradle*") if "__MACOSX" not in p.parts]
    return min(c, key=lambda p: len(p.parts)).parent if c else None


def _find_index(base):
    c = [p for p in base.rglob("index.html") if "__MACOSX" not in p.parts]
    return min(c, key=lambda p: len(p.parts)).parent if c else None


def _tail(out):
    i = out.find("What went wrong")
    chunk = out[i:] if i >= 0 else "\n".join(out.strip().splitlines()[-30:])
    return chunk[:2500]


async def _run(cmd, cwd, timeout=1500):
    p = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
    except asyncio.TimeoutError:
        p.kill()
        raise BuildError("বিল্ডে অনেক বেশি সময় লেগে গেছে, থামিয়ে দিয়েছি।")
    return p.returncode, out.decode(errors="replace")


async def build_apk(zip_path, name, tmp):
    tmp = Path(tmp)
    src = tmp / "src"
    src.mkdir()
    with zipfile.ZipFile(zip_path) as z:
        if sum(i.file_size for i in z.infolist()) > 300 * 1024 * 1024:
            raise BuildError("zip ফাইলটা অনেক বড়।")
        z.extractall(src)
    root = _gradle_root(src)
    if root is None:
        web = _find_index(src)
        if web is None:
            raise BuildError("zip-এর ভেতরে index.html বা Android Gradle প্রজেক্ট পাইনি।")
        root = tmp / "proj"
        prepare_project(root, name, web)
    wrapper = (root / "gradlew").exists() and (root / "gradle/wrapper/gradle-wrapper.jar").exists()
    if wrapper:
        (root / "gradlew").chmod(0o755)
    cmd = ["./gradlew"] if wrapper else ["gradle"]
    code, out = await _run(cmd + ["assembleDebug", "--no-daemon", "--console=plain"], root)
    if code != 0:
        raise BuildError(_tail(out))
    apks = [p for p in root.rglob("*.apk") if "unaligned" not in p.name]
    if not apks:
        raise BuildError("বিল্ড শেষ হয়েছে কিন্তু APK পাইনি।")
    apks.sort(key=lambda p: ("debug" in p.name, p.stat().st_mtime))
    return apks[-1]
