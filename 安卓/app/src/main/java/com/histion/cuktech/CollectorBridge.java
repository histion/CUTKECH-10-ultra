package com.histion.cuktech;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothAdapter;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothManager;
import android.bluetooth.le.ScanCallback;
import android.bluetooth.le.ScanFilter;
import android.bluetooth.le.ScanResult;
import android.bluetooth.le.ScanSettings;
import android.content.Context;
import android.os.Build;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;

/**
 * 采集器：BLE 扫描 → 连接 → 米家认证 → 加密会话 → 定时轮询 + 命令通道
 * （对齐 pyapp/collector_bridge.py + collector.py 的 link_loop / run_command）。
 *
 * 与桌面版的区别只是"采集器"跑在同一进程的一条线程里（安卓没有子进程），
 * 对外的状态机、日志文案、ack 结构完全一样。
 */
final class CollectorBridge implements AutoA.Bridge {
    private static final long REQ_TIMEOUT_MS = 10000;
    private static final long AUTH_TIMEOUT_S = 15;

    private final Context ctx;
    private final AppCore app;
    private final AppState state;
    private final File dataDir;
    private final LinkedBlockingQueue<Cmd> cmds = new LinkedBlockingQueue<Cmd>();

    private volatile Thread thread;
    private volatile boolean shouldRun;
    private volatile GattIo io;
    private volatile MiSession session;
    private volatile MiAuth auth;
    private volatile boolean sweeping;

    private static final class Cmd {
        final JSONObject body;
        final long timeoutMs;
        final CountDownLatch latch = new CountDownLatch(1);
        JSONObject result;

        Cmd(JSONObject body, long timeoutMs) { this.body = body; this.timeoutMs = timeoutMs; }
    }

    CollectorBridge(Context ctx, AppCore app) {
        this.ctx = ctx.getApplicationContext();
        this.app = app;
        this.state = app.state;
        this.dataDir = app.dataDir;
    }

    private void log(String msg) {
        state.pushLog(msg);
        state.fileLog(msg);
    }

    File tokenFile() { return new File(dataDir, "cuktech.token"); }

    boolean isRunning() { Thread t = thread; return t != null && t.isAlive(); }

    // ------------------------------------------------------------------ 启停

    synchronized void start() {
        if (isRunning()) return;
        if (!tokenFile().exists()) {
            state.setLink("need-login", "尚未登录小米云，请先扫码授权", 0, null);
            return;
        }
        String address = app.effectiveAddress();
        if (address.isEmpty()) {
            state.setLink("need-login", "还没有设备地址，请登录小米云自动识别充电头", 0, null);
            return;
        }
        shouldRun = true;
        state.collecting = true;
        state.lastSpawnAt = System.currentTimeMillis();
        thread = new Thread(new Runnable() {
            @Override public void run() { loop(); }
        }, "collector");
        thread.setDaemon(true);
        thread.start();
    }

    synchronized void stop() {
        shouldRun = false;
        state.collecting = false;
        state.writable = new JSONArray();
        state.switches = new JSONArray();
        rejectPending("采集器已停止，命令未送达");
        closeLink();
        Thread t = thread;
        thread = null;
        if (t != null) t.interrupt();
    }

    void restartSoon() {
        stop();
        new Thread(new Runnable() {
            @Override public void run() {
                try { Thread.sleep(600); } catch (InterruptedException ignored) { }
                Thread t = thread;
                if (t == null || !t.isAlive()) start();
            }
        }, "col-restart").start();
    }

    private void closeLink() {
        try { if (session != null) session.unsubscribe(); } catch (Exception ignored) { }
        try { if (auth != null) auth.unsubscribe(); } catch (Exception ignored) { }
        try { if (io != null) io.close(); } catch (Exception ignored) { }
        session = null; auth = null; io = null;
    }

    private void rejectPending(String reason) {
        Cmd c;
        while ((c = cmds.poll()) != null) {
            c.result = Control.err(reason);
            c.latch.countDown();
        }
    }

    // ------------------------------------------------------------------ 主循环

