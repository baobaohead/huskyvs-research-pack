# FORWARD_SIMULATION_V5_1_3_PREREGISTRATION

v5.1.3-RC2 is not formally started in this release package.

Frozen strategies:

- `hold_to_settlement`
- `tp_2x_sell_50pct`
- `tp_2x_sell_75pct`
- `tp_5x_sell_25pct`

Formal rules:

- Do not backfill historical signals.
- Do not delete losing events.
- Do not modify exit rules before the first 50 settled city-date events.
- Do not stop a lagging strategy early.
- Do not add new take-profit multiples based on interim results.
- Formal samples must use v5.1.3 module and v5.1.3 config only.
- Formal monitor uses public orderbook asks/bids, not page probability, midpoint, last trade, prices-history, or guessed prices.

Formal start command, only after manual confirmation:

```bash
PYTHONPATH=. python3 -m src.forward_simulation_v5_1_3 \
  --root /Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack \
  --config config/forward_simulation_v5_1_3.yaml \
  start-formal --confirm
```
