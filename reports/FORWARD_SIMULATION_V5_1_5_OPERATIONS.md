# FORWARD_SIMULATION_V5_1_5_OPERATIONS

## Register A Signal

Fill the v5 entry signal CSV with the forecast, city, local weather date, token id, intended dollars, and max buy price. The timestamp must end in `Z` or `+00:00` and must be fresh.

## Start One Monitor Pass

`.venv/bin/python -m src.forward_simulation_v5_1_5 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_5.yaml monitor-once --mode formal`

## Start Foreground Loop

`.venv/bin/python -m src.forward_simulation_v5_1_5 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack --config config/forward_simulation_v5_1_5.yaml run-loop --mode formal --iterations 0 --interval-seconds 60 --confirm-infinite`

## Pause, Resume, Stop

Use `pause`, `resume`, and `stop` subcommands. They update ledger state and keep the records intact.

## Check Health

Run `status --mode formal` and verify heartbeat, lock state, paused/stopped flags, and row counts.

## Network Failure

Network, rate-limit, empty-book, and token-specific errors are written to audit logs. The monitor does not guess prices.
