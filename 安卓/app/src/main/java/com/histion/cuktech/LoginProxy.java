package com.histion.cuktech;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.List;

/**
 * 扫码登录代理（对齐 pyapp/login_proxy.py）：
 * ``start`` 同步申请二维码并存成本地 png；``poll`` 受理即返回，实际等待在后台线程里跑，
 * 成功后自动在账号设备列表里认出 AD1204U、保存 12 字节 BLE 密钥并重启采集器。
 */
final class LoginProxy {
    private static final long POLL_WAIT_S = 280;
    private static final String[] TERMINAL = {
            "code=", "missing location", "serviceToken", "redirect failed",
            "HTTP 400", "HTTP 403", "HTTP 404",
    };

    private final AppCore app;
    private final Object lock = new Object();
    private volatile boolean running;
    private volatile String kind;
    private volatile JSONObject result;
    private XiaomiCloud.QRLogin qr;

    LoginProxy(AppCore app) { this.app = app; }

    JSONObject current() {
        JSONObject o = new JSONObject();
        try {
            o.put("running", running);
            o.put("kind", kind == null ? JSONObject.NULL : kind);
            o.put("result", result == null ? JSONObject.NULL : result);
        } catch (Exception ignored) { }
        return o;
    }

    // ------------------------------------------------------------------ 二维码

    JSONObject start() {
        synchronized (lock) {
            if (running) return Control.err("已有登录任务在进行");
            running = true;
            kind = "qr";
            result = null;
        }
        app.log("开始申请登录二维码…");
        try {
            XiaomiCloud.QRLogin login = XiaomiCloud.startQrLogin();
            qr = login;
            boolean pngOk = false;
            try {
                byte[] blob = XiaomiCloud.fetch(login.qrImageUrl, login.cookies, 30000);
                if (blob.length > 200) {
                    File f = qrFile();
                    FileOutputStream fos = new FileOutputStream(f, false);
                    fos.write(blob);
                    fos.close();
                    pngOk = true;
                }
            } catch (Exception e) {
                app.log("二维码图片下载失败：" + e.getMessage());
            }
            if (!pngOk) {
                synchronized (lock) { running = false; kind = null; }
                return Control.err("二维码图片下载失败");
            }
            synchronized (lock) { running = false; kind = null; }
            app.log("二维码已生成，等待扫码（" + login.timeout + "s 内有效）");
            JSONObject o = new JSONObject();
            o.put("ok", true);
            o.put("timeout", login.timeout);
            o.put("ts", System.currentTimeMillis());
            return o;
        } catch (Exception e) {
            synchronized (lock) { running = false; kind = null; }
            String msg = e.getMessage();
            app.log("申请二维码失败：" + msg);
            return Control.err(msg);
        }
    }

    File qrFile() { return new File(app.dataDir, "qr.png"); }

    // ------------------------------------------------------------------ 等待授权

    JSONObject poll() {
        synchronized (lock) {
            if (running) return Control.err("已有登录任务在进行");
            running = true;
            kind = "poll";
            result = null;
        }
        final XiaomiCloud.QRLogin login = qr;
        if (login == null) {
            synchronized (lock) { running = false; kind = null; }
            return Control.err("还没有生成二维码，请先点「生成二维码」");
        }
        final String want = app.effectiveAddress();
        new Thread(new Runnable() {
            @Override public void run() { waitForScan(login, want); }
        }, "login-poll").start();
        app.log("已开始等待扫码授权…");
        JSONObject o = new JSONObject();
        try { o.put("ok", true); } catch (Exception ignored) { }
        return o;
    }

