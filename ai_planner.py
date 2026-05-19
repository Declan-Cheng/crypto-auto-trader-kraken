#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from openai_runtime import resolve_openai_runtime_credentials


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def plan_age_minutes(plan: dict[str, Any]) -> float | None:
    try:
        ts = datetime.fromisoformat(str(plan["created_at"]))
    except Exception:
        return None
    return max((datetime.now(timezone.utc) - ts).total_seconds() / 60, 0)


def plan_is_fresh(plan: dict[str, Any], refresh_minutes: int) -> bool:
    age = plan_age_minutes(plan)
    return age is not None and age < refresh_minutes


def expected_market_radar_pairs(config: Any) -> int:
    if not config.market_radar.enabled:
        return 0
    primary_pairs: list[str] = []
    context_pairs: list[str] = []
    for pair in config.market_radar.pairs:
        if pair not in primary_pairs:
            primary_pairs.append(pair)
    for pair in config.market_radar.context_pairs:
        if pair not in context_pairs:
            context_pairs.append(pair)
    if config.market_radar.max_pairs_per_cycle <= 0:
        return len(primary_pairs) + len(context_pairs)
    context_budget = min(len(context_pairs), config.market_radar.max_pairs_per_cycle)
    primary_budget = max(config.market_radar.max_pairs_per_cycle - context_budget, 0)
    return min(len(primary_pairs), primary_budget) + context_budget


def plan_config_signature(config: Any) -> dict[str, Any]:
    execution = getattr(config, "execution", None)
    sprint_goal = config.raw.get("sprint_goal", {}) if isinstance(getattr(config, "raw", {}), dict) else {}
    return {
        "ai_max_risk_multiplier": str(config.ai_plan.max_risk_multiplier),
        "ai_max_aggressive_order_quote": str(config.ai_plan.max_aggressive_order_quote),
        "risk_max_order_quote": str(config.risk.max_order_quote),
        "risk_max_position_quote": str(config.risk.max_position_quote),
        "risk_reserve_quote_balance": str(config.risk.reserve_quote_balance),
        "risk_fee_bps": str(config.risk.fee_bps),
        "risk_min_net_profit_bps": str(config.risk.min_net_profit_bps),
        "trading_windows_utc": list(getattr(execution, "trading_windows_utc", [])),
        "aggressive_trading_windows_local": list(getattr(execution, "aggressive_trading_windows_local", [])),
        "sprint_goal": {
            "enabled": bool(sprint_goal.get("enabled", False)) if isinstance(sprint_goal, dict) else False,
            "label": str(sprint_goal.get("label", "")) if isinstance(sprint_goal, dict) else "",
            "target_equity_quote": str(sprint_goal.get("target_equity_quote", "")) if isinstance(sprint_goal, dict) else "",
            "deadline_local_date": str(sprint_goal.get("deadline_local_date", "")) if isinstance(sprint_goal, dict) else "",
        },
    }


def plan_matches_features(config: Any, plan: dict[str, Any]) -> bool:
    gates = plan.get("gates") if isinstance(plan.get("gates"), dict) else {}
    if plan.get("source") == "openai" and plan.get("config_signature") != plan_config_signature(config):
        return False
    if plan.get("status") == "outside_entry_window":
        return False
    runtime = resolve_openai_runtime_credentials()
    if plan.get("source") == "quant_fallback" and bool(runtime.get("api_key")):
        ai_status = plan.get("ai_status") if isinstance(plan.get("ai_status"), dict) else {}
        reason = str(ai_status.get("reason") or "")
        retry_cached_reasons = {"all_models_failed", "openai_incomplete"}
        daily_limit_reasons = {"ai_plan_daily_limit", "ai_plan_daily_limit_forced_search"}
        if reason in retry_cached_reasons:
            age = plan_age_minutes(plan)
            if age is None or age >= min(15, config.ai_plan.refresh_interval_minutes):
                return False
        elif reason not in daily_limit_reasons:
            return False
    if plan.get("source") == "openai":
        allowed_models = [config.ai_plan.model, *getattr(config.ai_plan, "model_fallbacks", [])]
        if plan.get("model") not in allowed_models:
            return False
        if plan.get("reasoning_effort") != config.ai_plan.reasoning_effort:
            return False
        if bool(plan.get("web_search")) != bool(config.ai_plan.web_search):
            return False
        if bool(plan.get("force_web_search")) != bool(config.ai_plan.force_web_search):
            return False
    if config.market_radar.enabled:
        if "positive_breadth" not in gates:
            return False
        if int(gates.get("market_radar_pairs_requested") or -1) != expected_market_radar_pairs(config):
            return False
    return True


