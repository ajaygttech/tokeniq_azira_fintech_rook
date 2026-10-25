# TokenIQ · Rook Capital

Next.js 15 / React 19 workspace with a Python FastAPI Supertrend execution engine and SQLite archive.

## Setup

```bash
npm ci
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
python3 scripts/init_env.py
npm run build
.venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8100 --workers 1
# Separate terminal
npm start
```

The initializer asks for an administrator password and exchange credentials without displaying them. Existing `.env` files are never overwritten. `.env`, databases, logs, generated files and credentials must never be committed. `.env.example` documents the required variables. For local HTTP set `COOKIE_SECURE=false`; use `true` with HTTPS. Backtest ranges default to explicit IST date/time inputs, with a UTC option; displayed event times are IST.

## Exchange configuration

This build is restricted to Delta Exchange Testnet. Create trading-enabled API credentials in the corresponding account and whitelist the server's outbound IPv4 when required. Set `DELTA_ENV=testnet`, `DELTA_API_KEY`, and `DELTA_API_SECRET` in `.env`, then restart the API. Production endpoints are not available to the transport. Credentials never enter browser responses, logs or WebSocket subscriptions.

Documentation: https://docs.delta.exchange/
REST: `https://cdn-ind.testnet.deltaex.org`
Public WebSocket: `wss://socket-ind-pub.testnet.deltaex.org`

Instruments come from the exchange products API: currently 17 active linear perpetuals. These include 8 crypto instruments, XAUT/PAXG gold tokens, silver, equity/index tokens and USOON (an oil ETF token, not direct crude futures). Availability may change. Unsupported instruments are never substituted silently.

All documented candle resolutions are supported: `1m`, `3m`, `5m`, `15m`, `30m`, `1h`, `2h`, `4h`, `6h`, `1d`, `1w`.

## Market navigation

The navbar marquee shows every available linear perpetual returned by the exchange, with full symbol labels, prices and 24-hour changes. BTCUSD, ETHUSD, XAUTUSD (gold) and SOLUSD remain pinned in the market selection boxes above it. One public WebSocket subscribes to trades and rolling tickers for the displayed markets; trades update prices immediately and ticker messages refresh 24-hour changes. REST quotes refresh every 15 seconds as a fallback. Click an instrument to open its chart; hover, keyboard focus or the pause button stops scrolling. Reduced-motion preferences disable the animation.

Both Overview and Market depth include an instrument selector inside the chart card. It stays synchronized with the page-level selector and market strip; switching instruments changes candles, public WebSocket subscription, price and depth. Price precision follows the exchange tick size so small-price instruments remain readable.

## Strategy and settings

Open **Strategy settings** or **Strategies**. Choose the instrument, ATR settings, timeframe, quantity, exit mode and account risk limits, then **Create instance**. Saving creates a persistent, stopped instance and never starts trading. Name each instance to distinguish its instrument and configuration. **Edit settings** updates an existing stopped, flat instance; **Duplicate settings** prepares a new draft without changing the original.

The **Overview** home page shows saved instances above the market dashboard. Creating an instance returns you to Overview, highlights the new instance and leaves it stopped. The same controls are also available under **Strategies**.

The **Instance manager** lists every saved instance with its own stable ID, status, settings and **Start / Stop / Exit** controls. Different instruments can run together. Several configurations may be saved for the same instrument, but only one may control that instrument while running, holding a position, exiting or awaiting order reconciliation. The exchange account has a net position per instrument; instances must not share ownership of that position. A creation request ID prevents a retried save from creating duplicate instances.

- **Start** arms only the selected saved instance. Repeated Start requests do not reset its candle baseline. It waits for a fresh closed-candle flip; no historical signal is replayed.
- **Stop** disables new entries without requiring an exchange request. Existing SL/target protection and pending-order reconciliation remain active. Stop does not liquidate positions.
- **Exit** first persists a stopped, exiting state, reconciles/cancels a pending entry, then closes only this instance's owned position with reduce-only orders. It cancels only its tracked protective orders. Other instances and external account positions are not closed. Partial fills leave it exiting; subsequent reconciliation uses only the remaining quantity. An uncertain order is looked up by its original client order ID before any further submission.
- **Delete** removes a stopped, flat instance from active views after confirmation. Running instances and instances with owned positions, unresolved orders, pending protection or an unfinished Exit cannot be deleted. The server checks again under the execution lock and returns HTTP 409 with a clear error if deletion is unsafe. Deletion never submits or closes exchange orders. A retained deletion record prevents delayed create retries or server restarts from restoring the deleted instance.
- **Exited** is shown only after the position is confirmed flat and tracked orders are cleared. Unavailable or ambiguous exchange outcomes remain **Exiting** with an error and are revisited by the worker; an unresolvable exchange order or bracket-creation outcome requires account reconciliation rather than assuming closure.

Existing symbol-based settings migrate once into stable instances, preserving configuration, ownership, protective order IDs and outstanding order intents. Server restarts stop new entries. Persisted Exit requests continue reconciliation, and pre-existing exchange protection is retained. No instance is automatically started after restart.

