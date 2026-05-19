#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STATE = ROOT / "state.json"
TRADES = ROOT / "trades.jsonl"
LEDGER = ROOT / "ledger.sqlite3"
OUTBOX = ROOT / "notification_outbox.jsonl"
SHADOW_STATE = ROOT / "shadow_state.json"
SHADOW_TRADES = ROOT / "shadow_trades.jsonl"
AI_PLAN = ROOT / "ai_plan.json"
LOG = ROOT / "logs" / "kraken-auto-trader.log"
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.chengziyou.kraken-auto-trader.plist"
BOT_PROCESS_PATTERN = r"[b]ot\.py --config"


def tail_jsonl(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return {"raw": lines[-1]}


def shell(args: list[str]) -> str:
    return subprocess.run(args, text=True, capture_output=True, check=False).stdout.strip()


def main() -> int:
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    latest_event = tail_jsonl(TRADES)
    status = {
        "config_mode": config.get("mode"),
        "approval_mode": config.get("execution", {}).get("approval_mode"),
        "aggressive_buy_score_max": config.get("execution", {}).get("aggressive_buy_score_max"),
        "live_short_enabled": config.get("downside", {}).get("live_short_enabled"),
        "shadow_short_enabled": config.get("downside", {}).get("shadow_short_enabled"),
        "ai_plan_enabled": config.get("ai_plan", {}).get("enabled"),
        "ai_plan_model": config.get("ai_plan", {}).get("model"),
        "ai_plan_model_fallbacks": config.get("ai_plan", {}).get("model_fallbacks"),
        "ai_plan_authority_mode": config.get("ai_plan", {}).get("authority_mode"),
        "ai_plan_reasoning_effort": config.get("ai_plan", {}).get("reasoning_effort"),
        "ai_plan_web_search": config.get("ai_plan", {}).get("web_search"),
        "ai_plan_force_web_search": config.get("ai_plan", {}).get("force_web_search"),
        "ai_plan_max_web_search_calls": config.get("ai_plan", {}).get("max_web_search_calls"),
        "ai_plan_allow_risk_increase": config.get("ai_plan", {}).get("allow_risk_increase"),
        "ai_plan_allow_decision_override": config.get("ai_plan", {}).get("allow_decision_override"),
        "ai_plan_max_calls_per_day": config.get("ai_plan", {}).get("max_calls_per_day"),
        "ai_plan_max_risk_multiplier": config.get("ai_plan", {}).get("max_risk_multiplier"),
        "market_radar_enabled": config.get("market_radar", {}).get("enabled"),
        "market_radar_pairs": len(config.get("market_radar", {}).get("pairs", [])),
        "market_radar_context_pairs": len(config.get("market_radar", {}).get("context_pairs", [])),
        "pair": config.get("pair"),
        "poll_seconds": config.get("poll_seconds"),
        "launch_agent_installed": PLIST.exists(),
        "launchctl": shell(["launchctl", "print", f"gui/{shell(['id', '-u'])}/com.chengziyou.kraken-auto-trader"]),
        "processes": shell(["pgrep", "-af", BOT_PROCESS_PATTERN]),
        "latest_trade_event": latest_event,
        "latest_shadow_event": tail_jsonl(SHADOW_TRADES),
        "latest_ai_plan": json.loads(AI_PLAN.read_text()) if AI_PLAN.exists() else None,
        "latest_pnl_snapshot": latest_event.get("pnl_snapshot") if isinstance(latest_event, dict) else None,
        "ledger_exists": LEDGER.exists(),
        "notification_outbox_exists": OUTBOX.exists(),
        "queued_notification_count": len(OUTBOX.read_text().splitlines()) if OUTBOX.exists() else 0,
        "state": json.loads(STATE.read_text()) if STATE.exists() else None,
        "shadow_state": json.loads(SHADOW_STATE.read_text()) if SHADOW_STATE.exists() else None,
        "log_exists": LOG.exists(),
    }
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