def summarize_history(config: Any) -> dict[str, Any]:
    events = read_jsonl_tail(config.trade_log, config.ai_plan.history_limit)
    shadow_events = read_jsonl_tail(config.shadow_log_file, min(config.ai_plan.history_limit, 500))
    signal_counts = Counter(str(event.get("signal")) for event in events if event.get("signal"))
    risk_counts = Counter(str(event.get("risk")) for event in events if event.get("risk"))
    orders = [event.get("order", {}) for event in events if isinstance(event.get("order"), dict)]
    filled_orders = [order for order in orders if order.get("status") in {"submitted", "closed", "filled_paper"}]
    equities = [decimal_or_zero(event.get("pnl_snapshot", {}).get("equity_quote") or event.get("quote_balance")) for event in events]
    equities = [equity for equity in equities if equity > 0]
    pnl_window = str(equities[-1] - equities[0]) if len(equities) >= 2 else "0"
    peak = max(equities) if equities else Decimal("0")
    drawdown = str(peak - equities[-1]) if equities else "0"
    shadow_equity = None
    short_equity = None
    if shadow_events:
        last_shadow = shadow_events[-1]
        shadow_equity = last_shadow.get("equity_quote")
        short = last_shadow.get("shadow_short") if isinstance(last_shadow.get("shadow_short"), dict) else {}
        short_equity = short.get("equity_quote")
    return {
        "events": len(events),
        "signal_counts": dict(signal_counts),
        "risk_counts": dict(risk_counts.most_common(8)),
        "filled_orders": len(filled_orders),
        "pnl_window_quote": pnl_window,
        "drawdown_window_quote": drawdown,
        "latest_shadow_equity": shadow_equity,
        "latest_shadow_short_equity": short_equity,
        "latest_error": next((event.get("message") for event in reversed(events) if event.get("error")), None),
    }


