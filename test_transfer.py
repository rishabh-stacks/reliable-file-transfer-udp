"""
test_transfer.py -- automated checks.  Run with:   python test_transfer.py -v

The important one: for every protocol the received file must be byte-for-byte identical to the
original, no matter how badly the simulated network behaves.
"""

import hashlib
import os
import socket
import tempfile
import threading
import unittest

from experiments import run_local_transfer
from protocol import ACK, DATA, FIN, FIN_ACK, START, START_ACK, decode, encode, encode_meta
from receiver import Receiver
from sender import Sender, TransferError

MODES = ("saw", "gbn", "sr")
FAST = dict(delay=0.0, timeout=0.03, size=30000, chunk=1000)      # 30 packets, no propagation delay


class CodecTests(unittest.TestCase):
    def test_roundtrip(self):
        pkt = decode(encode(DATA, 12345, b"hello"))
        self.assertEqual((pkt.ptype, pkt.seq, pkt.payload), (DATA, 12345, b"hello"))

    def test_every_single_bit_flip_is_detected(self):
        wire = encode(DATA, 7, bytes(range(200)))
        for bit in range(len(wire) * 8):
            damaged = bytearray(wire)
            damaged[bit // 8] ^= 1 << (bit % 8)
            self.assertIsNone(decode(bytes(damaged)), "flip of bit %d went unnoticed" % bit)

    def test_garbage_is_rejected(self):
        for junk in (b"", b"abc", encode(ACK, 1)[:-1], encode(DATA, 1, b"xyz") + b"extra"):
            self.assertIsNone(decode(junk))


class TransferTests(unittest.TestCase):
    def check(self, mode, **overrides):
        params = dict(FAST)
        params.update(overrides)
        sender, receiver, intact = run_local_transfer(mode, **params)
        self.assertTrue(intact, "%s: file arrived damaged or incomplete" % mode)
        self.assertTrue(sender.receiver_ok, "%s: receiver did not confirm the SHA-256" % mode)
        return sender, receiver

    def test_clean_network(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                sender, _ = self.check(mode)
                self.assertEqual(sender.retransmissions, 0)
                self.assertEqual(sender.data_sent, 30)

    def test_packet_loss(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                sender, _ = self.check(mode, loss=0.2, seed=3)
                self.assertGreater(sender.retransmissions, 0)

    def test_heavy_loss(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.check(mode, loss=0.4, seed=5, size=10000)

    def test_bit_errors_are_caught_by_the_crc(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                sender, receiver = self.check(mode, corrupt=0.15, seed=4)
                self.assertGreater(sender.bad_packets + receiver.bad_packets, 0)

    def test_duplicates_and_reordering(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.check(mode, duplicate=0.2, delay=0.003, jitter=0.012, timeout=0.08, seed=6)

    def test_everything_at_once(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                self.check(mode, loss=0.15, corrupt=0.1, duplicate=0.1, delay=0.003, jitter=0.01,
                           timeout=0.08, seed=8)

    def test_odd_file_sizes(self):
        for size in (0, 1, 999, 1000, 1001, 5000):
            for mode in MODES:
                with self.subTest(size=size, mode=mode):
                    data = bytes((i * 7 + 3) % 256 for i in range(size))
                    self.check(mode, loss=0.1, seed=size + 1, data=data)

    def test_window_of_one_behaves_like_stop_and_wait(self):
        for mode in ("gbn", "sr"):
            with self.subTest(mode=mode):
                self.check(mode, window=1, loss=0.1, seed=2)

    def test_window_larger_than_the_file(self):
        for mode in ("gbn", "sr"):
            with self.subTest(mode=mode):
                self.check(mode, window=100, loss=0.1, seed=2)

    def test_sender_gives_up_when_nobody_is_listening(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()                                       # nobody listens on this port any more
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.bin")
            with open(path, "wb") as f:
                f.write(b"data")
            with self.assertRaises(TransferError):
                Sender(path, ("127.0.0.1", port), timeout=0.02, max_retries=3).run()

    def test_receiver_cannot_be_tricked_into_writing_outside_its_folder(self):
        cases = {"../../evil.txt": "evil.txt", "..\\..\\evil.txt": "evil.txt", "..": "received.bin",
                 "a\x00b.txt": "ab.txt", "": "received.bin"}
        for hostile, saved_as in cases.items():
            with self.subTest(name=hostile):
                with tempfile.TemporaryDirectory() as tmp:
                    out_dir = os.path.join(tmp, "out")
                    rx = Receiver(host="127.0.0.1", port=0, out_dir=out_dir, linger=0.05)
                    thread = threading.Thread(target=rx.serve_one, daemon=True)
                    thread.start()
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.settimeout(3)
                    addr = ("127.0.0.1", rx.port)
                    meta = dict(name=hostile, size=0, chunk=1000, total=0, mode="gbn", window=1,
                                sha256=hashlib.sha256(b"").hexdigest())
                    sock.sendto(encode(START, 0, encode_meta(meta)), addr)
                    self.assertEqual(decode(sock.recvfrom(2048)[0]).ptype, START_ACK)
                    sock.sendto(encode(FIN, 0), addr)
                    self.assertEqual(decode(sock.recvfrom(2048)[0]).ptype, FIN_ACK)
                    thread.join(5)
                    rx.close()
                    sock.close()
                    self.assertEqual(os.listdir(out_dir), [saved_as])
                    self.assertEqual(os.listdir(tmp), ["out"])         # nothing escaped to the parent folder


if __name__ == "__main__":
    unittest.main()
