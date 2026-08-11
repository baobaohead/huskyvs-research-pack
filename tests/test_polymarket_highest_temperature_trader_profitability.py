from __future__ import annotations

import csv
import importlib.util
import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

import src.polymarket_highest_temperature_trader_pattern_v1 as study
import src.polymarket_highest_temperature_trader_profitability as profitability


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "skills/polymarket-highest-temperature-trader-pattern-v1/scripts/run_analysis.py"
PORTABLE = ROOT / "docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json"
HUSKY = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"


def target_rows(
    event_id: str,
    weather_date: str,
    *,
    city: str = "beijing",
    status: str = "CLOSED",
    condition_id: str | None = None,
    event_slug: str | None = None,
) -> list[dict[str, str]]:
    condition = condition_id or f"condition-{event_id}"
    slug = event_slug or f"highest-temperature-in-{city}-on-{weather_date}"
    return [
        {
            "event_id": event_id,
            "canonical_city": city,
            "weather_date_local": weather_date,
            "event_slug": slug,
            "condition_id": condition,
            "asset": f"yes-{condition}",
            "outcome": "YES",
            "market_status": status,
            "slug": f"{slug}-30c",
        },
        {
            "event_id": event_id,
            "canonical_city": city,
            "weather_date_local": weather_date,
            "event_slug": slug,
            "condition_id": condition,
            "asset": f"no-{condition}",
            "outcome": "NO",
            "market_status": status,
            "slug": f"{slug}-30c",
        },
    ]


def closed_position(
    wallet: str,
    event_id: str,
    pnl: float,
    *,
    condition_id: str | None = None,
    outcome: str = "YES",
) -> dict[str, object]:
    condition = condition_id or f"condition-{event_id}"
    normalized_outcome = outcome.upper()
    return {
        "proxyWallet": wallet,
        "conditionId": condition,
        "asset": f"{normalized_outcome.lower()}-{condition}",
        "outcome": normalized_outcome.title(),
        "realizedPnl": pnl,
        "avgPrice": 0.2,
        "totalBought": 10,
        "timestamp": 1,
        "eventSlug": "",
    }


class FakeClient:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, url: str, params: dict[str, object]):
        self.calls.append((url, dict(params)))
        response = self.responses[str(params["eventId"])]
        if isinstance(response, Exception):
            raise response
        if response and isinstance(response, list) and isinstance(response[0], list):
            page_number = int(params["offset"]) // int(params["limit"])
            return response[page_number] if page_number < len(response) else []
        return response


def collect(
    wallet: str,
    targets: list[dict[str, str]],
    responses: dict[str, object],
    *,
    limit: int = 50,
    offset_cap: int = 100_000,
):
    client = FakeClient(responses)
    result = profitability.collect_profitability_evidence(
        client, wallet, targets, date(2026, 5, 1), date(2026, 8, 4), ["beijing"],
        limit=limit, offset_cap=offset_cap, max_workers=1,
    )
    return client, result


def enriched_position(wallet: str, event_id: str, city: str, weather_date: str, pnl: float) -> dict[str, object]:
    row = closed_position(wallet, event_id, pnl)
    return {
        **row,
        "_queried_event_id": event_id,
        "_canonical_city": city,
        "_weather_date": weather_date,
        "_event_slug": f"highest-temperature-in-{city}-on-{weather_date}",
        "_condition_id": f"condition-{event_id}",
        "_asset": f"yes-condition-{event_id}",
        "_outcome": "YES",
        "_realized_pnl": str(pnl),
        "_source": profitability.PNL_SOURCE,
    }


def complete_audit(wallet: str, event_id: str, city: str, weather_date: str) -> dict[str, object]:
    return {
        "wallet": wallet,
        "canonical_city": city,
        "weather_date": weather_date,
        "event_id": event_id,
        "event_slug": f"highest-temperature-in-{city}-on-{weather_date}",
        "target_condition_count": 1,
        "closed_target_condition_count": 1,
        "request_status": "COMPLETE",
        "page_count": 1,
        "raw_position_count": 1,
        "included_position_count": 1,
        "exact_duplicate_count": 0,
        "excluded_position_count": 0,
        "realized_pnl_usd": 0,
        "included_in_profitability": True,
        "issue_codes": [],
        "source": profitability.PNL_SOURCE,
    }


