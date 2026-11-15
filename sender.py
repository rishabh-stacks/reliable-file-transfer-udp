"""
sender.py -- reliable file transfer over UDP (sender side).

    python sender.py FILE --host 127.0.0.1 --port 9000 --mode gbn --window 8 --loss 0.2 -v

Protocols (choose with --mode)
    saw  Stop-and-Wait     send one packet, wait for its ACK, resend on timeout.
    gbn  Go-Back-N         up to N packets in flight, cumulative ACKs and ONE timer (for the oldest
                           unacknowledged packet); on timeout resend everything from that packet on.
    sr   Selective Repeat  up to N packets in flight, one ACK per packet and ONE TIMER PER PACKET;
                           on timeout resend only the packet that timed out.

Sequence numbers count packets (0, 1, 2, ...), not bytes, and never wrap around.
What "ACK n" means:
    saw / gbn : every packet below n has arrived  (n = the next packet the receiver expects)
    sr        : packet n has arrived
"""

import argparse
import logging
import math
import os
import socket
import sys
import time
from dataclasses import dataclass, field

from channel import (ChannelStats, LossyChannel, add_channel_args, channel_opts_from_args,
                     setup_logging)
from protocol import (ACK, DATA, FIN, FIN_ACK, MAX_PAYLOAD, MODE_NAMES, MODES, START, START_ACK,
                      decode, encode, encode_meta, file_sha256)

log = logging.getLogger("sender")


class TransferError(Exception):
    """The transfer could not be completed (receiver silent, ...)."""


@dataclass
class SenderStats:
    mode: str = ""
    window: int = 1
    file_size: int = 0
    total_packets: int = 0
    data_sent: int = 0           # DATA packets put on the wire: first transmissions + retransmissions
    retransmissions: int = 0
    timeouts: int = 0            # timer expirations during the data phase
    acks_received: int = 0       # valid ACKs received
    bad_packets: int = 0         # packets that failed the CRC check
    data_time: float = 0.0       # seconds from the first DATA packet until the last one is acknowledged
    total_time: float = 0.0      # whole run, including START / FIN handshakes
    receiver_ok: object = None   # True / False as reported in FIN-ACK, None if never confirmed
    channel: ChannelStats = field(default_factory=ChannelStats)
    trace: object = None         # list of (time, seq, is_retransmission) if tracing was requested

    @property
    def throughput(self):        # bytes per second
        return self.file_size / self.data_time if self.data_time > 0 else 0.0

    @property
    def overhead(self):          # DATA packets sent per packet of the file (1.0 = no retransmissions)
        return self.data_sent / self.total_packets if self.total_packets else 1.0


