#!/usr/bin/env python3
"""Centralized controller for bw_probe agents with persistent UCX RC connections."""

import socket
import subprocess
import time
import json
import os
import sys
import signal
from datetime import datetime, timezone

NODES = [
    (0, "b04-13", "10.125.137.190"),
    (1, "b05-12", "10.125.137.210"),
    (2, "b05-14", "10.125.137.212"),
    (3, "b10-14", "10.125.138.4"),
]

PROBE_BIN   = "/home1/yizhuoli/bw-monitor/bw_probe"
UCX_DIR     = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6"
GCC_LIB     = "/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/gcc-runtime-13.3.0-cm7m2ub/lib"
CTRL_PORT   = 19400
OOB_BASE    = 19500
NUMA_CPU    = "32"
LOG_DIR     = "/scratch1/yizhuoli/bw-monitor/logs"
ROUND_INTERVAL = 1.0


class Agent:
    def __init__(self, nid, hostname, ib_ip):
        self.nid = nid
        self.hostname = hostname
        self.ib_ip = ib_ip
        self.sock = None
        self.f = None

    def connect_ctrl(self):
        for _ in range(30):
            try:
                s = socket.create_connection((self.ib_ip, CTRL_PORT), timeout=5)
                s.settimeout(None)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.sock = s
                self.f = s.makefile("rw", buffering=1)
                return True
            except OSError:
                time.sleep(0.5)
        return False

    def cmd(self, line):
        self.f.write(line + "\n")
        self.f.flush()
        return self.f.readline().strip()

    def close(self):
        if self.f:
            try: self.cmd("QUIT")
            except: pass
            self.f.close()
        if self.sock:
            self.sock.close()


def launch_agents():
    env = (
        f"export LD_LIBRARY_PATH={UCX_DIR}/lib:{GCC_LIB}:$LD_LIBRARY_PATH "
        f"UCX_WARN_UNUSED_ENV_VARS=n"
    )
    for nid, host, ip in NODES:
        log = f"/scratch1/yizhuoli/bw-monitor/logs/agent{nid}.log"
        subprocess.Popen(
            f'ssh -o StrictHostKeyChecking=no {host} "'
            f'{env} && taskset -c {NUMA_CPU} {PROBE_BIN} {nid} {CTRL_PORT}'
            f' 2>{log}"',
            shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    time.sleep(3)
    print("[ctrl] agents launched")


def kill_agents():
    for _, host, _ in NODES:
        subprocess.run(
            f"ssh -o StrictHostKeyChecking=no {host} 'pkill -f bw_probe' 2>/dev/null",
            shell=True, capture_output=True, timeout=5,
        )


def setup_mesh(agents):
    pairs = [(a, b) for a in agents for b in agents if a.nid < b.nid]
    for a, b in pairs:
        oob_port = OOB_BASE + a.nid * MAX_PEERS + b.nid
        # b (higher id) listens first, then a (lower id) connects
        b.f.write(f"CONNECT {a.nid} {a.ib_ip} {oob_port}\n")
        b.f.flush()
        a.f.write(f"CONNECT {b.nid} {b.ib_ip} {oob_port}\n")
        a.f.flush()
        resp_a = a.f.readline().strip()
        resp_b = b.f.readline().strip()
        ok = "CONNECTED" in resp_a and "CONNECTED" in resp_b
        print(f"  {a.hostname} ↔ {b.hostname}: {'OK' if ok else 'FAIL'}")
        if not ok:
            raise RuntimeError(f"mesh connect failed: {a.hostname}-{b.hostname}: {resp_a} / {resp_b}")
    print("[ctrl] full mesh established")


MAX_PEERS = 16


def measure_pair(src, dst):
    try:
        src.sock.settimeout(10)
        dst.sock.settimeout(10)

        dst.f.write(f"RESPOND {src.nid}\n")
        dst.f.flush()
        src.f.write(f"MEASURE {dst.nid}\n")
        src.f.flush()

        result = src.f.readline().strip()
        _ = dst.f.readline()

        src.sock.settimeout(None)
        dst.sock.settimeout(None)

        if result.startswith("OK ") and len(result.split()) >= 3:
            parts = result.split()
            bw_mbps = float(parts[1])
            if bw_mbps < 0:
                return None, 0
            cksum_ok = int(parts[2])
            return bw_mbps, cksum_ok
    except (socket.timeout, OSError, ValueError) as e:
        print(f"    [warn] {src.hostname}→{dst.hostname}: {e}")
        src.sock.settimeout(None)
        dst.sock.settimeout(None)
    return None, 0


def main():
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 300

    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"bw2_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl")

    directed_pairs = [(s, d) for s in range(len(NODES)) for d in range(len(NODES)) if s != d]

    print(f"[ctrl] BW Probe | {duration}s | {len(directed_pairs)} pairs | {ROUND_INTERVAL}s interval")
    print(f"[ctrl] Log → {log_path}")

    kill_agents()
    time.sleep(1)
    launch_agents()

    agents = []
    for nid, host, ip in NODES:
        a = Agent(nid, host, ip)
        if not a.connect_ctrl():
            print(f"[ctrl] FATAL: cannot connect to agent on {host}")
            kill_agents()
            return 1
        agents.append(a)
    print("[ctrl] all agents connected")

    setup_mesh(agents)

    running = True
    def on_sig(s, f):
        nonlocal running
        running = False
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    t0 = time.time()
    rnd = 0

    try:
        with open(log_path, "a") as lf:
            while running and (time.time() - t0) < duration:
                rs = time.time()
                rnd += 1
                print(f"\n── Round {rnd}  {datetime.now().strftime('%H:%M:%S')} ──")

                for si, di in directed_pairs:
                    src, dst = agents[si], agents[di]
                    bw_mbps, cksum_ok = measure_pair(src, dst)
                    gbps = round(bw_mbps * 8 / 1000, 2) if bw_mbps else None

                    rec = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "round": rnd,
                        "src": src.hostname,
                        "dst": dst.hostname,
                        "bw_MBps": round(bw_mbps, 1) if bw_mbps else None,
                        "bw_Gbps": gbps,
                        "cksum": cksum_ok,
                    }
                    lf.write(json.dumps(rec) + "\n")
                    lf.flush()

                    tag = f"{gbps:.1f} Gbps" if gbps else "FAIL"
                    ck = "✓" if cksum_ok else "✗"
                    print(f"  {src.hostname} → {dst.hostname}: {tag} {ck}")

                elapsed = time.time() - rs
                wait = max(0, ROUND_INTERVAL - elapsed)
                print(f"  ({elapsed:.2f}s, wait {wait:.2f}s)")
                if wait > 0 and running:
                    time.sleep(wait)
    finally:
        for a in agents:
            a.close()
        kill_agents()
        print(f"[ctrl] Done. {rnd} rounds in {time.time()-t0:.0f}s → {log_path}")


if __name__ == "__main__":
    main()
