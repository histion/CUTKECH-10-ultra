package com.histion.cuktech;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URLDecoder;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 只监听 127.0.0.1 的本地 HTTP 服务（对齐 pyapp/http_server.py 的路由与 JSON 结构）。
 *
 * 用本地 socket 而不是 JS Bridge，是为了让 assets/index.html 一行不改地复用桌面版
 * 的 fetch('/api/...') 调用。
 */
final class ApiServer {
    private final AppCore app;
    private final Object lock = new Object();
    private ServerSocket server;
    private Thread accept;
    private volatile boolean running;
    private int port;

    ApiServer(AppCore app) { this.app = app; }

    int port() { return port; }

    void start() {
        synchronized (lock) {
            if (running) return;
            try {
                server = new ServerSocket(0, 32, InetAddress.getByName("127.0.0.1"));
                port = server.getLocalPort();
                running = true;
                accept = new Thread(new Runnable() {
                    @Override public void run() { loop(); }
                }, "api-server");
                accept.setDaemon(true);
                accept.start();
            } catch (Exception e) {
                app.log("本地服务启动失败：" + e.getMessage());
            }
        }
    }

    void stop() {
        synchronized (lock) {
            running = false;
            try { if (server != null) server.close(); } catch (Exception ignored) { }
            server = null;
        }
    }

    private void loop() {
        while (running) {
            try {
                final Socket s = server.accept();
                Thread t = new Thread(new Runnable() {
                    @Override public void run() { handle(s); }
                }, "api-conn");
                t.setDaemon(true);
                t.start();
            } catch (Exception ignored) {
                if (!running) break;
            }
        }
    }

    private void handle(Socket s) {
        try {
            s.setSoTimeout(60000);
            InputStream in = s.getInputStream();
            String line = readLine(in);
            if (line == null) { s.close(); return; }
            String[] parts = line.split(" ");
            String method = parts[0];
            String target = parts.length > 1 ? parts[1] : "/";

            int contentLength = 0;
            while (true) {
                String h = readLine(in);
                if (h == null || h.isEmpty()) break;
                if (h.toLowerCase().startsWith("content-length:")) {
                    try { contentLength = Integer.parseInt(h.substring(h.indexOf(':') + 1).trim()); } catch (Exception ignored) { }
                }
            }
            String rawBody = "";
            if (contentLength > 0) {
                byte[] buf = new byte[contentLength];
                int off = 0;
                while (off < contentLength) {
                    int n = in.read(buf, off, contentLength - off);
                    if (n < 0) break;
                    off += n;
                }
                rawBody = new String(buf, 0, off, "UTF-8");
            }

            int q = target.indexOf('?');
            String path = q >= 0 ? target.substring(0, q) : target;
            String query = q >= 0 ? target.substring(q + 1) : "";
            Map<String, String> queryMap = parseQuery(query);
            JSONObject body = null;
            if (rawBody.length() > 0) {
                try { body = new JSONObject(rawBody); } catch (Exception ignored) { }
            }

            byte[] payload;
            String ctype = "application/json; charset=utf-8";
            if ("/api/login/qr.png".equals(path)) {
                java.io.File f = new java.io.File(app.dataDir, "qr.png");
                try {
                    byte[] b = new byte[(int) f.length()];
                    java.io.FileInputStream fis = new java.io.FileInputStream(f);
                    int n = fis.read(b);
                    fis.close();
                    payload = n > 0 ? java.util.Arrays.copyOf(b, n) : new byte[0];
                    ctype = "image/png";
                } catch (Exception e) {
                    payload = new byte[0];
                    ctype = "image/png";
                }
            } else if (path.startsWith("/api/")) {
                JSONObject res = route(method, path, queryMap, body);
                payload = res.toString().getBytes("UTF-8");
            } else {
                if ("/".equals(path) || "/index.html".equals(path)) {
                    payload = readAsset("index.html");
                    ctype = "text/html; charset=utf-8";
                } else {
                    try {
                        payload = readAsset(path.replaceFirst("^/", ""));
                        ctype = guessType(path);
                    } catch (Exception e) {
                        payload = "404".getBytes("UTF-8");
                        ctype = "text/plain; charset=utf-8";
                    }
                }
            }

            OutputStream out = s.getOutputStream();
            StringBuilder head = new StringBuilder();
            head.append("HTTP/1.1 200 OK\r\n");
            head.append("Content-Type: ").append(ctype).append("\r\n");
            head.append("Content-Length: ").append(payload.length).append("\r\n");
            head.append("Cache-Control: no-store\r\n");
            head.append("Connection: close\r\n\r\n");
            out.write(head.toString().getBytes("UTF-8"));
            out.write(payload);
            out.flush();
            s.close();
        } catch (Exception ignored) {
            try { s.close(); } catch (Exception ignored2) { }
        }
    }

