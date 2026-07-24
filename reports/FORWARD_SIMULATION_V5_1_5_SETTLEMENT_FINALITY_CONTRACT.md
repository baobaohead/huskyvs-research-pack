# FORWARD_SIMULATION_V5_1_5_SETTLEMENT_FINALITY_CONTRACT

Final settlement rule:
- Active, closed unresolved, resolution pending, proposed, disputed, or challenged markets are not final.
- `automaticallyResolved=true` plus proposed status is a conflict, not final.
- `outcomePrices` can only cross-check stronger evidence and never proves final by itself.
- Final settlement requires official winning asset id, or final status plus winning outcome and token mapping consistency.
- Any winner conflict blocks settlement and writes audit evidence.
