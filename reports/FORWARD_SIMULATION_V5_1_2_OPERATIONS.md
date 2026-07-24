# FORWARD_SIMULATION_V5_1_2_OPERATIONS

## Purpose

v5.1.2 is a short, foreground, public-data acceptance harness. It is not the formal forward simulation.

## Commands

```bash
python3 src/forward_simulation_v5_1_2.py --root . --config config/forward_simulation_v5_1_2.yaml discover
python3 src/forward_simulation_v5_1_2.py --root . --config config/forward_simulation_v5_1_2.yaml sample --iterations 15 --interval-seconds 60
python3 src/forward_reporting_v5_1_2.py --root .
```

Stop a sampling run with Ctrl+C. A stopped run leaves already written live-integration snapshots available for audit.

Only the live-integration directory is written. Formal directories remain off limits for this acceptance step.
