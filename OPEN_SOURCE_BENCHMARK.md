# Open Source Benchmark Notes

This project was compared against several mature open-source trading systems in
May 2026. The goal is not to copy large frameworks wholesale, but to borrow
small, testable mechanisms that fit a tiny Kraken spot account.

## Repositories Reviewed

- [freqtrade/freqtrade](https://github.com/freqtrade/freqtrade)
- [Drakkar-Software/OctoBot](https://github.com/Drakkar-Software/OctoBot)
- [jesse-ai/jesse](https://github.com/jesse-ai/jesse)
- [hummingbot/hummingbot](https://github.com/hummingbot/hummingbot)
- [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader)

## What They Do Better

- Freqtrade has mature pairlists, spread/price/age filters, protections,
  hyperopt, dry-run workflows, and UI/reporting.
- OctoBot has broad strategy modes such as AI, Grid, DCA, baskets,
  TradingView, and social indicators.
- Jesse has a strong research workflow with multi-timeframe/multi-symbol
  backtesting, benchmark runs, Monte Carlo analysis, and ML data collection.
- Hummingbot is much stronger for market making, connector architecture, and
  cross-exchange execution.
- NautilusTrader has the most professional event-driven architecture, order
  lifecycle modeling, risk/execution engines, and high-fidelity backtesting.

## What This Bot Still Does Differently

- Small Kraken spot account focus.
- Simple local setup.
- Paper-by-default behavior.
- Local ledger and review bundles.
- Optional OpenAI strategy planner with hard risk gates.
- Runtime files are intentionally local and gitignored.

## Improvements Borrowed First

1. Dynamic pairlist inspired by Freqtrade's VolumePairList and SpreadFilter:
   Kraken markets can be filtered by quote asset, spread, price, and quote
   volume before rotation scoring.

2. Monte Carlo backtest stress testing inspired by Jesse:
   `backtest.py --monte-carlo N` shuffles equity-curve returns to estimate
   ending-equity and drawdown distributions.

## Next Good Candidates

- Freqtrade-style protections: per-pair cooldown locks, stoploss guard, and
  rolling max-drawdown locks.
- Jesse-style benchmark runner: run one strategy over many pairs/timeframes and
  rank by return, drawdown, and trade count.
- Nautilus-style order lifecycle states: accepted, partially filled, filled,
  canceled, rejected, stale, reconciled.
- OctoBot-style smart DCA module: only in paper mode first, with fee/spread
  gates and explicit market-regime constraints.
- Hummingbot-style connector boundary: isolate Kraken REST/WebSocket code from
  strategy and risk code.
