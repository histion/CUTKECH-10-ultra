"""Post-auth encrypted MIOT transport for Mi BLE standard-auth devices.

Wraps an authenticated ``MiAuthClient`` + ``MiSessionKeys`` with AES-CCM
request/response framing. Protocol shape, reverse-engineered from AD1204U
traffic captured in Mi Home's btsnoop log:

- Outbound (host → device), write characteristic ``00000019`` (AVDTP):
    announcement ``00 00 00 00 NN NN`` (NN NN = parcel count LE, normally 01 00)
    → wait ``00 00 01 01`` (RCV_RDY)
    → N parcels ``01 00 CC CC <ct>`` (CC CC = TX counter LE)
    → wait ``00 00 01 00`` (RCV_OK)
- Inbound (device → host), notify characteristic ``0000001c``:
    official-inline ``00 00 02 00 CC CC <ct>`` (ACK with ``00 00 03 00``)
    OR ``00 00 00 00 NN NN`` + RCV_RDY/parcels/RCV_OK

``CC CC`` is an independent 16-bit LE counter in each direction.

AES-CCM parameters (verified against captured AD1204U traffic): nonce =
``IV(4) + b"\\x00\\x00\\x00\\x00" + counter(4 LE)``, no AAD, 4-byte tag.
Key is ``app_key`` for host→device and ``dev_key`` for device→host.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable

from cryptography.hazmat.primitives.ciphers.aead import AESCCM

from .auth import MiAuthClient, MiAuthError, MiSessionKeys, _ChannelQueue, _chunk_parcel
from .protocol import (
    OFFICIAL_ACK,
    PARCEL_CHUNK_SIZE,
    RCV_OK,
    RCV_RDY,
)

LOGGER = logging.getLogger(__name__)

# Post-auth MIOT channels. Observed on the AD1204U via Mi Home btsnoop:
#   write + RCV_RDY/RCV_OK (notify): 0x0000001a
#   notify (encrypted responses + telemetry pushes): 0x0000001b
MIOT_WRITE_UUID = "0000001a-0000-1000-8000-00805f9b34fb"
MIOT_NOTIFY_UUID = "0000001b-0000-1000-8000-00805f9b34fb"

# MIOT request/response announcement code (byte[3] of the 6-byte header).
MIOT_ANN_CODE = 0x00
MAX_CAPTURED_FRAMES = 16
# 允许的“乱序/延迟”窗口。分片传输期间被挂起的行内帧会在分片收完后才处理，
# counter 因此比“已处理到的位置”小 1~2；实测最大也就几帧，24 留足余量。
MAX_RX_OUT_OF_ORDER = 24
MAX_MIOT_PARCELS = 64
MAX_MIOT_COUNTER = 0xFFFF
EXPECTED_RESPONSE_OPCODES: dict[bytes, frozenset[bytes]] = {
    b"\x05\x20": frozenset((b"\x05\x20",)),
    b"\x0c\x20": frozenset((b"\x0b\x20",)),
    b"\x24\x20": frozenset((b"\x66\x20",)),
    b"\x33\x20": frozenset((b"\x0e\x20", b"\x1c\x20", b"\x93\x20")),
}


NonceBuilder = Callable[[bytes, int], bytes]
NotificationCallback = Callable[[bytes], None]
FatalErrorCallback = Callable[[Exception], None]


def mible_v1_nonce(iv: bytes, counter: int) -> bytes:
    """AD1204U MIOT nonce scheme: IV(4) + 4 zero bytes + counter(4 LE). 12 bytes."""
    if len(iv) != 4:
        raise ValueError("iv must be 4 bytes")
    return iv + b"\x00\x00\x00\x00" + counter.to_bytes(4, "little")


@dataclass(frozen=True)
class MiotCipherProfile:
    """AES-CCM framing parameters."""

    name: str
    tx_nonce: NonceBuilder
    rx_nonce: NonceBuilder
    aad: bytes = b""
    tag_length: int = 4


DEFAULT_PROFILE = MiotCipherProfile(
    name="mible-v1-noaad",
    tx_nonce=mible_v1_nonce,
    rx_nonce=mible_v1_nonce,
    aad=b"",
    tag_length=4,
)


@dataclass(frozen=True)
class MiotFrame:
    """A single encrypted MIOT frame captured on either direction."""

    counter: int
    ciphertext: bytes
    direction: str  # "tx" or "rx"


class MiSessionError(MiAuthError):
    pass


class MiSessionReauthenticationRequired(MiSessionError):
    """The session keys must be replaced before another encrypted frame."""


class MiSession:
    """Encrypted MIOT transport over an authenticated ``MiAuthClient``.

    Callers are expected to have already run ``login()``. The auth client's
    UPNP/AVDTP subscriptions stay put — we additionally subscribe to
    ``0000001c`` for responses and spontaneous telemetry pushes.
    """

    def __init__(
        self,
        auth: MiAuthClient,
        keys: MiSessionKeys,
        *,
        profile: MiotCipherProfile = DEFAULT_PROFILE,
        timeout: float = 10.0,
        notification_callback: NotificationCallback | None = None,
        fatal_error_callback: FatalErrorCallback | None = None,
    ) -> None:
        self._auth = auth
        self._keys = keys
        self._profile = profile
        self._timeout = timeout
        self._response = _ChannelQueue(MIOT_NOTIFY_UUID, maxsize=64)
        # RCV_RDY/RCV_OK for MIOT come on UUID 0x001a (same char we write
        # requests to), distinct from AVDTP used during auth.
        self._control = _ChannelQueue(MIOT_WRITE_UUID, maxsize=16)
        self._response_sub = False
        self._control_sub = False
        self._tx_counter = 0
        self._rx_counter = 0
        self._notification_callback = notification_callback
        self._fatal_error_callback = fatal_error_callback
        self._fatal_error_reported = False
        self._request_lock = asyncio.Lock()
        self._pending_seq: bytes | None = None
        self._pending_opcodes: frozenset[bytes] = frozenset()
        self._pending_response: asyncio.Future[bytes] | None = None
        self._request_task: asyncio.Task[Any] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reader_error: Exception | None = None
        self._closing = False
        self._deferred_frames: deque[MiotFrame] = deque(maxlen=MAX_CAPTURED_FRAMES)
        self._request_seq = 2
        self._app_ccm = AESCCM(keys.app_key, tag_length=profile.tag_length)
        self._dev_ccm = AESCCM(keys.dev_key, tag_length=profile.tag_length)
        self._rx_raw: deque[MiotFrame] = deque(maxlen=MAX_CAPTURED_FRAMES)
        self._rx_duplicates = 0
        self._rx_out_of_order = 0

    @property
    def profile(self) -> MiotCipherProfile:
        return self._profile

    @property
    def captured_frames(self) -> list[MiotFrame]:
        return list(self._rx_raw)

    @property
    def rx_duplicates(self) -> int:
        """How many inbound frames were dropped as link-layer retransmissions.

        A non-zero value is normal on Windows (our OFFICIAL_ACK is a
        write-without-response and the controller drops those under load).
        It only matters if it climbs very fast — that would mean the ACK path
        is broadly failing rather than occasionally losing a packet.
        """
        return self._rx_duplicates

    @property
    def rx_out_of_order(self) -> int:
        """How many inbound frames arrived out of counter order.

        Expected to be non-zero on this device: frames that show up in the
        middle of a parcel transfer are deferred and processed after the parcel
        completes, so their counters are naturally 1~2 behind.  Treating that as
        a dead session is what made the link look like it kept "dropping".
        """
        return self._rx_out_of_order

    @property
    def requires_reauthentication(self) -> bool:
        """Return whether either encryption counter has been exhausted."""
        return (
            self._tx_counter > MAX_MIOT_COUNTER
            or self._rx_counter > MAX_MIOT_COUNTER
        )

    async def __aenter__(self) -> "MiSession":
        await self.subscribe()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.unsubscribe()

    async def subscribe(self) -> None:
        self._closing = False
        if not self._response_sub:
            await self._auth._client.start_notify(
                MIOT_NOTIFY_UUID,
                self._response.handler(),
                **self._auth._start_kwargs,
            )
            self._response_sub = True
        if not self._control_sub:
            await self._auth._client.start_notify(
                MIOT_WRITE_UUID,
                self._control.handler(),
                **self._auth._start_kwargs,
            )
            self._control_sub = True
        if self._reader_task is None:
            self._reader_error = None
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="ad1204u_miot_reader"
            )

    async def unsubscribe(self) -> None:
        self._closing = True
        request_task = self._request_task
        if request_task is not None and request_task is not asyncio.current_task():
            request_task.cancel()

        for uuid, flag_attr in (
            (MIOT_NOTIFY_UUID, "_response_sub"),
            (MIOT_WRITE_UUID, "_control_sub"),
        ):
            if getattr(self, flag_attr):
                setattr(self, flag_attr, False)
                try:
                    await self._auth._client.stop_notify(uuid)
                except Exception:  # noqa: BLE001
                    LOGGER.debug("stop_notify(%s) failed", uuid, exc_info=True)

        reader_task = self._reader_task
        self._reader_task = None
        if reader_task is not None:
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task

        pending = self._pending_response
        self._pending_response = None
        self._pending_seq = None
        self._pending_opcodes = frozenset()
        if pending is not None and not pending.done():
            pending.cancel()

    async def _recv_until(self, channel: _ChannelQueue, expected: bytes) -> None:
        while True:
            if channel.dropped:
                raise MiSessionError(
                    f"notification queue overflow on {channel.uuid} "
                    f"({channel.dropped} frame(s) dropped)"
                )
            try:
                data = await asyncio.wait_for(channel.queue.get(), self._timeout)
            except asyncio.TimeoutError as exc:
                raise MiSessionError(
                    f"timeout waiting for {expected.hex()} on {channel.uuid}"
                ) from exc
            if data == expected:
                return
            LOGGER.debug("ignoring %s on %s", data.hex(), channel.uuid)

    # ---------------------------------------------------------- encode
    def encrypt(self, plaintext: bytes, counter: int | None = None) -> bytes:
        if counter is None:
            counter = self._tx_counter
        if not 0 <= counter <= MAX_MIOT_COUNTER:
            raise MiSessionReauthenticationRequired(
                "MIOT TX counter exhausted; reconnect and authenticate again"
            )
        nonce = self._profile.tx_nonce(self._keys.app_iv, counter)
        return self._app_ccm.encrypt(nonce, plaintext, self._profile.aad or None)

    def decrypt(self, ciphertext: bytes, counter: int) -> bytes:
        nonce = self._profile.rx_nonce(self._keys.dev_iv, counter)
        return self._dev_ccm.decrypt(nonce, ciphertext, self._profile.aad or None)

    def next_sequence(self) -> int:
        """Return the next non-zero per-session MIOT request sequence."""
        sequence = self._request_seq
        self._request_seq = (sequence + 1) & 0xFFFF
        if self._request_seq == 0:
            self._request_seq = 1
        return sequence

    async def initialize(self) -> None:
        """Enter the MIOT application session, matching Mi Home after login."""
        sequence = self.next_sequence()
        request = b"\x05\x20" + sequence.to_bytes(2, "little") + b"\xf0"
        response = await self.send_request(request)
        if response != request:
            raise MiSessionError(
                f"unexpected MIOT init response: {response.hex()}"
            )

    # ---------------------------------------------------------- frame IO
    async def send_request(self, plaintext: bytes) -> bytes:
        """Encrypt and send one request, then await its routed response."""
        req_seq = plaintext[2:4] if len(plaintext) >= 4 else b""
        if not req_seq:
            raise MiSessionError("request is missing a sequence number")

        async with self._request_lock:
            if self._closing:
                raise MiSessionError("MIOT session is unsubscribed")
            reader_task = self._reader_task
            if reader_task is None or reader_task.done():
                detail = f": {self._reader_error}" if self._reader_error else ""
                raise MiSessionError(f"MIOT notification reader is not running{detail}")

            loop = asyncio.get_running_loop()
            response: asyncio.Future[bytes] = loop.create_future()
            self._pending_seq = req_seq
            self._pending_opcodes = EXPECTED_RESPONSE_OPCODES.get(
                plaintext[:2], frozenset((plaintext[:2],))
            )
            self._pending_response = response
            request_task = asyncio.current_task()
            self._request_task = request_task
            counter = self._tx_counter
            try:
                if counter > MAX_MIOT_COUNTER:
                    error = MiSessionReauthenticationRequired(
                        "MIOT TX counter exhausted; reconnect and authenticate again"
                    )
                    self._report_fatal_error(error)
                    raise error
                # Spend the nonce before the first fallible operation. If a
                # write is cancelled or its result is uncertain, this session
                # can never encrypt another request with the same nonce.
                self._tx_counter = counter + 1
                ct = self.encrypt(plaintext, counter)
                await self._send_encrypted(ct, counter)
                try:
                    return await asyncio.wait_for(response, self._timeout)
                except asyncio.TimeoutError as exc:
                    raise MiSessionError(
                        f"timeout waiting for MIOT response seq={req_seq.hex()}"
                    ) from exc
            except asyncio.CancelledError as exc:
                if self._closing:
                    raise MiSessionError("MIOT session unsubscribed") from exc
                raise
            finally:
                if self._request_task is request_task:
                    self._request_task = None
                if self._pending_response is response:
                    self._pending_response = None
                    self._pending_seq = None
                    self._pending_opcodes = frozenset()
                if not response.done():
                    response.cancel()

    async def send_command(self, plaintext: bytes, *, grace: float = 0.6) -> None:
        """Fire a SET-style request without waiting for a routed response.

        Verified against the real AD1204U: a SET is answered by *two* frames,
        neither of which fits ``send_request``'s strict routing —

          ACK    ``0b 20 <seq> 00 01 01 <siid> <piid>``            (no status)
          Result ``0c 20 <seq> 00 04 01 <siid> <piid> 00 01 10 <v>``

        The reader loop already ACKs both at the BLE level, so the only thing
        ``send_request`` would add is a chance to time out on a frame shape it
        does not recognise.  Callers therefore verify the effect by reading the
        property back, and this method only guarantees the write hit the wire.

        ``grace`` gives the device time to apply the value before that read-back.
        """
        async with self._request_lock:
            if self._closing:
                raise MiSessionError("MIOT session is unsubscribed")
            counter = self._tx_counter
            if counter > MAX_MIOT_COUNTER:
                error = MiSessionReauthenticationRequired(
                    "MIOT TX counter exhausted; reconnect and authenticate again"
                )
                self._report_fatal_error(error)
                raise error
            # Spend the nonce before the first fallible operation, exactly as
            # send_request does, so a cancelled write can never reuse it.
            self._tx_counter = counter + 1
            await self._send_encrypted(self.encrypt(plaintext, counter), counter)
        if grace > 0:
            await asyncio.sleep(grace)

    async def _reader_loop(self) -> None:
        """ACK every inbound frame promptly and route decrypted plaintext."""
        try:
            while True:
                if self._response.dropped:
                    raise MiSessionError(
                        f"notification queue overflow on {self._response.uuid} "
                        f"({self._response.dropped} frame(s) dropped)"
                    )
                frame = await self._recv_encrypted()

                # ① 逐字节相同的重传：设备没及时收到我们的 OFFICIAL_ACK 时会原样重发
                #    上一帧。我们的 ACK 是 write-without-response，链路层不保证送达
                #    （Windows BLE 在 TX 队列拥塞时会静默丢弃），所以这是正常现象，
                #    不是会话失活的信号。_recv_encrypted 已经把 ACK 补发回去了，丢弃即可。
                duplicate = any(
                    f.counter == frame.counter and f.ciphertext == frame.ciphertext
                    for f in list(self._rx_raw)[:-1]
                )
                if duplicate:
                    self._rx_duplicates += 1
                    LOGGER.debug(
                        "duplicate MIOT frame counter=%d (%d so far); "
                        "re-ACKed and dropped as a retransmission",
                        frame.counter, self._rx_duplicates,
                    )
                    continue

                # ② counter 比预期**小一点点**同样正常，不是会话坏了：
                #    分片（parcel）传输期间插进来的行内帧会被挂起（_deferred_frames），
                #    等分片收完才处理 —— 于是“到达顺序”与“处理顺序”不一致，后处理的那
                #    一帧 counter 天然比前一小 1~2。2026-09-22 真机实测三条报错分别是
                #    prev-1 / prev-1 / prev-2，规律完全吻合。
                #    只有倒退幅度超出窗口，才说明设备侧会话真的重来过了。
                #
                # ⚠ 这里之前是 `frame.counter < self._rx_counter` 就直接判死，
                #   于是每一次乱序/重传都让整条 MIOT 会话作废、重新认证、重扫蓝牙——
                #   用户看到的“蓝牙过一会就断”其实全是这个，BLE 链路一直是好的。
                if frame.counter < self._rx_counter:
                    lag = self._rx_counter - frame.counter
                    if lag > MAX_RX_OUT_OF_ORDER:
                        prev = list(self._rx_raw)[-2] if len(self._rx_raw) >= 2 else None
                        raise MiSessionReauthenticationRequired(
                            "MIOT RX counter moved backwards too far "
                            f"(got counter={frame.counter}, expected >= {self._rx_counter}, "
                            f"previous counter={prev.counter if prev else None}); "
                            "reconnect and authenticate again"
                        )
                    self._rx_out_of_order += 1
                    LOGGER.debug(
                        "out-of-order MIOT frame counter=%d (lag=%d); routing anyway",
                        frame.counter, lag,
                    )

                try:
                    plaintext = self.decrypt(frame.ciphertext, frame.counter)
                except Exception as exc:  # noqa: BLE001
                    LOGGER.debug(
                        "skip undecryptable frame counter=%d: %s",
                        frame.counter,
                        exc,
                    )
                    continue

                # 只有新帧才推进水位；乱序帧不把水位往回拨。
                if frame.counter >= self._rx_counter:
                    self._rx_counter = frame.counter + 1
                response = self._pending_response
                if (
                    response is not None
                    and not response.done()
                    and len(plaintext) >= 4
                    and plaintext[2:4] == self._pending_seq
                    and plaintext[:2] in self._pending_opcodes
                ):
                    response.set_result(plaintext)
                else:
                    is_property_event = (
                        plaintext[:2] == b"\x0f\x20"
                        or (
                            len(plaintext) >= 5
                            and plaintext[:2] == b"\x0c\x20"
                            and plaintext[4] == 0x04
                        )
                    )
                    if is_property_event and self._notification_callback is not None:
                        try:
                            self._notification_callback(plaintext)
                        except Exception:  # noqa: BLE001
                            LOGGER.exception("MIOT notification callback failed")
                    else:
                        LOGGER.debug(
                            "unhandled MIOT plaintext counter=%d: %s",
                            frame.counter,
                            plaintext.hex(),
                        )

                if self._rx_counter > MAX_MIOT_COUNTER:
                    raise MiSessionReauthenticationRequired(
                        "MIOT RX counter exhausted; reconnect and authenticate again"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._reader_error = exc
            response = self._pending_response
            if response is not None and not response.done():
                response.set_exception(
                    MiSessionError(f"MIOT notification reader failed: {exc}")
                )
            self._report_fatal_error(exc)
            LOGGER.debug("MIOT notification reader stopped", exc_info=True)

    def _report_fatal_error(self, error: Exception) -> None:
        """Notify the owner once that this session can no longer be used."""
        if self._closing or self._fatal_error_reported:
            return
        self._fatal_error_reported = True
        callback = self._fatal_error_callback
        if callback is None:
            return
        try:
            callback(error)
        except Exception:  # noqa: BLE001
            LOGGER.exception("MIOT fatal-error callback failed")

    async def _send_encrypted(self, ct: bytes, counter: int) -> None:
        mtu = getattr(self._auth._client, "mtu_size", None)
        # frame overhead: 2-byte idx + 2-byte counter prefix → MTU-3 (write_cmd) - 4
        payload_cap = (
            max(PARCEL_CHUNK_SIZE - 2, mtu - 7) if isinstance(mtu, int) and mtu > 0
            else PARCEL_CHUNK_SIZE - 2
        )
        counter_prefix = counter.to_bytes(2, "little")
        frames = _chunk_parcel(ct, payload_cap)
        if len(frames) > MAX_MIOT_PARCELS:
            raise MiSessionError(
                f"MIOT request needs {len(frames)} parcels; "
                f"maximum is {MAX_MIOT_PARCELS}"
            )
        announcement = bytes([0x00, 0x00, 0x00, MIOT_ANN_CODE]) + len(frames).to_bytes(
            2, "little"
        )
        await self._auth._write(MIOT_WRITE_UUID, announcement)
        await self._recv_until(self._control, RCV_RDY)
        for idx, chunk in enumerate(frames, start=1):
            frame = bytes([idx & 0xFF, (idx >> 8) & 0xFF]) + counter_prefix + chunk
            await self._auth._write(MIOT_WRITE_UUID, frame)
        await self._recv_until(self._control, RCV_OK)

    async def _recv_encrypted(self) -> MiotFrame:
        """Receive one encrypted MIOT frame from the data-response channel."""
        if self._deferred_frames:
            return self._deferred_frames.popleft()
        while True:
            data = await self._response.queue.get()
            LOGGER.debug("← %s: %s", MIOT_NOTIFY_UUID, data.hex())
            if len(data) < 6:
                LOGGER.debug("discarding short frame: %s", data.hex())
                continue
            if data[:4] == b"\x00\x00\x02\x00":
                frame_counter = int.from_bytes(data[4:6], "little")
                ct = bytes(data[6:])
                await self._auth._write(MIOT_NOTIFY_UUID, OFFICIAL_ACK)
                frame = MiotFrame(
                    counter=frame_counter, ciphertext=ct, direction="rx"
                )
                self._rx_raw.append(frame)
                return frame
            if data[:4] == bytes([0x00, 0x00, 0x00, MIOT_ANN_CODE]):
                expected = int.from_bytes(data[4:6], "little")
                if not 1 <= expected <= MAX_MIOT_PARCELS:
                    raise MiSessionError(
                        f"invalid MIOT parcel count {expected}; "
                        f"maximum is {MAX_MIOT_PARCELS}"
                    )
                await self._auth._write(MIOT_NOTIFY_UUID, RCV_RDY)
                parts: dict[int, bytes] = {}
                parcel_counter: int | None = None
                while len(parts) < expected:
                    part = await asyncio.wait_for(
                        self._response.queue.get(), self._timeout
                    )
                    LOGGER.debug("parcel: %s", part.hex())
                    if len(part) >= 6 and part[:4] == b"\x00\x00\x02\x00":
                        await self._auth._write(MIOT_NOTIFY_UUID, OFFICIAL_ACK)
                        if len(self._deferred_frames) >= MAX_CAPTURED_FRAMES:
                            raise MiSessionError(
                                "too many inline frames interleaved with MIOT parcel"
                            )
                        deferred = MiotFrame(
                            counter=int.from_bytes(part[4:6], "little"),
                            ciphertext=bytes(part[6:]),
                            direction="rx",
                        )
                        self._rx_raw.append(deferred)
                        self._deferred_frames.append(deferred)
                        continue
                    if len(part) < 2:
                        continue
                    idx = int.from_bytes(part[:2], "little")
                    if not 1 <= idx <= expected:
                        raise MiSessionError(
                            f"MIOT parcel index {idx} outside 1..{expected}"
                        )
                    if idx == 1:
                        if len(part) < 4:
                            raise MiSessionError("first MIOT parcel is missing counter")
                        parcel_counter = int.from_bytes(part[2:4], "little")
                        parts[idx] = bytes(part[4:])
                    else:
                        parts[idx] = bytes(part[2:])
                await self._auth._write(MIOT_NOTIFY_UUID, RCV_OK)
                ct = b"".join(parts[i] for i in sorted(parts))
                assert parcel_counter is not None
                frame = MiotFrame(
                    counter=parcel_counter, ciphertext=ct, direction="rx"
                )
                self._rx_raw.append(frame)
                return frame
            LOGGER.debug("discarding unknown frame: %s", data.hex())

    # ---------------------------------------------------------- passive
    async def collect_pushes(self, duration: float) -> list[MiotFrame]:
        """Return the bounded frame history accumulated during `duration`."""
        before = tuple(self._rx_raw)
        await asyncio.sleep(duration)
        after = tuple(self._rx_raw)
        overlap = min(len(before), len(after))
        while overlap and before[-overlap:] != after[:overlap]:
            overlap -= 1
        return list(after[overlap:])
