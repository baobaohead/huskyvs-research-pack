from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from src.polymarket_highest_temperature_trader_discovery import (
    CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION,
    DISCOVERY_SCHEMA_VERSION,
    DiscoveryEvidenceError,
    KNOWN_BENCHMARK_WALLETS,
    _cache_path,
    _discovery_status,
    _fetch_event_trades,
    _source_statuses,
    _wallet,
    aggregate_wallets,
    build_canonical_evidence_index,
    build_watchlist,
    load_manifest_evidence,
    select_candidate_pool,
    sha256_file,
    write_json,
)


def target(condition: str, day: str, event: str = "event") -> dict[str, str]:
    return {
        "canonical_city": "beijing",
        "weather_date_local": day,
        "event_id": event,
        "event_slug": f"highest-temperature-in-beijing-on-{day}",
        "condition_id": condition,
        "asset": f"asset-{condition}",
        "outcome": "YES",
    }


def fill(wallet: str, condition: str, day: str, *, side: str = "BUY", tx: str | None = None, name: str = "") -> dict[str, object]:
    return {
        "proxyWallet": wallet.upper(),
        "conditionId": condition,
        "asset": f"asset-{condition}",
        "side": side,
        "outcome": "YES",
        "size": 10,
        "price": 0.2,
        "timestamp": 1,
        "transactionHash": tx or f"0x{condition}{day.replace('-', '')}",
        "name": name,
    }


def make_participant(wallet: str, days: list[str], *, prefix: str = "c", name: str = "") -> list[dict[str, object]]:
    return [fill(wallet, f"{prefix}{index}", day, tx=f"tx-{wallet[-4:]}-{index}", name=name) for index, day in enumerate(days)]


def test_aggregate_normalizes_wallet_and_collapses_duplicate_weather_day() -> None:
    wallet = "0x" + "a" * 40
    targets = [target("c1", "2026-03-01", "new"), target("c2", "2026-03-01", "arch")]
    fills = [
        fill(wallet, "c1", "2026-03-01", tx="one"),
        fill(wallet, "c2", "2026-03-01", tx="two", side="SELL"),
    ]
    rows, quality = aggregate_wallets(fills, targets)
    assert quality["invalid_wallet_rows"] == 0
    assert quality["invalid_target_rows"] == 0
    assert quality["invalid_unique_wallet_count"] == 0
    assert quality["invalid_proxy_wallet_evidence"] == []
    assert rows[0]["wallet"] == wallet
    assert rows[0]["active_weather_days"] == 1
    assert rows[0]["fill_count"] == 2
    assert rows[0]["buy_fill_count"] == 1
    assert rows[0]["sell_fill_count"] == 1


def test_active_weeks_and_months_are_distinct() -> None:
    wallet = "0x" + "b" * 40
    days = ["2026-03-01", "2026-03-08", "2026-04-01"]
    targets = [target(f"c{i}", day) for i, day in enumerate(days)]
    rows, _ = aggregate_wallets(make_participant(wallet, days), targets)
    assert rows[0]["active_weather_days"] == 3
    assert rows[0]["active_weeks"] == 3
    assert rows[0]["active_months"] == 2


