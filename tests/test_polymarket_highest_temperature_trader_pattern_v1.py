from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.polymarket_highest_temperature_trader_pattern_v1 as study


HUSKY = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
PORTABLE = Path("docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json")
REVIEWED_FILLS = Path("docs/husky_beijing_full_trade_study_v1/beijing_all_public_fills.csv")
TMP = Path("/tmp/polymarket_highest_temperature_trader_pattern_v1/tests")


def raw_fill(
    *,
    wallet: str = HUSKY,
    city: str = "beijing",
    weather_date: str = "2026-07-20",
    bucket: str = "31c",
    title_bucket: str = "31°C",
    outcome: str = "Yes",
    side: str = "BUY",
    price: float = 0.2,
    size: float = 10,
    timestamp: int | None = None,
    tx: str = "0xabc",
    asset: str = "asset-1",
) -> dict:
    day = datetime.fromisoformat(weather_date)
    month = day.strftime("%B").lower()
    event = f"highest-temperature-in-{city}-on-{month}-{day.day}-{day.year}"
    if timestamp is None:
        timestamp = int(datetime(2026, 7, 20, 2, tzinfo=timezone.utc).timestamp())
    return {
        "proxyWallet": wallet,
        "eventSlug": event,
        "slug": f"{event}-{bucket}",
        "title": f"Will the highest temperature in {city.replace('-', ' ').title()} be {title_bucket} on {day.strftime('%B')} {day.day}?",
        "conditionId": f"condition-{asset}",
        "asset": asset,
        "outcome": outcome,
        "side": side,
        "price": price,
        "size": size,
        "timestamp": timestamp,
        "transactionHash": tx,
    }


def normalized_fill(**changes) -> dict:
    raw = raw_fill(**{key: value for key, value in changes.items() if key in {
        "wallet", "city", "weather_date", "bucket", "title_bucket", "outcome",
        "side", "price", "size", "timestamp", "tx", "asset",
    }})
    fills, _, _ = study.normalize_fill_rows(
        raw["proxyWallet"].lower(), [raw], date_from=datetime(2026, 7, 20).date(),
        date_to=datetime(2026, 7, 20).date(), cities=[],
        timezone_registry={"beijing": "Asia/Shanghai"},
    )
    row = fills[0]
    row.update({key: value for key, value in changes.items() if key not in {
        "wallet", "city", "weather_date", "bucket", "title_bucket", "outcome",
        "side", "price", "size", "timestamp", "tx", "asset",
    }})
    return row


