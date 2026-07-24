#!/usr/bin/env bash
set -euo pipefail
WALLET="${1:-0xaf17116ae2b1476032785a67bd5b7c8c05905c20}"
python -m src.collect_public_ledger --wallet "$WALLET" --out data/raw
python -m src.analyze_weather_strategy --raw data/raw --out data/processed
python -m pytest -q
