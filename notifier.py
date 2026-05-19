from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ledger import record_notification


ROOT = Path(__file__).resolve().parent
HERMES_PYTHON = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"


def macos_notify(title: str, message: str) -> bool:
    script = 'display notification "{message}" with title "{title}"'.format(
        title=title.replace("\\", "\\\\").replace('"', '\\"'),
        message=message.replace("\\", "\\\\").replace('"', '\\"'),
    )
    result = subprocess.run(["/usr/bin/osascript", "-e", script], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def queue_outbox(outbox_path: Path, channel: str, target: str, title: str, message: str, reason: str) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "target": target,
        "title": title,
        "message": message,
        "status": "queued",
        "reason": reason,
    }
    with outbox_path.open("a") as fh:
        fh.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")


def send_weixin(target: str, message: str, *, timeout_seconds: int = 45) -> tuple[bool, str | None]:
    if not HERMES_PYTHON.exists():
        return False, f"missing Hermes python: {HERMES_PYTHON}"
    try:
        result = subprocess.run(
            [str(HERMES_PYTHON), str(ROOT / "hermes_send_message.py"), "--target", target, "--message", message],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout_after_{timeout_seconds}s"
    if result.returncode == 0:
        return True, None
    return False, (result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}")[:1000]


def notify_channels(config: Any, title: str, message: str, *, financial: bool = False) -> None:
    raw = getattr(config, "raw", {}) if hasattr(config, "raw") else {}
    notifications = raw.get("notifications", {})
    ledger_db = config.ledger_db

    macos_cfg = notifications.get("macos", {})
    if macos_cfg.get("enabled", True):
        ok = macos_notify(title, message)
        record_notification(ledger_db, "macos", "sent" if ok else "failed", title, message, None if ok else "osascript failed")

    weixin_cfg = notifications.get("weixin", {})
    if weixin_cfg.get("enabled", False):
        target = weixin_cfg.get("target", "weixin")
        if financial and not weixin_cfg.get("send_financial_data", False):
            queue_outbox(config.notification_outbox_file, "weixin", target, title, message, "financial_data_disabled")
            record_notification(ledger_db, "weixin", "queued", title, message, "financial_data_disabled")
            return
        ok, error = send_weixin(target, f"{title}\n{message}")
        if ok:
            record_notification(ledger_db, "weixin", "sent", title, message)
        else:
            queue_outbox(config.notification_outbox_file, "weixin", target, title, message, error or "send_failed")
            record_notification(ledger_db, "weixin", "queued", title, message, error)
