# FORWARD_SIMULATION_V5_1_4_CURRENT_STATUS

Generated at: 2026-07-22T03:34:39.429741+00:00

## Formal

| Started | Signals | Snapshots | Entry fills | Exit fills | Settlements | Event results |
| --- | --- | --- | --- | --- | --- | --- |
| None | 0 | 0 | 0 | 0 | 0 | 0 |

## Demo

| Signals | Snapshots | Entry fills | Exit fills | Settlements | Event results | Runs |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 1 | 1 | 0 | 4 | 4 | 2 |

## Integrity

- Formal audit ok: True
- Demo audit ok: True
- Formal empty proof ok: True
- Negative audit tests detected: 20/20

## Formal Start Command

`.venv/bin/python -m src.forward_simulation_v5_1_4 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_4.yaml start-formal --confirm`
