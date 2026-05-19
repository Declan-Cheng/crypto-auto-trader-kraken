from __future__ import annotations

import json
from pathlib import Path

import wechat_control


def make_root(tmp_path: Path) -> Path:
    config = {
        "mode": "paper",
        "pair": "XBTCAD",
        "paths": {
            "ledger_db": "ledger.sqlite3",
        },
    }
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (tmp_path / "secrets.env").write_text("KRAKEN_API_KEY=test\n", encoding="utf-8")
    return tmp_path


def read_config(root: Path) -> dict:
    return json.loads((root / "config.json").read_text(encoding="utf-8"))


def test_start_request_does_not_enable_live(tmp_path: Path) -> None:
    root = make_root(tmp_path)

    result = wechat_control.process_message("开始交易", "allowed-user", root=root)

    assert result.handled is True
    assert "还没有启动真钱交易" in result.reply
    assert (root / wechat_control.REQUEST_FILE_NAME).exists()
    assert read_config(root)["mode"] == "paper"


def test_wrong_confirmation_keeps_paper(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    wechat_control.process_message("开始交易", "allowed-user", root=root)

    result = wechat_control.process_message("确认启动真钱后台交易 WRONG", "allowed-user", root=root)

    assert "确认码不匹配" in result.reply
    assert read_config(root)["mode"] == "paper"


def test_dry_run_confirmation_does_not_start_service(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    wechat_control.process_message("开始交易", "allowed-user", root=root)
    pending = json.loads((root / wechat_control.REQUEST_FILE_NAME).read_text(encoding="utf-8"))

    result = wechat_control.process_message(
        f"确认启动真钱后台交易 {pending['code']}",
        "allowed-user",
        root=root,
        dry_run=True,
    )

    assert "演练模式" in result.reply
    assert read_config(root)["mode"] == "paper"


def test_unknown_message_passes_through(tmp_path: Path) -> None:
    root = make_root(tmp_path)

    result = wechat_control.process_message("随便聊聊", "allowed-user", root=root)

    assert result.handled is False
