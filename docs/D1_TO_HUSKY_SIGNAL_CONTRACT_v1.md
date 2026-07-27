# D1 → Husky Signal Contract v1

**Rules authority:** `D1次日最高温预测程序总规则手册_v1.0` (2026-07-24).  
If this document, old READMEs, templates, or chat notes conflict with the manual, **the manual wins**.

**Bridge version:** `d1_signal_bridge_v1`  
**Scope:** interface / validator / converter / tests / docs only. No formal start, no wallet, no signing, no real orders, no RC7 core fill-logic changes.

---

## Three-layer separation

| Layer | Type | File | Responsibility |
|---|---|---|---|
| A Weather probability | `D1WeatherProbabilityBundleV1` | `weather_probability_bundle.json` | Forecast only. No trading. |
| B Trading value | `D1ValueSignalBundleV1` | `value_signal_bundle.json` | Edge vs 15:00 order book. |
| C Husky execution | `entry_signal_v5_1_8` CSV | `husky_entry_signals.csv` | Existing Husky registerable rows. |

Value layer must not rewrite weather probabilities. Identity fields must bind across layers.

The public D1 bundle metric is exactly **`highest_temperature`**. Formal bundles reject `high`, `highest`, `最高温`, and `最高气温`; any upstream alias normalization must finish before the D1 bundle is produced.

Station/city aliases:

- ZSPD → Shanghai / 上海
- ZBAA → Beijing / 北京

---

## Audit layout (no circular hash)

```text
<data/d1_signal_bridge or /tmp/...>/<forecast_run_id>/
  weather_probability_bundle.json
  value_signal_bundle.json
  bridge_manifest_core.json      # immutable audit root
  husky_entry_signals.csv        # notes.bridge_manifest_sha256 = CORE FILE SHA256
  validation_report.json
  bridge_manifest.json           # wrapper; no self-hash
  bridge_manifest.sha256         # detached SHA256 of bridge_manifest.json
```

1. Write `bridge_manifest_core.json` first (no CSV/report/final hashes, no absolute paths, no self-hash).
2. Compute **actual file SHA256** of the core file.
3. Stamp CSV `notes.bridge_manifest_sha256` with that file SHA.
4. Write final `bridge_manifest.json` listing relative paths + hashes for all artifacts except itself.
5. Write detached `bridge_manifest.sha256`.

`verify-output` checks detached SHA, core SHA, CSV notes reference, all listed file hashes, input content hashes, counts, and safety flags. It also semantically replays weather validation, value validation, core-manifest construction, CSV generation, validation-report construction, source timestamp/leakage checks, and final-manifest identity binding. Recomputing wrapper hashes cannot make a changed semantic payload valid.

Runtime validation uses `jsonschema.Draft202012Validator` with `FormatChecker` for weather, value, core, and final manifests. Install `requirements-d1-signal-bridge-v1.txt` when the environment does not already supply `jsonschema`.

All SHA-256 values are exactly 64 lowercase hexadecimal characters: no upper-case characters, whitespace, or prefixes are normalized or accepted. Final-manifest artifact paths must exactly equal their five fixed filenames; absolute paths, traversal, and subdirectories are rejected.

Evidence paths inside manifests/reports are **output-relative** (portable). CLI stdout may show absolute paths.

---

## Formal D1_1500 time & leakage

- `as_of_time_cst` literal must end with `+08:00` and be exactly `15:00:00`
- `as_of_time_utc` literal must end with `Z` or `+00:00` and be exactly `07:00:00`
- UTC and CST must be the same instant
- `weather_date_local` = CST date + 1 day
- `generated_at_utc` must satisfy `as_of <= generated <= as_of + 5 minutes`
- Early or late generation ⇒ `formal_blocked` and formal convert refused
- Formal `model_version` must be exactly `D1_1500`
- Formal `rules_version` must be exactly `D1_manual_v1.0`

`source_snapshot_sha256` must equal canonical SHA256(`source_snapshot_manifest`) and be 64-char lowercase hex.  
All source timestamps (`acquired_at*`, `released_at*`, `published_at*`, `captured_at*`, `source_time*`) must parse with timezone; invalid ⇒ `SOURCE_TIMESTAMP_INVALID` (never silently ignored). Late ⇒ `LEAKAGE_INVALID`.

This v1 contract uses `orderbook_hash_verification=reference_format_only` only. It validates the order-book snapshot SHA-256 format, non-empty snapshot ID, and capture-time cutoff; it does **not** read, verify, or claim to verify an external local order-book file. A future self-contained evidence design must use a v2 contract that copies evidence into the bundle and manifests it.

---

## Weather ↔ value identity binding

Top-level value fields must match weather:

`forecast_run_id`, `model_version`, `rules_version`, `station`, `city`, `weather_date_local`, `weather_metric`, `weather_bundle_sha256`

Every value candidate must also carry `forecast_run_id`, `station`, `weather_date_local`, `weather_metric`, and `data_status`. Missing candidate identity returns `CANDIDATE_IDENTITY_MISSING`; any mismatch is rejected or recorded with its precise identity error.

Status rules:

- Value top-level must not upgrade weather status to COMPLETE
- COMPLETE weather may be downgraded by value
- `LEAKAGE_INVALID` always rejected for conversion

## Execution eligibility

`validate-weather --allow-informal` and `validate-value --allow-informal` remain research-only validation operations. `convert --allow-informal` and `convert_bundles(..., formal_mode=False)` are rejected with `INFORMAL_EXECUTION_EXPORT_FORBIDDEN` before any run directory, artifact, or CSV is created.

Every exported formal run records `execution_eligible=true`; core conversion parameters record `formal_mode=true`. The final manifest, validation report, and every CSV notes field also carry the same execution-eligibility assertion. `verify-output` rejects any disagreement.

`intended_usd` is strictly greater than zero. Core manifests preserve its canonical decimal string, including valid sub-dollar values such as `0.01`, `0.1`, and `0.5`.

---

## Reuse / immutability / atomic write

Same `forecast_run_id`:

1. Validate new inputs
2. Require complete existing output
3. `verify-output` must pass
4. Input content hashes must match

Only then `status=reused`.

| Condition | Error |
|---|---|
| Same inputs, corrupt existing | `CORRUPT_EXISTING_OUTPUT` |
| Existing dir incomplete | `INCOMPLETE_EXISTING_OUTPUT` |
| Different content | `FORECAST_RUN_ID_CONFLICT` |

New outputs write to a temp sibling directory, self-verify, then atomic rename. Failures delete the temp dir and leave no partial final directory.

---

## CLI

```bash
python -m src.d1_signal_bridge_v1 validate-weather --input <weather.json>
python -m src.d1_signal_bridge_v1 validate-value --weather <weather.json> --value <value.json>
python -m src.d1_signal_bridge_v1 convert --weather <weather.json> --value <value.json> --output-root <dir>
python -m src.d1_signal_bridge_v1 verify-output --output-dir <run_dir>
```

Schemas:

- `schemas/d1_weather_probability_v1.schema.json`
- `schemas/d1_value_signal_v1.schema.json`
- `schemas/d1_bridge_manifest_core_v1.schema.json`
- `schemas/d1_bridge_manifest_v1.schema.json`
