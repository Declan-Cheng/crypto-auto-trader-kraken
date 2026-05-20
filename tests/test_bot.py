from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ai_planner
import bot
import research
import review_pipeline


def make_config(tmp_path: Path, mode: str = "paper") -> bot.BotConfig:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "exchange": "kraken",
                "pair": "XBTUSD",
                "ws_symbol": "BTC/USD",
                "base_asset": "XXBT",
                "quote_asset": "ZUSD",
                "mode": mode,
                "poll_seconds": 300,
                "candle_interval_minutes": 5,
                "strategy": {
                    "fast_sma_period": 3,
                    "slow_sma_period": 5,
                    "rsi_period": 3,
                    "buy_rsi_max": 90,
                    "sell_rsi_min": 10,
                    "ema_trend_period": 5,
                    "macd_fast_period": 3,
                    "macd_slow_period": 5,
                    "macd_signal_period": 2,
                    "bollinger_period": 5,
                    "bollinger_stddev": 2,
                    "min_buy_score": 3,
                    "min_sell_score": 3,
                    "min_buy_momentum_pct": 0.05,
                },
                "risk": {
                    "starting_paper_quote": 50,
                    "max_order_quote": 10,
                    "max_position_quote": 25,
                    "risk_per_trade_quote": 1,
                    "reserve_quote_balance": 5,
                    "daily_loss_limit_quote": 5,
                    "stop_loss_pct": 0.04,
                    "take_profit_pct": 0.06,
                    "cooldown_minutes": 60,
                    "max_trades_per_day": 3,
                    "fee_bps": 26,
                    "slippage_bps": 10,
                    "min_net_profit_bps": 25,
                    "max_daily_equity_drawdown_quote": 5,
                    "max_high_water_drawdown_quote": 4,
                },
                "market_filters": {
                    "max_spread_bps": 35,
                    "max_candle_range_pct": 4,
                    "max_atr_pct": 2.5,
                    "atr_period": 3,
                },
                "llm": {
                    "enabled": False,
                    "model": "gpt-5.4-mini",
                    "veto_risk_score": 80,
                    "max_calls_per_day": 6,
                    "block_when_unavailable": False,
                },
                "execution": {
                    "approval_mode": "aggressive_only",
                    "aggressive_buy_score_max": 3,
                    "approval_ttl_minutes": 30,
                    "max_cycle_gap_seconds": 900,
                    "reconnect_observe_cycles": 1,
                    "trading_windows_utc": [],
                    "notify_on_signal": True,
                    "notify_on_fill": True,
                    "min_trade_interval_seconds": 300,
                },
                "scan_pairs": ["XBTUSD", "ETHUSD", "SOLUSD"],
                "paths": {
                    "state_file": "state.json",
                    "trade_log": "trades.jsonl",
                    "pending_order": "pending_order.json",
                    "research_cache": "research_snapshot.json",
                },
                "research": {
                    "enabled": True,
                    "reduce_size_risk_score": 40,
                    "block_buy_risk_score": 70,
                    "reduced_size_multiplier": 0.5,
                },
                "protections": {
                    "enabled": True,
                    "pair_cooldown_after_sell_minutes": 30,
                    "consecutive_loss_limit": 2,
                    "max_realized_loss_quote": 3,
                    "stop_duration_minutes": 120,
                },
            }
        )
    )
    return bot.load_config(config_path)


def candles(closes: list[str]) -> list[bot.Candle]:
    return [
        bot.Candle(
            timestamp=1000 + index,
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=Decimal("1"),
        )
        for index, close in enumerate(closes)
    ]


def test_sma_and_rsi() -> None:
    values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
    assert bot.sma(values, 2) == Decimal("3.5")
    assert bot.rsi(values, 3) == Decimal("100")
    assert bot.percentage_momentum(values, 2) == Decimal("100")


def test_buy_signal_when_fast_sma_crosses_above_slow(tmp_path: Path) -> None:
    config = make_config(tmp_path, mode="live")
    signal = bot.make_signal(config, candles(["10", "10.5", "10.2", "10.7", "11", "10.9", "11.2", "11.4"]), Decimal("0"), None)
    assert signal.side == "buy"


def test_advanced_strategy_scores_turtle_breakout_and_supertrend(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        advanced_strategy=replace(
            config.advanced_strategy,
            turtle_breakout_period=3,
            turtle_soup_period=3,
            supertrend_period=3,
            var_lookback=4,
            max_var_pct=50,
            max_es_pct=50,
        ),
    )

    signal = bot.make_signal(config, candles(["10", "10.1", "10.2", "10.3", "10.4", "10.5", "11.0", "11.5"]), Decimal("0"), None)

    assert "turtle_breakout_up" in signal.reason
    assert "supertrend_up" in signal.reason
    assert signal.score >= config.strategy.min_buy_score


def test_advanced_strategy_var_es_blocks_high_tail_risk(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        advanced_strategy=replace(
            config.advanced_strategy,
            supertrend_period=3,
            var_lookback=5,
            max_var_pct=1,
            max_es_pct=1,
            min_kelly_multiplier=Decimal("0.4"),
        ),
    )

    metrics = bot.advanced_strategy_metrics(config, candles(["100", "103", "99", "94", "88", "91", "85", "84"]))

    assert metrics["risk_model"]["risk_block"] is True
    assert metrics["risk_model"]["kelly_multiplier"] == "0.4"


