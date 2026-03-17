# bw-monitor

Continuous InfiniBand bandwidth monitoring across cluster nodes using native RDMA (UCX UCP `put_bw`). Measures all directed node-pair bandwidths every few seconds and produces a live timeline plot.

## Structure

```
src/
  bw_controller.py   # Centralized controller — orchestrates ucx_perftest measurements
  bw_controller2.py  # (WIP) Custom UCX agent controller with persistent RC connections
  bw_probe.c         # (WIP) Custom UCP tag send/recv probe agent with checksum
  bw_plot.py         # Async matplotlib plotter — reads JSONL logs, generates timeline PNG
scripts/
  run_test.sh        # 5-minute smoke test
  run_6h.sh          # Long-running deployment with live plotter (duration configurable)
Makefile             # Builds bw_probe from src/bw_probe.c (requires UCX)
```

## Prerequisites

- **Python 3.8+** with `matplotlib` (for plotting)
- **UCX** (≥ 1.14) with `ucx_perftest` binary
- **InfiniBand** fabric with `libibverbs`
- Passwordless SSH between all nodes

## Installation

### Ubuntu / Debian

```bash
sudo apt install -y libibverbs-dev libucx-dev ucx-utils python3-matplotlib
make                # UCX_DIR defaults to /usr
```

### RHEL / Rocky (with UCX from package manager)

```bash
sudo dnf install -y ucx ucx-devel libibverbs
pip install matplotlib
make
```

### HPC cluster with module system (e.g., CARC SLURM)

```bash
module load ucx/1.16.0
make UCX_DIR=/apps/spack/.../ucx-1.16.0-sozthz6   # adjust to your module path
```

`make` only builds the custom probe (`bw_probe`). The main controller (`bw_controller.py`) uses `ucx_perftest` which ships with UCX — no compilation needed.

## Configuration

Edit `src/bw_controller.py` and update the top-level constants:

```python
NODES = [
    ("node-0", "192.168.1.10"),   # (hostname, IB IP)
    ("node-1", "192.168.1.11"),
    ...
]
NUMA_CPU    = "0"           # CPU core on the NUMA node of your IB NIC
SERVER_PORT = 18515
ROUND_INTERVAL = 1.0        # seconds between measurement rounds
LOG_DIR  = "/tmp/bw-monitor/logs"
```

Find your IB IP with `ip addr show ib0` and IB NIC NUMA node with `cat /sys/class/infiniband/mlx5_0/device/numa_node`.

## Quick start

```bash
# 5-minute test (no compilation needed — uses ucx_perftest)
bash scripts/run_test.sh

# Long run with live plotting (default 6h, or pass seconds as arg)
nohup bash scripts/run_6h.sh 7200 &

# Build the custom probe (optional, WIP)
make
```

## How it works

The controller starts persistent `ucx_perftest` servers (UCP `put_bw`) on each node in a while-loop, then sequentially measures all directed pairs by running clients via SSH with ControlMaster. Measurements use RDMA over InfiniBand, NUMA-pinned to the IB NIC's local cores.

Logs are written as JSONL. The plotter reads all `bw_*.jsonl` files and generates a per-pair bandwidth timeline plot, refreshing every 60 seconds.

## CARC SLURM specifics

On the USC CARC cluster the environment differs from stock Ubuntu:

- **UCX path**: `/apps/spack/.../ucx-1.16.0-sozthz6/` (load via `module load ucx/1.16.0`)
- **Conda env**: `/scratch1/yizhuoli/conda-envs/sglang-fp` (has matplotlib)
- **Logs dir**: `/scratch1/yizhuoli/bw-monitor/logs/` (BeeGFS scratch, shared across nodes)
- **NUMA**: IB NIC on NUMA node 1, CPUs 32-63
- **Nodes**: SSH from any allocated compute node to any other; `/home1/` is NFS-shared

Build on a compute node (login nodes lack the IB libraries):
```bash
module load ucx/1.16.0
make UCX_DIR=/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6
```
