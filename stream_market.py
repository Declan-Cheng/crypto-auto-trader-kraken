#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import websockets

from bot import load_config


KRAKEN_WS_V2 = "wss://ws.kraken.com/v2"


async def stream_market(config_path: Path, output_path: Path, max_messages: int | None) -> None:
    config = load_config(config_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    async with websockets.connect(KRAKEN_WS_V2, ping_interval=20, ping_timeout=20) as websocket:
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "ticker",
                        "symbol": [config.ws_symbol],
                        "event_trigger": "bbo",
                    },
                    "req_id": 1,
                }
            )
        )
        await websocket.send(
            json.dumps(
                {
                    "method": "subscribe",
                    "params": {
                        "channel": "ohlc",
                        "symbol": [config.ws_symbol],
                        "interval": config.candle_interval_minutes,
                    },
                    "req_id": 2,
                }
            )
        )

        count = 0
        with output_path.open("a") as fh:
            async for message in websocket:
                event = normalize_event(json.loads(message))
                fh.write(json.dumps(event, sort_keys=True) + "\n")
                fh.flush()
                print(json.dumps(event, ensure_ascii=False, sort_keys=True))
                count += 1
                if max_messages is not None and count >= max_messages:
                    return


def normalize_event(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "source": "kraken_ws_v2",
        "message": message,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Stream Kraken realtime ticker/OHLC data to a JSONL file")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--output", type=Path, default=Path("market_stream.jsonl"))
    parser.add_argument("--max-messages", type=int, default=None)
    args = parser.parse_args()

    asyncio.run(stream_market(args.config, args.output, args.max_messages))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
