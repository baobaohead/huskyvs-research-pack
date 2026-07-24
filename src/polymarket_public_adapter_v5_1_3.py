#!/usr/bin/env python3
"""Public read-only Polymarket adapter for v5.1.3-RC2.

The adapter intentionally exposes only public GET methods and deterministic
normalizers/calculators. It contains no account, credential, or real-trade
execution capability.
"""

from __future__ import annotations

import json
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

ADAPTER_VERSION = "polymarket_public_adapter_v5.1.3-rc2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "huskyvs-v5.1.3-rc2-readonly/1.0"
ZERO = Decimal("0")
ONE = Decimal("1")
FEE_QUANT = Decimal("0.00001")
WEATHER_KEYWORDS = ("temperature", "weather", "high temp", "low temp", "highest temperature", "lowest temperature")


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
    if isinstance(clob_info.get("t"), list):
        return [{"outcome": str(x.get("o", "")), "token_id": str(x.get("t", ""))} for x in clob_info["t"] if x.get("t")]
    return []


def condition_id_from_gamma(market: dict[str, Any]) -> str:
    return str(market.get("conditionId") or market.get("condition_id") or "")


def condition_id_from_clob(clob_info: dict[str, Any]) -> str:
    return str(clob_info.get("condition_id") or clob_info.get("conditionId") or clob_info.get("id") or "")


def market_is_live_tradable(market: dict[str, Any]) -> bool:
    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    resolved = bool(market.get("resolved") or market.get("automaticallyResolved"))
    accepting = market.get("acceptingOrders")
    return active and not closed and not resolved and accepting is not False


def is_weather_market(market: dict[str, Any], event_title: str = "") -> bool:
    text = " ".join(str(x or "") for x in [event_title, market.get("question"), market.get("slug"), market.get("description"), market.get("category")]).lower()
    return any(k in text for k in WEATHER_KEYWORDS) and "whether" not in text


def parse_weather_market(market: dict[str, Any], event_title: str = "") -> dict[str, str]:
    text = " ".join(str(x or "") for x in [event_title, market.get("question"), market.get("slug"), market.get("groupItemTitle")])
    lower = text.lower()
    metric = "high" if any(x in lower for x in ["high temp", "highest", "high temperature"]) else ("low" if any(x in lower for x in ["low temp", "lowest", "low temperature"]) else "temperature")
    city = ""
    for marker in [" in ", " for "]:
        if marker in lower:
            part = text[lower.index(marker) + len(marker):]
            city = part.split(" on ")[0].split("?")[0].strip(" -")
            break
    if not city:
        city = str(market.get("groupItemTitle") or market.get("eventTitle") or event_title or "unknown city")
    date = str(market.get("endDate") or market.get("endDateIso") or "")[:10]
    return {"city": city, "weather_metric": metric, "weather_date_local": date}


def parse_temperature_bucket(text: str) -> str:
    raw = str(text or "").lower().replace("°", "").replace(" ", "")
    import re

    m = re.search(r"(-?\d+(?:\.\d+)?)(c|f|corbelow|forbelow|c-or-below|f-or-below)?", raw)
    if not m:
        return ""
    temp = m.group(1).rstrip("0").rstrip(".")
    suffix = m.group(2) or ""
    unit = "F" if suffix.startswith("f") else "C"
    if "below" in suffix:
        return f"{temp}{unit}_or_below"
    return f"{temp}{unit}"


def level_from_raw(level: dict[str, Any], tick_size: Decimal) -> dict[str, Decimal]:
    price = dec(level.get("price"))
    size = dec(level.get("size"))
    if price < ZERO or price > ONE:
        raise AdapterError("invalid_price", f"invalid price: {level.get('price')}")
    if size < ZERO:
        raise AdapterError("invalid_size", f"invalid size: {level.get('size')}")
    if tick_size > ZERO and (price / tick_size) != (price / tick_size).to_integral_value():
        raise AdapterError("invalid_tick", f"price {price} does not align with tick {tick_size}")
    return {"price": price, "size": size}


