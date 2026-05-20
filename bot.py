#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any, Literal

import httpx
from ai_planner import apply_ai_plan_multiplier, build_ai_plan, compact_ai_plan
from ledger import daily_summary, format_daily_summary, record_event
from notifier import notify_channels
from research import build_snapshot as build_research_snapshot
from research import compact_snapshot as compact_research_snapshot


Mode = Literal["paper", "live"]
Side = Literal["buy", "sell", "hold"]

KRAKEN_API_URL = "https://api.kraken.com"
LIVE_ACK = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"


class BotError(RuntimeError):
    pass


@dataclass(frozen=True)
class StrategyConfig:
    fast_sma_period: int
    slow_sma_period: int
    rsi_period: int
    buy_rsi_max: float
    sell_rsi_min: float
    ema_trend_period: int
    macd_fast_period: int
    macd_slow_period: int
    macd_signal_period: int
    bollinger_period: int
    bollinger_stddev: float
    min_buy_score: int
    min_sell_score: int
    min_buy_momentum_pct: Decimal


@dataclass(frozen=True)
class RiskConfig:
    starting_paper_quote: Decimal
    max_order_quote: Decimal
    max_position_quote: Decimal
    risk_per_trade_quote: Decimal
    reserve_quote_balance: Decimal
    daily_loss_limit_quote: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    cooldown_minutes: int
    max_trades_per_day: int
    fee_bps: Decimal
    slippage_bps: Decimal
    min_net_profit_bps: Decimal
    max_daily_equity_drawdown_quote: Decimal
    max_high_water_drawdown_quote: Decimal


@dataclass(frozen=True)
class MarketFilterConfig:
    max_spread_bps: Decimal
    max_candle_range_pct: Decimal
    max_atr_pct: Decimal
    atr_period: int


@dataclass(frozen=True)
class MarketRadarConfig:
    enabled: bool
    pairs: list[str]
    context_pairs: list[str]
    max_pairs_per_cycle: int
    min_positive_breadth_to_increase: Decimal
    block_buy_breadth_below: Decimal


@dataclass(frozen=True)
class AdvancedStrategyConfig:
    enabled: bool
    turtle_breakout_period: int
    turtle_soup_period: int
    supertrend_period: int
    supertrend_multiplier: Decimal
    var_lookback: int
    max_var_pct: Decimal
    max_es_pct: Decimal
    kelly_fraction: Decimal
    min_kelly_multiplier: Decimal
    max_kelly_multiplier: Decimal


@dataclass(frozen=True)
class LLMConfig:
    enabled: bool
    model: str
    veto_risk_score: int
    max_calls_per_day: int
    block_when_unavailable: bool


@dataclass(frozen=True)
class AIPlanConfig:
    enabled: bool
    model: str
    model_fallbacks: list[str]
    authority_mode: str
    reasoning_effort: str
    reasoning_summary: str
    web_search: bool
    force_web_search: bool
    max_web_search_calls: int
    web_search_context_size: str
    max_output_tokens: int
    refresh_interval_minutes: int
    max_calls_per_day: int
    allow_risk_increase: bool
    allow_decision_override: bool
    override_min_confidence: int
    override_min_signal_score: int
    override_max_research_score: int
    override_min_positive_breadth: Decimal
    max_risk_multiplier: Decimal
    max_aggressive_order_quote: Decimal
    min_confidence_to_increase: int
    min_signal_score_to_increase: int
    min_regime_score_to_increase: int
    max_research_score_to_increase: int
    max_spread_bps_to_increase: Decimal
    block_when_unavailable: bool
    history_limit: int


@dataclass(frozen=True)
class ExecutionConfig:
    approval_mode: str
    aggressive_buy_score_max: int
    approval_ttl_minutes: int
    max_cycle_gap_seconds: int
    reconnect_observe_cycles: int
    trading_windows_utc: list[dict[str, str]]
    notify_on_signal: bool
    notify_on_fill: bool
    min_trade_interval_seconds: int


@dataclass(frozen=True)
class MultiTimeframeConfig:
    enabled: bool
    interval_minutes: int
    min_buy_score: int


@dataclass(frozen=True)
class ShadowConfig:
    enabled: bool
    starting_quote: Decimal


@dataclass(frozen=True)
class DownsideConfig:
    enabled: bool
    min_short_score: int
    min_short_momentum_pct: Decimal
    close_short_reversal_score: int
    require_higher_timeframe_downtrend: bool
    block_buys_when_downside: bool
    shadow_short_enabled: bool
    live_short_enabled: bool


@dataclass(frozen=True)
class BotConfig:
    raw: dict[str, Any]
    exchange: str
    pair: str
    ws_symbol: str
    base_asset: str
    quote_asset: str
    mode: Mode
    poll_seconds: int
    candle_interval_minutes: int
    strategy: StrategyConfig
    risk: RiskConfig
    market_filters: MarketFilterConfig
    market_radar: MarketRadarConfig
    advanced_strategy: AdvancedStrategyConfig
    llm: LLMConfig
    ai_plan: AIPlanConfig
    execution: ExecutionConfig
    multi_timeframe: MultiTimeframeConfig
    shadow: ShadowConfig
    downside: DownsideConfig
    scan_pairs: list[str]
    state_file: Path
    trade_log: Path
    pending_order_file: Path
    ledger_db: Path
    notification_outbox_file: Path
    research_cache_file: Path
    ai_plan_file: Path
    ai_plan_log_file: Path
    shadow_state_file: Path
    shadow_log_file: Path


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class Signal:
    side: Side
    reason: str
    price: Decimal
    fast_sma: Decimal
    slow_sma: Decimal
    rsi: Decimal
    score: int = 0
    macd_histogram: Decimal = Decimal("0")
    bollinger_z: Decimal = Decimal("0")
    momentum_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class Ticker:
    bid: Decimal
    ask: Decimal
    last: Decimal
    spread_bps: Decimal
    change_pct: Decimal
    quote_volume: Decimal = Decimal("0")