    private void waitForScan(XiaomiCloud.QRLogin login, String want) {
        long deadline = System.currentTimeMillis() + POLL_WAIT_S * 1000;
        XiaomiCloud.CloudAuth auth = null;
        String lastErr = null;
        while (System.currentTimeMillis() < deadline) {
            try {
                auth = XiaomiCloud.pollQrLogin(login);
                break;
            } catch (XiaomiCloud.Pending p) {
                try { Thread.sleep(1000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
            } catch (XiaomiCloud.CloudError e) {
                lastErr = e.getMessage();
                if (isTerminal(lastErr)) break;
                app.log("登录：" + lastErr);
                try { Thread.sleep(2000); } catch (InterruptedException ie) { Thread.currentThread().interrupt(); return; }
            }
        }
        if (auth == null) {
            finish(null, lastErr == null ? "二维码登录超时（" + POLL_WAIT_S + "s）" : lastErr);
            return;
        }
        app.log("登录 OK user_id=" + auth.userId);

        boolean auto = want.isEmpty() || "auto".equalsIgnoreCase(want);
        String regionHit = null, macHit = null, tokenHit = null;
        StringBuilder failures = new StringBuilder();
        int listed = 0;
        for (String region : XiaomiCloud.REGIONS) {
            List<JSONObject> devices;
            try {
                devices = XiaomiCloud.listDevices(auth, region);
                listed++;
            } catch (Exception e) {
                String msg = String.valueOf(e.getMessage());
                app.log("  [" + region + "] list failed: " + msg);
                if (failures.length() == 0) failures.append(region).append("：").append(msg);
                continue;
            }
            app.log("  [" + region + "] " + devices.size() + " device(s)");
            if (auto) {
                int matched = 0;
                for (JSONObject dev : devices) {
                    if (!XiaomiCloud.looksLikeAd1204(dev)) continue;
                    matched++;
                    String mac = dev.optString("mac", "").trim();
                    String token = XiaomiCloud.validToken(dev.optString("token", ""));
                    app.log("  candidate: " + dev.optString("name") + " " + dev.optString("model")
                            + " mac=" + mac + " token_ok=" + (token != null));
                    if (token == null) continue;
                    if (tokenHit == null) { regionHit = region; macHit = mac; tokenHit = token; }
                }
                if (matched == 0 && !devices.isEmpty()) {
                    // 方便排查：把账号里前几台设备的型号打出来（不含密钥）
                    StringBuilder sb = new StringBuilder();
                    for (int i = 0; i < Math.min(5, devices.size()); i++) {
                        if (sb.length() > 0) sb.append(" / ");
                        sb.append(devices.get(i).optString("model", "?"))
                                .append("(").append(devices.get(i).optString("name", "?")).append(")");
                    }
                    app.log("  [" + region + "] 没有匹配到 AD1204U，账号前几台设备：" + sb);
                }
            } else {
                try {
                    String token = XiaomiCloud.findTokenByMac(devices, want);
                    if (token != null) { regionHit = region; macHit = want; tokenHit = token; }
                } catch (Exception e) {
                    app.log("  [" + region + "] token problem: " + e.getMessage());
                }
            }
            if (tokenHit != null) break;
        }

        if (tokenHit == null) {
            // 一个区都没拉成功时，真正的失败原因比"没找到设备"有用得多 —— 直接回传给用户。
            if (listed == 0 && failures.length() > 0) {
                finish(null, "拉取设备列表失败（" + failures + "）");
                return;
            }
            String tail = failures.length() > 0 ? "（部分区域失败：" + failures + "）" : "";
            finish(null, (auto
                    ? "账号里没有找到酷态科 AD1204U —— 请确认已用米家 App 添加这台充电头、并且它插着电在线"
                    : "账号里没有找到蓝牙地址为 " + want + " 的设备") + tail);
            return;
        }

        try {
            JSONObject tok = new JSONObject();
            tok.put("address", macHit.toUpperCase());
            tok.put("token_hex", tokenHit);
            tok.put("region", regionHit);
            File f = new File(app.dataDir, "cuktech.token");
            FileOutputStream fos = new FileOutputStream(f, false);
            fos.write(tok.toString().getBytes("UTF-8"));
            fos.close();
        } catch (Exception e) {
            finish(null, "保存 BLE 密钥失败：" + e.getMessage());
            return;
        }

        JSONObject res = new JSONObject();
        try {
            res.put("ok", true);
            res.put("region", regionHit);
            res.put("tokenLen", tokenHit.length());
        } catch (Exception ignored) { }

        String current = app.effectiveAddress();
        if (macHit != null && !macHit.equalsIgnoreCase(current)) {
            app.setAddress(macHit.toUpperCase());
            app.log("已记住设备地址 " + macHit.toUpperCase());
        }
        app.log("登录成功，已保存 BLE 密钥");
        finish(res, null);
        if (app.bridge != null) app.bridge.restartSoon();
    }

    private void finish(JSONObject res, String error) {
        synchronized (lock) {
            running = false;
            kind = null;
            if (res != null) result = res;
            else {
                result = new JSONObject();
                try {
                    result.put("ok", false);
                    result.put("error", error == null ? "登录失败" : error);
                } catch (Exception ignored) { }
                app.log("登录失败：" + (error == null ? "" : error));
            }
        }
    }

    private static boolean isTerminal(String msg) {
        if (msg == null) return false;
        for (String t : TERMINAL) if (msg.contains(t)) return true;
        return false;
    }
}
