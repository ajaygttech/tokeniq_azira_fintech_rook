import asyncio, csv, hashlib, hmac, io, json, math, os, secrets, sqlite3, time
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from uuid import UUID
from contextlib import asynccontextmanager
from backend.strategy import StrategyConfig, TIMEFRAMES, supertrend
from backend.backtest import Backtest, run_backtest, validate_history
from backend import delta_client as exchange
from backend.read_cache import ReadCache
from backend.trade_history import since_reset
from backend.brokers import BrokerStore,AccountInput,ExecutionSelection

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')
@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(engine.run())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title='TokenIQ API',docs_url=None,redoc_url=None,lifespan=lifespan)
@app.exception_handler(RequestValidationError)
async def validation_error(request:Request,exc:RequestValidationError):
    # Validation errors must never echo submitted API credentials.
    return JSONResponse({'detail':[{k:v for k,v in error.items() if k in ('type','loc','msg')} for error in exc.errors()]},status_code=422)

DB = Path(os.environ.get('TOKENIQ_DB',str(ROOT/'tokeniq.db')))
SESSION_SECRET = os.environ.get('SESSION_SECRET', '')
PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', '')
ENV = 'testnet'
BASE = exchange.BASE
KEY = os.environ.get('DELTA_API_KEY', '')
SECRET = os.environ.get('DELTA_API_SECRET', '')
COOKIE_SECURE = os.environ.get('COOKIE_SECURE', 'true').lower() == 'true'
LOGIN_ATTEMPTS = {}
CACHE = {}
LOCK = asyncio.Lock()
DASHBOARD_READS = ReadCache(3)

def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    return connection

with db() as c:
    c.execute('CREATE TABLE IF NOT EXISTS workspace_resets (id INTEGER PRIMARY KEY CHECK(id=1), fills_since REAL NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY, time TEXT, level TEXT, message TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS presets (id INTEGER PRIMARY KEY, name TEXT UNIQUE, config TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, time TEXT, config TEXT, result TEXT)')

def log(message, level='INFO'):
    with db() as c:
        c.execute('INSERT INTO logs(time,level,message) VALUES(?,?,?)', (datetime.now(timezone.utc).isoformat(), level, message))

def sign(secret, message):
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()

def authenticated(request: Request):
    token = request.cookies.get('tokeniq_session', '')
    try:
        payload, signature = token.rsplit('.', 1)
        if not SESSION_SECRET or not hmac.compare_digest(sign(SESSION_SECRET, payload), signature):
            raise ValueError()
        username, expiry, nonce = payload.split(':')
        if username != 'admin' or int(expiry) < time.time():
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(401, 'Please sign in')
    if request.method not in ('GET', 'HEAD'):
        origin = request.headers.get('origin')
        allowed = os.environ.get('PUBLIC_ORIGIN', 'https://tokeniq.rookcapital.in')
        if origin and origin != allowed and origin not in ('http://localhost:3100','http://127.0.0.1:3100'):
            raise HTTPException(403, 'Origin not allowed')
    return 'admin'

class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=200)

