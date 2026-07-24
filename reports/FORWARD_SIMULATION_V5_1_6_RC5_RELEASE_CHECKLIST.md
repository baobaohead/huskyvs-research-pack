# FORWARD_SIMULATION_V5_1_6_RC5_RELEASE_CHECKLIST

- [x] v5.1.6 files are independent and do not overwrite v1-v4 outputs.
- [x] No wallet, signing, or real order function exists.
- [x] Market state gating precedes entry/exit.
- [x] Shared token-level orderbook depth is enforced.
- [x] Strict future-signal rejection is tested.
- [x] Frozen-file deletion blocks formal writes.
- [x] Run-loop lock, heartbeat, pause/resume/stop are implemented.
- [x] Fee conflict and exponent handling are tested.
- [x] Tick/min constraint failures are tested.
- [x] Formal ledger is empty.
- [ ] Formal start is intentionally waiting for user confirmation.
