package com.histion.cuktech;

import java.io.IOException;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;

/**
 * 米家 BLE 标准认证的 login 流程（对齐 vendor/cuktech_ble/xiaomi/auth.py）。
 *
 * 只实现 login（绑定那一步由米家 App 完成，本机只拿云端的 12 字节 token 登录），
 * 时序与 Python 版一致：订阅 AVDTP → a4 问候 → 订阅 UPNP → CMD_LOGIN。
 */
final class MiAuth {
    /** 通知通道：满时丢最旧的一帧并计数（与 Python ``_ChannelQueue`` 同语义）。 */
    static final class Chan implements GattIo.Sink {
        final String uuid;
        final LinkedBlockingQueue<byte[]> q = new LinkedBlockingQueue<byte[]>(64);
        int dropped;

        Chan(String uuid) { this.uuid = uuid; }

        @Override public void onFrame(byte[] data) {
            if (q.remainingCapacity() == 0) {
                q.poll();
                dropped++;
            }
            try { q.put(data); } catch (InterruptedException ignored) { }
        }

        byte[] poll(long timeoutMs) throws IOException {
            try {
                return q.poll(timeoutMs, java.util.concurrent.TimeUnit.MILLISECONDS);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new IOException("等待中断");
            }
        }
    }

    static final class Keys {
        byte[] devKey, appKey, devIv, appIv;
    }

    final GattIo io;
    final Chan avdtp = new Chan(MiProtocol.AVDTP_UUID);
    final Chan upnp = new Chan(MiProtocol.UPNP_UUID);
    private final long timeoutMs;
    private boolean avdtpSub, upnpSub;

    MiAuth(GattIo io, double timeoutSec) {
        this.io = io;
        this.timeoutMs = (long) (timeoutSec * 1000);
    }

    // ------------------------------------------------------------------ 订阅

    void subscribe(boolean withUpnp) throws IOException {
        if (!avdtpSub) {
            io.enableNotify(io.chr(MiProtocol.AVDTP_UUID), avdtp);
            avdtpSub = true;
        }
        if (withUpnp) subscribeUpnp();
    }

    void subscribeUpnp() throws IOException {
        if (upnpSub) return;
        io.enableNotify(io.chr(MiProtocol.UPNP_UUID), upnp);
        upnpSub = true;
    }

    void unsubscribe() {
        if (upnpSub) { try { io.disableNotify(io.chr(MiProtocol.UPNP_UUID)); } catch (Exception ignored) { } upnpSub = false; }
        if (avdtpSub) { try { io.disableNotify(io.chr(MiProtocol.AVDTP_UUID)); } catch (Exception ignored) { } avdtpSub = false; }
    }

    // ------------------------------------------------------------------ 基础收发

    void write(String uuid, byte[] payload) throws IOException {
        io.write(uuid, payload, false);
    }

    private byte[] recv(Chan ch) throws IOException {
        if (ch.dropped > 0) {
            throw new IOException("通知队列溢出 " + ch.uuid + "（已丢 " + ch.dropped + " 帧）");
        }
        byte[] data = ch.poll(timeoutMs);
        if (data == null) throw new IOException("等待 " + ch.uuid + " 超时");
        for (byte[] err : MiProtocol.AUTH_ERRORS) {
            if (Util.eq(data, err)) throw new IOException("设备认证错误 " + Util.hex(data));
        }
        return data;
    }

    private void recvUntil(Chan ch, byte[] expected) throws IOException {
        long deadline = System.currentTimeMillis() + timeoutMs * 4;
        while (true) {
            long left = deadline - System.currentTimeMillis();
            if (left <= 0) throw new IOException("等待 " + Util.hex(expected) + " 超时");
            byte[] data = recv(ch);
            if (data == null) throw new IOException("等待 " + Util.hex(expected) + " 超时");
            if (Util.eq(data, expected)) return;
        }
    }

    private void sendParcel(String uuid, byte[] announcement, byte[] data) throws IOException {
        int chunk = Math.max(MiProtocol.PARCEL_CHUNK_SIZE, io.mtu() - 5);
        List<byte[]> frames = Util.chunk(data, chunk);
        if (frames.size() > MiProtocol.MAX_AUTH_PARCELS) {
            throw new IOException("认证载荷需要 " + frames.size() + " 个分片，超过上限");
        }
        byte[] head = Util.cat(
                java.util.Arrays.copyOfRange(announcement, 0, 4),
                Util.le(frames.size(), 2),
                java.util.Arrays.copyOfRange(announcement, 6, announcement.length));
        write(uuid, head);
        recvUntil(avdtp, MiProtocol.RCV_RDY);
        for (int i = 0; i < frames.size(); i++) {
            write(MiProtocol.AVDTP_UUID, Util.cat(Util.le(i + 1, 2), frames.get(i)));
        }
        recvUntil(avdtp, MiProtocol.RCV_OK);
    }