def normalize_orderbook(raw: dict[str, Any], expected_token_id: str | None = None, expected_condition_id: str | None = None) -> dict[str, Any]:
    if "bids" not in raw or "asks" not in raw:
        raise AdapterError("missing_field", "orderbook missing bids or asks")
    tick_size = dec(raw.get("tick_size") or raw.get("tickSize") or "0.001")
    min_order_size = dec(raw.get("min_order_size") or raw.get("minOrderSize") or "1")
    asset_id = str(raw.get("asset_id") or raw.get("token_id") or "")
    condition_id = str(raw.get("market") or raw.get("condition_id") or raw.get("conditionId") or "")
    if expected_token_id and asset_id and asset_id != expected_token_id:
        raise AdapterError("asset_mismatch", "orderbook asset_id does not match requested token")
    if expected_condition_id and condition_id and condition_id.lower() != expected_condition_id.lower():
        raise AdapterError("condition_mismatch", "orderbook market condition does not match signal")
    bids = [level_from_raw(x, tick_size) for x in raw.get("bids") or []]
    asks = [level_from_raw(x, tick_size) for x in raw.get("asks") or []]
    bids = sorted([x for x in bids if x["size"] > ZERO], key=lambda x: x["price"], reverse=True)
    asks = sorted([x for x in asks if x["size"] > ZERO], key=lambda x: x["price"])
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
        "neg_risk": raw.get("neg_risk"),
        "empty": not bids and not asks,
    }
    normalized["content_hash"] = content_hash({k: normalized[k] for k in ["market", "asset_id", "timestamp", "bids", "asks", "tick_size", "min_order_size"]})
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
        qty = min(level["size"], remaining / level["price"]) if level["price"] > ZERO else ZERO
        if qty <= ZERO:
            continue
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= usd
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
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
        qty = min(level["size"], remaining)
        if qty <= ZERO:
            continue
        usd = qty * level["price"]
        shares += qty
        gross += usd
        remaining -= qty
        levels.append({"price": level["price"], "shares": qty, "usd": usd})
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
    if fees_enabled is False:
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
        errors.append("gamma_condition_id_mismatch")
    clob_condition = condition_id_from_clob(clob_info)
    if clob_condition and clob_condition.lower() != str(signal.get("condition_id", "")).lower():
        errors.append("clob_condition_id_mismatch")
    gamma_pairs = gamma_token_pairs(gamma_market)
    clob_pairs = clob_token_pairs(clob_info)
    gamma_by_outcome = {normalize_outcome(x["outcome"]): x["token_id"] for x in gamma_pairs}
    clob_by_outcome = {normalize_outcome(x["outcome"]): x["token_id"] for x in clob_pairs}
    outcome_key = normalize_outcome(str(signal.get("outcome", "")))
    expected_token_gamma = gamma_by_outcome.get(outcome_key)
    expected_token_clob = clob_by_outcome.get(outcome_key)
    token_id = str(signal.get("token_id", ""))
    if not expected_token_gamma or expected_token_gamma != token_id:
        errors.append("gamma_outcome_token_mismatch")
    if clob_pairs and expected_token_clob != token_id:
        errors.append("clob_outcome_token_mismatch")
    for pair in gamma_pairs:
        c = clob_by_outcome.get(normalize_outcome(pair["outcome"]))
        if c and c != pair["token_id"]:
            errors.append("gamma_clob_token_order_conflict")
            break
    if orderbook:
        if str(orderbook.get("asset_id") or "") != token_id:
            errors.append("orderbook_asset_id_mismatch")
        if str(orderbook.get("market") or "").lower() != str(signal.get("condition_id", "")).lower():
            errors.append("orderbook_condition_id_mismatch")
    parsed_bucket = parse_temperature_bucket(" ".join(str(x or "") for x in [gamma_market.get("groupItemTitle"), gamma_market.get("question"), gamma_market.get("slug")]))
    signal_bucket = parse_temperature_bucket(str(signal.get("temperature_bucket", "")))
    if signal_bucket and parsed_bucket and signal_bucket != parsed_bucket:
        errors.append("temperature_bucket_mismatch")
    info = parse_weather_market(gamma_market, str(gamma_market.get("title") or ""))
    signal_event = "|".join([str(signal.get("city", "")).strip().lower(), str(signal.get("weather_date_local", "")), str(signal.get("weather_metric", "")).strip().lower()])
    market_event = "|".join([info["city"].strip().lower(), info["weather_date_local"], info["weather_metric"]])
    if signal_event != market_event:
        errors.append("event_key_mismatch")
    return {
        "mapping_valid": not errors,
        "errors": errors,
        "gamma_pairs": gamma_pairs,
        "clob_pairs": clob_pairs,
        "parsed_temperature_bucket": parsed_bucket,
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
    if status["market_status"] != "resolved":
        return {**status, "evidence_valid": False, "settlement_status": "not_settleable", "winning_asset_id": "", "winning_outcome": "", "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": "market_not_resolved"}

    winners: list[tuple[str, str]] = []
    winning_asset = str(gamma_market.get("winning_asset_id") or gamma_market.get("winningAssetId") or gamma_market.get("winningClobTokenId") or "")
    winning_outcome = str(gamma_market.get("winningOutcome") or gamma_market.get("outcome") or gamma_market.get("resolutionOutcome") or "")
    by_token = {p["token_id"]: p["outcome"] for p in token_pairs}
    by_outcome = {normalize_outcome(p["outcome"]): p["token_id"] for p in token_pairs}
    if winning_asset:
        if winning_asset in by_token:
            winners.append((winning_asset, by_token[winning_asset]))
        else:
            return {**status, "evidence_valid": False, "settlement_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": "winning_asset_not_in_mapping"}
    if winning_outcome:
        token = by_outcome.get(normalize_outcome(winning_outcome))
        if token:
            winners.append((token, winning_outcome))
    prices = parse_jsonish(gamma_market.get("outcomePrices"))
    if prices and len(prices) == len(token_pairs):
        binary_prices = all(dec(prices[i], ZERO) in {ZERO, ONE} for i in range(len(prices)))
        resolved_indices = [i for i, p in enumerate(prices) if dec(p, ZERO) == ONE]
        if binary_prices and len(resolved_indices) == 1:
            p = token_pairs[resolved_indices[0]]
            winners.append((p["token_id"], p["outcome"]))
        elif binary_prices and len(resolved_indices) != 1:
            return {**status, "evidence_valid": False, "settlement_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": "resolved_outcome_prices_conflict"}
    if not winners:
        return {**status, "evidence_valid": False, "settlement_status": "unknown", "winning_asset_id": "", "winning_outcome": "", "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": "winner_missing"}
    winner_tokens = {w[0] for w in winners}
    if len(winner_tokens) != 1:
        return {**status, "evidence_valid": False, "settlement_status": "conflict", "winning_asset_id": winning_asset, "winning_outcome": winning_outcome, "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": "winner_sources_conflict"}
    winner = next(iter(winner_tokens))
    for token in token_values:
        token_values[token] = "1" if token == winner else "0"
    return {**status, "evidence_valid": True, "settlement_status": "final", "winning_asset_id": winner, "winning_outcome": by_token.get(winner, winners[0][1]), "token_settlement_values": token_values, "raw_response_hash": raw_hash, "error": ""}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
