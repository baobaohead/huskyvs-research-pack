# FORWARD_SIMULATION_V5_1_3_OPERATIONS

## Register a Demo Signal

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  register-signal --mode demo --signals-file path/to/signals.csv
```

## Run One Public Monitor Pass

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  monitor-once --mode formal --run-id manual_run_id
```

This is foreground-only. It reads public market detail, CLOB parameters, orderbooks, and market status. It does not connect to an account and cannot place a real trade.

## Demo Rehearsal

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  demo-run
```

## Audit

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  audit-integrity --mode formal
```

## Short Live Integration

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  live-integration --iterations 1 --interval-seconds 0
```

Live integration writes only under `data/forward_v5_1_3/live_integration/<run_id>/`.
