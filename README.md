# Kraken Auto Trader

Experimental Kraken spot-trading bot for small-account research, paper trading,
strategy review, and community improvement.

This project is **not investment advice** and does **not guarantee profit**.
Crypto trading can lose money quickly. The default mode is `paper`.

## What It Does

- Polls Kraken public market data and calculates short-term technical signals.
- Supports paper trading by default and live Kraken spot trading when explicitly enabled.
- Tracks decisions, balances, PnL snapshots, orders, notifications, and review bundles.
- Scans multiple crypto pairs and basic USD/stablecoin/FX context pairs.
- Can build a dynamic Kraken pairlist filtered by quote asset, spread, price,
  and quote volume before selecting a rotation candidate.
- Applies hard risk gates for spread, fees, volatility, daily drawdown, max position, reconnect observation, and live acknowledgement.
- Optionally calls an OpenAI model through `OPENAI_API_KEY` for a structured strategy plan with web search when supported.
- Generates daily/weekly review bundles that can be analyzed by humans or external models.

## Safety Boundaries

- Default config is `paper`.
- Live mode requires Kraken API credentials and `KRAKEN_LIVE_TRADING_ACK`.
- Do not enable withdrawal permission on exchange API keys.
- No live leverage, margin, derivatives, borrowing, or withdrawals.
- Short logic is shadow/risk-analysis only unless you explicitly implement and review a compliant venue-specific integration.
- Hard risk gates are intended to remain non-overridable by model output.

## Quick Start

```bash
cd kraken_auto_trader
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.json
cp secrets.env.example secrets.env
chmod 600 secrets.env
```

Run one paper-trading cycle:

```bash
python bot.py --config config.json --once
```

Run continuously in paper mode:

```bash
python bot.py --config config.json
```

Preflight public connectivity and local config:

```bash
python doctor.py --config config.json
```

Run tests:

```bash
python -m pytest
```

Backtest with Monte Carlo stress testing:

```bash
python backtest.py --config config.json --monte-carlo 250 --seed 42
```

## Live Trading Setup

Create a Kraken API key with the minimum required trading permissions. Suggested:

- Query Funds
- Query Open Orders & Trades
- Query Closed Orders & Trades
- Modify Orders
- Cancel/Close Orders

Do **not** grant withdrawal permission.

Put credentials in `secrets.env`:

```bash
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
KRAKEN_LIVE_TRADING_ACK=I_UNDERSTAND_THIS_CAN_LOSE_MONEY
OPENAI_API_KEY=
```

Then change `config.json`:

```json
{
  "mode": "live"
}
```

Validate order payloads without sending live orders:

```bash
python bot.py --config config.json --once --validate-orders
```

## OpenAI Strategy Planner

The public version only reads `OPENAI_API_KEY` from the environment or
`secrets.env`. It does not read local ChatGPT, Codex, browser, or desktop-app
auth files.

The planner is optional. If no API key is present, the bot falls back to the
local quantitative plan.

Relevant config:

```json
{
  "ai_plan": {
    "enabled": true,
    "model": "gpt-5.5",
    "model_fallbacks": ["gpt-5.1", "gpt-5"],
    "reasoning_effort": "high",
    "web_search": true,
    "force_web_search": true,
    "refresh_interval_minutes": 90,
    "max_calls_per_day": 10
  }
}
```

## Review Tools

Status:

```bash
python status.py
python control.py status
```

Research snapshot:

```bash
python research.py show --config config.json
```

Scorecard:

```bash
python scorecard.py show --config config.json
```

Daily/weekly bundles:

```bash
python review_pipeline.py daily
python review_pipeline.py weekly
python review_pipeline.py nightly --send
```

Model feedback ingestion:

```bash
python review_pipeline.py ingest-feedback < model_feedback.md
python review_pipeline.py apply-proposal --proposal model_reviews/example_proposal.json
python review_pipeline.py apply-proposal --proposal model_reviews/example_proposal.json --apply
```

## Runtime Files

These files are generated locally and are intentionally ignored by git:

- `config.json`
- `secrets.env`
- `state.json`
- `trades.jsonl`
- `order_audit.jsonl`
- `ledger.sqlite3`
- `notification_outbox.jsonl`
- `pending_order.json`
- `research_snapshot.json`
- `ai_plan.json`
- `ai_plan_log.jsonl`
- `shadow_state.json`
- `shadow_trades.jsonl`
- `reports/`
- `model_reviews/`
- `logs/`

## Main Files

- `bot.py`: main Kraken client, strategy, execution, and risk gates.
- `ai_planner.py`: model-assisted or quant fallback strategy planner.
- `research.py`: public research/news/status risk overlay.
- `scorecard.py`: decision and performance scorecard.
- `ledger.py`: SQLite ledger and reporting helpers.
- `review_pipeline.py`: daily/weekly review bundles and model feedback proposals.
- `control.py`: local status, approval, and pending-order controls.
- `doctor.py`: environment and connectivity checks.
- `backtest.py`: quick Kraken OHLC backtest.
- `stream_market.py`: Kraken WebSocket market recorder.
- `notifier.py`: local notification adapters.
- `wechat_control.py`: optional external command gateway parser.

## Contributing

Good community contribution areas:

- Better fee/spread modeling for small accounts.
- More robust backtesting and walk-forward validation.
- Cleaner exchange abstractions.
- Safer model-plan evaluation.
- Better regime detection and pair rotation.
- Better tests for edge cases, reconnect behavior, and order reconciliation.

See [OPEN_SOURCE_BENCHMARK.md](OPEN_SOURCE_BENCHMARK.md) for notes from
comparing this bot against Freqtrade, OctoBot, Jesse, Hummingbot, and
NautilusTrader.

Please keep live-trading changes conservative by default and include tests for
new risk or execution behavior.