class Sender:
    def __init__(self, path, peer, mode="gbn", window=8, chunk=1024, timeout=0.2,
                 max_retries=50, fin_retries=10, channel_opts=None, trace=False):
        if mode not in MODES:
            raise ValueError("mode must be one of %s" % (MODES,))
        if window < 1:
            raise ValueError("window must be at least 1")
        if not 1 <= chunk <= MAX_PAYLOAD:
            raise ValueError("chunk must be between 1 and %d bytes" % MAX_PAYLOAD)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.path = path
        self.mode = mode
        self.window = 1 if mode == "saw" else window
        self.chunk = chunk
        self.timeout = timeout
        self.max_retries = max_retries
        self.give_up_after = max_retries * timeout       # seconds without progress before giving up
        self.fin_retries = fin_retries
        self.size = os.path.getsize(path)
        self.total = math.ceil(self.size / chunk)        # number of DATA packets
        self.sha256 = file_sha256(path)
        self.trace = [] if trace else None

        self.peer = (socket.gethostbyname(peer[0]), peer[1])
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        except OSError:
            pass
        self.chan = LossyChannel(self.sock, name="sender", **(channel_opts or {}))
        self.stats = SenderStats(mode=mode, window=self.window, file_size=self.size,
                                 total_packets=self.total, trace=self.trace)
        self._file = None
        self._t0 = self._last_progress = 0.0

    # ------------------------------------------------------------------ main flow
    def run(self):
        started = time.perf_counter()
        try:
            self._file = open(self.path, "rb")
            self._handshake()
            self._t0 = self._last_progress = time.perf_counter()
            {"saw": self._stop_and_wait, "gbn": self._go_back_n, "sr": self._selective_repeat}[self.mode]()
            self.stats.data_time = time.perf_counter() - self._t0
            self._close()
        finally:
            self.stats.total_time = time.perf_counter() - started
            if self._file:
                self._file.close()
            self.chan.close()
            self.sock.close()
            self.stats.channel = self.chan.stats
        return self.stats

    def _handshake(self):
        meta = dict(name=os.path.basename(self.path), size=self.size, chunk=self.chunk,
                    total=self.total, mode=self.mode, window=self.window, sha256=self.sha256)
        log.info("sending START to %s:%d  (%s, window=%d, %d bytes = %d packets of up to %d B)",
                 self.peer[0], self.peer[1], MODE_NAMES[self.mode], self.window,
                 self.size, self.total, self.chunk)
        if self._request(encode(START, 0, encode_meta(meta)), START_ACK, self.max_retries) is None:
            raise TransferError("no answer from receiver %s:%d after %d attempts - is receiver.py running?"
                                % (self.peer[0], self.peer[1], self.max_retries))
        log.info("receiver accepted the transfer")

    def _close(self):
        reply = self._request(encode(FIN, self.total), FIN_ACK, self.fin_retries)
        if reply is None:
            log.warning("FIN was never confirmed (all DATA was ACKed, so the file almost certainly arrived)")
        else:
            self.stats.receiver_ok = reply.payload[:1] == b"\x01"

    # ------------------------------------------------------------------ the three protocols
    def _stop_and_wait(self):
        for seq in range(self.total):
            attempt = 0
            while True:
                self._send_data(seq, retransmit=attempt > 0)
                attempt += 1
                deadline = time.perf_counter() + self.timeout
                acked = False
                while not acked:
                    pkt = self._recv(deadline)
                    if pkt is None:
                        break                                    # timer expired
                    if pkt.ptype == ACK:
                        self.stats.acks_received += 1
                        if pkt.seq == seq + 1:
                            log.debug("RECV   ACK %d  -> packet %d delivered", pkt.seq, seq)
                            acked = True
                        else:
                            log.debug("RECV   ACK %d  (old, ignored)", pkt.seq)
                if acked:
                    break
                self._on_timeout("no ACK for seq=%d -> resend it" % seq)
            self._last_progress = time.perf_counter()

    def _go_back_n(self):
        base = next_seq = 0                  # base = oldest unACKed packet, next_seq = next one to send
        deadline = None
        while base < self.total:
            # 1. fill the window
            while next_seq < min(base + self.window, self.total):
                self._send_data(next_seq)
                if base == next_seq:                             # window was empty: start the timer
                    deadline = time.perf_counter() + self.timeout
                next_seq += 1
            # 2. wait for an ACK or for the timer
            pkt = self._recv(deadline)
            if pkt is None:
                self._on_timeout("base=%d: go back and resend %d..%d" % (base, base, next_seq - 1))
                for seq in range(base, next_seq):
                    self._send_data(seq, retransmit=True)
                deadline = time.perf_counter() + self.timeout
            elif pkt.ptype == ACK:
                self.stats.acks_received += 1
                if base < pkt.seq <= next_seq:                   # cumulative ACK that moves the window
                    log.debug("RECV   ACK %d  -> window base %d -> %d", pkt.seq, base, pkt.seq)
                    base = pkt.seq
                    self._last_progress = time.perf_counter()
                    deadline = time.perf_counter() + self.timeout    # restart timer for the new oldest packet
                else:
                    log.debug("RECV   ACK %d  (duplicate, ignored)", pkt.seq)

    def _selective_repeat(self):
        base = next_seq = 0
        acked = [False] * self.total
        sent_at = {}                         # seq -> time of last transmission, for every un-ACKed packet
        while base < self.total:
            while next_seq < min(base + self.window, self.total):
                self._send_data(next_seq)
                sent_at[next_seq] = time.perf_counter()
                next_seq += 1
            deadline = min(sent_at.values()) + self.timeout      # earliest of the per-packet timers
            pkt = self._recv(deadline)
            if pkt is not None and pkt.ptype == ACK:
                self.stats.acks_received += 1
                seq = pkt.seq
                if base <= seq < next_seq and not acked[seq]:
                    acked[seq] = True
                    del sent_at[seq]
                    self._last_progress = time.perf_counter()
                    while base < self.total and acked[base]:     # slide the window over the ACKed prefix
                        base += 1
                    log.debug("RECV   ACK %d  -> window base is now %d", seq, base)
                else:
                    log.debug("RECV   ACK %d  (duplicate, ignored)", seq)
            now = time.perf_counter()
            for seq in sorted(s for s, t in sent_at.items() if now - t >= self.timeout):
                self._on_timeout("seq=%d: resend only this packet" % seq)
                self._send_data(seq, retransmit=True)
                sent_at[seq] = now

    # ------------------------------------------------------------------ helpers
    def _send_data(self, seq, retransmit=False):
        log.debug("%s DATA seq=%d", "RESEND" if retransmit else "SEND  ", seq)
        self._file.seek(seq * self.chunk)
        self.chan.sendto(encode(DATA, seq, self._file.read(self.chunk)), self.peer)
        self.stats.data_sent += 1
        if retransmit:
            self.stats.retransmissions += 1
        if self.trace is not None:
            self.trace.append((time.perf_counter() - self._t0, seq, retransmit))

    def _on_timeout(self, what):
        self.stats.timeouts += 1
        log.debug("TIMEOUT %s", what)
        if time.perf_counter() - self._last_progress > self.give_up_after:
            raise TransferError("no progress for %.1f s (%d timeouts in a row) - receiver gone?"
                                % (self.give_up_after, self.max_retries))

    def _request(self, packet, expected_type, tries):
        """Send a control packet (START / FIN) until the expected reply arrives; None if it never does."""
        for attempt in range(1, tries + 1):
            self.chan.sendto(packet, self.peer)
            deadline = time.perf_counter() + self.timeout
            while True:
                reply = self._recv(deadline)
                if reply is None:
                    break
                if reply.ptype == expected_type:
                    return reply
            log.debug("no reply yet (attempt %d of %d)", attempt, tries)
        return None

    def _recv(self, deadline):
        """Wait until `deadline` (a perf_counter() value) for one valid packet; None on timeout."""
        while True:
            self.sock.settimeout(max(deadline - time.perf_counter(), 0.0))
            try:
                datagram, _ = self.sock.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                return None
            except ConnectionError:          # Windows reports ICMP "port unreachable" like this; ignore
                continue
            pkt = decode(datagram)
            if pkt is None:
                self.stats.bad_packets += 1
                log.debug("RECV   corrupt packet discarded (CRC mismatch)")
                continue
            return pkt


