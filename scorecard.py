#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot import load_config
from ledger import daily_summary


ROOT = Path(__file__).resolve().parent


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


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def latest_event(config_path: Path) -> dict[str, Any] | None:
    config = load_config(config_path)
    rows = read_jsonl_tail(config.trade_log, 1)
    return rows[-1] if rows else None


def shadow_summary(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    state = load_json(config.shadow_state_file, {})
    events = read_jsonl_tail(config.shadow_log_file, 500)
    signal_counts = Counter(str(event.get("signal")) for event in events if event.get("signal"))
    orders = [event.get("order", {}) for event in events if isinstance(event.get("order"), dict)]
    filled_orders = [order for order in orders if order.get("status") == "filled_paper"]
    short_events = [event.get("shadow_short", {}) for event in events if isinstance(event.get("shadow_short"), dict)]
    short_orders = [event.get("order", {}) for event in short_events if isinstance(event.get("order"), dict)]
    filled_short_orders = [order for order in short_orders if order.get("status") in {"filled_shadow_short", "closed_shadow_short"}]
    quote = Decimal(str(state.get("paper_quote_balance", config.shadow.starting_quote)))
    base = Decimal(str(state.get("paper_base_balance", "0")))
    last_price = Decimal(str(events[-1].get("price", "0"))) if events else Decimal("0")
    equity = quote + base * last_price
    latest_short = short_events[-1] if short_events else {}
    short_equity = Decimal(str(latest_short.get("equity_quote", state.get("short_collateral_quote_balance", config.shadow.starting_quote))))
    return {
        "enabled": config.shadow.enabled,
        "events": len(events),
        "signal_counts": dict(signal_counts),
        "filled_orders": len(filled_orders),
        "quote_balance": str(quote),
        "base_balance": str(base),
        "last_price": str(last_price),
        "equity_quote": str(equity),
        "starting_quote": str(config.shadow.starting_quote),
        "pnl_quote": str(equity - config.shadow.starting_quote),
        "last_order": orders[-1] if orders else None,
        "short_enabled": config.downside.shadow_short_enabled,
        "short_equity_quote": str(short_equity),
        "short_pnl_quote": str(short_equity - config.shadow.starting_quote),
        "short_filled_orders": len(filled_short_orders),
        "last_short": latest_short or None,
    }


def strategy_scorecard(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    live = daily_summary(config.ledger_db, mode=config.mode)
    latest = latest_event(config_path) or {}
    shadow = shadow_summary(config_path)
    latest_research = latest.get("research", {}) if isinstance(latest.get("research"), dict) else {}
    latest_regime = latest.get("market_regime", {}) if isinstance(latest.get("market_regime"), dict) else {}
    latest_guard = latest.get("equity_guard", {}) if isinstance(latest.get("equity_guard"), dict) else {}
    latest_downside = latest.get("downside_bias", {}) if isinstance(latest.get("downside_bias"), dict) else {}
    latest_ai_plan = latest.get("ai_plan", {}) if isinstance(latest.get("ai_plan"), dict) else load_json(config.ai_plan_file, {})
    latest_market_radar = latest.get("market_radar", {}) if isinstance(latest.get("market_radar"), dict) else {}
    return {
        "mode": config.mode,
        "pair": config.pair,
        "live_today": live,
        "latest_signal": {
            "ts": latest.get("ts"),
            "signal": latest.get("signal"),
            "score": latest.get("score"),
            "reason": latest.get("signal_reason"),
            "order": latest.get("order"),
            "pnl_snapshot": latest.get("pnl_snapshot"),
        },
        "market_regime": latest_regime,
        "research": latest_research,
        "equity_guard": latest_guard,
        "downside_bias": latest_downside,
        "ai_plan": latest_ai_plan,
        "market_radar": latest_market_radar,
        "shadow": shadow,
    }


def format_scorecard(card: dict[str, Any]) -> str:
    live = card.get("live_today", {})
    latest = card.get("latest_signal", {})
    shadow = card.get("shadow", {})
    research = card.get("research", {})
    regime = card.get("market_regime", {})
    guard = card.get("equity_guard", {})
    downside = card.get("downside_bias", {})
    ai_plan = card.get("ai_plan", {})
    forecast = ai_plan.get("forecast", {}) if isinstance(ai_plan.get("forecast"), dict) else {}
    radar = card.get("market_radar", {})
    top_assets = radar.get("top_assets", []) if isinstance(radar.get("top_assets"), list) else []
    top_text = ", ".join(f"{item.get('pair')}:{item.get('score')}" for item in top_assets[:3])
    lines = [
        f"策略评分 {card.get('pair')} mode={card.get('mode')}",
        f"live今日PnL: {live.get('pnl_quote')} / 订单数: {len(live.get('orders', []))} / 信号: {json.dumps(live.get('signal_counts', {}), ensure_ascii=False, sort_keys=True)}",
        f"最新信号: {latest.get('signal')} score={latest.get('score')} reason={latest.get('reason')}",
        f"市场状态: {regime.get('regime')} score={regime.get('score')} interval={regime.get('interval_minutes')}m",
        f"广域雷达: breadth={radar.get('positive_breadth')} risk_on={radar.get('risk_on')} risk_off={radar.get('risk_off')} top={top_text}",
        f"AI计划: model={ai_plan.get('model')} action={ai_plan.get('action')} x={ai_plan.get('risk_multiplier')} conf={ai_plan.get('confidence')} forecast={forecast.get('direction')} override={ai_plan.get('override_program_rejection')}",
        f"空头风险: action={downside.get('action')} short_score={downside.get('short_score')} live_short={downside.get('live_short_enabled')}",
        f"研究风险: {research.get('risk_level')} score={research.get('risk_score')} reduce={research.get('reduce_size')} block={research.get('block_buys')}",
        f"权益熔断: {guard.get('active')} reason={guard.get('reason')} dd={guard.get('daily_drawdown_quote')}",
        f"影子盘PnL: {shadow.get('pnl_quote')} / equity={shadow.get('equity_quote')} / filled_orders={shadow.get('filled_orders')}",
        f"影子做空PnL: {shadow.get('short_pnl_quote')} / equity={shadow.get('short_equity_quote')} / filled_orders={shadow.get('short_filled_orders')}",
        f"影子盘信号: {json.dumps(shadow.get('signal_counts', {}), ensure_ascii=False, sort_keys=True)}",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize live, research, regime, and shadow-trading performance")
    parser.add_argument("command", choices=["show", "json"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()

    card = strategy_scorecard(args.config)
    if args.command == "json":
        print(json.dumps(card, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_scorecard(card))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
