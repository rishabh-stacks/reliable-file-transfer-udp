"""
channel.py -- a software model of an unreliable network link.

UDP over the loopback interface almost never loses a packet, so to exercise a reliable-transfer
protocol every packet is sent through a LossyChannel instead of straight into the socket.
For each packet the channel can

    * drop it                          (--loss)
    * flip one random bit              (--corrupt)    the CRC check at the other end rejects it
    * deliver it twice                 (--duplicate)
    * hold it back for a while         (--delay, --jitter)
                                       delay  = propagation delay (makes the RTT realistic)
                                       jitter = random extra delay, which re-orders packets

Sender and receiver each wrap their own socket, so DATA packets and ACKs both cross the same
kind of bad network.  (On Linux you could get real loss with `tc qdisc ... netem loss 10%`
instead; the protocol code does not care where the loss comes from.)
"""

import heapq
import itertools
import logging
import random
import sys
import threading
import time
from dataclasses import dataclass

from protocol import describe


@dataclass
class ChannelStats:
    sent: int = 0          # packets handed to the channel
    dropped: int = 0
    corrupted: int = 0
    duplicated: int = 0


class LossyChannel:
    def __init__(self, sock, loss=0.0, corrupt=0.0, duplicate=0.0, delay=0.0, jitter=0.0,
                 seed=None, name="channel"):
        for label, p in (("loss", loss), ("corrupt", corrupt), ("duplicate", duplicate)):
            if not 0.0 <= p < 1.0:
                raise ValueError("%s probability must be in [0, 1)" % label)
        if delay < 0 or jitter < 0:
            raise ValueError("delay and jitter must be >= 0")
        self.sock = sock
        self.loss, self.corrupt, self.duplicate = loss, corrupt, duplicate
        self.delay, self.jitter = delay, jitter
        self.stats = ChannelStats()
        self.log = logging.getLogger(name)
        self._rng = random.Random(seed)
        self._queue = []                       # heap of (due_time, counter, datagram, addr)
        self._counter = itertools.count()
        self._cv = threading.Condition()
        self._closed = False
        self._thread = None
        if delay > 0 or jitter > 0:            # a helper thread releases packets when they are due
            self._thread = threading.Thread(target=self._deliver_loop, name=name + "-link", daemon=True)
            self._thread.start()

    # ------------------------------------------------------------------ public API
    def sendto(self, datagram, addr):
        """Drop-in replacement for socket.sendto() that misbehaves like a real network."""
        self.stats.sent += 1
        debug = self.log.isEnabledFor(logging.DEBUG)
        if self._rng.random() < self.loss:
            self.stats.dropped += 1
            if debug:
                self.log.debug("      ~~ network DROPPED %s", describe(datagram))
            return
        if self._rng.random() < self.corrupt:
            if debug:
                self.log.debug("      ~~ network CORRUPTED %s (1 bit flipped)", describe(datagram))
            datagram = self._flip_random_bit(datagram)
            self.stats.corrupted += 1
        copies = 1
        if self._rng.random() < self.duplicate:
            copies = 2
            self.stats.duplicated += 1
            if debug:
                self.log.debug("      ~~ network DUPLICATED %s", describe(datagram))
        for _ in range(copies):
            if self._thread is None:
                self._raw_send(datagram, addr)
            else:
                wait = self.delay + self._rng.uniform(0.0, self.jitter)
                self._schedule(time.perf_counter() + wait, datagram, addr)

    def close(self):
        """Let packets that are still 'in flight' arrive, then stop the helper thread."""
        if self._thread is not None:
            with self._cv:
                self._closed = True
                self._cv.notify_all()
            self._thread.join(timeout=self.delay + self.jitter + 1.0)

    # ------------------------------------------------------------------ internals
    def _flip_random_bit(self, datagram):
        data = bytearray(datagram)
        bit = self._rng.randrange(len(data) * 8)
        data[bit // 8] ^= 1 << (bit % 8)
        return bytes(data)

    def _raw_send(self, datagram, addr):
        try:
            self.sock.sendto(datagram, addr)
        except OSError:                        # socket closed or destination unreachable: same as a loss
            pass

    def _schedule(self, due, datagram, addr):
        with self._cv:
            heapq.heappush(self._queue, (due, next(self._counter), datagram, addr))
            self._cv.notify()

    def _deliver_loop(self):
        while True:
            with self._cv:
                while True:
                    if self._queue:
                        remaining = self._queue[0][0] - time.perf_counter()
                        if remaining <= 0:
                            _, _, datagram, addr = heapq.heappop(self._queue)
                            break
                        self._cv.wait(remaining)
                    elif self._closed:
                        return
                    else:
                        self._cv.wait()
            self._raw_send(datagram, addr)


# ---------------------------------------------------------------------- CLI helpers
def add_channel_args(parser):
    group = parser.add_argument_group("simulated network (applied to the packets THIS program sends)")
    group.add_argument("--loss", type=float, default=0.0, metavar="P", help="probability a packet is dropped")
    group.add_argument("--corrupt", type=float, default=0.0, metavar="P", help="probability a packet has one bit flipped")
    group.add_argument("--duplicate", type=float, default=0.0, metavar="P", help="probability a packet is delivered twice")
    group.add_argument("--delay", type=float, default=0.0, metavar="SEC", help="one-way propagation delay")
    group.add_argument("--jitter", type=float, default=0.0, metavar="SEC", help="random extra delay in [0, SEC] (re-orders packets)")
    group.add_argument("--seed", type=int, default=None, help="random seed, for repeatable runs")


def channel_opts_from_args(args, seed_offset=0):
    seed = None if args.seed is None else args.seed + seed_offset
    return dict(loss=args.loss, corrupt=args.corrupt, duplicate=args.duplicate,
                delay=args.delay, jitter=args.jitter, seed=seed)


def setup_logging(verbose):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(relativeCreated)9.1f ms] %(name)-8s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
