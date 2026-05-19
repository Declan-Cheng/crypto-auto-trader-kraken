# Contributing

Thanks for helping improve this experimental trading bot.

## Ground Rules

- Keep `paper` mode as the default.
- Do not remove hard risk gates without strong justification and tests.
- Do not add withdrawal, leverage, borrowing, or live shorting behavior in a casual PR.
- Do not commit local runtime files, ledgers, account data, keys, or logs.
- Include tests for strategy, risk, ledger, or execution changes when practical.

## Useful Commands

```bash
python -m pytest
python doctor.py --config config.example.json
python bot.py --config config.example.json --once
python backtest.py --config config.example.json
```

## Pull Request Ideas

- Add deterministic fixtures for market data.
- Improve fee and spread simulation.
- Add exchange abstraction tests.
- Improve order reconciliation after reconnects.
- Add safer AI-plan validation and explainability.
- Improve strategy reporting and walk-forward validation.
