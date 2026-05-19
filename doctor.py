#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bot import LIVE_ACK, KrakenClient, load_config, load_env_file


def main() -> int:
    parser = argparse.ArgumentParser(description="Check config, environment, and Kraken public connectivity")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--env-file", type=Path, default=Path("secrets.env"))
    args = parser.parse_args()

    load_env_file(args.env_file)
    config = load_config(args.config)
    checks = {
        "config_loaded": True,
        "mode": config.mode,
        "pair": config.pair,
        "ws_symbol": config.ws_symbol,
        "kraken_public_ok": False,
        "kraken_private_keys_present": bool(os.environ.get("KRAKEN_API_KEY") and os.environ.get("KRAKEN_API_SECRET")),
        "live_ack_present": os.environ.get("KRAKEN_LIVE_TRADING_ACK") == LIVE_ACK,
        "openai_key_present": bool(os.environ.get("OPENAI_API_KEY")),
        "llm_enabled": config.llm.enabled,
    }

    client = KrakenClient(os.environ.get("KRAKEN_API_KEY"), os.environ.get("KRAKEN_API_SECRET"))
    try:
        ticker = client.ticker(config.pair)
        checks["kraken_public_ok"] = True
        checks["ticker"] = {
            "bid": str(ticker.bid),
            "ask": str(ticker.ask),
            "last": str(ticker.last),
            "spread_bps": str(ticker.spread_bps),
        }
    finally:
        client.close()

    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