    private static String readLine(InputStream in) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\n') break;
            if (c != '\r') bos.write(c);
        }
        if (bos.size() == 0 && c < 0) return null;
        return new String(bos.toByteArray(), "UTF-8");
    }

    private static Map<String, String> parseQuery(String q) {
        Map<String, String> out = new LinkedHashMap<String, String>();
        if (q == null || q.isEmpty()) return out;
        for (String pair : q.split("&")) {
            int eq = pair.indexOf('=');
            if (eq <= 0) continue;
            try {
                out.put(URLDecoder.decode(pair.substring(0, eq), "UTF-8"),
                        URLDecoder.decode(pair.substring(eq + 1), "UTF-8"));
            } catch (Exception ignored) { }
        }
        return out;
    }

    private byte[] readAsset(String name) throws Exception {
        InputStream is = app.ctx.getAssets().open(name);
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        is.close();
        return bos.toByteArray();
    }

    private static String guessType(String path) {
        String p = path.toLowerCase();
        if (p.endsWith(".js")) return "text/javascript; charset=utf-8";
        if (p.endsWith(".css")) return "text/css; charset=utf-8";
        if (p.endsWith(".png")) return "image/png";
        if (p.endsWith(".svg")) return "image/svg+xml";
        return "application/octet-stream";
    }

    // ------------------------------------------------------------------ 路由

    JSONObject route(String method, String path, Map<String, String> query, JSONObject body) {
        try {
            if ("GET".equals(method)) {
                if ("/api/ping".equals(path)) {
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("v", AppCore.VERSION);
                    return o;
                }
                if ("/api/state".equals(path)) return app.buildStatePayload();
                if ("/api/history".equals(path)) {
                    int sec = clamp(query.get("seconds"), 300, 10, 3600);
                    long since = System.currentTimeMillis() - sec * 1000L;
                    List<JSONObject> rows = app.state.historySince(since);
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("rows", AppState.rowsToArray(rows));
                    return o;
                }
                if ("/api/log".equals(path)) {
                    List<String> lines = app.state.tailLogs(200);
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("lines", AppState.toJsonArray(lines));
                    return o;
                }
                if ("/api/energy".equals(path)) {
                    int days = clamp(query.get("days"), 7, 1, 90);
                    return app.energy.snapshot(days);
                }
                JSONObject o = new JSONObject();
                o.put("ok", false);
                o.put("error", "未知接口 " + path);
                return o;
            }

            if ("POST".equals(method)) {
                if ("/api/login/start".equals(path)) return app.login.start();
                if ("/api/login/poll".equals(path)) return app.login.poll();
                if ("/api/reconnect".equals(path)) {
                    app.log("手动重连…");
                    if (app.bridge != null) app.bridge.restartSoon();
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    return o;
                }
                if ("/api/shortcut".equals(path)) {
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    o.put("message", "安卓端已固定显示在启动器里，无需再建快捷方式");
                    return o;
                }
                if ("/api/control".equals(path)) {
                    if (app.bridge == null) return Control.err("采集器未启动");
                    return Control.handleControl(app.bridge, app.state, body);
                }
                if ("/api/sweep".equals(path)) {
                    if (app.bridge == null) return Control.err("采集器未启动");
                    int lo = int0(query.get("lo"), 0x16);
                    int hi = int0(query.get("hi"), 0x40);
                    return app.bridge.runSweep(lo, hi);
                }
                if ("/api/config".equals(path)) return app.applyConfig(body == null ? new JSONObject() : body);
                if ("/api/show".equals(path)) {
                    if (MainActivity.instance != null) MainActivity.instance.moveToFront();
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    return o;
                }
                if ("/api/quit".equals(path)) {
                    app.log("收到退出请求");
                    new Thread(new Runnable() {
                        @Override public void run() {
                            try { Thread.sleep(200); } catch (InterruptedException ignored) { }
                            if (CoreService.instance != null) CoreService.instance.requestStop();
                        }
                    }).start();
                    JSONObject o = new JSONObject();
                    o.put("ok", true);
                    return o;
                }
            }
            JSONObject o = new JSONObject();
            o.put("ok", false);
            o.put("error", "未知接口 " + path);
            return o;
        } catch (Throwable t) {
            JSONObject o = new JSONObject();
            try {
                o.put("ok", false);
                o.put("error", String.valueOf(t.getMessage()));
            } catch (Exception ignored) { }
            return o;
        }
    }

    private static int clamp(String v, int dflt, int lo, int hi) {
        int n;
        try {
            n = Integer.parseInt(String.valueOf(v).trim());
        } catch (Exception e) {
            n = dflt;
        }
        return Math.max(lo, Math.min(hi, n));
    }

    /** 模拟 JS ``parseInt(x, 0)``（支持 0x 前缀）。 */
    private static int int0(String v, int dflt) {
        if (v == null) return dflt;
        String s = v.trim();
        if (s.isEmpty()) return dflt;
        try {
            if (s.toLowerCase().startsWith("0x")) return (int) Long.parseLong(s.substring(2), 16);
            if (s.toLowerCase().startsWith("-0x")) return -(int) Long.parseLong(s.substring(3), 16);
            return Integer.parseInt(s);
        } catch (Exception e) {
            java.util.regex.Matcher m = java.util.regex.Pattern.compile("^[+-]?\\d+").matcher(s);
            if (m.find()) {
                try { return Integer.parseInt(m.group()); } catch (Exception ignored) { }
            }
            return dflt;
        }
    }
}
