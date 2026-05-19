#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mode TEXT,
  pair TEXT,
  event_type TEXT NOT NULL,
  signal TEXT,
  risk TEXT,
  order_status TEXT,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mode TEXT,
  pair TEXT,
  quote_balance TEXT,
  base_balance TEXT,
  price TEXT,
  equity_quote TEXT
);

CREATE TABLE IF NOT EXISTS signal_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  pair TEXT,
  signal TEXT,
  signal_reason TEXT,
  score INTEGER,
  rsi TEXT,
  macd_histogram TEXT,
  bollinger_z TEXT,
  momentum_pct TEXT,
  fast_sma TEXT,
  slow_sma TEXT,
  market_metrics_json TEXT
);

CREATE TABLE IF NOT EXISTS order_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  mode TEXT,
  pair TEXT,
  side TEXT,
  status TEXT,
  reason TEXT,
  volume TEXT,
  quote TEXT,
  pnl TEXT,
  raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  channel TEXT NOT NULL,
  status TEXT NOT NULL,
  title TEXT,
  message TEXT NOT NULL,
  error TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def record_event(db_path: Path, event: dict[str, Any]) -> None:
    ts = event.get("ts") or datetime.now(timezone.utc).isoformat()
    order = event.get("order") if isinstance(event.get("order"), dict) else {}
    event_type = classify_event(event)
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bot_events (ts, mode, pair, event_type, signal, risk, order_status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                event.get("mode"),
                event.get("pair"),
                event_type,
                event.get("signal"),
                event.get("risk"),
                order.get("status"),
                json.dumps(event, sort_keys=True),
            ),
        )
        conn.execute(
            """
            INSERT INTO account_snapshots (ts, mode, pair, quote_balance, base_balance, price, equity_quote)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                event.get("mode"),
                event.get("pair"),
                event.get("quote_balance"),
                event.get("base_balance"),
                event.get("price"),
                equity_quote(event),
            ),
        )
        conn.execute(
            """
            INSERT INTO signal_snapshots
            (ts, pair, signal, signal_reason, score, rsi, macd_histogram, bollinger_z, momentum_pct, fast_sma, slow_sma, market_metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                event.get("pair"),
                event.get("signal"),
                event.get("signal_reason"),
                event.get("score"),
                event.get("rsi"),
                event.get("macd_histogram"),
                event.get("bollinger_z"),
                event.get("momentum_pct"),
                event.get("fast_sma"),
                event.get("slow_sma"),
                json.dumps(event.get("market_metrics", {}), sort_keys=True),
            ),
        )
        if order and order.get("status") != "skipped":
            conn.execute(
                """
                INSERT INTO order_records (ts, mode, pair, side, status, reason, volume, quote, pnl, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    event.get("mode"),
                    event.get("pair"),
                    order.get("side") or event.get("signal"),
                    order.get("status"),
                    order.get("reason"),
                    order.get("volume") or order.get("base"),
                    order.get("quote") or order.get("quote_to_spend"),
                    order.get("pnl"),
                    json.dumps(order, sort_keys=True),
                ),
            )


def record_notification(db_path: Path, channel: str, status: str, title: str, message: str, error: str | None = None) -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO notifications (ts, channel, status, title, message, error) VALUES (?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(), channel, status, title, message, error),
        )


def classify_event(event: dict[str, Any]) -> str:
    if "error" in event:
        return "error"
    order = event.get("order") if isinstance(event.get("order"), dict) else {}
    status = order.get("status")
    if status in {"submitted", "closed", "filled_paper", "validated"}:
        return "trade"
    if status == "awaiting_manual_approval":
        return "approval_required"
    return "decision"


def equity_quote(event: dict[str, Any]) -> str | None:
    try:
        quote = Decimal(str(event.get("quote_balance", "0")))
        base = Decimal(str(event.get("base_balance", "0")))
        price = Decimal(str(event.get("price", "0")))
    except Exception:
        return None
    return str(quote + base * price)


def daily_summary(db_path: Path, mode: str | None = None) -> dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    mode_clause = "AND mode = ?" if mode else ""
    params = (today, mode) if mode else (today,)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT ts, equity_quote FROM account_snapshots
            WHERE ts >= ? AND equity_quote IS NOT NULL {mode_clause}
            ORDER BY ts ASC
            """,
            params,
        ).fetchall()
        orders = conn.execute(
            f"""
            SELECT side, status, reason, volume, quote, pnl FROM order_records
            WHERE ts >= ? AND status NOT IN ('skipped', 'validated') {mode_clause}
            ORDER BY ts ASC
            """,
            params,
        ).fetchall()
        signals = conn.execute(
            f"""
            SELECT signal, COUNT(*) FROM bot_events
            WHERE ts >= ? AND signal IS NOT NULL {mode_clause}
            GROUP BY signal
            """,
            params,
        ).fetchall()

    start_equity = Decimal(rows[0][1]) if rows else Decimal("0")
    end_equity = Decimal(rows[-1][1]) if rows else Decimal("0")
    return {
        "date_utc": today,
        "mode": mode,
        "start_equity_quote": str(start_equity) if rows else None,
        "end_equity_quote": str(end_equity) if rows else None,
        "pnl_quote": str(end_equity - start_equity) if rows else None,
        "event_count": len(rows),
        "orders": [
            {"side": r[0], "status": r[1], "reason": r[2], "volume": r[3], "quote": r[4], "pnl": r[5]}
            for r in orders
        ],
        "signal_counts": {signal: count for signal, count in signals},
    }


def format_daily_summary(summary: dict[str, Any]) -> str:
    return (
        f"Kraken交易日报 UTC {summary['date_utc']}\n"
        f"模式: {summary.get('mode') or 'all'}\n"
        f"期初权益: {summary.get('start_equity_quote')}\n"
        f"当前权益: {summary.get('end_equity_quote')}\n"
        f"今日盈亏: {summary.get('pnl_quote')}\n"
        f"记录次数: {summary.get('event_count')}\n"
        f"信号统计: {json.dumps(summary.get('signal_counts', {}), ensure_ascii=False, sort_keys=True)}\n"
        f"订单数: {len(summary.get('orders', []))}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Kraken auto-trader ledger")
    parser.add_argument("command", choices=["summary", "json-summary"])
    parser.add_argument("--db", type=Path, default=Path("ledger.sqlite3"))
    parser.add_argument("--mode", choices=["paper", "live"])
    args = parser.parse_args()

    summary = daily_summary(args.db, mode=args.mode)
    if args.command == "json-summary":
        print(json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(format_daily_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
