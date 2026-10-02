package com.histion.cuktech;

import java.io.IOException;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.Deque;
import java.util.List;

/**
 * 认证后的 MIOT 加密会话（对齐 vendor/cuktech_ble/xiaomi/session.py）。
 *
 * 帧格式：outbound 写 0x001a（通告 00 00 00 00 NN NN → 等 0x001a 上的 RCV_RDY →
 * 分片 01 00 <counter_le2> <ct> → 等 RCV_OK）；inbound 在 0x001b 上，行内帧
 * ``00 00 02 00 <counter> <ct>``（回 ACK）或分片（回 RCV_RDY/RCV_OK）。
 * AES-CCM：nonce = IV(4) + 00000000 + counter(4 LE)，4 字节 tag，无 AAD。
 */
final class MiSession {
    private static final class Frame {
        final int counter;
        final byte[] ct;
        Frame(int counter, byte[] ct) { this.counter = counter; this.ct = ct; }
    }

    final GattIo io;
    final MiAuth auth;
    private final MiAuth.Keys keys;
    private final long timeoutMs;

    private final MiAuth.Chan resp = new MiAuth.Chan(MiProtocol.MIOT_NOTIFY_UUID);
    private final MiAuth.Chan ctrl = new MiAuth.Chan(MiProtocol.MIOT_WRITE_UUID);
    private final Deque<Frame> deferred = new ArrayDeque<Frame>();
    private final Deque<Frame> rxRaw = new ArrayDeque<Frame>();

    private int txCounter;
    private int rxCounter;
    private int requestSeq = 2;
    private Thread reader;
    private volatile boolean closing;
    private volatile String readerError;
    private int rxDuplicates;
    private int rxOutOfOrder;

    private final Object reqLock = new Object();
    private byte[] pendingResult;
    private String pendingError;
    private int pendingSeq = -1;
    private byte[][] pendingOpcodes = new byte[0][];
    private boolean reqDone;

    MiSession(GattIo io, MiAuth auth, MiAuth.Keys keys, double timeoutSec) {
        this.io = io;
        this.auth = auth;
        this.keys = keys;
        this.timeoutMs = (long) (timeoutSec * 1000);
    }

    int rxDuplicates() { return rxDuplicates; }
    int rxOutOfOrder() { return rxOutOfOrder; }

    void subscribe() throws IOException {
        closing = false;
        io.enableNotify(io.chr(MiProtocol.MIOT_NOTIFY_UUID), resp);
        io.enableNotify(io.chr(MiProtocol.MIOT_WRITE_UUID), ctrl);
        if (reader == null) {
            readerError = null;
            reader = new Thread(new Runnable() {
                @Override public void run() { readerLoop(); }
            }, "miot-reader");
            reader.setDaemon(true);
            reader.start();
        }
    }

    void unsubscribe() {
        closing = true;
        try { io.disableNotify(io.chr(MiProtocol.MIOT_NOTIFY_UUID)); } catch (Exception ignored) { }
        try { io.disableNotify(io.chr(MiProtocol.MIOT_WRITE_UUID)); } catch (Exception ignored) { }
        Thread t = reader;
        reader = null;
        if (t != null) t.interrupt();
        synchronized (reqLock) {
            reqDone = true;
            pendingError = "会话已取消订阅";
            reqLock.notifyAll();
        }
    }

    int nextSequence() {
        int seq = requestSeq;
        requestSeq = (seq + 1) & 0xFFFF;
        if (requestSeq == 0) requestSeq = 1;
        return seq;
    }

    byte[] encrypt(byte[] pt, int counter) {
        byte[] nonce = Util.cat(keys.appIv, Util.unhex("00000000"), Util.le(counter, 4));
        return Crypto.ccmEncrypt(keys.appKey, nonce, pt, null);
    }

    byte[] decrypt(byte[] ct, int counter) {
        byte[] nonce = Util.cat(keys.devIv, Util.unhex("00000000"), Util.le(counter, 4));
        return Crypto.ccmDecrypt(keys.devKey, nonce, ct, null);
    }

