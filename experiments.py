"""
experiments.py -- run sender and receiver inside ONE process over a simulated link and measure them.

    python experiments.py demo [--loss 0.2]    compare the three protocols once (about 10 seconds)
    python experiments.py full [--reps 5]      all experiments -> results/results.csv, summary.csv, *.png
    python experiments.py plot                 redraw the plots from results/results.csv

The simulated link has a 5 ms one-way delay (RTT ~ 10 ms), the retransmission timeout is 40 ms, and the
file is 100 KB sent as 100 packets of 1000 bytes.  Loss, corruption etc. are applied independently to DATA
packets and to ACKs.  Without a realistic RTT every protocol would look equally fast on loopback.
"""

import argparse
import csv
import math
import multiprocessing
import os
import random
import statistics
import tempfile
import threading

from protocol import MODE_NAMES
from receiver import Receiver
from sender import Sender, TransferError

SIZE, CHUNK, TIMEOUT, DELAY = 100000, 1000, 0.04, 0.005
LOSSES = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40]
WINDOWS = [1, 2, 4, 8, 16, 32]
BASE_WINDOW = 8


# ---------------------------------------------------------------------------- one transfer
def run_local_transfer(mode, window=BASE_WINDOW, loss=0.0, corrupt=0.0, duplicate=0.0, delay=DELAY,
                       jitter=0.0, timeout=TIMEOUT, size=SIZE, chunk=CHUNK, seed=1, data=None, trace=False):
    """Send one file through the simulated link.  Returns (SenderStats, ReceiverStats, file_arrived_intact)."""
    if data is None:
        data = random.Random(seed).getrandbits(8 * size).to_bytes(size, "big") if size else b""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input.bin")
        with open(src, "wb") as f:
            f.write(data)
        net = dict(loss=loss, corrupt=corrupt, duplicate=duplicate, delay=delay, jitter=jitter)
        rx = Receiver(host="127.0.0.1", port=0, out_dir=os.path.join(tmp, "out"), idle_timeout=30.0,
                      linger=10 * timeout, channel_opts=dict(net, seed=seed + 1))
        stop = threading.Event()
        box = {}

        def serve():
            try:
                box["stats"] = rx.serve_one(stop)
            except Exception as exc:                    # report it in the main thread
                box["error"] = exc

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            tx = Sender(src, ("127.0.0.1", rx.port), mode=mode, window=window, chunk=chunk, timeout=timeout,
                        channel_opts=dict(net, seed=seed), trace=trace)
            sender_stats = tx.run()
        finally:
            stop.set()                                  # the sender is done: no need to linger any longer
            thread.join()
            rx.close()
        if "error" in box:
            raise box["error"]
        receiver_stats = box["stats"]
        out_path = os.path.join(rx.out_dir, "input.bin")
        intact = False
        if receiver_stats.completed and receiver_stats.sha256_ok and os.path.exists(out_path):
            with open(out_path, "rb") as f:
                intact = f.read() == data
    return sender_stats, receiver_stats, intact


# ---------------------------------------------------------------------------- demo
def cmd_demo(args):
    print("Sending %d bytes (%d packets) over a simulated link: %.0f%% loss in each direction, RTT %.0f ms, "
          "timeout %.0f ms, window N=%d\n"
          % (args.size, math.ceil(args.size / CHUNK), args.loss * 100, 2 * args.delay * 1000,
             args.timeout * 1000, args.window))
    print(" %-24s %8s %9s %10s %9s %9s   %s" % ("protocol", "time (s)", "KB/s", "DATA sent", "retrans.", "timeouts", "file"))
    for mode in ("saw", "gbn", "sr"):
        s, r, ok = run_local_transfer(mode, window=args.window, loss=args.loss, delay=args.delay,
                                      timeout=args.timeout, size=args.size, seed=args.seed)
        label = MODE_NAMES[mode] + ("" if mode == "saw" else " (N=%d)" % s.window)
        print(" %-24s %8.2f %9.1f %10d %9d %9d   %s"
              % (label, s.data_time, s.throughput / 1000, s.data_sent, s.retransmissions, s.timeouts,
                 "intact (SHA-256 OK)" if ok else "DAMAGED"))