def test_shadow_state_resets_when_rotation_pair_changes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.new_paper_state(Decimal("50"))
    state.update(
        {
            "active_pair": "ETHUSD",
            "paper_quote_balance": "7454823.95",
            "paper_base_balance": "0.123",
            "avg_entry_price": "121",
            "short_collateral_quote_balance": "-8416850.48",
            "paper_short_base_balance": "99",
            "short_avg_entry_price": "100",
            "short_realized_pnl_today": "-123",
            "short_trades_today": 2,
        }
    )

    assert bot.reset_shadow_pair_if_needed(config, state) is True
    assert state["active_pair"] == "XBTUSD"
    assert state["shadow_previous_pair"] == "ETHUSD"
    assert state["paper_quote_balance"] == "50"
    assert state["paper_base_balance"] == "0"
    assert state["avg_entry_price"] is None
    assert state["short_collateral_quote_balance"] == "50"
    assert state["paper_short_base_balance"] == "0"
    assert state["short_realized_pnl_today"] == "0"
    assert state["short_trades_today"] == 0


def test_stop_loss_signal_for_existing_position(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    signal = bot.make_signal(
        config,
        candles(["101", "100", "99", "98", "97", "96", "95", "94"]),
        Decimal("0.1"),
        Decimal("100"),
    )
    assert signal.side == "sell"
    assert signal.reason == "stop_loss"


def test_take_profit_waits_for_fee_aware_minimum_net_profit(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    signal = bot.make_signal(
        config,
        candles(["100", "100.2", "100.3", "100.4", "100.5", "100.55", "100.58", "100.60"]),
        Decimal("0.1"),
        Decimal("100"),
    )
    assert signal.side == "hold"
    assert signal.reason.startswith("hold_for_min_net_profit")


def test_fee_edge_check_requires_momentum_to_cover_costs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        risk=replace(config.risk, fee_bps=Decimal("80"), slippage_bps=Decimal("10"), min_net_profit_bps=Decimal("50")),
    )

    weak = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"), momentum_pct=Decimal("1.0"))
    strong = replace(weak, momentum_pct=Decimal("2.5"))

    assert bot.fee_edge_check(config, weak)["allowed"] is False
    assert bot.fee_edge_check(config, strong)["allowed"] is True


def test_paper_round_trip_subtracts_fee_from_balances_and_pnl(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.load_state(config)

    buy = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))
    buy_result = bot.execute_paper_order(config, state, buy)
    sell = bot.Signal("sell", "test", Decimal("101"), Decimal("1"), Decimal("1"), Decimal("50"))
    sell_result = bot.execute_paper_order(config, state, sell)

    assert buy_result["fee_quote"] == "0.0260"
    assert sell_result["fee_quote"] == "0.026191724"
    assert Decimal(sell_result["pnl"]) == Decimal("0.047548276")
    assert Decimal(state["paper_quote_balance"]) == Decimal("50.047548276")


def test_daily_loss_limit_blocks_only_losses(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.raw["protections"]["enabled"] = False
    buy_signal = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))
    state = bot.load_state(config)

    state["realized_pnl_today"] = "6"
    assert bot.risk_allows_trade(config, state, buy_signal) == (True, "allowed")

    state["realized_pnl_today"] = "-5"
    assert bot.risk_allows_trade(config, state, buy_signal) == (False, "daily_loss_limit")


