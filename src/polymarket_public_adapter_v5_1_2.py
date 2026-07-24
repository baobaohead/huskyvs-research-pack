#!/usr/bin/env python3
"""Public read-only Polymarket adapter for v5.1.2 live integration."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable


ADAPTER_VERSION = "polymarket_public_adapter_v5.1.2"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "huskyvs-v5.1.2-readonly/1.0"
WEATHER_KEYWORDS = ("temperature", "temp", "weather", "high temp", "low temp", "highest temperature", "lowest temperature")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return sha256(stable_json(value).encode("utf-8")).hexdigest()


def fnum(value: Any, default: float = math.nan) -> float:
    try:
        if value in ("", None):
            return default
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


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
    latency_ms: float
    started_at_utc: str
    received_at_utc: str
    payload: Any
    raw_text: str


class PublicAdapter:
    def __init__(
        self,
        gamma_base: str = GAMMA_BASE,
        clob_base: str = CLOB_BASE,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        transport: Callable[[str, str, float], tuple[int, str]] | None = None,
    ):
        self.gamma_base = gamma_base.rstrip("/")
        self.clob_base = clob_base.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
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
                latency = (time.monotonic() - t0) * 1000
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
                self.visited_endpoints.append({"method": "GET", "url": url, "status_code": status, "latency_ms": latency})
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

    def list_events(self, limit: int = 100, offset: int = 0) -> HttpResult:
        return self.get_json(self.gamma_base, "/events", {"active": "true", "closed": "false", "limit": limit, "offset": offset})

    def list_markets(self, limit: int = 100, offset: int = 0) -> HttpResult:
        return self.get_json(self.gamma_base, "/markets", {"active": "true", "closed": "false", "limit": limit, "offset": offset})

    def search(self, query: str, limit_per_type: int = 10, page: int = 1) -> HttpResult:
        return self.get_json(
            self.gamma_base,
            "/public-search",
            {"q": query, "events_status": "active", "limit_per_type": limit_per_type, "page": page, "keep_closed_markets": 0},
        )

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


def token_mapping_from_market(market: dict[str, Any], clob_info: dict[str, Any] | None = None) -> list[dict[str, str]]:
    outcomes = [str(x) for x in parse_jsonish(market.get("outcomes"))]
    token_ids = [str(x) for x in parse_jsonish(market.get("clobTokenIds"))]
    if clob_info and isinstance(clob_info.get("t"), list):
        mapped = [{"outcome": str(x.get("o", "")), "token_id": str(x.get("t", ""))} for x in clob_info["t"] if x.get("t")]
        if mapped:
            return mapped
    return [{"outcome": outcome, "token_id": token_ids[i] if i < len(token_ids) else ""} for i, outcome in enumerate(outcomes)]


def normalize_level(level: dict[str, Any]) -> dict[str, float]:
    price = fnum(level.get("price"))
    size = fnum(level.get("size"))
    if not math.isfinite(price) or price < 0 or price > 1:
        raise AdapterError("invalid_price", f"invalid price: {level.get('price')}")
    if not math.isfinite(size) or size < 0:
        raise AdapterError("invalid_size", f"invalid size: {level.get('size')}")
    return {"price": price, "size": size}


def normalize_orderbook(raw: dict[str, Any]) -> dict[str, Any]:
    if "bids" not in raw or "asks" not in raw:
        raise AdapterError("missing_field", "orderbook missing bids or asks")
    bids = [normalize_level(x) for x in raw.get("bids") or []]
    asks = [normalize_level(x) for x in raw.get("asks") or []]
    bids = sorted([x for x in bids if x["size"] > 0], key=lambda x: x["price"], reverse=True)
    asks = sorted([x for x in asks if x["size"] > 0], key=lambda x: x["price"])
    best_bid = bids[0]["price"] if bids else None
    best_ask = asks[0]["price"] if asks else None
    crossed = best_bid is not None and best_ask is not None and best_bid > best_ask
    if crossed:
        raise AdapterError("crossed_book", "best bid is above best ask")
    normalized = {
        "market": raw.get("market"),
        "asset_id": raw.get("asset_id") or raw.get("token_id"),
        "timestamp": raw.get("timestamp"),
        "hash": raw.get("hash"),
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": (best_ask - best_bid) if best_bid is not None and best_ask is not None else None,
        "bid_depth_levels": len(bids),
        "ask_depth_levels": len(asks),
        "total_bid_shares": sum(x["size"] for x in bids),
        "total_ask_shares": sum(x["size"] for x in asks),
        "empty": not bids and not asks,
        "content_hash": content_hash({"bids": bids, "asks": asks, "market": raw.get("market"), "asset_id": raw.get("asset_id") or raw.get("token_id"), "timestamp": raw.get("timestamp")}),
    }
    return normalized


def simulate_buy_vwap(book: dict[str, Any], intended_usd: float) -> dict[str, Any]:
    remaining = float(intended_usd)
    filled_shares = 0.0
    filled_usd = 0.0
    levels = []
    for level in [dict(x) for x in book.get("asks", [])]:
        if remaining <= 1e-12:
            break
        qty = min(level["size"], remaining / level["price"]) if level["price"] > 0 else 0
        usd = qty * level["price"]
        filled_shares += qty
        filled_usd += usd
        remaining -= usd
        if qty > 0:
            levels.append({"price": level["price"], "shares": qty, "usd": usd})
    best = book.get("best_ask")
    vwap = filled_usd / filled_shares if filled_shares else math.nan
    return {"action": "buy", "filled_usd": filled_usd, "filled_shares": filled_shares, "vwap": vwap, "best_price": best, "slippage_vs_best": (vwap - best) if best is not None and filled_shares else math.nan, "depth_levels_consumed": len(levels), "fully_filled": remaining <= 1e-8, "unfilled_amount": max(remaining, 0.0), "levels": levels}


def simulate_sell_vwap(book: dict[str, Any], shares_to_sell: float) -> dict[str, Any]:
    remaining = float(shares_to_sell)
    filled_shares = 0.0
    filled_usd = 0.0
    levels = []
    for level in [dict(x) for x in book.get("bids", [])]:
        if remaining <= 1e-12:
            break
        qty = min(level["size"], remaining)
        usd = qty * level["price"]
        filled_shares += qty
        filled_usd += usd
        remaining -= qty
        if qty > 0:
            levels.append({"price": level["price"], "shares": qty, "usd": usd})
    best = book.get("best_bid")
    vwap = filled_usd / filled_shares if filled_shares else math.nan
    return {"action": "sell", "filled_usd": filled_usd, "filled_shares": filled_shares, "vwap": vwap, "best_price": best, "slippage_vs_best": (best - vwap) if best is not None and filled_shares else math.nan, "depth_levels_consumed": len(levels), "fully_filled": remaining <= 1e-8, "unfilled_amount": max(remaining, 0.0), "levels": levels}


def official_fee(shares: float, price: float, fees_enabled: bool | None, fee_rate: float | None, exponent: float | None = None) -> dict[str, Any]:
    gross = shares * price
    if fees_enabled is False:
        return {"gross_notional": gross, "official_fee": 0.0, "fallback_fee": None, "fee_status": "disabled", "net_proceeds_or_cost": gross, "fee_exponent_observed": exponent}
    if fees_enabled is None or fee_rate is None:
        return {"gross_notional": gross, "official_fee": None, "fallback_fee": None, "fee_status": "unknown", "net_proceeds_or_cost": None, "fee_exponent_observed": exponent}
    fee_value = round(shares * fee_rate * price * (1 - price), 5)
    return {"gross_notional": gross, "official_fee": fee_value, "fallback_fee": None, "fee_status": "official", "net_proceeds_or_cost": gross + fee_value, "fee_exponent_observed": exponent}


def parse_market_status(market: dict[str, Any]) -> dict[str, Any]:
    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    resolved = bool(market.get("resolved") or market.get("automaticallyResolved") or str(market.get("umaResolutionStatus", "")).lower() in {"resolved", "settled"})
    resolution_pending = closed and not resolved
    raw_status = market.get("umaResolutionStatus") or market.get("umaResolutionStatuses") or ""
    if resolved:
        status = "resolved"
    elif resolution_pending:
        status = "resolution_pending"
    elif active and not closed:
        status = "active"
    elif closed:
        status = "closed"
    else:
        status = "unknown"
    return {"active": active, "closed": closed, "resolved": resolved, "resolution_pending": resolution_pending, "raw_status": raw_status, "market_status": status, "winning_outcome": market.get("outcome") or market.get("winningOutcome") or market.get("resolutionOutcome") or ""}


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
    return {"city": city, "weather_metric": metric, "weather_date_local": str(market.get("endDate") or market.get("endDateIso") or "")[:10]}


def is_weather_market(market: dict[str, Any], event_title: str = "") -> bool:
    text = " ".join(str(x or "") for x in [event_title, market.get("question"), market.get("slug"), market.get("description"), market.get("category")]).lower()
    return any(k in text for k in WEATHER_KEYWORDS) and "whether" not in text


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