def quant_plan(config: Any, event: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    signal_score = int(event.get("score") or 0)
    regime = event.get("market_regime") if isinstance(event.get("market_regime"), dict) else {}
    regime_score = int(regime.get("score") or 0)
    research = event.get("research") if isinstance(event.get("research"), dict) else {}
    research_score = int(research.get("risk_score") or 0)
    spread_bps = decimal_or_zero(event.get("market_metrics", {}).get("spread_bps"))
    momentum_pct = decimal_or_zero(event.get("momentum_pct"))
    downside = event.get("downside_bias") if isinstance(event.get("downside_bias"), dict) else {}
    guard = event.get("equity_guard") if isinstance(event.get("equity_guard"), dict) else {}
    radar = event.get("market_radar") if isinstance(event.get("market_radar"), dict) else {}
    positive_breadth = decimal_or_zero(radar.get("positive_breadth", "0.5")) if radar.get("enabled") else Decimal("0.5")
    strong_negative_breadth = decimal_or_zero(radar.get("strong_negative_breadth", "0"))
    drawdown = decimal_or_zero(history.get("drawdown_window_quote"))

    confidence = Decimal("50")
    confidence += Decimal(signal_score * 5)
    confidence += Decimal(regime_score * 4)
    confidence += (positive_breadth - Decimal("0.5")) * Decimal("30")
    confidence -= strong_negative_breadth * Decimal("25")
    confidence += min(max(momentum_pct * Decimal("6"), Decimal("-12")), Decimal("12"))
    confidence -= Decimal(research_score) / Decimal("2")
    if spread_bps > 10:
        confidence -= min((spread_bps - Decimal("10")) / Decimal("2"), Decimal("15"))
    if drawdown > 0:
        confidence -= min(drawdown * Decimal("10"), Decimal("12"))
    if downside.get("action") == "open_or_hold_short":
        confidence -= Decimal("25")
    if guard.get("active"):
        confidence -= Decimal("40")
    confidence = max(Decimal("0"), min(Decimal("100"), confidence))

    should_block = (
        bool(research.get("block_buys"))
        or bool(guard.get("active"))
        or downside.get("action") == "open_or_hold_short"
        or bool(radar.get("risk_off"))
    )
    aggressive_gate = aggressive_gate_open(config, event, int(confidence))
    if should_block:
        action = "risk_off"
        multiplier = Decimal("0")
        forecast_direction = "risk_off"
    elif aggressive_gate:
        action = "aggressive_accumulate"
        multiplier = min(config.ai_plan.max_risk_multiplier, Decimal("1.5"))
        forecast_direction = "up"
    elif signal_score >= config.strategy.min_buy_score and regime.get("regime") == "uptrend":
        action = "standard_accumulate"
        multiplier = Decimal("1")
        forecast_direction = "up"
    else:
        action = "observe"
        multiplier = Decimal("0.75")
        forecast_direction = "range"

    return {
        "source": "quant",
        "created_at": now_iso(),
        "status": "ok",
        "config_signature": plan_config_signature(config),
        "action": action,
        "risk_multiplier": str(multiplier),
        "confidence": int(confidence),
        "should_block_buys": should_block,
        "forecast": {
            "horizon_hours": 6,
            "direction": forecast_direction,
            "rationale": (
                f"score={signal_score}, 15m_score={regime_score}, momentum={momentum_pct}, "
                f"research={research_score}, spread_bps={spread_bps}, breadth={positive_breadth}"
            ),
            "invalidation": "If research risk blocks buys, market breadth weakens, downside bias triggers, spread widens, or 15m trend breaks, reduce risk.",
        },
        "gates": {
            "aggressive_gate_open": aggressive_gate,
            "signal_score": signal_score,
            "regime_score": regime_score,
            "research_score": research_score,
            "spread_bps": str(spread_bps),
            "positive_breadth": str(positive_breadth),
            "strong_negative_breadth": str(strong_negative_breadth),
            "market_radar_pairs_requested": int(radar.get("pairs_requested") or 0),
            "market_radar_pairs_analyzed": int(radar.get("pairs_analyzed") or 0),
            "market_radar_risk_on": bool(radar.get("risk_on")),
            "market_radar_risk_off": bool(radar.get("risk_off")),
            "drawdown_window_quote": str(drawdown),
        },
        "history": history,
    }


def forced_search_block_plan(plan: dict[str, Any], reason: str) -> dict[str, Any]:
    blocked = dict(plan)
    blocked["source"] = "quant_fallback"
    blocked["action"] = "risk_off"
    blocked["risk_multiplier"] = "0"
    blocked["should_block_buys"] = True
    blocked["ai_status"] = {"status": "skipped", "reason": reason}
    forecast = dict(blocked.get("forecast") or {})
    forecast["direction"] = "risk_off"
    forecast["invalidation"] = "Forced web search is required before new buys; retry after OpenAI/search budget is available."
    blocked["forecast"] = forecast
    return blocked


def forced_search_unavailable_plan(plan: dict[str, Any], reason: str, ai_status: dict[str, Any] | None = None) -> dict[str, Any]:
    fallback = dict(plan)
    fallback["source"] = "quant_fallback"
    status = dict(ai_status or {})
    status.setdefault("status", "skipped")
    status["reason"] = reason
    status["force_web_search_failed"] = True
    fallback["ai_status"] = status
    forecast = dict(fallback.get("forecast") or {})
    existing = str(forecast.get("invalidation") or "").strip()
    note = "OpenAI/web search was unavailable; continue with the local quantified plan and retry AI search later."
    forecast["invalidation"] = f"{existing} {note}".strip() if existing else note
    fallback["forecast"] = forecast
    return fallback


def aggressive_gate_open(config: Any, event: dict[str, Any], confidence: int) -> bool:
    if not config.ai_plan.enabled or not config.ai_plan.allow_risk_increase:
        return False
    if confidence < config.ai_plan.min_confidence_to_increase:
        return False
    if int(event.get("score") or 0) < config.ai_plan.min_signal_score_to_increase:
        return False
    regime = event.get("market_regime") if isinstance(event.get("market_regime"), dict) else {}
    if regime.get("regime") != "uptrend" or int(regime.get("score") or 0) < config.ai_plan.min_regime_score_to_increase:
        return False
    research = event.get("research") if isinstance(event.get("research"), dict) else {}
    if bool(research.get("block_buys")) or int(research.get("risk_score") or 0) > config.ai_plan.max_research_score_to_increase:
        return False
    if decimal_or_zero(event.get("market_metrics", {}).get("spread_bps")) > config.ai_plan.max_spread_bps_to_increase:
        return False
    radar = event.get("market_radar") if isinstance(event.get("market_radar"), dict) else {}
    if radar.get("enabled"):
        if bool(radar.get("risk_off")):
            return False
        if decimal_or_zero(radar.get("positive_breadth")) < config.market_radar.min_positive_breadth_to_increase:
            return False
    downside = event.get("downside_bias") if isinstance(event.get("downside_bias"), dict) else {}
    if downside.get("action") == "open_or_hold_short":
        return False
    guard = event.get("equity_guard") if isinstance(event.get("equity_guard"), dict) else {}
    if guard.get("active"):
        return False
    return True


def hard_plan_block_present(event: dict[str, Any]) -> bool:
    guard = event.get("equity_guard") if isinstance(event.get("equity_guard"), dict) else {}
    if guard.get("active"):
        return True
    downside = event.get("downside_bias") if isinstance(event.get("downside_bias"), dict) else {}
    if downside.get("action") == "open_or_hold_short":
        return True
    radar = event.get("market_radar") if isinstance(event.get("market_radar"), dict) else {}
    if radar.get("risk_off"):
        return True
    return False


def openai_schema(max_multiplier: str) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": ["risk_off", "observe", "standard_accumulate", "aggressive_accumulate"]},
            "risk_multiplier": {"type": "number", "minimum": 0, "maximum": float(max_multiplier)},
            "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
            "should_block_buys": {"type": "boolean"},
            "override_program_rejection": {"type": "boolean"},
            "forecast_direction": {"type": "string", "enum": ["up", "down", "range", "risk_off"]},
            "market_direction": {"type": "string", "enum": ["bullish", "bearish", "range", "risk_off"]},
            "horizon_hours": {"type": "integer", "minimum": 1, "maximum": 48},
            "risk_math": {"type": "string"},
            "rationale": {"type": "string"},
            "invalidation": {"type": "string"},
            "override_rationale": {"type": "string"},
            "risk_notes": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        },
        "required": [
            "action",
            "risk_multiplier",
            "confidence",
            "should_block_buys",
            "override_program_rejection",
            "forecast_direction",
            "market_direction",
            "horizon_hours",
            "risk_math",
            "rationale",
            "invalidation",
            "override_rationale",
            "risk_notes",
        ],
    }