ATR uses Wilder RMA, seeded by the mean of the first N true ranges. Supertrend uses the midpoint of each candle, ATR bands and previous-close final-band rules. The initial band seed is not a trade signal. A subsequent green flip closes a strategy short, confirms flat, then buys; a red flip closes a strategy long, confirms flat, then sells. The current forming candle is never used for a decision. Starting baselines the latest closed candle and waits for a fresh flip, rather than replaying old signals. There is no automatic same-trend re-entry after an SL/target exit.

Points mean quote-price units, not ticks, percent or USD P&L. For example, an entry at 100 with a 5-point SL and a 10-point target gives long levels 95/110 and short levels 105/90. Exchange tick rounding applies. Choose **Opposite signal** to hold until reversal, **SL / TP in points only** to ignore flips while holding, or **Opposite signal + SL / TP** to use both. In a mode using points, set either field to 0 to disable that leg; SL/TP-only mode requires at least one positive distance. Choose distances appropriate to each instrument's price; values leaving nonpositive prices are rejected. Quantity is in exchange contracts; the UI displays each contract's underlying amount.

Settings for an instance that is running, holding a position or reconciling orders are locked: stop and exit before editing. Independent new drafts can always be prepared. Account positions opened outside the engine are displayed but never adopted or closed by it.

## Execution and risk

- A single serialized worker prevents concurrent entry/reversal operations. Run exactly one API worker/replica.
- Entry intents and unique client order IDs are persisted before submission. Timed-out POSTs are never blindly retried; uncertain outcomes pause entries and are reconciled using the exchange order lookup.
- Market orders use actual exchange fills. Partial entries are closed; reversal requires confirmation that the previous position is flat.
- SL and TP are exchange bracket market orders based on actual position entry. If protection cannot be confirmed, the engine attempts an immediate reduce-only close and pauses. Temporary exposure exists between entry fill and bracket acceptance; a transport outage can delay emergency closure.
- Maximum notional and available-margin checks run before entries. The engine does not increase the account's configured leverage.
- Daily loss compares account USD equity (USD wallet balance plus USD unrealized P&L) against the strategy's baseline for the current UTC day. It includes other account activity; deposits/withdrawals affect this comparison. This is not an exchange-guaranteed loss cap. At the limit, strategy positions are closed and entries paused.
- Delayed candle/ticker data blocks entries; missed candles are resynchronized without replaying old trades.
- Restarts pause all new entries while preserving ownership, outstanding intents and existing exchange protection. Inspect status and press Start to resume. Uncertain orders that the exchange cannot resolve require manual account reconciliation before reuse.

## Backtesting

Choose **Backtesting**, select settings, dates, initial capital, fees and slippage, then **Create & run backtest**. **Use saved instance settings** copies the selected instance’s strategy parameters without changing its execution state. The form runs directly without a preset. Exit mode selects opposite-signal reversal, point SL/TP only, or both; the same mode applies to forward trading and historical tests. Both paths call `backend/strategy.py::supertrend` and share the same configuration model and protection calculations.

Historical research and forward execution use **Delta Testnet only**, including candles, instruments, contract metadata and signed orders. Backtests never request production market data and have no market selector or fallback. Compare against the Delta demo Traded Price chart; production charts have different candles. Each new report and CSV identifies the Testnet source; the archive labels earlier reports as Testnet legacy runs.

Ranges use explicit date/time and timezone, defaulting to IST. Offset-aware API timestamps retain their offsets. Only bars opening at or after Start and closing by End are simulated; a candle opening exactly at End is excluded. Reports show actual coverage, with up to 5,000 requested bars plus ATR warmup. Missing or truncated history is rejected. A candle comparison panel exposes recent OHLC, ATR, Supertrend and flips. The trade journal separates signal candle opening time from entry fill time. Intrabar SL/TP exits identify the candle rather than claiming an exact fill timestamp. A closed-candle flip fills at the following candle's open; both long and short positions use exchange contract multipliers. SL/target are checked against candle high/low. If both levels are touched in one bar, the simulator chooses the stop; adverse gaps fill at the open. Fees and configurable slippage apply to both sides. Final positions are closed at the last bar's close and timestamped at its closing time. Existing archived results are retained unchanged; corrected reports use engine version 2.

The simulator assumes 1x collateral and excludes funding, liquidation, orderbook liquidity and exchange latency. Forward trades use actual margin, fills and account equity. Thus signal logic is shared, but historical fills/P&L cannot be identical to forward execution. Equity, drawdown, fees, win rate, profit factor and trade exit reasons are archived. CSV trades and full JSON reports are available from Reports. Earlier SMA archives/presets remain stored locally and are excluded from the Supertrend views.

## Code map

