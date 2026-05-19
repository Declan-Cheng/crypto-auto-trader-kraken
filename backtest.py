#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from bot import (
    KrakenClient,
    downside_bias,
    execute_paper_order,
    load_config,
    load_state,
    make_signal,
    market_regime,
    reset_daily_if_needed,
    shadow_short_cycle,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Quick Kraken OHLC strategy backtest over the latest available candles")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()

    config = load_config(args.config)
    client = KrakenClient()
    try:
        candles = client.candles(config.pair, config.candle_interval_minutes)
    finally:
        client.close()

    state = load_state(config)
    state["paper_quote_balance"] = str(config.risk.starting_paper_quote)
    state["paper_base_balance"] = "0"
    state["avg_entry_price"] = None
    state["trades_today"] = 0
    state["last_trade_at"] = None
    reset_daily_if_needed(state)

    indicator_history = max(
        config.strategy.slow_sma_period,
        config.strategy.ema_trend_period,
        config.strategy.macd_slow_period + config.strategy.macd_signal_period,
        config.strategy.bollinger_period,
        config.strategy.rsi_period + 1,
        config.strategy.fast_sma_period + 1,
        config.market_filters.atr_period,
    )
    start = indicator_history + 2
    trades = []
    short_trades = []
    short_state: dict[str, str | int | None] = {}
    equity_curve = []
    short_equity_curve = []
    peak_equity = config.risk.starting_paper_quote
    max_drawdown = Decimal("0")
    short_peak_equity = config.shadow.starting_quote
    short_max_drawdown = Decimal("0")
    for index in range(start, len(candles)):
        window = candles[: index + 1]
        base = Decimal(str(state["paper_base_balance"]))
        avg_entry = Decimal(str(state["avg_entry_price"])) if state.get("avg_entry_price") else None
        signal = make_signal(config, window, base, avg_entry)
        mark_price = window[-2].close
        equity = Decimal(str(state["paper_quote_balance"])) + base * mark_price
        peak_equity = max(peak_equity, equity)
        max_drawdown = max(max_drawdown, peak_equity - equity)
        equity_curve.append(str(equity))
        if signal.side == "hold":
            result = {"status": "skipped"}
        else:
            result = execute_paper_order(config, state, signal)
            if result["status"] == "filled_paper":
                trades.append({"index": index, "side": signal.side, "price": str(signal.price), "reason": signal.reason, "result": result})
                state["last_trade_at"] = None

        try:
            regime = market_regime(config, window)
            short_bias = downside_bias(config, window, regime)
            short_event = shadow_short_cycle(config, short_state, short_bias, signal.price, True, "backtest_market_ok", Decimal("1"))
            short_equity = Decimal(str(short_event["equity_quote"]))
            short_peak_equity = max(short_peak_equity, short_equity)
            short_max_drawdown = max(short_max_drawdown, short_peak_equity - short_equity)
            short_equity_curve.append(str(short_equity))
            short_order = short_event.get("order", {})
            if isinstance(short_order, dict) and short_order.get("status") in {"filled_shadow_short", "closed_shadow_short"}:
                short_trades.append(
                    {
                        "index": index,
                        "action": short_event.get("action"),
                        "price": str(signal.price),
                        "reason": short_event.get("reason"),
                        "result": short_order,
                    }
                )
        except Exception:
            pass

    last_price = candles[-2].close
    equity = Decimal(str(state["paper_quote_balance"])) + Decimal(str(state["paper_base_balance"])) * last_price
    return_pct = (equity - config.risk.starting_paper_quote) / config.risk.starting_paper_quote * Decimal("100")
    score = Decimal(str(return_pct)) - (max_drawdown / config.risk.starting_paper_quote * Decimal("100"))
    short_ending_equity = Decimal(str(short_equity_curve[-1])) if short_equity_curve else config.shadow.starting_quote
    short_return_pct = (short_ending_equity - config.shadow.starting_quote) / config.shadow.starting_quote * Decimal("100")
    short_score = Decimal(str(short_return_pct)) - (short_max_drawdown / config.shadow.starting_quote * Decimal("100"))
    print(
        json.dumps(
            {
                "pair": config.pair,
                "candles": len(candles),
                "trades": len(trades),
                "ending_equity_quote": str(equity),
                "starting_equity_quote": str(config.risk.starting_paper_quote),
                "return_pct": str(return_pct),
                "max_drawdown_quote": str(max_drawdown),
                "score_return_minus_drawdown_pct": str(score),
                "open_base": state["paper_base_balance"],
                "last_price": str(last_price),
                "trade_sample": trades[-5:],
                "shadow_short_trades": len(short_trades),
                "shadow_short_ending_equity_quote": str(short_ending_equity),
                "shadow_short_return_pct": str(short_return_pct),
                "shadow_short_max_drawdown_quote": str(short_max_drawdown),
                "shadow_short_score_return_minus_drawdown_pct": str(short_score),
                "shadow_short_open_base": str(short_state.get("paper_short_base_balance", "0")),
                "shadow_short_trade_sample": short_trades[-5:],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
