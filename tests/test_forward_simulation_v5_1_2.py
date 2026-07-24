import csv
import json
import math
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.forward_simulation_v5_1_2 import (
    collect_orderbook_round,
    event_key,
    fee_rate_from_market,
    formal_isolation,
    market_is_live_tradable,
    parse_temperature,
    read_only_scan,
    run_failure_probe,
    validate_token_mapping,
    write_fee_rows,
    write_status_rows,
    write_vwap_rows,
)
from src.polymarket_public_adapter_v5_1_2 import (
    AdapterError,
    CLOB_BASE,
    GAMMA_BASE,
    HttpResult,
    PublicAdapter,
    content_hash,
    normalize_orderbook,
    official_fee,
    parse_market_status,
    parse_weather_market,
    simulate_buy_vwap,
    simulate_sell_vwap,
    token_mapping_from_market,
)


def fixture_market(**extra):
    market = {
        "question": "Highest temperature in London on July 22?",
        "title": "Highest temperature in London on July 22?",
        "slug": "highest-temperature-in-london-on-july-22-2026-22c",
        "conditionId": "0xcondition",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        "outcomePrices": json.dumps(["0.07", "0.93"]),
        "active": True,
        "closed": False,
        "resolved": False,
        "feesEnabled": True,
        "feeSchedule": {"rate": 0.05, "exponent": 1},
        "endDate": "2026-07-22T23:59:00Z",
        "groupItemTitle": "22C",
        "acceptingOrders": True,
    }
    market.update(extra)
    return market


def fixture_clob():
    return {
        "t": [{"t": "yes-token", "o": "Yes"}, {"t": "no-token", "o": "No"}],
        "mos": 1,
        "mts": 0.001,
        "mbf": 0,
        "tbf": 500,
        "fd": {"r": 0.05, "e": 1, "to": True},
    }


def fixture_book(**extra):
    book = {
        "market": "0xcondition",
        "asset_id": "yes-token",
        "timestamp": "123",
        "hash": "bookhash",
        "bids": [{"price": "0.18", "size": "5"}, {"price": "0.17", "size": "10"}],
        "asks": [{"price": "0.22", "size": "10"}, {"price": "0.25", "size": "20"}],
        "min_order_size": "1",
        "tick_size": "0.001",
        "neg_risk": False,
        "last_trade_price": "0.20",
    }
    book.update(extra)
    return book


def http_result(payload, url="https://clob.polymarket.com/book?token_id=yes-token"):
    return HttpResult("GET", url, 200, 1.0, "2026-07-21T00:00:00+00:00", "2026-07-21T00:00:01+00:00", payload, json.dumps(payload))


def read_csv(path: Path):
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_real_api_fixture_fields_parse():
    info = parse_weather_market(fixture_market(), "Highest temperature in London on July 22?")
    assert info["city"] == "London"
    assert info["weather_metric"] == "high"
    assert info["weather_date_local"] == "2026-07-22"
    assert parse_temperature("22C") == ("22", "C")
    assert event_key("London", "2026-07-22", "high") == "london|2026-07-22|high"


def test_yes_no_token_mapping_prefers_clob_info():
    mapping = token_mapping_from_market(fixture_market(clobTokenIds=json.dumps(["wrong", "wrong2"])), fixture_clob())
    assert mapping == [{"outcome": "Yes", "token_id": "yes-token"}, {"outcome": "No", "token_id": "no-token"}]


def test_gamma_token_mapping_fallback():
    mapping = token_mapping_from_market(fixture_market(), None)
    assert mapping[0]["outcome"] == "Yes"
    assert mapping[0]["token_id"] == "yes-token"


def test_bids_and_asks_direction():
    book = normalize_orderbook(fixture_book())
    assert book["best_bid"] == pytest.approx(0.18)
    assert book["best_ask"] == pytest.approx(0.22)
    assert book["spread"] == pytest.approx(0.04)


