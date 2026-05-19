#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from bot import KrakenClient, load_config, load_env_file, notify, pending_is_active, read_pending_order


ROOT = Path(__file__).resolve().parent
BOT_PROCESS_PATTERN = r"[b]ot\.py --config"


def run(args: list[str]) -> str:
    return subprocess.run(args, text=True, capture_output=True, check=False).stdout.strip()


def service_name() -> str:
    return f"gui/{run(['id', '-u'])}/com.chengziyou.kraken-auto-trader"


def status(config_path: Path, env_file: Path) -> dict:
    load_env_file(env_file)
    config = load_config(config_path)
    pending = read_pending_order(config)
    return {
        "mode": config.mode,
        "pair": config.pair,
        "approval_mode": config.execution.approval_mode,
        "aggressive_buy_score_max": config.execution.aggressive_buy_score_max,
        "processes": run(["pgrep", "-af", BOT_PROCESS_PATTERN]),
        "launch_agent": run(["launchctl", "print", service_name()]),
        "pending_order": pending,
        "pending_active": pending_is_active(pending),
        "latest_event": latest_event(config.trade_log),
        "power": run(["pmset", "-g", "batt"]),
        "sleep_assertions_summary": run(["pmset", "-g", "assertions"]),
    }


def latest_event(path: Path) -> dict | None:
    if not path.exists():
        return None
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def approve(config_path: Path, env_file: Path, validate_only: bool) -> dict:
    load_env_file(env_file)
    config = load_config(config_path)
    pending = read_pending_order(config)
    if not pending_is_active(pending):
        return {"status": "skipped", "reason": "no_active_pending_order"}

    plan = pending["order_plan"]
    client = KrakenClient(os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET"))
    try:
        result = client.add_market_order(
            plan["pair"],
            plan["side"],
            volume_from_plan(plan),
            validate_only=validate_only,
        )
    finally:
        client.close()

    pending["status"] = "validated" if validate_only else "submitted"
    pending["approved_at"] = datetime.now(timezone.utc).isoformat()
    pending["kraken_result"] = result
    config.pending_order_file.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n")
    notify("Kraken approval processed", f"{pending['status']} {plan['side'].upper()} {plan['pair']} volume={plan['volume']}")
    return pending


def volume_from_plan(plan: dict):
    from decimal import Decimal

    return Decimal(str(plan["volume"]))


def reject(config_path: Path) -> dict:
    config = load_config(config_path)
    pending = read_pending_order(config)
    if not pending:
        return {"status": "skipped", "reason": "no_pending_order_file"}
    pending["status"] = "rejected"
    pending["rejected_at"] = datetime.now(timezone.utc).isoformat()
    config.pending_order_file.write_text(json.dumps(pending, indent=2, sort_keys=True) + "\n")
    notify("Kraken trade rejected", f"{pending.get('signal', '').upper()} {pending.get('pair', '')}")
    return pending


def explain(config_path: Path) -> dict:
    config = load_config(config_path)
    pending = read_pending_order(config)
    if not pending:
        return {"status": "none", "message": "No pending order."}
    return {
        "status": pending.get("status"),
        "summary": f"{pending.get('signal', '').upper()} {pending.get('pair')} score={pending.get('score')} at price={pending.get('price')}",
        "reason": pending.get("signal_reason"),
        "risk": pending.get("risk"),
        "market_metrics": pending.get("market_metrics"),
        "order_plan": pending.get("order_plan"),
        "expires_at": pending.get("expires_at"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Control and inspect the Kraken auto trader")
    parser.add_argument("command", choices=["status", "approve", "reject", "explain", "approve-validate"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / "secrets.env")
    args = parser.parse_args()

    if args.command == "status":
        payload = status(args.config, args.env_file)
    elif args.command == "approve":
        payload = approve(args.config, args.env_file, validate_only=False)
    elif args.command == "approve-validate":
        payload = approve(args.config, args.env_file, validate_only=True)
    elif args.command == "reject":
        payload = reject(args.config)
    else:
        payload = explain(args.config)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
