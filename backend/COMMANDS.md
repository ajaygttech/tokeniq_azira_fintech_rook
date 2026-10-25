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

## 6. View strategy, candle and error logs through the API

Sign in using section 1 first. These commands only read logs; they do not start trading. You can also open **Strategy logs** on the website.

Latest 300 saved events, as JSON (newest first):

```bash
curl --fail-with-body -sS -b "$COOKIE_FILE" "$API_BASE/api/logs" |
  .venv/bin/python -m json.tool
```

Readable events in IST, oldest to newest within the latest 300:

```bash
curl --fail-with-body -sS -b "$COOKIE_FILE" "$API_BASE/api/logs" |
  .venv/bin/python -c 'import sys,json; from datetime import datetime; from zoneinfo import ZoneInfo; rows=json.load(sys.stdin); [print(datetime.fromisoformat(r["time"]).astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M:%S IST"), r["level"], r["message"]) for r in reversed(rows)]'
```

Filter those events by instrument, instance name, BUY/SELL or error. Change the `rg` pattern as needed:

```bash
curl --fail-with-body -sS -b "$COOKIE_FILE" "$API_BASE/api/logs" |
  .venv/bin/python -c 'import sys,json; [print(r["time"],r["level"],r["message"]) for r in reversed(json.load(sys.stdin))]' |
  rg -i 'ETHUSD|ERROR|Red flip|Green flip'
```

API timestamps in this filtered command are UTC. No matching event makes `rg` exit with code 1. A candle log includes OHLC, ATR, Supertrend, direction, flip signal and decision. An initial baseline, HOLD or WAIT is not an order confirmation. Successful entry logs include filled quantity and average fill price. A reversal can create two orders: close the old position, then open the opposite position.

Download the latest 300 events as CSV:

```bash
curl --fail-with-body -sS -b "$COOKIE_FILE" \
  "$API_BASE/api/export/logs" -o /tmp/tokeniq-logs.csv
```

## 7. View service logs and follow live output

Run on the server. `sudo` may be needed for journal access. These are process/server logs; strategy candle events are saved in SQLite and exposed by section 6, and may not appear in the journal.

Recent backend logs:

```bash
sudo journalctl -u tokeniq-api.service -n 100 --no-pager
```

Follow new backend output continuously; press **Ctrl+C** to stop viewing (the service keeps running):

```bash
sudo journalctl -u tokeniq-api.service -n 50 -f
```

Last 30 minutes, with timestamps displayed in IST:

```bash
sudo env TZ=Asia/Kolkata journalctl -u tokeniq-api.service \
  --since '30 minutes ago' --no-pager -o short-iso
```

Frontend server logs:

```bash
sudo journalctl -u tokeniq-web.service -n 100 --no-pager
```

## 8. Read older saved strategy logs directly from SQLite

This reads the existing database without importing the application or restarting its engine. Unlike the API's latest-300 view, it can search older retained events. Run from `/var/www/tokeniq`. The example uses the deployment's default database path; adjust it if `TOKENIQ_DB` is configured differently.

Edit `symbol`, `start` and `end` for the incident. Times below are **IST**, and the end is exclusive:

```bash
.venv/bin/python - <<'PY'
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

symbol = "ETHUSD"
start = "2026-10-25 16:15:00"
end = "2026-10-25 16:21:00"
ist = ZoneInfo("Asia/Kolkata")

def utc(value):
    return datetime.fromisoformat(value).replace(tzinfo=ist).astimezone(timezone.utc).isoformat()

with sqlite3.connect("file:/var/www/tokeniq/tokeniq.db?mode=ro", uri=True) as db:
    rows = db.execute(
        "SELECT time,level,message FROM logs "
        "WHERE time >= ? AND time < ? AND message LIKE ? ORDER BY id",
        (utc(start), utc(end), "%" + symbol + "%")
    )
    for timestamp, level, message in rows:
        timestamp = datetime.fromisoformat(timestamp).astimezone(ist)
        print(timestamp.strftime("%d %b %Y %H:%M:%S IST"), level, message)
PY
```

## 9. Check service and API health

```bash
sudo systemctl status tokeniq-api.service tokeniq-web.service --no-pager -l
curl --fail-with-body -sS http://127.0.0.1:8100/health | .venv/bin/python -m json.tool
```

Run the health command on the server: the API health route uses the local backend port, not the public frontend URL. Health checks do not require login and do not prove that exchange credentials or trading permissions are valid. Check **Brokers → Verify** and instance status separately.

## 10. Remove the local sign-in cookie

Run this after finishing the API commands. The temporary cookie file contains a signed session token.

```bash
rm -f "$COOKIE_FILE"
unset COOKIE_FILE
```

Do not publish `.env`, API credentials, the `.broker-key` encryption key, or the temporary session cookie.
