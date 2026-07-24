# FORWARD_SIMULATION_V5_1_6_API_CONTRACT

Allowed capabilities:
- Public GET requests to Gamma and CLOB market-data endpoints.
- Market lookup by slug, CLOB market lookup by condition id, orderbook lookup by token id, search/list endpoints for read-only discovery, and server-time checks.

Forbidden capabilities:
- Wallet connection.
- Private-key, seed phrase, signing, allowance, order creation, cancellation, or submission.
- Any POST/PUT/PATCH/DELETE trade action.

Orderbook requirements:
- Entry uses ask depth and computes executable VWAP by consuming levels.
- Exit uses bid depth and computes executable VWAP by consuming levels.
- Missing tick size or min order size is an error.
- Gamma/orderbook constraint conflict is an error.