# ---------------------------------------------------------------------------- full experiments
def build_jobs(reps):
    combos = set()
    for loss in LOSSES:                                  # experiment A: time vs loss rate
        combos.add(("saw", 1, loss))
        combos.add(("gbn", BASE_WINDOW, loss))
        combos.add(("sr", BASE_WINDOW, loss))
    for loss in (0.0, 0.10):                             # experiment B: effect of the window size
        for mode in ("gbn", "sr"):
            for window in WINDOWS:
                combos.add((mode, window, loss))
    jobs = [dict(mode=m, window=w, loss=l, rep=rep) for (m, w, l) in combos for rep in range(reps)]
    jobs.sort(key=lambda j: (j["mode"] != "saw", -j["loss"], j["window"] != 1))   # slowest first
    return jobs


def run_job(job):
    row = dict(mode=job["mode"], window=job["window"], loss=job["loss"], rep=job["rep"])
    try:
        s, r, ok = run_local_transfer(job["mode"], window=job["window"], loss=job["loss"],
                                      seed=1000 * job["rep"] + 7)
    except TransferError:
        row.update(ok=0, total_packets=0, data_time=float("nan"), throughput_kBps=0, data_sent=0,
                   retransmissions=0, timeouts=0, overhead=float("nan"))
        return row
    row.update(ok=int(ok), total_packets=s.total_packets, data_time=round(s.data_time, 4),
               throughput_kBps=round(s.throughput / 1000, 2), data_sent=s.data_sent,
               retransmissions=s.retransmissions, timeouts=s.timeouts, overhead=round(s.overhead, 3))
    return row


FIELDS = ["mode", "window", "loss", "rep", "ok", "total_packets", "data_time", "throughput_kBps",
          "data_sent", "retransmissions", "timeouts", "overhead"]
METRICS = ("data_time", "throughput_kBps", "overhead", "retransmissions", "timeouts")


def aggregate(rows):
    groups = {}
    for r in rows:
        if int(r["ok"]):
            groups.setdefault((r["mode"], int(r["window"]), float(r["loss"])), []).append(r)
    summary = {}
    for key, g in groups.items():
        entry = {"n": len(g)}
        for metric in METRICS:
            values = [float(x[metric]) for x in g]
            entry[metric] = (statistics.mean(values), statistics.stdev(values) if len(values) > 1 else 0.0)
        summary[key] = entry
    return summary


