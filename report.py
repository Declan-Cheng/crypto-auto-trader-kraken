#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from bot import load_config
from ledger import daily_summary, format_daily_summary
from notifier import notify_channels


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or send Kraken auto-trader reports")
    parser.add_argument("command", choices=["daily", "send-daily"])
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    args = parser.parse_args()

    config = load_config(args.config)
    message = format_daily_summary(daily_summary(config.ledger_db, mode=config.mode))
    if args.command == "send-daily":
        notify_channels(config, "Kraken交易日报", message, financial=True)
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
