#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
mkdir -p logs

while true; do
  if "$ROOT/.venv/bin/python" - <<'PY' >/dev/null 2>&1
import json
import urllib.request

with urllib.request.urlopen("https://api.kraken.com/0/public/Time", timeout=10) as response:
    payload = json.loads(response.read().decode("utf-8"))
if payload.get("error"):
    raise SystemExit(1)
PY
  then
    . "$ROOT/.venv/bin/activate"
    python "$ROOT/bot.py" --config "$ROOT/config.json" --env-file "$ROOT/secrets.env"
  else
    printf '{"error":"network_unavailable","next_retry_seconds":60,"ts":"%s"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ROOT/logs/kraken-auto-trader.log"
    sleep 60
  fi
done