def extract_reasoning_summary(raw: dict[str, Any]) -> str | None:
    parts: list[str] = []
    for item in raw.get("output", []):
        if item.get("type") != "reasoning":
            continue
        for summary in item.get("summary", []):
            text = summary.get("text") or summary.get("summary_text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts)[:1200] if parts else None


def count_hosted_tool_calls(raw: dict[str, Any], tool_type: str) -> int:
    return sum(1 for item in raw.get("output", []) if item.get("type") == tool_type)


def parse_responses_stream(raw_text: str) -> dict[str, Any]:
    text_parts: list[str] = []
    output_items: list[dict[str, Any]] = []
    final_response: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    for line in raw_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line.removeprefix("data:").strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except Exception:
            continue
        event_type = str(event.get("type") or "")
        if "output_text.delta" in event_type and isinstance(event.get("delta"), str):
            text_parts.append(event["delta"])
        elif event_type == "response.output_item.done" and isinstance(event.get("item"), dict):
            output_items.append(event["item"])
        elif event_type == "response.completed" and isinstance(event.get("response"), dict):
            final_response = event["response"]
        elif event_type in {"response.failed", "response.incomplete"}:
            error_payload = event
    if final_response is None:
        final_response = {"status": "completed" if text_parts or output_items else "unknown"}
    if text_parts:
        final_response["output_text"] = "".join(text_parts)
    if output_items and not final_response.get("output"):
        final_response["output"] = output_items
    if error_payload and not text_parts:
        final_response["error"] = error_payload
    return final_response


def call_openai_plan(config: Any, quant: dict[str, Any], event: dict[str, Any], history: dict[str, Any]) -> dict[str, Any]:
    runtime = resolve_openai_runtime_credentials()
    auth_fields = {"auth_mode": runtime.get("auth_mode"), "auth_source": runtime.get("source")}
    api_key = str(runtime.get("api_key") or "")
    if not api_key:
        return {"status": "unavailable", "reason": "missing_openai_runtime_credentials", **auth_fields}
    base_url = str(runtime.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    default_headers = dict(runtime.get("default_headers") or {})
    instructions = (
        "You are the chief trading brain for a tiny Kraken spot bot. Think like an aggressive but numerate professional risk taker. "
        "Form a directional view, calculate net expected edge, size it, and challenge weak local rules when the evidence is strong. "
        "Do not recommend leverage, margin, derivatives, shorting live funds, or withdrawals. Return only JSON matching the requested schema."
    )

    prompt = {
        "role": "You are the chief trading brain for a tiny Kraken spot bot. Think like an aggressive but numerate professional risk taker: form a directional view, calculate net expected edge, size it, and challenge weak local rules when the evidence is strong.",
        "hard_rules": [
            "Do not recommend leverage, margin, derivatives, shorting live funds, or withdrawals.",
            "Do not exceed the configured maximum risk multiplier.",
            "Never override exchange validation, account balance, max position, daily loss, equity guard, wide spread, reconnect observe, downside-bias block, or a sell/stop-loss exit.",
            "If sprint_goal is enabled, treat it as an aggressive return objective, but never bypass fee-edge math, max position, daily drawdown, or exchange/account constraints to chase it.",
            "You have top decision authority over softer program rejections such as hold_signal, research_risk_off, or higher_timeframe_not_aligned when confidence is high and the market evidence supports a small spot buy.",
            "Before approving risk, calculate expected edge net of estimated fees, slippage, spread, current position PnL, stop distance, available CAD, and max order/position caps. Put the visible arithmetic summary in risk_math.",
            "Use web search every time when force_web_search is enabled. Keep the search concise and focused on current crypto market, macro, exchange-status, and asset-specific risk evidence.",
            "Return only JSON matching the schema. Provide a concise visible rationale and override_rationale, not hidden chain of thought.",
        ],
        "config": {
            "authority_mode": getattr(config.ai_plan, "authority_mode", "advisory"),
            "max_risk_multiplier": str(config.ai_plan.max_risk_multiplier),
            "max_aggressive_order_quote": str(config.ai_plan.max_aggressive_order_quote),
            "min_confidence_to_increase": config.ai_plan.min_confidence_to_increase,
            "min_signal_score_to_increase": config.ai_plan.min_signal_score_to_increase,
            "min_regime_score_to_increase": config.ai_plan.min_regime_score_to_increase,
            "max_research_score_to_increase": config.ai_plan.max_research_score_to_increase,
            "allow_decision_override": config.ai_plan.allow_decision_override,
            "force_web_search": config.ai_plan.force_web_search,
            "override_min_confidence": config.ai_plan.override_min_confidence,
            "override_min_signal_score": config.ai_plan.override_min_signal_score,
            "override_max_research_score": config.ai_plan.override_max_research_score,
            "override_min_positive_breadth": str(config.ai_plan.override_min_positive_breadth),
        },
        "quant_plan": quant,
        "latest_event": {
            key: event.get(key)
            for key in (
                "signal",
                "signal_reason",
                "score",
                "price",
                "rsi",
                "macd_histogram",
                "bollinger_z",
                "momentum_pct",
                "market_metrics",
                "market_regime",
                "research",
                "downside_bias",
                "market_radar",
                "equity_guard",
                "sprint_goal",
                "pnl_snapshot",
                "fee_edge_check",
                "scan_candidates",
            )
        },
        "history": history,
    }
    tools: list[dict[str, Any]] = []
    if config.ai_plan.web_search:
        tools.append({"type": "web_search", "search_context_size": config.ai_plan.web_search_context_size})
    models_to_try: list[str] = []
    for model in [config.ai_plan.model, *getattr(config.ai_plan, "model_fallbacks", [])]:
        if model and model not in models_to_try:
            models_to_try.append(model)
    input_payload = [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": json.dumps(prompt, ensure_ascii=False, sort_keys=True)}],
        }
    ]
    request_json = {
        "instructions": instructions,
        "input": input_payload,
        "store": False,
        "max_output_tokens": config.ai_plan.max_output_tokens,
        "reasoning": {
            "effort": config.ai_plan.reasoning_effort,
            "summary": config.ai_plan.reasoning_summary,
        },
        "text": {
            "format": {
                "type": "json_schema",
                "name": "chief_trader_plan",
                "schema": openai_schema(str(config.ai_plan.max_risk_multiplier)),
                "strict": True,
            },
            "verbosity": "low",
        },
    }
    if tools:
        request_json["tools"] = tools
        request_json["tool_choice"] = {"type": "web_search"} if config.ai_plan.force_web_search else "auto"
        request_json["max_tool_calls"] = max(1, config.ai_plan.max_web_search_calls)
        request_json["include"] = ["web_search_call.action.sources"]
    if runtime.get("auth_mode") == "chatgpt":
        request_json["stream"] = True
        request_json.pop("max_output_tokens", None)
        request_json.pop("max_tool_calls", None)
        request_json.pop("include", None)
    errors: dict[str, str] = {}
    try:
        with httpx.Client(timeout=45) as client:
            for model in models_to_try:
                payload = dict(request_json)
                payload["model"] = model
                try:
                    response = client.post(
                        f"{base_url}/responses",
                        headers={
                            **default_headers,
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    )
                    response.raise_for_status()
                    data = parse_responses_stream(response.text) if payload.get("stream") else response.json()
                    if data.get("status") == "incomplete":
                        details = json.dumps(data.get("incomplete_details"), ensure_ascii=False)[:500]
                        errors[model] = f"incomplete: {details}"
                        continue
                    return {
                        "status": "ok",
                        "model": model,
                        "raw": data,
                        "auth_mode": runtime.get("auth_mode"),
                        "auth_source": runtime.get("source"),
                    }
                except httpx.HTTPStatusError as exc:
                    errors[model] = f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
                    if exc.response.status_code in {400, 403, 404}:
                        continue
                    return {"status": "error", "reason": errors[model], "model_errors": errors, **auth_fields}
                except Exception as exc:
                    errors[model] = f"{type(exc).__name__}: {exc}"
                    continue
            return {"status": "error", "reason": "all_models_failed", "model_errors": errors, **auth_fields}
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}", **auth_fields}


def parse_openai_json(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("raw", payload)
    if raw.get("status") == "incomplete":
        return {"status": "error", "reason": "openai_incomplete", "incomplete_details": raw.get("incomplete_details")}
    if raw.get("error"):
        return {"status": "error", "reason": "openai_response_error", "error": raw.get("error")}
    if isinstance(raw.get("output_text"), str):
        text = raw["output_text"]
    else:
        text = ""
        for item in raw.get("output", []):
            for content in item.get("content", []):
                if content.get("type") in {"output_text", "text"} and isinstance(content.get("text"), str):
                    text += content["text"]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except Exception:
                return {"status": "error", "reason": "openai_returned_non_json", "text_excerpt": text[:500]}
        else:
            return {"status": "error", "reason": "openai_returned_non_json", "text_excerpt": text[:500], "output_types": [item.get("type") for item in raw.get("output", [])]}
    if not isinstance(parsed, dict):
        return {"status": "error", "reason": "openai_returned_non_object"}
    parsed["status"] = "ok"
    if payload.get("model"):
        parsed["model"] = payload.get("model")
    if payload.get("auth_mode"):
        parsed["auth_mode"] = payload.get("auth_mode")
    if payload.get("auth_source"):
        parsed["auth_source"] = payload.get("auth_source")
    parsed["reasoning_summary"] = extract_reasoning_summary(raw)
    parsed["web_search_calls"] = count_hosted_tool_calls(raw, "web_search_call")
    return parsed


def chief_authority_allows_soft_override(config: Any, plan: dict[str, Any], event: dict[str, Any]) -> bool:
    if getattr(config.ai_plan, "authority_mode", "advisory") != "chief":
        return False
    if plan.get("source") != "openai" or plan.get("should_block_buys"):
        return False
    if hard_plan_block_present(event):
        return False
    if int(plan.get("confidence") or 0) < config.ai_plan.override_min_confidence:
        return False
    forecast = plan.get("forecast") if isinstance(plan.get("forecast"), dict) else {}
    if plan.get("action") not in {"standard_accumulate", "aggressive_accumulate"} or forecast.get("direction") != "up":
        return False
    return True


def sanitize_plan(config: Any, quant: dict[str, Any], ai_payload: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    if ai_payload.get("status") != "ok":
        plan = dict(quant)
        plan["source"] = "quant_fallback"
        plan["ai_status"] = ai_payload
        return plan

    confidence = int(ai_payload.get("confidence", 0))
    requested = decimal_or_zero(ai_payload.get("risk_multiplier"))
    quant_multiplier = decimal_or_zero(quant.get("risk_multiplier"))
    gate = aggressive_gate_open(config, event, confidence)
    ai_override = bool(ai_payload.get("override_program_rejection"))
    quant_hard_block = hard_plan_block_present(event)
    if ai_payload.get("should_block_buys") or quant_hard_block or (quant.get("should_block_buys") and not ai_override):
        multiplier = Decimal("0")
        action = "risk_off"
    elif gate:
        multiplier = min(requested, config.ai_plan.max_risk_multiplier)
        action = str(ai_payload.get("action") or quant.get("action"))
    elif getattr(config.ai_plan, "authority_mode", "advisory") == "chief" and ai_override and confidence >= config.ai_plan.override_min_confidence:
        multiplier = min(requested, config.ai_plan.max_risk_multiplier)
        action = str(ai_payload.get("action") or "standard_accumulate")
    elif ai_override and confidence >= config.ai_plan.override_min_confidence:
        multiplier = min(requested, Decimal("1"))
        action = str(ai_payload.get("action") or "standard_accumulate")
    else:
        multiplier = min(requested, quant_multiplier, Decimal("1"))
        action = "observe" if str(ai_payload.get("action")) == "aggressive_accumulate" else str(ai_payload.get("action") or quant.get("action"))
    should_block = bool(ai_payload.get("should_block_buys")) or quant_hard_block or (bool(quant.get("should_block_buys")) and not ai_override)

    return {
        "source": "openai",
        "created_at": now_iso(),
        "status": "ok",
        "model": ai_payload.get("model") or config.ai_plan.model,
        "auth_mode": ai_payload.get("auth_mode"),
        "auth_source": ai_payload.get("auth_source"),
        "config_signature": plan_config_signature(config),
        "reasoning_effort": config.ai_plan.reasoning_effort,
        "web_search": config.ai_plan.web_search,
        "force_web_search": config.ai_plan.force_web_search,
        "action": action,
        "risk_multiplier": str(max(Decimal("0"), multiplier)),
        "confidence": confidence,
        "should_block_buys": should_block,
        "override_program_rejection": ai_override,
        "forecast": {
            "horizon_hours": int(ai_payload.get("horizon_hours", 6)),
            "direction": ai_payload.get("forecast_direction", "range"),
            "market_direction": ai_payload.get("market_direction", "range"),
            "risk_math": str(ai_payload.get("risk_math", ""))[:800],
            "rationale": str(ai_payload.get("rationale", ""))[:600],
            "invalidation": str(ai_payload.get("invalidation", ""))[:600],
            "override_rationale": str(ai_payload.get("override_rationale", ""))[:600],
        },
        "risk_notes": list(ai_payload.get("risk_notes", []))[:5],
        "reasoning_summary": ai_payload.get("reasoning_summary"),
        "web_search_calls": ai_payload.get("web_search_calls", 0),
        "gates": {**dict(quant.get("gates", {})), "aggressive_gate_open": gate},
        "quant_plan": quant,
    }


def build_ai_plan(config: Any, state: dict[str, Any], event: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
    existing = load_json(config.ai_plan_file, {})
    if not force and existing and plan_is_fresh(existing, config.ai_plan.refresh_interval_minutes) and plan_matches_features(config, existing):
        existing = dict(existing)
        existing["status"] = "cached"
        existing["age_minutes"] = round(plan_age_minutes(existing) or 0, 2)
        return existing

    history = summarize_history(config)
    quant = quant_plan(config, event, history)
    if not force and event.get("entry_window_open") is False:
        plan = forced_search_block_plan(quant, "outside_entry_window_no_search_spend")
        plan["status"] = "outside_entry_window"
        write_json(config.ai_plan_file, plan)
        append_jsonl(config.ai_plan_log_file, plan)
        return plan
    if not config.ai_plan.enabled:
        plan = dict(quant)
        plan["status"] = "disabled"
        write_json(config.ai_plan_file, plan)
        append_jsonl(config.ai_plan_log_file, plan)
        return plan

    current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("ai_plan_day") != current_day:
        state["ai_plan_day"] = current_day
        state["ai_plan_calls_today"] = 0
    if int(state.get("ai_plan_calls_today", 0)) >= config.ai_plan.max_calls_per_day:
        if config.ai_plan.force_web_search:
            plan = forced_search_unavailable_plan(quant, "ai_plan_daily_limit_forced_search")
        else:
            plan = dict(quant)
            plan["source"] = "quant_fallback"
            plan["ai_status"] = {"status": "skipped", "reason": "ai_plan_daily_limit"}
        write_json(config.ai_plan_file, plan)
        append_jsonl(config.ai_plan_log_file, plan)
        return plan

    state["ai_plan_calls_today"] = int(state.get("ai_plan_calls_today", 0)) + 1
    ai_result = call_openai_plan(config, quant, event, history)
    if ai_result.get("status") == "ok":
        ai_payload = parse_openai_json(ai_result)
    else:
        ai_payload = ai_result

    plan = sanitize_plan(config, quant, ai_payload, event)
    if ai_payload.get("status") != "ok" and config.ai_plan.force_web_search:
        plan = forced_search_unavailable_plan(
            quant,
            str(ai_payload.get("reason") or ai_payload.get("status") or "forced_search_unavailable"),
            ai_payload,
        )
    elif ai_payload.get("status") != "ok" and config.ai_plan.block_when_unavailable:
        plan["action"] = "risk_off"
        plan["risk_multiplier"] = "0"
        plan["should_block_buys"] = True
    write_json(config.ai_plan_file, plan)
    append_jsonl(config.ai_plan_log_file, plan)
    return plan


def apply_ai_plan_multiplier(config: Any, base_multiplier: Decimal, plan: dict[str, Any], event: dict[str, Any]) -> tuple[Decimal, dict[str, Any]]:
    plan_multiplier = decimal_or_zero(plan.get("risk_multiplier", "1"))
    if plan.get("should_block_buys"):
        return Decimal("0"), {"base_multiplier": str(base_multiplier), "plan_multiplier": str(plan_multiplier), "applied": "blocked"}
    if not aggressive_gate_open(config, event, int(plan.get("confidence", 0))) and not chief_authority_allows_soft_override(config, plan, event):
        plan_multiplier = min(plan_multiplier, Decimal("1"))
    plan_multiplier = min(max(plan_multiplier, Decimal("0")), config.ai_plan.max_risk_multiplier)
    final = min(base_multiplier * plan_multiplier, config.ai_plan.max_risk_multiplier)
    return final, {
        "base_multiplier": str(base_multiplier),
        "plan_multiplier": str(plan_multiplier),
        "final_multiplier": str(final),
        "applied": "scaled",
    }


def compact_ai_plan(plan: dict[str, Any]) -> dict[str, Any]:
    forecast = plan.get("forecast") if isinstance(plan.get("forecast"), dict) else {}
    return {
        "status": plan.get("status"),
        "source": plan.get("source"),
        "model": plan.get("model"),
        "config_signature": plan.get("config_signature"),
        "reasoning_effort": plan.get("reasoning_effort"),
        "web_search": plan.get("web_search"),
        "force_web_search": plan.get("force_web_search"),
        "action": plan.get("action"),
        "risk_multiplier": plan.get("risk_multiplier"),
        "confidence": plan.get("confidence"),
        "should_block_buys": plan.get("should_block_buys"),
        "override_program_rejection": plan.get("override_program_rejection"),
        "forecast": {
            "horizon_hours": forecast.get("horizon_hours"),
            "direction": forecast.get("direction"),
            "market_direction": forecast.get("market_direction"),
            "risk_math": forecast.get("risk_math"),
            "rationale": forecast.get("rationale"),
            "invalidation": forecast.get("invalidation"),
            "override_rationale": forecast.get("override_rationale"),
        },
        "reasoning_summary": plan.get("reasoning_summary"),
        "web_search_calls": plan.get("web_search_calls"),
        "age_minutes": plan.get("age_minutes"),
        "gates": plan.get("gates", {}),
        "risk_notes": plan.get("risk_notes", []),
        "ai_status": plan.get("ai_status"),
    }


def format_plan(plan: dict[str, Any]) -> str:
    compact = compact_ai_plan(plan)
    forecast = compact.get("forecast", {})
    return "\n".join(
        [
            f"AI长期计划: {compact.get('action')} source={compact.get('source')} status={compact.get('status')}",
            f"风险乘数: {compact.get('risk_multiplier')} confidence={compact.get('confidence')} block={compact.get('should_block_buys')}",
            f"预测: {forecast.get('direction')} / {forecast.get('horizon_hours')}h",
            f"理由: {forecast.get('rationale')}",
            f"失效条件: {forecast.get('invalidation')}",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Show the cached AI long-horizon trading plan")
    parser.add_argument("command", choices=["show", "json"])
    parser.add_argument("--plan", type=Path, default=Path("ai_plan.json"))
    args = parser.parse_args()
    plan = load_json(args.plan, {})
    if args.command == "json":
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_plan(plan) if plan else "no ai_plan.json yet")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
