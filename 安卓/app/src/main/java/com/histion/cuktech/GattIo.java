package com.histion.cuktech;

import android.annotation.SuppressLint;
import android.bluetooth.BluetoothDevice;
import android.bluetooth.BluetoothGatt;
import android.bluetooth.BluetoothGattCallback;
import android.bluetooth.BluetoothGattCharacteristic;
import android.bluetooth.BluetoothGattDescriptor;
import android.bluetooth.BluetoothGattService;
import android.bluetooth.BluetoothProfile;
import android.content.Context;
import android.os.Build;
import android.util.Log;

import java.io.IOException;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * Android BLE GATT 的最小封装：串行的操作队列 + 通知分发 + MTU 协商。
 *
 * 为什么必须串行：Android 的 GATT 栈同一时刻只允许一个未完成的操作（写特征、写
 * 描述符、读特征、请求 MTU），并发调用会被静默丢弃或返回 false。所有操作都放进
 * 一条工作线程排队执行，调用方阻塞等回调（带超时），语义与 bleak 的 await 一致。
 */
final class GattIo {
    private static final String TAG = "cuktech-gatt";

    interface Sink { void onFrame(byte[] data); }

    private static final class Op {
        final int kind;                 // 0=write 1=read 2=descriptor 3=mtu 4=discover
        final BluetoothGattCharacteristic chr;
        final BluetoothGattDescriptor desc;
        final byte[] payload;
        final boolean withResponse;
        final int mtu;
        boolean done;
        int status = -1;
        byte[] value;

        Op(int kind, BluetoothGattCharacteristic chr, BluetoothGattDescriptor desc,
           byte[] payload, boolean withResponse, int mtu) {
            this.kind = kind; this.chr = chr; this.desc = desc; this.payload = payload;
            this.withResponse = withResponse; this.mtu = mtu;
        }
    }

    private BluetoothGatt gatt;
    private final LinkedBlockingQueue<Op> queue = new LinkedBlockingQueue<Op>();
    private final Map<BluetoothGattCharacteristic, Sink> sinks = new HashMap<BluetoothGattCharacteristic, Sink>();
    private Thread worker;
    private volatile boolean closed;
    private volatile Op current;
    private int mtu = 23;
    private volatile boolean connected;
    private volatile String lastError;
    /** 连接/发现/MTU 的一次性等待点。 */
    private final Object connLock = new Object();
    private boolean servicesReady;
    private int mtuStatus = -1;

    // ------------------------------------------------------------------ 连接

