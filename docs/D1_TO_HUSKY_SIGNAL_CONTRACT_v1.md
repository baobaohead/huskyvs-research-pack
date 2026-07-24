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

These layers must never be collapsed into one input file. Value layer must not rewrite weather probabilities.

---

## Field gap table (manual ↔ Husky)

| Field | Manual / D1 layers | Husky `USER_SIGNAL_FIELDS` / CSV | Bridge handling |
|---|---|---|---|
| `forecast_run_id` | Required (A/B) | Not a native CSV column | Embedded in `notes` + output directory name |
| `model_version` | Required; formal=`D1_1500` | Not native | Embedded in `notes` |
| `rules_version` | Required | Not native | Embedded in `notes` |
| `as_of_time_utc` | Required formal D-1 07:00Z | Maps to `created_at_utc` | **Must** use weather `as_of_time_utc`, never converter wall clock |
| `as_of_time_cst` | Required formal D-1 15:00 Asia/Shanghai | Not native | Validated against UTC binding |
| `station` | ZSPD / ZBAA only | Not native | Validated; city derived/checked |
| `city` | Required | Required | Passed through |
| `weather_date_local` | Next calendar day after as_of CST date | Required | Hard validated as D+1 |
| `weather_metric` | Required | Required | Passed through |
| `temperature_bucket` | Integer buckets + tail/其他 | Via `temperature_bucket` or bucket_type/threshold/unit | Normalized to Husky parser labels; 其他 not marketable |
| `forecast_probability` | Per integer bucket, sum=1 | Required | Weather is source of truth |
| `market_ask_price` | Value layer | → `market_probability_at_signal` | Recalculated edge |
| `edge` | `forecast_probability - market_ask_price` | Not native | Recomputed; mismatch rejected; also in `notes` |
| `recommended_max_price` | Value layer | → `max_entry_price` | Range 0–1 |
| `intended_usd` | Value layer | Required | Must be > 0 |
| `reason` | Value layer | Not native | Embedded in `notes` |
| `data_status` | COMPLETE / PARTIAL / CONFLICTING / STALE / LEAKAGE_INVALID | Not native | Preserved in `notes`; LEAKAGE blocks CSV |
| `market_slug` / `condition_id` / `token_id` / `outcome` | Value layer | Required | Required non-empty |
| `source snapshot/hash` | `source_snapshot_sha256` + manifest | Not native | Weather audit; hashes in manifest |
| `orderbook_snapshot_*` | Value layer | Not native | In `notes` + value bundle |
| `created_at_utc` | Derived from as_of | Required | = `as_of_time_utc` |
| `entry_deadline_utc` | Execution timing | Recomputed by `register_signals` | Provisional in CSV; Husky recomputes from created_at + `entry_valid_minutes` |
| `side` | BUY only for entry | Required | Fixed `BUY` |
| `source` | Bridge tag | Required | Fixed `d1_signal_bridge_v1` |

Husky still accepts legacy `bucket_type` / `temperature_threshold` / `temperature_unit`. Bridge emits canonical `temperature_bucket` (`exact:32C`, `or_below:30C`, `or_higher:35C`) which `register_signals` already understands.

---

## Formal D1_1500 time & leakage rules

- Timezone: `Asia/Shanghai`
- `as_of_time_cst` = D-1 `15:00:00`
- `as_of_time_utc` = same instant `07:00:00Z`
- `weather_date_local` = next local calendar day
- `generated_at_utc` should be within as_of → as_of+5 minutes; late generation blocks formal conversion
- Any source `acquired/released/published/captured` time after `as_of_time_utc` ⇒ `data_status=LEAKAGE_INVALID` and no Husky CSV
- Order book `orderbook_captured_at_utc` must not be after `as_of_time_utc`
- Do not infer D-1/D+1 from system “today”; validate from bundle fields only

Allowed `data_status`: `COMPLETE`, `PARTIAL`, `CONFLICTING`, `STALE`, `LEAKAGE_INVALID`.  
PARTIAL / CONFLICTING / STALE must not be silently upgraded to COMPLETE.

---

## Probability basket rules

- Each probability ∈ [0, 1]
- Sum of all integer-temperature rows = 1 (tiny float tolerance)
- No duplicate buckets after normalization
- Keep tail / `其他` probability expression
- `其他` must never become a market token / Husky candidate
- Value layer may emit multiple adjacent marketable candidates; each keeps its own forecast probability and edge
- Weather bundle is read-only upstream evidence

---

## Output directory contract

```text
<data/d1_signal_bridge or /tmp/...>/<forecast_run_id>/
  weather_probability_bundle.json
  value_signal_bundle.json
  husky_entry_signals.csv
  bridge_manifest.json
  validation_report.json
```

Immutability:

- Same `forecast_run_id` + identical weather/value content hashes → reuse existing output
- Same `forecast_run_id` + different content → `FORECAST_RUN_ID_CONFLICT` (no overwrite)

`bridge_manifest.json` records bridge version, paths, SHA256s, counts, rejection reasons, and always:

- `formal_ledger_used=false`
- `wallet_or_real_order_used=false`

---

## CLI

```bash
python -m src.d1_signal_bridge_v1 validate-weather --input <weather_bundle.json>
python -m src.d1_signal_bridge_v1 validate-value --weather <weather.json> --value <value.json>
python -m src.d1_signal_bridge_v1 convert --weather <weather.json> --value <value.json> --output-root <dir>
python -m src.d1_signal_bridge_v1 verify-output --output-dir <run_dir>
```

All tests and demos must use `/tmp` (or pytest `tmp_path`). Do not write formal ledgers by default.

---

## Schemas & templates

- `schemas/d1_weather_probability_v1.schema.json`
- `schemas/d1_value_signal_v1.schema.json`
- `schemas/d1_bridge_manifest_v1.schema.json`
- `templates/d1_weather_probability_v1.json`
- `templates/d1_value_signal_v1.json`

Implementation: `src/d1_signal_bridge_v1.py`  
Tests: `tests/test_d1_signal_bridge_v1.py`
