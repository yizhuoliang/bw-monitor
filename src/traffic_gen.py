#!/usr/bin/env python3
"""
IB Traffic Generator — controlled interference via ucx_perftest UCP put_bw.

Usage:
    python3 traffic_gen.py --size 512 --concurrent 2 --gap 2.0 [--rounds 0] [--duration 60]

    --size        Per-transmission size in MiB (default: 512)
    --concurrent  Number of simultaneous src-dst pairs per round (default: 2)
    --gap         Seconds to wait between rounds (default: 2.0)
    --rounds      Number of rounds (0 = unlimited, stopped by --duration)
    --duration    Max runtime in seconds (default: 60)
"""

import argparse
import os
import random
import subprocess
import sys
import signal
import time
from datetime import datetime
from itertools import permutations

NODES = [
    ("b04-13", "10.125.137.190"),
    ("b05-12", "10.125.137.210"),
    ("b05-14", "10.125.137.212"),
    ("b10-14", "10.125.138.4"),
]

UCX_DIR  = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6"
GCC_LIB  = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/gcc-runtime-13.3.0-cm7m2ub/lib"
UCX_BIN  = f"{UCX_DIR}/bin/ucx_perftest"
ENV_PREFIX = f"LD_LIBRARY_PATH={UCX_DIR}/lib:{GCC_LIB}:$LD_LIBRARY_PATH UCX_WARN_UNUSED_ENV_VARS=n"

NUMA_CPU     = "32"
MSG_SIZE     = 4 * 1024 * 1024
SERVER_PORT  = 18600

SSH_SOCK = "/tmp/tgen-ssh-%h"
SSH_BASE = "-o StrictHostKeyChecking=no -o ConnectTimeout=5"

_cm_procs = []

ALL_PAIRS = [(s, d) for s in NODES for d in NODES if s[0] != d[0]]


def _ssh_sock(node):
    return f"/tmp/tgen-ssh-{node}"


def _ssh(node):
    return f"ssh -S {_ssh_sock(node)} {SSH_BASE} {node}"


def setup_ssh():
    import glob as _g
    for f in _g.glob("/tmp/tgen-ssh-*"):
        try: os.unlink(f)
        except OSError: pass
    for name, _ in NODES:
        p = subprocess.Popen(
            ["ssh", "-M", "-N",
             "-S", _ssh_sock(name),
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=30",
             name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _cm_procs.append(p)
    time.sleep(3)
    print("[tgen] SSH ControlMaster ready")


def cleanup_ssh():
    for name, _ in NODES:
        subprocess.run(
            f"ssh -S {_ssh_sock(name)} {SSH_BASE} -O exit {name} 2>/dev/null",
            shell=True, timeout=5, capture_output=True,
        )
    for p in _cm_procs:
        p.terminate()
        try: p.wait(timeout=3)
        except subprocess.TimeoutExpired: p.kill()


def start_servers():
    for name, _ in NODES:
        cmd = (
            f'{_ssh(name)} "nohup bash -c \''
            f"while true; do "
            f"  {ENV_PREFIX} taskset -c {NUMA_CPU} {UCX_BIN}"
            f"    -t ucp_put_bw -s {MSG_SIZE} -n 999999"
            f"    -p {SERVER_PORT} 2>/dev/null;"
            f"  sleep 0.05;"
            f"done"
            f"' >/dev/null 2>&1 &\""
        )
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    print("[tgen] Servers started on all nodes")


def stop_servers():
    for name, _ in NODES:
        subprocess.run(
            f'{_ssh(name)} "pkill -f \'ucx_perftest.*-p {SERVER_PORT}\' 2>/dev/null"',
            shell=True, capture_output=True, timeout=5,
        )
    time.sleep(0.5)


def run_transfer(src_name, dst_ip, n_iters):
    cmd = (
        f"{_ssh(src_name)} '"
        f"{ENV_PREFIX} taskset -c {NUMA_CPU} {UCX_BIN}"
        f" {dst_ip} -t ucp_put_bw -s {MSG_SIZE} -n {n_iters}"
        f" -w 0 -p {SERVER_PORT} 2>/dev/null'"
    )
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def pick_pairs(n):
    n = min(n, len(ALL_PAIRS))
    return random.sample(ALL_PAIRS, n)


def main():
    parser = argparse.ArgumentParser(description="IB traffic generator")
    parser.add_argument("--size", type=int, default=512, help="Per-transmission size in MiB")
    parser.add_argument("--concurrent", type=int, default=2, help="Simultaneous transmissions per round")
    parser.add_argument("--gap", type=float, default=2.0, help="Seconds between rounds")
    parser.add_argument("--rounds", type=int, default=0, help="Number of rounds (0 = unlimited)")
    parser.add_argument("--duration", type=float, default=60, help="Max runtime in seconds")
    args = parser.parse_args()

    n_iters = max(1, (args.size * 1024 * 1024) // MSG_SIZE)
    total_mib = n_iters * MSG_SIZE / (1024 * 1024)

    print(f"[tgen] Traffic Generator")
    print(f"  size={args.size} MiB ({n_iters} × {MSG_SIZE // (1024*1024)} MiB msgs)")
    print(f"  concurrent={args.concurrent}  gap={args.gap}s")
    print(f"  rounds={'unlimited' if args.rounds == 0 else args.rounds}  max_duration={args.duration}s")

    setup_ssh()
    start_servers()

    running = True
    def on_sig(s, f):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    t0 = time.time()
    rnd = 0

    try:
        while running and (time.time() - t0) < args.duration:
            if args.rounds > 0 and rnd >= args.rounds:
                break
            rnd += 1
            pairs = pick_pairs(args.concurrent)
            pair_strs = [f"{sn}→{dn}" for (sn, _), (dn, _) in pairs]
            print(f"\n── Round {rnd}  {datetime.now().strftime('%H:%M:%S')}  {', '.join(pair_strs)} ──")

            procs = []
            ts = time.time()
            for (src_name, _), (_, dst_ip) in pairs:
                procs.append(run_transfer(src_name, dst_ip, n_iters))

            for p in procs:
                p.wait()

            elapsed = time.time() - ts
            rate = total_mib / elapsed if elapsed > 0 else 0
            print(f"  {total_mib:.0f} MiB × {len(pairs)} in {elapsed:.2f}s ({rate:.0f} MiB/s per transfer)")

            if running and (time.time() - t0) < args.duration:
                time.sleep(args.gap)
    finally:
        stop_servers()
        cleanup_ssh()
        print(f"[tgen] Done. {rnd} rounds in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