def test_pair_cooldown_after_sell_blocks_new_buys(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.new_paper_state(Decimal("50"))
    state["paper_base_balance"] = "1"
    state["avg_entry_price"] = "12"
    sell_signal = bot.Signal("sell", "stop_loss", Decimal("10"), Decimal("1"), Decimal("1"), Decimal("50"))

    result = bot.execute_paper_order(config, state, sell_signal)

    assert result["status"] == "filled_paper"
    buy_signal = bot.Signal("buy", "score_buy", Decimal("10"), Decimal("1"), Decimal("1"), Decimal("50"))
    assert bot.risk_allows_trade(config, state, buy_signal) == (False, "pair_cooldown_after_sell")


def test_consecutive_loss_guard_locks_new_buys(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.new_paper_state(Decimal("50"))
    state["consecutive_loss_trades"] = 2
    buy_signal = bot.Signal("buy", "score_buy", Decimal("10"), Decimal("1"), Decimal("1"), Decimal("50"))

    allowed, reason = bot.risk_allows_trade(config, state, buy_signal)

    assert allowed is False
    assert reason == "consecutive_loss_guard"
    assert state["protection_global_lock_until"]


def test_min_trade_interval_is_independent_from_poll_seconds(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.raw["poll_seconds"] = 10
    state = bot.load_state(config)
    state["last_trade_at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    signal = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))

    assert bot.risk_allows_trade(config, state, signal) == (False, "min_trade_interval")


def test_paper_buy_updates_state(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.load_state(config)
    signal = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))
    result = bot.execute_paper_order(config, state, signal)

    assert result["status"] == "filled_paper"
    assert Decimal(state["paper_quote_balance"]) == Decimal("40")
    assert Decimal(state["paper_base_balance"]) == Decimal("0.09974")
    assert state["trades_today"] == 1


def test_live_submitted_buy_records_estimated_entry_for_risk_exits(tmp_path: Path) -> None:
    state = bot.load_state(make_config(tmp_path))
    order = {
        "status": "submitted",
        "side": "buy",
        "volume": "0.1",
        "quote_to_spend": "10",
    }

    bot.update_live_position_estimate(state, order, Decimal("0"))

    assert state["avg_entry_price"] == "1.0E+2"


def test_live_closed_buy_uses_actual_fill_and_fee_for_entry(tmp_path: Path) -> None:
    state = bot.load_state(make_config(tmp_path))
    order = {
        "status": "closed",
        "side": "buy",
        "executed_volume": "0.1",
        "executed_cost": "10",
        "fee_quote": "0.026",
    }

    bot.update_live_position_estimate(state, order, Decimal("0"))

    assert state["avg_entry_price"] == "100.26"


def test_live_submitted_sell_clears_estimated_entry(tmp_path: Path) -> None:
    state = bot.load_state(make_config(tmp_path))
    state["avg_entry_price"] = "100"
    order = {"status": "submitted", "side": "sell", "volume": "0.1"}

    bot.update_live_position_estimate(state, order, Decimal("0.1"))

    assert state["avg_entry_price"] is None


def test_reconcile_live_order_attaches_query_order_fill_details() -> None:
    class FakeClient:
        def private(self, endpoint: str, data: dict) -> dict:
            assert endpoint == "QueryOrders"
            assert data == {"txid": "T123"}
            return {
                "T123": {
                    "status": "closed",
                    "price": "101",
                    "cost": "10.1",
                    "fee": "0.02626",
                    "vol_exec": "0.1",
                }
            }

    order = {"status": "submitted", "side": "buy", "volume": "0.1", "result": {"txid": ["T123"]}}

    reconciled = bot.reconcile_live_order(FakeClient(), order)

    assert reconciled["status"] == "closed"
    assert reconciled["executed_price"] == "101"
    assert reconciled["executed_cost"] == "10.1"
    assert reconciled["fee_quote"] == "0.02626"


def test_reconcile_live_order_treats_full_open_fill_as_closed() -> None:
    class FakeClient:
        def private(self, endpoint: str, data: dict) -> dict:
            assert endpoint == "QueryOrders"
            return {
                "T123": {
                    "status": "open",
                    "price": "127.63",
                    "cost": "14.92242",
                    "fee": "0.11938",
                    "vol": "0.11691934",
                    "vol_exec": "0.11691934",
                }
            }

    order = {"status": "submitted", "side": "sell", "volume": "0.11691934", "result": {"txid": ["T123"]}}

    reconciled = bot.reconcile_live_order(FakeClient(), order)

    assert reconciled["status"] == "closed"
    assert reconciled["executed_cost"] == "14.92242"
    assert reconciled["executed_volume"] == "0.11691934"


def test_live_execution_uses_bid_ask_price_for_sizing() -> None:
    signal = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))
    ticker = bot.Ticker(Decimal("99"), Decimal("101"), Decimal("100"), Decimal("1"), Decimal("1"))

    buy = bot.execution_priced_signal(signal, ticker)
    sell = bot.execution_priced_signal(replace(signal, side="sell"), ticker)

    assert buy.price == Decimal("101")
    assert sell.price == Decimal("99")


def test_planned_buy_quote_size_respects_reserve(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert bot.planned_buy_quote_size(config, Decimal("12"), Decimal("0")) == Decimal("7")


def test_aggressive_plan_can_lift_order_size_with_hard_cap(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    aggressive_config = replace(
        config,
        ai_plan=replace(config.ai_plan, allow_risk_increase=True, max_risk_multiplier=Decimal("1.5"), max_aggressive_order_quote=Decimal("15")),
    )

    assert bot.planned_buy_quote_size(aggressive_config, Decimal("50"), Decimal("0"), Decimal("1.5")) == Decimal("15.0")


def test_research_size_multiplier_reduces_or_blocks_buys(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    assert bot.position_size_multiplier_from_research(config, {"reduce_size": True}) == Decimal("0.5")
    assert bot.position_size_multiplier_from_research(config, {"block_buys": True}) == Decimal("0")
    assert bot.planned_buy_quote_size(config, Decimal("25"), Decimal("0"), Decimal("0.5")) == Decimal("5.0")


def test_ai_plan_multiplier_only_increases_when_gate_is_open(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    aggressive_config = replace(
        config,
        ai_plan=replace(
            config.ai_plan,
            enabled=True,
            allow_risk_increase=True,
            max_risk_multiplier=Decimal("1.5"),
            min_confidence_to_increase=70,
            min_signal_score_to_increase=4,
            min_regime_score_to_increase=4,
            max_research_score_to_increase=55,
        ),
    )
    event = {
        "score": 5,
        "market_regime": {"regime": "uptrend", "score": 5},
        "research": {"risk_score": 20, "block_buys": False},
        "market_metrics": {"spread_bps": "5"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
    }

    final, detail = ai_planner.apply_ai_plan_multiplier(
        aggressive_config,
        Decimal("1"),
        {"risk_multiplier": "1.5", "confidence": 85, "should_block_buys": False},
        event,
    )

    assert final == Decimal("1.5")
    assert detail["applied"] == "scaled"


def test_ai_plan_multiplier_refuses_increase_when_research_risk_high(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    aggressive_config = replace(
        config,
        ai_plan=replace(config.ai_plan, enabled=True, allow_risk_increase=True, max_risk_multiplier=Decimal("1.5")),
    )
    event = {
        "score": 5,
        "market_regime": {"regime": "uptrend", "score": 5},
        "research": {"risk_score": 81, "block_buys": True},
        "market_metrics": {"spread_bps": "5"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
    }

    final, detail = ai_planner.apply_ai_plan_multiplier(
        aggressive_config,
        Decimal("1"),
        {"risk_multiplier": "1.5", "confidence": 90, "should_block_buys": True},
        event,
    )

    assert final == Decimal("0")
    assert detail["applied"] == "blocked"


def test_ai_plan_cache_invalidates_when_radar_universe_changes(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    radar_config = replace(
        config.market_radar,
        enabled=True,
        pairs=["ETHUSD", "SOLUSD", "ADAUSD"],
        context_pairs=["USDCAD", "EURUSD"],
        max_pairs_per_cycle=4,
    )
    config = replace(config, market_radar=radar_config)
    old_plan = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gates": {"positive_breadth": "0.5", "market_radar_pairs_requested": 2},
    }
    current_plan = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gates": {"positive_breadth": "0.5", "market_radar_pairs_requested": 4},
    }

    assert ai_planner.plan_matches_features(config, old_plan) is False
    assert ai_planner.plan_matches_features(config, current_plan) is True


def test_forced_search_daily_limit_keeps_quant_fallback_buys(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(
            config.ai_plan,
            enabled=True,
            force_web_search=True,
            max_calls_per_day=0,
            allow_risk_increase=True,
        ),
    )
    state = {"ai_plan_day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "ai_plan_calls_today": 0}
    event = {
        "score": 7,
        "market_regime": {"regime": "uptrend", "score": 7},
        "research": {"risk_score": 10, "block_buys": False},
        "market_metrics": {"spread_bps": "2"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.9"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
    }

    plan = ai_planner.build_ai_plan(config, state, event, force=True)

    assert plan["action"] != "risk_off"
    assert plan["risk_multiplier"] != "0"
    assert plan["should_block_buys"] is False
    assert plan["ai_status"]["reason"] == "ai_plan_daily_limit_forced_search"
    assert plan["ai_status"]["force_web_search_failed"] is True


def test_outside_entry_window_blocks_without_spending_search(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(config.ai_plan, enabled=True, force_web_search=True, max_calls_per_day=6),
    )
    state = {"ai_plan_day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "ai_plan_calls_today": 0}
    event = {
        "entry_window_open": False,
        "score": 7,
        "market_regime": {"regime": "uptrend", "score": 7},
        "research": {"risk_score": 10, "block_buys": False},
        "market_metrics": {"spread_bps": "2"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.9"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
    }

    plan = ai_planner.build_ai_plan(config, state, event)

    assert state["ai_plan_calls_today"] == 0
    assert plan["status"] == "outside_entry_window"
    assert plan["action"] == "risk_off"
    assert plan["should_block_buys"] is True


def test_forced_search_failure_counts_attempt_and_caches_briefly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(config.ai_plan, enabled=True, force_web_search=True, max_calls_per_day=10),
        market_radar=replace(config.market_radar, enabled=False),
    )
    state = {"ai_plan_day": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "ai_plan_calls_today": 0}
    event = {
        "score": 7,
        "market_regime": {"regime": "uptrend", "score": 7},
        "research": {"risk_score": 10, "block_buys": False},
        "market_metrics": {"spread_bps": "2"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.9"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
    }
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        ai_planner,
        "call_openai_plan",
        lambda *_args, **_kwargs: {"status": "error", "reason": "all_models_failed"},
    )

    plan = ai_planner.build_ai_plan(config, state, event)

    assert state["ai_plan_calls_today"] == 1
    assert plan["action"] != "risk_off"
    assert plan["should_block_buys"] is False
    assert plan["ai_status"]["reason"] == "all_models_failed"
    assert plan["ai_status"]["force_web_search_failed"] is True
    assert ai_planner.plan_matches_features(config, plan) is True


def test_openai_plan_can_override_soft_research_block_without_hard_blocks(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(
            config.ai_plan,
            enabled=True,
            allow_decision_override=True,
            override_min_confidence=88,
            override_max_research_score=90,
        ),
    )
    event = {
        "score": 1,
        "market_regime": {"regime": "range_or_transition", "score": 1},
        "research": {"risk_score": 82, "block_buys": True},
        "market_metrics": {"spread_bps": "4"},
        "downside_bias": {"action": "no_short_edge"},
        "equity_guard": {"active": False},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.62", "strong_negative_breadth": "0.05"},
    }
    quant = ai_planner.quant_plan(config, event, {"drawdown_window_quote": "0"})
    ai_payload = {
        "status": "ok",
        "action": "standard_accumulate",
        "risk_multiplier": 1,
        "confidence": 91,
        "should_block_buys": False,
        "override_program_rejection": True,
        "forecast_direction": "up",
        "market_direction": "bullish",
        "horizon_hours": 6,
        "rationale": "Broad market is constructive and risk is understood.",
        "invalidation": "Break of trend.",
        "override_rationale": "Soft research block is outweighed by breadth and low spread.",
        "risk_notes": [],
    }

    plan = ai_planner.sanitize_plan(config, quant, ai_payload, event)

    assert plan["source"] == "openai"
    assert plan["should_block_buys"] is False
    assert plan["override_program_rejection"] is True
    assert plan["risk_multiplier"] == "1"


def test_ai_override_decision_approves_soft_hold_only_with_strong_plan(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(
            config.ai_plan,
            allow_decision_override=True,
            override_min_confidence=88,
            override_min_signal_score=0,
            override_min_positive_breadth=Decimal("0.55"),
            override_max_research_score=90,
        ),
    )
    signal = bot.Signal("hold", "hold_signal", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"), score=1)
    event = {
        "score": 1,
        "research": {"risk_score": 40},
        "fee_edge_check": {"allowed": True},
        "market_metrics": {"spread_bps": "4"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.62"},
        "equity_guard": {"active": False},
        "downside_bias": {"action": "no_short_edge"},
    }
    plan = {
        "action": "standard_accumulate",
        "confidence": 91,
        "should_block_buys": False,
        "forecast": {"direction": "up", "override_rationale": "strong evidence"},
    }

    approved, detail = bot.ai_override_decision(config, signal, "hold_signal", plan, event)

    assert approved is True
    assert detail["reason"] == "ai_high_confidence_override"


def test_ai_override_decision_never_crosses_fee_edge_block(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        ai_plan=replace(
            config.ai_plan,
            allow_decision_override=True,
            override_min_confidence=72,
            override_min_signal_score=0,
            override_min_positive_breadth=Decimal("0.55"),
            override_max_research_score=90,
        ),
    )
    signal = bot.Signal("buy", "score_buy", Decimal("2.02"), Decimal("1"), Decimal("1"), Decimal("55"), score=6)
    event = {
        "score": 6,
        "research": {"risk_score": 55},
        "fee_edge_check": {"allowed": False, "required_bps": "185", "momentum_bps": "56"},
        "market_metrics": {"spread_bps": "2.6"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.88"},
        "equity_guard": {"active": False},
        "downside_bias": {"action": "no_short_edge"},
    }
    plan = {
        "action": "standard_accumulate",
        "confidence": 91,
        "should_block_buys": False,
        "forecast": {"direction": "up", "override_rationale": "thin but positive"},
    }

    approved, detail = bot.ai_override_decision(config, signal, "fee_edge_too_small", plan, event)

    assert approved is False
    assert detail["reason"] == "hard_block:fee_edge_too_small"


def test_ai_override_decision_never_crosses_equity_guard(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(config, ai_plan=replace(config.ai_plan, allow_decision_override=True))
    signal = bot.Signal("hold", "hold_signal", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"), score=5)
    event = {
        "score": 5,
        "research": {"risk_score": 10},
        "market_metrics": {"spread_bps": "1"},
        "market_radar": {"enabled": True, "risk_off": False, "positive_breadth": "0.8"},
        "equity_guard": {"active": True},
        "downside_bias": {"action": "no_short_edge"},
    }
    plan = {
        "action": "aggressive_accumulate",
        "confidence": 99,
        "should_block_buys": False,
        "forecast": {"direction": "up", "override_rationale": "strong evidence"},
    }

    approved, detail = bot.ai_override_decision(config, signal, "daily_equity_drawdown", plan, event)

    assert approved is False
    assert detail["reason"].startswith("hard_block:")


def test_equity_guard_blocks_new_buys_after_drawdown(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.load_state(config)
    bot.update_equity_guard(config, state, {"equity_quote": "50"})
    guard = bot.update_equity_guard(config, state, {"equity_quote": "44.9"})
    signal = bot.Signal("buy", "test", Decimal("100"), Decimal("1"), Decimal("1"), Decimal("50"))

    assert guard["active"] is True
    assert bot.risk_allows_trade(config, state, signal) == (False, "daily_equity_drawdown")


def test_market_filter_blocks_wide_spread(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ticker = bot.Ticker(
        bid=Decimal("99"),
        ask=Decimal("101"),
        last=Decimal("100"),
        spread_bps=Decimal("200"),
        change_pct=Decimal("0"),
    )
    allowed, reason, _metrics = bot.market_filter_allows(
        config,
        ticker,
        candles(["100", "100", "100", "100", "100"]),
    )
    assert allowed is False
    assert reason == "spread_too_wide"


def test_buy_filter_rejects_micro_momentum_noise(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    signal = bot.make_signal(
        config,
        candles(["100", "100.01", "100.02", "100.00", "100.03", "100.01", "100.04", "100.05"]),
        Decimal("0"),
        None,
    )
    assert signal.side == "hold"
    assert signal.reason.startswith("buy_filter_min_momentum")


def test_scan_pair_candidates_scores_without_orders(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ticker = bot.Ticker(
        bid=Decimal("99.95"),
        ask=Decimal("100.05"),
        last=Decimal("100"),
        spread_bps=Decimal("10"),
        change_pct=Decimal("1"),
    )
    market_data = {
        "ETHUSD": (ticker, candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])),
        "SOLUSD": (ticker, candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])),
    }

    results = bot.scan_pair_candidates(config, market_data)

    assert [candidate["pair"] for candidate in results] == ["ETHUSD", "SOLUSD"]
    assert results[0]["order_execution"] == "scanner_only"
    assert results[0]["score"] >= results[1]["score"]
    assert "estimated_round_trip_cost_bps" in results[0]


def test_market_radar_computes_breadth_from_multiple_pairs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    make_candles = candles

    class FakeClient:
        def ticker(self, pair: str) -> bot.Ticker:
            return bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), Decimal("10"), Decimal("1"))

        def candles(self, pair: str, _interval_minutes: int | None = None) -> list[bot.Candle]:
            if pair == "ETHUSD":
                return make_candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])
            return make_candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])

    radar_config = replace(config.market_radar, enabled=True, pairs=["ETHUSD", "SOLUSD"], context_pairs=[])
    config = replace(config, market_radar=radar_config)

    radar = bot.build_market_radar(config, FakeClient(), {})

    assert radar["enabled"] is True
    assert radar["pairs_analyzed"] == 2
    assert Decimal(radar["positive_breadth"]) > 0
    assert radar["top_assets"][0]["pair"] == "ETHUSD"


def test_market_radar_preserves_context_pairs_when_capped(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    seen: list[str] = []

    class FakeClient:
        def ticker(self, pair: str) -> bot.Ticker:
            seen.append(pair)
            return bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), Decimal("10"), Decimal("1"))

        def candles(self, pair: str, _interval_minutes: int | None = None) -> list[bot.Candle]:
            if pair == "ETHUSD":
                return candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])
            return candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])

    radar_config = replace(
        config.market_radar,
        enabled=True,
        pairs=["ETHUSD", "SOLUSD", "ADAUSD"],
        context_pairs=["USDCAD", "EURUSD"],
        max_pairs_per_cycle=3,
    )
    config = replace(config, market_radar=radar_config)

    radar = bot.build_market_radar(config, FakeClient(), {})

    assert radar["pairs_requested"] == 3
    assert seen == ["ETHUSD", "USDCAD", "EURUSD"]


def test_rotation_selects_best_quote_matched_candidate(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.raw["rotation"] = {
        "enabled": True,
        "quote_asset": "ZUSD",
        "pairs": ["XBTUSD", "ETHUSD", "SOLUSD"],
        "min_score": 2,
        "max_spread_bps": 25,
    }

    class FakeClient:
        def balances(self) -> dict[str, Decimal]:
            return {"ZUSD": Decimal("50")}

        def asset_pair_rules(self, pair: str) -> dict[str, str]:
            return {"base": {"XBTUSD": "XXBT", "ETHUSD": "XETH", "SOLUSD": "SOL"}[pair], "quote": "ZUSD"}

        def ticker(self, pair: str) -> bot.Ticker:
            return bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), Decimal("10"), Decimal("1"))

        def candles(self, pair: str, _interval_minutes: int | None = None) -> list[bot.Candle]:
            if pair == "ETHUSD":
                return candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])
            return candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])

    selected, detail = bot.select_rotation_pair(config, FakeClient(), {"paper_base_balance": "0"})

    assert selected.pair in {"ETHUSD", "SOLUSD"}
    assert selected.base_asset == "XETH"
    assert detail["reason"] == "selected_best_candidate"


