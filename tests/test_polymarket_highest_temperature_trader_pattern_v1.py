from __future__ import annotations

import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock

import pytest

import src.polymarket_highest_temperature_trader_pattern_v1 as study


HUSKY = "0xaf17116ae2b1476032785a67bd5b7c8c05905c20"
PORTABLE = Path("docs/husky_beijing_full_trade_study_v1/saved_evidence_v1/manifest.json")
REVIEWED_FILLS = Path("docs/husky_beijing_full_trade_study_v1/beijing_all_public_fills.csv")
TMP = Path("/tmp/polymarket_highest_temperature_trader_pattern_v1/tests")
VERIFIED_POLYMARKET_CITIES = {
    "amsterdam", "ankara", "atlanta", "austin", "beijing", "buenos-aires",
    "busan", "cape-town", "chengdu", "chicago", "chongqing", "dallas",
    "denver", "dubai", "guangzhou", "helsinki", "hong-kong", "houston",
    "istanbul", "jakarta", "jeddah", "karachi", "kuala-lumpur", "lagos",
    "london", "los-angeles", "lucknow", "madrid", "manila", "mexico-city",
    "miami", "milan", "moscow", "munich", "nyc", "panama-city", "paris",
    "qingdao", "san-francisco", "sao-paulo", "seattle", "seoul", "shanghai",
    "shenzhen", "singapore", "taipei", "tel-aviv", "tokyo", "toronto",
    "warsaw", "wellington", "wuhan", "zhengzhou",
}


def summary_fixture(*, with_buy: bool = False, blocked: bool = False) -> dict:
    return {
        "wallet": HUSKY,
        "weather_date_from": "2026-03-21",
        "weather_date_to": "2026-07-23",
        "requested_cities": ["beijing"],
        "discovered_cities": ["beijing"],
        "weather_event_count": 22,
        "total_public_fill_count": 69,
        "buy_fill_count": 2 if with_buy else 0,
        "sell_fill_count": 69,
        "buy_yes_fill_count": 1 if with_buy else 0,
        "buy_no_fill_count": 1 if with_buy else 0,
        "sell_yes_fill_count": 53,
        "sell_no_fill_count": 16,
        "buy_yes_shares": 10 if with_buy else 0,
        "buy_yes_trade_usd": 2 if with_buy else 0,
        "buy_no_shares": 20 if with_buy else 0,
        "buy_no_trade_usd": 4 if with_buy else 0,
        "sell_yes_shares": 9841.0373,
        "sell_yes_trade_usd": 8819.29,
        "sell_no_shares": 1281.13,
        "sell_no_trade_usd": 1279.27,
        "main_relative_weather_day_by_usd": "D0",
        "main_d0_bucket_by_usd": "D0_16_24",
        "buy_yes_main_price_band_by_usd": "PRICE_30_70C" if with_buy else "UNKNOWN",
        "buy_no_main_price_band_by_usd": "PRICE_10_30C" if with_buy else "UNKNOWN",
        "sell_yes_main_price_band_by_usd": "PRICE_90_100C",
        "sell_no_main_price_band_by_usd": "PRICE_90_100C",
        "main_cumulative_shares_band_by_usd": "SHARES_100_500",
        "single_yes_temperature_event_count": 1 if with_buy else 0,
        "single_no_temperature_event_count": 0,
        "multi_yes_event_count": 1 if with_buy else 0,
        "multi_no_only_event_count": 0,
        "mixed_yes_no_event_count": 1 if with_buy else 0,
        "collection_start_utc": "2026-03-18T00:00:00+00:00",
        "collection_end_utc": "2026-07-26T23:59:59+00:00",
        "pattern_report_status": "BLOCKED_INCOMPLETE_EVIDENCE" if blocked else "READY",
        "pattern_report_block_reason": "TARGET_MARKET_EVIDENCE_INCOMPLETE" if blocked else "",
        "data_quality": {
            "pagination_saturation_status": "PAGINATION_INCOMPLETE",
            "api_request_failure_count": 0,
            "unknown_timezone_fill_count": 0,
            "unknown_relative_day_count": 0,
            "market_identity_conflict_count": 0,
            "unknown_side_count": 0,
            "unknown_outcome_count": 0,
            "trade_usd_missing_count": 69,
            "unparseable_market_count": 0,
            "target_event_count": 125,
            "target_condition_count": 1375,
            "activity_only_fill_count": 0,
            "trades_only_fill_count": 0,
            "orphan_sell_asset_count": 0,
            "targeted_market_fetch_saturated": blocked,
            "pattern_report_status": "BLOCKED_INCOMPLETE_EVIDENCE" if blocked else "READY",
            "pattern_report_block_reason": "TARGET_MARKET_EVIDENCE_INCOMPLETE" if blocked else "",
        },
    }


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