def send_file(path, host, port, **kwargs):
    """Convenience wrapper: send `path` to host:port and return SenderStats."""
    return Sender(path, (host, port), **kwargs).run()


def print_summary(s, name):
    verdict = {True: "OK - receiver confirms the SHA-256 of the file matches",
               False: "FAILED - receiver reports a SHA-256 mismatch",
               None: "no confirmation received from the receiver"}[s.receiver_ok]
    window = "" if s.mode == "saw" else ", window N = %d" % s.window
    print("=" * 70)
    print(" Transfer finished: %s" % name)
    print("=" * 70)
    print(" protocol          : %s%s" % (MODE_NAMES[s.mode], window))
    print(" file              : %d bytes in %d packets" % (s.file_size, s.total_packets))
    print(" transfer time     : %.3f s   (%.1f KB/s)" % (s.data_time, s.throughput / 1000))
    print(" DATA packets      : %d sent = %d first transmissions + %d retransmissions"
          % (s.data_sent, s.data_sent - s.retransmissions, s.retransmissions))
    print(" timeouts          : %d" % s.timeouts)
    print(" ACKs received     : %d  (corrupt packets discarded: %d)" % (s.acks_received, s.bad_packets))
    c = s.channel
    print(" simulated network : of %d packets sent, %d dropped, %d corrupted, %d duplicated"
          % (c.sent, c.dropped, c.corrupted, c.duplicated))
    print(" integrity         : %s" % verdict)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Reliable file sender over UDP: Stop-and-Wait, Go-Back-N or Selective Repeat.")
    ap.add_argument("file", help="file to send")
    ap.add_argument("--host", default="127.0.0.1", help="receiver address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=9000, help="receiver UDP port (default 9000)")
    ap.add_argument("--mode", choices=MODES, default="gbn", help="saw | gbn | sr  (default gbn)")
    ap.add_argument("--window", type=int, default=8, help="window size N in packets, for gbn and sr (default 8)")
    ap.add_argument("--chunk", type=int, default=1024, help="payload bytes per packet (default 1024, max %d)" % MAX_PAYLOAD)
    ap.add_argument("--timeout", type=float, default=0.2, help="retransmission timeout in seconds (default 0.2)")
    ap.add_argument("--max-retries", type=int, default=50, help="give up after this many timeouts without progress")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every packet event")
    add_channel_args(ap)
    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    try:
        sender = Sender(args.file, (args.host, args.port), mode=args.mode, window=args.window,
                        chunk=args.chunk, timeout=args.timeout, max_retries=args.max_retries,
                        channel_opts=channel_opts_from_args(args, 0))
        stats = sender.run()
    except (TransferError, OSError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print_summary(stats, os.path.basename(args.file))
    return 1 if stats.receiver_ok is False else 0


if __name__ == "__main__":
    sys.exit(main())