def write_evidence(root: Path, wallet: str, rows: list[dict], start="2026-07-20", end="2026-07-20") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    activity = [{**row, "type": "TRADE", "usdcSize": row["price"] * row["size"]} for row in rows]
    aggregates = {}
    for name, payload in (("trades", rows), ("activity", activity)):
        path = root / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        aggregates[name] = {
            "relative_path": path.name,
            "record_count": len(payload),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {
        "schema_version": study.EVIDENCE_SCHEMA,
        "wallet": wallet,
        "weather_date_from": start,
        "weather_date_to": end,
        "collection_start_utc": "2026-07-17T00:00:00+00:00",
        "collection_end_utc": "2026-07-23T23:59:59+00:00",
        "public_data_only": True,
        "public_get_only": True,
        "account_connection": False,
        "signing": False,
        "real_order": False,
        "pagination_saturation_status": "COMPLETE",
        "requests": [],
        "aggregates": aggregates,
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_single_wallet_and_lowercase_normalization():
    assert study.normalize_wallets([HUSKY.upper().replace("0X", "0x")]) == [HUSKY]


def test_multiple_wallets_and_duplicate_deduplication():
    other = "0x" + "1" * 40
    assert study.normalize_wallets([HUSKY, HUSKY.upper().replace("0X", "0x"), other]) == [HUSKY, other]


@pytest.mark.parametrize("wallet", ["", "0x1", "1" * 42, "0x" + "g" * 40, "0x" + "1" * 41])
def test_invalid_wallet_rejected(wallet):
    with pytest.raises(ValueError):
        study.normalize_wallets([wallet])


def test_multiple_wallets_do_not_mix(tmp_path):
    w1, w2 = "0x" + "1" * 40, "0x" + "2" * 40
    evidence = tmp_path / "evidence"
    m1 = write_evidence(evidence / w1, w1, [raw_fill(wallet=w1, tx="0x1", asset="a1")])
    m2 = write_evidence(evidence / w2, w2, [raw_fill(wallet=w2, tx="0x2", asset="a2")])
    root_manifest = evidence / "manifest.json"
    root_manifest.write_text(json.dumps({
        "schema_version": f"{study.EVIDENCE_SCHEMA}_multi_wallet",
        "public_data_only": True, "public_get_only": True,
        "account_connection": False, "signing": False, "real_order": False,
        "wallet_manifests": [str(m1.relative_to(evidence)), str(m2.relative_to(evidence))],
    }), encoding="utf-8")
    output = TMP / "multi_wallet"
    result = study.analyze([w1, w2], "2026-07-20", "2026-07-20", [], output, saved_public_evidence_manifest=root_manifest)
    assert [row["wallet"] for row in result["summaries"]] == [w1, w2]
    assert all(row["total_public_fill_count"] == 1 for row in result["summaries"])
    assert (output / w1 / "all_fills.csv").is_file()
    assert (output / w2 / "all_fills.csv").is_file()


@pytest.mark.parametrize(
    ("city", "expected"),
    [("Beijing", "beijing"), ("new york", "new-york"), ("new-york", "new-york"), ("NEW_YORK", "new-york")],
)
def test_city_normalization(city, expected):
    assert study.canonical_city(city) == expected


@pytest.mark.parametrize("city", ["beijing", "shanghai", "new-york"])
def test_event_slug_city_and_date_parsing(city):
    row = raw_fill(city=city)
    parsed = study.parse_highest_temperature_market(row)
    assert parsed["canonical_city"] == city
    assert parsed["weather_date_local"] == "2026-07-20"


def test_market_slug_temperature_suffix_parses():
    parsed = study.parse_highest_temperature_market(raw_fill(bucket="31c", title_bucket="31°C"))
    assert parsed["temperature_bucket"] == "31°C"
    assert parsed["bucket_kind"] == "exact"


def test_title_assists_when_event_slug_missing():
    row = raw_fill()
    row["eventSlug"] = ""
    row["slug"] = ""
    row["title"] = "Will the highest temperature in Beijing be 31°C on July 20, 2026?"
    parsed = study.parse_highest_temperature_market(row)
    assert parsed["canonical_city"] == "beijing"
    assert parsed["weather_date_local"] == "2026-07-20"


@pytest.mark.parametrize("title", [
    "Will it rain in Beijing on July 20?",
    "Will the lowest temperature in Beijing be 20°C on July 20?",
    "Weather forecast for Beijing",
])
def test_non_highest_markets_excluded(title):
    row = raw_fill()
    row["eventSlug"] = "other-market"
    row["slug"] = "other-market"
    row["title"] = title
    assert study.parse_highest_temperature_market(row) is None


def test_all_cities_default_and_explicit_city_filter():
    rows = [raw_fill(city="beijing", tx="0x1"), raw_fill(city="shanghai", tx="0x2", asset="a2")]
    all_fills, _, _ = study.normalize_fill_rows(HUSKY, rows, date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=[], timezone_registry={"beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai"})
    only_beijing, _, _ = study.normalize_fill_rows(HUSKY, rows, date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=["beijing"], timezone_registry={"beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai"})
    assert {row["canonical_city"] for row in all_fills} == {"beijing", "shanghai"}
    assert {row["canonical_city"] for row in only_beijing} == {"beijing"}


def test_slug_title_conflict_is_marked_and_not_silently_used():
    row = raw_fill(city="beijing")
    row["title"] = "Will the highest temperature in Shanghai be 31°C on July 20?"
    assert study.parse_highest_temperature_market(row)["market_identity_status"] == "MARKET_IDENTITY_CONFLICT"


@pytest.mark.parametrize(
    ("bucket", "title_bucket", "kind", "unit"),
    [
        ("31c", "31°C", "exact", "C"),
        ("31corbelow", "31°C or below", "below", "C"),
        ("31corhigher", "31°C or higher", "above", "C"),
        ("88f", "88°F", "exact", "F"),
        ("88forbelow", "88°F or below", "below", "F"),
        ("88forhigher", "88°F or higher", "above", "F"),
    ],
)
def test_temperature_bucket_kinds_and_units(bucket, title_bucket, kind, unit):
    parsed = study.parse_highest_temperature_market(raw_fill(bucket=bucket, title_bucket=title_bucket))
    assert parsed["bucket_kind"] == kind
    assert parsed["unit"] == unit


@pytest.mark.parametrize("factor", [1, 1_000, 1_000_000, 1_000_000_000])
def test_epoch_seconds_magnitude_normalization(factor):
    assert study.epoch_seconds(1_774_000_000 * factor) == 1_774_000_000


def test_utc_beijing_and_market_local_outputs():
    ts = int(datetime(2026, 7, 20, 0, tzinfo=timezone.utc).timestamp())
    result = study.classify_relative_weather_time(ts, "2026-07-20", "Asia/Shanghai")
    assert result["trade_time_utc"].endswith("+00:00")
    assert result["trade_time_beijing"].endswith("+08:00")
    assert result["trade_time_market_local"].endswith("+08:00")


def test_unknown_timezone_returns_unknown_without_beijing_fallback():
    result = study.classify_relative_weather_time(1_774_000_000, "2026-03-20", None)
    assert result["trade_time_market_local"] is None
    assert result["relative_weather_day"] == result["report_time_bucket"] == "UNKNOWN"


@pytest.mark.parametrize(
    ("local_iso", "relative", "bucket"),
    [
        ("2026-07-18T12:00:00+08:00", "D-2", "D-2"),
        ("2026-07-19T12:00:00+08:00", "D-1", "D-1"),
        ("2026-07-20T00:00:00+08:00", "D0", "D0_00_08"),
        ("2026-07-21T00:00:00+08:00", "POST_EVENT", "POST_EVENT"),
        ("2026-07-17T23:59:59+08:00", "EARLIER_THAN_D2", "EARLIER_THAN_D2"),
        ("2026-07-20T07:59:59+08:00", "D0", "D0_00_08"),
        ("2026-07-20T08:00:00+08:00", "D0", "D0_08_12"),
        ("2026-07-20T11:59:59+08:00", "D0", "D0_08_12"),
        ("2026-07-20T12:00:00+08:00", "D0", "D0_12_16"),
        ("2026-07-20T15:59:59+08:00", "D0", "D0_12_16"),
        ("2026-07-20T16:00:00+08:00", "D0", "D0_16_24"),
        ("2026-07-20T23:59:59+08:00", "D0", "D0_16_24"),
    ],
)
def test_relative_day_and_d0_boundaries(local_iso, relative, bucket):
    timestamp = int(datetime.fromisoformat(local_iso).timestamp())
    result = study.classify_relative_weather_time(timestamp, "2026-07-20", "Asia/Shanghai")
    assert (result["relative_weather_day"], result["report_time_bucket"]) == (relative, bucket)


@pytest.mark.parametrize("outcome", ["yes", "YES", "Yes", "no", "NO", "No"])
@pytest.mark.parametrize("side", ["buy", "BUY", "Buy", "sell", "SELL", "Sell"])
def test_outcome_and_side_case_normalization(outcome, side):
    row = raw_fill(outcome=outcome, side=side)
    fills, _, _ = study.normalize_fill_rows(HUSKY, [row], date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=[], timezone_registry={"beijing": "Asia/Shanghai"})
    assert fills[0]["outcome"] in {"YES", "NO"}
    assert fills[0]["side"] in {"BUY", "SELL"}


def test_all_four_side_outcome_identities_remain_separate():
    rows = []
    for index, (side, outcome) in enumerate((("BUY", "YES"), ("BUY", "NO"), ("SELL", "YES"), ("SELL", "NO"))):
        rows.append(raw_fill(side=side, outcome=outcome, tx=f"0x{index}", asset=f"a{index}"))
    fills, _, _ = study.normalize_fill_rows(HUSKY, rows, date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=[], timezone_registry={"beijing": "Asia/Shanghai"})
    assert {(row["side"], row["outcome"]) for row in fills} == {("BUY", "YES"), ("BUY", "NO"), ("SELL", "YES"), ("SELL", "NO")}


def test_no_complement_is_not_a_yes_price():
    row = normalized_fill(outcome="NO", price=0.2)
    assert row["price"] == 0.2
    assert row["implied_yes_equivalent_price"] == pytest.approx(0.8)
    assert study.price_band(row["price"]) == "PRICE_10_30C"


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        (0, "PRICE_0_10C"), (0.09999, "PRICE_0_10C"),
        (0.10, "PRICE_10_30C"), (0.29999, "PRICE_10_30C"),
        (0.30, "PRICE_30_70C"), (0.69999, "PRICE_30_70C"),
        (0.70, "PRICE_70_90C"), (0.89999, "PRICE_70_90C"),
        (0.90, "PRICE_90_100C"), (1.0, "PRICE_90_100C"),
    ],
)
def test_price_band_boundaries(price, expected):
    assert study.price_band(price) == expected


