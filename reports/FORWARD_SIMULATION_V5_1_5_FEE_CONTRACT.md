# FORWARD_SIMULATION_V5_1_5_FEE_CONTRACT

Fee source policy:
- CLOB fee fields are primary.
- Gamma fee schedule is a cross-check.
- Unknown fee is not treated as zero.
- Fee conflicts block simulated entry/exit.
- Both Gamma-disabled and CLOB-disabled with zero or missing rates means fee_status=disabled and fee=0.
- Gamma disabled with a nonzero CLOB fee is conflict.
- CLOB disabled with a nonzero Gamma fee is conflict.
- Only fee exponent 1 is supported in this release; other exponents are rejected as unsupported_fee_exponent.