def test_dynamic_pairlist_adds_high_volume_filtered_pairs(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config.raw["rotation"] = {
        "enabled": True,
        "quote_asset": "ZUSD",
        "pairs": ["XBTUSD"],
        "min_score": 2,
        "max_spread_bps": 25,
    }
    config.raw["dynamic_pairlist"] = {
        "enabled": True,
        "quote_asset": "ZUSD",
        "max_assets": 3,
        "max_source_pairs": 10,
        "max_spread_bps": 25,
        "min_quote_volume": "1000",
        "include_static_pairs": True,
    }

    class FakeClient:
        def balances(self) -> dict[str, Decimal]:
            return {"ZUSD": Decimal("50")}

        def asset_pairs(self) -> dict[str, dict[str, str]]:
            return {
                "ETHUSD": {"base": "XETH", "quote": "ZUSD", "status": "online"},
                "SOLUSD": {"base": "SOL", "quote": "ZUSD", "status": "online"},
                "WIDEUSD": {"base": "WIDE", "quote": "ZUSD", "status": "online"},
                "ETHEUR": {"base": "XETH", "quote": "ZEUR", "status": "online"},
            }

        def asset_pair_rules(self, pair: str) -> dict[str, str]:
            return {
                "XBTUSD": {"base": "XXBT", "quote": "ZUSD"},
                "ETHUSD": {"base": "XETH", "quote": "ZUSD"},
                "SOLUSD": {"base": "SOL", "quote": "ZUSD"},
                "WIDEUSD": {"base": "WIDE", "quote": "ZUSD"},
            }[pair]

        def ticker(self, pair: str) -> bot.Ticker:
            spread = Decimal("100") if pair == "WIDEUSD" else Decimal("10")
            volume = {"XBTUSD": "1500", "ETHUSD": "8000", "SOLUSD": "5000", "WIDEUSD": "9999"}[pair]
            return bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), spread, Decimal("1"), Decimal(volume))

        def candles(self, pair: str, _interval_minutes: int | None = None) -> list[bot.Candle]:
            if pair == "ETHUSD":
                return candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])
            if pair == "SOLUSD":
                return candles(["9", "9.05", "9.1", "9.2", "9.35", "9.55", "9.75", "10.0"])
            return candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])

    selected, detail = bot.select_rotation_pair(config, FakeClient(), {"paper_base_balance": "0"})

    assert selected.pair in {"ETHUSD", "SOLUSD"}
    assert detail["dynamic_pairlist"]["enabled"] is True
    assert "ETHUSD" in detail["dynamic_pairlist"]["selected_pairs"]
    assert "WIDEUSD" not in detail["dynamic_pairlist"]["selected_pairs"]


