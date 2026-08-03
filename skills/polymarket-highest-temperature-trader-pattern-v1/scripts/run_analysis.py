#!/usr/bin/env python3
"""Thin, deterministic launcher for the fixed repository analysis module."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_input(path: Path) -> dict[str, Any]:
    """Load the examples' JSON-compatible YAML without an extra dependency."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("input must be a mapping")
    return payload


def build_command(
    payload: dict[str, Any],
    output_root: Path,
    *,
    refresh_public_data: bool,
    saved_public_evidence_manifest: Path | None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "src.polymarket_highest_temperature_trader_pattern_v1",
        "analyze",
    ]
    for wallet in payload.get("trader_ids") or []:
        command.extend(["--wallet", str(wallet)])
    command.extend([
        "--date-from", str(payload["date_from"]),
        "--date-to", str(payload["date_to"]),
        "--output-root", str(output_root),
    ])
    for city in payload.get("cities") or []:
        command.extend(["--city", str(city)])
    for override in payload.get("city_timezones") or []:
        command.extend(["--city-timezone", str(override)])
    if refresh_public_data:
        command.append("--refresh-public-data")
    elif saved_public_evidence_manifest:
        command.extend([
            "--saved-public-evidence-manifest",
            str(saved_public_evidence_manifest),
        ])
    else:
        raise ValueError("choose exactly one evidence mode")
    return command


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-root", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--refresh-public-data", action="store_true")
    source.add_argument("--saved-public-evidence-manifest")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input).resolve()
    repo_root = Path(__file__).resolve().parents[3]
    command = build_command(
        load_input(input_path),
        Path(args.output_root).resolve(),
        refresh_public_data=args.refresh_public_data,
        saved_public_evidence_manifest=(
            Path(args.saved_public_evidence_manifest).resolve()
            if args.saved_public_evidence_manifest else None
        ),
    )
    return subprocess.run(command, cwd=repo_root, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