def ready_meta(wallet: str, event_count: int) -> dict[str, object]:
    return {
        "profitability_status": "READY",
        "profitability_status_reasons": [],
        "wallet": wallet,
        "target_event_count": event_count,
        "closed_target_event_count": event_count,
        "complete_closed_target_event_count": event_count,
        "affected_closed_target_event_count": 0,
        "excluded_not_closed_event_count": 0,
        "included_position_count": event_count,
    }


def test_official_closed_position_source_and_get_parameters_are_fixed() -> None:
    targets = target_rows("event-1", "2026-05-01")
    client, (positions, audit, meta) = collect(HUSKY, targets, {"event-1": [closed_position(HUSKY, "event-1", 2.5)]})
    assert len(positions) == 1
    assert audit[0]["request_status"] == "COMPLETE"
    assert meta["profitability_status"] == "READY"
    url, params = client.calls[0]
    assert url == "https://data-api.polymarket.com/closed-positions"
    assert params["user"] == HUSKY and params["eventId"] == "event-1"
    assert params["sortBy"] == "TIMESTAMP" and params["sortDirection"] == "ASC"
    assert profitability.PNL_SOURCE == "OFFICIAL_DATA_API_CLOSED_POSITIONS_REALIZED_PNL"


def test_scope_keeps_only_requested_city_date_and_highest_temperature() -> None:
    targets = (
        target_rows("beijing-good", "2026-05-01")
        + target_rows("shanghai", "2026-05-01", city="shanghai")
        + target_rows("outside", "2026-04-30")
        + target_rows("other-market", "2026-05-02", event_slug="will-it-rain")
    )
    scopes, issues = profitability.target_event_scopes(
        targets, date(2026, 5, 1), date(2026, 8, 4), ["beijing"]
    )
    assert [scope["event_id"] for scope in scopes] == ["beijing-good"]
    assert "NON_HIGHEST_TEMPERATURE_TARGET" in issues


def test_active_events_are_excluded_without_fabricating_pnl() -> None:
    targets = target_rows("closed", "2026-05-01") + target_rows("active", "2026-05-02", status="ACTIVE")
    client, (positions, audit, meta) = collect(HUSKY, targets, {"closed": []})
    assert len(client.calls) == 1
    assert {row["event_id"]: row["request_status"] for row in audit} == {
        "closed": "COMPLETE", "active": "EXCLUDED_NOT_CLOSED"
    }
    assert positions == [] and meta["profitability_status"] == "READY"
    assert meta["unsettled_event_count"] == 1
    assert meta["unsettled_boundary_dates"] == ["2026-05-02"]
    assert meta["unsettled_boundary_events"][0]["event_slug"].endswith("2026-05-02")
    assert meta["settled_scope_end"] == "2026-05-01"
    summary, _, _, _ = profitability.summarize_profitability(HUSKY, positions, audit, meta)
    assert summary["PROFITABILITY_STATUS"] == "READY"
    assert summary["UNSETTLED_BOUNDARY_COUNT"] == 1
    assert summary["UNSETTLED_BOUNDARY_DATES"] == ["2026-05-02"]


def test_no_closed_target_event_blocks_profitability() -> None:
    targets = target_rows("active", "2026-05-01", status="ACTIVE")
    _, (_, _, meta) = collect(HUSKY, targets, {})
    assert meta["profitability_status"] == "BLOCKED"
    assert "NO_CLOSED_TARGET_EVENTS" in meta["profitability_status_reasons"]


def test_duplicate_arch_new_events_stay_in_audit_but_merge_daily() -> None:
    positions = [
        enriched_position(HUSKY, "arch", "beijing", "2026-07-30", 10),
        enriched_position(HUSKY, "new", "beijing", "2026-07-30", -3),
    ]
    audit = [
        complete_audit(HUSKY, "arch", "beijing", "2026-07-30"),
        complete_audit(HUSKY, "new", "beijing", "2026-07-30"),
    ]
    summary, daily, _, audited = profitability.summarize_profitability(
        HUSKY, positions, audit, ready_meta(HUSKY, 2)
    )
    assert len(audited) == 2
    assert daily == [{
        "wallet": HUSKY,
        "canonical_city": "beijing",
        "weather_date": "2026-07-30",
        "event_count": 2,
        "settled_position_count": 2,
        "realized_pnl_usd": 7.0,
        "profitable_or_loss": "PROFIT",
        "source": profitability.PNL_SOURCE,
    }]
    assert summary["SETTLED_MARKET_WEATHER_DAYS"] == 1