def test_orderbook_sorting_does_not_depend_on_api_order():
    book = normalize_orderbook(fixture_book(bids=[{"price": "0.17", "size": "1"}, {"price": "0.19", "size": "2"}], asks=[{"price": "0.30", "size": "1"}, {"price": "0.21", "size": "2"}]))
    assert [x["price"] for x in book["bids"]] == [0.19, 0.17]
    assert [x["price"] for x in book["asks"]] == [0.21, 0.30]


def test_buy_vwap_consumes_asks():
    book = {"asks": [{"price": 0.2, "size": 10}, {"price": 0.4, "size": 10}], "bids": [], "best_ask": 0.2, "best_bid": None}
    calc = simulate_buy_vwap(book, 6)
    assert calc["filled_usd"] == pytest.approx(6)
    assert calc["filled_shares"] == pytest.approx(20)
    assert calc["vwap"] == pytest.approx(0.3)
    assert calc["depth_levels_consumed"] == 2


def test_sell_vwap_consumes_bids():
    book = {"bids": [{"price": 0.5, "size": 5}, {"price": 0.4, "size": 10}], "asks": [], "best_bid": 0.5, "best_ask": None}
    calc = simulate_sell_vwap(book, 10)
    assert calc["filled_usd"] == pytest.approx(4.5)
    assert calc["filled_shares"] == pytest.approx(10)
    assert calc["vwap"] == pytest.approx(0.45)


def test_partial_buy_fill_does_not_exceed_depth():
    book = {"asks": [{"price": 0.5, "size": 10}], "bids": [], "best_ask": 0.5, "best_bid": None}
    calc = simulate_buy_vwap(book, 10)
    assert calc["filled_usd"] == pytest.approx(5)
    assert calc["unfilled_amount"] == pytest.approx(5)
    assert not calc["fully_filled"]


def test_partial_sell_fill_does_not_exceed_depth():
    book = {"bids": [{"price": 0.4, "size": 3}], "asks": [], "best_bid": 0.4, "best_ask": None}
    calc = simulate_sell_vwap(book, 10)
    assert calc["filled_shares"] == pytest.approx(3)
    assert calc["unfilled_amount"] == pytest.approx(7)
    assert not calc["fully_filled"]


def test_empty_orderbook_is_marked_without_zero_price():
    book = normalize_orderbook(fixture_book(bids=[], asks=[]))
    assert book["empty"] is True
    assert book["best_bid"] is None
    assert book["best_ask"] is None
    assert math.isnan(simulate_buy_vwap(book, 1)["vwap"])


def test_duplicate_snapshot_has_stable_content_hash_and_one_vwap_set(tmp_path):
    book = normalize_orderbook(fixture_book())
    snap = {"snapshot_id": "dup", "token_id": "yes-token", "bids": book["bids"], "asks": book["asks"], "best_bid": book["best_bid"], "best_ask": book["best_ask"]}
    rows = write_vwap_rows(tmp_path, [snap, dict(snap)], {"live_rehearsal": {"virtual_buy_usd": [1], "virtual_sell_shares": [1]}})
    assert content_hash(fixture_book()) == content_hash(fixture_book())
    assert len(rows) == 2


def test_crossed_book_is_rejected():
    with pytest.raises(AdapterError, match="best bid"):
        normalize_orderbook(fixture_book(bids=[{"price": "0.6", "size": "1"}], asks=[{"price": "0.5", "size": "1"}]))


def test_invalid_price_is_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(fixture_book(asks=[{"price": "1.2", "size": "1"}]))


def test_negative_size_is_rejected():
    with pytest.raises(AdapterError):
        normalize_orderbook(fixture_book(bids=[{"price": "0.2", "size": "-1"}]))


def test_fee_disabled_is_zero():
    calc = official_fee(10, 0.5, False, 0.05)
    assert calc["fee_status"] == "disabled"
    assert calc["official_fee"] == 0


def test_official_fee_formula_and_rounding():
    calc = official_fee(10, 0.30, True, 0.05)
    assert calc["fee_status"] == "official"
    assert calc["official_fee"] == pytest.approx(0.105)


