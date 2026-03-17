#!/usr/bin/env python3
"""
IB traffic generator — controlled RDMA interference via ucx_perftest.

Randomly picks src-dst pairs each round, fires concurrent transfers, waits,
repeats. Requires ucx_perftest on all nodes (via PATH or --ucx-perftest).

Example:
    python3 traffic_gen.py \\
        --nodes node0:10.0.0.1,node1:10.0.0.2,node2:10.0.0.3 \\
        --size 512 --concurrent 2 --gap 2.0 --duration 60
"""

import argparse
import glob
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime

MSG_CHUNK = 4 * 1024 * 1024
SSH_OPTS = "-o StrictHostKeyChecking=no -o ConnectTimeout=5"

_cm_procs = []


def parse_nodes(raw):
    nodes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" not in entry:
            sys.exit(f"bad node format '{entry}', expected hostname:ib_ip")
        host, ip = entry.split(":", 1)
        nodes.append((host.strip(), ip.strip()))
    if len(nodes) < 2:
        sys.exit("need at least 2 nodes")
    return nodes


def _sock(host):
    return f"/tmp/tgen-ssh-{host}"


def _ssh(host):
    return f"ssh -S {_sock(host)} {SSH_OPTS} {host}"


def setup_ssh(nodes):
    for f in glob.glob("/tmp/tgen-ssh-*"):
        try:
            os.unlink(f)
        except OSError:
            pass
    for host, _ in nodes:
        p = subprocess.Popen(
            ["ssh", "-M", "-N", "-S", _sock(host),
             "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=30",
             host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _cm_procs.append(p)
    time.sleep(3)


def cleanup_ssh(nodes):
    for host, _ in nodes:
        subprocess.run(
            f"ssh -S {_sock(host)} {SSH_OPTS} -O exit {host} 2>/dev/null",
            shell=True, timeout=5, capture_output=True,
        )
    for p in _cm_procs:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()


def remote_cmd(host, cmd, env_cmd, cpu):
    prefix = f"{env_cmd} && " if env_cmd else ""
    pin = f"taskset -c {cpu} " if cpu else ""
    return f"{_ssh(host)} '{prefix}{pin}{cmd}'"


def start_servers(nodes, ucx_bin, port, env_cmd, cpu):
    for host, _ in nodes:
        inner = (
            f"while true; do {ucx_bin} -t ucp_put_bw -s {MSG_CHUNK}"
            f" -n 999999 -p {port} 2>/dev/null; sleep 0.05; done"
        )
        cmd = remote_cmd(host, f"nohup bash -c \\'{inner}\\' >/dev/null 2>&1 &", env_cmd, cpu)
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)


def stop_servers(nodes, port):
    for host, _ in nodes:
        subprocess.run(
            f'{_ssh(host)} "pkill -f \'ucx_perftest.*-p {port}\' 2>/dev/null"',
            shell=True, capture_output=True, timeout=5,
        )


def run_transfer(src_host, dst_ip, ucx_bin, n_iters, port, env_cmd, cpu):
    cmd = remote_cmd(
        src_host,
        f"{ucx_bin} {dst_ip} -t ucp_put_bw -s {MSG_CHUNK} -n {n_iters} -w 0 -p {port} 2>/dev/null",
        env_cmd, cpu,
    )
    return subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def main():
    p = argparse.ArgumentParser(
        description="IB traffic generator using ucx_perftest RDMA transfers",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n  %(prog)s --nodes n0:10.0.0.1,n1:10.0.0.2 --size 512 --concurrent 2 --gap 2",
    )
    p.add_argument("--nodes", required=True,
                   help="comma-separated host:ib_ip list (e.g. node0:10.0.0.1,node1:10.0.0.2)")
    p.add_argument("--size", type=int, default=512, help="per-transmission size in MiB (default: 512)")
    p.add_argument("--concurrent", type=int, default=2, help="simultaneous transfers per round (default: 2)")
    p.add_argument("--gap", type=float, default=2.0, help="seconds between rounds (default: 2.0)")
    p.add_argument("--rounds", type=int, default=0, help="max rounds, 0=unlimited (default: 0)")
    p.add_argument("--duration", type=float, default=60, help="max runtime in seconds (default: 60)")
    p.add_argument("--port", type=int, default=18600, help="ucx_perftest server port (default: 18600)")
    p.add_argument("--cpu", default=None, help="pin to CPU core via taskset (e.g. 32)")
    p.add_argument("--ucx-perftest", default="ucx_perftest", dest="ucx_bin",
                   help="path to ucx_perftest on remote nodes (default: ucx_perftest from PATH)")
    p.add_argument("--env", default=None, dest="env_cmd",
                   help="shell command to run on remote nodes before ucx_perftest "
                        '(e.g. "module load ucx/1.16.0")')
    args = p.parse_args()

    nodes = parse_nodes(args.nodes)
    all_pairs = [(s, d) for s in nodes for d in nodes if s[0] != d[0]]

    n_iters = max(1, (args.size * 1024 * 1024) // MSG_CHUNK)
    total_mib = n_iters * MSG_CHUNK / (1024 * 1024)

    print(f"[tgen] Traffic Generator")
    print(f"  nodes={[h for h, _ in nodes]}  size={args.size} MiB  concurrent={args.concurrent}")
    print(f"  gap={args.gap}s  port={args.port}  duration={args.duration}s")

    setup_ssh(nodes)
    start_servers(nodes, args.ucx_bin, args.port, args.env_cmd, args.cpu)
    print("[tgen] ready")

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
            k = min(args.concurrent, len(all_pairs))
            pairs = random.sample(all_pairs, k)
            labels = ", ".join(f"{s}→{d}" for (s, _), (d, _) in pairs)
            print(f"\n── Round {rnd}  {datetime.now().strftime('%H:%M:%S')}  {labels} ──")

            ts = time.time()
            procs = [
                run_transfer(sh, di, args.ucx_bin, n_iters, args.port, args.env_cmd, args.cpu)
                for (sh, _), (_, di) in pairs
            ]
            for proc in procs:
                proc.wait()

            elapsed = time.time() - ts
            rate = total_mib / elapsed if elapsed > 0 else 0
            print(f"  {total_mib:.0f} MiB x{len(pairs)} in {elapsed:.2f}s ({rate:.0f} MiB/s ea)")

            if running and (time.time() - t0) < args.duration:
                time.sleep(args.gap)
    finally:
        stop_servers(nodes, args.port)
        cleanup_ssh(nodes)
        print(f"\n[tgen] done. {rnd} rounds in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