@app.post('/api/login')
async def login(body: Login, request: Request):
    ip = request.headers.get('x-real-ip') or request.client.host
    now = time.time()
    attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now-t < 900]
    LOGIN_ATTEMPTS[ip] = attempts
    if len(attempts) >= 8:
        raise HTTPException(429, 'Too many attempts. Try again in 15 minutes.')
    try:
        salt, expected = PASSWORD_HASH.split(':')
        actual = hashlib.scrypt(body.password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        valid = body.username == 'admin' and hmac.compare_digest(actual, expected)
    except ValueError:
        valid = False
    if not valid or not SESSION_SECRET:
        attempts.append(now)
        raise HTTPException(401, 'Incorrect username or password')
    LOGIN_ATTEMPTS.pop(ip, None)
    payload = f'admin:{int(now)+43200}:{secrets.token_hex(12)}'
    response = JSONResponse({'user': 'admin'})
    response.set_cookie('tokeniq_session', payload+'.'+sign(SESSION_SECRET,payload), httponly=True, secure=COOKIE_SECURE, samesite='strict', max_age=43200, path='/')
    log('Administrator signed in')
    return response

@app.get('/api/session')
async def session(user=Depends(authenticated)):
    return {'user': user, 'environment': ENV, 'configured': bool(brokers.accounts())}

@app.post('/api/logout')
async def logout(user=Depends(authenticated)):
    response = JSONResponse({'ok': True})
    response.delete_cookie('tokeniq_session', path='/')
    return response

async def delta(path,params=None,private=False):
    return await exchange.request(path,params,private)

@app.get('/api/snapshot')
async def snapshot(symbol: str='BTCUSD',user=Depends(authenticated)):
    try:
        catalog = {p['id']:p for p in await asyncio.wait_for(DASHBOARD_READS.get(('catalog',),exchange.products),8)}
        if not any(p['symbol']==symbol for p in catalog.values()):raise ValueError('Instrument is not an available linear perpetual')
    except ValueError as e:raise HTTPException(400,str(e))
    except Exception:raise HTTPException(503,'Instruments are temporarily unavailable; reconnecting')
    accounts=brokers.accounts()
    async def read(args):
        try:return await asyncio.wait_for(delta(*args),8)
        except Exception as exc:return exc
    async def market_read():
        return await asyncio.gather(read(('/v2/tickers/'+symbol,None,False)),read(('/v2/l2orderbook/'+symbol,{'depth':10},False)))
    async def account_read():
        stamp=datetime.now(timezone.utc).isoformat()
        jobs={'positions':('/v2/positions/margined',None,True),'orders':('/v2/orders',{'state':'open,pending','page_size':100},True),'fills':('/v2/fills',{'page_size':100},True),'balances':('/v2/wallet/balances',None,True)}
        async def client_read(account):
            with brokers.context(account['id']):values=await asyncio.gather(*(read(args) for args in jobs.values()))
            return account,values
        results={key:[] for key in jobs};errors={}
        for account,values in await asyncio.gather(*(client_read(account) for account in accounts)):
            for kind,value in zip(jobs,values):
                if isinstance(value,Exception) or not isinstance(value,list):
                    detail=str(value) if isinstance(value,ValueError) else 'Exchange data delayed; retrying'
                    errors[kind]=(errors.get(kind,'')+' '+account['client_name']+': '+detail).strip()
                else:results[kind].extend({**row,'account_id':account['id'],'client_name':account['client_name'],'broker':'Delta Testnet'} for row in value)
        if not accounts:errors={key:'Connect a Delta Testnet account in Brokers' for key in jobs}
        return {'timestamp':stamp,'errors':errors,**results}
    # Account data is shared across symbols/tabs; public markets load independently.
    account_key=('accounts',tuple((a['id'],a['client_name']) for a in accounts))
    public,private=await asyncio.gather(DASHBOARD_READS.get(('market',symbol),market_read),DASHBOARD_READS.get(account_key,account_read))
    data={**private,'environment':ENV,'accounts':accounts,'symbol':symbol,'errors':dict(private['errors'])}
    for kind,value in zip(('ticker','book'),public):
        if isinstance(value,Exception):data[kind]=None;data['errors'][kind]='Market data delayed; retrying'
        else:data[kind]=value
    for key in ('positions','orders','fills'):
        data[key]=[{**row,'product_symbol':row.get('product_symbol') or (row.get('product') or {}).get('symbol') or row.get('symbol') or catalog.get(row.get('product_id'),{}).get('symbol')} for row in data[key]]
    with db() as c:reset=c.execute('SELECT fills_since FROM workspace_resets WHERE id=1').fetchone()
    data['fills']=since_reset(data['fills'],reset['fills_since'] if reset else None)
    data['fills']=sorted(data['fills'],key=lambda row:str(row.get('created_at','')),reverse=True)
    return data

MARKET_TICKERS = {'time':0, 'data':None}
MARKET_TICKERS_LOCK = asyncio.Lock()

@app.get('/api/market-tickers')
async def market_tickers(user=Depends(authenticated)):
    async with MARKET_TICKERS_LOCK:
        if time.time()-MARKET_TICKERS['time'] < 10:
            return MARKET_TICKERS['data']
        try:
            products, tickers = await asyncio.gather(
                exchange.products(),
                exchange.request('/v2/tickers',{'contract_types':'perpetual_futures'}))
            quotes = {t['symbol']:t for t in tickers}
            rows = []
            for product in products:
                quote = quotes.get(product['symbol'])
                if not quote:
                    continue
                def number(value):
                    try:
                        result = float(value)
                        return result if math.isfinite(result) else None
                    except (TypeError,ValueError):
                        return None
                price = number(quote.get('close'))
                if price is None or price <= 0:
                    continue
                rows.append({'symbol':product['symbol'],'price':price,
                             'change_24h':number(quote.get('ltp_change_24h')),
                             'tick_size':product['tick_size'],'timestamp':quote.get('timestamp')})
            if not rows:
                raise ValueError('No market quotes available')
            data = {'tickers':rows,'updated_at':datetime.now(timezone.utc).isoformat()}
            MARKET_TICKERS.update(time=time.time(),data=data)
            return data
        except Exception:
            raise HTTPException(502,'Market prices are temporarily unavailable')

@app.get('/api/products')
async def products(user=Depends(authenticated)):
    try:
        rows = await exchange.products()
        return {'products':[{k:p.get(k) for k in ('id','symbol','description','contract_value','tick_size','trading_status','notional_type')} for p in rows],
                'timeframes':list(TIMEFRAMES)}
    except Exception:
        raise HTTPException(502,'Unable to load exchange instruments')

@app.get('/api/candles')
async def candles(resolution: str='5m',symbol: str='BTCUSD',instance_id: UUID|None=None,user=Depends(authenticated)):
    if resolution not in TIMEFRAMES:
        raise HTTPException(400,'Invalid timeframe')
    try:
        await exchange.product(symbol)
        seconds = TIMEFRAMES[resolution]
        rows = await exchange.history(symbol,resolution,int(time.time())-seconds*1000,int(time.time()))
        if instance_id:
            record = engine.record(str(instance_id))
            if not record or record['symbol'] != symbol:
                raise ValueError('Instance does not match the requested instrument')
        else:
            matches = [r for r in engine.records() if r['symbol'] == symbol]
            record = matches[0] if len(matches) == 1 else None
        config = StrategyConfig(**record['config']) if record else StrategyConfig(symbol=symbol)
        return supertrend(rows,config.atr_period,config.multiplier)[-200:]
    except ValueError as e:
        raise HTTPException(400,str(e))
    except Exception:
        raise HTTPException(502,'Unable to load exchange candles')

@app.get('/api/strategy/settings')
async def strategy_settings(user=Depends(authenticated)):
    return {'strategies':engine.records(),'defaults':StrategyConfig().model_dump(),'timeframes':list(TIMEFRAMES)}

@app.post('/api/strategy/settings')
async def save_strategy(body:StrategyConfig,user=Depends(authenticated)):
    try:
        record = await engine.save(body)
        return {'ok':True,'strategy':record}
    except ValueError as e:
        raise HTTPException(400,str(e))
    except Exception:
        raise HTTPException(502,'Exchange unavailable; settings were not applied')

class InstanceSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(default='', max_length=80)
    config: StrategyConfig
    execution: ExecutionSelection = Field(default_factory=ExecutionSelection)

class InstanceCreate(InstanceSettings):
    request_id: UUID

class InstanceControl(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['start', 'stop', 'exit']

@app.get('/api/strategy/instances')
async def list_instances(user=Depends(authenticated)):
    return {'instances':engine.records(), 'defaults':StrategyConfig().model_dump(), 'timeframes':list(TIMEFRAMES)}

@app.post('/api/strategy/instances')
async def create_instance(body:InstanceCreate,user=Depends(authenticated)):
    try:
        record = await engine.create_instance(body.config, body.name, str(body.request_id),**({'execution':body.execution} if body.execution.mode!='primary' else {}))
        return {'ok':True, 'instance':record}
    except ValueError as error:
        raise HTTPException(400,str(error))
    except Exception:
        raise HTTPException(502,'Exchange unavailable; retry this same create request to check whether the instance was saved')

@app.put('/api/strategy/instances/{instance_id}')
async def update_instance(instance_id:UUID,body:InstanceSettings,user=Depends(authenticated)):
    if not engine.record(str(instance_id)):
        raise HTTPException(404,'Instance not found')
    try:
        return {'ok':True,'instance':await engine.update_instance(str(instance_id),body.config,body.name,**({'execution':body.execution} if body.execution.mode!='primary' else {}))}
    except ValueError as error:
        raise HTTPException(400,str(error))
    except Exception:
        raise HTTPException(502,'Exchange unavailable; check saved instance settings before retrying')

@app.delete('/api/strategy/instances/{instance_id}')
async def delete_instance(instance_id:UUID,user=Depends(authenticated)):
    if not engine.record(str(instance_id)):
        raise HTTPException(404,'Instance not found')
    try:
        deleted_id = await engine.delete_instance(str(instance_id))
        return {'ok':True,'deleted_id':deleted_id}
    except InstanceConflict as error:
        raise HTTPException(409,str(error))
    except ValueError as error:
        raise HTTPException(400,str(error))
    except Exception:
        raise HTTPException(500,'Instance could not be deleted; refresh its status before retrying')

@app.post('/api/strategy/instances/{instance_id}/control')
async def control_instance(instance_id:UUID,body:InstanceControl,user=Depends(authenticated)):
    if not engine.record(str(instance_id)):
        raise HTTPException(404,'Instance not found')
    try:
        record = await engine.control(str(instance_id),body.action)
        CACHE.clear();DASHBOARD_READS.clear()
        return JSONResponse({'ok':True,'instance':record},status_code=202 if record['state'].get('exit_requested') else 200)
    except ValueError as error:
        raise HTTPException(400,str(error))
    except Exception:
        raise HTTPException(502,'Exchange unavailable; refresh instance status before retrying')

class Control(BaseModel):
    symbol: str = Field(pattern=r'^[A-Z0-9]{3,24}$')
    action: str = Field(pattern=r'^(start|pause|close)$')

@app.post('/api/strategy/control')
async def control_strategy(body:Control,user=Depends(authenticated)):
    try:
        result = await engine.control(body.symbol,body.action)
        CACHE.clear();DASHBOARD_READS.clear()
        return result
    except ValueError as e:
        raise HTTPException(400,str(e))
    except Exception:
        raise HTTPException(502,'Exchange unavailable; check strategy status before retrying')

@app.get('/api/logs')
async def logs(user=Depends(authenticated)):
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 300')]

class Preset(BaseModel):
    name: str = Field(min_length=1,max_length=80)
    config: dict

@app.get('/api/presets')
async def presets(user=Depends(authenticated)):
    with db() as c:
        return [{'id':r['id'],'name':r['name'],'config':json.loads(r['config'])} for r in c.execute("SELECT * FROM presets WHERE config LIKE '%atr_period%'")]

@app.post('/api/presets')
async def save_preset(body: Preset,user=Depends(authenticated)):
    with db() as c:
        c.execute('INSERT INTO presets(name,config) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET config=excluded.config',(body.name,json.dumps(body.config)))
    log(f'Backtest preset saved: {body.name}')
    return {'ok':True}

@app.post('/api/backtest')
async def backtest(body:Backtest,user=Depends(authenticated)):
    try:
        start, end = body.time_bounds(time.time())
        seconds = TIMEFRAMES[body.resolution]
        p = await exchange.product(body.symbol)
        warmup = max(500,body.atr_period*5)
        history = await exchange.history(body.symbol,body.resolution,start-seconds*warmup,end)
        history, selected, warmup_count = validate_history(history,body,start,end)
        result = run_backtest(history,body,float(p['contract_value']),float(p['tick_size']),trade_start=start,trade_end=end)
        result['source'] = 'Delta Testnet · demo market · traded-price candles'
        result['data'] = {'source':'testnet','label':'Delta Testnet · demo market','endpoint':exchange.BASE,
                          'price_type':'traded_price','timezone':body.timezone,'requested_start':start,'requested_end':end,
                          'first_candle':selected[0]['time'],'last_candle':selected[-1]['time'],
                          'closed_through':selected[-1]['time']+seconds,'warmup_candles':warmup_count,
                          'range_convention':'Candle open >= start; candle close <= end. End is exclusive for candle opens.'}
        for trade in result['trades']:
            trade['data_source'] = body.data_source
        result['config'] = body.model_dump()
    except ValueError as e:
        raise HTTPException(400,str(e))
    except Exception:
        raise HTTPException(502,'Historical market data unavailable. Backtest was not run.')
    with db() as c:
        cursor=c.execute('INSERT INTO runs(time,config,result) VALUES(?,?,?)',(datetime.now(timezone.utc).isoformat(),body.model_dump_json(),json.dumps(result)))
        result['id']=cursor.lastrowid
    log(f'Backtest #{result["id"]} completed: {len(result["trades"])} trades')
    return result

@app.get('/api/runs')
async def runs(summary:bool=False,user=Depends(authenticated)):
    with db() as c:
        if summary:
            return [{'id':r['id'],'time':r['time'],'config':json.loads(r['config']),'metrics':json.loads(r['metrics'] or '{}'),'data':json.loads(r['data'] or '{}'),'source':r['source']} for r in c.execute("SELECT id,time,config,json_extract(result,'$.metrics') metrics,json_extract(result,'$.data') data,json_extract(result,'$.source') source FROM runs WHERE config LIKE '%atr_period%' ORDER BY id DESC LIMIT 50")]
        return [{'id':r['id'],'time':r['time'],'config':json.loads(r['config']),**json.loads(r['result'])} for r in c.execute("SELECT * FROM runs WHERE config LIKE '%atr_period%' ORDER BY id DESC LIMIT 50")]


@app.get('/api/runs/{run_id}')
async def run_report(run_id:int,user=Depends(authenticated)):
    with db() as c:row=c.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row:raise HTTPException(404,'Run not found')
    return {'id':row['id'],'time':row['time'],'config':json.loads(row['config']),**json.loads(row['result'])}


@app.get('/api/export/{kind}')
async def export(kind:str,run_id:int|None=None,user=Depends(authenticated)):
    if kind=='backtest':
        with db() as c:
            row=c.execute('SELECT result FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row: raise HTTPException(404,'Run not found')
        rows=json.loads(row['result'])['trades']
    elif kind=='logs':
        rows=await logs(user)
    elif kind in ('fills','orders','positions'):
        data=await snapshot(user=user)
        if kind in data['errors']: raise HTTPException(502,data['errors'][kind])
        rows=data[kind]
    else: raise HTTPException(404,'Report not found')
    output=io.StringIO()
    fields=list(dict.fromkeys(k for row in rows for k in row)) if rows else ['status']
    writer=csv.DictWriter(output,fieldnames=fields)
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("'"+str(v) if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v) for k,v in row.items()})
    return Response(output.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="tokeniq-{kind}.csv"'})

@app.get('/health')
async def health():
    return {'status':'ok'}

from backend.engine import InstanceConflict
from backend.multi_account import MultiAccountEngine
brokers = BrokerStore(db,os.environ.get('BROKER_KEY_FILE',str(DB.parent/'.broker-key')))
engine = MultiAccountEngine(db,log,brokers)

@app.get('/api/brokers')
async def broker_accounts(user=Depends(authenticated)):
    return {'accounts':brokers.accounts(),'brokers':[{'id':'delta_testnet','name':'Delta Exchange','environment':'Testnet'}]}

@app.post('/api/brokers')
async def add_broker(body:AccountInput,user=Depends(authenticated)):
    try:
        account=await brokers.add(body)
        CACHE.clear();DASHBOARD_READS.clear();log(f'Broker account connected: {account["client_name"]} · Delta Testnet')
        return {'account':account}
    except ValueError as exc:raise HTTPException(400,str(exc))
    except Exception:raise HTTPException(502,'Unable to verify Delta Testnet credentials. Check API permissions and IP whitelist.')

@app.post('/api/brokers/{account_id}/primary')
async def primary_broker(account_id:str,user=Depends(authenticated)):
    try:
        async with engine.lock:brokers.primary(account_id)
        CACHE.clear();DASHBOARD_READS.clear();return {'accounts':brokers.accounts()}
    except ValueError as exc:raise HTTPException(400,str(exc))

@app.post('/api/brokers/{account_id}/verify')
async def verify_broker(account_id:str,user=Depends(authenticated)):
    try:
        account=await brokers.verify(account_id)
        return {'account':account}
    except ValueError as exc:raise HTTPException(400,str(exc))
    except Exception:raise HTTPException(502,'Delta Testnet account verification unavailable')