def test_fee_changes_with_price():
    fee_low = official_fee(10, 0.10, True, 0.05)["official_fee"]
    fee_mid = official_fee(10, 0.50, True, 0.05)["official_fee"]
    assert fee_mid > fee_low


def test_partial_fill_fee_only_uses_filled_shares():
    calc = official_fee(3, 0.40, True, 0.05)
    assert calc["gross_notional"] == pytest.approx(1.2)
    assert calc["official_fee"] == pytest.approx(0.036)


def test_unknown_fee_is_not_zero():
    calc = official_fee(10, 0.5, True, None)
    assert calc["fee_status"] == "unknown"
    assert calc["official_fee"] is None


def test_fee_rate_bps_normalized_from_clob():
    enabled, rate, exp, source = fee_rate_from_market({"raw_market": {"feesEnabled": True}, "clob_info": {"tbf": 500}})
    assert enabled is True
    assert rate == pytest.approx(0.05)
    assert exp == pytest.approx(1)
    assert source == "clob.tbf"


def test_market_status_active_resolution_pending_and_resolved():
    assert parse_market_status(fixture_market())["market_status"] == "active"
    assert parse_market_status(fixture_market(active=False, closed=True, resolved=False))["market_status"] == "resolution_pending"
    resolved = parse_market_status(fixture_market(active=False, closed=True, resolved=True, winningOutcome="Yes"))
    assert resolved["market_status"] == "resolved"
    assert resolved["winning_outcome"] == "Yes"


def test_resolved_evidence_parses_token_value(tmp_path):
    item = {
        "condition_id": "0xcondition",
        "market_slug": "slug",
        "outcome_label": "Yes",
        "raw_market": fixture_market(active=False, closed=True, resolved=True, winningOutcome="Yes"),
    }
    rows = write_status_rows(tmp_path, [item])
    assert rows[0]["market_status"] == "resolved"
    assert rows[0]["token_settlement_value"] == "1"


def test_unresolved_market_does_not_create_settlement_value(tmp_path):
    item = {"condition_id": "0xcondition", "market_slug": "slug", "outcome_label": "Yes", "raw_market": fixture_market()}
    rows = write_status_rows(tmp_path, [item])
    assert rows[0]["market_status"] == "active"
    assert rows[0]["token_settlement_value"] == ""


def test_timeout_like_failure_recovers_with_retry():
    calls = []

    def transport(url, method, timeout):
        calls.append(method)
        if len(calls) == 1:
            raise AdapterError("network_error", "fixture failure", endpoint=url)
        return 200, "{\"ok\": true}"

    adapter = PublicAdapter(max_retries=1, backoff_seconds=0, transport=transport)
    result = adapter.get_json(GAMMA_BASE, "/public-search", {"q": "temperature"})
    assert result.payload == {"ok": True}
    assert len(calls) == 2


def test_http_429_uses_limited_retry():
    calls = []

    def transport(url, method, timeout):
        calls.append(method)
        return (429, "{}") if len(calls) == 1 else (200, "{\"ok\": true}")

    adapter = PublicAdapter(max_retries=1, backoff_seconds=0, transport=transport)
    result = adapter.get_json(GAMMA_BASE, "/public-search", {"q": "temperature"})
    assert result.payload["ok"] is True
    assert adapter.audit_events[0]["category"] == "rate_limited"


def test_non_json_response_is_classified():
    adapter = PublicAdapter(max_retries=0, transport=lambda url, method, timeout: (200, "not-json"))
    with pytest.raises(AdapterError) as exc:
        adapter.get_json(GAMMA_BASE, "/public-search", {"q": "temperature"})
    assert exc.value.category == "json_error"


def test_single_token_failure_does_not_stop_other_token(tmp_path):
    class FakeAdapter:
        def orderbook(self, token_id):
            if token_id == "bad-token":
                raise AdapterError("http_error", "bad token", status_code=404)
            return http_result(fixture_book(asset_id=token_id), url=f"{CLOB_BASE}/book?token_id={token_id}")

    markets = [
        {"token_id": "bad-token", "market_slug": "m1", "condition_id": "c1"},
        {"token_id": "good-token", "market_slug": "m2", "condition_id": "c2"},
    ]
    snapshots, raw_index = collect_orderbook_round(FakeAdapter(), tmp_path, markets, 0)
    assert raw_index == 1
    assert len(snapshots) == 1
    assert "bad-token" in (tmp_path / "adapter_audit_log.jsonl").read_text(encoding="utf-8")


