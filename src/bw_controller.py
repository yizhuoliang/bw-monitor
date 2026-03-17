#!/usr/bin/env python3
"""
IB Bandwidth Monitor — Centralized Controller

Starts persistent ucx_perftest servers (while-true loop) on each node,
then sequentially measures all directed pairs every ROUND_INTERVAL seconds.
Logs JSONL to /scratch1/yizhuoli/bw-monitor/logs/.

NUMA pinning: -c flag pins to NUMA node 1 CPUs where mlx5_0 resides.
Transport: UCP layer auto-selects RDMA (mlx5_0) for data path.
"""

import subprocess
import time
import json
import os
import re
import sys
import signal
from datetime import datetime, timezone

# ── Cluster topology ──
NODES = [
    ("b04-13", "10.125.137.190"),
    ("b05-12", "10.125.137.210"),
    ("b05-14", "10.125.137.212"),
    ("b10-14", "10.125.138.4"),
]

# ── UCX paths (avoids 'module load' overhead per SSH call) ──
UCX_DIR = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6"
UCX_BIN = f"{UCX_DIR}/bin/ucx_perftest"
UCX_LIB = f"{UCX_DIR}/lib"
GCC_LIB = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/gcc-runtime-13.3.0-cm7m2ub/lib"
ENV_PREFIX = f"LD_LIBRARY_PATH={UCX_LIB}:{GCC_LIB}:$LD_LIBRARY_PATH UCX_WARN_UNUSED_ENV_VARS=n"

# ── Measurement config ──
MSG_SIZE   = 4 * 1024 * 1024   # 4 MiB per RDMA write
N_ITERS    = 200               # ~35 ms of data at line rate
WARMUP     = 10
SERVER_PORT = 18515
NUMA_CPU   = "32"              # CPU on NUMA node 1 (IB NIC node)
ROUND_INTERVAL = 1.0           # seconds between rounds

LOG_DIR = "/scratch1/yizhuoli/bw-monitor/logs"
SSH_BASE = "-o StrictHostKeyChecking=no -o ConnectTimeout=5"
_cm_procs = []

def _ssh_sock(node):
    return f"/tmp/bw-mon-ssh-{node}"

def _ssh(node):
    return f"ssh -S {_ssh_sock(node)} {SSH_BASE} {node}"

def ssh_run(node, cmd, timeout=15):
    full = f"{_ssh(node)} '{cmd}'"
    try:
        r = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    except subprocess.TimeoutExpired:
        return ""

def ssh_bg(node, cmd):
    full = f'{_ssh(node)} "{cmd}"'
    subprocess.Popen(full, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def setup_ssh():
    import glob as _g
    for f in _g.glob("/tmp/bw-mon-ssh-*"):
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
    print("[ctrl] SSH ControlMaster ready")

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

# ── Server lifecycle ──

def start_servers():
    """Launch persistent ucx_perftest server loop on every node."""
    for name, _ in NODES:
        ssh_bg(name,
            f"nohup bash -c '"
            f"while true; do "
            f"  {ENV_PREFIX} taskset -c {NUMA_CPU} {UCX_BIN}"
            f"    -t ucp_put_bw -s {MSG_SIZE} -n {N_ITERS} -w {WARMUP}"
            f"    -p {SERVER_PORT} 2>/dev/null;"
            f"  sleep 0.05;"
            f"done"
            f"' >/dev/null 2>&1 &"
        )
    time.sleep(1.5)  # let first server instance bind
    print("[ctrl] Persistent servers started on all nodes")

def stop_servers():
    for name, _ in NODES:
        ssh_bg(name, "pkill -f ucx_perftest 2>/dev/null")
    time.sleep(0.5)
    print("[ctrl] Servers stopped")

# ── Measurement ──

def parse_bw(output):
    """Extract average bandwidth (MB/s) from ucx_perftest Final: line."""
    for line in output.split("\n"):
        if "Final" in line:
            parts = line.split()
            try:
                return float(parts[5])   # avg bandwidth MB/s
            except (IndexError, ValueError):
                pass
    return None

def measure(src_name, dst_ip):
    """Run ucx_perftest client on src → dst, return bandwidth in MB/s or None."""
    cmd = (
        f"{ENV_PREFIX} taskset -c {NUMA_CPU} {UCX_BIN}"
        f" {dst_ip} -t ucp_put_bw -s {MSG_SIZE} -n {N_ITERS} -w {WARMUP}"
        f" -p {SERVER_PORT} 2>/dev/null"
    )
    out = ssh_run(src_name, cmd, timeout=15)
    return parse_bw(out)

# ── Main loop ──

def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 300

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"bw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")

    # All directed pairs, ordered so consecutive dst nodes differ
    pairs = []
    node_names = [n for n, _ in NODES]
    for i, (sn, si) in enumerate(NODES):
        for dn, di in NODES:
            if sn != dn:
                pairs.append((sn, si, dn, di))

    print(f"[ctrl] IB BW Monitor | {duration}s | {len(pairs)} pairs | {ROUND_INTERVAL}s interval")
    print(f"[ctrl] Log → {log_path}")

    setup_ssh()
    start_servers()

    running = True
    def on_sig(s, f):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT,  on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    t0  = time.time()
    rnd = 0

    try:
        with open(log_path, "a") as f:
            while running and (time.time() - t0) < duration:
                rs = time.time()
                rnd += 1
                print(f"\n── Round {rnd}  {datetime.now().strftime('%H:%M:%S')} ──")

                for sn, si, dn, di in pairs:
                    bw = measure(sn, di)
                    gbps = round(bw * 8 / 1000, 2) if bw else None

                    rec = {
                        "ts":       datetime.now(timezone.utc).isoformat(),
                        "round":    rnd,
                        "src":      sn,
                        "dst":      dn,
                        "bw_MBps":  round(bw, 1) if bw else None,
                        "bw_Gbps":  gbps,
                    }
                    f.write(json.dumps(rec) + "\n")
                    f.flush()

                    tag = f"{gbps:.1f} Gbps" if gbps else "FAIL"
                    print(f"  {sn} → {dn}: {tag}")

                elapsed = time.time() - rs
                wait = max(0, ROUND_INTERVAL - elapsed)
                print(f"  round {elapsed:.1f}s, wait {wait:.1f}s")
                if wait > 0 and running:
                    time.sleep(wait)
    finally:
        stop_servers()
        cleanup_ssh()
        print(f"[ctrl] Done. {rnd} rounds in {time.time()-t0:.0f}s  →  {log_path}")

if __name__ == "__main__":
    main()