def test_rotation_ignores_dust_active_position(tmp_path: Path) -> None:
    config = make_config(tmp_path, mode="live")
    config.raw["rotation"] = {
        "enabled": True,
        "quote_asset": "ZUSD",
        "pairs": ["XBTUSD", "ETHUSD", "SOLUSD"],
        "min_score": 2,
        "max_spread_bps": 25,
    }

    class FakeClient:
        def balances(self) -> dict[str, Decimal]:
            return {"ZUSD": Decimal("50"), "SOL": Decimal("0.00000001")}

        def asset_pair_rules(self, pair: str) -> dict[str, str]:
            return {"base": {"XBTUSD": "XXBT", "ETHUSD": "XETH", "SOLUSD": "SOL"}[pair], "quote": "ZUSD", "ordermin": "0.02"}

        def ticker(self, pair: str) -> bot.Ticker:
            return bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), Decimal("10"), Decimal("1"))

        def candles(self, pair: str, _interval_minutes: int | None = None) -> list[bot.Candle]:
            if pair == "ETHUSD":
                return candles(["10", "10.1", "10.2", "10.35", "10.5", "10.7", "10.9", "11.2"])
            return candles(["20", "20", "19.9", "19.8", "19.7", "19.6", "19.5", "19.4"])

    state = {"active_pair": "SOLUSD", "paper_base_balance": "0"}
    selected, detail = bot.select_rotation_pair(config, FakeClient(), state)

    assert selected.pair == "ETHUSD"
    assert state["active_pair_dust"]["pair"] == "SOLUSD"
    assert detail["reason"] == "selected_best_candidate"


