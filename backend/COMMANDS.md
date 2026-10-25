# TokenIQ Backend Commands

Run these commands in a Bash terminal on the TokenIQ server. All exchange operations use Delta Testnet. Run each operation separately; this document is not a script to execute from top to bottom.

## 1. Sign in

Run this first, then keep using the same terminal for the API commands below. Enter your TokenIQ administrator password when prompted. The password is not printed or included in shell history.

```bash
cd /var/www/tokeniq

API_BASE="https://tokeniq.rookcapital.in"
COOKIE_FILE=$(mktemp /tmp/tokeniq-session.XXXXXX)

.venv/bin/python -c 'import getpass,json; print(json.dumps({"username":"admin","password":getpass.getpass("Password: ")}))' |
  curl --fail-with-body -sS \
    -c "$COOKIE_FILE" \
    -H "Content-Type: application/json" \
    --data-binary @- \
    "$API_BASE/api/login"
```

A successful response contains `"user":"admin"`. If sign-in fails, resolve that error before continuing.

## 2. Run a historical backtest

This example tests BTCUSD over the previous 24 hours using five-minute candles, ATR period 10, multiplier 3, one contract, a 100-point stop loss, and a 200-point target. It uses both SL/target exits and opposite-signal reversals.

It uses Testnet historical candles, saves a backtest report, and prints the run ID and performance metrics. It does not place exchange orders or start an instance.

```bash
.venv/bin/python - <<'PY' | curl --fail-with-body -sS -b "$COOKIE_FILE" -H "Content-Type: application/json" --data-binary @- "$API_BASE/api/backtest" | .venv/bin/python -c 'import sys,json; r=json.load(sys.stdin); print(json.dumps({"run_id":r.get("id"),"metrics":r.get("metrics"),"error":r.get("detail")},indent=2))'
import json
import time
from datetime import datetime, timezone

end = int(time.time()) // 300 * 300

def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()

print(json.dumps({
    "symbol": "BTCUSD",
    "resolution": "5m",
    "atr_period": 10,
    "multiplier": 3,
    "quantity": 1,
    "exit_mode": "both",
    "sl_points": 100,
    "target_points": 200,
    "start": stamp(end - 86400),
    "end": stamp(end),
    "timezone": "UTC",
    "capital": 10000,
    "data_source": "testnet"
}))
PY
```

Exit modes:

- `both`: SL/target exits and opposite-signal reversals.
- `sl_tp`: SL/target exits; opposite signals are ignored while holding a position.
- `opposite_signal`: opposite-signal reversals; point-based SL and target are ignored.

The date range must be in the past and fit within the 5,000-candle limit. Position-value and daily-loss limits still apply; an oversized quantity can result in blocked entries.

## 3. List saved instances

Use the returned ID to select the exact instance you want to start.

```bash
curl --fail-with-body -sS \
  -b "$COOKIE_FILE" \
  "$API_BASE/api/strategy/instances" |
  .venv/bin/python -c 'import sys,json; r=json.load(sys.stdin); print(json.dumps([{"id":i["id"],"name":i["name"],"running":i["enabled"]} for i in r["instances"]],indent=2))'
```

## 4. Start a saved strategy instance

Replace the placeholder with an ID from the previous command.

```bash
INSTANCE_ID="PASTE_INSTANCE_ID_HERE"

curl --fail-with-body -sS \
  -b "$COOKIE_FILE" \
  -H "Content-Type: application/json" \
  -d '{"action":"start"}' \
  "$API_BASE/api/strategy/instances/$INSTANCE_ID/control" |
  .venv/bin/python -m json.tool
```

The instance uses its saved strategy and execution-account settings. Start arms it for a fresh confirmed closed-candle flip; it does not immediately enter on an old signal.

## 5. Place a direct manual Testnet market order

This command submits a BTCUSD BUY order for one contract using the API credentials configured in `.env`. It does not resolve the primary account selected in Brokers; that account may be different from the `.env` account.

This is a manual exchange order. It is not owned or managed by a strategy instance, and this command does not attach SL/target orders. An external position on the same account and instrument can block an instance from starting.

```bash
cd /var/www/tokeniq

.venv/bin/python - <<'PY'
import asyncio
import json
import uuid
from dotenv import load_dotenv

load_dotenv(".env")
from backend import delta_client as exchange

async def main():
    product = await exchange.product("BTCUSD")
    client_id = "tqmanual" + uuid.uuid4().hex[:24]
    print("Client order ID:", client_id, flush=True)

    result = await exchange.request(
        "/v2/orders",
        private=True,
        method="POST",
        body={
            "product_id": product["id"],
            "side": "buy",
            "size": 1,
            "order_type": "market_order",
            "reduce_only": False,
            "client_order_id": client_id
        }
    )
    print(json.dumps(result, indent=2))

asyncio.run(main())
PY
```

Change `"side": "buy"` to `"side": "sell"` for a sell order. `size` is the number of contracts. A sell order with `reduce_only=False` can open or increase a short position.

If submission times out, check the printed client order ID before submitting again. A timeout does not prove the exchange rejected the order.

### Check a manual order by client order ID

This is a read-only status request using the same `.env` account.

```bash
.venv/bin/python - <<'PY'
import asyncio
import json
from dotenv import load_dotenv

load_dotenv(".env")
from backend import delta_client as exchange

client_id = "PASTE_CLIENT_ORDER_ID_HERE"

async def main():
    result = await exchange.request(
        "/v2/orders/client_order_id/" + client_id,
        private=True
    )
    print(json.dumps(result, indent=2))

asyncio.run(main())
PY
```

Replace `PASTE_CLIENT_ORDER_ID_HERE` with the client order ID printed during submission.

## 6. Remove the local sign-in cookie

Run this after finishing the API commands. The temporary cookie file contains a signed session token.

```bash
rm -f "$COOKIE_FILE"
unset COOKIE_FILE
```

Do not publish `.env`, API credentials, the `.broker-key` encryption key, or the temporary session cookie.
