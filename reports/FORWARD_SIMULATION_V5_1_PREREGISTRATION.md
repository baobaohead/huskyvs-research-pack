# FORWARD_SIMULATION_V5_1_PREREGISTRATION

This preregistration freezes the v5.1 forward simulation rules before any formal sample is started.

## Formal Start

Formal v5.1 has not been started in this delivery. The start time will be recorded only when the user explicitly runs:

`python3 -m src.forward_simulation_v5_1 --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack start-formal --confirm`

At that moment the system records the formal start time and hashes for config, core code, reporting code, and this preregistration file in `data/forward_v5_1/formal/system_state.json`.

## Frozen Strategies

The four strategy branches are fixed:

1. `hold_to_settlement`: never sells before settlement.
2. `tp_2x_sell_50pct`: once executable bid-depth VWAP reaches 2x rolling average cost, sell 50 percent of current signal-level inventory once.
3. `tp_2x_sell_75pct`: once executable bid-depth VWAP reaches 2x rolling average cost, sell 75 percent of current signal-level inventory once.
4. `tp_5x_sell_25pct`: once executable bid-depth VWAP reaches 5x rolling average cost, sell 25 percent of current signal-level inventory once.

Each take-profit stage can be created once per signal and strategy. Partial depth does not create a new target; the remaining target stays open.

## Sample Rules

- Formal rows must be generated after formal start.
- Historical winners cannot be backfilled.
- Losing events cannot be deleted.
- Demo and test rows are excluded from formal statistics.
- The first review point is 50 settled traded city-date-metric events.
- Rules cannot be changed before the first 50-event review because one branch is temporarily ahead or behind.
- No new take-profit multiples are added during the first 50-event window.
- If 50 events are still noisy, the sample continues to 100 settled traded events before any rule decision.

## Event Unit

The event key is:

`normalized_city | weather_date_local | normalized_weather_metric`

Multiple temperature buckets for the same city, date, and metric are aggregated to one event. The review threshold counts settled traded events.

## Entry Rule

The simulator only accepts externally provided entry signals. It does not invent a forecast strategy.

Entry uses ask-side order-book depth. Partial entries may continue filling until the configured deadline, currently 10 minutes after signal creation. Signals older than 300 seconds at registration, before the formal start time, or more than 30 seconds in the future are rejected in formal mode.

## Exit Rule

Exit uses bid-side order-book depth. The trigger check uses executable VWAP for the planned shares, not best bid alone and not prices-history.

Each strategy branch has independent inventory, so one branch's sell does not affect the others.

## Fee Rule

Net PnL subtracts the frozen conservative fee assumptions:

- entry fee: 10 bps
- exit fee: 10 bps
- settlement fee: 0 bps

Fees are computed on gross traded notional and rounded to 8 decimals.

## Delivery Hashes

Current delivery hashes before formal start:

- `config/forward_simulation_v5_1.yaml`: `159429609cf2c6a71243ec0cf84e5f29ca6964c0bc65479d386a69882f63b15e`
- `src/forward_simulation_v5_1.py`: `6d259a6836e19a38cdfeff82e18afbbf6a6734885caab0ad379014f4480322f8`
- `src/forward_reporting_v5_1.py`: `c52c5f0f92ae1e1a4127281bd4ce1da83d23f83e52abd0a36b1630c9a9678656`
- `tests/test_forward_simulation_v5_1.py`: `4b7ee5bb013b0ff8ca91bf7441a0c4801fd3f79433de8a8095f369fd9bce213b`

The preregistration file's own hash is captured at formal start. If this file changes after formal start, formal writes are rejected.

## Explicit Non-Actions

This delivery does not start formal monitoring. It does not register real signals. It does not connect a wallet. It does not submit real orders.
