"""
protocol.py -- packet format and small helpers shared by sender and receiver.

Every UDP datagram carries exactly one packet:

     0        1                  5          7                  11
     +--------+------------------+----------+------------------+-------------------+
     |  type  |  sequence number |  length  |  CRC-32 checksum |  payload          |
     | 1 byte |  4 bytes         |  2 bytes |  4 bytes         |  0 .. 1400 bytes  |
     +--------+------------------+----------+------------------+-------------------+
     (all numbers are big-endian, i.e. "network byte order")

The CRC-32 covers the header (with the CRC field set to 0) and the payload.  A packet whose
CRC does not match is silently discarded, so to the protocol a corrupted packet is simply
a lost packet.

Packet types
    START      sender   -> receiver   file name, size, protocol, window, SHA-256 (JSON payload)
    START_ACK  receiver -> sender
    DATA       sender   -> receiver   seq = packet number, payload = bytes of the file
    ACK        receiver -> sender     seq = see sender.py (cumulative for saw/gbn, single for sr)
    FIN        sender   -> receiver   "every DATA packet has been acknowledged"
    FIN_ACK    receiver -> sender     payload = 1 byte: 1 if the file's SHA-256 matched, else 0
"""

import hashlib
import json
import struct
import zlib
from collections import namedtuple

START, START_ACK, DATA, ACK, FIN, FIN_ACK = range(1, 7)
TYPE_NAMES = {START: "START", START_ACK: "START-ACK", DATA: "DATA",
              ACK: "ACK", FIN: "FIN", FIN_ACK: "FIN-ACK"}

HEADER = struct.Struct("!BIHI")     # type, seq, payload length, crc
HEADER_SIZE = HEADER.size           # 11 bytes
MAX_PAYLOAD = 1400                  # header + payload stays below the usual 1500-byte Ethernet MTU

MODES = ("saw", "gbn", "sr")
MODE_NAMES = {"saw": "Stop-and-Wait", "gbn": "Go-Back-N", "sr": "Selective Repeat"}

Packet = namedtuple("Packet", "ptype seq payload")


def _checksum(ptype, seq, payload):
    header = HEADER.pack(ptype, seq, len(payload), 0)          # CRC field is zero while computing
    return zlib.crc32(payload, zlib.crc32(header)) & 0xFFFFFFFF


def encode(ptype, seq=0, payload=b""):
    """Build the bytes that go into one UDP datagram."""
    return HEADER.pack(ptype, seq, len(payload), _checksum(ptype, seq, payload)) + payload


def decode(datagram):
    """Return a Packet, or None if the datagram is malformed or fails the CRC check."""
    if len(datagram) < HEADER_SIZE:
        return None
    ptype, seq, length, crc = HEADER.unpack_from(datagram)
    payload = datagram[HEADER_SIZE:]
    if length != len(payload) or ptype not in TYPE_NAMES:
        return None
    if crc != _checksum(ptype, seq, payload):
        return None
    return Packet(ptype, seq, payload)


def describe(datagram):
    """Short text for log lines, e.g. 'DATA seq=7' (reads the header only, no CRC check)."""
    if len(datagram) < HEADER_SIZE:
        return "<%d-byte fragment>" % len(datagram)
    ptype, seq, _, _ = HEADER.unpack_from(datagram)
    name = TYPE_NAMES.get(ptype, "type-%d" % ptype)
    return ("%s %d" % (name, seq)) if ptype == ACK else ("%s seq=%d" % (name, seq))


def encode_meta(meta):
    return json.dumps(meta, separators=(",", ":")).encode("utf-8")


def decode_meta(payload):
    return json.loads(payload.decode("utf-8"))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()
