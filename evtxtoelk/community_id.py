"""Community ID v1 flow hash (https://github.com/corelight/community-id-spec).

Shared with parsezeeklogs; kept verbatim so both projects hash flows the same way.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import struct

__all__ = ["community_id"]

_PROTO_NUMBERS = {"tcp": 6, "udp": 17, "sctp": 132, "icmp": 1, "icmp6": 58, "icmpv6": 58}
_ICMP4_PAIRS = {8: 0, 0: 8, 13: 14, 14: 13, 15: 16, 16: 15, 10: 9, 9: 10, 17: 18, 18: 17}
_ICMP6_PAIRS = {
    128: 129, 129: 128, 133: 134, 134: 133, 135: 136, 136: 135,
    130: 131, 131: 130, 139: 140, 140: 139, 144: 145, 145: 144,
}  # fmt: skip


def _protocol_number(proto: str | int, ip_version: int) -> int | None:
    if isinstance(proto, int):
        return proto
    name = proto.lower()
    if name == "icmp" and ip_version == 6:
        name = "icmp6"
    return _PROTO_NUMBERS.get(name)


def _flow_ports(
    number: int, sport: int | None, dport: int | None
) -> tuple[int | None, int | None, bool] | None:
    """Return (sport, dport, one_way) for the hash, or None when the flow has no valid ports."""
    if number in (1, 58):
        pairs = _ICMP4_PAIRS if number == 1 else _ICMP6_PAIRS
        icmp_type = sport if sport is not None else 0
        if icmp_type in pairs:
            return icmp_type, pairs[icmp_type], False
        return icmp_type, dport if dport is not None else 0, True
    if number in (6, 17, 132):
        if sport is None or dport is None:
            return None
        return sport, dport, False
    return None, None, False


def community_id(
    saddr: str,
    daddr: str,
    proto: str | int,
    sport: int | None = None,
    dport: int | None = None,
    seed: int = 0,
) -> str | None:
    """Community ID v1 flow hash (https://github.com/corelight/community-id-spec).

    For ICMP, ``sport``/``dport`` are the ICMP type and code, as Zeek logs them.
    Returns ``None`` when the addresses or protocol cannot be interpreted.
    """
    try:
        src = ipaddress.ip_address(saddr)
        dst = ipaddress.ip_address(daddr)
    except ValueError:
        return None
    if src.version != dst.version:
        return None
    number = _protocol_number(proto, src.version)
    if number is None:
        return None
    ports = _flow_ports(number, sport, dport)
    if ports is None:
        return None
    sport, dport, one_way = ports

    if not one_way and (dst.packed < src.packed or (src == dst and (dport or 0) < (sport or 0))):
        src, dst = dst, src
        sport, dport = dport, sport

    data = struct.pack("!H", seed) + src.packed + dst.packed + struct.pack("!BB", number, 0)
    if sport is not None and dport is not None:
        data += struct.pack("!HH", sport & 0xFFFF, dport & 0xFFFF)
    # SHA-1 is what the Community ID specification mandates; this is a flow
    # identifier, not a security control.
    digest = hashlib.sha1(data).digest()  # NOSONAR python:S4790  # noqa: S324
    return "1:" + base64.b64encode(digest).decode("ascii")
