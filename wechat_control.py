#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ledger import connect


ROOT = Path(__file__).resolve().parent
REQUEST_FILE_NAME = "wechat_control_request.json"
START_TTL_MINUTES = 10
CONFIRM_PREFIX = "确认启动真钱后台交易"
LIVE_ACK_VALUE = "I_UNDERSTAND_THIS_CAN_LOSE_MONEY"


@dataclass(frozen=True)
class CommandResult:
    handled: bool
    reply: str
    action: str = "none"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def project_python(root: Path) -> str:
    candidate = root / ".venv" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return sys.executable


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_command(args: list[str], root: Path, timeout: int = 60) -> tuple[int, str]:
    result = subprocess.run(args, cwd=root, text=True, capture_output=True, check=False, timeout=timeout)
    output = (result.stdout.strip() or result.stderr.strip() or f"exit={result.returncode}").strip()
    return result.returncode, output


def config_path(root: Path) -> Path:
    return root / "config.json"


def request_path(root: Path) -> Path:
    return root / REQUEST_FILE_NAME


def load_config(root: Path) -> dict[str, Any]:
    return load_json(config_path(root), {})


def save_config(root: Path, config: dict[str, Any]) -> None:
    write_json(config_path(root), config)


def set_mode(root: Path, mode: str) -> None:
    config = load_config(root)
    config["mode"] = mode
    save_config(root, config)


def update_live_ack(root: Path, enabled: bool) -> None:
    env_path = root / "secrets.env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    kept = [line for line in lines if not line.strip().startswith("KRAKEN_LIVE_TRADING_ACK=")]
    if enabled:
        kept.append(f"KRAKEN_LIVE_TRADING_ACK={LIVE_ACK_VALUE}")
    env_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")


def record_control_event(root: Path, user_id: str, action: str, message: str, status: str) -> None:
    config = load_config(root)
    paths = config.get("paths", {})
    db_path = root / paths.get("ledger_db", "ledger.sqlite3")
    payload = {
        "ts": now_utc().isoformat(),
        "mode": config.get("mode"),
        "pair": config.get("pair"),
        "event_type": "wechat_control",
        "control_action": action,
        "control_status": status,
        "user_id": user_id,
        "message": message,
    }
    try:
        with connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO bot_events (ts, mode, pair, event_type, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (payload["ts"], payload.get("mode"), payload.get("pair"), "wechat_control", json.dumps(payload, sort_keys=True)),
            )
    except Exception:
        # Control commands must not fail just because the ledger is temporarily locked.
        pass


def summarize_status(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "status.py")], root, timeout=45)
    if code != 0:
        return f"状态读取失败：{output[:800]}"
    data = load_json_from_text(output)
    if not isinstance(data, dict):
        return output[:1200]
    running = bool(str(data.get("processes") or "").strip())
    launchctl = str(data.get("launchctl") or "")
    latest = data.get("latest_trade_event") or {}
    signal = latest.get("signal") if isinstance(latest, dict) else None
    order = latest.get("order") if isinstance(latest, dict) else {}
    order_status = order.get("status") if isinstance(order, dict) else None
    return (
        "Kraken机器人状态\n"
        f"模式: {data.get('config_mode')}\n"
        f"交易对: {data.get('pair')}\n"
        f"后台服务: {'运行中' if running or 'state = running' in launchctl else '未运行'}\n"
        f"审批模式: {data.get('approval_mode')} / 激进阈值<= {data.get('aggressive_buy_score_max')}\n"
        f"AI计划: {'开' if data.get('ai_plan_enabled') else '关'} / {data.get('ai_plan_model')} / reasoning={data.get('ai_plan_reasoning_effort')} / 搜索={'开' if data.get('ai_plan_web_search') else '关'}\n"
        f"AI权限: 放大={'允许' if data.get('ai_plan_allow_risk_increase') else '不允许'} / 二次挑战={'允许' if data.get('ai_plan_allow_decision_override') else '不允许'} / 上限x{data.get('ai_plan_max_risk_multiplier')}\n"
        f"广域雷达: {'开' if data.get('market_radar_enabled') else '关'} / 市场数={data.get('market_radar_pairs')}+{data.get('market_radar_context_pairs')}\n"
        f"做空: 真实盘={'开' if data.get('live_short_enabled') else '关'} / 影子盘={'开' if data.get('shadow_short_enabled') else '关'}\n"
        f"最近信号: {signal or '暂无'}\n"
        f"最近订单状态: {order_status or '暂无'}\n"
        f"通知待发: {data.get('queued_notification_count', 0)}"
    )