    @SuppressLint("MissingPermission")
    boolean connect(Context ctx, BluetoothDevice dev, int timeoutMs) {
        lastError = null;
        connected = false;
        servicesReady = false;
        mtuStatus = -1;
        closed = false;
        worker = new Thread(new Runnable() {
            @Override public void run() { loop(); }
        }, "gatt-io");
        worker.setDaemon(true);
        worker.start();

        if (Build.VERSION.SDK_INT >= 23) {
            gatt = dev.connectGatt(ctx, false, callback, BluetoothDevice.TRANSPORT_LE);
        } else {
            gatt = dev.connectGatt(ctx, false, callback);
        }
        if (gatt == null) { lastError = "connectGatt 返回 null"; return false; }

        long deadline = System.currentTimeMillis() + timeoutMs;
        synchronized (connLock) {
            while (!connected && System.currentTimeMillis() < deadline) {
                try { connLock.wait(200); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            }
        }
        if (!connected) { lastError = lastError != null ? lastError : "连接超时"; return false; }
        // 发现服务
        deadline = System.currentTimeMillis() + 15000;
        boolean ok = gatt.discoverServices();
        if (!ok) { lastError = "discoverServices 调用失败"; return false; }
        synchronized (connLock) {
            while (!servicesReady && System.currentTimeMillis() < deadline) {
                try { connLock.wait(200); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            }
        }
        // 协商 MTU（失败不强求，回落到 23 也能跑，只是分片多一点）
        try { requestMtu(517); } catch (Exception ignored) { }
        return true;
    }

    void close() {
        closed = true;
        try {
            if (gatt != null) { gatt.disconnect(); gatt.close(); }
        } catch (Exception ignored) { }
        gatt = null;
        if (worker != null) worker.interrupt();
        worker = null;
    }

    String lastError() { return lastError; }
    int mtu() { return mtu; }
    List<BluetoothGattService> services() {
        return gatt == null ? new ArrayList<BluetoothGattService>() : gatt.getServices();
    }

    // ------------------------------------------------------------------ 操作

    void write(String uuid, byte[] data, boolean withResponse) throws IOException {
        BluetoothGattCharacteristic c = chr(uuid);
        if (c == null) throw new IOException("找不到特征 " + uuid);
        submit(new Op(0, c, null, data, withResponse, 0));
    }

    void writeChar(BluetoothGattCharacteristic c, byte[] data, boolean withResponse) throws IOException {
        submit(new Op(0, c, null, data, withResponse, 0));
    }

    byte[] readChar(BluetoothGattCharacteristic c) throws IOException {
        if (c == null) throw new IOException("特征为空");
        Op op = new Op(1, c, null, null, true, 0);
        submit(op);
        return op.value == null ? new byte[0] : op.value;
    }

    void requestMtu(int want) throws IOException {
        submit(new Op(3, null, null, null, true, want));
        if (mtuStatus != 0) throw new IOException("MTU 协商失败 status=" + mtuStatus);
    }

    void enableNotify(BluetoothGattCharacteristic c, Sink sink) throws IOException {
        if (c == null) throw new IOException("特征为空");
        synchronized (sinks) { sinks.put(c, sink); }
        try {
            if (!gatt.setCharacteristicNotification(c, true)) throw new IOException("setCharacteristicNotification 失败");
            BluetoothGattDescriptor d = c.getDescriptor(UUID.fromString(MiProtocol.CCCD_UUID));
            if (d == null) throw new IOException("特征没有 CCCD 描述符");
            submit(new Op(2, c, d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE, true, 0));
        } catch (IOException e) {
            synchronized (sinks) { sinks.remove(c); }
            throw e;
        }
    }

    void disableNotify(BluetoothGattCharacteristic c) {
        if (c == null || gatt == null) return;
        synchronized (sinks) { sinks.remove(c); }
        try {
            gatt.setCharacteristicNotification(c, false);
            BluetoothGattDescriptor d = c.getDescriptor(UUID.fromString(MiProtocol.CCCD_UUID));
            if (d != null) submit(new Op(2, c, d, BluetoothGattDescriptor.DISABLE_NOTIFICATION_VALUE, true, 0));
        } catch (Exception ignored) { }
    }

    /** 按 UUID 找特征：先按完整 UUID 比，再按 16 位短 UUID 比（各家 ROM 上报形式不同）。 */
    BluetoothGattCharacteristic chr(String uuid) {
        if (gatt == null) return null;
        UUID want = UUID.fromString(uuid);
        for (BluetoothGattService s : gatt.getServices()) {
            for (BluetoothGattCharacteristic c : s.getCharacteristics()) {
                if (sameUuid(c.getUuid(), want)) return c;
            }
        }
        return null;
    }

    static boolean sameUuid(UUID a, UUID b) {
        if (a.equals(b)) return true;
        String sa = a.toString().toLowerCase(Locale.US);
        String sb = b.toString().toLowerCase(Locale.US);
        if (sa.length() > 8 && sb.length() > 8) {
            String ta = sa.substring(4, 8), tb = sb.substring(4, 8);
            if (sa.endsWith("-0000-1000-8000-00805f9b34fb") && sb.endsWith("-0000-1000-8000-00805f9b34fb")
                    && ta.equals(tb)) return true;
        }
        return false;
    }

    private void submit(Op op) throws IOException {
        if (closed || gatt == null) throw new IOException("GATT 已关闭");
        queue.add(op);
        synchronized (op) {
            long deadline = System.currentTimeMillis() + 15000;
            while (!op.done && System.currentTimeMillis() < deadline) {
                long left = deadline - System.currentTimeMillis();
                if (left <= 0) break;
                try { op.wait(Math.min(left, 500)); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            }
            if (!op.done) throw new IOException("GATT 操作超时（kind=" + op.kind + "）");
            if (op.status != 0) throw new IOException("GATT 操作失败 status=" + op.status + "（kind=" + op.kind + "）");
        }
    }

    private void loop() {
        while (!closed) {
            Op op;
            try {
                op = queue.take();
            } catch (InterruptedException e) {
                return;
            }
            if (closed) return;
            current = op;
            try {
                switch (op.kind) {
                    case 0:
                        op.chr.setWriteType(op.withResponse
                                ? BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                                : BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE);
                        op.chr.setValue(op.payload);
                        if (!gatt.writeCharacteristic(op.chr)) fail(op, -1000);
                        break;
                    case 1:
                        if (!gatt.readCharacteristic(op.chr)) fail(op, -1000);
                        break;
                    case 2:
                        op.desc.setValue(op.payload);
                        if (!gatt.writeDescriptor(op.desc)) fail(op, -1000);
                        break;
                    case 3:
                        if (!gatt.requestMtu(op.mtu)) fail(op, -1000);
                        break;
                    default:
                        fail(op, -1001);
                }
            } catch (Exception e) {
                fail(op, -1002);
            }
        }
    }

    private void fail(Op op, int status) {
        synchronized (op) { op.status = status; op.done = true; op.notifyAll(); }
    }

    private void done(int status, byte[] value) {
        Op op = current;
        if (op == null) return;
        synchronized (op) {
            op.status = status;
            op.value = value;
            op.done = true;
            op.notifyAll();
        }
    }

    // ------------------------------------------------------------------ 回调

    private final BluetoothGattCallback callback = new BluetoothGattCallback() {
        @Override public void onConnectionStateChange(BluetoothGatt g, int status, int newState) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                connected = true;
                synchronized (connLock) { connLock.notifyAll(); }
            } else {
                connected = false;
                if (lastError == null) lastError = "蓝牙断开（status=" + status + "）";
                synchronized (connLock) { connLock.notifyAll(); }
                // 唤醒所有在途操作，避免在关闭后一直等
                Op op = current;
                if (op != null) fail(op, -1003);
            }
        }

        @Override public void onServicesDiscovered(BluetoothGatt g, int status) {
            servicesReady = true;
            synchronized (connLock) { connLock.notifyAll(); }
        }

        // Android 13（API 33）加了带 value 的三参回调，其默认实现会转调两参版本；
        // 这里显式覆盖三参版本，并让两参版本只在旧系统上生效，避免同一帧被处理两次。
        // 写回调在所有版本上只有三参这一种（javap 核对过 API 34 的 android.jar）
        @Override public void onCharacteristicWrite(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            done(status, null);
        }

        @Override public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value, int status) {
            done(status, value);
        }

        @Override public void onCharacteristicRead(BluetoothGatt g, BluetoothGattCharacteristic c, int status) {
            if (Build.VERSION.SDK_INT >= 33) return;
            done(status, c.getValue());
        }

        @Override public void onDescriptorWrite(BluetoothGatt g, BluetoothGattDescriptor d, int status) {
            done(status, null);
        }

        @Override public void onMtuChanged(BluetoothGatt g, int m, int status) {
            mtuStatus = status;
            if (status == 0) GattIo.this.mtu = m;
            done(status, null);
        }

        @Override public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c, byte[] value) {
            dispatch(c, value);
        }

        @Override public void onCharacteristicChanged(BluetoothGatt g, BluetoothGattCharacteristic c) {
            if (Build.VERSION.SDK_INT >= 33) return;
            dispatch(c, c.getValue());
        }

        private void dispatch(BluetoothGattCharacteristic c, byte[] raw) {
            Sink sink;
            synchronized (sinks) { sink = sinks.get(c); }
            if (sink == null) return;
            if (raw == null) return;
            try {
                sink.onFrame(raw.clone());
            } catch (Throwable t) {
                Log.w(TAG, "sink failed", t);
            }
        }
    };
}
