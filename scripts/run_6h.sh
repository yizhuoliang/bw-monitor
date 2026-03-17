#!/usr/bin/env bash
# 6-hour production run with live plotter
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../src"
DURATION=${1:-21600}

for node in b04-13 b05-12 b05-14 b10-14; do
    ssh -o StrictHostKeyChecking=no "$node" "pkill -f ucx_perftest 2>/dev/null" || true
done
sleep 1

echo "=== Starting live plotter (every 60 s) ==="
(
    eval "$(/home1/yizhuoli/miniconda3/bin/conda shell.bash hook)"
    conda activate /scratch1/yizhuoli/conda-envs/sglang-fp
    python3 "$SRC_DIR/bw_plot.py"
) &
PLOTTER_PID=$!

cleanup() {
    kill "$PLOTTER_PID" 2>/dev/null
    wait "$PLOTTER_PID" 2>/dev/null
    (
        eval "$(/home1/yizhuoli/miniconda3/bin/conda shell.bash hook)"
        conda activate /scratch1/yizhuoli/conda-envs/sglang-fp
        python3 "$SRC_DIR/bw_plot.py" --once
    )
    echo "Log dir : /scratch1/yizhuoli/bw-monitor/logs/"
    echo "Plot    : /scratch1/yizhuoli/bw-monitor/plots/bw_timeline.png"
}
trap cleanup EXIT

echo "=== Starting ${DURATION}s IB bandwidth monitor ==="
python3 "$SRC_DIR/bw_controller.py" "$DURATION"