def load_json_from_text(text: str) -> Any:
    try:
        return json.loads(text)
    except Exception:
        return None


def explain_signal(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "control.py"), "explain"], root, timeout=45)
    if code != 0:
        return f"信号解释失败：{output[:800]}"
    data = load_json_from_text(output)
    if not isinstance(data, dict):
        return output[:1200]
    if data.get("status") in {"none", None}:
        return "当前没有等待审批的激进交易。"
    return (
        "当前待审批交易\n"
        f"摘要: {data.get('summary')}\n"
        f"原因: {data.get('reason')}\n"
        f"风险: {data.get('risk')}\n"
        f"过期: {data.get('expires_at')}\n"
        "如确认执行，回复：批准交易；如取消，回复：拒绝交易"
    )


def daily_report(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "report.py"), "daily"], root, timeout=45)
    if code != 0:
        return f"日报读取失败：{output[:800]}"
    return output[:1400]


def research_report(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "research.py"), "show", "--config", str(root / "config.json")], root, timeout=45)
    if code != 0:
        return f"研究风险读取失败：{output[:800]}"
    return output[:1400]


def scorecard_report(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "scorecard.py"), "show", "--config", str(root / "config.json")], root, timeout=45)
    if code != 0:
        return f"策略评分读取失败：{output[:800]}"
    return output[:1400]


def ai_plan_report(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "ai_planner.py"), "show", "--plan", str(root / "ai_plan.json")], root, timeout=45)
    if code != 0:
        return f"AI计划读取失败：{output[:800]}"
    return output[:1400]


def start_request(root: Path, dry_run: bool) -> str:
    code = secrets.token_hex(3).upper()
    expires_at = now_utc() + timedelta(minutes=START_TTL_MINUTES)
    if dry_run:
        return (
            "演练模式：会创建“开始交易”二次确认请求，但现在没有落盘，也没有启动真钱交易。\n"
            f"示例确认句：{CONFIRM_PREFIX} {code}"
        )
    write_json(
        request_path(root),
        {
            "action": "start_live",
            "code": code,
            "created_at": now_utc().isoformat(),
            "expires_at": expires_at.isoformat(),
        },
    )
    return (
        "已收到“开始交易”请求，但还没有启动真钱交易。\n"
        f"如果你确认接受真实亏损风险，请在 {START_TTL_MINUTES} 分钟内回复：\n"
        f"{CONFIRM_PREFIX} {code}"
    )


def confirm_start(root: Path, text: str, dry_run: bool) -> str:
    pending = load_json(request_path(root), {})
    code = text.removeprefix(CONFIRM_PREFIX).strip().upper()
    if pending.get("action") != "start_live":
        return "没有待确认的启动请求。请先发送：开始交易"
    try:
        expires_at = datetime.fromisoformat(str(pending.get("expires_at")))
    except Exception:
        return "启动请求已损坏，已取消。请重新发送：开始交易"
    if now_utc() > expires_at:
        request_path(root).unlink(missing_ok=True)
        return "启动确认已过期，已取消。请重新发送：开始交易"
    if code != str(pending.get("code", "")).upper():
        return "确认码不匹配，没有启动。请按上一条消息里的完整确认句回复。"

    if dry_run:
        return "演练模式：确认码有效，但没有切换 live，也没有启动后台服务。"

    set_mode(root, "live")
    update_live_ack(root, enabled=True)
    run_command([project_python(root), str(root / "install_launch_agent.py"), "start"], root, timeout=45)
    request_path(root).unlink(missing_ok=True)
    return "已切换为 live 并启动后台服务。\n" + summarize_status(root)


