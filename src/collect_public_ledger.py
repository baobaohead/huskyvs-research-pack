#!/usr/bin/env python3
"""Download a public Polymarket account ledger without authentication.

The collector deliberately fetches:
- trades with takerOnly=false (maker + taker fills)
- activity including TRADE/SPLIT/MERGE/REDEEM and other cash-flow events
- current positions
- closed positions
- the accounting snapshot ZIP

It uses bounded time windows so Data API offset caps do not silently truncate history.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DATA_API = "https://data-api.polymarket.com"
WALLET_DEFAULT = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"


@dataclass(frozen=True)
class Window:
    start: int
    end: int


def epoch(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def make_session() -> requests.Session:
    retry = Retry(
        total=8,
        connect=8,
        read=8,
        backoff_factor=0.8,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    s = requests.Session()
    s.headers.update({"User-Agent": "huskyvs-public-ledger-research/1.0"})
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def get_json(session: requests.Session, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    r = session.get(f"{DATA_API}{path}", params=params, timeout=60)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected response for {path}: {type(payload).__name__}")
    return payload


def get_bytes(session: requests.Session, path: str, params: dict[str, Any]) -> bytes:
    r = session.get(f"{DATA_API}{path}", params=params, timeout=120)
    r.raise_for_status()
    return r.content


def split_window(window: Window) -> tuple[Window, Window]:
    mid = (window.start + window.end) // 2
    if mid <= window.start or mid >= window.end:
        raise RuntimeError(f"Cannot split saturated one-second window: {window}")
    # Overlap by one second; deduplication removes boundary duplicates.
    return Window(window.start, mid), Window(mid, window.end)


def stable_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """A conservative key that keeps distinct partial fills in the same transaction."""
    return (
        row.get("transactionHash"),
        row.get("type"),
        row.get("side"),
        row.get("asset"),
        row.get("conditionId"),
        row.get("timestamp"),
        row.get("size"),
        row.get("price"),
        row.get("usdcSize"),
    )


def fetch_windowed(
    session: requests.Session,
    path: str,
    wallet: str,
    window: Window,
    *,
    limit: int,
    offset_cap: int,
    base_params: dict[str, Any] | None = None,
    depth: int = 0,
) -> list[dict[str, Any]]:
    if depth > 40:
        raise RuntimeError(f"Excessive recursive splitting at {window}")

    params0 = dict(base_params or {})
    params0.update({"user": wallet, "start": window.start, "end": window.end, "limit": limit})

    rows: list[dict[str, Any]] = []
    offset = 0
    saturated = False
    while True:
        params = dict(params0)
        params["offset"] = offset
        page = get_json(session, path, params)
        rows.extend(page)
        if len(page) < limit:
            break
        offset += limit
        if offset > offset_cap:
            saturated = True
            break
        time.sleep(0.05)

    # A full final page at the offset cap is not proof of completeness.
    if saturated:
        left, right = split_window(window)
        return (
            fetch_windowed(
                session, path, wallet, left, limit=limit, offset_cap=offset_cap,
                base_params=base_params, depth=depth + 1
            )
            + fetch_windowed(
                session, path, wallet, right, limit=limit, offset_cap=offset_cap,
                base_params=base_params, depth=depth + 1
            )
        )
    return rows


def month_windows(start: int, end: int) -> Iterable[Window]:
    cur = datetime.fromtimestamp(start, tz=timezone.utc)
    finish = datetime.fromtimestamp(end, tz=timezone.utc)
    while cur < finish:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1, day=1, hour=0, minute=0, second=0)
        else:
            nxt = cur.replace(month=cur.month + 1, day=1, hour=0, minute=0, second=0)
        nxt = min(nxt, finish)
        yield Window(int(cur.timestamp()), int(nxt.timestamp()))
        cur = nxt


def fetch_all_windows(
    session: requests.Session,
    path: str,
    wallet: str,
    start: int,
    end: int,
    *,
    limit: int,
    offset_cap: int,
    base_params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for w in month_windows(start, end):
        page_rows = fetch_windowed(
            session, path, wallet, w, limit=limit, offset_cap=offset_cap,
            base_params=base_params
        )
        for row in page_rows:
            key = stable_key(row)
            if key not in seen:
                seen.add(key)
                out.append(row)
        print(f"{path}: {datetime.fromtimestamp(w.start, timezone.utc):%Y-%m} -> {len(page_rows)} rows")
    out.sort(key=lambda r: (int(r.get("timestamp") or 0), str(r.get("transactionHash") or "")))
    return out


def fetch_offset_endpoint(
    session: requests.Session,
    path: str,
    wallet: str,
    *,
    limit: int,
    max_offset: int,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while offset <= max_offset:
        q = dict(params or {})
        q.update({"user": wallet, "limit": limit, "offset": offset})
        page = get_json(session, path, q)
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
        time.sleep(0.05)
    if offset > max_offset and out and len(out) % limit == 0:
        raise RuntimeError(f"{path} may be truncated at API offset cap")
    return out


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for row in rows for k in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--wallet", default=WALLET_DEFAULT)
    p.add_argument("--start", default="2025-11-01T00:00:00Z")
    p.add_argument("--end", default=datetime.now(timezone.utc).isoformat())
    p.add_argument("--out", default="data/raw")
    p.add_argument("--skip-snapshot", action="store_true")
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    start, end = epoch(args.start), epoch(args.end)
    if start >= end:
        raise SystemExit("--start must precede --end")

    session = make_session()

    # Critical: false includes fills where the wallet was the maker.
    trades = fetch_all_windows(
        session, "/trades", args.wallet, start, end,
        limit=10_000, offset_cap=10_000,
        base_params={"takerOnly": "false"},
    )
    activity = fetch_all_windows(
        session, "/activity", args.wallet, start, end,
        limit=500, offset_cap=5_000,
        base_params={"sortBy": "TIMESTAMP", "sortDirection": "ASC"},
    )
    current = fetch_offset_endpoint(
        session, "/positions", args.wallet,
        limit=500, max_offset=10_000,
        params={"sizeThreshold": 0},
    )
    closed = fetch_offset_endpoint(
        session, "/closed-positions", args.wallet,
        limit=50, max_offset=100_000,
        params={"sortBy": "TIMESTAMP", "sortDirection": "ASC"},
    )

    for name, rows in {
        "trades": trades,
        "activity": activity,
        "current_positions": current,
        "closed_positions": closed,
    }.items():
        write_jsonl(out / f"{name}.jsonl", rows)
        write_csv(out / f"{name}.csv", rows)
        print(f"wrote {name}: {len(rows)} rows")

    if not args.skip_snapshot:
        blob = get_bytes(session, "/v1/accounting/snapshot", {"user": args.wallet})
        snap = out / "accounting_snapshot.zip"
        snap.write_bytes(blob)
        if not zipfile.is_zipfile(snap):
            raise RuntimeError("Accounting snapshot response was not a valid ZIP")
        print(f"wrote {snap}")

    manifest = {
        "wallet": args.wallet,
        "start_epoch": start,
        "end_epoch": end,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "trades": len(trades),
            "activity": len(activity),
            "current_positions": len(current),
            "closed_positions": len(closed),
        },
        "critical_parameters": {"takerOnly": False, "activity_sort": "ASC"},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
