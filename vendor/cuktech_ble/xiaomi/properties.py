"""High-level MIOT property read/write over an authenticated ``MiSession``.

The wire format is settled (see project memory): siid=2 request/response
with per-entry type+marker+value bytes. This module encodes/decodes that
format and gives the caller a simple dict of {(siid, piid): value}.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .session import MiSession

# The full siid=2 property catalogue Mi Home polls in one shot.
# (siid, piid, python type hint)
DEFAULT_READ_TUPLES: tuple[tuple[int, int], ...] = (
    (2, 1), (2, 2), (2, 3), (2, 4),   # port C1/C2/C3/A info (u32)
    (2, 5), (2, 6), (2, 7),           # scene_mode, screen_save_time, protocol_ctl (u8)
    (2, 0x0d),                        # device_language (u8)
    (2, 0x0f),                        # usb_a_always_on (bool)
    (2, 0x10),                        # port_ctl (u8)
    (2, 0x11), (2, 0x12),             # c1c2_protocol, c3a_protocol (u32)
    (2, 0x13), (2, 0x14),             # screenoff_while_idle, screen_dir_lock (bool)
    (2, 0x15),                        # protocol_ctl_extend (u32)
)


@dataclass(frozen=True)
class PropertyValue:
    siid: int
    piid: int
    status: int
    type_byte: int
    marker: int
    value: Any

    @property
    def key(self) -> tuple[int, int]:
        return (self.siid, self.piid)


class MiotProtocolError(Exception):
    pass


def encode_get_properties(seq: int, tuples: tuple[tuple[int, int], ...]) -> bytes:
    body = b"\x33\x20" + seq.to_bytes(2, "little")
    body += bytes([0x02, len(tuples)])
    for siid, piid in tuples:
        body += bytes([siid]) + piid.to_bytes(2, "little")
    return body


def parse_response(pt: bytes) -> list[PropertyValue]:
    # The charger's get-response opcode varies with request shape:
    #   0x93 — original btsnoop captures (bulk read).
    #   0x1c — multi-property reads from our integration.
    #   0x0e — single-property reads.
    # Body layout is identical across all three.
    if len(pt) < 6 or pt[1] != 0x20 or pt[0] not in (0x93, 0x1c, 0x0e):
        raise MiotProtocolError(f"bad response header: {pt[:6].hex()}")
    return _parse_property_entries(pt, count=pt[5], includes_status=True)


def parse_notification(pt: bytes) -> list[PropertyValue]:
    """Decode a spontaneous property update from the charger.

    Port telemetry uses opcode ``0x0f``. Setting changes are echoed with
    opcode ``0x0c`` and the event flag ``0x04``. Both omit the two-byte status
    field found in get-property responses.
    """
    if (
        len(pt) < 6
        or pt[1] != 0x20
        or (pt[0] == 0x0C and pt[4] != 0x04)
        or pt[0] not in (0x0F, 0x0C)
    ):
        raise MiotProtocolError(f"bad notification header: {pt[:6].hex()}")
    return _parse_property_entries(pt, count=pt[5], includes_status=False)


def _parse_property_entries(
    pt: bytes, *, count: int, includes_status: bool
) -> list[PropertyValue]:
    """Decode the typed property entries shared by responses and events."""
    i = 6
    out: list[PropertyValue] = []
    for _ in range(count):
        prefix_size = 7 if includes_status else 5
        if i + prefix_size > len(pt):
            raise MiotProtocolError(
                f"truncated property entry at offset {i}: {pt.hex()}"
            )
        siid = pt[i]
        piid = int.from_bytes(pt[i + 1 : i + 3], "little")
        if includes_status:
            status = int.from_bytes(pt[i + 3 : i + 5], "little")
            type_byte = pt[i + 5]
            marker = pt[i + 6]
            value_offset = i + 7
        else:
            status = 0
            type_byte = pt[i + 3]
            marker = pt[i + 4]
            value_offset = i + 5

        value_size = {0x01: 1, 0x02: 2, 0x04: 4}.get(type_byte)
        if value_size is None:
            raise MiotProtocolError(
                f"unknown type 0x{type_byte:02x} at offset {i} in {pt.hex()}"
            )
        if value_offset + value_size > len(pt):
            raise MiotProtocolError(
                f"truncated property value at offset {i}: {pt.hex()}"
            )
        raw = pt[value_offset : value_offset + value_size]
        value: Any
        if type_byte == 0x01 and marker == 0x00:
            value = bool(raw[0])
        else:
            value = int.from_bytes(raw, "little")
        out.append(PropertyValue(siid, piid, status, type_byte, marker, value))
        i = value_offset + value_size
    return out


async def get_properties(
    session: MiSession,
    tuples: tuple[tuple[int, int], ...] = DEFAULT_READ_TUPLES,
    *,
    seq: int | None = None,
) -> dict[tuple[int, int], PropertyValue]:
    if seq is None:
        seq = session.next_sequence()
    request = encode_get_properties(seq, tuples)
    response_pt = await session.send_request(request)
    return {item.key: item for item in parse_response(response_pt)}


# --- set_properties (single prop) -------------------------------------------
#
# Wire format reversed from a tablet Mi Home capture (2026-04-23):
#   request  = 0c 20 <seq_le2> 00 <count> (<siid> <piid_le2> <type> <marker> <value>)*
#   response = 0b 20 <seq_le2> 01 <count> (<siid> <piid_le2> <status_le2>)*
# See memory/project_ad1204u_miot_set.md for the full reasoning.

SET_REQUEST_OPCODE = b"\x0c\x20"
SET_RESPONSE_OPCODE = b"\x0b\x20"


def encode_set_property(
    seq: int,
    siid: int,
    piid: int,
    value: int | bool,
    *,
    u32: bool = False,
    u16: bool = False,
) -> bytes:
    """Encode a set_properties request.

    The value type must match the property's declared MIOT format, because the
    ``type`` byte carries the width: ``0x01`` = 1 byte, ``0x02`` = 2 bytes,
    ``0x04`` = 4 bytes. ``marker`` separates bool (``0x00``) from unsigned
    (``0x10``), with ``0x50`` used for the u32 form seen in Mi Home captures.
    """
    body = SET_REQUEST_OPCODE + (seq & 0xFFFF).to_bytes(2, "little")
    body += bytes([0x00, 0x01])  # flags(0)=0, count=1
    body += bytes([siid]) + (piid & 0xFFFF).to_bytes(2, "little")
    if isinstance(value, bool):
        body += bytes([0x01, 0x00, int(value)])
    elif u32:
        body += bytes([0x04, 0x50]) + (int(value) & 0xFFFFFFFF).to_bytes(4, "little")
    elif u16:
        body += bytes([0x02, 0x10]) + (int(value) & 0xFFFF).to_bytes(2, "little")
    else:
        body += bytes([0x01, 0x10, int(value) & 0xFF])
    return body


def parse_set_response(pt: bytes) -> list[tuple[int, int, int]]:
    """Decode a SET reply. Returns ``[(siid, piid, status), ...]``.

    Two reply frames exist on the wire and both are treated as success:

    * ``0b 20 <seq> 01 <count>`` — the ACK. Each entry is either
      ``siid(1) piid_le2(2) status_le2(2)`` (the shape Mi Home's capture shows,
      status ``0`` = ok) or a bare ``siid(1) piid(1)``; the stride is inferred
      from the frame length so both decode.
    * ``0c 20 <seq> 04 01 siid(1) piid_le2(2) 00 01 10 <value>`` — the Result
      echo, which carries no status and simply means the write landed.
    """
    if len(pt) < 6:
        raise MiotProtocolError(f"bad set-response header: {pt.hex()}")
    opcode = pt[:2]
    if opcode != SET_RESPONSE_OPCODE:
        if opcode == SET_REQUEST_OPCODE and pt[4] == 0x04:
            siid = pt[6]
            piid = int.from_bytes(pt[7:9], "little")
            return [(siid, piid, 0)]
        raise MiotProtocolError(f"bad set-response opcode: {pt[:6].hex()}")
    if pt[4] != 0x01:
        raise MiotProtocolError(f"bad set-response flag: {pt[:6].hex()}")

    count = pt[5]
    body = len(pt) - 6
    if count == 0:
        return []
    if body == count * 5:
        stride, wide = 5, True
    elif body == count * 2:
        stride, wide = 2, False
    else:
        raise MiotProtocolError(
            f"set-response body of {body} bytes does not fit {count} entries: {pt.hex()}"
        )

    out: list[tuple[int, int, int]] = []
    i = 6
    for _ in range(count):
        siid = pt[i]
        if wide:
            piid = int.from_bytes(pt[i + 1 : i + 3], "little")
            status = int.from_bytes(pt[i + 3 : i + 5], "little")
        else:
            piid = pt[i + 1]
            status = 0
        out.append((siid, piid, status))
        i += stride
    return out


async def set_property(
    session: MiSession,
    siid: int,
    piid: int,
    value: int | bool,
    *,
    seq: int | None = None,
    u32: bool = False,
    u16: bool = False,
    verify: bool = True,
    grace: float = 0.6,
):
    """Write one property, then read it back to confirm the device took it.

    The AD1204U acknowledges a SET with frames that do not fit
    ``send_request``'s strict response routing (see ``MiSession.send_command``),
    so this writes fire-and-forget and decides success from the read-back. The
    returned value is the property's value after the write, or ``None`` when the
    read-back produced nothing.
    """
    if seq is None:
        seq = session.next_sequence()
    request = encode_set_property(seq, siid, piid, value, u32=u32, u16=u16)
    await session.send_command(request, grace=grace)
    if not verify:
        return None
    result = await get_properties(session, ((siid, piid),))
    item = result.get((siid, piid))
    return None if item is None else item.value