class RoutingClient:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []
        self.requests = []

    def get_json(self, url, params):
        self.calls.append((url, dict(params)))
        return self.responder(url, dict(params))


def gamma_event(*, city="beijing", weather_date="2026-07-20", conditions=("condition-1",)):
    day = date.fromisoformat(weather_date)
    event_slug = f"highest-temperature-in-{city}-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"
    markets = []
    for index, condition_id in enumerate(conditions):
        temperature = 30 + index
        markets.append({
            "id": f"market-{index}",
            "conditionId": condition_id,
            "slug": f"{event_slug}-{temperature}c",
            "question": f"Will the highest temperature in Beijing be {temperature}°C on July 20?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": json.dumps([f"yes-{condition_id}", f"no-{condition_id}"]),
            "active": True,
            "closed": False,
        })
    return {
        "id": f"event-{city}-{weather_date}",
        "slug": event_slug,
        "title": f"Highest temperature in Beijing on July 20?",
        "endDate": "2026-07-21T12:00:00Z",
        "markets": markets,
    }


def target_market_rows(conditions=("condition-1",)):
    client = RoutingClient(lambda url, params: [gamma_event(conditions=conditions)] if params.get("offset") == 0 else [])
    rows, status = study.discover_target_markets(
        client, date(2026, 7, 20), date(2026, 7, 20), ["beijing"],
    )
    assert status == "COMPLETE"
    return rows


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


@pytest.mark.parametrize("city", ["beijing", "shanghai", "new-york", "cape-town", "kuala-lumpur"])
def test_event_slug_city_and_date_parsing(city):
    row = raw_fill(city=city)
    parsed = study.parse_highest_temperature_market(row)
    assert parsed["canonical_city"] == city
    assert parsed["weather_date_local"] == "2026-07-20"


def test_yearless_legacy_event_slug_uses_nearest_trade_year():
    row = raw_fill(city="dubai", title_bucket="99-100°F")
    row["eventSlug"] = "highest-temperature-in-dubai-on-may-31"
    row["slug"] = "will-the-highest-temperature-in-dubai-be-between-99-100f-on-may-31"
    row["title"] = "Will the highest temperature in Dubai be between 99-100°F on May 31?"
    row["timestamp"] = int(datetime(2025, 5, 29, 18, tzinfo=timezone.utc).timestamp())
    parsed = study.parse_highest_temperature_market(row)
    assert parsed["weather_date_local"] == "2025-05-31"
    assert parsed["temperature_bucket"] == "99-100°F"
    assert parsed["bucket_kind"] == "range"
    assert (parsed["bucket_low"], parsed["bucket_high"]) == (99, 100)


def test_yearless_january_slug_chooses_next_year_near_year_boundary():
    row = raw_fill(city="london")
    row["eventSlug"] = "highest-temperature-in-london-on-january-1"
    row["slug"] = "highest-temperature-in-london-on-january-1-10c"
    row["title"] = "Will the highest temperature in London be 10°C on January 1?"
    row["timestamp"] = int(datetime(2025, 12, 30, tzinfo=timezone.utc).timestamp())
    assert study.parse_highest_temperature_market(row)["weather_date_local"] == "2026-01-01"


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


def test_parenthetical_station_name_does_not_conflict_with_canonical_slug_city():
    row = raw_fill(city="seoul")
    row["title"] = "Will the highest temperature in Seoul (Incheon) be 31°C on July 20?"
    parsed = study.parse_highest_temperature_market(row)
    assert parsed["canonical_city"] == "seoul"
    assert parsed["market_identity_status"] == "OBSERVED"


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
    only_beijing, beijing_discovery, _ = study.normalize_fill_rows(HUSKY, rows, date_from=datetime(2026, 7, 20).date(), date_to=datetime(2026, 7, 20).date(), cities=["beijing"], timezone_registry={"beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai"})
    assert {row["canonical_city"] for row in all_fills} == {"beijing", "shanghai"}
    assert {row["canonical_city"] for row in only_beijing} == {"beijing"}
    assert {row["canonical_city"] for row in beijing_discovery} == {"beijing"}


