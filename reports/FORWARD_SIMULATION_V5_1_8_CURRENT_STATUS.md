# Weather Forward Simulation v5.1.8-RC7

Generated at: 2026-07-24T06:13:47.424746+00:00

Status: PASS_FOR_FORMAL_START

This release extends replay from raw orderbook-to-fill evidence into end-to-end ledger reconstruction: signal evidence, market evidence, fees, constraints, entry state, lots, allocations, settlement, event, strategy, and total ledger PnL. It contains no wallet, signing, or real order functionality.

## Validation separation
- saved public response replay: pass
- current live readonly selection: pass
- selected_market_count: 3
- selected_token_count: 3
- snapshot_count: 3
- error_count: 0
- raw_orderbook_evidence_count: 3
- raw_evidence_hash_result: pass
- raw_payload_binding_result: pass
- raw_payload_binding_checked_count: 15
- raw_payload_binding_failed_count: 0
- snapshot_replay_result: pass
- same_run_evidence_chain: True
- blocked_reasons: []
- formal start: ALLOWED_BUT_NOT_STARTED

## Formal Status
```json
{
  "generated_at_utc": "2026-07-24T06:13:47.417845+00:00",
  "formal_started_at_utc": null,
  "signals": 0,
  "snapshots": 0,
  "entry_fills": 0,
  "exit_fills": 0,
  "settlements": 0,
  "event_results": 0,
  "ok": true
}
```

## Release Gate
```json
{
  "release_status": "PASS_FOR_FORMAL_START",
  "formal_start": "ALLOWED_BUT_NOT_STARTED",
  "blocked_reasons": [],
  "live_readonly_status": "pass",
  "live_readonly_reason": null,
  "saved_public_response_replay_status": "pass",
  "selected_market_count": 3,
  "selected_token_count": 3,
  "snapshot_count": 3,
  "error_count": 0,
  "raw_market_evidence_count": 3,
  "raw_orderbook_evidence_count": 3,
  "raw_evidence_hash_result": "pass",
  "snapshot_replay_result": "pass",
  "same_run_evidence_chain": true,
  "raw_payload_binding_result": "pass",
  "raw_payload_binding_checked_count": 15,
  "raw_payload_binding_failed_count": 0,
  "quick_audit_ok": true,
  "full_replay_ok": true,
  "negative_detected": 30,
  "formal_empty_ok": true,
  "live_pass": true,
  "saved_ok": true
}
```
