#!/usr/bin/env python3
"""Compatibility/research entry point for the reusable advanced Skill core."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.polymarket_highest_temperature_trader_pattern_advanced import *  # noqa: F401,F403
from src.polymarket_highest_temperature_trader_pattern_advanced import make_report


# These are only the reviewed research fixture defaults for this compatibility
# command. The formal Skill core accepts arbitrary normalized wallet sets.
DEFAULT_RESEARCH_WALLETS = (
    "0x7c63520c2ca9b336af0c205b9ccf68217bb393d4",
    "0x8fbd7cf5f806f563080864694415829f7229a959",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet", action="append", default=None)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--city", default="beijing")
    parser.add_argument("--analysis-depth", choices=("basic", "advanced"), default="advanced")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.city != "beijing":
        raise SystemExit("This compatibility research command is scoped to Beijing evidence.")
    wallets = tuple(args.wallet or DEFAULT_RESEARCH_WALLETS)
    result = make_report(
        args.output_root.resolve(),
        args.report.resolve(),
        date.fromisoformat(args.date_from),
        date.fromisoformat(args.date_to),
        args.city,
        wallets,
    )
    if args.analysis_depth == "basic":
        # The old command has no basic second-stage output; keep the switch
        # accepted for callers while retaining the reviewed report behavior.
        del result
    print(args.report.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
