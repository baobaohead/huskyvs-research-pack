# D1 Value Signal Contract V2

V2 is an additive D1 contract.  V1 bundles remain handled by the V1 bridge.
`schema_version: "2.0"` requires the V2 path and can never be downgraded to
reference-only orderbook hashes.

The V2 bundle contains a canonical-hashed market snapshot with raw selected
Gamma and CLOB objects, plus a non-empty set of raw CLOB orderbook responses.
The verifier recomputes each raw payload hash, calls the existing public
`normalize_orderbook` algorithm, recomputes the normalized hash and binds the
candidate ask to the replayed best ask.  Candidate evidence references are
complete: missing and orphaned snapshots both fail validation.

The value bundle must be generated at or after the weather bundle whose
canonical hash it carries. Gamma and CLOB outcome/token arrays are replayed in
full: both arrays must be non-empty, equal in length, ordered identically, and
contain unique non-empty values. The CLOB market payload must also state its
condition explicitly, matching the Gamma payload, manifest, and orderbook
evidence.

When multiple raw fields alias the same security-critical identity, every
present alias must agree exactly. Conflicting aliases are never silently
selected by priority; array aliases are parsed as JSON arrays and compared
item-by-item in their original order. Security-critical condition, token,
outcome, snapshot, and evidence-reference strings must be non-empty and must
not contain leading or trailing whitespace. The validator rejects padded or
ambiguous raw evidence instead of trimming, normalizing, deleting, or
rewriting it.

The bridge dispatches by the bridge manifest version.  Registration performs
that replay before its SQLite transaction and persists
`orderbook_hash_verification=self_contained_semantic_replay` in the D1
registration evidence.  This implementation is synthetic/offline only: it
does not fetch markets, connect accounts, sign, place orders, or start formal
mode.