def test_all_cities_default_has_registered_local_time_across_regions():
    cities = ("seoul", "london", "nyc", "cape-town", "jakarta")
    rows = [
        raw_fill(city=city, tx=f"0x{index}", asset=f"asset-{index}")
        for index, city in enumerate(cities)
    ]
    fills, _, quality = study.normalize_fill_rows(
        HUSKY,
        rows,
        date_from=datetime(2026, 7, 20).date(),
        date_to=datetime(2026, 7, 20).date(),
        cities=[],
        timezone_registry=study.load_timezone_registry(),
    )
    assert {row["canonical_city"] for row in fills} == set(cities)
    assert all(row["market_timezone"] and row["trade_time_market_local"] for row in fills)
    assert quality["unknown_relative_day_count"] == 0


def test_timezone_registry_covers_verified_polymarket_highest_temperature_cities():
    registry = study.load_timezone_registry()
    assert VERIFIED_POLYMARKET_CITIES <= set(registry)
    assert all(study.ZoneInfo(registry[city]) for city in VERIFIED_POLYMARKET_CITIES)


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


def test_bundled_husky_manifest_explains_how_to_run_a_new_wallet():
    new_wallet = "0x4ce3f17be91c3d0d6dbfed7bd4d326957dec4291"
    with pytest.raises(RuntimeError, match="use --refresh-public-data for a new wallet"):
        study.load_saved_evidence(
            PORTABLE,
            wallets=[new_wallet],
            date_from=datetime(2026, 3, 21).date(),
            date_to=datetime(2026, 7, 23).date(),
        )


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


def test_analyze_resets_network_call_count_between_runs(monkeypatch, tmp_path):
    row = raw_fill(weather_date="2026-07-20")
    activity_row = {**row, "type": "TRADE", "usdcSize": 2}

    def fake_refresh(wallet, date_from, date_to, root, cities=()):
        with study.NETWORK_CALL_COUNT_LOCK:
            study.NETWORK_CALL_COUNT += 3
        manifest = {
            "schema_version": study.EVIDENCE_SCHEMA,
            "wallet": wallet,
            "weather_date_from": date_from.isoformat(),
            "weather_date_to": date_to.isoformat(),
            "collection_start_utc": "2026-07-17T00:00:00+00:00",
            "collection_end_utc": "2026-07-23T23:59:59+00:00",
            "public_data_only": True,
            "public_get_only": True,
            "account_connection": False,
            "signing": False,
            "real_order": False,
            "pagination_saturation_status": "COMPLETE",
            "requests": [{"success": True}] * 3,
        }
        root.mkdir(parents=True, exist_ok=True)
        study.write_json(root / "manifest.json", manifest)
        return manifest, {"activity": [activity_row], "trades": [row]}

    monkeypatch.setattr(study, "refresh_wallet_evidence", fake_refresh)
    study.NETWORK_CALL_COUNT = 999
    first = study.analyze(
        [HUSKY], "2026-07-20", "2026-07-20", ["beijing"],
        tmp_path / "first", refresh_public_data=True,
    )
    study.NETWORK_CALL_COUNT = 888
    second = study.analyze(
        [HUSKY], "2026-07-20", "2026-07-20", ["beijing"],
        tmp_path / "second", refresh_public_data=True,
    )
    assert first["run_manifest"]["network_call_count"] == 3
    assert second["run_manifest"]["network_call_count"] == 3


def test_concurrent_public_requests_count_each_actual_attempt(monkeypatch, tmp_path):
    attempts = {"count": 0}
    attempts_lock = Lock()

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(request, timeout):
        with attempts_lock:
            attempts["count"] += 1
        return FakeResponse()

    monkeypatch.setattr(study.urllib.request, "urlopen", fake_urlopen)
    study.NETWORK_CALL_COUNT = 0
    client = study.PublicGetClient(tmp_path, attempts=1)
    request_count = 64
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(
            lambda _: client.get_json(f"{study.DATA_API}/trades", {"limit": 1}),
            range(request_count),
        ))
    assert attempts["count"] == request_count
    assert study.NETWORK_CALL_COUNT == attempts["count"]
    assert len(client.requests) == request_count


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