class KrakenClient:
    def __init__(self, api_key: str | None = None, api_secret: str | None = None) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = httpx.Client(base_url=KRAKEN_API_URL, timeout=20)

    def close(self) -> None:
        self.client.close()

    def public(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.get(f"/0/public/{endpoint}", params=params or {})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise BotError(f"Kraken public API error: {payload['error']}")
        return payload["result"]

    def private(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key or not self.api_secret:
            raise BotError("KRAKEN_API_KEY and KRAKEN_API_SECRET are required for live mode")

        urlpath = f"/0/private/{endpoint}"
        payload = dict(data or {})
        payload["nonce"] = str(int(time.time() * 1000))
        encoded = urllib.parse.urlencode(payload)
        signature = self._signature(urlpath, encoded, payload["nonce"])
        response = self.client.post(
            urlpath,
            content=encoded,
            headers={
                "API-Key": self.api_key,
                "API-Sign": signature,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise BotError(f"Kraken private API error: {body['error']}")
        return body["result"]

    def _signature(self, urlpath: str, encoded_data: str, nonce: str) -> str:
        assert self.api_secret is not None
        secret = base64.b64decode(self.api_secret)
        sha = hashlib.sha256((nonce + encoded_data).encode()).digest()
        mac = hmac.new(secret, urlpath.encode() + sha, hashlib.sha512)
        return base64.b64encode(mac.digest()).decode()

    def ticker_price(self, pair: str) -> Decimal:
        result = self.public("Ticker", {"pair": pair})
        market = next(iter(result.values()))
        return Decimal(market["c"][0])

    def ticker(self, pair: str) -> Ticker:
        result = self.public("Ticker", {"pair": pair})
        market = next(iter(result.values()))
        bid = Decimal(market["b"][0])
        ask = Decimal(market["a"][0])
        last = Decimal(market["c"][0])
        midpoint = (bid + ask) / Decimal("2")
        spread_bps = ((ask - bid) / midpoint * Decimal("10000")) if midpoint > 0 else Decimal("999999")
        return Ticker(
            bid=bid,
            ask=ask,
            last=last,
            spread_bps=spread_bps,
            change_pct=((last - Decimal(market["o"])) / Decimal(market["o"]) * Decimal("100"))
            if Decimal(market["o"]) > 0
            else Decimal("0"),
            quote_volume=Decimal(market.get("v", ["0", "0"])[1]) * last,
        )

    def candles(self, pair: str, interval_minutes: int) -> list[Candle]:
        result = self.public("OHLC", {"pair": pair, "interval": interval_minutes})
        key = next(k for k in result.keys() if k != "last")
        candles = []
        for row in result[key]:
            candles.append(
                Candle(
                    timestamp=int(row[0]),
                    open=Decimal(row[1]),
                    high=Decimal(row[2]),
                    low=Decimal(row[3]),
                    close=Decimal(row[4]),
                    volume=Decimal(row[6]),
                )
            )
        return candles

    def asset_pair_rules(self, pair: str) -> dict[str, Any]:
        result = self.public("AssetPairs", {"pair": pair})
        return next(iter(result.values()))

    def asset_pairs(self) -> dict[str, Any]:
        return self.public("AssetPairs")

    def balances(self) -> dict[str, Decimal]:
        result = self.private("Balance")
        return {asset: Decimal(amount) for asset, amount in result.items()}

    def add_market_order(
        self,
        pair: str,
        side: Literal["buy", "sell"],
        volume: Decimal,
        validate_only: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pair": pair,
            "type": side,
            "ordertype": "market",
            "volume": format_decimal(volume),
        }
        if validate_only:
            payload["validate"] = "true"
        return self.private("AddOrder", payload)


def load_config(path: Path) -> BotConfig:
    raw = json.loads(path.read_text())
    base_dir = path.parent
    return BotConfig(
        raw=raw,
        exchange=raw["exchange"],
        pair=raw["pair"],
        ws_symbol=raw.get("ws_symbol", raw["pair"]),
        base_asset=raw["base_asset"],
        quote_asset=raw["quote_asset"],
        mode=raw["mode"],
        poll_seconds=int(raw["poll_seconds"]),
        candle_interval_minutes=int(raw["candle_interval_minutes"]),
        strategy=StrategyConfig(
            fast_sma_period=int(raw["strategy"]["fast_sma_period"]),
            slow_sma_period=int(raw["strategy"]["slow_sma_period"]),
            rsi_period=int(raw["strategy"]["rsi_period"]),
            buy_rsi_max=float(raw["strategy"]["buy_rsi_max"]),
            sell_rsi_min=float(raw["strategy"]["sell_rsi_min"]),
            ema_trend_period=int(raw["strategy"].get("ema_trend_period", raw["strategy"]["slow_sma_period"])),
            macd_fast_period=int(raw["strategy"].get("macd_fast_period", 12)),
            macd_slow_period=int(raw["strategy"].get("macd_slow_period", 26)),
            macd_signal_period=int(raw["strategy"].get("macd_signal_period", 9)),
            bollinger_period=int(raw["strategy"].get("bollinger_period", 20)),
            bollinger_stddev=float(raw["strategy"].get("bollinger_stddev", 2.0)),
            min_buy_score=int(raw["strategy"].get("min_buy_score", 3)),
            min_sell_score=int(raw["strategy"].get("min_sell_score", 3)),
            min_buy_momentum_pct=Decimal(str(raw["strategy"].get("min_buy_momentum_pct", "0"))),
        ),
        risk=RiskConfig(
            starting_paper_quote=Decimal(str(raw["risk"]["starting_paper_quote"])),
            max_order_quote=Decimal(str(raw["risk"]["max_order_quote"])),
            max_position_quote=Decimal(str(raw["risk"]["max_position_quote"])),
            risk_per_trade_quote=Decimal(str(raw["risk"].get("risk_per_trade_quote", "1"))),
            reserve_quote_balance=Decimal(str(raw["risk"].get("reserve_quote_balance", "0"))),
            daily_loss_limit_quote=Decimal(str(raw["risk"]["daily_loss_limit_quote"])),
            stop_loss_pct=Decimal(str(raw["risk"]["stop_loss_pct"])),
            take_profit_pct=Decimal(str(raw["risk"]["take_profit_pct"])),
            cooldown_minutes=int(raw["risk"]["cooldown_minutes"]),
            max_trades_per_day=int(raw["risk"]["max_trades_per_day"]),
            fee_bps=Decimal(str(raw["risk"].get("fee_bps", "26"))),
            slippage_bps=Decimal(str(raw["risk"].get("slippage_bps", "10"))),
            min_net_profit_bps=Decimal(str(raw["risk"].get("min_net_profit_bps", "0"))),
            max_daily_equity_drawdown_quote=Decimal(str(raw["risk"].get("max_daily_equity_drawdown_quote", "5"))),
            max_high_water_drawdown_quote=Decimal(str(raw["risk"].get("max_high_water_drawdown_quote", "4"))),
        ),
        market_filters=MarketFilterConfig(
            max_spread_bps=Decimal(str(raw.get("market_filters", {}).get("max_spread_bps", "50"))),
            max_candle_range_pct=Decimal(str(raw.get("market_filters", {}).get("max_candle_range_pct", "5"))),
            max_atr_pct=Decimal(str(raw.get("market_filters", {}).get("max_atr_pct", "3"))),
            atr_period=int(raw.get("market_filters", {}).get("atr_period", 14)),
        ),
        market_radar=MarketRadarConfig(
            enabled=bool(raw.get("market_radar", {}).get("enabled", True)),
            pairs=list(raw.get("market_radar", {}).get("pairs", raw.get("scan_pairs", [raw["pair"]]))),
            context_pairs=list(raw.get("market_radar", {}).get("context_pairs", [])),
            max_pairs_per_cycle=int(raw.get("market_radar", {}).get("max_pairs_per_cycle", 24)),
            min_positive_breadth_to_increase=Decimal(str(raw.get("market_radar", {}).get("min_positive_breadth_to_increase", "0.55"))),
            block_buy_breadth_below=Decimal(str(raw.get("market_radar", {}).get("block_buy_breadth_below", "0.25"))),
        ),
        advanced_strategy=AdvancedStrategyConfig(
            enabled=bool(raw.get("advanced_strategy", {}).get("enabled", True)),
            turtle_breakout_period=int(raw.get("advanced_strategy", {}).get("turtle_breakout_period", 20)),
            turtle_soup_period=int(raw.get("advanced_strategy", {}).get("turtle_soup_period", 20)),
            supertrend_period=int(raw.get("advanced_strategy", {}).get("supertrend_period", 10)),
            supertrend_multiplier=Decimal(str(raw.get("advanced_strategy", {}).get("supertrend_multiplier", "3"))),
            var_lookback=int(raw.get("advanced_strategy", {}).get("var_lookback", 48)),
            max_var_pct=Decimal(str(raw.get("advanced_strategy", {}).get("max_var_pct", "3"))),
            max_es_pct=Decimal(str(raw.get("advanced_strategy", {}).get("max_es_pct", "5"))),
            kelly_fraction=Decimal(str(raw.get("advanced_strategy", {}).get("kelly_fraction", "0.25"))),
            min_kelly_multiplier=Decimal(str(raw.get("advanced_strategy", {}).get("min_kelly_multiplier", "0.4"))),
            max_kelly_multiplier=Decimal(str(raw.get("advanced_strategy", {}).get("max_kelly_multiplier", "1.2"))),
        ),
        llm=LLMConfig(
            enabled=bool(raw.get("llm", {}).get("enabled", False)),
            model=str(raw.get("llm", {}).get("model", "gpt-5.4-mini")),
            veto_risk_score=int(raw.get("llm", {}).get("veto_risk_score", 80)),
            max_calls_per_day=int(raw.get("llm", {}).get("max_calls_per_day", 6)),
            block_when_unavailable=bool(raw.get("llm", {}).get("block_when_unavailable", False)),
        ),
        ai_plan=AIPlanConfig(
            enabled=bool(raw.get("ai_plan", {}).get("enabled", False)),
            model=str(raw.get("ai_plan", {}).get("model", "gpt-5.5")),
            model_fallbacks=list(raw.get("ai_plan", {}).get("model_fallbacks", [])),
            authority_mode=str(raw.get("ai_plan", {}).get("authority_mode", "advisory")),
            reasoning_effort=str(raw.get("ai_plan", {}).get("reasoning_effort", "medium")),
            reasoning_summary=str(raw.get("ai_plan", {}).get("reasoning_summary", "concise")),
            web_search=bool(raw.get("ai_plan", {}).get("web_search", True)),
            force_web_search=bool(raw.get("ai_plan", {}).get("force_web_search", False)),
            max_web_search_calls=int(raw.get("ai_plan", {}).get("max_web_search_calls", 1)),
            web_search_context_size=str(raw.get("ai_plan", {}).get("web_search_context_size", "low")),
            max_output_tokens=int(raw.get("ai_plan", {}).get("max_output_tokens", 1200)),
            refresh_interval_minutes=int(raw.get("ai_plan", {}).get("refresh_interval_minutes", 30)),
            max_calls_per_day=int(raw.get("ai_plan", {}).get("max_calls_per_day", 4)),
            allow_risk_increase=bool(raw.get("ai_plan", {}).get("allow_risk_increase", False)),
            allow_decision_override=bool(raw.get("ai_plan", {}).get("allow_decision_override", False)),
            override_min_confidence=int(raw.get("ai_plan", {}).get("override_min_confidence", 88)),
            override_min_signal_score=int(raw.get("ai_plan", {}).get("override_min_signal_score", 0)),
            override_max_research_score=int(raw.get("ai_plan", {}).get("override_max_research_score", 90)),
            override_min_positive_breadth=Decimal(str(raw.get("ai_plan", {}).get("override_min_positive_breadth", "0.55"))),
            max_risk_multiplier=Decimal(str(raw.get("ai_plan", {}).get("max_risk_multiplier", "1"))),
            max_aggressive_order_quote=Decimal(
                str(raw.get("ai_plan", {}).get("max_aggressive_order_quote", raw["risk"].get("max_order_quote", "10")))
            ),
            min_confidence_to_increase=int(raw.get("ai_plan", {}).get("min_confidence_to_increase", 75)),
            min_signal_score_to_increase=int(raw.get("ai_plan", {}).get("min_signal_score_to_increase", 4)),
            min_regime_score_to_increase=int(raw.get("ai_plan", {}).get("min_regime_score_to_increase", 4)),
            max_research_score_to_increase=int(raw.get("ai_plan", {}).get("max_research_score_to_increase", 55)),
            max_spread_bps_to_increase=Decimal(str(raw.get("ai_plan", {}).get("max_spread_bps_to_increase", "12"))),
            block_when_unavailable=bool(raw.get("ai_plan", {}).get("block_when_unavailable", False)),
            history_limit=int(raw.get("ai_plan", {}).get("history_limit", 500)),
        ),
        execution=ExecutionConfig(
            approval_mode=str(raw.get("execution", {}).get("approval_mode", "aggressive_only")),
            aggressive_buy_score_max=int(raw.get("execution", {}).get("aggressive_buy_score_max", raw["strategy"].get("min_buy_score", 3))),
            approval_ttl_minutes=int(raw.get("execution", {}).get("approval_ttl_minutes", 30)),
            max_cycle_gap_seconds=int(raw.get("execution", {}).get("max_cycle_gap_seconds", int(raw["poll_seconds"]) * 3)),
            reconnect_observe_cycles=int(raw.get("execution", {}).get("reconnect_observe_cycles", 1)),
            trading_windows_utc=list(raw.get("execution", {}).get("trading_windows_utc", [])),
            notify_on_signal=bool(raw.get("execution", {}).get("notify_on_signal", True)),
            notify_on_fill=bool(raw.get("execution", {}).get("notify_on_fill", True)),
            min_trade_interval_seconds=int(
                raw.get("execution", {}).get("min_trade_interval_seconds", int(raw["candle_interval_minutes"]) * 60)
            ),
        ),
        multi_timeframe=MultiTimeframeConfig(
            enabled=bool(raw.get("multi_timeframe", {}).get("enabled", True)),
            interval_minutes=int(raw.get("multi_timeframe", {}).get("interval_minutes", 15)),
            min_buy_score=int(raw.get("multi_timeframe", {}).get("min_buy_score", 2)),
        ),
        shadow=ShadowConfig(
            enabled=bool(raw.get("shadow", {}).get("enabled", True)),
            starting_quote=Decimal(str(raw.get("shadow", {}).get("starting_quote", raw["risk"].get("starting_paper_quote", "50")))),
        ),
        downside=DownsideConfig(
            enabled=bool(raw.get("downside", {}).get("enabled", True)),
            min_short_score=int(raw.get("downside", {}).get("min_short_score", raw["strategy"].get("min_sell_score", 3))),
            min_short_momentum_pct=Decimal(str(raw.get("downside", {}).get("min_short_momentum_pct", "0.05"))),
            close_short_reversal_score=int(raw.get("downside", {}).get("close_short_reversal_score", 2)),
            require_higher_timeframe_downtrend=bool(raw.get("downside", {}).get("require_higher_timeframe_downtrend", True)),
            block_buys_when_downside=bool(raw.get("downside", {}).get("block_buys_when_downside", True)),
            shadow_short_enabled=bool(raw.get("downside", {}).get("shadow_short_enabled", True)),
            live_short_enabled=bool(raw.get("downside", {}).get("live_short_enabled", False)),
        ),
        scan_pairs=list(raw.get("scan_pairs", [raw["pair"]])),
        state_file=base_dir / raw["paths"]["state_file"],
        trade_log=base_dir / raw["paths"]["trade_log"],
        pending_order_file=base_dir / raw["paths"].get("pending_order", "pending_order.json"),
        ledger_db=base_dir / raw["paths"].get("ledger_db", "ledger.sqlite3"),
        notification_outbox_file=base_dir / raw["paths"].get("notification_outbox", "notification_outbox.jsonl"),
        research_cache_file=base_dir / raw["paths"].get("research_cache", "research_snapshot.json"),
        ai_plan_file=base_dir / raw["paths"].get("ai_plan", "ai_plan.json"),
        ai_plan_log_file=base_dir / raw["paths"].get("ai_plan_log", "ai_plan_log.jsonl"),
        shadow_state_file=base_dir / raw["paths"].get("shadow_state", "shadow_state.json"),
        shadow_log_file=base_dir / raw["paths"].get("shadow_log", "shadow_trades.jsonl"),
    )


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_state(config: BotConfig) -> dict[str, Any]:
    if config.state_file.exists():
        return json.loads(config.state_file.read_text())
    return new_paper_state(config.risk.starting_paper_quote)


def new_paper_state(starting_quote: Decimal) -> dict[str, Any]:
    return {
        "paper_quote_balance": str(starting_quote),
        "paper_base_balance": "0",
        "avg_entry_price": None,
        "last_trade_at": None,
        "last_cycle_started_at": None,
        "reconnect_observe_cycles_left": 0,
        "daily_report_sent_day": None,
        "llm_calls_today": 0,
        "llm_day": today_key(),
        "ai_plan_calls_today": 0,
        "ai_plan_day": today_key(),
        "active_pair": None,
        "realized_pnl_today": "0",
        "consecutive_loss_trades": 0,
        "protection_global_lock_until": None,
        "protection_global_lock_reason": None,
        "protection_pair_locks": {},
        "trade_day": today_key(),
        "trades_today": 0,
    }


def save_state(config: BotConfig, state: dict[str, Any]) -> None:
    config.state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def load_shadow_state(config: BotConfig) -> dict[str, Any]:
    if config.shadow_state_file.exists():
        state = json.loads(config.shadow_state_file.read_text())
        ensure_shadow_short_state(config, state)
        return state
    state = new_paper_state(config.shadow.starting_quote)
    state["shadow_started_at"] = datetime.now(timezone.utc).isoformat()
    ensure_shadow_short_state(config, state)
    return state


def ensure_shadow_short_state(config: BotConfig, state: dict[str, Any]) -> None:
    state.setdefault("short_collateral_quote_balance", str(config.shadow.starting_quote))
    state.setdefault("paper_short_base_balance", "0")
    state.setdefault("short_avg_entry_price", None)
    state.setdefault("short_open_fee_quote", "0")
    state.setdefault("short_realized_pnl_today", "0")
    state.setdefault("short_trade_day", today_key())
    state.setdefault("short_trades_today", 0)


def reset_shadow_pair_if_needed(config: BotConfig, state: dict[str, Any]) -> bool:
    active_pair = state.get("active_pair")
    if active_pair in (None, config.pair):
        state["active_pair"] = config.pair
        return False

    state["shadow_previous_pair"] = active_pair
    state["shadow_pair_reset_at"] = datetime.now(timezone.utc).isoformat()
    state["active_pair"] = config.pair
    state["paper_quote_balance"] = str(config.shadow.starting_quote)
    state["paper_base_balance"] = "0"
    state["avg_entry_price"] = None
    state["last_trade_at"] = None
    state["realized_pnl_today"] = "0"
    state["trade_day"] = today_key()
    state["trades_today"] = 0
    state["short_collateral_quote_balance"] = str(config.shadow.starting_quote)
    state["paper_short_base_balance"] = "0"
    state["short_avg_entry_price"] = None
    state["short_open_fee_quote"] = "0"
    state["short_realized_pnl_today"] = "0"
    state["short_trade_day"] = today_key()
    state["short_trades_today"] = 0
    return True


def reset_shadow_short_daily_if_needed(state: dict[str, Any]) -> None:
    current = today_key()
    if state.get("short_trade_day") != current:
        state["short_trade_day"] = current
        state["short_trades_today"] = 0
        state["short_realized_pnl_today"] = "0"


def save_shadow_state(config: BotConfig, state: dict[str, Any]) -> None:
    config.shadow_state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def log_shadow_event(config: BotConfig, event: dict[str, Any]) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with config.shadow_log_file.open("a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")


def log_event(config: BotConfig, event: dict[str, Any]) -> None:
    event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
    with config.trade_log.open("a") as fh:
        fh.write(json.dumps(event, sort_keys=True) + "\n")
    record_event(config.ledger_db, event)


def notify(title: str, message: str) -> None:
    script = 'display notification "{message}" with title "{title}"'.format(
        title=title.replace("\\", "\\\\").replace('"', '\\"'),
        message=message.replace("\\", "\\\\").replace('"', '\\"'),
    )
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def write_pending_order(config: BotConfig, event: dict[str, Any], order_plan: dict[str, Any]) -> dict[str, Any]:
    pending = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(
            time.time() + config.execution.approval_ttl_minutes * 60,
            tz=timezone.utc,
        ).isoformat(),
        "status": "pending",
        "pair": config.pair,
        "signal": event["signal"],
        "signal_reason": event["signal_reason"],
        "price": event["price"],
        "score": event.get("score"),
        "risk": event.get("risk"),
        "market_metrics": event.get("market_metrics", {}),
        "order_plan": order_plan,
    }
    config.pending_order_file.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n")
    return pending


def read_pending_order(config: BotConfig) -> dict[str, Any] | None:
    if not config.pending_order_file.exists():
        return None
    return json.loads(config.pending_order_file.read_text())


def pending_is_active(pending: dict[str, Any] | None) -> bool:
    if not pending or pending.get("status") != "pending":
        return False
    expires_at = datetime.fromisoformat(pending["expires_at"])
    return datetime.now(timezone.utc) < expires_at


def requires_manual_approval(config: BotConfig, signal: Signal, order_plan: dict[str, Any]) -> bool:
    if order_plan.get("status") != "planned":
        return False
    if config.execution.approval_mode == "always":
        return True
    if config.execution.approval_mode == "never":
        return False
    if config.execution.approval_mode != "aggressive_only":
        return True
    if signal.side == "buy" and signal.score <= config.execution.aggressive_buy_score_max:
        return True
    return False


def mark_cycle_start(config: BotConfig, state: dict[str, Any]) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    last_started_raw = state.get("last_cycle_started_at")
    state["last_cycle_started_at"] = now.isoformat()
    if not last_started_raw:
        return False, "first_cycle"
    last_started = datetime.fromisoformat(last_started_raw)
    gap_seconds = (now - last_started).total_seconds()
    if gap_seconds > config.execution.max_cycle_gap_seconds:
        state["reconnect_observe_cycles_left"] = max(
            int(state.get("reconnect_observe_cycles_left", 0)),
            config.execution.reconnect_observe_cycles,
        )
        return True, f"cycle_gap_{int(gap_seconds)}s"
    return False, "normal_cycle"


def consume_reconnect_observe_cycle(state: dict[str, Any]) -> bool:
    cycles_left = int(state.get("reconnect_observe_cycles_left", 0))
    if cycles_left <= 0:
        return False
    state["reconnect_observe_cycles_left"] = cycles_left - 1
    return True


def within_trading_window(config: BotConfig, now: datetime | None = None) -> bool:
    if not config.execution.trading_windows_utc:
        return True
    now = now or datetime.now(timezone.utc)
    current_minutes = now.hour * 60 + now.minute
    for window in config.execution.trading_windows_utc:
        start = parse_hhmm(window["start"])
        end = parse_hhmm(window["end"])
        if start <= end and start <= current_minutes < end:
            return True
        if start > end and (current_minutes >= start or current_minutes < end):
            return True
    return False


def sprint_goal_context(
    config: BotConfig,
    pnl_snapshot: dict[str, str],
    now: datetime | None = None,
) -> dict[str, Any]:
    raw = config.raw.get("sprint_goal", {})
    if not isinstance(raw, dict) or not raw.get("enabled", False):
        return {"enabled": False}

    now = now or datetime.now(timezone.utc)
    current_equity = Decimal(str(pnl_snapshot.get("equity_quote", "0")))
    target_equity = Decimal(str(raw.get("target_equity_quote", "0")))
    start_equity = Decimal(str(raw.get("start_equity_quote", current_equity)))
    remaining = max(target_equity - current_equity, Decimal("0"))
    gained = current_equity - start_equity
    total_needed = target_equity - start_equity
    progress_pct = Decimal("100") if total_needed <= 0 and current_equity >= target_equity else Decimal("0")
    if total_needed > 0:
        progress_pct = max(Decimal("0"), min(Decimal("100"), gained / total_needed * Decimal("100")))

    deadline = str(raw.get("deadline_local_date") or "")
    days_left: int | None = None
    required_daily_profit: Decimal | None = None
    if deadline:
        try:
            deadline_date = datetime.strptime(deadline, "%Y-%m-%d").date()
            days_left = max((deadline_date - now.date()).days, 0)
            required_daily_profit = remaining / Decimal(max(days_left, 1))
        except Exception:
            days_left = None
            required_daily_profit = None

    return {
        "enabled": True,
        "label": str(raw.get("label") or "sprint_goal"),
        "policy": str(raw.get("policy") or raw.get("aggressive_policy") or "aggressive_fee_edge_gated"),
        "start_equity_quote": str(start_equity),
        "target_equity_quote": str(target_equity),
        "current_equity_quote": str(current_equity),
        "remaining_quote": str(remaining),
        "gained_since_start_quote": str(gained),
        "progress_pct": str(progress_pct.quantize(Decimal("0.01"))),
        "deadline_local_date": deadline,
        "days_left": days_left,
        "required_daily_profit_quote": str(required_daily_profit) if required_daily_profit is not None else None,
        "target_reached": bool(target_equity > 0 and current_equity >= target_equity),
        "stop_on_target": bool(raw.get("stop_on_target", True)),
    }


def parse_hhmm(value: str) -> int:
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


def maybe_send_daily_report(config: BotConfig, state: dict[str, Any]) -> None:
    report_time = config.raw.get("notifications", {}).get("daily_report_local_time")
    if not report_time:
        return
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    if state.get("daily_report_sent_day") == today:
        return
    if now.strftime("%H:%M") < report_time:
        return
    notify_channels(config, "Kraken交易日报", format_daily_summary(daily_summary(config.ledger_db, mode=config.mode)), financial=True)
    state["daily_report_sent_day"] = today


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def reset_daily_if_needed(state: dict[str, Any]) -> None:
    current = today_key()
    if state.get("trade_day") != current:
        state["trade_day"] = current
        state["trades_today"] = 0
        state["realized_pnl_today"] = "0"
        state["consecutive_loss_trades"] = 0
        state["protection_global_lock_until"] = None
        state["protection_global_lock_reason"] = None
        state["protection_pair_locks"] = {}
        state["daily_report_sent_day"] = None
    if state.get("llm_day") != current:
        state["llm_day"] = current
        state["llm_calls_today"] = 0
    if state.get("ai_plan_day") != current:
        state["ai_plan_day"] = current
        state["ai_plan_calls_today"] = 0


def sma(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period:
        raise BotError(f"Need at least {period} candles, got {len(values)}")
    return sum(values[-period:]) / Decimal(period)


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        raise BotError(f"Need at least {period} values for EMA, got {len(values)}")
    multiplier = Decimal("2") / Decimal(period + 1)
    output = [sum(values[:period]) / Decimal(period)]
    for value in values[period:]:
        output.append((value - output[-1]) * multiplier + output[-1])
    return output


def macd_histogram(values: list[Decimal], fast_period: int, slow_period: int, signal_period: int) -> Decimal:
    needed = slow_period + signal_period
    if len(values) < needed:
        raise BotError(f"Need at least {needed} values for MACD, got {len(values)}")
    fast = ema_series(values, fast_period)
    slow = ema_series(values, slow_period)
    aligned_fast = fast[-len(slow) :]
    macd_line = [fast_value - slow_value for fast_value, slow_value in zip(aligned_fast, slow)]
    signal = ema_series(macd_line, signal_period)
    return macd_line[-1] - signal[-1]


def bollinger_zscore(values: list[Decimal], period: int, stddev_multiplier: float) -> Decimal:
    if len(values) < period:
        raise BotError(f"Need at least {period} values for Bollinger score, got {len(values)}")
    window = values[-period:]
    mean = sum(window) / Decimal(period)
    variance = sum((value - mean) ** 2 for value in window) / Decimal(period)
    stddev = variance.sqrt()
    band_width = stddev * Decimal(str(stddev_multiplier))
    if band_width == 0:
        return Decimal("0")
    return (window[-1] - mean) / band_width


def percentage_momentum(values: list[Decimal], lookback: int) -> Decimal:
    if len(values) <= lookback:
        raise BotError(f"Need more than {lookback} values for momentum, got {len(values)}")
    previous = values[-lookback - 1]
    if previous == 0:
        return Decimal("0")
    return (values[-1] - previous) / previous * Decimal("100")


def rsi(values: list[Decimal], period: int) -> Decimal:
    if len(values) < period + 1:
        raise BotError(f"Need at least {period + 1} candles for RSI, got {len(values)}")
    gains = Decimal("0")
    losses = Decimal("0")
    window = values[-(period + 1) :]
    for previous, current in zip(window, window[1:]):
        change = current - previous
        if change >= 0:
            gains += change
        else:
            losses += abs(change)
    if losses == 0:
        return Decimal("100")
    rs = gains / losses
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def atr_pct(candles: list[Candle], period: int) -> Decimal:
    committed = candles[:-1]
    if len(committed) < period:
        raise BotError(f"Need at least {period} candles for ATR, got {len(committed)}")
    ranges = [c.high - c.low for c in committed[-period:]]
    last_close = committed[-1].close
    if last_close <= 0:
        return Decimal("999999")
    return (sum(ranges) / Decimal(period)) / last_close * Decimal("100")


def true_ranges(candles: list[Candle]) -> list[Decimal]:
    committed = candles[:-1]
    ranges: list[Decimal] = []
    for index, candle in enumerate(committed):
        if index == 0:
            ranges.append(candle.high - candle.low)
            continue
        previous_close = committed[index - 1].close
        ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
    return ranges


def average_true_range(candles: list[Candle], period: int) -> Decimal:
    ranges = true_ranges(candles)
    if len(ranges) < period:
        raise BotError(f"Need at least {period} true ranges, got {len(ranges)}")
    return sum(ranges[-period:]) / Decimal(period)


def return_loss_stats_pct(values: list[Decimal], lookback: int) -> dict[str, str]:
    window = values[-(lookback + 1) :] if lookback > 0 else values
    returns: list[Decimal] = []
    for previous, current in zip(window, window[1:]):
        if previous > 0:
            returns.append((current - previous) / previous * Decimal("100"))
    losses = sorted([-ret for ret in returns if ret < 0])
    wins = [ret for ret in returns if ret > 0]
    if not returns or not losses:
        return {
            "var_pct": "0",
            "es_pct": "0",
            "win_rate": str(Decimal(len(wins)) / Decimal(len(returns))) if returns else "0",
            "avg_win_pct": str((sum(wins) / Decimal(len(wins))) if wins else Decimal("0")),
            "avg_loss_pct": "0",
            "kelly_raw": "0",
        }
    var_index = min(max(int(Decimal(len(losses)) * Decimal("0.95")) - 1, 0), len(losses) - 1)
    var = losses[var_index]
    tail = losses[var_index:] or [var]
    es = sum(tail) / Decimal(len(tail))
    avg_win = (sum(wins) / Decimal(len(wins))) if wins else Decimal("0")
    avg_loss = sum(losses) / Decimal(len(losses))
    win_rate = Decimal(len(wins)) / Decimal(len(returns))
    loss_rate = Decimal("1") - win_rate
    payoff = avg_win / avg_loss if avg_loss > 0 else Decimal("0")
    kelly_raw = (win_rate - (loss_rate / payoff)) if payoff > 0 else Decimal("0")
    return {
        "var_pct": str(var),
        "es_pct": str(es),
        "win_rate": str(win_rate),
        "avg_win_pct": str(avg_win),
        "avg_loss_pct": str(avg_loss),
        "kelly_raw": str(kelly_raw),
    }


def advanced_strategy_metrics(config: BotConfig, candles: list[Candle]) -> dict[str, Any]:
    if not config.advanced_strategy.enabled:
        return {"enabled": False}
    committed = candles[:-1]
    closes = [c.close for c in committed]
    metrics: dict[str, Any] = {
        "enabled": True,
        "turtle": {"signal": "neutral"},
        "supertrend": {"trend": "neutral"},
        "risk_model": {},
        "score_adjustment": 0,
        "reasons": [],
    }
    reasons: list[str] = []
    score_adjustment = 0

    turtle_period = config.advanced_strategy.turtle_breakout_period
    if len(committed) > turtle_period:
        last = committed[-1]
        prior = committed[-(turtle_period + 1) : -1]
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        turtle_signal = "neutral"
        if last.close > prior_high:
            turtle_signal = "breakout_up"
            score_adjustment += 2
            reasons.append("turtle_breakout_up")
        elif last.close < prior_low:
            turtle_signal = "breakout_down"
            score_adjustment -= 2
            reasons.append("turtle_breakout_down")
        metrics["turtle"] = {
            "signal": turtle_signal,
            "period": turtle_period,
            "prior_high": str(prior_high),
            "prior_low": str(prior_low),
        }

    soup_period = config.advanced_strategy.turtle_soup_period
    if len(committed) > soup_period:
        last = committed[-1]
        prior = committed[-(soup_period + 1) : -1]
        prior_high = max(c.high for c in prior)
        prior_low = min(c.low for c in prior)
        soup_signal = "none"
        if last.high > prior_high and last.close < prior_high:
            soup_signal = "failed_breakout_down"
            score_adjustment -= 1
            reasons.append("turtle_soup_failed_breakout")
        elif last.low < prior_low and last.close > prior_low:
            soup_signal = "failed_breakdown_up"
            score_adjustment += 1
            reasons.append("turtle_soup_failed_breakdown")
        metrics["turtle_soup"] = {
            "signal": soup_signal,
            "period": soup_period,
            "prior_high": str(prior_high),
            "prior_low": str(prior_low),
        }

    try:
        atr = average_true_range(candles, config.advanced_strategy.supertrend_period)
        last = committed[-1]
        hl2 = (last.high + last.low) / Decimal("2")
        upper = hl2 + config.advanced_strategy.supertrend_multiplier * atr
        lower = hl2 - config.advanced_strategy.supertrend_multiplier * atr
        trend_ema = ema_series(closes, max(config.strategy.ema_trend_period, config.advanced_strategy.supertrend_period))[-1]
        if last.close >= trend_ema and last.close > lower:
            trend = "up"
            score_adjustment += 1
            reasons.append("supertrend_up")
        elif last.close <= trend_ema and last.close < upper:
            trend = "down"
            score_adjustment -= 1
            reasons.append("supertrend_down")
        else:
            trend = "neutral"
        metrics["supertrend"] = {
            "trend": trend,
            "period": config.advanced_strategy.supertrend_period,
            "multiplier": str(config.advanced_strategy.supertrend_multiplier),
            "upper": str(upper),
            "lower": str(lower),
            "atr": str(atr),
        }
    except Exception as exc:
        metrics["supertrend"] = {"trend": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}

    risk = return_loss_stats_pct(closes, config.advanced_strategy.var_lookback)
    var_pct = Decimal(str(risk.get("var_pct", "0")))
    es_pct = Decimal(str(risk.get("es_pct", "0")))
    kelly_raw = Decimal(str(risk.get("kelly_raw", "0")))
    if kelly_raw <= 0:
        kelly_multiplier = config.advanced_strategy.min_kelly_multiplier
    else:
        kelly_multiplier = min(
            config.advanced_strategy.max_kelly_multiplier,
            max(config.advanced_strategy.min_kelly_multiplier, Decimal("1") + kelly_raw * config.advanced_strategy.kelly_fraction),
        )
    risk_block = var_pct > config.advanced_strategy.max_var_pct or es_pct > config.advanced_strategy.max_es_pct
    if risk_block:
        score_adjustment -= 1
        reasons.append("var_es_high")
    risk.update(
        {
            "lookback": config.advanced_strategy.var_lookback,
            "max_var_pct": str(config.advanced_strategy.max_var_pct),
            "max_es_pct": str(config.advanced_strategy.max_es_pct),
            "risk_block": risk_block,
            "kelly_multiplier": str(kelly_multiplier),
        }
    )
    metrics["risk_model"] = risk
    metrics["score_adjustment"] = score_adjustment
    metrics["reasons"] = reasons
    return metrics


def last_candle_range_pct(candles: list[Candle]) -> Decimal:
    committed = candles[:-1]
    if not committed:
        return Decimal("999999")
    last = committed[-1]
    if last.close <= 0:
        return Decimal("999999")
    return (last.high - last.low) / last.close * Decimal("100")


def make_signal(config: BotConfig, candles: list[Candle], position_base: Decimal, avg_entry: Decimal | None) -> Signal:
    closes = [c.close for c in candles[:-1]]
    price = closes[-1]
    fast = sma(closes, config.strategy.fast_sma_period)
    slow = sma(closes, config.strategy.slow_sma_period)
    trend = ema_series(closes, config.strategy.ema_trend_period)[-1]
    current_rsi = rsi(closes, config.strategy.rsi_period)
    macd_hist = macd_histogram(
        closes,
        config.strategy.macd_fast_period,
        config.strategy.macd_slow_period,
        config.strategy.macd_signal_period,
    )
    bollinger_z = bollinger_zscore(closes, config.strategy.bollinger_period, config.strategy.bollinger_stddev)
    momentum = percentage_momentum(closes, config.strategy.fast_sma_period)
    score, reasons = score_market(config, price, fast, slow, trend, current_rsi, macd_hist, bollinger_z, momentum)
    advanced = advanced_strategy_metrics(config, candles)
    score += int(advanced.get("score_adjustment") or 0)
    reasons.extend(str(reason) for reason in advanced.get("reasons", []))

    if position_base > 0 and avg_entry:
        stop_price = avg_entry * (Decimal("1") - config.risk.stop_loss_pct)
        target_price = avg_entry * (Decimal("1") + max(config.risk.take_profit_pct, min_net_profit_pct(config)))
        if price <= stop_price:
            return Signal("sell", "stop_loss", price, fast, slow, current_rsi, score, macd_hist, bollinger_z, momentum)
        if avg_entry > 0 and price > avg_entry:
            gross_profit_pct = (price - avg_entry) / avg_entry
            if gross_profit_pct < min_net_profit_pct(config):
                return Signal(
                    "hold",
                    "hold_for_min_net_profit",
                    price,
                    fast,
                    slow,
                    current_rsi,
                    score,
                    macd_hist,
                    bollinger_z,
                    momentum,
                )
        if price >= target_price:
            return Signal("sell", "take_profit", price, fast, slow, current_rsi, score, macd_hist, bollinger_z, momentum)

    if (
        position_base <= 0
        and score >= config.strategy.min_buy_score
        and current_rsi <= Decimal(str(config.strategy.buy_rsi_max))
    ):
        if momentum < config.strategy.min_buy_momentum_pct:
            return Signal(
                "hold",
                f"buy_filter_min_momentum:{momentum}<{config.strategy.min_buy_momentum_pct}",
                price,
                fast,
                slow,
                current_rsi,
                score,
                macd_hist,
                bollinger_z,
                momentum,
            )
        return Signal("buy", "score_buy:" + ",".join(reasons), price, fast, slow, current_rsi, score, macd_hist, bollinger_z, momentum)

    if (
        position_base > 0
        and score <= -config.strategy.min_sell_score
        and current_rsi >= Decimal(str(config.strategy.sell_rsi_min))
    ):
        return Signal("sell", "score_sell:" + ",".join(reasons), price, fast, slow, current_rsi, score, macd_hist, bollinger_z, momentum)

    return Signal("hold", "no_edge:" + ",".join(reasons), price, fast, slow, current_rsi, score, macd_hist, bollinger_z, momentum)


def score_market(
    config: BotConfig,
    price: Decimal,
    fast: Decimal,
    slow: Decimal,
    trend: Decimal,
    current_rsi: Decimal,
    macd_hist: Decimal,
    bollinger_z: Decimal,
    momentum: Decimal,
) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []

    if fast > slow:
        score += 1
        reasons.append("fast_above_slow")
    else:
        score -= 1
        reasons.append("fast_below_slow")

    if price > trend:
        score += 1
        reasons.append("above_trend_ema")
    else:
        score -= 1
        reasons.append("below_trend_ema")

    if macd_hist > 0:
        score += 1
        reasons.append("macd_positive")
    elif macd_hist < 0:
        score -= 1
        reasons.append("macd_negative")

    if Decimal("35") <= current_rsi <= Decimal(str(config.strategy.buy_rsi_max)):
        score += 1
        reasons.append("rsi_constructive")
    elif current_rsi > Decimal("75"):
        score -= 1
        reasons.append("rsi_hot")
    elif current_rsi < Decimal("25"):
        score -= 1
        reasons.append("rsi_weak")

    if Decimal("-1.2") <= bollinger_z <= Decimal("0.7"):
        score += 1
        reasons.append("bollinger_reasonable")
    elif bollinger_z > Decimal("1.2"):
        score -= 1
        reasons.append("bollinger_extended")

    if momentum > 0:
        score += 1
        reasons.append("momentum_positive")
    elif momentum < 0:
        score -= 1
        reasons.append("momentum_negative")

    return score, reasons


def market_regime(config: BotConfig, candles: list[Candle]) -> dict[str, Any]:
    closes = [c.close for c in candles[:-1]]
    price = closes[-1]
    fast = sma(closes, config.strategy.fast_sma_period)
    slow = sma(closes, config.strategy.slow_sma_period)
    trend = ema_series(closes, config.strategy.ema_trend_period)[-1]
    current_rsi = rsi(closes, config.strategy.rsi_period)
    macd_hist = macd_histogram(
        closes,
        config.strategy.macd_fast_period,
        config.strategy.macd_slow_period,
        config.strategy.macd_signal_period,
    )
    bollinger_z = bollinger_zscore(closes, config.strategy.bollinger_period, config.strategy.bollinger_stddev)
    momentum = percentage_momentum(closes, config.strategy.fast_sma_period)
    score, reasons = score_market(config, price, fast, slow, trend, current_rsi, macd_hist, bollinger_z, momentum)
    advanced = advanced_strategy_metrics(config, candles)
    score += int(advanced.get("score_adjustment") or 0)
    reasons.extend(str(reason) for reason in advanced.get("reasons", []))
    if score >= config.multi_timeframe.min_buy_score and momentum > 0 and price >= trend:
        regime = "uptrend"
    elif score <= -config.strategy.min_sell_score and momentum < 0 and price < trend:
        regime = "downtrend"
    else:
        regime = "range_or_transition"
    return {
        "enabled": config.multi_timeframe.enabled,
        "interval_minutes": config.multi_timeframe.interval_minutes,
        "regime": regime,
        "score": score,
        "price": str(price),
        "fast_sma": str(fast),
        "slow_sma": str(slow),
        "trend_ema": str(trend),
        "rsi": str(current_rsi),
        "macd_histogram": str(macd_hist),
        "bollinger_z": str(bollinger_z),
        "momentum_pct": str(momentum),
        "advanced_strategy": advanced,
        "reasons": reasons,
    }


def higher_timeframe_allows_buy(config: BotConfig, regime: dict[str, Any] | None) -> bool:
    if not config.multi_timeframe.enabled:
        return True
    if not regime:
        return False
    return str(regime.get("regime")) == "uptrend" and int(regime.get("score", -999)) >= config.multi_timeframe.min_buy_score


def higher_timeframe_allows_short(config: BotConfig, regime: dict[str, Any] | None) -> bool:
    if not config.multi_timeframe.enabled or not config.downside.require_higher_timeframe_downtrend:
        return True
    if not regime:
        return False
    return str(regime.get("regime")) == "downtrend" and int(regime.get("score", 999)) <= -config.downside.min_short_score


def downside_bias(config: BotConfig, candles: list[Candle], regime: dict[str, Any] | None) -> dict[str, Any]:
    if not config.downside.enabled:
        return {"enabled": False, "action": "disabled", "short_score": 0}

    probe = make_signal(config, candles, Decimal("0"), None)
    short_score = max(0, -probe.score)
    score_ok = short_score >= config.downside.min_short_score
    momentum_ok = probe.momentum_pct <= -config.downside.min_short_momentum_pct
    regime_ok = higher_timeframe_allows_short(config, regime)
    action = "open_or_hold_short" if score_ok and momentum_ok and regime_ok else "no_short_edge"
    if score_ok and momentum_ok and not regime_ok:
        action = "wait_for_higher_timeframe_downtrend"

    return {
        "enabled": True,
        "action": action,
        "short_score": short_score,
        "raw_score": probe.score,
        "price": str(probe.price),
        "rsi": str(probe.rsi),
        "macd_histogram": str(probe.macd_histogram),
        "bollinger_z": str(probe.bollinger_z),
        "momentum_pct": str(probe.momentum_pct),
        "min_short_score": config.downside.min_short_score,
        "min_short_momentum_pct": str(config.downside.min_short_momentum_pct),
        "close_short_reversal_score": config.downside.close_short_reversal_score,
        "higher_timeframe_ok": regime_ok,
        "live_short_enabled": config.downside.live_short_enabled,
        "shadow_short_enabled": config.downside.shadow_short_enabled,
        "reason": probe.reason,
    }


def ensure_live_ack(config: BotConfig) -> None:
    if config.mode != "live":
        return
    if os.environ.get("KRAKEN_LIVE_TRADING_ACK") != LIVE_ACK:
        raise BotError(
            f"Live mode blocked. Set KRAKEN_LIVE_TRADING_ACK={LIVE_ACK!r} only when you accept real loss risk."
        )


def quantize_volume(volume: Decimal, decimals: int) -> Decimal:
    step = Decimal("1").scaleb(-decimals)
    return volume.quantize(step, rounding=ROUND_DOWN)


def format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def side_cost_rate(config: BotConfig) -> Decimal:
    return config.risk.fee_bps / Decimal("10000")


def estimated_round_trip_cost_bps(config: BotConfig) -> Decimal:
    return (config.risk.fee_bps + config.risk.slippage_bps) * Decimal("2")


def min_net_profit_pct(config: BotConfig) -> Decimal:
    required_bps = estimated_round_trip_cost_bps(config) + config.risk.min_net_profit_bps
    return required_bps / Decimal("10000")


def position_pnl_snapshot(
    config: BotConfig,
    quote_balance: Decimal,
    base_balance: Decimal,
    price: Decimal,
    avg_entry: Decimal | None,
) -> dict[str, str]:
    position_value = base_balance * price
    equity = quote_balance + position_value
    entry_value = base_balance * avg_entry if avg_entry and base_balance > 0 else Decimal("0")
    unrealized = position_value - entry_value if entry_value > 0 else Decimal("0")
    exit_fee = position_value * side_cost_rate(config)
    return {
        "equity_quote": str(equity),
        "position_value_quote": str(position_value),
        "cost_basis_quote": str(entry_value),
        "unrealized_pnl_quote": str(unrealized),
        "estimated_exit_fee_quote": str(exit_fee),
        "unrealized_pnl_after_estimated_exit_fee_quote": str(unrealized - exit_fee if entry_value > 0 else Decimal("0")),
        "estimated_round_trip_cost_bps": str(estimated_round_trip_cost_bps(config)),
        "minimum_net_profit_bps": str(config.risk.min_net_profit_bps),
    }


def fee_edge_check(config: BotConfig, signal: Signal) -> dict[str, Any]:
    required_bps = estimated_round_trip_cost_bps(config) + config.risk.min_net_profit_bps
    momentum_bps = max(signal.momentum_pct, Decimal("0")) * Decimal("100")
    return {
        "allowed": momentum_bps >= required_bps,
        "required_bps": str(required_bps),
        "momentum_bps": str(momentum_bps),
        "round_trip_cost_bps": str(estimated_round_trip_cost_bps(config)),
        "minimum_net_profit_bps": str(config.risk.min_net_profit_bps),
    }


def in_cooldown(state: dict[str, Any], cooldown_minutes: int) -> bool:
    last_trade_at = state.get("last_trade_at")
    if not last_trade_at:
        return False
    last = datetime.fromisoformat(last_trade_at)
    elapsed = datetime.now(timezone.utc) - last
    return elapsed.total_seconds() < cooldown_minutes * 60


def iso_now_plus(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


def iso_is_future(value: Any) -> bool:
    if not value:
        return False
    try:
        until = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return datetime.now(timezone.utc) < until


def pair_lock_reason(config: BotConfig, state: dict[str, Any]) -> tuple[bool, str]:
    protections = config.raw.get("protections", {})
    if not protections.get("enabled", False):
        return False, "disabled"
    if iso_is_future(state.get("protection_global_lock_until")):
        return True, str(state.get("protection_global_lock_reason") or "protection_global_lock")

    pair_locks = state.get("protection_pair_locks")
    if isinstance(pair_locks, dict):
        pair_lock = pair_locks.get(config.pair)
        if isinstance(pair_lock, dict) and iso_is_future(pair_lock.get("until")):
            return True, str(pair_lock.get("reason") or "protection_pair_lock")

    stop_minutes = int(protections.get("stop_duration_minutes", 0))
    max_loss = Decimal(str(protections.get("max_realized_loss_quote", "0")))
    if stop_minutes > 0 and max_loss > 0 and Decimal(str(state.get("realized_pnl_today", "0"))) <= -max_loss:
        state["protection_global_lock_until"] = iso_now_plus(stop_minutes)
        state["protection_global_lock_reason"] = "realized_loss_guard"
        return True, "realized_loss_guard"

    loss_limit = int(protections.get("consecutive_loss_limit", 0))
    if stop_minutes > 0 and loss_limit > 0 and int(state.get("consecutive_loss_trades", 0)) >= loss_limit:
        state["protection_global_lock_until"] = iso_now_plus(stop_minutes)
        state["protection_global_lock_reason"] = "consecutive_loss_guard"
        return True, "consecutive_loss_guard"

    return False, "ok"


def below_min_trade_interval(state: dict[str, Any], min_trade_interval_seconds: int) -> bool:
    if min_trade_interval_seconds <= 0:
        return False
    last_trade_at = state.get("last_trade_at")
    if not last_trade_at:
        return False
    last = datetime.fromisoformat(last_trade_at)
    elapsed = datetime.now(timezone.utc) - last
    return elapsed.total_seconds() < min_trade_interval_seconds


def risk_allows_trade(config: BotConfig, state: dict[str, Any], signal: Signal) -> tuple[bool, str]:
    if signal.side == "hold":
        return False, "hold_signal"
    if signal.side == "buy" and state.get("equity_kill_switch_active"):
        return False, str(state.get("equity_kill_switch_reason") or "equity_kill_switch")
    if signal.side == "buy":
        locked, reason = pair_lock_reason(config, state)
        if locked:
            return False, reason
    if below_min_trade_interval(state, config.execution.min_trade_interval_seconds):
        return False, "min_trade_interval"
    if in_cooldown(state, config.risk.cooldown_minutes):
        return False, "cooldown"
    if int(state.get("trades_today", 0)) >= config.risk.max_trades_per_day:
        return False, "max_trades_per_day"
    if Decimal(str(state.get("realized_pnl_today", "0"))) <= -config.risk.daily_loss_limit_quote:
        return False, "daily_loss_limit"
    return True, "allowed"


HARD_AI_OVERRIDE_BLOCKS = {
    "daily_loss_limit",
    "realized_loss_guard",
    "consecutive_loss_guard",
    "pair_cooldown_after_sell",
    "protection_global_lock",
    "protection_pair_lock",
    "equity_kill_switch",
    "daily_equity_drawdown",
    "high_water_equity_drawdown",
    "max_trades_per_day",
    "min_trade_interval",
    "cooldown",
    "reconnect_observe",
    "outside_trading_window",
    "spread_too_wide",
    "candle_range_too_wide",
    "atr_too_high",
    "var_es_too_high",
    "fee_edge_too_small",
    "downside_bias_block",
    "market_breadth_risk_off",
    "ai_plan_risk_off",
    "sprint_goal_reached",
}


def ai_override_decision(
    config: BotConfig,
    signal: Signal,
    risk_reason: str,
    ai_plan: dict[str, Any],
    event: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    detail: dict[str, Any] = {
        "enabled": config.ai_plan.allow_decision_override,
        "approved": False,
        "reason": "not_evaluated",
        "original_risk": risk_reason,
    }
    if not config.ai_plan.allow_decision_override:
        detail["reason"] = "disabled"
        return False, detail
    if signal.side == "sell":
        detail["reason"] = "never_override_sell_or_exit"
        return False, detail
    if risk_reason in HARD_AI_OVERRIDE_BLOCKS or risk_reason.startswith("market_"):
        detail["reason"] = f"hard_block:{risk_reason}"
        return False, detail
    fee_edge = event.get("fee_edge_check") if isinstance(event.get("fee_edge_check"), dict) else {}
    if fee_edge and not fee_edge.get("allowed", False):
        detail["reason"] = "fee_edge_not_cleared"
        return False, detail
    if ai_plan.get("should_block_buys"):
        detail["reason"] = "ai_plan_blocks_buys"
        return False, detail
    action = str(ai_plan.get("action") or "")
    forecast = ai_plan.get("forecast") if isinstance(ai_plan.get("forecast"), dict) else {}
    wants_buy = action in {"standard_accumulate", "aggressive_accumulate"} and forecast.get("direction") == "up"
    if not wants_buy:
        detail["reason"] = f"ai_not_bullish:{action}/{forecast.get('direction')}"
        return False, detail
    confidence = int(ai_plan.get("confidence") or 0)
    if confidence < config.ai_plan.override_min_confidence:
        detail["reason"] = f"confidence_below_{config.ai_plan.override_min_confidence}"
        return False, detail
    if int(event.get("score") or 0) < config.ai_plan.override_min_signal_score:
        detail["reason"] = f"signal_score_below_{config.ai_plan.override_min_signal_score}"
        return False, detail
    research = event.get("research") if isinstance(event.get("research"), dict) else {}
    if int(research.get("risk_score") or 0) > config.ai_plan.override_max_research_score:
        detail["reason"] = f"research_score_above_{config.ai_plan.override_max_research_score}"
        return False, detail
    radar = event.get("market_radar") if isinstance(event.get("market_radar"), dict) else {}
    if radar.get("risk_off"):
        detail["reason"] = "market_radar_risk_off"
        return False, detail
    positive_breadth = Decimal(str(radar.get("positive_breadth", "0.5"))) if radar.get("enabled") else Decimal("0.5")
    if positive_breadth < config.ai_plan.override_min_positive_breadth:
        detail["reason"] = f"positive_breadth_below_{config.ai_plan.override_min_positive_breadth}"
        return False, detail
    market_metrics = event.get("market_metrics") if isinstance(event.get("market_metrics"), dict) else {}
    if Decimal(str(market_metrics.get("spread_bps", "999"))) > config.ai_plan.max_spread_bps_to_increase:
        detail["reason"] = "spread_above_ai_override_limit"
        return False, detail
    if (event.get("equity_guard") or {}).get("active"):
        detail["reason"] = "equity_guard_active"
        return False, detail
    if (event.get("downside_bias") or {}).get("action") == "open_or_hold_short":
        detail["reason"] = "downside_bias_active"
        return False, detail

    detail.update(
        {
            "approved": True,
            "reason": "ai_high_confidence_override",
            "confidence": confidence,
            "action": action,
            "positive_breadth": str(positive_breadth),
            "override_rationale": forecast.get("override_rationale"),
        }
    )
    return True, detail


def ai_buy_signal_from_hold(signal: Signal, ai_plan: dict[str, Any]) -> Signal:
    if signal.side == "buy":
        return signal
    forecast = ai_plan.get("forecast") if isinstance(ai_plan.get("forecast"), dict) else {}
    reason = "ai_override:" + str(forecast.get("override_rationale") or forecast.get("rationale") or "chief_trader_buy")[:180]
    return Signal(
        side="buy",
        reason=reason,
        price=signal.price,
        fast_sma=signal.fast_sma,
        slow_sma=signal.slow_sma,
        rsi=signal.rsi,
        score=signal.score,
        macd_histogram=signal.macd_histogram,
        bollinger_z=signal.bollinger_z,
        momentum_pct=signal.momentum_pct,
    )


def update_equity_guard(config: BotConfig, state: dict[str, Any], pnl_snapshot: dict[str, str]) -> dict[str, str | bool]:
    current_day = today_key()
    current_equity = Decimal(str(pnl_snapshot.get("equity_quote", "0")))
    if state.get("equity_guard_day") != current_day:
        state["equity_guard_day"] = current_day
        state["daily_start_equity_quote"] = str(current_equity)
        state["high_water_equity_quote"] = str(current_equity)
        state["equity_kill_switch_active"] = False
        state["equity_kill_switch_reason"] = None

    daily_start = Decimal(str(state.get("daily_start_equity_quote", current_equity)))
    high_water = Decimal(str(state.get("high_water_equity_quote", current_equity)))
    if current_equity > high_water:
        high_water = current_equity
        state["high_water_equity_quote"] = str(high_water)

    daily_drawdown = daily_start - current_equity
    high_water_drawdown = high_water - current_equity
    existing_reason = state.get("equity_kill_switch_reason")
    reason = str(existing_reason) if existing_reason else None
    if not reason and config.risk.max_daily_equity_drawdown_quote > 0 and daily_drawdown >= config.risk.max_daily_equity_drawdown_quote:
        reason = "daily_equity_drawdown"
    if not reason and config.risk.max_high_water_drawdown_quote > 0 and high_water_drawdown >= config.risk.max_high_water_drawdown_quote:
        reason = "high_water_equity_drawdown"

    active = bool(reason)
    state["equity_kill_switch_active"] = active
    state["equity_kill_switch_reason"] = reason
    return {
        "active": active,
        "reason": reason or "ok",
        "current_equity_quote": str(current_equity),
        "daily_start_equity_quote": str(daily_start),
        "high_water_equity_quote": str(high_water),
        "daily_drawdown_quote": str(daily_drawdown),
        "high_water_drawdown_quote": str(high_water_drawdown),
    }


def position_size_multiplier_from_research(config: BotConfig, research_snapshot: dict[str, Any]) -> Decimal:
    research_cfg = config.raw.get("research", {})
    if not research_cfg.get("enabled", False):
        return Decimal("1")
    if research_snapshot.get("block_buys"):
        return Decimal("0")
    if research_snapshot.get("reduce_size"):
        return Decimal(str(research_cfg.get("reduced_size_multiplier", "0.5")))
    return Decimal("1")


def market_filter_allows(config: BotConfig, ticker: Ticker, candles: list[Candle]) -> tuple[bool, str, dict[str, str]]:
    candle_range = last_candle_range_pct(candles)
    atr = atr_pct(candles, config.market_filters.atr_period)
    metrics = {
        "spread_bps": str(ticker.spread_bps),
        "last_candle_range_pct": str(candle_range),
        "atr_pct": str(atr),
        "daily_change_pct": str(ticker.change_pct),
    }
    if ticker.spread_bps > config.market_filters.max_spread_bps:
        return False, "spread_too_wide", metrics
    if candle_range > config.market_filters.max_candle_range_pct:
        return False, "last_candle_too_volatile", metrics
    if atr > config.market_filters.max_atr_pct:
        return False, "atr_too_high", metrics
    return True, "market_ok", metrics


def scan_pair_candidates(config: BotConfig, market_data: dict[str, tuple[Ticker, list[Candle]]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    configured_pairs = config.scan_pairs or list(market_data.keys())
    for pair in configured_pairs:
        if pair not in market_data:
            continue
        ticker, pair_candles = market_data[pair]
        try:
            signal = make_signal(config, pair_candles, Decimal("0"), None)
            market_allowed, market_reason, market_metrics = market_filter_allows(config, ticker, pair_candles)
            advanced = advanced_strategy_metrics(config, pair_candles)
            candidate = {
                "pair": pair,
                "signal": signal.side,
                "signal_reason": signal.reason,
                "score": signal.score,
                "short_score": max(0, -signal.score),
                "price": str(signal.price),
                "rsi": str(signal.rsi),
                "momentum_pct": str(signal.momentum_pct),
                "market_allowed": market_allowed,
                "market_reason": market_reason,
                "market_metrics": market_metrics,
                "advanced_strategy": advanced,
                "estimated_round_trip_cost_bps": str(estimated_round_trip_cost_bps(config)),
                "order_execution": "scanner_only",
            }
        except Exception as exc:
            candidate = {
                "pair": pair,
                "signal": "hold",
                "signal_reason": "scan_error",
                "score": -999,
                "market_allowed": False,
                "market_reason": type(exc).__name__,
                "error": str(exc),
                "estimated_round_trip_cost_bps": str(estimated_round_trip_cost_bps(config)),
                "order_execution": "scanner_only",
            }
        candidates.append(candidate)
    return sorted(
        candidates,
        key=lambda item: (
            item.get("market_allowed") is True,
            item.get("signal") == "buy",
            int(item.get("score", -999)),
            -Decimal(str(item.get("market_metrics", {}).get("spread_bps", "999999"))),
        ),
        reverse=True,
    )


def build_market_radar(
    config: BotConfig,
    client: KrakenClient,
    market_data: dict[str, tuple[Ticker, list[Candle]]],
) -> dict[str, Any]:
    if not config.market_radar.enabled:
        return {"enabled": False}

    primary_pairs: list[str] = []
    context_pairs: list[str] = []
    for pair in config.market_radar.pairs:
        if pair not in primary_pairs:
            primary_pairs.append(pair)
    for pair in config.market_radar.context_pairs:
        if pair not in context_pairs:
            context_pairs.append(pair)

    if config.market_radar.max_pairs_per_cycle > 0:
        context_budget = min(len(context_pairs), config.market_radar.max_pairs_per_cycle)
        primary_budget = max(config.market_radar.max_pairs_per_cycle - context_budget, 0)
        requested = primary_pairs[:primary_budget] + context_pairs[:context_budget]
    else:
        requested = primary_pairs + context_pairs

    assets: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for pair in requested:
        try:
            if pair not in market_data:
                market_data[pair] = (client.ticker(pair), client.candles(pair, config.candle_interval_minutes))
            ticker, pair_candles = market_data[pair]
            signal = make_signal(config, pair_candles, Decimal("0"), None)
            market_allowed, market_reason, market_metrics = market_filter_allows(config, ticker, pair_candles)
            advanced = advanced_strategy_metrics(config, pair_candles)
            assets.append(
                {
                    "pair": pair,
                    "signal": signal.side,
                    "score": signal.score,
                    "short_score": max(0, -signal.score),
                    "price": str(signal.price),
                    "rsi": str(signal.rsi),
                    "momentum_pct": str(signal.momentum_pct),
                    "market_allowed": market_allowed,
                    "market_reason": market_reason,
                    "spread_bps": market_metrics.get("spread_bps"),
                    "daily_change_pct": market_metrics.get("daily_change_pct"),
                    "advanced_strategy": advanced,
                }
            )
        except Exception as exc:
            errors[pair] = f"{type(exc).__name__}: {exc}"

    analyzed = len(assets)
    positive = sum(1 for asset in assets if int(asset.get("score", 0)) > 0)
    strong_positive = sum(1 for asset in assets if int(asset.get("score", 0)) >= config.strategy.min_buy_score)
    negative = sum(1 for asset in assets if int(asset.get("score", 0)) < 0)
    strong_negative = sum(1 for asset in assets if int(asset.get("score", 0)) <= -config.strategy.min_sell_score)
    positive_breadth = Decimal(positive) / Decimal(analyzed) if analyzed else Decimal("0")
    strong_positive_breadth = Decimal(strong_positive) / Decimal(analyzed) if analyzed else Decimal("0")
    negative_breadth = Decimal(negative) / Decimal(analyzed) if analyzed else Decimal("0")
    strong_negative_breadth = Decimal(strong_negative) / Decimal(analyzed) if analyzed else Decimal("0")
    sorted_assets = sorted(
        assets,
        key=lambda item: (
            bool(item.get("market_allowed")),
            int(item.get("score", -999)),
            Decimal(str(item.get("momentum_pct", "0"))),
        ),
        reverse=True,
    )
    weakest = sorted(
        assets,
        key=lambda item: (
            int(item.get("score", 999)),
            Decimal(str(item.get("momentum_pct", "0"))),
        ),
    )

    context = {asset["pair"]: asset for asset in assets if asset["pair"] in config.market_radar.context_pairs}
    risk_on = (
        positive_breadth >= config.market_radar.min_positive_breadth_to_increase
        and strong_negative_breadth < Decimal("0.25")
    )
    risk_off = positive_breadth < config.market_radar.block_buy_breadth_below or strong_negative_breadth >= Decimal("0.35")
    return {
        "enabled": True,
        "pairs_requested": len(requested),
        "pairs_analyzed": analyzed,
        "positive_breadth": str(positive_breadth),
        "strong_positive_breadth": str(strong_positive_breadth),
        "negative_breadth": str(negative_breadth),
        "strong_negative_breadth": str(strong_negative_breadth),
        "risk_on": risk_on,
        "risk_off": risk_off,
        "top_assets": sorted_assets[:5],
        "weak_assets": weakest[:5],
        "usd_context": {
            key: {
                "price": value.get("price"),
                "score": value.get("score"),
                "momentum_pct": value.get("momentum_pct"),
                "daily_change_pct": value.get("daily_change_pct"),
                "spread_bps": value.get("spread_bps"),
            }
            for key, value in context.items()
        },
        "errors": errors,
    }


def shadow_cycle(
    config: BotConfig,
    ticker: Ticker,
    candles: list[Candle],
    market_metrics: dict[str, str],
    research_snapshot: dict[str, Any],
    regime: dict[str, Any] | None,
    downside: dict[str, Any],
    size_multiplier: Decimal,
) -> dict[str, Any]:
    state = load_shadow_state(config)
    shadow_pair_reset = reset_shadow_pair_if_needed(config, state)
    reset_daily_if_needed(state)
    reset_shadow_short_daily_if_needed(state)
    quote_balance = Decimal(str(state["paper_quote_balance"]))
    base_balance = Decimal(str(state["paper_base_balance"]))
    avg_entry = Decimal(str(state["avg_entry_price"])) if state.get("avg_entry_price") else None
    signal = make_signal(config, candles, base_balance, avg_entry)
    allowed, risk_reason = risk_allows_trade(config, state, signal)
    market_allowed, market_reason, _ = market_filter_allows(config, ticker, candles)
    if allowed and not market_allowed:
        allowed = False
        risk_reason = market_reason
    if allowed and signal.side == "buy" and research_snapshot.get("block_buys"):
        allowed = False
        risk_reason = "research_risk_off"
    if allowed and signal.side == "buy" and not higher_timeframe_allows_buy(config, regime):
        allowed = False
        risk_reason = "higher_timeframe_not_aligned"
    if (
        allowed
        and signal.side == "buy"
        and config.downside.block_buys_when_downside
        and downside.get("action") == "open_or_hold_short"
    ):
        allowed = False
        risk_reason = "downside_bias_block"

    if allowed:
        order = execute_paper_order(config, state, signal, size_multiplier)
    else:
        order = {"status": "skipped", "reason": risk_reason}

    equity = Decimal(str(state["paper_quote_balance"])) + Decimal(str(state["paper_base_balance"])) * signal.price
    short_event = shadow_short_cycle(config, state, downside, signal.price, market_allowed, market_reason, size_multiplier)
    event = {
        "mode": "shadow",
        "pair": config.pair,
        "shadow_pair_reset": shadow_pair_reset,
        "signal": signal.side,
        "signal_reason": signal.reason,
        "risk": risk_reason,
        "score": signal.score,
        "price": str(signal.price),
        "rsi": str(signal.rsi),
        "macd_histogram": str(signal.macd_histogram),
        "bollinger_z": str(signal.bollinger_z),
        "momentum_pct": str(signal.momentum_pct),
        "market_metrics": market_metrics,
        "advanced_strategy": advanced_strategy_metrics(config, candles),
        "market_regime": regime,
        "downside_bias": downside,
        "research_risk_score": research_snapshot.get("risk_score", 0),
        "position_size_multiplier": str(size_multiplier),
        "quote_balance": state["paper_quote_balance"],
        "base_balance": state["paper_base_balance"],
        "equity_quote": str(equity),
        "shadow_short": short_event,
        "order": order,
    }
    save_shadow_state(config, state)
    log_shadow_event(config, event)
    return event


def current_balances(config: BotConfig, client: KrakenClient, state: dict[str, Any]) -> tuple[Decimal, Decimal]:
    if config.mode == "paper":
        return Decimal(str(state["paper_quote_balance"])), Decimal(str(state["paper_base_balance"]))
    balances = client.balances()
    return balances.get(config.quote_asset, Decimal("0")), balances.get(config.base_asset, Decimal("0"))


def config_for_pair(config: BotConfig, client: KrakenClient, pair: str) -> BotConfig:
    if pair == config.pair:
        return config
    rules = client.asset_pair_rules(pair)
    base_asset = str(rules.get("base") or config.base_asset)
    quote_asset = str(rules.get("quote") or config.quote_asset)
    raw = json.loads(json.dumps(config.raw))
    raw["pair"] = pair
    raw["base_asset"] = base_asset
    raw["quote_asset"] = quote_asset
    raw["ws_symbol"] = pair
    return replace(config, raw=raw, pair=pair, ws_symbol=pair, base_asset=base_asset, quote_asset=quote_asset)


def build_dynamic_pairlist(
    config: BotConfig,
    client: KrakenClient,
    static_pairs: list[str],
) -> tuple[list[str], dict[str, Any]]:
    pairlist_cfg = config.raw.get("dynamic_pairlist", {})
    if not pairlist_cfg.get("enabled", False):
        return static_pairs, {"enabled": False}

    quote_asset = str(pairlist_cfg.get("quote_asset", config.raw.get("rotation", {}).get("quote_asset", config.quote_asset)))
    max_assets = int(pairlist_cfg.get("max_assets", 12))
    max_source_pairs = int(pairlist_cfg.get("max_source_pairs", 80))
    max_spread_bps = Decimal(str(pairlist_cfg.get("max_spread_bps", config.market_filters.max_spread_bps)))
    min_quote_volume = Decimal(str(pairlist_cfg.get("min_quote_volume", "0")))
    min_price = Decimal(str(pairlist_cfg.get("min_price", "0")))
    max_price = Decimal(str(pairlist_cfg.get("max_price", "0")))
    include_static = bool(pairlist_cfg.get("include_static_pairs", True))

    pairs: list[str] = []
    errors: dict[str, str] = {}
    try:
        for pair, meta in client.asset_pairs().items():
            if len(pairs) >= max_source_pairs:
                break
            if not isinstance(meta, dict):
                continue
            if meta.get("status") not in {None, "", "online"}:
                continue
            if str(meta.get("quote") or "") != quote_asset:
                continue
            if ".d" in str(pair).lower():
                continue
            pairs.append(str(pair))
    except Exception as exc:
        errors["asset_pairs"] = f"{type(exc).__name__}: {exc}"
        pairs = []

    if include_static:
        for pair in static_pairs:
            if pair not in pairs:
                pairs.insert(0, pair)

    candidates: list[dict[str, Any]] = []
    for pair in pairs[:max_source_pairs]:
        try:
            ticker = client.ticker(pair)
            if ticker.spread_bps > max_spread_bps:
                continue
            if ticker.quote_volume < min_quote_volume:
                continue
            if min_price > 0 and ticker.last < min_price:
                continue
            if max_price > 0 and ticker.last > max_price:
                continue
            candidates.append(
                {
                    "pair": pair,
                    "spread_bps": str(ticker.spread_bps),
                    "quote_volume": str(ticker.quote_volume),
                    "last": str(ticker.last),
                    "change_pct": str(ticker.change_pct),
                }
            )
        except Exception as exc:
            errors[pair] = f"{type(exc).__name__}: {exc}"

    candidates.sort(
        key=lambda item: (
            Decimal(str(item.get("quote_volume", "0"))),
            Decimal(str(item.get("change_pct", "0"))),
            -Decimal(str(item.get("spread_bps", "999999"))),
        ),
        reverse=True,
    )
    selected: list[str] = []
    for item in candidates:
        pair = str(item["pair"])
        if pair not in selected:
            selected.append(pair)
        if len(selected) >= max_assets:
            break

    return selected or static_pairs, {
        "enabled": True,
        "quote_asset": quote_asset,
        "max_assets": max_assets,
        "max_source_pairs": max_source_pairs,
        "min_quote_volume": str(min_quote_volume),
        "max_spread_bps": str(max_spread_bps),
        "selected_pairs": selected,
        "top_candidates": candidates[:8],
        "errors": errors,
    }


def select_rotation_pair(config: BotConfig, client: KrakenClient, state: dict[str, Any]) -> tuple[BotConfig, dict[str, Any]]:
    rotation_cfg = config.raw.get("rotation", {})
    if not rotation_cfg.get("enabled", False):
        return config, {"enabled": False, "active_pair": config.pair, "reason": "disabled"}

    allowed_pairs = list(rotation_cfg.get("pairs") or config.scan_pairs or [config.pair])
    if config.pair not in allowed_pairs:
        allowed_pairs.insert(0, config.pair)
    allowed_pairs, dynamic_pairlist = build_dynamic_pairlist(config, client, allowed_pairs)
    if config.pair not in allowed_pairs:
        allowed_pairs.insert(0, config.pair)
    quote_asset = str(rotation_cfg.get("quote_asset", config.quote_asset))
    min_score = int(rotation_cfg.get("min_score", config.strategy.min_buy_score))
    max_spread_bps = Decimal(str(rotation_cfg.get("max_spread_bps", config.market_filters.max_spread_bps)))
    active_pair = str(state.get("active_pair") or config.pair)

    try:
        active_config = config_for_pair(config, client, active_pair)
        balances = client.balances() if config.mode == "live" else {}
        active_base = balances.get(active_config.base_asset, Decimal("0")) if config.mode == "live" else Decimal(str(state.get("paper_base_balance", "0")))
        active_rules = client.asset_pair_rules(active_pair) if config.mode == "live" else {"ordermin": "0"}
        active_min_volume = Decimal(str(active_rules.get("ordermin", "0")))
        if active_base > 0 and (active_min_volume <= 0 or active_base >= active_min_volume):
            return active_config, {"enabled": True, "active_pair": active_pair, "reason": "holding_active_pair", "base_balance": str(active_base)}
        if active_base > 0:
            state["active_pair_dust"] = {"pair": active_pair, "base_balance": str(active_base), "min_volume": str(active_min_volume)}
    except Exception as exc:
        return config, {"enabled": True, "active_pair": config.pair, "reason": f"active_pair_check_failed:{type(exc).__name__}"}

    candidates: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for pair in allowed_pairs:
        try:
            pair_config = config_for_pair(config, client, pair)
            if pair_config.quote_asset != quote_asset:
                continue
            ticker = client.ticker(pair)
            pair_candles = client.candles(pair, config.candle_interval_minutes)
            signal = make_signal(config, pair_candles, Decimal("0"), None)
            market_allowed, market_reason, market_metrics = market_filter_allows(config, ticker, pair_candles)
            advanced = advanced_strategy_metrics(config, pair_candles)
            candidates.append(
                {
                    "pair": pair,
                    "score": signal.score,
                    "signal": signal.side,
                    "signal_reason": signal.reason,
                    "momentum_pct": str(signal.momentum_pct),
                    "market_allowed": market_allowed,
                    "market_reason": market_reason,
                    "spread_bps": market_metrics.get("spread_bps"),
                    "quote_volume": str(ticker.quote_volume),
                    "price": str(signal.price),
                    "advanced_strategy": advanced,
                }
            )
        except Exception as exc:
            errors[pair] = f"{type(exc).__name__}: {exc}"

    eligible = [
        item
        for item in candidates
        if item.get("market_allowed")
        and int(item.get("score", -999)) >= min_score
        and Decimal(str(item.get("spread_bps", "999"))) <= max_spread_bps
        and not item.get("advanced_strategy", {}).get("risk_model", {}).get("risk_block")
    ]
    eligible.sort(
        key=lambda item: (
            int(item.get("score", -999)),
            Decimal(str(item.get("momentum_pct", "0"))),
            Decimal(str(item.get("quote_volume", "0"))),
        ),
        reverse=True,
    )
    chosen = eligible[0]["pair"] if eligible else config.pair
    state["active_pair"] = chosen
    return config_for_pair(config, client, chosen), {
        "enabled": True,
        "active_pair": chosen,
        "reason": "selected_best_candidate" if eligible else "no_candidate_met_rotation_gate",
        "min_score": min_score,
        "max_spread_bps": str(max_spread_bps),
        "dynamic_pairlist": dynamic_pairlist,
        "top_candidates": candidates[:8],
        "errors": errors,
    }


def planned_buy_quote_size(
    config: BotConfig,
    quote_balance: Decimal,
    current_position_quote: Decimal,
    size_multiplier: Decimal = Decimal("1"),
) -> Decimal:
    available = quote_balance - config.risk.reserve_quote_balance
    remaining_position = config.risk.max_position_quote - current_position_quote
    if available <= 0 or remaining_position <= 0 or size_multiplier <= 0:
        return Decimal("0")
    multiplier = max(size_multiplier, Decimal("0"))
    hard_order_cap = max(config.risk.max_order_quote, config.ai_plan.max_aggressive_order_quote)
    order_cap = min(config.risk.max_order_quote * multiplier, hard_order_cap)
    risk_sized_quote = min((config.risk.risk_per_trade_quote / config.risk.stop_loss_pct) * multiplier, hard_order_cap)
    return min(order_cap, remaining_position, available, risk_sized_quote)


def execute_paper_order(
    config: BotConfig,
    state: dict[str, Any],
    signal: Signal,
    size_multiplier: Decimal = Decimal("1"),
) -> dict[str, Any]:
    quote_balance = Decimal(str(state["paper_quote_balance"]))
    base_balance = Decimal(str(state["paper_base_balance"]))
    price = signal.price

    if signal.side == "buy":
        current_position_quote = base_balance * price
        quote_to_spend = planned_buy_quote_size(config, quote_balance, current_position_quote, size_multiplier)
        if quote_to_spend <= 0:
            return {"status": "skipped", "reason": "no_quote_balance"}
        fee_quote = quote_to_spend * side_cost_rate(config)
        net_quote_to_convert = quote_to_spend - fee_quote
        base_bought = net_quote_to_convert / price
        if base_bought <= 0:
            return {"status": "skipped", "reason": "below_fee_adjusted_size"}
        previous_avg_entry = Decimal(str(state["avg_entry_price"])) if state.get("avg_entry_price") else price
        previous_cost_basis = previous_avg_entry * base_balance
        new_base_balance = base_balance + base_bought
        new_cost_basis = previous_cost_basis + quote_to_spend
        state["paper_quote_balance"] = str(quote_balance - quote_to_spend)
        state["paper_base_balance"] = str(new_base_balance)
        state["avg_entry_price"] = str(new_cost_basis / new_base_balance)
        mark_trade(state)
        return {
            "status": "filled_paper",
            "side": "buy",
            "quote": str(quote_to_spend),
            "base": str(base_bought),
            "fee_quote": str(fee_quote),
            "avg_entry_price": state["avg_entry_price"],
        }

    if signal.side == "sell":
        if base_balance <= 0:
            return {"status": "skipped", "reason": "no_base_balance"}
        gross_quote_received = base_balance * price
        fee_quote = gross_quote_received * side_cost_rate(config)
        quote_received = gross_quote_received - fee_quote
        avg_entry = Decimal(str(state["avg_entry_price"] or price))
        pnl = quote_received - (avg_entry * base_balance)
        state["paper_quote_balance"] = str(quote_balance + quote_received)
        state["paper_base_balance"] = "0"
        state["avg_entry_price"] = None
        state["realized_pnl_today"] = str(Decimal(str(state["realized_pnl_today"])) + pnl)
        update_protections_after_sell(config, state, pnl)
        mark_trade(state)
        return {
            "status": "filled_paper",
            "side": "sell",
            "quote": str(quote_received),
            "gross_quote": str(gross_quote_received),
            "base": str(base_balance),
            "fee_quote": str(fee_quote),
            "pnl": str(pnl),
        }

    return {"status": "skipped", "reason": "hold"}


def shadow_short_equity(config: BotConfig, state: dict[str, Any], price: Decimal) -> Decimal:
    collateral = Decimal(str(state.get("short_collateral_quote_balance", config.shadow.starting_quote)))
    short_base = Decimal(str(state.get("paper_short_base_balance", "0")))
    avg_entry = Decimal(str(state["short_avg_entry_price"])) if state.get("short_avg_entry_price") else None
    if short_base <= 0 or not avg_entry:
        return collateral
    unrealized = (avg_entry - price) * short_base
    estimated_close_fee = short_base * price * side_cost_rate(config)
    return collateral + unrealized - estimated_close_fee


def execute_shadow_short_order(
    config: BotConfig,
    state: dict[str, Any],
    action: str,
    price: Decimal,
    size_multiplier: Decimal = Decimal("1"),
) -> dict[str, Any]:
    collateral = Decimal(str(state.get("short_collateral_quote_balance", config.shadow.starting_quote)))
    short_base = Decimal(str(state.get("paper_short_base_balance", "0")))
    avg_entry = Decimal(str(state["short_avg_entry_price"])) if state.get("short_avg_entry_price") else None

    if action == "open_short":
        if short_base > 0:
            return {"status": "skipped", "reason": "short_already_open"}
        quote_to_short = min(config.risk.max_order_quote, collateral, config.risk.risk_per_trade_quote / config.risk.stop_loss_pct)
        quote_to_short *= min(max(size_multiplier, Decimal("0")), Decimal("1"))
        if quote_to_short <= 0:
            return {"status": "skipped", "reason": "no_short_collateral"}
        base_sold = quote_to_short / price
        open_fee = quote_to_short * side_cost_rate(config)
        state["short_collateral_quote_balance"] = str(collateral - open_fee)
        state["paper_short_base_balance"] = str(base_sold)
        state["short_avg_entry_price"] = str(price)
        state["short_open_fee_quote"] = str(open_fee)
        state["short_trades_today"] = int(state.get("short_trades_today", 0)) + 1
        return {
            "status": "filled_shadow_short",
            "side": "short",
            "quote_notional": str(quote_to_short),
            "base": str(base_sold),
            "entry_price": str(price),
            "fee_quote": str(open_fee),
        }

    if action == "close_short":
        if short_base <= 0 or not avg_entry:
            return {"status": "skipped", "reason": "no_shadow_short"}
        entry_notional = short_base * avg_entry
        buyback_quote = short_base * price
        close_fee = buyback_quote * side_cost_rate(config)
        pnl = entry_notional - buyback_quote - close_fee
        state["short_collateral_quote_balance"] = str(collateral + pnl)
        state["paper_short_base_balance"] = "0"
        state["short_avg_entry_price"] = None
        state["short_open_fee_quote"] = "0"
        state["short_realized_pnl_today"] = str(Decimal(str(state.get("short_realized_pnl_today", "0"))) + pnl)
        state["short_trades_today"] = int(state.get("short_trades_today", 0)) + 1
        return {
            "status": "closed_shadow_short",
            "side": "cover",
            "entry_price": str(avg_entry),
            "exit_price": str(price),
            "base": str(short_base),
            "gross_quote": str(entry_notional - buyback_quote),
            "fee_quote": str(close_fee),
            "pnl": str(pnl),
        }

    return {"status": "skipped", "reason": action}


def shadow_short_cycle(
    config: BotConfig,
    state: dict[str, Any],
    bias: dict[str, Any],
    price: Decimal,
    market_allowed: bool,
    market_reason: str,
    size_multiplier: Decimal,
) -> dict[str, Any]:
    ensure_shadow_short_state(config, state)
    reset_shadow_short_daily_if_needed(state)
    short_base = Decimal(str(state.get("paper_short_base_balance", "0")))
    avg_entry = Decimal(str(state["short_avg_entry_price"])) if state.get("short_avg_entry_price") else None
    action = str(bias.get("action", "no_short_edge"))

    order_action = "hold_short" if short_base > 0 else "no_short"
    reason = action
    if not config.downside.shadow_short_enabled:
        order_action = "shadow_short_disabled"
    elif not market_allowed:
        order_action = "market_filter_block"
        reason = market_reason
    elif short_base > 0 and avg_entry:
        stop_price = avg_entry * (Decimal("1") + config.risk.stop_loss_pct)
        target_price = avg_entry * (Decimal("1") - config.risk.take_profit_pct)
        if price >= stop_price:
            order_action = "close_short"
            reason = "shadow_short_stop_loss"
        elif price <= target_price:
            order_action = "close_short"
            reason = "shadow_short_take_profit"
        elif int(bias.get("raw_score", 0)) >= config.downside.close_short_reversal_score:
            order_action = "close_short"
            reason = "short_reversal_score"
    elif action == "open_or_hold_short":
        order_action = "open_short"

    if order_action in {"open_short", "close_short"}:
        order = execute_shadow_short_order(config, state, order_action, price, size_multiplier)
    else:
        order = {"status": "skipped", "reason": reason}

    equity = shadow_short_equity(config, state, price)
    return {
        "enabled": config.downside.shadow_short_enabled,
        "action": order_action,
        "reason": reason,
        "price": str(price),
        "base_balance": state.get("paper_short_base_balance", "0"),
        "avg_entry_price": state.get("short_avg_entry_price"),
        "collateral_quote": state.get("short_collateral_quote_balance", str(config.shadow.starting_quote)),
        "equity_quote": str(equity),
        "realized_pnl_today": state.get("short_realized_pnl_today", "0"),
        "trades_today": state.get("short_trades_today", 0),
        "order": order,
    }


def mark_trade(state: dict[str, Any]) -> None:
    state["last_trade_at"] = datetime.now(timezone.utc).isoformat()
    state["trades_today"] = int(state.get("trades_today", 0)) + 1


def update_protections_after_sell(config: BotConfig, state: dict[str, Any], pnl: Decimal) -> None:
    protections = config.raw.get("protections", {})
    if pnl < 0:
        state["consecutive_loss_trades"] = int(state.get("consecutive_loss_trades", 0)) + 1
    elif pnl > 0:
        state["consecutive_loss_trades"] = 0

    if not protections.get("enabled", False):
        return
    pair_cooldown = int(protections.get("pair_cooldown_after_sell_minutes", 0))
    if pair_cooldown <= 0:
        return
    locks = state.get("protection_pair_locks")
    if not isinstance(locks, dict):
        locks = {}
    locks[config.pair] = {
        "until": iso_now_plus(pair_cooldown),
        "reason": "pair_cooldown_after_sell",
        "pnl": str(pnl),
    }
    state["protection_pair_locks"] = locks


def update_live_position_estimate(
    state: dict[str, Any],
    order: dict[str, Any],
    prior_base_balance: Decimal,
    config: BotConfig | None = None,
) -> None:
    """Keep a live entry estimate so stop/take-profit logic can protect positions."""
    if order.get("status") not in {"submitted", "closed"}:
        return
    side = order.get("side")
    if side == "sell":
        if state.get("avg_entry_price") and order.get("executed_volume") and order.get("executed_cost"):
            executed_volume = Decimal(str(order.get("executed_volume", "0")))
            executed_cost = Decimal(str(order.get("executed_cost", "0")))
            fee_quote = Decimal(str(order.get("fee_quote", "0")))
            avg_entry = Decimal(str(state["avg_entry_price"]))
            pnl = (executed_cost - fee_quote) - (avg_entry * executed_volume)
            state["realized_pnl_today"] = str(Decimal(str(state.get("realized_pnl_today", "0"))) + pnl)
            if config is not None:
                update_protections_after_sell(config, state, pnl)
        state["avg_entry_price"] = None
        return
    if side != "buy":
        return

    volume = Decimal(str(order.get("executed_volume") or order.get("volume", "0")))
    quote_to_spend = Decimal(str(order.get("executed_cost") or order.get("quote_to_spend", "0")))
    fee_quote = Decimal(str(order.get("fee_quote", "0")))
    if volume <= 0 or quote_to_spend <= 0:
        return

    estimated_entry = (quote_to_spend + fee_quote) / volume
    if prior_base_balance > 0 and state.get("avg_entry_price"):
        previous_entry = Decimal(str(state["avg_entry_price"]))
        blended = ((previous_entry * prior_base_balance) + quote_to_spend + fee_quote) / (prior_base_balance + volume)
        state["avg_entry_price"] = str(blended)
    else:
        state["avg_entry_price"] = str(estimated_entry)


def execute_live_order(
    config: BotConfig,
    client: KrakenClient,
    signal: Signal,
    quote_balance: Decimal,
    base_balance: Decimal,
    validate_orders: bool,
    size_multiplier: Decimal = Decimal("1"),
) -> dict[str, Any]:
    rules = client.asset_pair_rules(config.pair)
    volume_decimals = int(rules.get("lot_decimals", 8))
    min_volume = Decimal(str(rules.get("ordermin", "0")))

    if signal.side == "buy":
        current_position_quote = base_balance * signal.price
        quote_to_spend = planned_buy_quote_size(config, quote_balance, current_position_quote, size_multiplier)
        fee_adjusted_quote = quote_to_spend * (Decimal("1") - side_cost_rate(config))
        volume = quantize_volume(fee_adjusted_quote / signal.price, volume_decimals)
        if volume < min_volume:
            return {"status": "skipped", "reason": "below_min_order", "volume": str(volume), "min_volume": str(min_volume)}
        result = client.add_market_order(config.pair, "buy", volume, validate_orders)
        return {
            "status": "validated" if validate_orders else "submitted",
            "side": "buy",
            "volume": str(volume),
            "quote_to_spend": str(quote_to_spend),
            "estimated_fee_quote": str(quote_to_spend * side_cost_rate(config)),
            "result": result,
        }

    if signal.side == "sell":
        volume = quantize_volume(base_balance, volume_decimals)
        if volume < min_volume:
            return {"status": "skipped", "reason": "below_min_order", "volume": str(volume), "min_volume": str(min_volume)}
        result = client.add_market_order(config.pair, "sell", volume, validate_orders)
        return {"status": "validated" if validate_orders else "submitted", "side": "sell", "volume": str(volume), "result": result}

    return {"status": "skipped", "reason": "hold"}


def execution_priced_signal(signal: Signal, ticker: Ticker) -> Signal:
    if signal.side == "buy" and ticker.ask > 0:
        return replace(signal, price=ticker.ask)
    if signal.side == "sell" and ticker.bid > 0:
        return replace(signal, price=ticker.bid)
    return signal


def reconcile_live_order(client: KrakenClient, order: dict[str, Any]) -> dict[str, Any]:
    if order.get("status") != "submitted":
        return order
    txids = order.get("result", {}).get("txid") if isinstance(order.get("result"), dict) else None
    if isinstance(txids, str):
        txids = [txids]
    if not txids:
        return order

    reconciled = dict(order)
    query: dict[str, Any] = {}
    for attempt in range(3):
        try:
            query = client.private("QueryOrders", {"txid": ",".join(txids)})
        except Exception as exc:
            reconciled["reconcile_error"] = f"{type(exc).__name__}: {exc}"
            return reconciled
        if len(txids) != 1 or txids[0] in query or attempt == 2:
            break
        time.sleep(1)

    reconciled["query_order"] = query
    if len(txids) == 1 and txids[0] in query:
        detail = query[txids[0]]
        exchange_status = str(detail.get("status") or "")
        if exchange_status:
            reconciled["exchange_status"] = exchange_status
        if exchange_status in {"closed", "canceled", "expired"}:
            reconciled["status"] = exchange_status
        for source_key, target_key in (
            ("price", "executed_price"),
            ("cost", "executed_cost"),
            ("fee", "fee_quote"),
            ("vol_exec", "executed_volume"),
        ):
            if detail.get(source_key) not in {None, ""}:
                reconciled[target_key] = str(detail[source_key])
        try:
            requested_volume = Decimal(str(detail.get("vol") or order.get("volume") or "0"))
            executed_volume = Decimal(str(detail.get("vol_exec") or "0"))
        except Exception:
            requested_volume = Decimal("0")
            executed_volume = Decimal("0")
        if requested_volume > 0 and executed_volume >= requested_volume and detail.get("cost") not in {None, ""}:
            reconciled["status"] = "closed"
    return reconciled


def plan_live_order(
    config: BotConfig,
    client: KrakenClient,
    signal: Signal,
    quote_balance: Decimal,
    base_balance: Decimal,
    size_multiplier: Decimal = Decimal("1"),
) -> dict[str, Any]:
    rules = client.asset_pair_rules(config.pair)
    volume_decimals = int(rules.get("lot_decimals", 8))
    min_volume = Decimal(str(rules.get("ordermin", "0")))

    if signal.side == "buy":
        current_position_quote = base_balance * signal.price
        quote_to_spend = planned_buy_quote_size(config, quote_balance, current_position_quote, size_multiplier)
        fee_adjusted_quote = quote_to_spend * (Decimal("1") - side_cost_rate(config))
        volume = quantize_volume(fee_adjusted_quote / signal.price, volume_decimals) if signal.price > 0 else Decimal("0")
        if volume < min_volume:
            return {
                "status": "skipped",
                "reason": "below_min_order",
                "side": "buy",
                "volume": str(volume),
                "min_volume": str(min_volume),
                "quote_to_spend": str(quote_to_spend),
            }
        return {
            "status": "planned",
            "side": "buy",
            "pair": config.pair,
            "volume": str(volume),
            "quote_to_spend": str(quote_to_spend),
            "estimated_fee_quote": str(quote_to_spend * side_cost_rate(config)),
            "validate_only": False,
        }

    if signal.side == "sell":
        volume = quantize_volume(base_balance, volume_decimals)
        if volume < min_volume:
            return {"status": "skipped", "reason": "below_min_order", "side": "sell", "volume": str(volume), "min_volume": str(min_volume)}
        return {
            "status": "planned",
            "side": "sell",
            "pair": config.pair,
            "volume": str(volume),
            "validate_only": False,
        }

    return {"status": "skipped", "reason": "hold"}


def maybe_llm_veto(config: BotConfig, state: dict[str, Any], event: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    if not config.llm.enabled or event.get("signal") == "hold":
        return False, {"status": "disabled_or_hold"}
    if int(state.get("llm_calls_today", 0)) >= config.llm.max_calls_per_day:
        return False, {"status": "skipped", "reason": "llm_daily_limit"}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return config.llm.block_when_unavailable, {"status": "unavailable", "reason": "missing_OPENAI_API_KEY"}

    prompt = {
        "task": "You are a risk reviewer for a tiny crypto spot-trading bot. Return only compact JSON.",
        "rules": [
            "You may veto a proposed trade if market conditions look unsafe.",
            "You may not propose larger size, leverage, derivatives, or a new trade.",
            "JSON keys: risk_score integer 0-100, veto boolean, reason short string.",
        ],
        "event": event,
    }
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": config.llm.model,
                    "input": json.dumps(prompt, sort_keys=True),
                    "text": {"format": {"type": "json_object"}},
                },
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return config.llm.block_when_unavailable, {"status": "error", "reason": str(exc)}

    state["llm_calls_today"] = int(state.get("llm_calls_today", 0)) + 1
    advice = parse_openai_json(payload)
    risk_score = int(advice.get("risk_score", 0))
    veto = bool(advice.get("veto", False)) or risk_score >= config.llm.veto_risk_score
    return veto, {"status": "ok", "advice": advice}


def parse_openai_json(payload: dict[str, Any]) -> dict[str, Any]:
    if isinstance(payload.get("output_text"), str):
        text = payload["output_text"]
    else:
        text = ""
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    text += content["text"]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"risk_score": 0, "veto": False, "reason": "llm_returned_non_json"}
    if not isinstance(parsed, dict):
        return {"risk_score": 0, "veto": False, "reason": "llm_returned_non_object"}
    return parsed


def run_once(config: BotConfig, validate_orders: bool) -> dict[str, Any]:
    ensure_live_ack(config)
    state = load_state(config)
    reset_daily_if_needed(state)
    reconnect_gap_detected, cycle_status = mark_cycle_start(config, state)

    client = KrakenClient(os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET"))
    try:
        config, rotation_event = select_rotation_pair(config, client, state)
        quote_balance, base_balance = current_balances(config, client, state)
        ticker = client.ticker(config.pair)
        candles = client.candles(config.pair, config.candle_interval_minutes)
        effective_base_balance = base_balance
        dust_position: dict[str, str] | None = None
        if config.mode == "live" and base_balance > 0:
            try:
                rules = client.asset_pair_rules(config.pair)
                min_volume = Decimal(str(rules.get("ordermin", "0")))
            except Exception:
                min_volume = Decimal("0")
            if min_volume > 0 and base_balance < min_volume:
                dust_position = {"base_balance": str(base_balance), "min_volume": str(min_volume)}
                effective_base_balance = Decimal("0")
                state["avg_entry_price"] = None
        regime: dict[str, Any] | None = None
        if config.multi_timeframe.enabled:
            try:
                regime = market_regime(config, client.candles(config.pair, config.multi_timeframe.interval_minutes))
            except Exception as exc:
                regime = {
                    "enabled": True,
                    "interval_minutes": config.multi_timeframe.interval_minutes,
                    "regime": "unavailable",
                    "error": f"{type(exc).__name__}: {exc}",
                }
        avg_entry = Decimal(str(state["avg_entry_price"])) if state.get("avg_entry_price") else None
        signal = make_signal(config, candles, effective_base_balance, avg_entry)
        pnl_snapshot = position_pnl_snapshot(config, quote_balance, base_balance, signal.price, avg_entry)
        equity_guard = update_equity_guard(config, state, pnl_snapshot)
        sprint_goal = sprint_goal_context(config, pnl_snapshot)
        research_snapshot = build_research_snapshot(config.raw.get("research", {}), config.research_cache_file)
        research_compact = compact_research_snapshot(research_snapshot)
        research_size_multiplier = position_size_multiplier_from_research(config, research_snapshot)
        size_multiplier = research_size_multiplier
        downside = downside_bias(config, candles, regime)
        allowed, risk_reason = risk_allows_trade(config, state, signal)
        entry_window_open = within_trading_window(config)
        reconnect_observe = consume_reconnect_observe_cycle(state)
        if allowed and reconnect_observe:
            allowed = False
            risk_reason = "reconnect_observe"
        if allowed and signal.side == "buy" and sprint_goal.get("target_reached") and sprint_goal.get("stop_on_target"):
            allowed = False
            risk_reason = "sprint_goal_reached"
        if allowed and signal.side == "buy" and not entry_window_open:
            allowed = False
            risk_reason = "outside_entry_window"
        market_allowed, market_reason, market_metrics = market_filter_allows(config, ticker, candles)
        advanced = advanced_strategy_metrics(config, candles)
        fee_edge = fee_edge_check(config, signal)
        if allowed and not market_allowed:
            allowed = False
            risk_reason = market_reason
        if allowed and signal.side == "buy" and not fee_edge["allowed"]:
            allowed = False
            risk_reason = "fee_edge_too_small"
        if allowed and signal.side == "buy" and advanced.get("risk_model", {}).get("risk_block"):
            allowed = False
            risk_reason = "var_es_too_high"
        if allowed and signal.side == "buy" and research_snapshot.get("block_buys"):
            allowed = False
            risk_reason = "research_risk_off"
        if allowed and signal.side == "buy" and not higher_timeframe_allows_buy(config, regime):
            allowed = False
            risk_reason = "higher_timeframe_not_aligned"
        if (
            allowed
            and signal.side == "buy"
            and config.downside.block_buys_when_downside
            and downside.get("action") == "open_or_hold_short"
        ):
            allowed = False
            risk_reason = "downside_bias_block"
        market_data: dict[str, tuple[Ticker, list[Candle]]] = {config.pair: (ticker, candles)}
        event: dict[str, Any] = {
            "mode": config.mode,
            "pair": config.pair,
            "signal": signal.side,
            "signal_reason": signal.reason,
            "risk": risk_reason,
            "cycle_status": cycle_status,
            "reconnect_gap_detected": reconnect_gap_detected,
            "reconnect_observe_cycles_left": state.get("reconnect_observe_cycles_left", 0),
            "price": str(signal.price),
            "ticker_bid": str(ticker.bid),
            "ticker_ask": str(ticker.ask),
            "ticker_last": str(ticker.last),
            "market_metrics": market_metrics,
            "fee_edge_check": fee_edge,
            "advanced_strategy": advanced,
            "fast_sma": str(signal.fast_sma),
            "slow_sma": str(signal.slow_sma),
            "rsi": str(signal.rsi),
            "score": signal.score,
            "macd_histogram": str(signal.macd_histogram),
            "bollinger_z": str(signal.bollinger_z),
            "momentum_pct": str(signal.momentum_pct),
            "quote_balance": str(quote_balance),
            "base_balance": str(base_balance),
            "effective_base_balance": str(effective_base_balance),
            "dust_position": dust_position,
            "pnl_snapshot": pnl_snapshot,
            "equity_guard": equity_guard,
            "sprint_goal": sprint_goal,
            "research": research_compact,
            "market_regime": regime,
            "downside_bias": downside,
            "position_size_multiplier": str(size_multiplier),
            "research_position_size_multiplier": str(research_size_multiplier),
            "entry_window_open": entry_window_open,
            "entry_windows_utc": config.execution.trading_windows_utc,
            "rotation": rotation_event,
        }
        if len(config.scan_pairs) > 1:
            for pair in config.scan_pairs:
                if pair == config.pair:
                    continue
                try:
                    market_data[pair] = (client.ticker(pair), client.candles(pair, config.candle_interval_minutes))
                except Exception as exc:
                    event.setdefault("scan_errors", {})[pair] = f"{type(exc).__name__}: {exc}"
            event["scan_candidates"] = scan_pair_candidates(config, market_data)
        market_radar = build_market_radar(config, client, market_data)
        event["market_radar"] = market_radar
        if (
            allowed
            and signal.side == "buy"
            and market_radar.get("risk_off")
        ):
            allowed = False
            risk_reason = "market_breadth_risk_off"
            event["risk"] = risk_reason
        ai_plan = build_ai_plan(config, state, event)
        size_multiplier, ai_multiplier_event = apply_ai_plan_multiplier(config, research_size_multiplier, ai_plan, event)
        kelly_multiplier = Decimal(str(advanced.get("risk_model", {}).get("kelly_multiplier", "1")))
        size_multiplier = min(config.ai_plan.max_risk_multiplier, max(Decimal("0"), size_multiplier * kelly_multiplier))
        event["ai_plan"] = compact_ai_plan(ai_plan)
        event["ai_position_size"] = ai_multiplier_event
        event["advanced_position_size"] = {
            "kelly_multiplier": str(kelly_multiplier),
            "final_multiplier": str(size_multiplier),
        }
        event["position_size_multiplier"] = str(size_multiplier)
        if allowed and signal.side == "buy" and ai_plan.get("should_block_buys"):
            allowed = False
            risk_reason = "ai_plan_risk_off"
            event["risk"] = risk_reason
        if not allowed:
            override_allowed, override_event = ai_override_decision(config, signal, risk_reason, ai_plan, event)
            event["ai_override"] = override_event
            if override_allowed:
                candidate_signal = ai_buy_signal_from_hold(signal, ai_plan)
                hard_allowed, hard_reason = risk_allows_trade(config, state, candidate_signal)
                if hard_allowed:
                    signal = candidate_signal
                    allowed = True
                    risk_reason = "ai_override_approved"
                    event["signal"] = signal.side
                    event["signal_reason"] = signal.reason
                    event["risk"] = risk_reason
                else:
                    event["ai_override"]["approved"] = False
                    event["ai_override"]["reason"] = f"post_override_hard_block:{hard_reason}"
        if config.shadow.enabled:
            event["shadow"] = shadow_cycle(config, ticker, candles, market_metrics, research_snapshot, regime, downside, size_multiplier)
        if allowed:
            llm_veto, llm_event = maybe_llm_veto(config, state, event)
            event["llm"] = llm_event
            if llm_veto:
                allowed = False
                risk_reason = "llm_veto"
                event["risk"] = risk_reason

        if allowed and config.mode == "paper":
            event["order"] = execute_paper_order(config, state, signal, size_multiplier)
        elif allowed and config.mode == "live":
            live_signal = execution_priced_signal(signal, ticker)
            event["execution_price"] = str(live_signal.price)
            order_plan = plan_live_order(config, client, live_signal, quote_balance, base_balance, size_multiplier)
            if requires_manual_approval(config, signal, order_plan) and not validate_orders:
                existing = read_pending_order(config)
                if pending_is_active(existing):
                    event["order"] = {"status": "skipped", "reason": "pending_order_exists", "pending_order": existing}
                else:
                    if order_plan["status"] == "planned":
                        pending = write_pending_order(config, event, order_plan)
                        event["order"] = {"status": "awaiting_manual_approval", "pending_order": pending}
                        if config.execution.notify_on_signal:
                            notify_channels(
                                config,
                                "Kraken trade approval needed",
                                f"{signal.side.upper()} {config.pair} score={signal.score}. Run: python control.py approve",
                                financial=True,
                            )
                    else:
                        event["order"] = order_plan
            else:
                event["order"] = execute_live_order(
                    config,
                    client,
                    live_signal,
                    quote_balance,
                    base_balance,
                    validate_orders,
                    size_multiplier,
                )
                if event["order"]["status"] == "submitted" and not validate_orders:
                    event["order"] = reconcile_live_order(client, event["order"])
            if event["order"]["status"] in {"submitted", "closed"}:
                update_live_position_estimate(state, event["order"], base_balance, config)
                mark_trade(state)
                if config.execution.notify_on_fill:
                    notify_channels(
                        config,
                        "Kraken order processed",
                        (
                            f"{event['order'].get('side', signal.side).upper()} {config.pair}: "
                            f"{event['order'].get('executed_volume') or event['order'].get('volume', '')} "
                            f"status={event['order'].get('status')}"
                        ),
                        financial=True,
                    )
        else:
            event["order"] = {"status": "skipped", "reason": risk_reason}

        log_event(config, event)
        maybe_send_daily_report(config, state)
        save_state(config, state)
        return event
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Conservative Kraken spot auto trader")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--env-file", type=Path, default=Path("secrets.env"))
    parser.add_argument("--once", action="store_true", help="run one decision cycle and exit")
    parser.add_argument("--fail-fast", action="store_true", help="exit on the first recoverable loop error")
    parser.add_argument("--validate-orders", action="store_true", help="ask Kraken to validate orders without placing them")
    args = parser.parse_args()

    load_env_file(args.env_file)
    config = load_config(args.config)
    while True:
        try:
            event = run_once(config, args.validate_orders)
        except Exception as exc:
            event = {
                "mode": config.mode,
                "pair": config.pair,
                "error": type(exc).__name__,
                "message": str(exc),
                "next_retry_seconds": config.poll_seconds,
            }
            log_event(config, event)
            print(json.dumps(event, indent=2, sort_keys=True))
            if args.once or args.fail_fast:
                return 1
        else:
            print(json.dumps(event, indent=2, sort_keys=True))
        if args.once:
            return 0
        time.sleep(config.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
