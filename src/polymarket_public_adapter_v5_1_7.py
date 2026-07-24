#!/usr/bin/env python3
"""Public read-only Polymarket adapter for v5.1.7-RC6.

The adapter intentionally exposes only public GET methods and deterministic
normalizers/calculators. It contains no account, credential, or real-trade
execution capability.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, getcontext
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable


getcontext().prec = 28

ADAPTER_NAME = "PolymarketPublicAdapterV5_1_7"
ADAPTER_VERSION = "polymarket_public_adapter_v5.1.7-rc6"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "huskyvs-v5.1.7-rc6-readonly/1.0"
ZERO = Decimal("0")
ONE = Decimal("1")
FEE_QUANT = Decimal("0.00001")
NORMALIZED_BOOK_ALGORITHM_VERSION = "orderbook_normalize_v5_1_7_rc6"
FILL_ALGORITHM_VERSION = "depth_replay_v5_1_7_rc6"
WEATHER_KEYWORDS = ("temperature", "weather", "high temp", "low temp", "highest temperature", "lowest temperature")
MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def dec(value: Any, default: Decimal | None = None) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value in ("", None):
        if default is not None:
            return default
        raise ValueError("missing decimal")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        if default is not None:
            return default
        raise ValueError(f"invalid decimal: {value}") from exc


def dstr(value: Any) -> str:
    x = dec(value)
    if x == x.to_integral():
        return str(x.quantize(Decimal("1")))
    return format(x.normalize(), "f")


def norm_text(value: Any) -> str:
    return " ".join(str(value or "").replace("°", "").replace("-", " ").strip().split())


def norm_cmp(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def canonical_temp_decimal(value: Any) -> str:
    return dstr(dec(value))


def json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return dstr(value)
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def stable_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256(stable_json(value).encode("utf-8")).hexdigest()


class AdapterError(RuntimeError):
    def __init__(self, category: str, message: str, status_code: int | None = None, endpoint: str | None = None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.endpoint = endpoint


@dataclass
class HttpResult:
    method: str
    url: str
    status_code: int
    latency_ms: Decimal
    started_at_utc: str
    received_at_utc: str
    payload: Any
    raw_text: str


class PublicAdapter:
    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        timeout_seconds: Decimal | float | int = Decimal("10"),
        max_retries: int = 2,
        backoff_seconds: Decimal | float | int = Decimal("0.5"),
        transport: Callable[[str, str, float], tuple[int, str]] | None = None,
    ):
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.backoff_seconds = float(backoff_seconds)
        self.transport = transport
        self.audit_events: list[dict[str, Any]] = []
        self.visited_endpoints: list[dict[str, Any]] = []

    def _url(self, base: str, path: str, params: dict[str, Any] | None = None) -> str:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        return base + path + (("?" + query) if query else "")

    def get_json(self, base: str, path: str, params: dict[str, Any] | None = None) -> HttpResult:
        url = self._url(base, path, params)
        last_error: AdapterError | None = None
        for attempt in range(self.max_retries + 1):
            started = utcnow()
            t0 = time.monotonic()
            try:
                if self.transport:
                    status, raw = self.transport(url, "GET", self.timeout_seconds)
                else:
                    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, method="GET")
                    with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                        status = int(resp.status)
                        raw = resp.read().decode("utf-8")
                latency = Decimal(str((time.monotonic() - t0) * 1000))
                received = utcnow()
                if status == 429:
                    raise AdapterError("rate_limited", "HTTP 429 rate limited", status, url)
                if status >= 500:
                    raise AdapterError("server_error", f"HTTP {status} server error", status, url)
                if status >= 400:
                    raise AdapterError("http_error", f"HTTP {status} error", status, url)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AdapterError("json_error", f"non-json response: {exc}", status, url) from exc
                result = HttpResult("GET", url, status, latency, iso(started), iso(received), payload, raw)
                self.visited_endpoints.append({"method": "GET", "url": url, "status_code": status, "latency_ms": dstr(latency)})
                return result
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
                category = "rate_limited" if exc.code == 429 else ("server_error" if exc.code >= 500 else "http_error")
                last_error = AdapterError(category, raw or str(exc), exc.code, url)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = AdapterError("network_error", str(exc), None, url)
            except AdapterError as exc:
                last_error = exc
            self.audit_events.append({"created_at_utc": iso(), "event_type": "request_error", "category": last_error.category if last_error else "unknown", "url": url, "attempt": attempt})
            if attempt < self.max_retries:
                time.sleep(self.backoff_seconds * (2**attempt))
        assert last_error is not None
        raise last_error

    def search(self, query: str, limit_per_type: int = 10, page: int = 1, events_status: str = "active", keep_closed_markets: int = 0) -> HttpResult:
        return self.get_json(
            self.gamma_base,
            "/public-search",
            {"q": query, "events_status": events_status, "limit_per_type": limit_per_type, "page": page, "keep_closed_markets": keep_closed_markets},
        )

    def list_events(self, active: bool = True, closed: bool = False, limit: int = 100, offset: int = 0) -> HttpResult:
        return self.get_json(self.gamma_base, "/events", {"active": str(active).lower(), "closed": str(closed).lower(), "limit": limit, "offset": offset})

    def list_markets(self, active: bool = True, closed: bool = False, limit: int = 100, offset: int = 0) -> HttpResult:
        return self.get_json(self.gamma_base, "/markets", {"active": str(active).lower(), "closed": str(closed).lower(), "limit": limit, "offset": offset})

    def market_by_slug(self, slug: str) -> HttpResult:
        return self.get_json(self.gamma_base, "/markets/slug/" + urllib.parse.quote(slug, safe=""))

    def market_by_token(self, token_id: str) -> HttpResult:
        return self.get_json(self.clob_base, "/markets-by-token/" + urllib.parse.quote(token_id, safe=""))

    def clob_market_info(self, condition_id: str) -> HttpResult:
        return self.get_json(self.clob_base, "/clob-markets/" + urllib.parse.quote(condition_id, safe=""))

    def clob_public_market(self, condition_id: str) -> HttpResult:
        return self.get_json(self.clob_base, "/markets/" + urllib.parse.quote(condition_id, safe=""))

    def orderbook(self, token_id: str) -> HttpResult:
        return self.get_json(self.clob_base, "/book", {"token_id": token_id})

    def server_time(self) -> HttpResult:
        return self.get_json(self.clob_base, "/time")


def parse_jsonish(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            payload = json.loads(value)
            return payload if isinstance(payload, list) else []
        except json.JSONDecodeError:
            return [x.strip() for x in value.split(",") if x.strip()]
    return []


def normalize_outcome(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def gamma_token_pairs(market: dict[str, Any]) -> list[dict[str, str]]:
    outcomes = [str(x) for x in parse_jsonish(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_jsonish(market.get("clobTokenIds"))]
    return [{"outcome": outcome, "token_id": token_ids[i] if i < len(token_ids) else ""} for i, outcome in enumerate(outcomes)]


def clob_token_pairs(clob_info: dict[str, Any]) -> list[dict[str, str]]:
    if isinstance(clob_info.get("tokens"), list):
        pairs = []
        for item in clob_info["tokens"]:
            token_id = str(item.get("token_id") or item.get("t") or "")
            if token_id:
                pairs.append(
                    {
                        "outcome": str(item.get("outcome") or item.get("o") or ""),
                        "token_id": token_id,
                        "winner": item.get("winner"),
                        "price": item.get("price"),
                    }
                )
        return pairs
    if isinstance(clob_info.get("t"), list):
        return [{"outcome": str(x.get("o", "")), "token_id": str(x.get("t", ""))} for x in clob_info["t"] if x.get("t")]
    return []


def condition_id_from_gamma(market: dict[str, Any]) -> str:
    return str(market.get("conditionId") or market.get("condition_id") or "")


def condition_id_from_clob(clob_info: dict[str, Any]) -> str:
    return str(clob_info.get("condition_id") or clob_info.get("conditionId") or clob_info.get("id") or "")


def explicit_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return None
    raw = str(value).strip().lower()
    if raw in {"true", "1", "yes", "y"}:
        return True
    if raw in {"false", "0", "no", "n"}:
        return False
    return None


def market_accepting_orders_status(market: dict[str, Any]) -> dict[str, Any]:
    observed: dict[str, bool] = {}
    for key in ("acceptingOrders", "accepting_orders", "enableOrderBook"):
        if key in market:
            parsed = explicit_bool(market.get(key))
            if parsed is not None:
                observed[key] = parsed
    if not observed:
        return {"accepting_orders": False, "accepting_orders_status": "unknown", "accepting_orders_fields": {}}
    values = set(observed.values())
    if len(values) > 1:
        return {"accepting_orders": False, "accepting_orders_status": "conflict", "accepting_orders_fields": observed}
    value = next(iter(values))
    return {"accepting_orders": value, "accepting_orders_status": "true" if value else "false", "accepting_orders_fields": observed}


def market_accepting_orders(market: dict[str, Any]) -> bool:
    return bool(market_accepting_orders_status(market)["accepting_orders"])


def market_state(market: dict[str, Any], clob_info: dict[str, Any] | None = None) -> dict[str, Any]:
    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    raw_status = str(market.get("umaResolutionStatus") or market.get("resolutionStatus") or market.get("umaResolutionStatuses") or "")
    raw_lower = raw_status.lower()
    resolved = bool(market.get("resolved") or market.get("automaticallyResolved") or raw_lower in {"resolved", "settled"})
    disputed = "dispute" in raw_lower or bool(market.get("disputed"))
    accepting_info = market_accepting_orders_status(market)
    accepting = bool(accepting_info["accepting_orders"])
    status_conflicts: list[str] = []
    if clob_info:
        clob_accepting = market_accepting_orders_status(clob_info)
        if accepting_info["accepting_orders_status"] not in {"unknown"} and clob_accepting["accepting_orders_status"] not in {"unknown"} and accepting_info["accepting_orders"] != clob_accepting["accepting_orders"]:
            status_conflicts.append("accepting_orders")
        for key in ("active", "closed", "resolved"):
            gamma_value = explicit_bool(market.get(key))
            clob_value = explicit_bool(clob_info.get(key))
            if gamma_value is not None and clob_value is not None and gamma_value != clob_value:
                status_conflicts.append(key)
    if status_conflicts or accepting_info["accepting_orders_status"] == "conflict":
        status = "status_conflict"
    elif accepting_info["accepting_orders_status"] == "unknown" and active and not closed and not resolved:
        status = "active_accepting_orders_unknown"
    elif accepting_info["accepting_orders_status"] == "false" and active and not closed and not resolved:
        status = "active_not_accepting_orders"
    elif disputed:
        status = "disputed"
    elif resolved:
        status = "resolved"
    elif closed:
        status = "resolution_pending"
    elif active and accepting:
        status = "active_trading"
    else:
        status = "unknown"
    return {
        "active": active,
        "closed": closed,
        "accepting_orders": accepting,
        "accepting_orders_status": accepting_info["accepting_orders_status"],
        "accepting_orders_fields": accepting_info["accepting_orders_fields"],
        "resolved": resolved,
        "resolution_pending": closed and not resolved,
        "disputed": disputed,
        "market_status": status,
        "raw_status": raw_status,
        "status_conflicts": status_conflicts,
    }


def market_is_live_tradable(market: dict[str, Any]) -> bool:
    return market_state(market)["market_status"] == "active_trading"


def is_weather_market(market: dict[str, Any], event_title: str = "") -> bool:
    text = " ".join(str(x or "") for x in [event_title, market.get("question"), market.get("slug"), market.get("description"), market.get("category")]).lower()
    return any(k in text for k in WEATHER_KEYWORDS) and "whether" not in text


def canonical_city(value: Any) -> str:
    raw = " ".join(str(value or "").replace("-", " ").strip(" ?.,;:!").split())
    if not raw:
        return ""
    return " ".join(part if part.isupper() else part[:1].upper() + part[1:].lower() for part in raw.split())


def parse_weather_metric(text: str) -> str:
    raw = norm_cmp(text)
    if re.search(r"\b(highest|high|max(?:imum)?)\s+(?:temp|temperature)\b", raw):
        return "high"
    if re.search(r"\b(lowest|low|min(?:imum)?)\s+(?:temp|temperature)\b", raw):
        return "low"
    return ""


def infer_year(text: str, fallback_year: int | None = None) -> int:
    m = re.search(r"\b(20\d{2}|19\d{2}|21\d{2})\b", str(text or ""))
    if m:
        return int(m.group(1))
    if fallback_year:
        return fallback_year
    return utcnow().year


def fallback_year_from_market(market: dict[str, Any]) -> int | None:
    for key in ("weather_date_local", "endDate", "endDateIso", "end_date_iso", "end_date"):
        raw = str(market.get(key) or "")
        m = re.search(r"\b(20\d{2}|19\d{2}|21\d{2})-\d{2}-\d{2}", raw)
        if m:
            return int(m.group(1))
    return None


def parse_weather_date(text: str, fallback_year: int | None = None) -> str:
    raw = norm_cmp(text)
    m = re.search(r"\b(20\d{2}|19\d{2}|21\d{2})\s+(\d{1,2})\s+(\d{1,2})\b", raw)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\bon\s+({month_names})\s+(\d{{1,2}})(?:\s+(20\d{{2}}|19\d{{2}}|21\d{{2}}))?\b", raw)
    if m:
        year = int(m.group(3)) if m.group(3) else infer_year(text, fallback_year)
        return f"{year:04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.search(rf"\bon\s+(\d{{1,2}})\s+({month_names})(?:\s+(20\d{{2}}|19\d{{2}}|21\d{{2}}))?\b", raw)
    if m:
        year = int(m.group(3)) if m.group(3) else infer_year(text, fallback_year)
        return f"{year:04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:\s+(20\d{{2}}|19\d{{2}}|21\d{{2}}))?\b", raw)
    if m:
        year = int(m.group(3)) if m.group(3) else infer_year(text, fallback_year)
        return f"{year:04d}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    return ""


def parse_temperature_bucket_info(text: str) -> dict[str, Any]:
    raw = str(text or "").replace("°", "")
    canonical = re.search(r"\b(?P<bucket_type>exact|or_below|or_higher)\s*:\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[cCfF])\b", raw)
    if canonical:
        threshold = dec(canonical.group("value"))
        unit = canonical.group("unit").upper()
        bucket_type = canonical.group("bucket_type").lower()
        return {
            "bucket_type": bucket_type,
            "threshold_value": threshold,
            "unit": unit,
            "canonical_label": f"{bucket_type}:{canonical_temp_decimal(threshold)}{unit}",
            "parsing_status": "ok",
        }
    spaced = re.sub(r"-(?!\d)", " ", raw)
    spaced = re.sub(r"[_/]+", " ", spaced)
    spaced = re.sub(r"\s+", " ", spaced).strip()
    m = re.search(r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>[cCfF])\b(?:\s*(?:or\s*)?(?P<qual>below|lower|less|higher|above|more))?", spaced, flags=re.I)
    if not m:
        return {"bucket_type": "", "threshold_value": None, "unit": "", "canonical_label": "", "parsing_status": "unknown"}
    threshold = dec(m.group("value"))
    unit = m.group("unit").upper()
    qual = (m.group("qual") or "").lower()
    if qual in {"below", "lower", "less"}:
        bucket_type = "or_below"
    elif qual in {"higher", "above", "more"}:
        bucket_type = "or_higher"
    else:
        bucket_type = "exact"
    label = f"{bucket_type}:{canonical_temp_decimal(threshold)}{unit}"
    return {
        "bucket_type": bucket_type,
        "threshold_value": threshold,
        "unit": unit,
        "canonical_label": label,
        "parsing_status": "ok",
    }


def parse_temperature_bucket(text: str) -> str:
    return str(parse_temperature_bucket_info(text).get("canonical_label") or "")


def parse_city_from_weather_text(text: str) -> str:
    original = " ".join(str(text or "").replace("°", "").replace("-", " ").strip().split())
    if not original:
        return ""
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    pattern = (
        r"(?:highest|lowest|high|low)\s+(?:temp|temperature)\s+"
        r"(?:in|for)\s+"
        r"(?P<city>.+?)"
        r"(?="
        r"\s+(?:be|reach|at)\s+-?\d"
        r"|\s+-?\d+(?:\.\d+)?\s*[cCfF]\b"
        rf"|\s+on\s+(?:{month_names}|\d{{1,2}}|20\d{{2}})"
        r"|\?|$)"
    )
    m = re.search(pattern, original, flags=re.I)
    if not m:
        return ""
    city = re.sub(r"\b(will|the|be|reach|at)\b", " ", m.group("city"), flags=re.I)
    return canonical_city(city)


def parse_weather_text(text: str, fallback_year: int | None = None) -> dict[str, Any]:
    metric = parse_weather_metric(text)
    bucket = parse_temperature_bucket_info(text)
    date = parse_weather_date(text, fallback_year)
    city = parse_city_from_weather_text(text)
    status = "ok" if metric and date and city and bucket.get("canonical_label") else "unknown"
    missing = [name for name, value in [("city", city), ("weather_metric", metric), ("weather_date_local", date), ("temperature_bucket", bucket.get("canonical_label"))] if not value]
    return {
        "city": city,
        "weather_metric": metric,
        "weather_date_local": date,
        "bucket_type": bucket.get("bucket_type") or "",
        "threshold_value": bucket.get("threshold_value"),
        "unit": bucket.get("unit") or "",
        "canonical_label": bucket.get("canonical_label") or "",
        "parsing_status": status,
        "missing_fields": missing,
    }


def parse_weather_market(market: dict[str, Any], event_title: str = "") -> dict[str, Any]:
    fallback_year = fallback_year_from_market(market)
    question_text = " ".join(str(x or "") for x in [market.get("question"), market.get("title"), event_title]).strip()
    slug_text = str(market.get("slug") or "").replace("-", " ")
    group_text = str(market.get("groupItemTitle") or "")
    question_parse = parse_weather_text(question_text, fallback_year)
    slug_parse = parse_weather_text(slug_text, fallback_year)
    group_bucket = parse_temperature_bucket_info(group_text)
    if not question_parse.get("canonical_label") and group_bucket.get("canonical_label"):
        question_parse.update(
            {
                "bucket_type": group_bucket.get("bucket_type"),
                "threshold_value": group_bucket.get("threshold_value"),
                "unit": group_bucket.get("unit"),
                "canonical_label": group_bucket.get("canonical_label"),
            }
        )
        question_parse["missing_fields"] = [x for x in question_parse.get("missing_fields", []) if x != "temperature_bucket"]
        question_parse["parsing_status"] = "ok" if not question_parse["missing_fields"] else "unknown"
    merged: dict[str, Any] = {}
    conflicts: list[str] = []
    fields = ["city", "weather_metric", "weather_date_local", "bucket_type", "unit", "canonical_label"]
    for field in fields:
        qv = question_parse.get(field) or ""
        sv = slug_parse.get(field) or ""
        merged[field] = qv or sv
        if qv and sv:
            left = norm_cmp(qv) if field == "city" else str(qv)
            right = norm_cmp(sv) if field == "city" else str(sv)
            if left != right:
                conflicts.append(field)
    qthr = question_parse.get("threshold_value")
    sthr = slug_parse.get("threshold_value")
    merged["threshold_value"] = qthr if qthr is not None else sthr
    if qthr is not None and sthr is not None and dec(qthr) != dec(sthr):
        conflicts.append("threshold_value")
    if not merged.get("weather_date_local"):
        date_raw = str(market.get("weather_date_local") or market.get("endDate") or market.get("endDateIso") or "")
        m = re.search(r"\b(20\d{2}|19\d{2}|21\d{2})-\d{2}-\d{2}", date_raw)
        if m:
            merged["weather_date_local"] = m.group(0)
    missing = [name for name in ["city", "weather_metric", "weather_date_local", "canonical_label"] if not merged.get(name)]
    if conflicts:
        status = "conflict"
    elif missing:
        status = "unknown"
    else:
        status = "ok"
    city = canonical_city(merged.get("city", ""))
    event_key = "|".join([norm_cmp(city), str(merged.get("weather_date_local", "")), str(merged.get("weather_metric", ""))])
    return {
        "city": city,
        "weather_metric": str(merged.get("weather_metric", "")),
        "weather_date_local": str(merged.get("weather_date_local", "")),
        "bucket_type": str(merged.get("bucket_type", "")),
        "threshold_value": merged.get("threshold_value"),
        "unit": str(merged.get("unit", "")),
        "canonical_label": str(merged.get("canonical_label", "")),
        "temperature_bucket": str(merged.get("canonical_label", "")),
        "event_key": event_key,
        "parsing_status": status,
        "parsing_errors": conflicts + [f"missing_{x}" for x in missing],
        "question_parse": question_parse,
        "slug_parse": slug_parse,
    }


def level_from_raw(level: dict[str, Any], tick_size: Decimal) -> dict[str, Decimal]:
    price = dec(level.get("price"))
    size = dec(level.get("size"))
    if price < ZERO or price > ONE:
        raise AdapterError("invalid_price", f"invalid price: {level.get('price')}")
    if size <= ZERO:
        raise AdapterError("invalid_size", f"invalid size: {level.get('size')}")
    if tick_size > ZERO and (price / tick_size) != (price / tick_size).to_integral_value():
        raise AdapterError("invalid_tick", f"price {price} does not align with tick {tick_size}")
    return {"price": price, "size": size}


def merge_price_levels(levels: list[dict[str, Decimal]], *, reverse: bool) -> list[dict[str, Decimal]]:
    merged: dict[str, Decimal] = {}
    for level in levels:
        key = dstr(level["price"])
        merged[key] = merged.get(key, ZERO) + level["size"]
    out = [{"price": dec(price), "size": size} for price, size in merged.items() if size > ZERO]
    return sorted(out, key=lambda x: x["price"], reverse=reverse)


def first_decimal(source: dict[str, Any], keys: tuple[str, ...]) -> tuple[Decimal | None, str]:
    for key in keys:
        value = source.get(key)
        if value not in ("", None):
            return dec(value), key
    return None, ""


def order_constraints(raw_book: dict[str, Any], gamma_market: dict[str, Any] | None = None) -> dict[str, Any]:
    clob_tick, clob_tick_field = first_decimal(raw_book, ("tick_size", "tickSize", "minimumTickSize"))
    clob_min, clob_min_field = first_decimal(raw_book, ("min_order_size", "minOrderSize", "minimumOrderSize"))
    gamma_tick, gamma_tick_field = first_decimal(gamma_market or {}, ("orderPriceMinTickSize", "tickSize", "minimumTickSize", "tick_size"))
    gamma_min, gamma_min_field = first_decimal(gamma_market or {}, ("orderMinSize", "minOrderSize", "minimumOrderSize", "min_order_size"))
    details: list[str] = []
    selected_tick = clob_tick
    selected_min = clob_min
    if clob_tick is not None and clob_tick <= ZERO:
        raise AdapterError("invalid_constraints", "clob tick_size must be positive")
    if clob_min is not None and clob_min <= ZERO:
        raise AdapterError("invalid_constraints", "clob min_order_size must be positive")
    if gamma_tick is not None and gamma_tick <= ZERO:
        raise AdapterError("invalid_constraints", "gamma tick_size must be positive")
    if gamma_min is not None and gamma_min <= ZERO:
        raise AdapterError("invalid_constraints", "gamma min_order_size must be positive")
    if clob_tick is not None and gamma_tick is not None and clob_tick != gamma_tick:
        status = "conflict"
        details.append(f"gamma_tick={gamma_tick} clob_tick={clob_tick}")
    elif clob_min is not None and gamma_min is not None and clob_min != gamma_min:
        status = "conflict"
        details.append(f"gamma_min_order={gamma_min} clob_min_order={clob_min}")
    elif clob_tick is not None and clob_min is not None and gamma_tick is not None and gamma_min is not None:
        status = "official"
    elif clob_tick is not None and clob_min is not None:
        status = "official_clob_only"
        details.append("gamma_missing")
    elif gamma_tick is not None and gamma_min is not None:
        status = "unknown"
        details.append("clob_missing")
        selected_tick = gamma_tick
        selected_min = gamma_min
    else:
        status = "unknown"
        details.append("constraints_missing")
    if status in {"conflict", "unknown"}:
        raise AdapterError("constraints_" + status, ";".join(details))
    return {
        "gamma_tick_size": gamma_tick,
        "gamma_tick_field": gamma_tick_field,
        "gamma_min_order_size": gamma_min,
        "gamma_min_order_field": gamma_min_field,
        "clob_tick_size": clob_tick,
        "clob_tick_field": clob_tick_field,
        "clob_min_order_size": clob_min,
        "clob_min_order_field": clob_min_field,
        "selected_tick_size": selected_tick,
        "selected_min_order_size": selected_min,
        "constraint_crosscheck_status": status,
        "constraint_conflict_details": ";".join(details),
        "raw_gamma_hash": content_hash(gamma_market or {}),
        "raw_clob_hash": content_hash(raw_book),
    }


def normalize_orderbook(raw: dict[str, Any], expected_token_id: str | None = None, expected_condition_id: str | None = None, gamma_market: dict[str, Any] | None = None) -> dict[str, Any]:
    if "bids" not in raw or "asks" not in raw:
        raise AdapterError("missing_field", "orderbook missing bids or asks")
    constraints = order_constraints(raw, gamma_market)
    tick_size = constraints["selected_tick_size"]
    min_order_size = constraints["selected_min_order_size"]
    if tick_size is None or min_order_size is None:
        raise AdapterError("constraints_unknown", "selected tick/min constraints are unavailable")
    asset_id = str(raw.get("asset_id") or raw.get("token_id") or "")
    condition_id = str(raw.get("market") or raw.get("condition_id") or raw.get("conditionId") or "")
    if expected_token_id and asset_id and asset_id != expected_token_id:
        raise AdapterError("asset_mismatch", "orderbook asset_id does not match requested token")
    if expected_condition_id and condition_id and condition_id.lower() != expected_condition_id.lower():
        raise AdapterError("condition_mismatch", "orderbook market condition does not match signal")
    bids = merge_price_levels([level_from_raw(x, tick_size) for x in raw.get("bids") or []], reverse=True)
    asks = merge_price_levels([level_from_raw(x, tick_size) for x in raw.get("asks") or []], reverse=False)
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    if best_bid is not None and best_ask is not None and best_bid > best_ask:
        raise AdapterError("crossed_book", "best bid is above best ask")
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    normalized = {
        "market": condition_id,
        "asset_id": asset_id,
        "timestamp": raw.get("timestamp"),
        "hash": raw.get("hash"),
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": spread,
        "bid_depth_levels": len(bids),
        "ask_depth_levels": len(asks),
        "total_bid_shares": sum((x["size"] for x in bids), ZERO),
        "total_ask_shares": sum((x["size"] for x in asks), ZERO),
        "tick_size": tick_size,
        "min_order_size": min_order_size,
        **constraints,
        "neg_risk": raw.get("neg_risk"),
        "empty": not bids and not asks,
    }
    book_for_hash = {
        "algorithm_version": NORMALIZED_BOOK_ALGORITHM_VERSION,
        "market": normalized["market"],
        "asset_id": normalized["asset_id"],
        "timestamp": normalized["timestamp"],
        "bids": normalized["bids"],
        "asks": normalized["asks"],
        "tick_size": normalized["tick_size"],
        "min_order_size": normalized["min_order_size"],
        "best_bid": normalized["best_bid"],
        "best_ask": normalized["best_ask"],
        "spread": normalized["spread"],
    }
    normalized["normalized_book"] = book_for_hash
    normalized["normalized_book_json"] = stable_json(book_for_hash)
    normalized["normalized_book_sha256"] = sha256(normalized["normalized_book_json"].encode("utf-8")).hexdigest()
    normalized["normalization_algorithm_version"] = NORMALIZED_BOOK_ALGORITHM_VERSION
    normalized["content_hash"] = normalized["normalized_book_sha256"]
    return normalized


def consume_buy_depth(book: dict[str, Any], intended_usd: Decimal, max_price: Decimal) -> dict[str, Any]:
    if intended_usd <= ZERO:
        return {"status": "invalid_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_usd": intended_usd, "vwap": None, "levels": []}
    remaining = intended_usd
    shares = ZERO
    gross = ZERO
    levels: list[dict[str, Decimal]] = []
    min_order_size = dec(book.get("min_order_size") or "1")
    for level in [dict(x) for x in book.get("asks", [])]:
        if remaining <= ZERO or level["price"] > max_price:
            break
        before = level["size"]
        qty = min(level["size"], remaining / level["price"]) if level["price"] > ZERO else ZERO
        if qty <= ZERO:
            continue
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= usd
        after = before - qty
        levels.append({
            "price": level["price"],
            "shares": qty,
            "usd": usd,
            "book_price": level["price"],
            "available_shares_before": before,
            "consumed_shares": qty,
            "available_shares_after": after,
            "notional": usd,
            "sequence_index": len(levels) + 1,
        })
    if ZERO < shares < min_order_size:
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_usd": intended_usd, "vwap": None, "levels": [], "would_have_filled_shares": shares}
    return {
        "status": "filled" if remaining <= Decimal("0.00000001") else "partial",
        "filled_shares": shares,
        "filled_usd": gross,
        "remaining_usd": max(remaining, ZERO),
        "vwap": gross / shares if shares > ZERO else None,
        "levels": levels,
        "remaining_below_min_order_size": ZERO < shares and sum((x["size"] for x in book.get("asks", [])), ZERO) - shares < min_order_size,
    }


def consume_sell_depth(book: dict[str, Any], shares_to_sell: Decimal) -> dict[str, Any]:
    min_order_size = dec(book.get("min_order_size") or "1")
    if shares_to_sell < min_order_size:
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_shares": shares_to_sell, "vwap": None, "levels": []}
    remaining = shares_to_sell
    shares = ZERO
    gross = ZERO
    levels: list[dict[str, Decimal]] = []
    for level in [dict(x) for x in book.get("bids", [])]:
        if remaining <= ZERO:
            break
        before = level["size"]
        qty = min(level["size"], remaining)
        if qty <= ZERO:
            continue
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= qty
        after = before - qty
        levels.append({
            "price": level["price"],
            "shares": qty,
            "usd": usd,
            "book_price": level["price"],
            "available_shares_before": before,
            "consumed_shares": qty,
            "available_shares_after": after,
            "notional": usd,
            "sequence_index": len(levels) + 1,
        })
    if ZERO < shares < min_order_size:
        return {"status": "below_min_order_size", "filled_shares": ZERO, "filled_usd": ZERO, "remaining_shares": shares_to_sell, "vwap": None, "levels": []}
    return {
        "status": "filled" if remaining <= Decimal("0.00000001") else "partial",
        "filled_shares": shares,
        "filled_usd": gross,
        "remaining_shares": max(remaining, ZERO),
        "vwap": gross / shares if shares > ZERO else None,
        "levels": levels,
        "remaining_below_min_order_size": ZERO < remaining < min_order_size,
    }


def gamma_fee_rate(market: dict[str, Any]) -> Decimal | None:
    schedule = market.get("feeSchedule") if isinstance(market.get("feeSchedule"), dict) else {}
    raw = schedule.get("rate")
    return None if raw in ("", None) else dec(raw)


def clob_fee_details(clob_info: dict[str, Any]) -> dict[str, Any]:
    fd = clob_info.get("fd") if isinstance(clob_info.get("fd"), dict) else {}
    rate = None if fd.get("r") in ("", None) else dec(fd.get("r"))
    exponent = None if fd.get("e") in ("", None) else dec(fd.get("e"))
    return {
        "clob_fee_rate": rate,
        "clob_fee_exponent": exponent,
        "clob_taker_only": fd.get("to"),
        "clob_fees_disabled": bool(clob_info.get("fees_disabled") or clob_info.get("feesDisabled") or fd.get("disabled")),
        "clob_fee_effective_from": fd.get("ef") or fd.get("effectiveFrom") or fd.get("from") or "",
        "raw_fd": fd,
    }


def extract_fee_policy(gamma_market: dict[str, Any], clob_info: dict[str, Any]) -> dict[str, Any]:
    fees_enabled = gamma_market.get("feesEnabled")
    gamma_rate = gamma_fee_rate(gamma_market)
    clob = clob_fee_details(clob_info)
    clob_rate = clob["clob_fee_rate"]
    schedule = gamma_market.get("feeSchedule") if isinstance(gamma_market.get("feeSchedule"), dict) else {}
    raw_gamma_hash = content_hash(gamma_market)
    raw_clob_hash = content_hash(clob_info)
    clob_disabled = bool(clob.get("clob_fees_disabled"))
    gamma_disabled = fees_enabled is False
    gamma_nonzero = gamma_rate not in (None, ZERO)
    clob_nonzero = clob_rate not in (None, ZERO)
    if clob["clob_fee_exponent"] not in (None, Decimal("1")):
        status = "unsupported_fee_exponent"
        conflict = f"unsupported_clob_fee_exponent={clob['clob_fee_exponent']}"
    elif gamma_disabled and clob_nonzero:
        status = "conflict"
        conflict = f"gamma_disabled_but_clob_fee_rate={clob_rate}"
    elif clob_disabled and gamma_nonzero:
        status = "conflict"
        conflict = f"clob_disabled_but_gamma_fee_rate={gamma_rate}"
    elif gamma_disabled and (clob_disabled or clob_rate in (None, ZERO)):
        status = "disabled"
        conflict = ""
    elif clob_rate is None and gamma_rate is None:
        status = "unknown"
        conflict = "missing_clob_and_gamma_fee_rate"
    elif clob_rate is None:
        status = "unknown"
        conflict = "missing_clob_fee_rate"
    elif gamma_rate is not None and gamma_rate != clob_rate:
        status = "conflict"
        conflict = f"clob_fee_rate={clob_rate} gamma_fee_rate={gamma_rate}"
    elif gamma_rate is None:
        status = "official"
        conflict = "gamma_fee_schedule_missing"
    else:
        status = "official"
        conflict = ""
    return {
        "fees_enabled": fees_enabled,
        "fee_status": status,
        "fee_rate": clob_rate if status == "official" else (Decimal("0") if status == "disabled" else None),
        "gamma_fee_rate": gamma_rate,
        "gamma_fee_schedule": schedule,
        "fee_crosscheck_status": status,
        "fee_conflict_details": conflict,
        "raw_gamma_market_hash": raw_gamma_hash,
        "raw_clob_market_hash": raw_clob_hash,
        **clob,
    }


def calculate_fee(action: str, shares: Decimal, price: Decimal, fee_policy: dict[str, Any]) -> dict[str, Any]:
    gross = shares * price
    status = fee_policy.get("fee_status")
    if action == "settlement":
        return {
            "action": action,
            "gross_notional": gross,
            "official_fee": ZERO,
            "fallback_fee": None,
            "fee_status": "settlement_fee_not_confirmed",
            "gross_cost_or_proceeds": gross,
            "net_cost_or_proceeds": gross,
        }
    if status == "disabled":
        fee_value = ZERO
    elif status == "official" and fee_policy.get("fee_rate") is not None:
        fee_value = (shares * dec(fee_policy["fee_rate"]) * price * (ONE - price)).quantize(FEE_QUANT, rounding=ROUND_HALF_EVEN)
    else:
        return {
            "action": action,
            "gross_notional": gross,
            "official_fee": None,
            "fallback_fee": None,
            "fee_status": status or "unknown",
            "gross_cost_or_proceeds": gross,
            "net_cost_or_proceeds": None,
        }
    net = gross + fee_value if action == "buy" else gross - fee_value
    return {
        "action": action,
        "gross_notional": gross,
        "official_fee": fee_value,
        "fallback_fee": None,
        "fee_status": status,
        "gross_cost_or_proceeds": gross,
        "net_cost_or_proceeds": net,
    }


def validate_token_mapping(signal: dict[str, Any], gamma_market: dict[str, Any], clob_info: dict[str, Any], orderbook: dict[str, Any] | None = None) -> dict[str, Any]:
    errors: list[str] = []
    gamma_condition = condition_id_from_gamma(gamma_market)
    if gamma_condition.lower() != str(signal.get("condition_id", "")).lower():
        errors.append("CONDITION_ID_MISMATCH")
    clob_condition = condition_id_from_clob(clob_info)
    if clob_condition and clob_condition.lower() != str(signal.get("condition_id", "")).lower():
        errors.append("CONDITION_ID_MISMATCH")
    gamma_pairs = gamma_token_pairs(gamma_market)
    clob_pairs = clob_token_pairs(clob_info)
    gamma_by_outcome = {normalize_outcome(x["outcome"]): x["token_id"] for x in gamma_pairs}
    clob_by_outcome = {normalize_outcome(x["outcome"]): x["token_id"] for x in clob_pairs}
    outcome_key = normalize_outcome(str(signal.get("outcome", "")))
    expected_token_gamma = gamma_by_outcome.get(outcome_key)
    expected_token_clob = clob_by_outcome.get(outcome_key)
    token_id = str(signal.get("token_id", ""))
    if not expected_token_gamma or expected_token_gamma != token_id:
        errors.append("TOKEN_ID_MISMATCH")
    if clob_pairs and expected_token_clob != token_id:
        errors.append("TOKEN_ID_MISMATCH")
    for pair in gamma_pairs:
        c = clob_by_outcome.get(normalize_outcome(pair["outcome"]))
        if c and c != pair["token_id"]:
            errors.append("OUTCOME_MAPPING_MISMATCH")
            break
    if orderbook:
        if str(orderbook.get("asset_id") or "") != token_id:
            errors.append("TOKEN_ID_MISMATCH")
        if str(orderbook.get("market") or "").lower() != str(signal.get("condition_id", "")).lower():
            errors.append("CONDITION_ID_MISMATCH")
    info = parse_weather_market(gamma_market, str(gamma_market.get("title") or ""))
    if info.get("parsing_status") == "conflict":
        errors.append("WEATHER_MARKET_PARSING_CONFLICT")
    elif info.get("parsing_status") != "ok":
        errors.append("WEATHER_MARKET_PARSING_UNKNOWN")
    signal_bucket_info = parse_temperature_bucket_info(str(signal.get("temperature_bucket", "")))
    if signal_bucket_info.get("parsing_status") != "ok":
        errors.append("TEMPERATURE_BUCKET_UNKNOWN")
    if norm_cmp(str(signal.get("city", ""))) != norm_cmp(info.get("city", "")):
        errors.append("CITY_MISMATCH")
    if str(signal.get("weather_date_local", "")) != str(info.get("weather_date_local", "")):
        errors.append("WEATHER_DATE_MISMATCH")
    signal_metric = str(signal.get("weather_metric", "")).strip().lower()
    signal_metric = {"highest": "high", "highest temperature": "high", "lowest": "low", "lowest temperature": "low"}.get(signal_metric, signal_metric)
    if signal_metric != str(info.get("weather_metric", "")):
        errors.append("WEATHER_METRIC_MISMATCH")
    if signal_bucket_info.get("bucket_type") != info.get("bucket_type"):
        errors.append("BUCKET_TYPE_MISMATCH")
    if signal_bucket_info.get("threshold_value") is not None and info.get("threshold_value") is not None and dec(signal_bucket_info["threshold_value"]) != dec(info["threshold_value"]):
        errors.append("TEMPERATURE_THRESHOLD_MISMATCH")
    if signal_bucket_info.get("unit") != info.get("unit"):
        errors.append("TEMPERATURE_UNIT_MISMATCH")
    market_event = str(info.get("event_key") or "")
    signal_event = "|".join([norm_cmp(str(signal.get("city", ""))), str(signal.get("weather_date_local", "")), signal_metric])
    errors = sorted(set(errors))
    return {
        "mapping_valid": not errors,
        "errors": errors,
        "gamma_pairs": gamma_pairs,
        "clob_pairs": clob_pairs,
        "parsed_temperature_bucket": info.get("canonical_label", ""),
        "signal_temperature_bucket": signal_bucket_info.get("canonical_label", ""),
        "market_parse": info,
        "signal_event_key": signal_event,
        "market_event_key": market_event,
        "raw_gamma_market_hash": content_hash(gamma_market),
        "raw_clob_market_hash": content_hash(clob_info),
        "raw_orderbook_hash": content_hash(orderbook) if orderbook else "",
    }


def parse_market_status(market: dict[str, Any]) -> dict[str, Any]:
    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    raw_status = market.get("umaResolutionStatus") or market.get("umaResolutionStatuses") or ""
    resolved = bool(market.get("resolved") or market.get("automaticallyResolved") or str(raw_status).lower() in {"resolved", "settled"})
    if resolved:
        status = "resolved"
    elif closed:
        status = "resolution_pending"
    elif active:
        status = "active"
    else:
        status = "unknown"
    return {"active": active, "closed": closed, "resolved": resolved, "market_status": status, "resolution_status": raw_status}


def parse_settlement_evidence(gamma_market: dict[str, Any], token_pairs: list[dict[str, str]]) -> dict[str, Any]:
    status = parse_market_status(gamma_market)
    raw_hash = content_hash(gamma_market)
    token_values = {p["token_id"]: None for p in token_pairs if p.get("token_id")}
    uma_raw = gamma_market.get("umaResolutionStatus") or gamma_market.get("resolutionStatus") or gamma_market.get("umaResolutionStatuses") or ""
    uma_text = stable_json(uma_raw).lower() if isinstance(uma_raw, (dict, list)) else str(uma_raw).lower()
    proposed = "proposed" in uma_text or "pending" in uma_text
    disputed = any(x in uma_text for x in ["disputed", "challenged", "challenge"])
    final_status = any(x in uma_text for x in ["final", "resolved", "settled"]) or bool(gamma_market.get("marketResolved") or gamma_market.get("market_resolved"))
    automatically_resolved = bool(gamma_market.get("automaticallyResolved"))
    base = {
        **status,
        "uma_status": uma_text,
        "finality_status": "unknown",
        "resolution_status": uma_text,
        "evidence_tier": "",
        "conflict_details": "",
        "winning_asset_id": "",
        "winning_outcome": "",
        "token_settlement_values": token_values,
        "raw_response_hash": raw_hash,
        "error": "",
    }
    if disputed:
        return {**base, "evidence_valid": False, "settlement_status": "disputed", "finality_status": "disputed", "error": "market_disputed"}
    if proposed and automatically_resolved:
        return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "status_conflict", "conflict_details": "automaticallyResolved_with_proposed", "error": "automatically_resolved_proposed_conflict"}
    if proposed:
        return {**base, "evidence_valid": False, "settlement_status": "proposed", "finality_status": "proposed", "error": "resolution_proposed_not_final"}
    if status["market_status"] != "resolved":
        pending = "closed_unresolved" if status.get("closed") else "not_settleable"
        return {**base, "evidence_valid": False, "settlement_status": pending, "finality_status": "not_final", "error": "market_not_resolved"}

    winners: list[tuple[str, str]] = []
    winning_asset = str(gamma_market.get("winning_asset_id") or gamma_market.get("winningAssetId") or gamma_market.get("winningClobTokenId") or "")
    winning_outcome = str(gamma_market.get("winningOutcome") or gamma_market.get("outcome") or gamma_market.get("resolutionOutcome") or "")
    by_token = {p["token_id"]: p["outcome"] for p in token_pairs}
    by_outcome = {normalize_outcome(p["outcome"]): p["token_id"] for p in token_pairs}
    pair_winners = [p for p in token_pairs if p.get("winner") is True or str(p.get("winner", "")).lower() == "true"]
    if pair_winners:
        if len(pair_winners) != 1:
            return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "error": "multiple_token_winners"}
        token = str(pair_winners[0].get("token_id") or "")
        if token not in by_token:
            return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": token, "winning_outcome": winning_outcome, "error": "token_winner_not_in_mapping"}
        winners.append((token, by_token[token]))
    if winning_asset:
        if winning_asset in by_token:
            winners.append((winning_asset, by_token[winning_asset]))
        else:
            return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "error": "winning_asset_not_in_mapping"}
    if winning_outcome:
        token = by_outcome.get(normalize_outcome(winning_outcome))
        if token:
            winners.append((token, winning_outcome))
        else:
            return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "error": "winning_outcome_not_in_mapping"}
    prices = parse_jsonish(gamma_market.get("outcomePrices"))
    price_winner: tuple[str, str] | None = None
    if prices and len(prices) == len(token_pairs):
        binary_prices = all(dec(prices[i], ZERO) in {ZERO, ONE} for i in range(len(prices)))
        resolved_indices = [i for i, p in enumerate(prices) if dec(p, ZERO) == ONE]
        if binary_prices and len(resolved_indices) == 1:
            p = token_pairs[resolved_indices[0]]
            price_winner = (p["token_id"], p["outcome"])
        elif binary_prices and len(resolved_indices) != 1:
            return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "error": "resolved_outcome_prices_conflict"}
    if price_winner and winners and price_winner[0] not in {w[0] for w in winners}:
        return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "conflict_details": "outcomePrices_conflict_with_winner", "error": "winner_sources_conflict"}
    if not winners:
        return {**base, "evidence_valid": False, "settlement_status": "unknown", "finality_status": "winner_missing", "error": "winner_missing"}
    if not winning_asset and winning_outcome and not final_status:
        return {**base, "evidence_valid": False, "settlement_status": "unknown", "finality_status": "winner_not_final", "winning_outcome": winning_outcome, "error": "winning_outcome_without_final_status"}
    if not winning_asset and not final_status:
        return {**base, "evidence_valid": False, "settlement_status": "unknown", "finality_status": "not_final", "winning_outcome": winning_outcome, "error": "final_status_missing"}
    winner_tokens = {w[0] for w in winners}
    if len(winner_tokens) != 1:
        return {**base, "evidence_valid": False, "settlement_status": "conflict", "finality_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "conflict_details": "winner_sources_conflict", "error": "winner_sources_conflict"}
    winner = next(iter(winner_tokens))
    for token in token_values:
        token_values[token] = "1" if token == winner else "0"
    tier = "A_winning_asset_id" if winning_asset else ("A_clob_token_winner" if pair_winners else "B_final_winning_outcome")
    return {**base, "evidence_valid": True, "settlement_status": "final", "finality_status": "resolved_final", "evidence_tier": tier, "winning_asset_id": winner, "winning_outcome": by_token.get(winner, winners[0][1]), "token_settlement_values": token_values, "error": ""}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
