#!/usr/bin/env python3
"""
IB Bandwidth Monitor — Live Plotter

Reads JSONL logs produced by bw_controller.py and generates a per-pair
bandwidth timeline plot.  Runs in a loop (default every 60 s) or once
with --once.
"""

import json
import os
import sys
import time
import glob
from datetime import datetime
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

LOG_DIR   = "/scratch1/yizhuoli/bw-monitor/logs"
PLOT_DIR  = "/scratch1/yizhuoli/bw-monitor/plots"
PLOT_FILE = os.path.join(PLOT_DIR, "bw_timeline.png")
INTERVAL  = 60  # seconds between plot refreshes

def read_all_logs():
    records = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bw_*.jsonl"))):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return records

def build_series(records):
    """Group records by directed pair → {label: (times, bws)}."""
    series = defaultdict(lambda: {"t": [], "bw": []})
    for r in records:
        bw = r.get("bw_Gbps")
        if bw is None:
            continue
        label = f"{r['src']} → {r['dst']}"
        try:
            t = datetime.fromisoformat(r["ts"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        series[label]["t"].append(t)
        series[label]["bw"].append(bw)
    return series

def plot(series, output_path):
    if not series:
        print("[plot] No data yet")
        return

    fig, ax = plt.subplots(figsize=(18, 9))

    for label in sorted(series):
        d = series[label]
        ax.plot(d["t"], d["bw"], label=label, alpha=0.7, linewidth=1.0)

    ax.axhline(y=200, color="red", linestyle="--", linewidth=0.8, alpha=0.4)
    ax.set_xlabel("Time (UTC)")
    ax.set_ylabel("Bandwidth (Gbps)")
    ax.set_title("InfiniBand Pair-wise Bandwidth Over Time")
    ax.set_ylim(0, 210)
    ax.legend(loc="lower left", fontsize=7, ncol=4)
    ax.grid(True, alpha=0.25)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"[plot] Saved {output_path}  ({sum(len(d['bw']) for d in series.values())} points)")

def main():
    os.makedirs(PLOT_DIR, exist_ok=True)
    once = "--once" in sys.argv

    while True:
        records = read_all_logs()
        series  = build_series(records)
        plot(series, PLOT_FILE)
        if once:
            break
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
