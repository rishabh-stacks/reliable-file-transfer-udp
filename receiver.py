"""
receiver.py -- reliable file transfer over UDP (receiver side).

    python receiver.py --port 9000 --out-dir received --loss 0.2 -v

The receiver learns the protocol (saw / gbn / sr) and the window size from the sender's START
packet, so it needs no --mode option.

  saw / gbn  Accept only the packet it expects next.  Anything else (duplicate or ahead of time)
             is discarded.  After EVERY DATA packet it sends a cumulative ACK:
             "ACK n" = all packets below n have arrived.
  sr         Accept any packet inside its window [base, base+N), keep out-of-order ones in a buffer
             and answer every packet with its own "ACK seq".  The file is written in order as soon
             as the gaps are filled.

When the sender's FIN arrives the receiver checks the SHA-256 of what it wrote against the one in
START, answers FIN-ACK (1 byte: 1 = match), and then lingers for a moment so that it can answer a
repeated FIN if the FIN-ACK was lost -- the same job TCP's TIME-WAIT does.
"""

import argparse
import hashlib
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass, field

from channel import (ChannelStats, LossyChannel, add_channel_args, channel_opts_from_args,
                     setup_logging)
from protocol import (ACK, DATA, FIN, FIN_ACK, MODE_NAMES, MODES, START, START_ACK,
                      decode, decode_meta, encode)

log = logging.getLogger("receiver")


def safe_name(name):
    """File name from the network -> a plain name that can only land inside --out-dir."""
    name = os.path.basename(name.replace("\\", "/").replace("\x00", ""))
    return name if name not in ("", ".", "..") else "received.bin"


@dataclass
class ReceiverStats:
    filename: str = ""
    out_path: str = ""
    mode: str = ""
    window: int = 1
    file_size: int = 0
    total_packets: int = 0
    data_received: int = 0       # valid DATA packets that arrived (including duplicates / out-of-order)
    duplicates: int = 0          # packets that had already been received
    out_of_order: int = 0        # packets that arrived ahead of the one expected
    acks_sent: int = 0
    bad_packets: int = 0         # packets that failed the CRC check
    bytes_written: int = 0
    completed: bool = False      # FIN received after every packet
    sha256_ok: bool = False
    elapsed: float = 0.0
    channel: ChannelStats = field(default_factory=ChannelStats)


