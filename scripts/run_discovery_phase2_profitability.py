#!/usr/bin/env python3
"""Run frozen Discovery candidates through official Hybrid Profitability."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.polymarket_highest_temperature_trader_profitability_batch import (  # noqa: E402
    DATE_FROM,
    DATE_TO,
    run_phase2_batch,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-pool", required=True, type=Path)
    parser.add_argument("--target-markets", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--discovery-commit", required=True)
    parser.add_argument("--profitability-code-commit", required=True)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--saved-control-profitability-manifest", type=Path)
    args = parser.parse_args(argv)
    run_phase2_batch(
        candidate_pool_path=args.candidate_pool,
        target_markets_path=args.target_markets,
        output_root=args.output_root,
        discovery_commit=args.discovery_commit,
        profitability_code_commit=args.profitability_code_commit,
        batch_size=args.batch_size,
        saved_control_profitability_manifest=args.saved_control_profitability_manifest,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
