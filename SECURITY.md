# Security Policy

## Secret Handling

Never commit:

- `secrets.env`
- Kraken API keys or API secrets
- OpenAI API keys
- live account balances
- order IDs, trade IDs, ledgers, fills, or notification queues
- screenshots or logs containing account identifiers

Use `secrets.env.example` as the only committed credential template.

## Exchange API Key Guidance

For live spot trading, use the narrowest Kraken permissions possible:

- Query Funds
- Query Open Orders & Trades
- Query Closed Orders & Trades
- Modify Orders
- Cancel/Close Orders

Do not grant withdrawal permissions.

## Reporting Issues

If you find a vulnerability that could leak credentials, bypass risk controls,
or place unintended live orders, please open a private security advisory or
contact the maintainer before posting exploit details publicly.