def test_watchlist_is_five_to_nine_days_only() -> None:
    wallet5 = "0x" + "c" * 40
    wallet4 = "0x" + "d" * 40
    targets = [target(f"c{i}", f"2026-03-{i + 1:02d}") for i in range(5)]
    rows, _ = aggregate_wallets(make_participant(wallet5, [t["weather_date_local"] for t in targets]), targets)
    rows4, _ = aggregate_wallets(make_participant(wallet4, ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04"]), [target(f"d{i}", f"2026-03-{i + 1:02d}") for i in range(4)])
    assert len(build_watchlist(rows)) == 1
    assert build_watchlist(rows4) == []


def test_three_channels_are_deduplicated_and_benchmark_does_not_take_slot() -> None:
    benchmark = next(iter(KNOWN_BENCHMARK_WALLETS))
    long_wallet = "0x" + "1" * 40
    emerging_wallet = "0x" + "2" * 40
    days_long = [f"2026-03-{i + 1:02d}" for i in range(10)] + ["2026-04-15"]
    days_emerging = ["2026-07-01", "2026-07-08", "2026-07-15", "2026-07-22", "2026-07-29", "2026-08-01", "2026-08-02", "2026-08-03"]
    targets = [target(f"l{i}", day) for i, day in enumerate(days_long)]
    targets += [target(f"e{i}", day) for i, day in enumerate(days_emerging)]
    rows, _ = aggregate_wallets(
        make_participant(long_wallet, days_long, prefix="l")
        + make_participant(emerging_wallet, days_emerging, prefix="e")
        + make_participant(benchmark, days_long, prefix="l"),
        targets,
    )
    candidates, counts = select_candidate_pool(rows, date_to=date(2026, 8, 10))
    wallets = {row["wallet"] for row in candidates}
    assert benchmark not in wallets
    assert long_wallet in wallets
    assert emerging_wallet in wallets
    assert len(wallets) == len(candidates)
    assert counts["long_term_active_selected"] == 0
    assert counts["emerging_high_density_selected"] == 1


def test_candidate_pool_marks_profitability_not_run_and_is_capped() -> None:
    wallets = []
    targets = []
    fills = []
    days = ["2026-03-01", "2026-03-08", "2026-03-15", "2026-03-22", "2026-03-29", "2026-04-01", "2026-04-08", "2026-04-15", "2026-04-22", "2026-04-29"]
    for wallet_index in range(35):
        wallet = "0x" + f"{wallet_index + 1:040x}"
        for index, day in enumerate(days):
            condition = f"x{wallet_index}-{index}"
            targets.append(target(condition, day))
            fills.append(fill(wallet, condition, day, tx=f"tx-{wallet_index}-{index}"))
    rows, _ = aggregate_wallets(fills, targets)
    candidates, _ = select_candidate_pool(rows, date_to=date(2026, 8, 10))
    assert len(candidates) == 30
    assert [row["discovery_priority_rank"] for row in candidates] == list(range(1, 31))
    assert all(row["profitability_run_status"] == "NOT_RUN" for row in candidates)


class FakeEventClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_json(self, _url: str, params: dict[str, object]) -> list[dict[str, object]]:
        self.calls.append(dict(params))
        side = params.get("side")
        offset = int(params["offset"])
        if side is None:
            if offset == 0:
                return [{"proxyWallet": "0x" + "a" * 40, "timestamp": index, "conditionId": "c"} for index in range(10000)]
            return [{"proxyWallet": "0x" + "b" * 40, "timestamp": index + 10000, "conditionId": "c"} for index in range(10000)]
        count = 3 if side == "BUY" and offset == 0 else 2 if side == "SELL" and offset == 0 else 0
        return [{"proxyWallet": "0x" + ("c" if side == "BUY" else "d") * 40, "timestamp": offset + index, "conditionId": "c", "side": side} for index in range(count)]


def test_saturated_event_falls_back_to_complete_buy_sell_pages() -> None:
    client = FakeEventClient()
    event_id, result = _fetch_event_trades(client, "349010")
    assert event_id == "349010"
    assert result.status == "COMPLETE"
    assert len(result.rows) == 5
    assert any(call.get("side") == "BUY" for call in client.calls)
    assert any(call.get("side") == "SELL" for call in client.calls)


def test_incomplete_audit_blocks_discovery_even_with_targets() -> None:
    status = _discovery_status(
        [{"condition_id": "c1"}],
        [{"condition_id": "c1", "completeness_status": "PAGINATION_INCOMPLETE"}],
        request_failures=0,
        invalid_wallet_rows=0,
    )
    assert status == "BLOCKED_INCOMPLETE_EVIDENCE"


def test_wallet_identity_is_strict_and_normalizes_only_valid_addresses() -> None:
    wallet = "0x" + "ab" * 20
    assert _wallet(f"  0X{('AB' * 20)}  ") == wallet
    assert _wallet(wallet[:-1]) == ""
    assert _wallet(wallet[:-1] + "g") == ""
    assert _wallet("wallet-name") == ""


def test_invalid_proxy_wallet_evidence_blocks_discovery() -> None:
    targets = [target("c1", "2026-03-01")]
    rows, quality = aggregate_wallets([fill("not-a-wallet", "c1", "2026-03-01")], targets)
    assert rows == []
    assert quality["invalid_wallet_rows"] == 1
    assert quality["invalid_unique_wallet_count"] == 1
    assert quality["invalid_proxy_wallet_evidence"] == [{
        "reason": "INVALID_PROXY_WALLET_EVIDENCE",
        "raw_proxy_wallet": "not-a-wallet",
        "row_count": 1,
    }]
    assert _discovery_status(
        targets,
        [{"condition_id": "c1", "completeness_status": "COMPLETE"}],
        request_failures=0,
        invalid_wallet_rows=quality["invalid_wallet_rows"],
    ) == "BLOCKED_INCOMPLETE_EVIDENCE"


def test_request_failure_fails_closed_and_external_not_available_is_explicit() -> None:
    assert _source_statuses()["manual_candidate"]["status"] == "NOT_AVAILABLE"
    assert _discovery_status(
        [{"condition_id": "c1"}],
        [{"condition_id": "c1", "completeness_status": "COMPLETE"}],
        request_failures=1,
        invalid_wallet_rows=0,
    ) == "BLOCKED_INCOMPLETE_EVIDENCE"


def _selection_row(wallet: str, *, first: str = "2026-03-01", external: int = 0) -> dict[str, object]:
    return {
        "wallet": wallet,
        "known_benchmark_wallet": "NO",
        "first_beijing_trade_date": first,
        "last_beijing_trade_date": "2026-08-01",
        "active_weather_days": 10,
        "active_weeks": 6,
        "active_months": 2,
        "activity_density": 0.5,
        "fill_count": 20,
        "external_signal_count": external,
        "top_holder_weather_days": 0,
    }


def test_candidate_tie_break_is_deterministic_and_reserve_fills_after_long_term_slots() -> None:
    low_wallet = "0x" + "1" * 40
    high_wallet = "0x" + "2" * 40
    tied, _ = select_candidate_pool(
        [_selection_row(high_wallet), _selection_row(low_wallet)],
        date_to=date(2026, 8, 10),
    )
    assert [row["wallet"] for row in tied] == [low_wallet, high_wallet]

    rows = [_selection_row("0x" + f"{index + 1:040x}") for index in range(19)]
    candidates, counts = select_candidate_pool(rows, date_to=date(2026, 8, 10))
    assert len(candidates) == 19
    assert counts["long_term_active_selected"] == 18
    assert counts["general_reserve_selected"] == 1
    assert candidates[-1]["selection_channel"] == "GENERAL_RESERVE"


def _write_manifest_fixture(root: Path) -> Path:
    evidence_root = root / "_public_evidence"
    target_markets = [target("c1", "2026-03-01")]
    row = fill("0x" + "a" * 40, "c1", "2026-03-01")
    audit = {
        "canonical_city": "beijing",
        "weather_date_local": "2026-03-01",
        "event_id": "event",
        "event_slug": "highest-temperature-in-beijing-on-2026-03-01",
        "condition_id": "c1",
        "activity_status": "NOT_USED_V1",
        "trades_status": "COMPLETE",
        "completeness_status": "COMPLETE",
    }
    cache_path = _cache_path(evidence_root, "c1")
    write_json(cache_path, {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "condition_id": "c1",
        "activity_status": "NOT_USED_V1",
        "trades_status": "COMPLETE",
        "union_rows": [row],
        "audit": audit,
    })
    index_path = build_canonical_evidence_index(
        evidence_root,
        target_markets,
        [audit],
        date_from="2026-03-01",
        date_to="2026-03-01",
    )
    manifest = {
        "schema_version": DISCOVERY_SCHEMA_VERSION,
        "city": "beijing",
        "icao": "ZBAA",
        "date_from": "2026-03-01",
        "date_to": "2026-03-01",
        "target_event_count": 1,
        "target_condition_count": 1,
        "target_condition_ids": ["c1"],
        "total_public_fills": 1,
        "canonical_evidence_index": "_public_evidence/canonical_evidence_index.json",
        "canonical_evidence_index_sha256": sha256_file(index_path),
        "canonical_evidence_index_schema_version": CANONICAL_EVIDENCE_INDEX_SCHEMA_VERSION,
    }
    manifest_path = root / "discovery_manifest.json"
    write_json(manifest_path, manifest)
    return manifest_path


def test_manifest_loader_uses_exact_references_and_ignores_unreferenced_raw(tmp_path: Path) -> None:
    manifest_path = _write_manifest_fixture(tmp_path)
    garbage = tmp_path / "_public_evidence" / "raw_event_batch" / "unreferenced.json"
    garbage.parent.mkdir(parents=True)
    garbage.write_text("not-json", encoding="utf-8")
    targets, fills, audits, cache_audits = load_manifest_evidence(manifest_path)
    assert len(targets) == len(fills) == len(audits) == len(cache_audits) == 1
    assert cache_audits[0]["method"] == "MANIFEST_CACHE"

    cache_path = _cache_path(tmp_path / "_public_evidence", "c1")
    cache_path.unlink()
    with pytest.raises(DiscoveryEvidenceError):
        load_manifest_evidence(manifest_path)