    private byte[] recvParcel(byte[] announcement) throws IOException {
        int expected = Util.u16le(announcement, 4);
        if (expected < 1 || expected > MiProtocol.MAX_AUTH_PARCELS) {
            throw new IOException("分片数非法 " + expected);
        }
        write(MiProtocol.AVDTP_UUID, MiProtocol.RCV_RDY);
        byte[][] parts = new byte[expected + 1][];
        int got = 0;
        List<Integer> seen = new ArrayList<Integer>();
        while (got < expected) {
            byte[] data = recv(avdtp);
            if (data.length < 2) throw new IOException("分片太短 " + Util.hex(data));
            int idx = Util.u16le(data, 0);
            if (idx < 1 || idx > expected) throw new IOException("分片序号越界 " + idx);
            if (parts[idx] != null) throw new IOException("分片序号重复 " + idx);
            parts[idx] = java.util.Arrays.copyOfRange(data, 2, data.length);
            seen.add(idx);
            got++;
        }
        write(MiProtocol.AVDTP_UUID, MiProtocol.RCV_OK);
        byte[] out = new byte[0];
        for (int i = 1; i <= expected; i++) out = Util.cat(out, parts[i]);
        return out;
    }

    // ------------------------------------------------------------------ 问候

    void greet() throws IOException {
        write(MiProtocol.UPNP_UUID, MiProtocol.GREETING_TRIGGER);
        for (int i = 0; i < 2; i++) {
            byte[] challenge = recv(avdtp);
            if (challenge.length < 3 || challenge[0] != 0x00 || challenge[1] != 0x00 || challenge[2] != 0x04) {
                throw new IOException("问候帧不符合预期 " + Util.hex(challenge));
            }
            byte[] echo = Util.cat(Util.unhex("000005"),
                    java.util.Arrays.copyOfRange(challenge, 3, challenge.length));
            write(MiProtocol.AVDTP_UUID, echo);
        }
    }

    private byte[] recvVariant(int expectedCode) throws IOException {
        byte[] announcement = recv(avdtp);
        if (announcement.length < 4) throw new IOException("通告帧太短 " + Util.hex(announcement));
        int variant = announcement[2] & 0xFF;
        int code = announcement[3] & 0xFF;
        if (code != expectedCode) throw new IOException("期望 code 0x" + Integer.toHexString(expectedCode)
                + "，实际 " + Util.hex(announcement));
        if (variant == 0x02) {
            byte[] payload = java.util.Arrays.copyOfRange(announcement, 4, announcement.length);
            write(MiProtocol.AVDTP_UUID, MiProtocol.OFFICIAL_ACK);
            return payload;
        }
        if (variant == 0x00) return recvParcel(announcement);
        throw new IOException("未知的 variant 0x" + Integer.toHexString(variant));
    }

    // ------------------------------------------------------------------ 登录

    /** 用云端的 12 字节 token 完成登录，返回会话密钥；token 不对时抛 IOException。 */
    Keys login(byte[] token) throws IOException {
        if (token == null || token.length != 12) throw new IOException("token 必须是 12 字节");
        byte[] appRand = Crypto.random(16);

        write(MiProtocol.UPNP_UUID, MiProtocol.CMD_LOGIN);
        sendParcel(MiProtocol.AVDTP_UUID, MiProtocol.CMD_SEND_KEY, appRand);

        byte[] devRand = recvVariant(0x0d);
        if (devRand.length != 16) throw new IOException("设备随机数长度不对 " + devRand.length);

        byte[] devInfo = recvVariant(0x0c);

        byte[][] k = Crypto.deriveLogin(token, appRand, devRand);
        byte[] devKey = k[0], appKey = k[1], devIv = k[2], appIv = k[3];
        byte[] expect = Crypto.hmacSha256(devKey, Util.cat(devRand, appRand));
        if (!Util.eq(devInfo, expect)) throw new IOException("设备 HMAC 不匹配 —— token 不对或需要重新绑定");

        byte[] appInfo = Crypto.hmacSha256(appKey, Util.cat(appRand, devRand));
        sendParcel(MiProtocol.AVDTP_UUID, MiProtocol.CMD_SEND_INFO, appInfo);

        byte[] conf = recv(upnp);
        if (Util.eq(conf, MiProtocol.CFM_LOGIN_OK)) {
            Keys keys = new Keys();
            keys.devKey = devKey; keys.appKey = appKey; keys.devIv = devIv; keys.appIv = appIv;
            return keys;
        }
        if (Util.eq(conf, MiProtocol.CFM_LOGIN_ERR)) throw new IOException("设备返回登录失败（23 00 00 00）");
        throw new IOException("意外的登录确认帧 " + Util.hex(conf));
    }
}