def pause_trading(root: Path, dry_run: bool) -> str:
    if dry_run:
        return "演练模式：会停止后台服务、切回 paper，并移除 live ack。"
    run_command([project_python(root), str(root / "install_launch_agent.py"), "stop"], root, timeout=45)
    set_mode(root, "paper")
    update_live_ack(root, enabled=False)
    return "已暂停交易：后台服务已停止，配置已切回 paper。"


def approve_trade(root: Path, dry_run: bool) -> str:
    command = "approve-validate" if dry_run else "approve"
    code, output = run_command([project_python(root), str(root / "control.py"), command], root, timeout=60)
    if code != 0:
        return f"批准失败：{output[:1000]}"
    return "批准处理完成。\n" + output[:1200]


def reject_trade(root: Path) -> str:
    code, output = run_command([project_python(root), str(root / "control.py"), "reject"], root, timeout=45)
    if code != 0:
        return f"拒绝失败：{output[:1000]}"
    return "已拒绝待审批交易。\n" + output[:1200]


def help_text() -> str:
    return (
        "可用命令：\n"
        "状态\n"
        "今日盈亏 / 日报\n"
        "研究 / 市场情报\n"
        "评分 / 复盘\n"
        "AI计划 / 长期计划\n"
        "解释 / 当前信号\n"
        "开始交易（会要求二次确认）\n"
        "暂停交易\n"
        "批准交易 / 拒绝交易（仅用于已发出的激进交易审批）"
    )


def process_message(message: str, user_id: str, *, root: Path = ROOT, dry_run: bool = False) -> CommandResult:
    text = " ".join(message.strip().split())
    if not text:
        return CommandResult(False, "")

    if text in {"帮助", "菜单", "命令"}:
        result = CommandResult(True, help_text(), "help")
    elif text in {"状态", "进度", "机器人状态"}:
        result = CommandResult(True, summarize_status(root), "status")
    elif text in {"今日盈亏", "日报", "今日报告"}:
        result = CommandResult(True, daily_report(root), "daily_report")
    elif text in {"研究", "市场情报", "风险情报", "新闻风险"}:
        result = CommandResult(True, research_report(root), "research_report")
    elif text in {"评分", "复盘", "策略评分", "影子盘"}:
        result = CommandResult(True, scorecard_report(root), "scorecard_report")
    elif text in {"AI计划", "长期计划", "策略计划"}:
        result = CommandResult(True, ai_plan_report(root), "ai_plan_report")
    elif text in {"解释", "当前信号", "为什么交易"}:
        result = CommandResult(True, explain_signal(root), "explain")
    elif text == "开始交易":
        result = CommandResult(True, start_request(root, dry_run), "start_request")
    elif text.startswith(CONFIRM_PREFIX):
        result = CommandResult(True, confirm_start(root, text, dry_run), "start_confirm")
    elif text in {"暂停交易", "停止交易", "关掉交易"}:
        result = CommandResult(True, pause_trading(root, dry_run), "pause")
    elif text in {"批准交易", "同意交易"}:
        result = CommandResult(True, approve_trade(root, dry_run), "approve_trade")
    elif text in {"拒绝交易", "取消交易"}:
        result = CommandResult(True, reject_trade(root), "reject_trade")
    else:
        return CommandResult(False, "")

    record_control_event(root, user_id, result.action, text, "handled")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Handle Weixin commands for the Kraken auto trader")
    parser.add_argument("--message", required=True)
    parser.add_argument("--user-id", default="")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    result = process_message(args.message, args.user_id, root=args.root, dry_run=args.dry_run)
    print(json.dumps(result.__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
