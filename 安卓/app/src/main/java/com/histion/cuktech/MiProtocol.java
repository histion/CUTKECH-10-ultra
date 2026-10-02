package com.histion.cuktech;

/** 米家 BLE 标准认证 + MIOT 加密会话的线协议常量（对齐 xiaomi/protocol.py、session.py）。 */
final class MiProtocol {
    private MiProtocol() {}

    static final String UPNP_UUID = "00000010-0000-1000-8000-00805f9b34fb";
    static final String AVDTP_UUID = "00000019-0000-1000-8000-00805f9b34fb";
    static final String MIOT_WRITE_UUID = "0000001a-0000-1000-8000-00805f9b34fb";
    static final String MIOT_NOTIFY_UUID = "0000001b-0000-1000-8000-00805f9b34fb";
    static final String CCCD_UUID = "00002902-0000-1000-8000-00805f9b34fb";

    static final byte[] CMD_GET_INFO = Util.unhex("a2000000");
    static final byte[] CMD_SET_KEY = Util.unhex("15000000");
    static final byte[] CMD_LOGIN = Util.unhex("24000000");
    static final byte[] CMD_AUTH = Util.unhex("13000000");

    static final byte[] CMD_SEND_DATA = Util.unhex("000000030400");
    static final byte[] CMD_SEND_DID = Util.unhex("000000000200");
    static final byte[] CMD_SEND_KEY = Util.unhex("0000000b0100");
    static final byte[] CMD_SEND_INFO = Util.unhex("0000000a0200");

    static final byte[] RCV_RDY = Util.unhex("00000101");
    static final byte[] RCV_OK = Util.unhex("00000100");
    static final byte[] GREETING_TRIGGER = Util.unhex("a4");
    static final byte[] OFFICIAL_ACK = Util.unhex("00000300");

    static final byte[] CFM_REGISTER_OK = Util.unhex("11000000");
    static final byte[] CFM_REGISTER_ERR = Util.unhex("12000000");
    static final byte[] CFM_LOGIN_OK = Util.unhex("21000000");
    static final byte[] CFM_LOGIN_ERR = Util.unhex("23000000");

    static final byte[][] AUTH_ERRORS = new byte[][]{
            Util.unhex("e0000000"), Util.unhex("e1000000"),
            Util.unhex("e2000000"), Util.unhex("e3000000"),
    };

    static final int PARCEL_CHUNK_SIZE = 18;
    static final int MAX_AUTH_PARCELS = 64;
    static final int MAX_MIOT_PARCELS = 64;
    static final int MAX_MIOT_COUNTER = 0xFFFF;
    static final int MAX_RX_OUT_OF_ORDER = 24;
    static final int MAX_CAPTURED_FRAMES = 16;

    /** 请求 opcode → 期望的应答 opcode 集合（与 session.py 一致）。 */
    static byte[][] expectedResponseOpcodes(byte[] request) {
        byte[] op = new byte[]{request[0], request[1]};
        if (op[0] == 0x05 && op[1] == 0x20) return new byte[][]{op};
        if (op[0] == 0x0c && op[1] == 0x20) return new byte[][]{new byte[]{0x0b, 0x20}};
        if (op[0] == 0x24 && op[1] == 0x20) return new byte[][]{new byte[]{0x66, 0x20}};
        if (op[0] == 0x33 && op[1] == 0x20) {
            return new byte[][]{
                    new byte[]{0x0e, 0x20}, new byte[]{0x1c, 0x20},
                    new byte[]{(byte) 0x93, 0x20}};
        }
        return new byte[][]{op};
    }
}