    private void loop() {
        int attempt = 0;
        long reqTimeout = REQ_TIMEOUT_MS;
        while (shouldRun) {
            attempt++;
            GattIo localIo = null;
            try {
                String address = app.effectiveAddress();
                String tokenHex = readTokenHex();
                if (tokenHex == null) {
                    state.setLink("need-login", "token 读取失败，请重新登录小米云", 0, null);
                    return;
                }
                byte[] token = Util.unhex(tokenHex);

                BluetoothManager bmm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
                if (bmm == null || bmm.getAdapter() == null || !bmm.getAdapter().isEnabled()) {
                    throw new java.io.IOException("蓝牙未开启，请在系统设置里打开蓝牙后再试");
                }

                state.setLink("scanning", "正在搜索充电器 " + address, attempt, null);
                log("链路：scanning — 正在搜索充电器 " + address);
                Hit hit = scan(address, 45);
                BluetoothDevice dev = null;
                Integer rssi = null;
                if (hit != null) { dev = hit.dev; rssi = hit.rssi; }
                if (dev == null) {
                    BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
                    if (bm != null && bm.getAdapter() != null) {
                        try { dev = bm.getAdapter().getRemoteDevice(address); } catch (Exception ignored) { }
                    }
                }
                if (dev == null) {
                    throw new java.io.IOException("没有发现该蓝牙地址；充电器是否已通电？米家 App 是否占用了连接？");
                }

                state.setLink("connecting", "已发现，正在建立 BLE 连接", attempt, rssi);
                log("链路：connecting — 已发现，正在建立 BLE 连接");

                localIo = new GattIo();
                io = localIo;
                if (!localIo.connect(ctx, dev, 30000)) {
                    throw new java.io.IOException(localIo.lastError() != null ? localIo.lastError() : "BLE 连接失败");
                }

                state.setLink("auth", "正在完成 MiOT 认证", attempt, rssi);
                MiAuth localAuth = null;
                MiAuth.Keys keys = null;
                Throwable last = null;
                for (int i = 0; i < 4; i++) {
                    localAuth = new MiAuth(localIo, AUTH_TIMEOUT_S);
                    try {
                        localAuth.subscribe(false);
                        localAuth.greet();
                        localAuth.subscribeUpnp();
                        keys = localAuth.login(token);
                        break;
                    } catch (Throwable t) {
                        last = t;
                        log("登录尝试 " + (i + 1) + " 失败：" + t.getMessage());
                        try { localAuth.unsubscribe(); } catch (Exception ignored) { }
                        try { Thread.sleep(1500); } catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
                    }
                }
                if (keys == null) throw last != null ? new java.io.IOException(String.valueOf(last.getMessage())) : new java.io.IOException("认证失败");
                auth = localAuth;

                MiSession localSession = new MiSession(localIo, localAuth, keys, reqTimeout / 1000.0);
                localSession.subscribe();
                session = localSession;
                attempt = 0;
                state.setLink("connected", "已连接并认证", 0, rssi);
                log("链路：connected — 已连接并认证");
                state.writable = Miot.writableSchema();
                state.switches = Miot.switchesSchema();

                pollLoop(localSession, rssi);
                if (!shouldRun) break;
                throw new java.io.IOException("采集循环结束，准备重连");
            } catch (Throwable t) {
                if (!shouldRun) break;
                closeLink();
                int back = Math.min(30, 2 * Math.min(attempt, 10));
                state.setLink("reconnecting", t.getMessage() + "（" + back + "s 后重试）", attempt, null);
                log("链路：reconnecting — " + t.getMessage() + "（" + back + "s 后重试）");
                try { Thread.sleep(back * 1000); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            }
        }
        closeLink();
        state.collecting = false;
    }

    private void pollLoop(MiSession s, Integer rssi) throws Exception {
        while (shouldRun) {
            if (sweeping) { Thread.sleep(300); continue; }
            long t0 = System.currentTimeMillis();
            List<Miot.Item> items = readFull(s);
            JSONObject st = Miot.buildState(items, rssi, s.rxDuplicates(), s.rxOutOfOrder());
            state.latest = st;
            state.restarts = 0;
            JSONObject link = state.snapshotLink();
            if (!"connected".equals(link.optString("state"))) {
                state.setLink("connected", "已连接", 0, rssi);
            } else if (rssi != null) {
                try { link.put("rssi", rssi.intValue()); state.link = link; } catch (Exception ignored) { }
            }
            state.pushHistory(st);
            if (app.autoA != null && app.config.autoA()) app.autoA.requestEvaluate();

            double interval = app.config.interval();
            long spent = System.currentTimeMillis() - t0;
            long wait = Math.max(200, (long) (interval * 1000) - spent);
            Cmd cmd = cmds.poll(wait, TimeUnit.MILLISECONDS);
            if (cmd != null) execute(s, cmd);
        }
    }

    private void execute(MiSession s, Cmd cmd) {
        try {
            cmd.result = runCommand(s, cmd.body);
        } catch (Throwable t) {
            cmd.result = Control.err(String.valueOf(t.getMessage()));
        }
        cmd.latch.countDown();
    }

    // ------------------------------------------------------------------ 命令

    @Override
    public JSONObject sendCommand(JSONObject cmd, long timeoutMs) {
        if (!isRunning()) return Control.err("采集器未运行，无法下发控制命令");
        JSONObject link = state.snapshotLink();
        if (!"connected".equals(link.optString("state"))) {
            return Control.err("蓝牙尚未连接（当前：" + link.optString("state") + "）");
        }
        Cmd c = new Cmd(cmd, timeoutMs);
        cmds.add(c);
        try {
            if (c.latch.await(timeoutMs, TimeUnit.MILLISECONDS) && c.result != null) return c.result;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        return Control.err("设备响应超时");
    }

    private List<Miot.Item> readFull(MiSession s) throws Exception {
        byte[] req = Miot.encodeGetProperties(s.nextSequence(), Miot.DEFAULT_QUERY);
        byte[] resp = s.sendRequest(req);
        return Miot.parseResponse(resp);
    }

    private Map<Integer, Miot.Item> readFullMap(MiSession s) throws Exception {
        Map<Integer, Miot.Item> map = new HashMap<Integer, Miot.Item>();
        for (Miot.Item it : readFull(s)) if (it.siid == 2) map.put(it.piid, it);
        return map;
    }

    private JSONObject runCommand(MiSession s, JSONObject cmd) throws Exception {
        String kind = cmd.optString("cmd");
        JSONObject ack = new JSONObject();
        ack.put("t", "ack");
        if (cmd.has("id")) ack.put("id", cmd.get("id"));
        ack.put("cmd", kind);

        if ("ping".equals(kind) || "read".equals(kind)) {
            ack.put("ok", true);
            return ack;
        }
        if ("ext-read".equals(kind)) {
            try {
                byte[] req = Miot.encodeGetProperties(s.nextSequence(), Miot.alt15(0x07, 0x08));
                List<Miot.Item> items = Miot.parseResponse(s.sendRequest(req));
                JSONObject values = new JSONObject();
                for (Miot.Item it : items) if (it.siid == 2) values.put("2." + Util.pad2(it.piid), it.value);
                ack.put("ok", true);
                ack.put("values", values);
                ack.put("note", "用 15 项替换形状读到的扩展属性（本机只有 2.8 是新的）");
                return ack;
            } catch (Exception e) {
                ack.put("ok", false);
                ack.put("error", e.getClass().getSimpleName() + "：" + e.getMessage());
                return ack;
            }
        }
        if ("reset".equals(kind)) {
            try {
                byte[] raw = s.sendRequest(Miot.encodeAction(s.nextSequence(), 2, 1));
                ack.put("ok", true);
                ack.put("note", "已调用「恢复默认设置」，回帧 " + Util.hex(raw));
                return ack;
            } catch (Exception e) {
                ack.put("ok", false);
                ack.put("error", e.getClass().getSimpleName() + "：" + e.getMessage());
                return ack;
            }
        }
        if (!"set".equals(kind)) {
            ack.put("ok", false);
            ack.put("error", "未知命令 " + kind);
            return ack;
        }

        Integer piid = cmd.has("piid") ? cmd.optInt("piid") : null;
        Object valueObj = cmd.opt("value");
        Object[] spec = null;
        for (Object[] row : Miot.WRITABLE) {
            if (((Integer) row[0]).intValue() == (piid == null ? -1 : piid.intValue())) { spec = row; break; }
        }
        if (spec == null) {
            ack.put("ok", false);
            ack.put("error", "属性 2." + piid + " 不在可写清单内");
            return ack;
        }
        String name = (String) spec[1];
        String width = (String) spec[2];
        long lo = ((Number) spec[3]).longValue();
        long hi = ((Number) spec[4]).longValue();
        Integer value = Control.asInt(valueObj);
        ack.put("piid", piid.intValue());
        ack.put("name", name);
        if (value == null) {
            ack.put("ok", false);
            ack.put("error", "value 必须是整数");
            return ack;
        }
        if (value < lo || value > hi) {
            ack.put("ok", false);
            ack.put("error", name + " 取值需在 " + lo + ".." + hi + " 之间");
            return ack;
        }

        try {
            s.sendCommand(Miot.encodeSetProperty(s.nextSequence(), 2, piid, value,
                    "u32".equals(width), "u16".equals(width)), 0.6);
        } catch (Exception e) {
            ack.put("ok", false);
            ack.put("value", value.intValue());
            ack.put("error", e.getClass().getSimpleName() + "：" + e.getMessage());
            return ack;
        }

        boolean inFull = false;
        for (int[] t : Miot.DEFAULT_QUERY) if (t[1] == piid) inFull = true;
        if (!inFull) {
            ack.put("ok", true);
            ack.put("value", value.intValue());
            ack.put("readback", JSONObject.NULL);
            ack.put("note", "该属性不在标准 15 项里，本机无法回读验证");
            return ack;
        }
        Long readback;
        try {
            Miot.Item it = readFullMap(s).get(piid);
            readback = it == null ? null : it.value;
        } catch (Exception e) {
            ack.put("ok", false);
            ack.put("value", value.intValue());
            ack.put("readback", JSONObject.NULL);
            ack.put("error", "写入已发出，但回读失败：" + e.getMessage());
            return ack;
        }
        boolean ok = readback != null && readback.longValue() == value.longValue();
        ack.put("ok", ok);
        ack.put("value", value.intValue());
        ack.put("readback", readback == null ? JSONObject.NULL : readback.longValue());
        ack.put("error", ok ? JSONObject.NULL : "写入已确认，但回读为 " + readback + "（可能被设备规整过）");
        return ack;
    }

    // ------------------------------------------------------------------ 扫描

    private static final class Hit {
        BluetoothDevice dev;
        int rssi;
    }

    @SuppressLint("MissingPermission")
    private Hit scan(String address, int timeoutSec) {
        BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
        if (bm == null) return null;
        BluetoothAdapter adapter = bm.getAdapter();
        if (adapter == null || !adapter.isEnabled()) return null;
        BluetoothLeScannerHolder holder = new BluetoothLeScannerHolder(adapter);
        if (holder.scanner == null) return null;

        final Hit[] hit = new Hit[1];
        final CountDownLatch latch = new CountDownLatch(1);
        ScanCallback cb = new ScanCallback() {
            @Override public void onScanResult(int callbackType, ScanResult result) {
                BluetoothDevice d = result.getDevice();
                if (d != null && address.equalsIgnoreCase(d.getAddress())) {
                    Hit h = new Hit();
                    h.dev = d;
                    h.rssi = result.getRssi();
                    hit[0] = h;
                    latch.countDown();
                }
            }

            @Override public void onScanFailed(int errorCode) {
                latch.countDown();
            }
        };
        try {
            List<ScanFilter> filters = Collections.singletonList(
                    new ScanFilter.Builder().setDeviceAddress(address).build());
            ScanSettings settings = new ScanSettings.Builder()
                    .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build();
            holder.scanner.startScan(filters, settings, cb);
            long deadline = System.currentTimeMillis() + timeoutSec * 1000L;
            while (hit[0] == null && System.currentTimeMillis() < deadline) {
                if (latch.await(2, TimeUnit.SECONDS)) break;
                if (!shouldRun) break;
            }
            holder.scanner.stopScan(cb);
        } catch (Throwable t) {
            try { holder.scanner.stopScan(cb); } catch (Exception ignored) { }
        }
        return hit[0];
    }

    private static final class BluetoothLeScannerHolder {
        final android.bluetooth.le.BluetoothLeScanner scanner;

        BluetoothLeScannerHolder(BluetoothAdapter adapter) {
            android.bluetooth.le.BluetoothLeScanner s = null;
            try { s = adapter.getBluetoothLeScanner(); } catch (Throwable ignored) { }
            scanner = s;
        }
    }

    private String readTokenHex() {
        try {
            byte[] b = new byte[(int) tokenFile().length()];
            java.io.FileInputStream in = new java.io.FileInputStream(tokenFile());
            int n = in.read(b);
            in.close();
            JSONObject o = new JSONObject(new String(b, 0, n, "UTF-8"));
            return o.optString("token_hex", null);
        } catch (Exception e) {
            return null;
        }
    }

    // ------------------------------------------------------------------ 隐藏属性扫描

    /** 扫描 siid=2 的未知属性 + 枚举 GATT（对齐 collector.py 的 sweep_hidden）。 */
    JSONObject runSweep(int lo, int hi) {
        if (!tokenFile().exists()) return Control.err("尚未登录小米云，无法认证");
        sweeping = true;
        boolean resume = isRunning();
        if (resume) {
            log("扫描前暂停采集器（蓝牙同一时间只允许一个连接）");
            stop();
            try { Thread.sleep(1000); } catch (InterruptedException ignored) { }
        }
        log("开始扫描 siid=2 隐藏属性并枚举 GATT，约 20-40 秒…");
        JSONObject out;
        GattIo localIo = null;
        try {
            String address = app.effectiveAddress();
            String tokenHex = readTokenHex();
            byte[] token = Util.unhex(tokenHex);
            Hit hit = scan(address, 30);
            BluetoothDevice dev = hit != null ? hit.dev : null;
            if (dev == null) {
                BluetoothManager bm = (BluetoothManager) ctx.getSystemService(Context.BLUETOOTH_SERVICE);
                if (bm != null && bm.getAdapter() != null) dev = bm.getAdapter().getRemoteDevice(address);
            }
            if (dev == null) throw new java.io.IOException("没有发现充电器");
            localIo = new GattIo();
            if (!localIo.connect(ctx, dev, 30000)) throw new java.io.IOException("BLE 连接失败");
            MiAuth a = new MiAuth(localIo, AUTH_TIMEOUT_S);
            a.subscribe(false); a.greet(); a.subscribeUpnp();
            MiAuth.Keys keys = a.login(token);
            MiSession s = new MiSession(localIo, a, keys, 10);
            s.subscribe();
            out = sweepInner(localIo, s, lo, hi);
            s.unsubscribe();
            a.unsubscribe();
        } catch (Throwable t) {
            out = Control.err(String.valueOf(t.getMessage()));
        } finally {
            sweeping = false;
            if (localIo != null) localIo.close();
            if (resume) {
                new Thread(new Runnable() {
                    @Override public void run() {
                        try { Thread.sleep(500); } catch (InterruptedException ignored) { }
                        start();
                    }
                }).start();
            }
        }
        if (out.optBoolean("ok", false)) {
            try { out.put("at", System.currentTimeMillis()); state.sweep = new JSONObject(out.toString()); } catch (Exception ignored) { }
            log("扫描完成：2.0x" + Integer.toHexString(lo) + "..2.0x" + Integer.toHexString(hi)
                    + " 共 " + out.optJSONArray("rows").length() + " 个 piid，设备可读 "
                    + out.optJSONArray("found").length() + " 个");
        } else {
            log("扫描失败：" + out.optString("error", "未知原因"));
        }
        return out;
    }

    private JSONObject sweepInner(GattIo io, MiSession s, int lo, int hi) throws Exception {
        JSONArray rows = new JSONArray();
        JSONArray found = new JSONArray();
        JSONObject control = new JSONObject();
        JSONArray attempts = new JSONArray();
        control.put("ok", false);
        control.put("items", 0);
        control.put("attempts", attempts);
        int dropped = 0;

        int[][] base = Miot.DEFAULT_QUERY;
        java.util.Set<Integer> basePiids = new java.util.HashSet<Integer>();
        for (int[] t : base) basePiids.add(t[1]);

        int[][][] controls = new int[][][]{
                base,
                append(base, 0x16),
                new int[][]{{2, 1}, {2, 2}, {2, 0x10}},
        };
        String[] labels = {"full", "full+1", "three"};
        for (int i = 0; i < controls.length; i++) {
            JSONObject a = new JSONObject();
            a.put("label", labels[i]);
            a.put("sent", controls[i].length);
            try {
                List<Miot.Item> items = Miot.parseResponse(
                        s.sendRequest(Miot.encodeGetProperties(0x001B, controls[i])));
                a.put("got", items.size());
                JSONArray extra = new JSONArray();
                JSONObject extraStatus = new JSONObject();
                for (Miot.Item it : items) {
                    if (!basePiids.contains(it.piid)) {
                        extra.put(it.piid);
                        extraStatus.put("2." + it.piid, it.status);
                    }
                }
                a.put("extra_echoed", extra);
                a.put("extra_status", extraStatus);
                if ("full".equals(labels[i]) && !control.optBoolean("ok", false)) {
                    control.put("ok", true);
                    control.put("items", items.size());
                }
            } catch (Exception e) {
                a.put("got", 0);
                a.put("error", e.getClass().getSimpleName() + ": " + e.getMessage());
            }
            attempts.put(a);
            Thread.sleep(250);
        }

        int batch = 6;
        for (int start = lo; start <= hi; start += batch) {
            List<Integer> pids = new ArrayList<Integer>();
            for (int p = start; p <= Math.min(hi, start + batch - 1); p++) pids.add(p);
            int[][] tuples = base;
            for (Integer p : pids) tuples = append(tuples, p);
            try {
                List<Miot.Item> items = Miot.parseResponse(
                        s.sendRequest(Miot.encodeGetProperties(0x001C + (start - lo) / batch, tuples)));
                Map<Integer, Miot.Item> byPiid = new HashMap<Integer, Miot.Item>();
                for (Miot.Item it : items) if (it.siid == 2) byPiid.put(it.piid, it);
                for (Integer p : pids) {
                    Miot.Item it = byPiid.get(p);
                    if (it == null) {
                        JSONObject r = new JSONObject();
                        r.put("piid", p.intValue());
                        r.put("status", JSONObject.NULL);
                        r.put("note", "设备已应答但表里没有它 —— 即该属性不存在");
                        rows.put(r);
                        continue;
                    }
                    JSONObject r = new JSONObject();
                    r.put("piid", it.piid);
                    r.put("type", it.type);
                    r.put("value", it.value);
                    r.put("status", it.status);
                    r.put("raw", it.raw);
                    rows.put(r);
                    if (it.status == 0) found.put(r);
                }
            } catch (Exception e) {
                dropped++;
                for (Integer p : pids) {
                    JSONObject r = new JSONObject();
                    r.put("piid", p.intValue());
                    r.put("status", JSONObject.NULL);
                    r.put("note", "整批未应答（" + e.getClass().getSimpleName() + "）");
                    rows.put(r);
                }
                Thread.sleep(300);
                continue;
            }
            Thread.sleep(120);
        }

        JSONArray services = new JSONArray();
        JSONArray vendor = new JSONArray();
        for (BluetoothGattService svc : io.services()) {
            JSONObject entry = new JSONObject();
            entry.put("uuid", svc.getUuid().toString());
            entry.put("description", svc.getUuid().toString());
            JSONArray chars = new JSONArray();
            for (BluetoothGattCharacteristic c : svc.getCharacteristics()) {
                JSONObject ch = new JSONObject();
                ch.put("uuid", c.getUuid().toString());
                JSONArray props = new JSONArray();
                if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_READ) != 0) props.put("read");
                if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE) != 0) props.put("write");
                if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE) != 0) props.put("write-without-response");
                if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_NOTIFY) != 0) props.put("notify");
                if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_INDICATE) != 0) props.put("indicate");
                ch.put("props", props);
                chars.put(ch);
            }
            entry.put("chars", chars);
            services.put(entry);

            if (svc.getUuid().toString().toLowerCase().startsWith("0000af")) {
                for (BluetoothGattCharacteristic c : svc.getCharacteristics()) {
                    JSONObject ch = new JSONObject();
                    ch.put("uuid", c.getUuid().toString());
                    ch.put("props", "notify/read");
                    ch.put("read", JSONObject.NULL);
                    if ((c.getProperties() & BluetoothGattCharacteristic.PROPERTY_READ) != 0) {
                        try {
                            ch.put("read", Util.hex(io.readChar(c)));
                        } catch (Exception e) {
                            ch.put("read", "ERR " + e.getClass().getSimpleName() + ": " + e.getMessage());
                        }
                    }
                    vendor.put(ch);
                }
            }
        }

        JSONObject out = new JSONObject();
        out.put("t", "sweep");
        out.put("ok", true);
        out.put("lo", lo);
        out.put("hi", hi);
        out.put("batch", batch);
        int answered = 0, missing = 0;
        for (int i = 0; i < rows.length(); i++) {
            JSONObject r = rows.optJSONObject(i);
            if (r != null && !r.isNull("status")) answered++;
            if (r != null && r.isNull("status") && String.valueOf(r.opt("note")).contains("不存在")) missing++;
        }
        out.put("answered", answered);
        out.put("missing", missing);
        out.put("dropped", dropped);
        out.put("control", control);
        out.put("rows", rows);
        out.put("found", found);
        out.put("services", services);
        out.put("vendor", vendor);
        out.put("notify", new JSONArray());
        return out;
    }

    private static int[][] append(int[][] base, int piid) {
        int[][] out = new int[base.length + 1][];
        System.arraycopy(base, 0, out, 0, base.length);
        out[base.length] = new int[]{2, piid};
        return out;
    }
}