def test_live_and_formal_directories_are_isolated(tmp_path):
    formal = tmp_path / "data/forward_v5_1/formal"
    formal.mkdir(parents=True)
    (formal / "system_state.json").write_text("{\"formal_started_at_utc\": null}", encoding="utf-8")
    (formal / "signals.csv").write_text("signal_id\n", encoding="utf-8")
    (formal / "orderbook_snapshots.jsonl").write_text("", encoding="utf-8")
    proof = formal_isolation(tmp_path, tmp_path / "data/forward_v5_1_2/live_integration")
    assert proof["ok"] is True
    assert proof["formal_dirs"]["data/forward_v5_1/formal"]["row_counts"]["signals.csv"] == 0


def test_formal_directory_pollution_fails_isolation(tmp_path):
    formal = tmp_path / "data/forward_v5_1/formal"
    formal.mkdir(parents=True)
    (formal / "signals.csv").write_text("signal_id\nsig1\n", encoding="utf-8")
    proof = formal_isolation(tmp_path, tmp_path / "data/forward_v5_1_2/live_integration")
    assert proof["ok"] is False


def test_static_read_only_scan_passes_new_project_code(tmp_path):
    result = read_only_scan(Path(__file__).resolve().parents[1], tmp_path)
    assert result["ok"] is True
    assert result["forbidden_findings"] == []


def test_network_failure_probe_records_no_false_fill(tmp_path):
    probe = run_failure_probe(tmp_path)
    assert probe["ok"] is True
    assert probe["false_fill_created"] is False


def test_token_mapping_validation_outputs_event_keys(tmp_path):
    markets = [{
        "raw_market": fixture_market(),
        "clob_info": fixture_clob(),
        "event_title": "Highest temperature in London on July 22?",
        "condition_id": "0xcondition",
        "market_slug": "slug",
        "outcome_label": "Yes",
    }]
    rows = validate_token_mapping(markets, tmp_path)
    assert len(rows) == 2
    assert all(r["mapping_valid"] == "true" for r in rows)
    assert rows[0]["event_key"] == "london|2026-07-22|high"


def test_market_live_tradable_filter_rejects_closed_or_unresolved():
    assert market_is_live_tradable(fixture_market()) is True
    assert market_is_live_tradable(fixture_market(closed=True)) is False
    assert market_is_live_tradable(fixture_market(resolved=True)) is False


def test_write_fee_rows_keeps_unknown_separate(tmp_path):
    book = normalize_orderbook(fixture_book())
    snap = {"snapshot_id": "s1", "token_id": "yes-token", "market_slug": "slug", "best_ask": book["best_ask"], "best_bid": book["best_bid"]}
    rows = write_fee_rows(tmp_path, [{"token_id": "yes-token", "market_slug": "slug", "raw_market": {"feesEnabled": True}, "clob_info": {}}], [snap])
    assert rows[0]["fee_status"] == "unknown"
    assert rows[0]["official_fee"] is None


def test_offline_fixture_run_is_deterministic(tmp_path):
    book = normalize_orderbook(fixture_book())
    snap = {"snapshot_id": "stable", "token_id": "yes-token", "bids": book["bids"], "asks": book["asks"], "best_bid": book["best_bid"], "best_ask": book["best_ask"]}
    rows1 = write_vwap_rows(tmp_path / "one", [snap], {"live_rehearsal": {"virtual_buy_usd": [1, 5], "virtual_sell_shares": [1]}})
    rows2 = write_vwap_rows(tmp_path / "two", [snap], {"live_rehearsal": {"virtual_buy_usd": [1, 5], "virtual_sell_shares": [1]}})
    assert rows1 == rows2