def test_discovered_event_and_market_weather_day_counts_keep_duplicate_event_audit() -> None:
    targets = target_rows("arch", "2026-05-19") + target_rows("new", "2026-05-19")
    _, (_, audit, meta) = collect(HUSKY, targets, {"arch": [], "new": []})
    assert len(audit) == 2
    assert meta["discovered_event_count"] == 2
    assert meta["discovered_market_weather_day_count"] == 1
    assert meta["closed_event_count"] == 2
    assert meta["unsettled_event_count"] == 0


def test_two_cities_on_same_date_remain_two_profitability_days() -> None:
    positions = [
        enriched_position(HUSKY, "b", "beijing", "2026-05-01", 3),
        enriched_position(HUSKY, "s", "shanghai", "2026-05-01", 4),
    ]
    audit = [
        complete_audit(HUSKY, "b", "beijing", "2026-05-01"),
        complete_audit(HUSKY, "s", "shanghai", "2026-05-01"),
    ]
    summary, daily, _, _ = profitability.summarize_profitability(HUSKY, positions, audit, ready_meta(HUSKY, 2))
    assert len(daily) == 2
    assert summary["SETTLED_MARKET_WEATHER_DAYS"] == 2


def test_daily_monthly_statistics_zero_tolerance_streaks_and_concentration() -> None:
    facts = [
        ("e1", "2026-05-01", 10.0),
        ("e2", "2026-05-02", -5.0),
        ("e3", "2026-05-03", 0.001),
        ("e4", "2026-06-01", 20.0),
    ]
    positions = [enriched_position(HUSKY, event, "beijing", day, pnl) for event, day, pnl in facts]
    audit = [complete_audit(HUSKY, event, "beijing", day) for event, day, _ in facts]
    summary, daily, monthly, _ = profitability.summarize_profitability(HUSKY, positions, audit, ready_meta(HUSKY, 4))
    assert summary["TOTAL_SETTLED_PNL_USD"] == pytest.approx(25.001)
    assert (summary["PROFITABLE_DAYS"], summary["LOSS_DAYS"], summary["ZERO_PNL_DAYS"]) == (2, 1, 1)
    assert summary["PROFITABLE_DAY_RATE"] == pytest.approx(0.5)
    assert summary["AVERAGE_DAILY_PNL"] == pytest.approx(6.25025)
    assert summary["MEDIAN_DAILY_PNL"] == pytest.approx(5.0005)
    assert summary["MAX_DAILY_PROFIT"] == 20 and summary["MAX_DAILY_LOSS"] == -5
    assert summary["LONGEST_PROFIT_STREAK"] == 1 and summary["LONGEST_LOSS_STREAK"] == 1
    assert summary["TOP1_PROFIT_DAYS_SHARE"] == pytest.approx(2 / 3)
    assert summary["TOP1_PROFIT_DAY_SHARE"] == summary["TOP1_PROFIT_DAYS_SHARE"]
    assert summary["TOP3_PROFIT_DAYS_SHARE"] == pytest.approx(1)
    assert summary["TOP10_PROFIT_DAYS_SHARE"] == pytest.approx(1)
    assert {row["weather_month"]: row["realized_pnl_usd"] for row in monthly} == {
        "2026-05": pytest.approx(5.001), "2026-06": pytest.approx(20)
    }
    assert [row["profitable_or_loss"] for row in daily] == ["PROFIT", "LOSS", "FLAT", "PROFIT"]


def test_longest_profit_and_loss_streaks_can_exceed_one() -> None:
    pnls = [2, 3, -1, -2, -3, 4]
    positions = []
    audit = []
    for index, pnl in enumerate(pnls, start=1):
        event = f"e{index}"
        day = (date(2026, 5, 1) + timedelta(days=index - 1)).isoformat()
        positions.append(enriched_position(HUSKY, event, "beijing", day, pnl))
        audit.append(complete_audit(HUSKY, event, "beijing", day))
    summary, _, _, _ = profitability.summarize_profitability(HUSKY, positions, audit, ready_meta(HUSKY, len(pnls)))
    assert summary["LONGEST_PROFIT_STREAK"] == 2
    assert summary["LONGEST_LOSS_STREAK"] == 3