def test_summary_is_readable_chinese_for_complete_no_buy_fixture():
    summary = summary_fixture()
    summary["data_quality"]["pagination_saturation_status"] = "COMPLETE"
    text = study.render_summary(summary)
    assert text.startswith("# Polymarket最高温市场交易模式报告")
    for heading in (
        "## 一、先说结论", "## 二、本次研究范围", "## 三、先看数据完整性",
        "## 四、成交概览", "## 五、买入方式", "## 六、卖出方式",
        "## 七、交易时间集中在哪里", "## 八、主要在什么价格成交",
        "## 九、同一价格累计成交规模", "## 十、他通常买几个温度",
        "## 十一、目前可以确认的交易特点", "## 十二、目前不能下的结论",
    ):
        assert heading in text
    assert "没有观察到买入成交" in text
    assert "不能判断" in text and "建仓" in text
    assert "该交易员没有买入" not in text
    assert "天气当天" in text
    assert "当天16:00—24:00" in text
    assert "90—100美分" in text
    assert "累计100—500份" in text
    assert "当前公开接口抓取未发现分页截断" in text
    assert "卖出成交额不是利润" in text
    assert "PnL" in text and "ROI" in text
    assert "OBSERVED" not in text and "INFERRED" not in text and "UNKNOWN" not in text
    assert not any(f"\n{number}." in text for number in range(1, 24))


def test_summary_chinese_rendering_keeps_buy_and_temperature_sections():
    summary = summary_fixture(with_buy=True)
    summary["data_quality"]["pagination_saturation_status"] = "COMPLETE"
    text = study.render_summary(summary)
    assert "当前观察到2笔买入成交" in text
    assert "买入YES主要在30—70美分" in text
    assert "买入NO主要在10—30美分" in text
    assert "多YES事件1个" in text
    assert "YES/NO混合事件1个" in text
    assert study._zh_price("UNKNOWN") == "当前没有足够成交可判断"
    assert study._zh_shares("SHARES_100_500") == "同一价格累计100—500份"


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


def test_global_trades_high_volume_marks_saturation():
    def responder(url, params):
        if params["side"] == "BUY":
            return [raw_fill(tx=f"0x{params['offset']}-{index}", asset=f"b-{params['offset']}-{index}") for index in range(2)]
        return []
    rows, saturated = study.fetch_trades_by_side(
        RoutingClient(responder), HUSKY, limit=2, offset_cap=2,
    )
    assert saturated is True
    assert len(rows) == 4


def test_target_market_query_recovers_buy_outside_global_slice():
    buy = raw_fill(tx="0xtarget", asset="yes-condition-1")
    buy["conditionId"] = "condition-1"
    def responder(url, params):
        if params.get("market") == "condition-1":
            return [buy]
        return []
    result = study.fetch_target_trades(RoutingClient(responder), HUSKY, "condition-1")
    assert result.status == "COMPLETE"
    assert [row["side"] for row in result.rows] == ["BUY"]


def test_activity_wide_empty_but_single_market_returns_buy():
    buy = {**raw_fill(), "type": "TRADE"}
    client = RoutingClient(lambda url, params: [buy] if params.get("market") else [])
    assert client.get_json(f"{study.DATA_API}/activity", {"user": HUSKY}) == []
    result = study.fetch_target_activity(client, HUSKY, buy["conditionId"], 1, 2)
    assert result.status == "COMPLETE" and len(result.rows) == 1
    assert result.rows[0]["side"] == "BUY"


def test_target_activity_supports_explicit_buy_and_sell_queries():
    client = RoutingClient(lambda url, params: [] if params.get("side") == "SELL" else [raw_fill()])
    buy = study._target_activity_page(client, HUSKY, "condition", study.Window(1, 1), side="BUY")
    sell = study._target_activity_page(client, HUSKY, "condition", study.Window(1, 1), side="SELL")
    assert len(buy.rows) == 1 and sell.rows == []
    assert {params.get("side") for _, params in client.calls} == {"BUY", "SELL"}


def test_target_markets_are_discovered_independently_of_wallet_fills():
    event = gamma_event(conditions=("condition-a", "condition-b"))
    client = RoutingClient(lambda url, params: [event])
    rows, status = study.discover_target_markets(
        client, date(2026, 7, 20), date(2026, 7, 20), ["beijing"],
    )
    assert status == "COMPLETE"
    assert {row["condition_id"] for row in rows} == {"condition-a", "condition-b"}
    assert len(rows) == 4


