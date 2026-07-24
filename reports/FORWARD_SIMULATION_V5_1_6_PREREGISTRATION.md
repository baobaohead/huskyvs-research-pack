# FORWARD_SIMULATION_V5_1_6_PREREGISTRATION

v5.1.6-RC5 is not formally started in this release.

Frozen strategies:
- hold_to_settlement
- tp_2x_sell_50pct
- tp_2x_sell_75pct
- tp_5x_sell_25pct

Formal sample rules:
- Formal samples start only after `start-formal --confirm`.
- Minimum first review point is 50 settled traded city-date events.
- Do not backfill historical winners.
- Do not delete losing events.
- Do not stop recording because one branch temporarily underperforms.
- Do not add new take-profit multiples before the first review point.
- Any post-start code change must be recorded as either a logic change or pure technical fix with frozen-file hashes.