    /** 发一条请求并等它对应的应答明文。 */
    byte[] sendRequest(byte[] pt) throws IOException {
        if (pt.length < 4) throw new IOException("请求缺少序号");
        int seq = Util.u16le(pt, 2);
        synchronized (reqLock) {
            if (closing) throw new IOException("MIOT 会话已停止");
            if (reader == null || !reader.isAlive()) {
                throw new IOException("MIOT 读取线程未运行" + (readerError != null ? "：" + readerError : ""));
            }
            pendingSeq = seq;
            pendingOpcodes = MiProtocol.expectedResponseOpcodes(pt);
            pendingResult = null;
            pendingError = null;
            reqDone = false;
        }
        int counter;
        synchronized (this) { counter = txCounter++; }
        if (counter > MiProtocol.MAX_MIOT_COUNTER) {
            throw new IOException("MIOT 发送计数耗尽，需要重新认证");
        }
        sendEncrypted(encrypt(pt, counter), counter);

        synchronized (reqLock) {
            long deadline = System.currentTimeMillis() + timeoutMs;
            while (!reqDone && System.currentTimeMillis() < deadline) {
                long left = deadline - System.currentTimeMillis();
                if (left <= 0) break;
                try { reqLock.wait(Math.min(left, 300)); } catch (InterruptedException e) { Thread.currentThread().interrupt(); break; }
            }
            if (!reqDone) throw new IOException("等待 MIOT 应答超时 seq=" + seq);
            if (pendingError != null) throw new IOException(pendingError);
            if (pendingResult == null) throw new IOException("设备响应异常");
            return pendingResult;
        }
    }

