from datetime import datetime, timezone

from src.analyze_weather_strategy import parse_weather_title, parse_bucket
from src.analyze_weather_strategy_v2 import lead_bin, local_day_end_epoch

def test_exact_bucket():
    assert parse_bucket("30°C")["bucket_low"] == 30

def test_below_bucket():
    x = parse_bucket("18°C or below")
    assert x["bucket_kind"] == "below" and x["bucket_high"] == 18

def test_title():
    x = parse_weather_title(
        "Will the highest temperature in Beijing be 30°C on July 20?",
        "2026-07-20T23:59:00Z",
    )
    assert x["city"] == "Beijing"
    assert x["weather_metric"] == "high"
    assert x["weather_date"] == "2026-07-20"


def test_v2_local_day_end_uses_city_timezone():
    beijing_end = datetime.fromtimestamp(
        local_day_end_epoch("Beijing", "2026-07-20"),
        timezone.utc,
    )
    new_york_end = datetime.fromtimestamp(
        local_day_end_epoch("New York City", "2026-07-20"),
        timezone.utc,
    )

    assert beijing_end.isoformat() == "2026-07-20T16:00:00+00:00"
    assert new_york_end.isoformat() == "2026-07-21T04:00:00+00:00"


def test_v2_entry_lead_bin_boundaries():
    assert lead_bin(5.999) == "0-6h"
    assert lead_bin(6) == "6-12h"
    assert lead_bin(47.999) == "24-48h"
    assert lead_bin(48) == "48-72h"
    assert lead_bin(72) == "72h+"
