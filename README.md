# Reliable File Transfer over UDP (CSN 303, mini project 4)

A file is sent over plain UDP sockets and arrives byte-for-byte intact even when the network
drops, corrupts, duplicates or re-orders packets. The reliability (sequence numbers, ACKs,
timeouts, retransmission, sliding window) is written from scratch on top of UDP. Three
protocols are implemented: **Stop-and-Wait**, **Go-Back-N** and **Selective Repeat**.

## Files

| file | what it does |
|---|---|
| `protocol.py` | packet format (11-byte header + CRC-32), `encode` / `decode` |
| `channel.py` | `LossyChannel`: simulates loss, bit errors, duplicates, delay and re-ordering |
| `sender.py` | the three sender protocols, plus the command-line tool |
| `receiver.py` | the receiver (cumulative ACKs for saw/gbn, buffered selective ACKs for sr), plus CLI |
| `experiments.py` | runs sender and receiver in one process, `demo` / `full` experiments, plots |
| `test_transfer.py` | 14 automated tests (loss, corruption, duplicates, odd file sizes, hostile file names, ...) |
| `results/` | `results.csv` (all 190 runs), `summary.csv`, `fig1..fig4*.png`, `sample_logs/` |

Needs Python 3.8+ and nothing else. Only the plots need `pip install matplotlib`.
No root access, no Linux-only features: everything runs on one machine over loopback.

## Quick start

```bash
python test_transfer.py -v                 # all tests should say OK
python experiments.py demo --loss 0.2      # the three protocols side by side, about 10 s
```

Real two-terminal run (this is what to screenshot):

```bash
python -c "import os; open('sample.bin','wb').write(os.urandom(20000))"     # make a test file

# terminal 1
python receiver.py --port 9000 --loss 0.2 --once -v

# terminal 2
python sender.py sample.bin --port 9000 --mode gbn --window 4 --timeout 0.1 --loss 0.2 -v
```

* `--mode saw | gbn | sr` picks the protocol. The receiver learns it from the sender, so it needs no option.
* `--loss 0.2` is given to **both** programs: the sender's network drops 20% of DATA packets, the
  receiver's network drops 20% of ACKs. Use `--corrupt`, `--duplicate`, `--delay`, `--jitter`
  for other kinds of damage and `--seed N` for a repeatable run.
* Without `-v` you only get the final summary. With `-v` you see every send, ACK, drop, timeout and resend.
* The received file lands in `./received/`. Both programs verify the SHA-256; to check it yourself use
  `sha256sum` (Linux), `shasum -a 256` (macOS) or `certutil -hashfile FILE SHA256` (Windows).
* Two machines: run the receiver on one, give the sender `--host <receiver IP>`, and allow UDP port 9000 in the firewall.

Regenerate all results and plots (about 2 minutes):

```bash
python experiments.py full --reps 5
```

## How it works

**Packet** (every UDP datagram holds one):

```
| type 1B | sequence number 4B | payload length 2B | CRC-32 4B | payload 0..1400 B |
```

The CRC-32 covers header and payload. A packet that fails the check is silently discarded, so
corruption becomes loss, which the retransmission machinery already handles. (CRC-32 detects every
single-bit error; `test_transfer.py` flips each bit of a packet in turn and checks this.)
Sequence numbers count packets, not bytes, and never wrap around.

**Session**: `START` (file name, size, protocol, window, SHA-256) -> `START-ACK` -> `DATA`/`ACK` ... ->
`FIN` -> `FIN-ACK` (1 byte: did the SHA-256 match). START and FIN are retransmitted until answered.
After sending FIN-ACK the receiver lingers briefly so it can answer a repeated FIN if the FIN-ACK was
lost (the same reason TCP has TIME-WAIT).

