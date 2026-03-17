# Running on USC CARC SLURM

Environment-specific instructions for the 4-node A100 allocation.

## Nodes

| Hostname | IB IP (ib0) | Node rank |
|----------|-------------|-----------|
| b04-13   | 10.125.137.190 | 0 (head) |
| b05-12   | 10.125.137.210 | 1 |
| b05-14   | 10.125.137.212 | 2 |
| b10-14   | 10.125.138.4   | 3 |

IB NIC `mlx5_0` is on NUMA node 1 (CPUs 32-63).

## UCX setup

```bash
source /etc/profile.d/modules.sh
module load ucx/1.16.0
```

UCX perftest binary lives at:
```
/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6/bin/ucx_perftest
```

## Traffic generator

```bash
NODES="b04-13:10.125.137.190,b05-12:10.125.137.210,b05-14:10.125.137.212,b10-14:10.125.138.4"
UCX_BIN="/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6/bin/ucx_perftest"
UCX_LIB="/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6/lib"
GCC_LIB="/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/gcc-runtime-13.3.0-cm7m2ub/lib"
ENV="export LD_LIBRARY_PATH=$UCX_LIB:$GCC_LIB:\$LD_LIBRARY_PATH UCX_WARN_UNUSED_ENV_VARS=n"

python3 src/traffic_gen.py \
    --nodes "$NODES" \
    --ucx-perftest "$UCX_BIN" \
    --env "$ENV" \
    --cpu 32 \
    --size 512 --concurrent 2 --gap 2.0 --duration 60
```

## Bandwidth monitor

```bash
# Edit src/bw_controller.py to set NODES, UCX paths, NUMA_CPU, LOG_DIR
# then:
python3 src/bw_controller.py 300    # 5-minute test

# With live plotter (needs matplotlib from conda env):
eval "$(/home1/yizhuoli/miniconda3/bin/conda shell.bash hook)"
conda activate /scratch1/yizhuoli/conda-envs/sglang-fp
python3 src/bw_plot.py --once       # one-shot plot
python3 src/bw_plot.py              # continuous (every 60s)
```

## Build custom probe

```bash
module load ucx/1.16.0
make UCX_DIR=/apps/spack/2406/apps/linux-rocky8-x86_64_v3/gcc-13.3.0/ucx-1.16.0-sozthz6
```