@pytest.mark.parametrize("price", [-0.0001, 1.0001, None, "nan"])
def test_price_outside_binary_range_rejected(price):
    with pytest.raises(ValueError):
        study.price_band(price)


@pytest.mark.parametrize(
    ("shares", "expected"),
    [(0, "SHARES_0_100"), (99.999, "SHARES_0_100"), (100, "SHARES_100_500"), (499.999, "SHARES_100_500"), (500, "SHARES_500_PLUS")],
)
def test_cumulative_shares_band_boundaries(shares, expected):
    assert study.cumulative_shares_band(shares) == expected


def test_same_price_cumulative_group_accumulates_and_crosses_band():
    a = normalized_fill(size=60, tx="0x1")
    b = normalized_fill(size=50, tx="0x2")
    groups = study.build_same_price_cumulative_groups([a, b])
    assert len(groups) == 1
    assert groups[0]["fill_count"] == 2
    assert groups[0]["cumulative_shares"] == 110
    assert groups[0]["shares_band"] == "SHARES_100_500"


@pytest.mark.parametrize("field,value", [
    ("wallet", "0x" + "1" * 40),
    ("event_key", "other-event"),
    ("asset", "other-asset"),
    ("temperature_bucket", "32°C"),
    ("outcome", "NO"),
    ("side", "SELL"),
    ("report_time_bucket", "D0_12_16"),
])
def test_same_price_group_key_separates_required_identity_fields(field, value):
    a = normalized_fill(tx="0x1")
    b = normalized_fill(tx="0x2")
    b[field] = value
    if field == "report_time_bucket":
        b["relative_weather_day"] = "D0"
    assert len(study.build_same_price_cumulative_groups([a, b])) == 2