    /** 写一条 SET 指令不等应答（设备会用两个形状特殊的帧回应，读回校验更可靠）。 */
    void sendCommand(byte[] pt, double graceSec) throws IOException {
        int counter;
        synchronized (this) { counter = txCounter++; }
        if (counter > MiProtocol.MAX_MIOT_COUNTER) {
            throw new IOException("MIOT 发送计数耗尽，需要重新认证");
        }
        sendEncrypted(encrypt(pt, counter), counter);
        if (graceSec > 0) {
            try { Thread.sleep((long) (graceSec * 1000)); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
    }

    private void sendEncrypted(byte[] ct, int counter) throws IOException {
        int cap = Math.max(MiProtocol.PARCEL_CHUNK_SIZE - 2, io.mtu() - 7);
        List<byte[]> frames = Util.chunk(ct, cap);
        if (frames.size() > MiProtocol.MAX_MIOT_PARCELS) {
            throw new IOException("MIOT 请求需要 " + frames.size() + " 个分片，超过上限");
        }
        io.write(MiProtocol.MIOT_WRITE_UUID,
                Util.cat(Util.unhex("00000000"), Util.le(frames.size(), 2)), false);
        recvUntil(ctrl, MiProtocol.RCV_RDY);
        byte[] prefix = Util.le(counter, 2);
        for (int i = 0; i < frames.size(); i++) {
            io.write(MiProtocol.MIOT_WRITE_UUID,
                    Util.cat(Util.le(i + 1, 2), prefix, frames.get(i)), false);
        }
        recvUntil(ctrl, MiProtocol.RCV_OK);
    }

    private void recvUntil(MiAuth.Chan ch, byte[] expected) throws IOException {
        long deadline = System.currentTimeMillis() + timeoutMs * 3;
        while (true) {
            long left = deadline - System.currentTimeMillis();
            if (left <= 0) throw new IOException("等待 " + Util.hex(expected) + " 超时");
            byte[] data = ch.poll(Math.max(1, left));
            if (data == null) throw new IOException("等待 " + Util.hex(expected) + " 超时");
            if (Util.eq(data, expected)) return;
        }
    }

    // ------------------------------------------------------------------ 读取线程

    private void readerLoop() {
        try {
            while (!closing) {
                Frame frame = recvEncrypted();
                if (frame == null) return;

                boolean duplicate = false;
                Frame[] history = rxRaw.toArray(new Frame[0]);
                for (int i = 0; i < history.length - 1; i++) {
                    if (history[i].counter == frame.counter && Arrays.equals(history[i].ct, frame.ct)) {
                        duplicate = true;
                        break;
                    }
                }
                if (duplicate) {
                    rxDuplicates++;
                    continue;
                }
                if (frame.counter < rxCounter) {
                    int lag = rxCounter - frame.counter;
                    if (lag > MiProtocol.MAX_RX_OUT_OF_ORDER) {
                        throw new IOException("MIOT 接收计数倒退过多（" + frame.counter + " < " + rxCounter + "）");
                    }
                    rxOutOfOrder++;
                }

                byte[] pt;
                try {
                    pt = decrypt(frame.ct, frame.counter);
                } catch (Exception ignored) {
                    continue;
                }
                if (frame.counter >= rxCounter) rxCounter = frame.counter + 1;

                boolean routed = false;
                synchronized (reqLock) {
                    if (!reqDone && pendingSeq >= 0 && pt.length >= 4
                            && Util.u16le(pt, 2) == pendingSeq && opcodeMatches(pt)) {
                        pendingResult = pt;
                        reqDone = true;
                        reqLock.notifyAll();
                        routed = true;
                    }
                }
                if (!routed) {
                    // 属性主动推送（0f 20 / 0c 20 + 0x04）：当前界面靠轮询刷新，收到即忽略。
                }
                if (rxCounter > MiProtocol.MAX_MIOT_COUNTER) {
                    throw new IOException("MIOT 接收计数耗尽，需要重新认证");
                }
            }
        } catch (Throwable t) {
            readerError = String.valueOf(t.getMessage());
            synchronized (reqLock) {
                if (!reqDone) {
                    pendingError = "MIOT 读取线程结束：" + readerError;
                    reqDone = true;
                    reqLock.notifyAll();
                }
            }
        }
    }

    private boolean opcodeMatches(byte[] pt) {
        for (byte[] op : pendingOpcodes) {
            if (op.length == 2 && pt[0] == op[0] && pt[1] == op[1]) return true;
        }
        return false;
    }

    private Frame recvEncrypted() throws IOException {
        if (!deferred.isEmpty()) return deferred.removeFirst();
        while (true) {
            byte[] data = resp.poll(Math.max(timeoutMs, 20000));
            if (data == null) throw new IOException("等待 MIOT 帧超时");
            if (data.length < 6) continue;
            if (data[0] == 0x00 && data[1] == 0x00 && data[2] == 0x02 && data[3] == 0x00) {
                int counter = Util.u16le(data, 4);
                byte[] ct = Arrays.copyOfRange(data, 6, data.length);
                ack(MiProtocol.OFFICIAL_ACK);
                Frame f = new Frame(counter, ct);
                push(f);
                return f;
            }
            if (data[0] == 0x00 && data[1] == 0x00 && data[2] == 0x00 && data[3] == 0x00) {
                int expected = Util.u16le(data, 4);
                if (expected < 1 || expected > MiProtocol.MAX_MIOT_PARCELS) {
                    throw new IOException("MIOT 分片数非法 " + expected);
                }
                ack(MiProtocol.RCV_RDY);
                byte[][] parts = new byte[expected + 1][];
                Integer counter = null;
                int got = 0;
                while (got < expected) {
                    byte[] part = resp.poll(Math.max(timeoutMs, 15000));
                    if (part == null) throw new IOException("等待 MIOT 分片超时");
                    if (part.length >= 6 && part[0] == 0x00 && part[1] == 0x00
                            && part[2] == 0x02 && part[3] == 0x00) {
                        ack(MiProtocol.OFFICIAL_ACK);
                        Frame d = new Frame(Util.u16le(part, 4),
                                Arrays.copyOfRange(part, 6, part.length));
                        push(d);
                        deferred.addLast(d);
                        continue;
                    }
                    if (part.length < 2) continue;
                    int idx = Util.u16le(part, 0);
                    if (idx < 1 || idx > expected) throw new IOException("MIOT 分片序号越界 " + idx);
                    if (parts[idx] != null) continue;
                    if (idx == 1) {
                        if (part.length < 4) throw new IOException("首个分片缺少计数");
                        counter = Util.u16le(part, 2);
                        parts[1] = Arrays.copyOfRange(part, 4, part.length);
                    } else {
                        parts[idx] = Arrays.copyOfRange(part, 2, part.length);
                    }
                    got++;
                }
                ack(MiProtocol.RCV_OK);
                byte[] ct = new byte[0];
                for (int i = 1; i <= expected; i++) {
                    if (parts[i] != null) ct = Util.cat(ct, parts[i]);
                }
                Frame f = new Frame(counter == null ? 0 : counter, ct);
                push(f);
                return f;
            }
        }
    }

    private void ack(byte[] frame) {
        try {
            io.write(MiProtocol.MIOT_NOTIFY_UUID, frame, false);
        } catch (Exception ignored) { }
    }

    private void push(Frame f) {
        rxRaw.addLast(f);
        while (rxRaw.size() > MiProtocol.MAX_CAPTURED_FRAMES) rxRaw.removeFirst();
    }
}