def test_target_market_discovery_filters_city_and_weather_date():
    events = [
        gamma_event(city="beijing", weather_date="2026-07-20"),
        gamma_event(city="shanghai", weather_date="2026-07-20", conditions=("shanghai",)),
        gamma_event(city="beijing", weather_date="2026-07-21", conditions=("tomorrow",)),
    ]
    client = RoutingClient(lambda url, params: events)
    rows, _ = study.discover_target_markets(
        client, date(2026, 7, 20), date(2026, 7, 20), ["beijing"],
    )
    assert {row["condition_id"] for row in rows} == {"condition-1"}


def test_multiple_condition_queries_are_not_mixed():
    targets = target_market_rows(("condition-a", "condition-b"))
    def responder(url, params):
        condition = params["market"]
        row = raw_fill(tx=f"0x{condition}", asset=f"yes-{condition}")
        row["conditionId"] = condition
        return [{**row, "type": "TRADE", "usdcSize": 2}] if url.endswith("/activity") else [row]
    payloads, audit = study.collect_target_market_fills(
        RoutingClient(responder), HUSKY, targets, 1, 2, max_workers=1,
    )
    assert {row["condition_id"] for row in audit} == {"condition-a", "condition-b"}
    assert all(row["union_fill_count"] == 1 for row in audit)
    assert {row["conditionId"] for row in payloads["reconciled_fills"]} == {"condition-a", "condition-b"}


def test_reconciliation_keeps_activity_only_fill():
    row = {**raw_fill(), "type": "TRADE"}
    union, metrics = study.reconcile_fill_sources(HUSKY, [row], [])
    assert len(union) == metrics["activity_only_count"] == 1
    assert union[0]["_source_types"] == "source_activity"


def test_reconciliation_keeps_trades_only_fill():
    row = raw_fill()
    union, metrics = study.reconcile_fill_sources(HUSKY, [], [row])
    assert len(union) == metrics["trades_only_count"] == 1
    assert union[0]["_source_types"] == "source_trades"


def test_reconciliation_same_fill_is_not_double_counted():
    trade = raw_fill(size=100)
    activity = {**trade, "type": "TRADE", "usdcSize": 20}
    union, metrics = study.reconcile_fill_sources(HUSKY, [activity], [trade])
    assert len(union) == metrics["intersection_fill_count"] == 1
    assert union[0]["_source_types"] == "source_both"


def test_reconciliation_tolerates_tiny_size_rounding_difference():
    trade = raw_fill(size=100)
    activity = {**trade, "size": 100.0000005, "type": "TRADE", "usdcSize": 20}
    union, metrics = study.reconcile_fill_sources(HUSKY, [activity], [trade])
    assert len(union) == 1
    assert metrics["intersection_fill_count"] == 1


def test_reconciliation_does_not_collapse_distinct_same_transaction_fills():
    first = raw_fill(size=10)
    second = raw_fill(size=20)
    union, metrics = study.reconcile_fill_sources(HUSKY, [{**first, "type": "TRADE"}], [first, second])
    assert len(union) == 2
    assert metrics["intersection_fill_count"] == 1
    assert metrics["trades_only_count"] == 1


def test_pagination_incomplete_blocks_pattern_summary():
    text = study.render_summary(summary_fixture(blocked=True))
    assert "本次数据不完整，交易模式分析暂停" in text
    assert "交易时间集中在哪里" not in text
    assert "主要在什么价格成交" not in text
    assert "他通常买几个温度" not in text


def test_blocked_zero_buy_does_not_claim_wallet_has_no_buy():
    text = study.render_summary(summary_fixture(blocked=True))
    assert "该交易员没有买入" not in text
    assert "没有观察到的成交不能解释为没有发生" in text


def test_complete_targeted_zero_buy_uses_direct_buy_caveat():
    summary = summary_fixture()
    summary["data_quality"]["pagination_saturation_status"] = "COMPLETE"
    text = study.render_summary(summary)
    assert "当前完整公开证据未观察到直接BUY" in text
    assert "拆分、转换或转入" in text


def test_api_request_failure_blocks_quality_status():
    row = normalized_fill()
    manifest = {
        "schema_version": study.EVIDENCE_SCHEMA,
        "pagination_saturation_status": "COMPLETE",
        "requests": [{"success": False}],
    }
    payloads = {"activity": [], "trades": [raw_fill()], "non_trade_activity": []}
    quality = study._quality_payload(1, 1, [row], 0, 1, [], Counter(), manifest, payloads)
    assert quality["pattern_report_status"] == "BLOCKED_INCOMPLETE_EVIDENCE"
    assert "API_REQUEST_FAILED" in quality["pattern_report_block_reason"]