def structure_rows(yes_names=(), no_names=(), sells=()):
    rows = []
    for index, name in enumerate(yes_names):
        rows.append(normalized_fill(title_bucket=name, bucket=name.lower().replace("°", ""), outcome="YES", asset=f"y{index}", tx=f"0xy{index}"))
    for index, name in enumerate(no_names):
        rows.append(normalized_fill(title_bucket=name, bucket=name.lower().replace("°", ""), outcome="NO", asset=f"n{index}", tx=f"0xn{index}"))
    rows.extend(sells)
    return rows


@pytest.mark.parametrize(
    ("yes_names", "no_names", "expected"),
    [
        (("30°C",), (), "SINGLE_YES_TEMPERATURE"),
        ((), ("30°C",), "SINGLE_NO_TEMPERATURE"),
        (("30°C", "31°C"), (), "MULTI_YES_ONLY"),
        ((), ("30°C", "31°C"), "MULTI_NO_ONLY"),
        (("30°C",), ("31°C",), "MIXED_YES_NO"),
    ],
)
def test_event_temperature_primary_structures(yes_names, no_names, expected):
    assert study.classify_event_temperature_structure(structure_rows(yes_names, no_names))["event_buy_structure"] == expected


def test_no_buy_and_sell_does_not_determine_buy_structure():
    sell = normalized_fill(side="SELL", outcome="YES")
    assert study.classify_event_temperature_structure([sell])["event_buy_structure"] == "NO_BUY"


@pytest.mark.parametrize(
    ("yes_names", "no_names", "expected"),
    [
        (("30°C",), ("30°C",), "SAME_BUCKET_BOTH_SIDES"),
        (("30°C",), ("31°C",), "CROSS_BUCKET_YES_NO"),
        (("30°C", "31°C"), ("30°C",), "BOTH"),
    ],
)
def test_mixed_yes_no_subtypes(yes_names, no_names, expected):
    assert study.classify_event_temperature_structure(structure_rows(yes_names, no_names))["mixed_yes_no_subtype"] == expected


def test_no_does_not_enter_yes_temperature_count():
    result = study.classify_event_temperature_structure(structure_rows(("30°C",), ("31°C", "32°C")))
    assert result["yes_temperature_bucket_count"] == 1
    assert result["no_temperature_bucket_count"] == 2


def test_exact_adjacent_and_tail_not_adjacent():
    adjacent = study.classify_event_temperature_structure(structure_rows(("30°C", "31°C"), ()))
    assert adjacent["adjacent_yes_pairs"]
    tail_rows = structure_rows(("30°C or below", "31°C"), ())
    tail = study.classify_event_temperature_structure(tail_rows)
    assert not tail["adjacent_yes_pairs"]
    assert tail["has_yes_tail_bucket"]


def test_unknown_timezone_fill_is_retained_and_reported():
    fills, _, q = study.normalize_fill_rows(HUSKY, [raw_fill(city="unknown-city")], date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=[], timezone_registry={})
    assert len(fills) == 1
    assert fills[0]["relative_weather_day"] == "UNKNOWN"
    assert q["unknown_relative_day_count"] == 1