class Receiver:
    def __init__(self, host="0.0.0.0", port=9000, out_dir="received", idle_timeout=30.0, linger=2.0,
                 channel_opts=None):
        os.makedirs(out_dir, exist_ok=True)
        self.out_dir = out_dir
        self.idle_timeout = idle_timeout     # abort a transfer if the sender is silent this long
        self.linger = linger                 # how long to keep answering FIN after finishing
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.chan = LossyChannel(self.sock, name="receiver", **(channel_opts or {}))
        self.stats = ReceiverStats()

    def close(self):
        self.chan.close()
        self.sock.close()

    # ------------------------------------------------------------------ one complete transfer
    def serve_one(self, stop=None):
        """Wait for a sender, receive one file, return ReceiverStats.  `stop` is an optional threading.Event."""
        self.stats = stats = ReceiverStats()
        self.chan.stats = ChannelStats()
        stats.channel = self.chan.stats
        got = self._wait_for_start(stop)
        if got is None:
            return stats
        peer, meta = got
        total, window, mode = meta["total"], meta["window"], meta["mode"]
        selective = mode == "sr"
        stats.filename = safe_name(meta["name"])                  # never trust a path from the network
        stats.out_path = os.path.join(self.out_dir, stats.filename)
        stats.mode, stats.window, stats.file_size, stats.total_packets = mode, window, meta["size"], total
        log.info("START from %s:%d: '%s', %d bytes, %d packets, %s, window=%d",
                 peer[0], peer[1], stats.filename, meta["size"], total, MODE_NAMES[mode], window)

        started = time.perf_counter()
        sha = hashlib.sha256()
        expected = 0          # saw/gbn: next packet we want.   sr: lowest packet not yet delivered (window base)
        buffer = {}           # sr only: packets that arrived early, waiting for the gap to be filled

        with open(stats.out_path, "wb") as out:
            def deliver(payload):
                out.write(payload)
                sha.update(payload)
                stats.bytes_written += len(payload)

            self._send(START_ACK, 0, peer)
            last_activity = time.perf_counter()
            while True:
                if stop is not None and stop.is_set():
                    return stats
                if time.perf_counter() - last_activity > self.idle_timeout:
                    log.error("sender silent for %.0f s - giving up on this transfer", self.idle_timeout)
                    return stats
                pkt, src = self._recv(0.2)
                if pkt is None or src != peer:
                    continue
                last_activity = time.perf_counter()

                if pkt.ptype == START:                           # our START-ACK was lost: say it again
                    log.debug("RECV   START again -> repeat START-ACK")
                    self._send(START_ACK, 0, peer)
                elif pkt.ptype == FIN:
                    if expected >= total:
                        break
                elif pkt.ptype == DATA and pkt.seq < total:
                    seq = pkt.seq
                    stats.data_received += 1
                    if not selective:
                        # ---- Stop-and-Wait / Go-Back-N receiver: cumulative ACK, no buffering
                        if seq == expected:
                            deliver(pkt.payload)
                            expected += 1
                            log.debug("RECV   DATA seq=%d  in order -> deliver, send ACK %d", seq, expected)
                        elif seq < expected:
                            stats.duplicates += 1
                            log.debug("RECV   DATA seq=%d  duplicate -> discard, send ACK %d", seq, expected)
                        else:
                            stats.out_of_order += 1
                            log.debug("RECV   DATA seq=%d  ahead (expected %d) -> discard, send ACK %d",
                                      seq, expected, expected)
                        self._send(ACK, expected, peer)
                    else:
                        # ---- Selective Repeat receiver: buffer early packets, ACK each one individually
                        if seq < expected or seq in buffer:
                            stats.duplicates += 1
                            log.debug("RECV   DATA seq=%d  duplicate -> send ACK %d again", seq, seq)
                            self._send(ACK, seq, peer)
                        elif seq < expected + window:
                            buffer[seq] = pkt.payload
                            if seq != expected:
                                stats.out_of_order += 1
                            log.debug("RECV   DATA seq=%d  accepted%s -> send ACK %d", seq,
                                      "" if seq == expected else " (buffered, waiting for %d)" % expected, seq)
                            self._send(ACK, seq, peer)
                            while expected in buffer:                    # deliver the in-order prefix
                                deliver(buffer.pop(expected))
                                expected += 1
                        else:
                            log.debug("RECV   DATA seq=%d  outside window [%d, %d) -> ignored",
                                      seq, expected, expected + window)

        stats.elapsed = time.perf_counter() - started
        stats.completed = True
        stats.sha256_ok = (sha.hexdigest() == meta["sha256"]) and stats.bytes_written == meta["size"]
        log.info("FIN received, file %s", "verified (SHA-256 matches)" if stats.sha256_ok else "CORRUPT (SHA-256 mismatch)")
        fin_ack = encode(FIN_ACK, total, b"\x01" if stats.sha256_ok else b"\x00")
        self.chan.sendto(fin_ack, peer)

        end = time.perf_counter() + self.linger                  # TIME-WAIT style linger
        while time.perf_counter() < end and not (stop is not None and stop.is_set()):
            pkt, src = self._recv(0.05)
            if pkt is not None and src == peer and pkt.ptype == FIN:
                log.debug("RECV   FIN again -> repeat FIN-ACK")
                self.chan.sendto(fin_ack, peer)
        return stats

    # ------------------------------------------------------------------ helpers
    def _wait_for_start(self, stop):
        while stop is None or not stop.is_set():
            pkt, src = self._recv(0.2)
            if pkt is None or pkt.ptype != START:
                continue
            try:
                meta = decode_meta(pkt.payload)
                meta = dict(name=str(meta["name"]), size=int(meta["size"]), total=int(meta["total"]),
                            mode=str(meta["mode"]), window=int(meta["window"]), sha256=str(meta["sha256"]))
                if meta["mode"] not in MODES or meta["window"] < 1 or meta["total"] < 0 or meta["size"] < 0:
                    raise ValueError("bad parameters")
            except (ValueError, KeyError, TypeError):
                log.warning("ignored a malformed START packet from %s:%d", src[0], src[1])
                continue
            return src, meta
        return None

    def _send(self, ptype, seq, peer):
        if ptype == ACK:
            self.stats.acks_sent += 1
        self.chan.sendto(encode(ptype, seq), peer)

    def _recv(self, timeout):
        self.sock.settimeout(timeout)
        try:
            datagram, src = self.sock.recvfrom(65535)
        except (socket.timeout, BlockingIOError, ConnectionError):
            return None, None
        pkt = decode(datagram)
        if pkt is None:
            self.stats.bad_packets += 1
            log.debug("RECV   corrupt packet discarded (CRC mismatch)")
        return pkt, src


def print_summary(s):
    print("=" * 70)
    print(" File received: %s" % s.filename)
    print("=" * 70)
    print(" saved as          : %s  (%d bytes)" % (s.out_path, s.bytes_written))
    print(" protocol          : %s%s" % (MODE_NAMES[s.mode], "" if s.mode == "saw" else ", window N = %d" % s.window))
    print(" DATA arrivals     : %d for %d packets  (%d duplicates, %d ahead of order)"
          % (s.data_received, s.total_packets, s.duplicates, s.out_of_order))
    print(" ACKs sent         : %d  (network dropped %d of the %d packets this side sent)"
          % (s.acks_sent, s.channel.dropped, s.channel.sent))
    print(" corrupt discarded : %d" % s.bad_packets)
    if not s.completed:
        print(" integrity         : INCOMPLETE - transfer was aborted")
    else:
        print(" integrity         : %s" % ("OK - SHA-256 matches the sender's" if s.sha256_ok else "FAILED - SHA-256 mismatch"))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Reliable file receiver over UDP (pairs with sender.py).")
    ap.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    ap.add_argument("--port", type=int, default=9000, help="UDP port (default 9000)")
    ap.add_argument("--out-dir", default="received", help="folder for received files (default ./received)")
    ap.add_argument("--idle-timeout", type=float, default=30.0, help="abort if the sender is silent this long (s)")
    ap.add_argument("--linger", type=float, default=2.0, help="seconds to keep answering FIN after finishing")
    ap.add_argument("--once", action="store_true", help="exit after one file instead of waiting for the next")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every packet event")
    add_channel_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    try:
        rx = Receiver(args.host, args.port, args.out_dir, args.idle_timeout, args.linger,
                      channel_opts_from_args(args, 1))
    except OSError as exc:
        print("ERROR: cannot listen on %s:%d: %s" % (args.host, args.port, exc), file=sys.stderr)
        return 1
    print("Receiver listening on UDP port %d, saving files to '%s'. Ctrl+C to stop." % (rx.port, args.out_dir))
    try:
        while True:
            stats = rx.serve_one()
            print_summary(stats)
            if args.once:
                return 0 if stats.completed and stats.sha256_ok else 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0
    finally:
        rx.close()


if __name__ == "__main__":
    sys.exit(main())
