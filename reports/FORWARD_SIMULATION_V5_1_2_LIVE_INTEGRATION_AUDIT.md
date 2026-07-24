# FORWARD_SIMULATION_V5_1_2_LIVE_INTEGRATION_AUDIT

Generated at: 2026-07-21T08:39:35.893162+00:00

Acceptance conclusion: **PASS_WITH_MINOR_LIMITATIONS**

## Scope

- Public read-only API integration only.
- No formal sample start, no formal prediction signal, no live trade action.
- Demo data is isolated under `data/forward_v5_1_2/live_integration/`.

## Selected Markets

| Event | Market | Outcome | Active | Closed | Resolved | Fees |
| --- | --- | --- | --- | --- | --- | --- |
| Highest temperature in London on July 22? | highest-temperature-in-london-on-july-22-2026-20corbelow | Yes | True | False | False | True |
| Highest temperature in London on July 22? | highest-temperature-in-london-on-july-22-2026-29c | Yes | True | False | False | True |

## Checks

- Token mapping valid: True
- Bid/ask direction valid: True
- 1/5/10/25/50 USD buy VWAP rows present: True
- Sell VWAP has executable bid depth in at least one token: True
- Partial fill observed: True
- Empty order books: 0
- Manual-program VWAP check snapshots marked: 5
- Fee handling valid: True
- Network recovery probe valid: True
- Formal isolation valid: True
- Read-only static scan valid: True
- All recorded real endpoints used GET: True
