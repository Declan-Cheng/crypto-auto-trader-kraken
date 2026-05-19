#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot import load_config
from notifier import notify_channels
from scorecard import format_scorecard, strategy_scorecard


ROOT = Path(__file__).resolve().parent
REPORTS_DIR = ROOT / "reports"
REVIEWS_DIR = ROOT / "model_reviews"

ALLOWED_CONFIG_PATHS: dict[str, dict[str, Any]] = {
    "strategy.min_buy_score": {"type": int, "min": 2, "max": 6},
    "strategy.min_sell_score": {"type": int, "min": 2, "max": 6},
    "strategy.min_buy_momentum_pct": {"type": float, "min": 0.0, "max": 0.5},
    "strategy.buy_rsi_max": {"type": float, "min": 50, "max": 80},
    "strategy.sell_rsi_min": {"type": float, "min": 20, "max": 55},
    "risk.max_order_quote": {"type": float, "min": 5, "max": 30},
    "risk.max_position_quote": {"type": float, "min": 10, "max": 65},
    "risk.reserve_quote_balance": {"type": float, "min": 1, "max": 30},
    "risk.cooldown_minutes": {"type": int, "min": 5, "max": 240},
    "risk.max_trades_per_day": {"type": int, "min": 1, "max": 40},
    "risk.stop_loss_pct": {"type": float, "min": 0.02, "max": 0.08},
    "risk.take_profit_pct": {"type": float, "min": 0.03, "max": 0.12},
    "risk.max_daily_equity_drawdown_quote": {"type": float, "min": 2, "max": 5},
    "risk.max_high_water_drawdown_quote": {"type": float, "min": 2, "max": 5},
    "market_filters.max_spread_bps": {"type": float, "min": 5, "max": 35},
    "market_filters.max_candle_range_pct": {"type": float, "min": 1, "max": 5},
    "market_filters.max_atr_pct": {"type": float, "min": 1, "max": 3},
    "multi_timeframe.min_buy_score": {"type": int, "min": 1, "max": 5},
    "ai_plan.refresh_interval_minutes": {"type": int, "min": 10, "max": 240},
    "ai_plan.max_calls_per_day": {"type": int, "min": 0, "max": 72},
    "ai_plan.max_risk_multiplier": {"type": float, "min": 0.5, "max": 2.2},
    "ai_plan.max_aggressive_order_quote": {"type": float, "min": 5, "max": 30},
    "ai_plan.override_min_confidence": {"type": int, "min": 60, "max": 98},
    "ai_plan.override_max_research_score": {"type": int, "min": 50, "max": 90},
    "ai_plan.override_min_positive_breadth": {"type": float, "min": 0.05, "max": 0.75},
    "market_radar.min_positive_breadth_to_increase": {"type": float, "min": 0.1, "max": 0.75},
    "market_radar.block_buy_breadth_below": {"type": float, "min": 0.05, "max": 0.35},
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_jsonl_tail(path: Path, limit: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def decimal_or_zero(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def ledger_range_summary(db_path: Path, mode: str, days: int) -> dict[str, Any]:
    since = (now_utc() - timedelta(days=days)).isoformat()
    if not db_path.exists():
        return {"days": days, "mode": mode, "error": "ledger_missing"}
    with sqlite3.connect(db_path) as conn:
        snapshots = conn.execute(
            """
            SELECT ts, equity_quote FROM account_snapshots
            WHERE ts >= ? AND mode = ? AND equity_quote IS NOT NULL
            ORDER BY ts ASC
            """,
            (since, mode),
        ).fetchall()
        events = conn.execute(
            """
            SELECT signal, risk, order_status FROM bot_events
            WHERE ts >= ? AND mode = ?
            ORDER BY ts ASC
            """,
            (since, mode),
        ).fetchall()
        orders = conn.execute(
            """
            SELECT ts, side, status, reason, volume, quote, pnl FROM order_records
            WHERE ts >= ? AND mode = ? AND status NOT IN ('skipped', 'validated')
            ORDER BY ts ASC
            """,
            (since, mode),
        ).fetchall()
    equities = [decimal_or_zero(row[1]) for row in snapshots]
    start_equity = equities[0] if equities else None
    end_equity = equities[-1] if equities else None
    high_water = max(equities) if equities else None
    max_drawdown = max((high_water - equity for equity in equities), default=Decimal("0")) if high_water else Decimal("0")
    return {
        "days": days,
        "mode": mode,
        "since": since,
        "snapshot_count": len(snapshots),
        "event_count": len(events),
        "start_equity_quote": str(start_equity) if start_equity is not None else None,
        "end_equity_quote": str(end_equity) if end_equity is not None else None,
        "pnl_quote": str(end_equity - start_equity) if start_equity is not None and end_equity is not None else None,
        "high_water_quote": str(high_water) if high_water is not None else None,
        "max_drawdown_quote": str(max_drawdown),
        "signal_counts": dict(Counter(row[0] for row in events if row[0])),
        "risk_counts": dict(Counter(row[1] for row in events if row[1]).most_common(12)),
        "order_status_counts": dict(Counter(row[2] for row in events if row[2]).most_common(12)),
        "orders": [
            {"ts": row[0], "side": row[1], "status": row[2], "reason": row[3], "volume": row[4], "quote": row[5], "pnl": row[6]}
            for row in orders
        ],
    }


def config_digest(config_path: Path) -> dict[str, Any]:
    raw = read_json(config_path, {})
    keys = [
        "pair",
        "mode",
        "poll_seconds",
        "candle_interval_minutes",
        "strategy",
        "risk",
        "market_filters",
        "market_radar",
        "ai_plan",
        "execution",
        "multi_timeframe",
        "downside",
        "shadow",
    ]
    return {key: raw.get(key) for key in keys if key in raw}


def build_review_report(config_path: Path, period: str) -> dict[str, Any]:
    config = load_config(config_path)
    days = 7 if period == "weekly" else 1
    scorecard = strategy_scorecard(config_path)
    trade_tail = read_jsonl_tail(config.trade_log, 240 if period == "daily" else 1000)
    ai_log_tail = read_jsonl_tail(config.ai_plan_log_file, 12 if period == "daily" else 40)
    latest_event = trade_tail[-1] if trade_tail else {}
    report = {
        "generated_at": now_utc().isoformat(),
        "period": period,
        "days": days,
        "mode": config.mode,
        "pair": config.pair,
        "safety_boundary": {
            "no_leverage": True,
            "no_live_short": not config.downside.live_short_enabled,
            "manual_ui_required_for_chatgpt_web": True,
            "auto_apply_config_changes": False,
            "allowed_config_paths": sorted(ALLOWED_CONFIG_PATHS),
        },
        "config_digest": config_digest(config_path),
        "ledger": ledger_range_summary(config.ledger_db, config.mode, days),
        "scorecard": scorecard,
        "latest_event": latest_event,
        "latest_ai_plan": read_json(config.ai_plan_file, {}),
        "research_snapshot": read_json(config.research_cache_file, {}),
        "recent_ai_plans": ai_log_tail,
        "recent_trade_events": trade_tail,
    }
    report["summary"] = summarize_report(report)
    return report


def summarize_report(report: dict[str, Any]) -> dict[str, Any]:
    ledger = report.get("ledger", {})
    scorecard = report.get("scorecard", {})
    latest = report.get("latest_event", {})
    radar = latest.get("market_radar", {}) if isinstance(latest.get("market_radar"), dict) else {}
    research = latest.get("research", {}) if isinstance(latest.get("research"), dict) else {}
    ai_plan = report.get("latest_ai_plan", {})
    return {
        "period": report.get("period"),
        "pnl_quote": ledger.get("pnl_quote"),
        "event_count": ledger.get("event_count"),
        "orders": ledger.get("orders", []),
        "latest_signal": scorecard.get("latest_signal"),
        "market_regime": scorecard.get("market_regime", {}).get("regime"),
        "market_breadth": radar.get("positive_breadth"),
        "market_risk_off": radar.get("risk_off"),
        "research_risk_score": research.get("risk_score"),
        "research_risk_level": research.get("risk_level"),
        "ai_action": ai_plan.get("action"),
        "ai_source": ai_plan.get("source"),
    }


def format_markdown_report(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    ledger = report.get("ledger", {})
    card = report.get("scorecard", {})
    latest = report.get("latest_event", {})
    ai_plan = report.get("latest_ai_plan", {})
    research = report.get("research_snapshot", {})
    lines = [
        f"# Kraken {report['period']} review",
        "",
        f"- Generated UTC: {report['generated_at']}",
        f"- Mode/pair: {report['mode']} / {report['pair']}",
        f"- PnL: {summary.get('pnl_quote')}",
        f"- Events: {summary.get('event_count')}",
        f"- Orders: {len(ledger.get('orders', []))}",
        f"- Latest signal: {(card.get('latest_signal') or {}).get('signal')} score={(card.get('latest_signal') or {}).get('score')}",
        f"- Market regime: {summary.get('market_regime')}",
        f"- Market breadth: {summary.get('market_breadth')} risk_off={summary.get('market_risk_off')}",
        f"- Research risk: {summary.get('research_risk_level')} score={summary.get('research_risk_score')}",
        f"- AI plan: {summary.get('ai_action')} source={summary.get('ai_source')}",
        "",
        "## Scorecard",
        "",
        "```text",
        format_scorecard(card),
        "```",
        "",
        "## Latest Decision",
        "",
        "```json",
        json.dumps(
            {
                key: latest.get(key)
                for key in [
                    "ts",
                    "signal",
                    "signal_reason",
                    "risk",
                    "score",
                    "market_metrics",
                    "market_regime",
                    "market_radar",
                    "research",
                    "ai_plan",
                    "ai_override",
                    "order",
                    "pnl_snapshot",
                ]
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## Latest AI Plan",
        "",
        "```json",
        json.dumps(ai_plan, ensure_ascii=False, indent=2, sort_keys=True),
        "```",
        "",
        "## Research Snapshot",
        "",
        "```json",
        json.dumps(research, ensure_ascii=False, indent=2, sort_keys=True)[:8000],
        "```",
    ]
    return "\n".join(lines) + "\n"


def build_model_prompt(report: dict[str, Any]) -> str:
    compact = {
        "summary": report.get("summary"),
        "safety_boundary": report.get("safety_boundary"),
        "config_digest": report.get("config_digest"),
        "ledger": report.get("ledger"),
        "scorecard": report.get("scorecard"),
        "latest_event": report.get("latest_event"),
        "latest_ai_plan": report.get("latest_ai_plan"),
        "research_snapshot": report.get("research_snapshot"),
        "recent_ai_plans": report.get("recent_ai_plans", [])[-8:],
        "recent_trade_events": report.get("recent_trade_events", [])[-80:],
    }
    return (
        "你是一个极其谨慎但进取的加密货币现货交易系统审计员。请分析下面的机器人复盘包。\n"
        "目标：判断方向、风险、参数是否需要改。不要建议杠杆、合约、真实做空、提现、绕过硬风控。\n"
        "请先给中文分析，然后给一个 JSON 代码块，格式如下：\n"
        "{\n"
        '  "direction": "risk_off|range|risk_on",\n'
        '  "confidence": 0,\n'
        '  "summary": "一句话结论",\n'
        '  "config_changes": [\n'
        '    {"path": "strategy.min_buy_score", "value": 4, "reason": "为什么"}\n'
        "  ],\n"
        '  "do_not_change": ["原因"],\n'
        '  "next_observations": ["接下来重点观察什么"]\n'
        "}\n"
        "只允许建议 allowed_config_paths 里的 path。若证据不足，config_changes 返回空数组。\n\n"
        "复盘包 JSON：\n"
        "```json\n"
        f"{json.dumps(compact, ensure_ascii=False, indent=2, sort_keys=True)}\n"
        "```\n"
    )


def report_paths(period: str, generated_at: datetime | None = None) -> dict[str, Path]:
    generated_at = generated_at or now_utc()
    stamp = generated_at.strftime("%Y-%m-%d")
    prefix = REPORTS_DIR / f"{stamp}_{period}"
    return {
        "json": prefix.with_suffix(".json"),
        "md": prefix.with_suffix(".md"),
        "prompt": REPORTS_DIR / f"{stamp}_{period}_model_prompt.md",
    }


def write_report_bundle(config_path: Path, period: str) -> dict[str, Any]:
    report = build_review_report(config_path, period)
    paths = report_paths(period, datetime.fromisoformat(report["generated_at"]))
    write_json(paths["json"], report)
    paths["md"].write_text(format_markdown_report(report), encoding="utf-8")
    paths["prompt"].write_text(build_model_prompt(report), encoding="utf-8")
    return {"report": report, "paths": {key: str(value) for key, value in paths.items()}}


def extract_json_block(text: str) -> dict[str, Any]:
    matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates = matches or [text]
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("No JSON object found in model feedback")


def validate_change(path: str, value: Any) -> Any:
    rule = ALLOWED_CONFIG_PATHS.get(path)
    if not rule:
        raise ValueError(f"Config path is not allowed: {path}")
    expected = rule["type"]
    coerced = expected(value)
    if coerced < rule["min"] or coerced > rule["max"]:
        raise ValueError(f"{path}={coerced} outside allowed range {rule['min']}..{rule['max']}")
    return coerced


def set_nested(config: dict[str, Any], dotted_path: str, value: Any) -> None:
    parts = dotted_path.split(".")
    target = config
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def apply_proposal(config_path: Path, proposal_path: Path, *, apply: bool = False) -> dict[str, Any]:
    proposal = read_json(proposal_path, {})
    changes = proposal.get("config_changes", [])
    if not isinstance(changes, list):
        raise ValueError("proposal.config_changes must be a list")
    current = read_json(config_path, {})
    validated: list[dict[str, Any]] = []
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each config change must be an object")
        path = str(change.get("path"))
        value = validate_change(path, change.get("value"))
        validated.append({"path": path, "value": value, "reason": str(change.get("reason", ""))[:300]})
    proposed = json.loads(json.dumps(current))
    for change in validated:
        set_nested(proposed, change["path"], change["value"])
    result = {
        "apply": apply,
        "proposal_path": str(proposal_path),
        "validated_changes": validated,
        "message": "dry_run_only" if not apply else "applied",
    }
    if apply and validated:
        backup = config_path.with_suffix(config_path.suffix + f".bak-{now_utc().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(config_path, backup)
        write_json(config_path, proposed)
        result["backup"] = str(backup)
    return result


def ingest_feedback(text: str) -> dict[str, str]:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime("%Y-%m-%d_%H%M%S")
    feedback_path = REVIEWS_DIR / f"{stamp}_feedback.md"
    proposal_path = REVIEWS_DIR / f"{stamp}_proposal.json"
    feedback_path.write_text(text, encoding="utf-8")
    proposal = extract_json_block(text)
    write_json(proposal_path, proposal)
    return {"feedback": str(feedback_path), "proposal": str(proposal_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate model-review bundles and safely handle strategy proposals")
    parser.add_argument("command", choices=["daily", "weekly", "nightly", "ingest-feedback", "apply-proposal"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--send", action="store_true")
    args = parser.parse_args()

    if args.command in {"daily", "weekly"}:
        result = write_report_bundle(args.config, args.command)
        print(json.dumps(result["paths"], ensure_ascii=False, indent=2, sort_keys=True))
        if args.send:
            config = load_config(args.config)
            summary = result["report"]["summary"]
            notify_channels(
                config,
                f"Kraken {args.command} review ready",
                f"PnL={summary.get('pnl_quote')} orders={len(summary.get('orders', []))} prompt={result['paths']['prompt']}",
                financial=True,
            )
        return 0
    if args.command == "nightly":
        daily = write_report_bundle(args.config, "daily")
        if now_utc().weekday() == 6:
            weekly = write_report_bundle(args.config, "weekly")
        else:
            weekly = None
        config = load_config(args.config)
        notify_channels(
            config,
            "Kraken夜间复盘包已生成",
            f"daily_prompt={daily['paths']['prompt']}" + (f"\nweekly_prompt={weekly['paths']['prompt']}" if weekly else ""),
            financial=True,
        )
        print(json.dumps({"daily": daily["paths"], "weekly": weekly["paths"] if weekly else None}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "ingest-feedback":
        result = ingest_feedback(Path("/dev/stdin").read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "apply-proposal":
        if not args.proposal:
            raise SystemExit("--proposal is required")
        print(json.dumps(apply_proposal(args.config, args.proposal, apply=args.apply), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