| | Stop-and-Wait | Go-Back-N | Selective Repeat |
|---|---|---|---|
| packets in flight | 1 | up to N | up to N |
| receiver | accepts only the next packet | accepts only the next packet, discards the rest | buffers packets inside its window |
| ACK meaning | `ACK n`: all packets below n arrived | same (cumulative) | `ACK n`: packet n arrived |
| sender timers | 1 | 1 (oldest unACKed packet) | 1 per packet |
| on timeout | resend that packet | resend the whole window from the oldest packet | resend only the packet that timed out |

The sender gives up (and says so) if there is no progress for `--max-retries` consecutive timeouts
(default 50), so a dead receiver cannot hang it forever. The receiver uses only the base name from
the START packet, never a path, so a sender cannot make it write outside `--out-dir`.

**Simulated network.** Loopback UDP almost never loses packets, so each program sends through a
`LossyChannel` that drops / corrupts / duplicates / delays packets with the probabilities you give.
A 5 ms one-way delay makes the RTT about 10 ms; without any delay every protocol would look equally
fast and window size would not matter. (Optional on Linux: real loss with
`sudo tc qdisc add dev lo root netem loss 10% delay 5ms`, then run without `--loss`;
remove it with `sudo tc qdisc del dev lo root`.)

## Results

100 KB file = 100 packets, RTT 10 ms, timeout 40 ms, window N = 8, loss applied to DATA and ACKs,
mean of 5 runs per setting. All 190 transfers delivered an intact file.

| loss | Stop-and-Wait | Go-Back-N | Selective Repeat |
|---|---|---|---|
| 0 % | 1.06 s | 0.14 s | 0.14 s |
| 5 % | 1.51 s | 0.41 s | 0.41 s |
| 10 % | 2.10 s | 0.62 s | 0.64 s |
| 20 % | 3.63 s | 1.32 s | 1.10 s |
| 30 % | 5.90 s | 1.98 s | 1.84 s |
| 40 % | 8.74 s | 3.18 s | 2.48 s |

* **Pipelining is the big win.** With no loss, N = 8 is 7.5x faster than Stop-and-Wait, and throughput
  grows with the window: 95 KB/s (N=1), 726 KB/s (N=8), 2383 KB/s (N=32) (`fig3`).
* **Stop-and-Wait matches a simple model**: time = packets x (RTT + (1/q - 1) x timeout) with
  q = (1 - loss)^2, the chance that both the packet and its ACK get through (dotted line in `fig1`).
  Measured times are 1-13% above it.
* **Go-Back-N vs Selective Repeat.** Similar up to 10% loss at N=8; Selective Repeat is 22% faster at
  40% loss. The gap grows with the window: at 10% loss and N=32, SR reaches 375 KB/s and GBN only
  182 KB/s. GBN also sends far more packets: 2.97 vs 1.64 per file packet at 20% loss, 6.2 vs 2.9 at
  40% (`fig2`). `fig4` shows why: GBN resends whole windows (red columns), SR only the lost packets.
* **A caveat worth knowing for the viva:** at small windows (N = 2 to 4, 10% loss) GBN was slightly
  *faster* than SR (92 vs 72 KB/s at N=2), because one cumulative ACK covers earlier lost ACKs, while SR
  needs every individual ACK. Also, the simulated link has unlimited bandwidth, so GBN's extra packets
  cost no time by themselves; on a real bottleneck link they would slow things down further.

## What to put in the submission

1. Screenshot of `python test_transfer.py -v` ending in `OK`.
2. Screenshot of `python experiments.py demo --loss 0.2`.
3. Screenshots of the two-terminal run for each protocol, showing the `DROPPED` and `TIMEOUT` lines and
   both final summaries (SHA-256 OK). `results/sample_logs/` shows what yours should look like.
4. `fig1` to `fig4` in the Results section of the report.
5. Optional: Wireshark on the loopback interface with filter `udp.port == 9000`; the first byte of
   each UDP payload is the packet type (3 = DATA, 4 = ACK, 1 = START, 5 = FIN).

On Windows the system timer can be coarse (around 15 ms), which blurs the 5 ms simulated delay. If the
numbers look odd, use larger values, e.g. `python experiments.py demo --delay 0.02 --timeout 0.15`.