def test_earlier_than_d2_excluded_from_core_distribution():
    early_ts = int(datetime.fromisoformat("2026-07-17T12:00:00+08:00").timestamp())
    early = normalized_fill(timestamp=early_ts)
    groups = study.build_same_price_cumulative_groups([early])
    assert study._aggregate_distribution([early], groups, ("relative_weather_day",)) == []


def test_offline_manifest_rejects_absolute_parent_and_sha_mismatch(tmp_path):
    manifest_path = write_evidence(tmp_path / "evidence", HUSKY, [raw_fill()])
    manifest = json.loads(manifest_path.read_text())
    manifest["aggregates"]["trades"]["relative_path"] = "/tmp/trades.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="PORTABLE"):
        study.load_saved_evidence(manifest_path, wallets=[HUSKY], date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date())
    manifest["aggregates"]["trades"]["relative_path"] = "../trades.json"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(RuntimeError, match="PORTABLE"):
        study.load_saved_evidence(manifest_path, wallets=[HUSKY], date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date())


def test_offline_mode_makes_zero_network_calls(monkeypatch):
    study.NETWORK_CALL_COUNT = 0
    monkeypatch.setenv(study.NO_NETWORK_ENV, "1")
    output = TMP / "offline_zero_network"
    study.analyze([HUSKY], "2026-03-21", "2026-07-23", ["beijing"], output, saved_public_evidence_manifest=PORTABLE)
    assert study.NETWORK_CALL_COUNT == 0
    assert json.loads((output / "run_manifest.json").read_text())["network_call_count"] == 0


def test_public_get_client_rejects_nonofficial_endpoint(tmp_path):
    with pytest.raises(ValueError, match="official Polymarket"):
        study.PublicGetClient(tmp_path).get_json("https://example.com/trades", {})


def test_husky_regression_counts_and_fill_set_match():
    output = TMP / "husky_regression"
    result = study.analyze([HUSKY], "2026-03-21", "2026-07-23", ["beijing"], output, saved_public_evidence_manifest=PORTABLE)
    summary = result["summaries"][0]
    assert {
        "events": summary["weather_event_count"],
        "fills": summary["total_public_fill_count"],
        "buy": summary["buy_fill_count"],
        "sell": summary["sell_fill_count"],
        "buy_yes": summary["buy_yes_fill_count"],
        "buy_no": summary["buy_no_fill_count"],
        "multi_yes": summary["multi_yes_event_count"],
        "adjacent_yes": summary["adjacent_yes_event_count"],
    } == {"events": 50, "fills": 537, "buy": 453, "sell": 84, "buy_yes": 400, "buy_no": 53, "multi_yes": 29, "adjacent_yes": 21}
    with REVIEWED_FILLS.open(encoding="utf-8", newline="") as handle:
        old = list(csv.DictReader(handle))
    with (output / HUSKY / "all_fills.csv").open(encoding="utf-8", newline="") as handle:
        new = list(csv.DictReader(handle))
    fields = ("transaction_hash", "condition_id", "asset", "side", "outcome", "price", "shares", "timestamp_epoch")
    def key(row):
        values = []
        for field in fields:
            value = row[field]
            if field in {"side", "outcome", "transaction_hash", "condition_id"}:
                value = value.upper() if field in {"side", "outcome"} else value.lower()
            values.append(value)
        return tuple(values)
    assert {key(row) for row in old} == {key(row) for row in new}


def test_output_tree_city_all_cities_quality_and_no_pnl_fields():
    output = TMP / "output_tree"
    result = study.analyze([HUSKY], "2026-03-21", "2026-07-23", [], output, saved_public_evidence_manifest=PORTABLE)
    wallet_root = output / HUSKY
    expected = {
        "summary.md", "summary.json", "all_fills.csv", "same_price_cumulative_groups.csv",
        "buy_yes_distribution.csv", "buy_no_distribution.csv", "sell_yes_distribution.csv",
        "sell_no_distribution.csv", "price_time_cumulative_shares_distribution.csv",
        "event_temperature_structure.csv", "city_summary.csv", "market_discovery.csv",
        "data_quality.csv", "source_manifest.json",
    }
    assert expected <= {path.name for path in wallet_root.iterdir()}
    assert {"trader_comparison.csv", "trader_comparison.md", "run_manifest.json"} <= {path.name for path in output.iterdir()}
    summary = result["summaries"][0]
    assert summary["all_cities_default"] is True
    assert not any("pnl" in key.lower() or "roi" in key.lower() for key in summary)
    assert summary["public_data_only"] and summary["public_get_only"]
    assert not summary["account_connection"] and not summary["signing"] and not summary["real_order"]
