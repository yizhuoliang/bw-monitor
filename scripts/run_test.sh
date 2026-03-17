#!/usr/bin/env bash
# 5-minute smoke test
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC_DIR="$SCRIPT_DIR/../src"

for node in b04-13 b05-12 b05-14 b10-14; do
    ssh -o StrictHostKeyChecking=no "$node" "pkill -f ucx_perftest 2>/dev/null" || true
done
sleep 1

echo "=== 5-minute IB bandwidth test ==="
python3 "$SRC_DIR/bw_controller.py" 300

echo ""
echo "=== Generating plot ==="
eval "$(/home1/yizhuoli/miniconda3/bin/conda shell.bash hook)"
conda activate /scratch1/yizhuoli/conda-envs/sglang-fp
python3 "$SRC_DIR/bw_plot.py" --once

echo ""
echo "Log dir : /scratch1/yizhuoli/bw-monitor/logs/"
echo "Plot    : /scratch1/yizhuoli/bw-monitor/plots/bw_timeline.png"
