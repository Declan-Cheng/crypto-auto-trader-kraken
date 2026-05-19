# OpenAI API Key Setup

把 OpenAI API key 放在同目录的 `secrets.env` 里：

```env
OPENAI_API_KEY=你的_OpenAI_API_Key
```

不要把 key 发到聊天里，也不要提交到 Git。

保存后重启机器人。如果你是手动运行，停止旧进程后重新执行：

```bash
cd kraken_auto_trader
. .venv/bin/activate
python doctor.py
python status.py
```

如果你安装了 macOS LaunchAgent，可以用：

```bash
python install_launch_agent.py restart
```

当前 AI 交易设置：

- 主模型：`gpt-5.5`
- 自动备用模型：`gpt-5.1`, `gpt-5`
- Reasoning：`high`
- Web search：开启
- AI 权限：`chief`，可以覆盖软拒绝
- 不能覆盖：余额不足、交易所校验、点差过宽、权益熔断、止损/止盈卖出、重连观察、真实做空禁用
- 调用频率：由 `config.json` 的 `ai_plan.max_calls_per_day` 和 `refresh_interval_minutes` 控制
