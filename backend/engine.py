"""Persistent instances with serialized, explicitly requested execution."""
import asyncio, hashlib, json, time, uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from backend import delta_client as exchange
from backend.strategy import StrategyConfig, TIMEFRAMES, supertrend, protection


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class InstanceConflict(ValueError):
    """The requested operation conflicts with instance execution or ownership."""


class Engine:
    def __init__(self, db, log):
        self.db, self.log = db, log
        self.lock = asyncio.Lock()
        self.buffers = {}
        with db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('CREATE TABLE IF NOT EXISTS strategies (symbol TEXT PRIMARY KEY, config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT "{}")')
            c.execute('''CREATE TABLE IF NOT EXISTS strategy_instances (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, symbol TEXT NOT NULL,
                config TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                state TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            c.execute('CREATE TABLE IF NOT EXISTS strategy_instance_deletions (instance_id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS instance_symbol ON strategy_instances(symbol)')
            c.execute('CREATE TABLE IF NOT EXISTS engine_migrations (name TEXT PRIMARY KEY)')
            c.execute('''CREATE TABLE IF NOT EXISTS strategy_candle_logs (
                instance_id TEXT NOT NULL, config_key TEXT NOT NULL, candle_time INTEGER NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(instance_id,config_key,candle_time))''')
            if not c.execute('SELECT 1 FROM engine_migrations WHERE name=?', ('instances_v1',)).fetchone():
                for row in c.execute('SELECT * FROM strategies').fetchall():
                    instance_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'tokeniq:legacy:'+row['symbol']))
                    c.execute('INSERT OR IGNORE INTO strategy_instances (id,name,symbol,config,enabled,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                              (instance_id, 'Supertrend · '+row['symbol'], row['symbol'], row['config'], 0, row['state'], utc_now(), utc_now()))
                c.execute('INSERT INTO engine_migrations VALUES (?)', ('instances_v1',))
            # Keep ownership and outstanding Exit requests, but never auto-resume entries.
            for row in c.execute('SELECT id,state FROM strategy_instances WHERE enabled=1').fetchall():
                state = json.loads(row['state'])
                state.update(status='Stopped after server restart; existing protection remains active', last_bar=None)
                c.execute('UPDATE strategy_instances SET enabled=0,state=?,updated_at=? WHERE id=?', (json.dumps(state), utc_now(), row['id']))

    def records(self):
        with self.db() as c:
            records = []
            for row in c.execute('SELECT * FROM strategy_instances WHERE id NOT IN (SELECT instance_id FROM strategy_instance_deletions) ORDER BY created_at,id'):
                record = dict(row)
                record.update(config=json.loads(row['config']), state=json.loads(row['state']), enabled=bool(row['enabled']))
                state = record['state']
                record['lifecycle'] = ('exiting' if state.get('exit_requested') else
                                       'running' if record['enabled'] else
                                       'attention' if state.get('pending') or state.get('error') else
                                       'exited' if state.get('exited_at') else 'stopped')
                records.append(record)
            return records

    def record(self, identifier):
        records = self.records()
        exact = next((row for row in records if row['id'] == identifier), None)
        if exact:
            return exact
        # Compatibility for old clients; a symbol must never select an arbitrary instance.
        matches = [row for row in records if row['symbol'] == identifier]
        if len(matches) > 1:
            raise ValueError('Multiple instances use this instrument; select an instance by ID')
        return matches[0] if matches else None

    def persist(self, record):
        record['updated_at'] = utc_now()
        with self.db() as c:
            c.execute('UPDATE strategy_instances SET enabled=?,state=?,updated_at=? WHERE id=?',
                      (int(record['enabled']), json.dumps(record['state']), record['updated_at'], record['id']))

    def editable(self, record):
        return not (record['enabled'] or any(record['state'].get(key) for key in ('owned', 'pending', 'exit_requested', 'protection_ids', 'protection_pending')))

    async def validate_product(self, config):
        product = await exchange.product(config.symbol)
        if product.get('trading_status') != 'operational':
            raise ValueError('Instrument is not operational')
        return product

    async def create_instance(self, config, name='', request_id=None):
        async with self.lock:
            return await self._create_instance(config, name, request_id)

    async def _create_instance(self, config, name='', request_id=None):
        name = name.strip() or f'Supertrend · {config.symbol} · {config.resolution}'
        if len(name) > 80:
            raise ValueError('Instance name must be at most 80 characters')
        instance_id = str(uuid.UUID(str(request_id))) if request_id else str(uuid.uuid4())
        with self.db() as connection:
            archived = connection.execute('SELECT deleted_at FROM strategy_instance_deletions WHERE instance_id=?', (instance_id,)).fetchone()
        if archived and archived['deleted_at']:
            raise ValueError('This instance was deleted; create a new instance with a new request ID')
        previous = next((r for r in self.records() if r['id'] == instance_id), None)
        if previous:
            if previous['name'] != name or previous['config'] != config.model_dump():
                raise ValueError('This create request was already used for different settings')
            return previous
        await self.validate_product(config)
        state = {'status': 'Instance saved. Stopped until you press Start.'}
        now = utc_now()
        with self.db() as c:
            c.execute('INSERT INTO strategy_instances (id,name,symbol,config,enabled,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                      (instance_id, name, config.symbol, config.model_dump_json(), 0, json.dumps(state), now, now))
        self.log(f'{name} [{instance_id}]: instance created; stopped')
        return self.record(instance_id)

    async def update_instance(self, instance_id, config, name=''):
        async with self.lock:
            record = self.record(instance_id)
            if not record:
                raise ValueError('Instance not found')
            return await self._update_instance(record, config, name)

    async def _update_instance(self, record, config, name=''):
        if not self.editable(record):
            raise ValueError('Pause and close the instance position before changing its settings')
        await self.validate_product(config)
        name = name.strip() or record['name']
        if len(name) > 80:
            raise ValueError('Instance name must be at most 80 characters')
        # Editing a stopped instance must not reset its current account-loss baseline.
        state = {key: record['state'][key] for key in ('day','day_balance','daily_pnl') if key in record['state']}
        state['status'] = 'Settings saved. Instance is stopped.'
        with self.db() as c:
            c.execute('UPDATE strategy_instances SET name=?,symbol=?,config=?,enabled=0,state=?,updated_at=? WHERE id=?',
                      (name, config.symbol, config.model_dump_json(), json.dumps(state), utc_now(), record['id']))
        self.buffers.pop(record['id'], None)
        self.log(f'{name} [{record["id"]}]: settings saved; stopped')
        return self.record(record['id'])

    async def delete_instance(self, instance_id):
        async with self.lock:
            record = self.record(instance_id)
            if not record:
                raise ValueError('Instance not found')
            if record['enabled']:
                raise InstanceConflict('Cannot delete a running instance. Stop it first, then Exit any open position or pending orders.')
            if any(record['state'].get(key) for key in ('owned', 'pending', 'exit_requested', 'protection_ids', 'protection_pending', 'needs_protection')):
                raise InstanceConflict('Cannot delete this instance while a position, tracked orders or exit reconciliation remains. Use Exit and wait for completion.')
            # Retain the request ID as a tombstone so delayed create retries cannot resurrect it.
            with self.db() as connection:
                connection.execute('INSERT INTO strategy_instance_deletions(instance_id,deleted_at) VALUES (?,?)', (record['id'], utc_now()))
            self.buffers.pop(record['id'], None)
            self.log(f'{record["name"]} [{record["id"]}]: instance deleted')
            return record['id']

    async def save(self, config):
        """Legacy settings endpoint: never selects between multiple same-symbol instances."""
        async with self.lock:
            previous = self.record(config.symbol)
            if previous:
                return await self._update_instance(previous, config)
            return await self._create_instance(config)

    async def position(self, product):
        result = await exchange.request('/v2/positions', {'product_id': product['id']}, True)
        if not isinstance(result, dict) or 'size' not in result:
            raise ValueError('Unable to verify real-time position')
        return result

    async def active_orders(self, product):
        result = await exchange.request('/v2/orders', {'product_ids': str(product['id']), 'state': 'open,pending', 'page_size': 100}, True)
        if not isinstance(result, list):
            raise ValueError('Unable to verify active orders')
        return result

    def ensure_exclusive(self, record):
        for other in self.records():
            if other['id'] != record['id'] and other['symbol'] == record['symbol'] and (
                other['enabled'] or any(other['state'].get(key) for key in ('owned', 'pending', 'exit_requested', 'protection_ids', 'protection_pending'))
            ):
                raise ValueError('Another instance already controls this instrument; stop and exit that instance first')

    async def control(self, identifier, action):
        async with self.lock:
            record = self.record(identifier)
            if not record:
                raise ValueError('Save an instance first')
            state = record['state']
            legacy_close = action == 'close'
            action = {'pause': 'stop', 'close': 'exit'}.get(action, action)
            if action == 'stop':
                record['enabled'] = False
                if not state.get('exit_requested'):
                    state['status'] = 'Stopped; no new entries. Existing protection remains active.'
                self.persist(record)
            elif action == 'exit':
                # Old clients must not use Close to act on an unrelated account position.
                if legacy_close and not state.get('owned') and not state.get('pending'):
                    product = await exchange.product(record['symbol'])
                    if float((await self.position(product))['size']):
                        raise ValueError('This position was opened outside the strategy; it will not be modified')
                record['enabled'] = False
                state.update(exit_requested=True, status='Exit requested; reconciling orders and closing owned positions', error=None)
                self.persist(record)
                try:
                    await self.finish_exit(record)
                except Exception as exc:
                    self.record_error(record, exc)
            elif action == 'start':
                if state.get('exit_requested'):
                    raise ValueError('Exit is still pending; wait for confirmed closure before starting')
                if record['enabled']:
                    return self.record(record['id'])
                if state.get('pending'):
                    raise ValueError('An order has an uncertain outcome; reconcile it before restarting')
                self.ensure_exclusive(record)
                product = await self.validate_product(StrategyConfig(**record['config']))
                position = await self.position(product)
                size = int(position['size'])
                if size and not state.get('owned'):
                    raise ValueError('An existing account position is not owned by this instance; close it separately first')
                tracked = state.get('position')
                if state.get('owned') and tracked and size != int(tracked['size']):
                    raise ValueError('Position changed externally; reconcile it before restarting')
                if not state.get('owned') and await self.active_orders(product):
                    raise ValueError('Existing account orders on this instrument must be resolved first')
                wallet = await exchange.request('/v2/wallet/balances', private=True)
                usd = next((row for row in wallet if row['asset_symbol'] == 'USD'), None)
                if not usd or float(usd['available_balance']) <= 0:
                    raise ValueError('No available USD balance')
                record['enabled'] = True
                state.pop('exited_at', None)
                state.update(error=None, last_bar=None, status='Running; waiting for the next closed-candle flip', started_at=time.time())
                state.setdefault('day', datetime.now(timezone.utc).date().isoformat())
                self.buffers.pop(record['id'], None)
                self.persist(record)
            else:
                raise ValueError('Unknown instance action')
            self.log(f'{record["name"]} [{record["id"]}]: {action}')
            return self.record(record['id'])

    def finalize_order(self, record, order):
        state, pending = record['state'], record['state']['pending']
        if 'size' not in order or 'unfilled_size' not in order:
            raise ValueError('Cannot confirm order fills; reconciliation remains pending')
        filled = int(order['size']) - int(order['unfilled_size'])
        if filled < 0 or filled > int(pending['size']):
            raise ValueError('Unexpected order fill size; manual reconciliation required')
        if filled:
            signed = filled if pending['side'] == 'buy' else -filled
            if pending['reduce_only']:
                tracked = state.get('position')
                if tracked:
                    tracked['size'] = int(tracked['size']) + signed
            else:
                state['owned'] = True
                state['needs_protection'] = True
                state['position'] = {'size': signed, 'entry': order.get('average_fill_price')}
        state.update(pending=None, last_order_id=order.get('id'), last_fill_size=filled)
        self.persist(record)
        return filled

    async def reconcile_pending(self, record, product, cancel_entry=False):
        pending = record['state'].get('pending')
        if not pending:
            return
        order = await exchange.request('/v2/orders/client_order_id/'+pending['client_order_id'], private=True)
        if order.get('state') not in ('closed', 'cancelled'):
            if cancel_entry and not pending['reduce_only'] and order.get('id'):
                await exchange.request('/v2/orders', private=True, method='DELETE', body={'id': order['id'], 'product_id': product['id']})
                order = await exchange.request('/v2/orders/client_order_id/'+pending['client_order_id'], private=True)
            if order.get('state') not in ('closed', 'cancelled'):
                raise ValueError('Submitted order still pending; no retry will be sent')
        self.finalize_order(record, order)

    async def submit(self, record, product, side, size, reduce_only=False):
        state = record['state']
        if state.get('pending'):
            raise ValueError('Order outcome needs reconciliation')
        oid = 'tq'+record['id'].replace('-', '')[:8]+uuid.uuid4().hex[:20]
        state['pending'] = {'client_order_id': oid, 'side': side, 'size': int(size), 'reduce_only': reduce_only, 'time': time.time()}
        self.persist(record)
        try:
            result = await exchange.request('/v2/orders', private=True, method='POST', body={
                'product_id': product['id'], 'side': side, 'size': int(size), 'order_type': 'market_order',
                'reduce_only': reduce_only, 'client_order_id': oid})
        except exchange.ExchangeError as exc:
            if 400 <= exc.status < 500 and exc.status not in (408, 409, 429):
                state['pending'] = None
                self.persist(record)
            raise
        for _ in range(8):
            if result.get('state') in ('closed', 'cancelled'):
                break
            await asyncio.sleep(.4)
            result = await exchange.request('/v2/orders/client_order_id/'+oid, private=True)
        if result.get('state') not in ('closed', 'cancelled'):
            raise ValueError('Order is not final; entry paused for reconciliation')
        self.finalize_order(record, result)
        return result

    async def cancel_protection(self, record, product):
        ids = list(record['state'].get('protection_ids', []))
        if not ids:
            return
        active = {order['id'] for order in await self.active_orders(product)}
        for oid in ids:
            if oid in active:
                await exchange.request('/v2/orders', private=True, method='DELETE', body={'id': oid, 'product_id': product['id']})
        active = {order['id'] for order in await self.active_orders(product)}
        record['state']['protection_ids'] = [oid for oid in ids if oid in active]
        self.persist(record)
        if record['state']['protection_ids']:
            raise ValueError('Protection cancellation is not yet confirmed')

    async def close(self, record, product, signal_candle=None, signal_seconds=None):
        state = record['state']
        if not state.get('owned'):
            if float((await self.position(product))['size']):
                raise ValueError('This position was opened outside the strategy; it will not be modified')
            return
        position = await self.position(product)
        size = int(position['size'])
        if size:
            tracked = state.get('position')
            if tracked and int(tracked['size']) != size:
                raise ValueError('Position changed externally; reconcile it before closing through the instance')
            if signal_candle is not None and not self.signal_still_fresh(record,signal_candle,signal_seconds):
                return False
            await self.submit(record, product, 'sell' if size > 0 else 'buy', abs(size), True)
            if float((await self.position(product))['size']):
                raise ValueError('Close was not fully filled; reversal cancelled')
        await self.cancel_protection(record, product)
        state.update(owned=False, position=None)
        self.persist(record)
        return True

    async def finish_exit(self, record):
        state = record['state']
        record['enabled'] = False
        if any(state.get(key) for key in ('pending', 'owned', 'protection_ids', 'protection_pending')):
            product = await exchange.product(record['symbol'])
            await self.reconcile_pending(record, product, cancel_entry=True)
            if state.get('protection_pending'):
                await self.discover_protection(record, product)
            if state.get('owned'):
                await self.close(record, product)
            else:
                await self.cancel_protection(record, product)
            if state.get('protection_pending'):
                raise ValueError('Bracket creation is still uncertain; verify exchange orders before restarting')
        state['needs_protection'] = False
        state.update(exit_requested=False, exited_at=utc_now(), error=None, status='Exited; owned position is flat and tracked orders are cleared')
        self.persist(record)

    def signal_still_fresh(self, record, candle, seconds):
        """Recheck after awaited account/exchange work, before signal orders."""
        closed_at = candle['time'] + seconds
        age = time.time() - closed_at
        if closed_at <= float(record['state'].get('started_at') or 0):
            reason = 'Signal predates Start; waiting for a fresh flip'
        elif age < 0:
            reason = 'Candle is not closed; signal skipped'
        elif age > max(30,min(seconds,120)):
            reason = 'Fresh-flip window expired during checks; signal skipped'
        else:
            return True
        record['state']['status'] = reason
        self.persist(record)
        self.log(f'{record["name"]} [{record["id"]}] · {record["symbol"]} · {record["config"]["resolution"]}: {reason}')
        return False

    def log_candle(self, record, point, decision, now=None):
        """Persist one immutable audit row per instance/config/closed candle.

        This journal is independent of order submission and survives Start/Stop
        and process restarts. Warmup history is never emitted as live activity.
        """
        config = record['config']
        seconds = TIMEFRAMES[config['resolution']]
        now = time.time() if now is None else now
        if point['time'] + seconds > now:
            return False
        audit_config = {**config, 'execution_account_id':record['account_id']} if record.get('account_id') else config
        config_key = hashlib.sha256(json.dumps(audit_config,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        direction = 'UP (GREEN)' if point['direction']==1 else 'DOWN (RED)' if point['direction']==-1 else 'WARMUP'
        signal = 'BUY' if point['signal']==1 else 'SELL' if point['signal']==-1 else 'NONE'
        payload = {'account_id':record.get('account_id'),'client_name':record.get('client_name'),'instance_name':record['name'],'symbol':record['symbol'],'resolution':config['resolution'],
                   'environment':'testnet','config':config,'candle_close_time':point['time']+seconds,
                   **point,'direction_label':direction,'signal_label':signal,'decision':decision}
        with self.db() as connection:
            inserted = connection.execute('INSERT OR IGNORE INTO strategy_candle_logs VALUES (?,?,?,?,?)',
                (record['id'],config_key,point['time'],utc_now(),json.dumps(payload))).rowcount
        if not inserted:
            return False
        def number(value):
            return f'{value:.8f}'.rstrip('0').rstrip('.') if value is not None else '—'
        def stamp(value):
            return datetime.fromtimestamp(value,ZoneInfo('Asia/Kolkata')).strftime('%d %b %H:%M')
        self.log(f'{record["name"]} [{record["id"]}] · {record["symbol"]} · {config["resolution"]} | '
                 f'Closed candle {stamp(point["time"])} → {stamp(point["time"]+seconds)} IST | '
                 f'O {number(point["open"])} · H {number(point["high"])} · L {number(point["low"])} · C {number(point["close"])} | '
                 f'ATR {number(point["atr"])} · ST {number(point["supertrend"])} | '
                 f'Direction {direction} · Signal {signal} | {decision}', 'CANDLE')
        return True

    def record_error(self, record, exc):
        message = str(exc) if isinstance(exc, ValueError) else 'Exchange connection unavailable; reconciliation will continue'
        previous = record['state'].get('error')
        record['enabled'] = False
        record['state'].update(error=message, status=('Exit pending: ' if record['state'].get('exit_requested') else 'Stopped: ')+message)
        self.persist(record)
        if previous != message:
            self.log(f'{record["name"]} [{record["id"]}] · {record["symbol"]} · {record["config"]["resolution"]}: {message}', 'ERROR')

    async def discover_protection(self, record, product):
        pending = record['state']['protection_pending']
        orders = await self.active_orders(product)
        ids = [order['id'] for order in orders if order['id'] not in pending['before'] and order.get('bracket_order')]
        record['state']['protection_ids'] = list(set(record['state'].get('protection_ids', []) + ids))
        if len(ids) >= pending['expected']:
            record['state']['protection_pending'] = None
        self.persist(record)

    async def protect(self,r,p,config,pos):
        if r['state'].get('protection_pending'):
            raise ValueError('Bracket creation is uncertain; no duplicate protection request will be sent')
        size = int(pos['size'])
        tracked = r['state'].get('position')
        if tracked and int(tracked['size']) != size:
            raise ValueError('Position changed externally; manual reconciliation required')
        levels = protection(float(pos['entry_price']),1 if size>0 else -1,config,float(p['tick_size']))
        body = {'product_id':p['id'],'bracket_stop_trigger_method':'last_traded_price'}
        if levels['stop'] is not None:
            body['stop_loss_order'] = {'order_type':'market_order','stop_price':str(levels['stop'])}
        if levels['target'] is not None:
            body['take_profit_order'] = {'order_type':'market_order','stop_price':str(levels['target'])}
        if len(body)>2:
            before = {o['id'] for o in await self.active_orders(p)}
            expected = int(levels['stop'] is not None)+int(levels['target'] is not None)
            r['state']['protection_pending'] = {'before': list(before), 'expected': expected}
            self.persist(r)
            try:
                await exchange.request('/v2/orders/bracket',private=True,method='POST',body=body)
            except exchange.ExchangeError as exc:
                if 400 <= exc.status < 500 and exc.status not in (408, 409, 429):
                    r['state']['protection_pending'] = None
                    self.persist(r)
                raise
            orders = await self.active_orders(p)
            ids = [o['id'] for o in orders if o['id'] not in before and o.get('bracket_order')]
            expected = int(levels['stop'] is not None)+int(levels['target'] is not None)
            r['state']['protection_ids'] = ids
            self.persist(r)
            if len(ids)<expected:
                raise ValueError('Exchange protection could not be confirmed')
            r['state']['protection_pending'] = None
        r['state']['needs_protection'] = False
        r['state'].update(position={'size':size,'entry':pos['entry_price'],**levels})
        self.persist(r)

    async def step(self,r):
        config, state = StrategyConfig(**r['config']),r['state']
        p = await exchange.product(config.symbol)
        if state.get('exit_requested'):
            await self.finish_exit(r)
            return
        if state.get('pending'):
            pending = dict(state['pending'])
            await self.reconcile_pending(r, p)
            if not pending['reduce_only'] and state.get('owned'):
                pos = await self.position(p)
                tracked = state.get('position') or {}
                if int(pos['size']) != int(tracked.get('size', 0)):
                    raise ValueError('Position changed externally; manual reconciliation required')
                if state.get('last_fill_size') != config.quantity:
                    state['exit_requested'] = True
                    self.persist(r)
                    await self.finish_exit(r)
                    return
                try:
                    await self.protect(r, p, config, pos)
                except Exception:
                    state['exit_requested'] = True
                    self.persist(r)
                    await self.finish_exit(r)
                    raise
        if state.get('owned') and state.get('needs_protection'):
            try:
                pos = await self.position(p)
                await self.protect(r, p, config, pos)
            except Exception:
                state['exit_requested'] = True
                r['enabled'] = False
                self.persist(r)
                await self.finish_exit(r)
                raise
        if state.get('owned'):
            pos = await self.position(p)
            if not int(pos['size']):
                await self.cancel_protection(r,p)
                state.update(owned=False,position=None,status='SL/target exit confirmed; waiting for a new flip')
                self.log(f'{config.symbol}: position exit confirmed')
            elif int(pos['size'])!=int((state.get('position') or {}).get('size',pos['size'])) or abs(int(pos['size']))!=config.quantity:
                r['enabled'] = False
                state['status'] = 'Position changed externally; entries paused'
                self.persist(r)
                return
        if not r['enabled']:
            self.persist(r)
            return
        self.ensure_exclusive(r)
        wallet, positions = await asyncio.gather(exchange.request('/v2/wallet/balances',private=True),exchange.request('/v2/positions/margined',private=True))
        usd = next((w for w in wallet if w['asset_symbol']=='USD'),None)
        if usd is None:
            raise ValueError('USD wallet unavailable')
        # Account USD balance includes realized P&L/fees; add USD position UPL.
        unrealized = sum(float(x.get('unrealized_pnl') or 0) for x in positions
                         if (x.get('product',{}).get('settling_asset') or {}).get('symbol')=='USD')
        equity = float(usd['balance'])+unrealized
        day = datetime.now(timezone.utc).date().isoformat()
        if state.get('day')!=day:
            state.update(day=day,day_balance=equity)
        state.setdefault('day_balance',equity)
        state['daily_pnl'] = equity-state['day_balance']
        if -state['daily_pnl']>=config.daily_loss_limit:
            r['enabled'] = False
            state['exit_requested'] = True
            self.persist(r)
            await self.finish_exit(r)
            state['status'] = 'Daily account loss limit reached; instance exited'
            self.persist(r)
            return
        seconds = TIMEFRAMES[config.resolution]
        now = int(time.time())
        existing = self.buffers.get(r['id'])
        start = existing[-1]['time'] if existing else now-seconds*max(500,config.atr_period*5)
        fresh = await exchange.history(config.symbol,config.resolution,start,now)
        # Re-read time after the await: the candle may have closed in transit.
        # Provisional/future rows must never enter the ATR/Supertrend buffer.
        now = int(time.time())
        rows = {row['time']:row for row in (existing or [])+fresh if row['time']+seconds<=now}
        points = supertrend(list(rows.values()),config.atr_period,config.multiplier)
        self.buffers[r['id']] = sorted(rows.values(),key=lambda x:x['time'])
        if len(points)<config.atr_period+2:
            state.update(status='Waiting for sufficient closed candles to initialize ATR', checked_at=now)
            self.persist(r)
            return
        last = points[-1]
        if last['time']+seconds > now:
            state.update(status='Waiting for the candle to close; no signal processed', checked_at=now)
            self.persist(r)
            return
        state.update(direction=last['direction'],supertrend=last['supertrend'],atr=last['atr'],candle_time=last['time'],checked_at=now)
        previous = state.get('last_bar')
        new_points = [last] if previous is None else [point for point in points if point['time'] > previous]
        for point in new_points:
            if previous is None:
                decision = 'START BASELINE · wait for a fresh flip; no historical entry'
            elif last['time']-previous > seconds:
                decision = 'RESYNC · missed/delayed history recorded; no old trade replay'
            elif now-(point['time']+seconds) > max(30,min(seconds,120)):
                decision = 'LATE · entry skipped'
            elif point['signal']:
                decision = 'SIGNAL · evaluating exit mode and account/risk checks'
            else:
                decision = 'HOLD · no new flip' if state.get('owned') else 'WAIT · no new flip'
            self.log_candle(r,point,decision,now)
        if now-(last['time']+seconds)>seconds+30:
            raise ValueError('Market candles are delayed; entries paused')
        if state.get('last_bar') is None:
            state['last_bar'] = last['time']
            state['status'] = 'Armed; waiting for a new green/red flip'
            self.persist(r)
            return
        if last['time']<=state['last_bar']:
            self.persist(r)
            return
        gap = last['time']-state['last_bar']
        state['last_bar'] = last['time']
        self.persist(r)
        if gap>seconds:
            state['status'] = 'Missed candles; resynchronized without replaying old entries'
            self.persist(r)
            return
        if now-(last['time']+seconds)>max(30,min(seconds,120)):
            state['status'] = 'Late candle update skipped; waiting for the next fresh flip'
            self.persist(r)
            return
        direction = last['signal']
        if not direction:
            state['status'] = 'Waiting for a new green/red flip'
            self.persist(r)
            return
        if not self.signal_still_fresh(r,last,seconds):
            return
        if state.get('owned'):
            held_direction = 1 if int(state['position']['size']) > 0 else -1
            if config.exit_mode == 'sl_tp' or held_direction == direction:
                state['status'] = 'Holding position until SL/target exit' if config.exit_mode == 'sl_tp' else 'Holding position until an opposite flip'
                self.log(f'{r["name"]} [{r["id"]}] · {config.symbol} · {config.resolution}: '
                         f'Signal {"BUY" if direction==1 else "SELL"} ignored; {state["status"]}')
                self.persist(r)
                return
            if not await self.close(r,p,signal_candle=last,signal_seconds=seconds):
                return
        if int((await self.position(p))['size']):
            raise ValueError('Existing external position prevents entry')
        if await self.active_orders(p):
            raise ValueError('Existing orders prevent entry')
        ticker = await exchange.request('/v2/tickers/'+config.symbol)
        timestamp = float(ticker.get('timestamp') or 0)/1000000
        if time.time()-timestamp>120:
            raise ValueError('Ticker is delayed; entry was not sent')
        price = float(ticker['close'])
        notional = config.quantity*float(p['contract_value'])*price
        # Fixed quantity, no leverage increase by the bot. Exchange margin may vary.
        if notional>config.max_notional:
            raise ValueError('Configured quantity exceeds maximum notional')
        leverage_info = await exchange.request(f'/v2/products/{p["id"]}/orders/leverage',private=True)
        leverage = float(leverage_info.get('leverage') or 1)
        if leverage<=0:
            raise ValueError('Unable to verify configured exchange leverage')
        state['leverage'] = leverage
        if notional/leverage+notional*.002>float(usd['available_balance']):
            raise ValueError('Insufficient available margin for configured quantity')
        protection(price,direction,config,float(p['tick_size']))
        # A fresh signal can expire while leverage, margin or position requests
        # are in flight. Never submit a late entry/reversal after those awaits.
        if not self.signal_still_fresh(r,last,seconds):
            return
        result = await self.submit(r,p,'buy' if direction==1 else 'sell',config.quantity)
        filled = int(result.get('size',0))-int(result.get('unfilled_size',0))
        if filled<=0:
            raise ValueError('Entry did not fill')
        state['owned'] = True
        self.persist(r)
        try:
            pos = await self.position(p)
            await self.protect(r,p,config,pos)
            if filled!=config.quantity:
                raise ValueError('Partial entry; closing filled quantity')
        except Exception:
            state['exit_requested'] = True
            r['enabled'] = False
            self.persist(r)
            await self.finish_exit(r)
            raise
        state['status'] = ('Green flip → BUY' if direction==1 else 'Red flip → SELL')+ ('; waiting for opposite flip' if config.exit_mode == 'opposite_signal' else '; exchange SL/target active')
        self.log(f'{r["name"]} [{r["id"]}] · {config.symbol} · {config.resolution}: {state["status"]} | '
                 f'Filled {filled} contracts · Average fill {result.get("average_fill_price") or "—"}')
        self.persist(r)

    async def run(self):
        while True:
            for record in self.records():
                if not record['enabled'] and not any(record['state'].get(key) for key in ('owned', 'pending', 'exit_requested', 'protection_pending')):
                    continue
                async with self.lock:
                    record = self.record(record['id'])
                    if record is None:
                        continue
                    try:
                        await self.step(record)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self.record_error(record, exc)
            await asyncio.sleep(3)