def test_market_regime_requires_uptrend_for_buy_alignment(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    uptrend = bot.market_regime(config, candles(["10", "10.2", "10.4", "10.6", "10.8", "11.0", "11.2", "11.4"]))

    assert uptrend["regime"] == "uptrend"
    assert bot.higher_timeframe_allows_buy(config, uptrend) is True
    assert bot.higher_timeframe_allows_buy(config, {"regime": "range_or_transition", "score": 2}) is False


def test_shadow_cycle_logs_paper_decision_without_live_order(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    ticker = bot.Ticker(Decimal("99.95"), Decimal("100.05"), Decimal("100"), Decimal("10"), Decimal("1"))
    pair_candles = candles(["10", "10.2", "10.4", "10.6", "10.8", "11.0", "11.2", "11.4"])
    regime = bot.market_regime(config, pair_candles)
    downside = bot.downside_bias(config, pair_candles, regime)

    event = bot.shadow_cycle(
        config,
        ticker,
        pair_candles,
        {"spread_bps": "10"},
        {"risk_score": 0, "block_buys": False, "reduce_size": False},
        regime,
        downside,
        Decimal("1"),
    )

    assert event["mode"] == "shadow"
    assert config.shadow_log_file.exists()
    assert config.shadow_state_file.exists()
    assert "shadow_short" in event


def test_downside_bias_waits_for_higher_timeframe_confirmation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    falling = candles(["11.4", "11.2", "11.0", "10.8", "10.6", "10.4", "10.2", "10.0"])
    bias = bot.downside_bias(config, falling, {"regime": "range_or_transition", "score": -3})

    assert bias["short_score"] >= 3
    assert bias["action"] == "wait_for_higher_timeframe_downtrend"


def test_shadow_short_opens_and_closes_synthetic_short(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.load_shadow_state(config)
    bias = {"action": "open_or_hold_short"}

    opened = bot.shadow_short_cycle(config, state, bias, Decimal("100"), True, "market_ok", Decimal("1"))
    assert opened["order"]["status"] == "filled_shadow_short"
    assert Decimal(state["paper_short_base_balance"]) > 0

    closed = bot.shadow_short_cycle(config, state, {"action": "no_short_edge", "raw_score": 2}, Decimal("94"), True, "market_ok", Decimal("1"))
    assert closed["order"]["status"] == "closed_shadow_short"
    assert Decimal(closed["order"]["pnl"]) > 0


def test_position_pnl_snapshot_reports_cost_aware_unrealized_pnl(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    snapshot = bot.position_pnl_snapshot(
        config,
        quote_balance=Decimal("40"),
        base_balance=Decimal("0.1"),
        price=Decimal("101"),
        avg_entry=Decimal("100"),
    )

    assert snapshot["equity_quote"] == "50.1"
    assert snapshot["unrealized_pnl_quote"] == "0.1"
    assert snapshot["estimated_exit_fee_quote"] == "0.02626"
    assert snapshot["unrealized_pnl_after_estimated_exit_fee_quote"] == "0.07374"


def test_live_mode_requires_explicit_ack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path, mode="live")
    monkeypatch.delenv("KRAKEN_LIVE_TRADING_ACK", raising=False)

    with pytest.raises(bot.BotError):
        bot.ensure_live_ack(config)


def test_reconnect_gap_sets_observe_cycle(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    state = bot.load_state(config)
    state["last_cycle_started_at"] = "2026-01-01T00:00:00+00:00"

    detected, reason = bot.mark_cycle_start(config, state)

    assert detected is True
    assert reason.startswith("cycle_gap_")
    assert state["reconnect_observe_cycles_left"] == 1
    assert bot.consume_reconnect_observe_cycle(state) is True
    assert bot.consume_reconnect_observe_cycle(state) is False


def test_trading_window_defaults_to_24_7(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    assert bot.within_trading_window(config) is True


def test_trading_window_supports_overnight_utc_window(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config = replace(
        config,
        execution=replace(
            config.execution,
            trading_windows_utc=[{"start": "00:00", "end": "08:00"}],
        ),
    )

    assert bot.within_trading_window(config, datetime(2026, 5, 10, 1, 0, tzinfo=timezone.utc)) is True
    assert bot.within_trading_window(config, datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)) is False


def test_research_parser_scores_crypto_regulatory_headline() -> None:
    xml = """<?xml version="1.0"?>
    <rss><channel><item>
      <title>SEC opens investigation into crypto exchange after alleged fraud</title>
      <description>Digital asset trading platform faces scrutiny.</description>
      <pubDate>Wed, 29 Apr 2026 20:00:00 GMT</pubDate>
      <link>https://example.test/item</link>
    </item></channel></rss>
    """
    items = research.parse_rss(xml, "Test", limit=1)
    scored = research.score_item(items[0], research.DEFAULT_KEYWORDS, 1.0, datetime(2026, 4, 29, 21, tzinfo=timezone.utc))

    assert scored["score"] >= 70
    assert "crypto_regulatory_context" in scored["hits"]


def test_research_html_scraper_extracts_treasury_press_links() -> None:
    html = """
    <html><body>
      <a href="/news/press-releases/sb0001">Treasury Sanctions Virtual Currency Exchange</a>
      <a href="/news/press-releases/readouts">Readouts</a>
    </body></html>
    """

    items = research.parse_html_links(
        html,
        "Treasury",
        "https://home.treasury.gov/news/press-releases",
        include_href=["/news/press-releases/"],
        exclude_titles=["readouts"],
        limit=5,
    )

    assert len(items) == 1
    assert items[0]["title"] == "Treasury Sanctions Virtual Currency Exchange"
    assert items[0]["link"] == "https://home.treasury.gov/news/press-releases/sb0001"


def test_review_pipeline_validates_allowed_proposal_without_applying(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(
        json.dumps(
            {
                "direction": "range",
                "confidence": 80,
                "config_changes": [
                    {"path": "strategy.min_buy_score", "value": 4, "reason": "Avoid weak entries"},
                    {"path": "risk.cooldown_minutes", "value": 30, "reason": "Reduce churn"},
                ],
            }
        )
    )

    result = review_pipeline.apply_proposal(config.raw_path if hasattr(config, "raw_path") else tmp_path / "config.json", proposal_path)

    assert result["apply"] is False
    assert result["validated_changes"][0]["path"] == "strategy.min_buy_score"
    assert bot.load_config(tmp_path / "config.json").strategy.min_buy_score == 3


def test_review_pipeline_rejects_unapproved_or_out_of_range_change(tmp_path: Path) -> None:
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"config_changes": [{"path": "risk.max_order_quote", "value": 500}]}))

    with pytest.raises(ValueError):
        review_pipeline.apply_proposal(tmp_path / "config.json", bad_path)

    bad_path.write_text(json.dumps({"config_changes": [{"path": "mode", "value": "live"}]}))
    with pytest.raises(ValueError):
        review_pipeline.apply_proposal(tmp_path / "config.json", bad_path)
