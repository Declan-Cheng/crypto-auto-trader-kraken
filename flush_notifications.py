#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bot import load_config
from notifier import send_weixin


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Retry queued Weixin notifications")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--limit", type=int, default=None, help="Only retry the first N queued messages")
    parser.add_argument("--newest-first", action="store_true", help="Retry newest queued messages first")
    parser.add_argument("--timeout-seconds", type=int, default=20, help="Per-message send timeout")
    args = parser.parse_args()

    config = load_config(args.config)
    path = config.notification_outbox_file
    if not path.exists():
        print("no queued notifications")
        return 0

    raw_items = []
    for line in path.read_text().splitlines():
        if line.strip():
            raw_items.append(json.loads(line))
    indexed_items = list(enumerate(raw_items))
    if args.newest_first:
        indexed_items.reverse()
    retry_indices = {index for index, _ in indexed_items[: args.limit]} if args.limit else {index for index, _ in indexed_items}

    remaining_by_index = {}
    sent = 0
    for index, item in enumerate(raw_items):
        if index not in retry_indices:
            remaining_by_index[index] = item
            continue
        ok, error = send_weixin(item["target"], f"{item['title']}\n{item['message']}", timeout_seconds=args.timeout_seconds)
        if ok:
            sent += 1
        else:
            item["reason"] = error
            remaining_by_index[index] = item

    remaining = [remaining_by_index[index] for index in sorted(remaining_by_index)]
    if remaining:
        path.write_text("\n".join(json.dumps(item, sort_keys=True, ensure_ascii=False) for item in remaining) + "\n")
    else:
        path.unlink()
    print(json.dumps({"sent": sent, "remaining": len(remaining)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