def cmd_full(args):
    os.makedirs(args.out, exist_ok=True)
    jobs = build_jobs(args.reps)
    print("running %d transfers on %d worker processes ..." % (len(jobs), args.jobs), flush=True)
    rows = []
    with multiprocessing.Pool(args.jobs) as pool:
        for i, row in enumerate(pool.imap_unordered(run_job, jobs), 1):
            rows.append(row)
            if i % 25 == 0 or i == len(jobs):
                print("  %d / %d done" % (i, len(jobs)), flush=True)
    rows.sort(key=lambda r: (r["mode"], r["window"], r["loss"], r["rep"]))
    with open(os.path.join(args.out, "results.csv"), "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    failed = [r for r in rows if not int(r["ok"])]
    print("all %d transfers delivered an intact file" % len(rows) if not failed
          else "WARNING: %d transfers failed or delivered a damaged file" % len(failed))
    finish(rows, args.out)


def cmd_plot(args):
    with open(os.path.join(args.out, "results.csv")) as f:
        rows = list(csv.DictReader(f))
    finish(rows, args.out)


def finish(rows, out_dir):
    summary = aggregate(rows)
    write_summary(summary, out_dir)
    print_tables(summary)
    make_plots(summary, rows, out_dir)
    make_trace_plot(out_dir)
    print("\nwrote results.csv, summary.csv and fig*.png to %s/" % out_dir)


def write_summary(summary, out_dir):
    with open(os.path.join(out_dir, "summary.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        header = ["mode", "window", "loss", "runs"]
        for m in METRICS:
            header += [m + "_mean", m + "_std"]
        writer.writerow(header)
        for (mode, window, loss), e in sorted(summary.items()):
            line = [mode, window, loss, e["n"]]
            for m in METRICS:
                line += ["%.4f" % e[m][0], "%.4f" % e[m][1]]
            writer.writerow(line)


def print_tables(summary):
    print("\nMean transfer time in seconds (window N=%d for gbn and sr)" % BASE_WINDOW)
    print(" %-6s %12s %12s %12s" % ("loss", "Stop-and-Wait", "Go-Back-N", "Sel. Repeat"))
    for loss in LOSSES:
        cells = []
        for mode, window in (("saw", 1), ("gbn", BASE_WINDOW), ("sr", BASE_WINDOW)):
            e = summary.get((mode, window, loss))
            cells.append("%12.2f" % e["data_time"][0] if e else "%12s" % "-")
        print(" %-6s %s" % ("%d%%" % round(loss * 100), " ".join(cells)))


# ---------------------------------------------------------------------------- plots
STYLE = {"saw": ("Stop-and-Wait", "tab:gray", "s", "-"),
         "gbn": ("Go-Back-N", "tab:blue", "o", "-"),
         "sr": ("Selective Repeat", "tab:green", "^", "--")}


def make_plots(summary, rows, out_dir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def series(mode, window, metric, losses=LOSSES):
        pts = [(l, summary[(mode, window, l)][metric]) for l in losses if (mode, window, l) in summary]
        return [p[0] * 100 for p in pts], [p[1][0] for p in pts], [p[1][1] for p in pts]

    n_packets = int(next(r["total_packets"] for r in rows if int(r["ok"])))
    plan = [("saw", 1), ("gbn", BASE_WINDOW), ("sr", BASE_WINDOW)]

    # Figure 1: transfer time vs loss rate
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for mode, window in plan:
        x, y, err = series(mode, window, "data_time")
        label = STYLE[mode][0] + ("" if mode == "saw" else " (N=%d)" % window)
        ax.errorbar(x, y, yerr=err, label=label, color=STYLE[mode][1], marker=STYLE[mode][2],
                    linestyle=STYLE[mode][3], capsize=3)
    if ("saw", 1, 0.0) in summary:                       # theory for Stop-and-Wait
        rtt = summary[("saw", 1, 0.0)]["data_time"][0] / n_packets
        xs = [l / 100.0 for l in range(0, 41)]
        ys = [n_packets * (rtt + (1 / (1 - l) ** 2 - 1) * TIMEOUT) for l in xs]
        ax.plot([l * 100 for l in xs], ys, color="black", linestyle=":", linewidth=1.2,
                label="Stop-and-Wait, theory")
    ax.set_xlabel("packet loss rate in each direction (%)")
    ax.set_ylabel("transfer time of a 100 KB file (s)")
    ax.set_title("Transfer time vs loss rate (RTT 10 ms, timeout 40 ms)")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "fig1_time_vs_loss.png"), dpi=150)
    plt.close(fig)

    # Figure 2: retransmission overhead vs loss rate
    fig, ax = plt.subplots(figsize=(7, 4.5))
    for mode, window in plan:
        x, y, err = series(mode, window, "overhead")
        label = STYLE[mode][0] + ("" if mode == "saw" else " (N=%d)" % window)
        if mode == "saw":
            label += " (hidden under Selective Repeat:\nboth resend only what was lost)"
        ax.errorbar(x, y, yerr=err, label=label, color=STYLE[mode][1], marker=STYLE[mode][2],
                    linestyle=STYLE[mode][3], capsize=3)
    ax.set_xlabel("packet loss rate in each direction (%)")
    ax.set_ylabel("DATA packets sent / packets in the file")
    ax.set_title("Cost of recovery: packets sent per packet of the file")
    ax.grid(alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "fig2_overhead_vs_loss.png"), dpi=150)
    plt.close(fig)

    # Figure 3: throughput vs window size
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.3))
    for ax, loss in zip(axes, (0.0, 0.10)):
        for mode in ("gbn", "sr"):
            pts = [(w, summary[(mode, w, loss)]["throughput_kBps"]) for w in WINDOWS if (mode, w, loss) in summary]
            ax.errorbar([p[0] for p in pts], [p[1][0] for p in pts], yerr=[p[1][1] for p in pts],
                        label=STYLE[mode][0], color=STYLE[mode][1], marker=STYLE[mode][2],
                        linestyle=STYLE[mode][3], capsize=3)
        ax.set_xscale("log", base=2)
        ax.set_xticks(WINDOWS)
        ax.set_xticklabels([str(w) for w in WINDOWS])
        ax.set_xlabel("window size N (packets)")
        ax.set_title("%d%% loss" % round(loss * 100))
        ax.grid(alpha=0.3)
        ax.legend()
    for ax in axes:
        ax.set_ylabel("throughput (KB/s)")
    fig.suptitle("Throughput vs window size (N=1 is Stop-and-Wait)")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "fig3_throughput_vs_window.png"), dpi=150)
    plt.close(fig)