def test_month_comes_from_weather_date_not_position_timestamp() -> None:
    position = enriched_position(HUSKY, "e1", "beijing", "2026-05-31", 5)
    position["timestamp"] = 1780272000  # June UTC; must not control grouping.
    audit = [complete_audit(HUSKY, "e1", "beijing", "2026-05-31")]
    summary, _, monthly, _ = profitability.summarize_profitability(HUSKY, [position], audit, ready_meta(HUSKY, 1))
    assert summary["MONTH_MAPPING"] == "WEATHER_DATE_LOCAL_MONTH"
    assert [row["weather_month"] for row in monthly] == ["2026-05"]


def stability_fixture(**changes) -> dict[str, object]:
    payload = {
        "SETTLED_MARKET_WEATHER_DAYS": 30,
        "MONTHLY_PNL": {"2026-05": 1, "2026-06": 1, "2026-07": 1, "2026-08": 1},
        "TOTAL_SETTLED_PNL_USD": 50,
        "TOTAL_POSITIVE_DAILY_PNL_USD": 100,
        "PROFITABLE_DAY_RATE": 0.7,
        "MONTHS_WITH_POSITIVE_PNL": 4,
        "TOP3_PROFIT_DAYS_SHARE": 0.4,
        "MAX_DAILY_LOSS": -10,
        "LONGEST_LOSS_STREAK": 2,
    }
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "HIGH"),
        ({"PROFITABLE_DAY_RATE": 0.55, "MONTHS_WITH_POSITIVE_PNL": 2, "TOP3_PROFIT_DAYS_SHARE": 0.6, "MAX_DAILY_LOSS": -25, "LONGEST_LOSS_STREAK": 4}, "MEDIUM"),
        ({"TOTAL_SETTLED_PNL_USD": -1}, "LOW"),
        ({"SETTLED_MARKET_WEATHER_DAYS": 5}, "INSUFFICIENT_DATA"),
    ],
)
def test_stability_classes_are_deterministic(changes: dict[str, object], expected: str) -> None:
    assert profitability.classify_stability(stability_fixture(**changes))[0] == expected


def test_request_failure_is_partial_and_failed_event_isolated() -> None:
    targets = target_rows("good", "2026-05-01") + target_rows("bad", "2026-05-02")
    _, (positions, audit, meta) = collect(
        HUSKY, targets,
        {"good": [closed_position(HUSKY, "good", 5)], "bad": RuntimeError("boom")},
    )
    assert meta["profitability_status"] == "PARTIAL"
    assert [row["_queried_event_id"] for row in positions] == ["good"]
    assert {row["event_id"]: row["included_in_profitability"] for row in audit} == {"bad": False, "good": True}


def test_single_event_request_failure_is_blocked() -> None:
    _, (positions, _, meta) = collect(HUSKY, target_rows("bad", "2026-05-01"), {"bad": RuntimeError("boom")})
    assert positions == [] and meta["profitability_status"] == "BLOCKED"


def test_pagination_incomplete_blocks_and_discards_partial_pages() -> None:
    first_page = [
        closed_position(HUSKY, "e1", 1, outcome="YES"),
        closed_position(HUSKY, "e1", 2, outcome="NO"),
    ]
    _, (positions, audit, meta) = collect(
        HUSKY, target_rows("e1", "2026-05-01"), {"e1": [first_page]},
        limit=2, offset_cap=0,
    )
    assert positions == []
    assert audit[0]["request_status"] == "PAGINATION_INCOMPLETE"
    assert meta["profitability_status"] == "BLOCKED"