- `backend/strategy.py`: validated configuration, candle normalization, Wilder ATR, Supertrend, SL/TP price calculation.
- `backend/delta_client.py`: signing, HTTP transport, products and paginated candles.
- `backend/engine.py`: instance migration/persistence, serialized lifecycle controls, orders, ownership, protection, risk controls and reconciliation.
- `backend/backtest.py`: historical simulator.
- `backend/main.py`: authenticated API, snapshots, configuration, reports and lifecycle.
- `app/StrategyWorkspace.tsx`: independent configuration drafts, instance manager and historical backtesting.
- `/api/strategy/instances`: authenticated list/create; `PUT /api/strategy/instances/{id}` edits stopped settings; `POST /api/strategy/instances/{id}/control` accepts `start`, `stop` or `exit`. An unresolved Exit returns HTTP 202. `DELETE /api/strategy/instances/{id}` deletes only a stopped, settled instance; active execution or exposure returns HTTP 409. Legacy symbol-based controls reject ambiguous same-symbol selections.

## Validation

```bash
npm run typecheck
npm run build
node --test tests/test_display.cjs
.venv/bin/python -m unittest discover -s tests -v
```

Backend tests use a temporary database and mocked exchange requests; they never trade or load account data into test outputs. Tests cover no-lookahead signals, ATR seeding, both flip directions, next-open fills, contract sizing, costs, SL precedence, validation, authentication/CSRF, original-position isolation, duplicate-intent prevention and restart behavior.

## Server deployment

Existing service templates are in `deploy/`. Review paths, ports and domain before installing them. The API binds `127.0.0.1:8100`; Next.js binds `127.0.0.1:3100`. Nginx serves HTTPS and proxies `/api` directly to FastAPI. Build the frontend before restarting both services:

```bash
sudo systemctl restart tokeniq-api tokeniq-web
```

Use HTTPS, restrict direct service ports, back up the SQLite database, and keep secrets out of backups intended for public sharing. Rotating `SESSION_SECRET` invalidates login cookies.

## Hackathon originality

The supplied rules allow 20–30% AI-generated code and require the team to explain its implementation. AI-assisted files must be represented honestly; this setup does not establish that the complete repository meets the organisers' originality limit. The team must check its own contributions and submission eligibility.

Net positions calculate linear unrealized P&L from public WebSocket mark prices using signed contracts × contract value × (mark − entry). All open instruments are subscribed independently of the selected chart. Prices older than 15 seconds fall back to the exchange snapshot. Order book shows active orders and labels pending protective orders as awaiting their trigger; Trade book shows actual fills.


### Per-instance candle audit

Strategy logs records one `CANDLE` event per completed candle for each running instance, using that instance's own timeframe. Each entry identifies the name/ID, symbol, candle open/close times in IST, OHLC, Wilder ATR, Supertrend, direction, flip signal and processing decision. A 5m instance therefore emits a new candle record after each completed five-minute bar, rather than every engine poll. Initial Start records the latest closed candle as a baseline and waits for a fresh flip. Warmup candles are excluded. Available missed candles are recorded as RESYNC without replaying historical trades; absent exchange bars are never invented.

A durable `strategy_candle_logs` journal deduplicates instance/config/candle keys across polls, Start/Stop and restarts. Its payload retains the exact numeric indicator values and configuration; formatted events appear in Strategy logs and the existing log export. Trade fills, ignored signals and errors identify the same instance/timeframe. Existing exit modes and account/risk guards determine whether a signal becomes an order. The log page shows the latest 300 events; persisted audit/history remains in SQLite.


### Fresh-flip entry policy

Start/resume baselines the latest closed candle and never enters from an already-existing flip. Entries require a new confirmed flip after Start; duplicate bars and missed/late history are skipped. A candle that is still forming cannot advance the processing watermark. Freshness is rechecked after awaited account/margin checks and immediately before submitting a signal-driven position close or new entry. If a reversal close finishes after the freshness window, its new entry leg is skipped. Expiry decisions appear in Strategy logs. Explicit Exit and protective/risk closures remain available independently of signal age.


### Delta Testnet broker accounts

Brokers connects named client accounts using Testnet API keys. Credentials are encrypted with Fernet; the separate `.broker-key` file is created with mode 0600 beside the database (override with `BROKER_KEY_FILE`). Back up that key securely with the database; losing it prevents account decryption. Credentials are never returned by the API. Existing environment credentials are imported once as the initial primary account. `GET /v2/users/trading_preferences` verifies the exchange user ID, preventing two keys for the same account from executing twice. Read access is verified without placing an order; trading permissions must be enabled on the API key.

Instances choose primary, all, or explicit selected accounts. Start resolves and freezes those accounts; primary changes and newly added accounts never redirect an already running instance. Quantity, daily loss checks, ownership, pending-order reconciliation and exchange protection remain independent per account. Start preflights every target before enabling execution; a preflight failure leaves all targets paused. A failure after orders are submitted is isolated and reported per client; execution across accounts is not an atomic exchange transaction. Stop and Exit operate on the saved execution accounts, even after the primary changes. Changing targets with existing ownership requires Exit first.

Orders, fills, positions, wallet details and CSV exports carry the client name and account ID. Instance MTM sums independently verified client positions using the public Testnet mark-price WebSocket. API restart pauses entries and preserves each account's position/protection state.