def make_trace_plot(out_dir, loss=0.20):
    """Figure 4: when was each packet sent?  One real run per protocol, with 20% loss."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(3, 1, figsize=(8, 9))
    for ax, mode in zip(axes, ("saw", "gbn", "sr")):
        s, _, ok = run_local_transfer(mode, window=BASE_WINDOW, loss=loss, seed=11, trace=True)
        first = [(t, q) for t, q, re in s.trace if not re]
        again = [(t, q) for t, q, re in s.trace if re]
        ax.scatter([p[0] for p in first], [p[1] for p in first], s=10, color="tab:blue", label="first transmission")
        ax.scatter([p[0] for p in again], [p[1] for p in again], s=22, color="tab:red", marker="x", label="retransmission")
        label = MODE_NAMES[mode] + ("" if mode == "saw" else " (N=%d)" % BASE_WINDOW)
        ax.set_title("%s: %.2f s, %d retransmissions, %d timeouts" % (label, s.data_time, s.retransmissions, s.timeouts),
                     fontsize=10)
        ax.set_ylabel("packet number")
        ax.grid(alpha=0.3)
        ax.legend(loc="upper left", fontsize=8)
    axes[-1].set_xlabel("time since the first DATA packet (s)")
    fig.suptitle("When each packet was sent (%.0f%% loss in each direction)" % (loss * 100))
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "fig4_packet_trace.png"), dpi=150)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="compare Stop-and-Wait, Go-Back-N and Selective Repeat once")
    demo.add_argument("--loss", type=float, default=0.20)
    demo.add_argument("--window", type=int, default=BASE_WINDOW)
    demo.add_argument("--size", type=int, default=SIZE, help="file size in bytes")
    demo.add_argument("--delay", type=float, default=DELAY, help="one-way delay in seconds")
    demo.add_argument("--timeout", type=float, default=TIMEOUT)
    demo.add_argument("--seed", type=int, default=1)
    demo.set_defaults(func=cmd_demo)

    full = sub.add_parser("full", help="run every experiment, write CSV files and plots")
    full.add_argument("--reps", type=int, default=5, help="repetitions per setting (default 5)")
    full.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="parallel worker processes")
    full.add_argument("--out", default="results")
    full.set_defaults(func=cmd_full)

    plot = sub.add_parser("plot", help="redraw the plots from an existing results.csv")
    plot.add_argument("--out", default="results")
    plot.set_defaults(func=cmd_plot)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
