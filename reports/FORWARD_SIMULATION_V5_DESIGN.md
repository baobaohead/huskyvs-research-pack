# FORWARD_SIMULATION_V5_DESIGN

Generated for project path: `/Users/baobaotou/Documents/竞争对手分析/huskyvs_research_pack`

## Purpose

v5 is a forward-only weather market simulation system. It starts from user-created prediction signals and records only information visible at the time:

1. the signal,
2. the live public orderbook,
3. simulated executable fills,
4. four frozen exit strategy branches,
5. settlement results when they become known.

It has no wallet code, no private key handling, no signing, and no order-submission path.

## Frozen Strategy Branches

Every executable simulated entry is copied into these four branches with identical entry shares and cost:

- `hold_to_settlement`: never sells before settlement.
- `tp_2x_sell_50pct`: if executable bid-depth VWAP for the planned sale is at least 2x rolling average cost, sell 50% of current remaining inventory.
- `tp_2x_sell_75pct`: if executable bid-depth VWAP for the planned sale is at least 2x rolling average cost, sell 75% of current remaining inventory.
- `tp_5x_sell_25pct`: if executable bid-depth VWAP for the planned sale is at least 5x rolling average cost, sell 25% of current remaining inventory.

The four rules are hard-coded in `src/forward_simulation_v5.py` and mirrored in `config/forward_simulation_v5.yaml`.

## Data Flow

Signal -> entry orderbook snapshot -> simulated entry fill -> four strategy inventory branches -> exit orderbook snapshots -> simulated exit fills -> settlement -> event-level results.

The formal files live at `data/forward_v5/`. Demo files live at `data/forward_v5/demo/` and are excluded from formal reporting.

## Orderbook-Only Execution

Entry uses asks. The simulator walks ask levels up to `max_entry_price`, computes executable shares and VWAP, and records any unfilled budget.

Exit uses bids. The simulator first computes the bid-depth VWAP for the planned sell quantity. It triggers only if that executable VWAP meets the strategy threshold. If depth is insufficient, only the executable shares are sold and the rest stays in inventory.

Public orderbook endpoint: `https://clob.polymarket.com/book?token_id=...`

Polymarket orderbook docs: https://docs.polymarket.com/api-reference/market-data/get-order-book

## Leakage Controls

- Formal signals are rejected unless `start-formal --confirm` has already set a formal start time.
- Formal signals older than the formal start time are rejected.
- Demo data is written under `data/forward_v5/demo/`, not into formal ledgers.
- No final temperature or settlement field is required for entry or exit decisions.
- Exit decisions use only current orderbook bids and current branch inventory.
- Same raw orderbook snapshot cannot be reused to book the same strategy-token exit twice.
- Add-on signals for the same token roll into the same strategy-token inventory and rolling cost.

## Append-Only Records

The following formal records are append-only ledgers:

- `signals.csv`
- `events.csv`
- `orderbook_snapshots.jsonl`
- `entry_fills.csv`
- `strategy_positions.csv`
- `exit_fills.csv`
- `settlements.csv`
- `audit_log.jsonl`

`system_state.json` and generated reports may be refreshed, but they are not raw fill/snapshot ledgers.

## Known Limits

- The system records public orderbook snapshots, not hidden liquidity or queue position.
- Fees are not confirmed; config stores a zero-fee base case and a conservative fee scenario field.
- Settlement is currently accepted via explicit settlement file; it is not guessed from future data.
- Long-running monitoring is not started automatically.
