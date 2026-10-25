"""Signed Delta transport. Execution is locked to the configured test endpoint."""
import hashlib, hmac, json, os, time
from urllib.parse import urlencode
from contextvars import ContextVar
from contextlib import contextmanager
import httpx
BASE = 'https://cdn-ind.testnet.deltaex.org'
_credentials = ContextVar('delta_account_credentials', default=None)

@contextmanager
def account_credentials(credentials):
    token = _credentials.set(credentials)
    try:yield
    finally:_credentials.reset(token)

class ExchangeError(ValueError):
    def __init__(self,status,code):
        self.status, self.code = status, code
        super().__init__(f'Exchange HTTP {status}: {code}')

async def request(path,params=None,private=False,method='GET',body=None):
    if os.environ.get('DELTA_ENV') != 'testnet':
        raise ValueError('Exchange environment must be testnet')
    query = '?'+urlencode(params) if params else ''
    payload = json.dumps(body,separators=(',',':')) if body is not None else ''
    headers = {'User-Agent':'TokenIQ/2.0','Accept':'application/json','Content-Type':'application/json'}
    if private:
        key, secret = _credentials.get() or (os.environ.get('DELTA_API_KEY',''),os.environ.get('DELTA_API_SECRET',''))
        if not key or not secret:
            raise ValueError('Exchange credentials are not configured')
        timestamp = str(int(time.time()))
        signature = hmac.new(secret.encode(),(method+timestamp+path+query+payload).encode(),hashlib.sha256).hexdigest()
        headers.update({'api-key':key,'timestamp':timestamp,'signature':signature})
    transport = httpx.AsyncHTTPTransport(local_address='0.0.0.0',retries=0)
    async with httpx.AsyncClient(transport=transport,timeout=15,trust_env=False) as client:
        response = await client.request(method,BASE+path+query,headers=headers,content=payload or None)
        try:
            data = response.json()
        except ValueError:
            raise ValueError(f'Exchange HTTP {response.status_code}: invalid response') from None
        if response.status_code>=400 or not data.get('success'):
            error = data.get('error') or {}
            code = error.get('code','request_failed') if isinstance(error,dict) else 'request_failed'
            raise ExchangeError(response.status_code,code)
        return data.get('result')

_products = {'time':0,'rows':[]}
async def products():
    if time.time()-_products['time']<300:
        return _products['rows']
    rows = await request('/v2/products',{'page_size':1000})
    rows = [p for p in rows if p.get('contract_type')=='perpetual_futures' and p.get('state')=='live' and p.get('notional_type')=='vanilla']
    preferred = ['BTCUSD','ETHUSD','SOLUSD','XRPUSD','DOGEUSD','ADAUSD','1000SHIBUSD','DOGSUSD','XAUTUSD','PAXGUSD','USOONUSD']
    rows.sort(key=lambda p:preferred.index(p['symbol']) if p['symbol'] in preferred else 100)
    _products.update(time=time.time(),rows=rows)
    return rows

async def product(symbol):
    row = next((p for p in await products() if p['symbol']==symbol),None)
    if row is None:
        raise ValueError('Instrument is not an available linear perpetual')
    return row

async def history(symbol,resolution,start,end):
    from backend.strategy import TIMEFRAMES, normalize
    seconds = TIMEFRAMES[resolution]
    rows = []
    for chunk in range(int(start),int(end)+1,seconds*1500):
        rows.extend(await request('/v2/history/candles',{'symbol':symbol,'resolution':resolution,'start':chunk,'end':min(int(end),chunk+seconds*1500-1)}))
    # A request has one immutable end cutoff, even if the network call crosses
    # a candle boundary. Never include a bar that closes after the requested end.
    closed_through = min(float(end), time.time())
    return [r for r in normalize(rows) if start<=r['time']<=end and r['time']+seconds<=closed_through]