@pytest.mark.parametrize(
    ("mutation", "issue"),
    [
        (lambda row: row.update({"conditionId": "foreign"}), "CONDITION_MAPPING_CONFLICT"),
        (lambda row: row.pop("realizedPnl"), "POSITION_KEY_OR_PNL_MISSING"),
        (lambda row: row.update({"proxyWallet": "0x" + "1" * 40}), "WALLET_MAPPING_CONFLICT"),
    ],
)
def test_position_mapping_or_key_failure_blocks_event(mutation, issue: str) -> None:
    row = closed_position(HUSKY, "e1", 5)
    mutation(row)
    _, (positions, audit, meta) = collect(HUSKY, target_rows("e1", "2026-05-01"), {"e1": [row]})
    assert positions == [] and meta["profitability_status"] == "BLOCKED"
    assert issue in audit[0]["issue_codes"]


def test_unexplained_duplicate_pnl_blocks_event_but_exact_duplicate_is_deduped() -> None:
    exact = closed_position(HUSKY, "e1", 5)
    _, (positions, audit, meta) = collect(HUSKY, target_rows("e1", "2026-05-01"), {"e1": [exact, dict(exact)]})
    assert len(positions) == 1 and audit[0]["exact_duplicate_count"] == 1
    assert meta["profitability_status"] == "READY"
    conflict = dict(exact, realizedPnl=6)
    _, (positions, audit, meta) = collect(HUSKY, target_rows("e1", "2026-05-01"), {"e1": [exact, conflict]})
    assert positions == [] and meta["profitability_status"] == "BLOCKED"
    assert "UNEXPLAINED_DUPLICATE_PNL" in audit[0]["issue_codes"]


def test_condition_mapped_to_two_events_blocks_both_events() -> None:
    targets = target_rows("e1", "2026-05-01", condition_id="shared") + target_rows("e2", "2026-05-02", condition_id="shared")
    _, (positions, audit, meta) = collect(HUSKY, targets, {})
    assert positions == [] and meta["profitability_status"] == "BLOCKED"
    assert all(row["request_status"] == "MAPPING_CONFLICT" for row in audit)


def test_no_wallet_positions_is_ready_but_stability_is_insufficient() -> None:
    _, (positions, audit, meta) = collect(HUSKY, target_rows("e1", "2026-05-01"), {"e1": []})
    summary, daily, monthly, _ = profitability.summarize_profitability(HUSKY, positions, audit, meta)
    assert summary["PROFITABILITY_STATUS"] == "READY"
    assert summary["TOTAL_SETTLED_PNL_USD"] == 0
    assert summary["STABILITY_RATING"] == "INSUFFICIENT_DATA"
    assert summary["PROFITABILITY_STABILITY"] == "INSUFFICIENT_DATA"
    assert daily == [] and monthly == []


def test_profitability_outputs_have_required_files_and_columns(tmp_path: Path) -> None:
    position = enriched_position(HUSKY, "e1", "beijing", "2026-05-01", 5)
    audit = [complete_audit(HUSKY, "e1", "beijing", "2026-05-01")]
    evidence = {
        HUSKY: {
            "closed_positions": [position],
            "profitability_event_audit": audit,
            "profitability_collection_meta": ready_meta(HUSKY, 1),
        }
    }
    result = profitability.run_profitability_analysis([HUSKY], evidence, tmp_path)
    assert result["wallets"][HUSKY]["TOTAL_SETTLED_PNL_USD"] == 5
    wallet_root = tmp_path / HUSKY
    for name in (
        "profitability_summary.md", "profitability_summary.json", "daily_profitability.csv",
        "monthly_profitability.csv", "profitability_data_quality.csv", "event_profitability_audit.csv",
    ):
        assert (wallet_root / name).is_file()
    fields = next(csv.DictReader((wallet_root / "daily_profitability.csv").open(encoding="utf-8"))).keys()
    assert set(profitability.DAILY_FIELDS) <= set(fields)
    report = (wallet_root / "profitability_summary.md").read_text(encoding="utf-8")
    assert "Skill 内部用于研究对比" in report
    assert "WEATHER_DATE" not in report  # plain-language report, machine field stays in JSON/CSV
    assert "ROI" in report and "Negative Risk" in report