def test_orphan_sell_without_acquisition_is_detected():
    sell = normalized_fill(side="SELL", size=12)
    count, shares, activities = study._orphan_sell_metrics([sell], [])
    assert (count, shares, activities) == (1, 12, 0)


@pytest.mark.parametrize("activity_type", ["SPLIT", "CONVERSION", "MERGE", "TRANSFER"])
def test_non_buy_acquisition_explains_orphan_without_entering_buy_stats(activity_type):
    sell = normalized_fill(side="SELL", size=12)
    acquisition = {"type": activity_type, "asset": sell["asset"]}
    count, shares, activities = study._orphan_sell_metrics([sell], [acquisition])
    assert (count, shares, activities) == (0, 0, 1)
    assert sell["side"] == "SELL"


def test_current_wallet_forensic_fixture_reconciles_buy_and_sell():
    wallet = "0x8fbd7cf5f806f563080864694415829f7229a959"
    activity = []
    trades = []
    for index in range(38):
        row = raw_fill(wallet=wallet, side="BUY", tx=f"0xb{index}", asset=f"a{index}")
        activity.append({**row, "type": "TRADE"})
        trades.append(dict(row))
    for index in range(3):
        row = raw_fill(wallet=wallet, side="SELL", tx=f"0xs{index}", asset=f"s{index}")
        activity.append({**row, "type": "TRADE"})
        trades.append(dict(row))
    union, metrics = study.reconcile_fill_sources(wallet, activity, trades)
    assert Counter(row["side"] for row in union) == {"BUY": 38, "SELL": 3}
    assert metrics["union_fill_count"] == metrics["intersection_fill_count"] == 41


def test_target_market_request_failure_is_fail_closed():
    targets = target_market_rows()
    class FailedClient(RoutingClient):
        def get_json(self, url, params):
            raise RuntimeError("request failed")
    _, audit = study.collect_target_market_fills(
        FailedClient(lambda *_: []), HUSKY, targets, 1, 2, max_workers=1,
    )
    assert audit[0]["completeness_status"] == "REQUEST_FAILED"


def test_target_market_source_conflict_is_not_silently_accepted():
    targets = target_market_rows()
    def responder(url, params):
        return [{**raw_fill(), "conditionId": "condition-1", "type": "TRADE"}] if url.endswith("/activity") else []
    _, audit = study.collect_target_market_fills(
        RoutingClient(responder), HUSKY, targets, 1, 2, max_workers=1,
    )
    assert audit[0]["completeness_status"] == "SOURCE_CONFLICT"


def test_activity_only_sub_cent_dust_fill_is_explained_and_retained():
    targets = target_market_rows()
    dust = {
        **raw_fill(size=0.004817, price=0.1700228358),
        "conditionId": "condition-1",
        "type": "TRADE",
        "usdcSize": 0.000819,
    }
    client = RoutingClient(lambda url, params: [dust] if url.endswith("/activity") else [])
    payloads, audit = study.collect_target_market_fills(
        client, HUSKY, targets, 1, 2, max_workers=1,
    )
    assert audit[0]["completeness_status"] == "COMPLETE"
    assert audit[0]["source_difference_explanation"] == "ACTIVITY_DUST_FILL_BELOW_TRADES_VISIBILITY"
    assert payloads["reconciled_fills"][0]["_source_types"] == "source_activity"


def test_event_completeness_rolls_up_all_target_conditions():
    rows = [
        {
            "canonical_city": "beijing", "weather_date_local": "2026-07-20",
            "event_id": "event-1", "event_slug": "event-slug",
            "condition_id": "condition-a", "completeness_status": "COMPLETE",
        },
        {
            "canonical_city": "beijing", "weather_date_local": "2026-07-20",
            "event_id": "event-1", "event_slug": "event-slug",
            "condition_id": "condition-b", "completeness_status": "REQUEST_FAILED",
        },
    ]
    audit = study.build_target_event_audit(rows)
    assert audit == [{
        "canonical_city": "beijing", "weather_date_local": "2026-07-20",
        "event_id": "event-1", "event_slug": "event-slug",
        "condition_count": 2, "complete_condition_count": 1,
        "partial_condition_count": 1, "unknown_condition_count": 0,
        "completeness_status": "REQUEST_FAILED",
    }]
