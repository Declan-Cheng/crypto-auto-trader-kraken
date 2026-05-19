#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


HERMES_HOME = Path.home() / ".hermes"
HERMES_AGENT = HERMES_HOME / "hermes-agent"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a message through Hermes send_message tool")
    parser.add_argument("--target", default="weixin")
    parser.add_argument("--message", required=True)
    args = parser.parse_args()

    sys.path.insert(0, str(HERMES_AGENT))
    load_env(HERMES_HOME / ".env")
    from tools.send_message_tool import send_message_tool

    result = send_message_tool({"action": "send", "target": args.target, "message": args.message})
    print(result)
    try:
        payload = json.loads(result)
    except Exception:
        return 1
    return 0 if payload.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