def load_runner():
    spec = importlib.util.spec_from_file_location("profitability_skill_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_runner_routes_profitability_and_full() -> None:
    runner = load_runner()
    payload = {
        "trader_ids": [HUSKY], "date_from": "2026-03-21", "date_to": "2026-07-23",
        "cities": ["beijing"],
    }
    for depth in ("profitability", "full"):
        command = runner.build_command(
            payload, Path("/tmp/out"), refresh_public_data=False,
            saved_public_evidence_manifest=PORTABLE, analysis_depth=depth,
        )
        assert command[command.index("--analysis-depth") + 1] == depth


def test_saved_profitability_evidence_replays_and_detects_tamper(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    position = enriched_position(HUSKY, "e1", "beijing", "2026-05-01", 5)
    audit = [complete_audit(HUSKY, "e1", "beijing", "2026-05-01")]
    profitability.save_profitability_evidence(
        root, HUSKY, date(2026, 5, 1), date(2026, 8, 4), ["beijing"],
        [position], audit, ready_meta(HUSKY, 1), [],
    )
    loaded = profitability.load_profitability_evidence(
        root / "manifest.json", [HUSKY], date(2026, 5, 1), date(2026, 8, 4)
    )
    assert loaded[HUSKY]["closed_positions"][0]["_realized_pnl"] == "5"
    (root / "closed_positions.json").write_text("[]\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="SHA256_MISMATCH"):
        profitability.load_profitability_evidence(
            root / "manifest.json", [HUSKY], date(2026, 5, 1), date(2026, 8, 4)
        )


def test_pattern_and_profitability_statuses_are_independent_in_full_mode(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(study.NO_NETWORK_ENV, "1")
    result = study.analyze(
        [HUSKY], "2026-03-21", "2026-07-23", ["beijing"], tmp_path,
        saved_public_evidence_manifest=PORTABLE, analysis_depth="full",
    )
    assert result["summaries"][0]["pattern_report_status"] == "READY"
    assert result["profitability"]["wallets"][HUSKY]["PROFITABILITY_STATUS"] == "BLOCKED"
    full_status = result["full"]["wallets"][HUSKY]
    assert full_status["PATTERN_STATUS"] == "READY"
    assert full_status["PROFITABILITY_STATUS"] == "BLOCKED"
    assert full_status["basic_pattern_status"] == "READY"
    assert full_status["advanced_pattern_status"] == "READY"
    assert full_status["full_status"] == "PARTIAL"
    assert (tmp_path / "full_trader_report.md").is_file()


def test_pattern_blocked_does_not_block_ready_profitability(tmp_path: Path, monkeypatch) -> None:
    evidence_root = tmp_path / "pnl-evidence"
    position = enriched_position(HUSKY, "e1", "beijing", "2026-05-01", 5)
    audit = [complete_audit(HUSKY, "e1", "beijing", "2026-05-01")]
    profitability.save_profitability_evidence(
        evidence_root, HUSKY, date(2026, 3, 21), date(2026, 7, 23), ["beijing"],
        [position], audit, ready_meta(HUSKY, 1), [],
    )
    original_quality = study._quality_payload

    def blocked_quality(*args, **kwargs):
        quality = original_quality(*args, **kwargs)
        quality["pattern_report_status"] = "BLOCKED_INCOMPLETE_EVIDENCE"
        quality["pattern_report_block_reason"] = "TEST_PATTERN_ONLY_BLOCK"
        return quality

    monkeypatch.setattr(study, "_quality_payload", blocked_quality)
    result = study.analyze(
        [HUSKY], "2026-03-21", "2026-07-23", ["beijing"], tmp_path / "output",
        saved_public_evidence_manifest=PORTABLE,
        saved_profitability_evidence_manifest=evidence_root / "manifest.json",
        analysis_depth="full",
    )
    assert result["summaries"][0]["pattern_report_status"] == "BLOCKED_INCOMPLETE_EVIDENCE"
    assert result["profitability"]["wallets"][HUSKY]["PROFITABILITY_STATUS"] == "READY"
    assert result["profitability"]["wallets"][HUSKY]["TOTAL_SETTLED_PNL_USD"] == 5
    assert result["full"]["wallets"][HUSKY]["full_status"] == "PARTIAL"


def test_profitability_source_contains_no_strategy_attribution_or_roi_formula() -> None:
    source = (ROOT / "src/polymarket_highest_temperature_trader_profitability.py").read_text(encoding="utf-8")
    assert "closed-positions" in source and "realizedPnl" in source
    assert "strategy_pnl" not in source.lower()
    assert "return / capital" not in source.lower()
