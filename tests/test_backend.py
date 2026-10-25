import asyncio, hashlib, json, os, secrets, sqlite3, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import httpx

_test_db=tempfile.TemporaryDirectory(prefix='tokeniq-tests-')
os.environ['TOKENIQ_DB']=str(Path(_test_db.name)/'tests.db')
os.environ['SESSION_SECRET']=secrets.token_hex(32)
os.environ['DELTA_ENV']='testnet'
from backend import main
from backend.strategy import StrategyConfig,supertrend,protection,TIMEFRAMES
from backend.backtest import Backtest,run_backtest
from backend.engine import Engine
from backend import delta_client as exchange

PRODUCT={'id':84,'symbol':'BTCUSD','contract_value':'0.001','tick_size':'0.1','initial_margin':'1','trading_status':'operational'}

def bars():
    values=[100]*15+[105,110,120,130,140,145,140,125,110,95,80,70,60,65,80,100,120,140,150]
    return [{'time':i*60,'open':v,'high':v+2,'low':v-2,'close':v} for i,v in enumerate(values)]

class IndicatorTests(unittest.TestCase):
    def test_wilder_atr_and_first_seed_not_entry(self):
        data=supertrend(bars(),3,2)
        self.assertEqual(data[2]['atr'],4)
        self.assertEqual(data[2]['signal'],0)
        self.assertEqual(data[2]['direction'],-1)
        self.assertEqual({p['signal'] for p in data},{-1,0,1})
    def test_no_future_candle_changes_a_prior_signal(self):
        rows=bars();full=supertrend(rows,3,2)
        for i in range(3,len(rows)):
            self.assertEqual(supertrend(rows[:i],3,2),full[:i])
    def test_bad_ohlc_and_nonfinite_rejected(self):
        for bad in ({'open':1,'high':.5,'low':.1,'close':1},{'open':1,'high':float('inf'),'low':.1,'close':1}):
            with self.assertRaises(ValueError):supertrend([{'time':1,**bad}])
    def test_points_and_tick_rounding_both_sides(self):
        c=StrategyConfig(sl_points=5,target_points=10)
        self.assertEqual(protection(100,1,c,.1),{'stop':95,'target':110})
        self.assertEqual(protection(100,-1,c,.1),{'stop':105,'target':90})
        self.assertEqual(protection(100,1,StrategyConfig(sl_points=0,target_points=0)),{'stop':None,'target':None})
    def test_opposite_signal_mode_disables_point_protection(self):
        self.assertEqual(protection(100,1,StrategyConfig(exit_mode='opposite_signal')),{'stop':None,'target':None})
        with self.assertRaises(ValueError):
            StrategyConfig(exit_mode='sl_tp',sl_points=0,target_points=0)
        with self.assertRaises(ValueError):
            StrategyConfig(exit_mode='unknown')

    def test_config_rejects_invalid_inputs(self):
        for values in ({'quantity':0},{'multiplier':float('nan')},{'resolution':'7d'},{'atr_period':1},{'fast':9}):
            with self.assertRaises(ValueError):StrategyConfig(**values)
        self.assertEqual(len(TIMEFRAMES),11)

class BacktestTests(unittest.TestCase):
    def config(self,**kwargs):
        return Backtest(start='2026-01-01',end='2026-01-02',atr_period=3,multiplier=2,quantity=10,sl_points=0,target_points=0,**kwargs)
    def test_flat_market_produces_no_trades(self):
        rows=[{'time':i*60,'open':100,'high':101,'low':99,'close':100} for i in range(20)]
        self.assertEqual(run_backtest(rows,self.config())['metrics']['total_trades'],0)
    def test_flips_trade_both_sides_at_next_open(self):
        c=self.config();rows=bars();result=run_backtest(rows,c)
        signals={rows[i+1]['time']:p['signal'] for i,p in enumerate(supertrend(rows,c.atr_period,c.multiplier)[:-1]) if p['signal']}
        self.assertEqual({t['side'] for t in result['trades']},{'LONG','SHORT'})
        for trade in result['trades']:
            i=next(i for i,r in enumerate(rows) if r['time']==trade['entry_time'])
            side=1 if trade['side']=='LONG' else -1
            self.assertEqual(signals[trade['entry_time']],side)
            self.assertAlmostEqual(trade['entry'],rows[i]['open']*(1+side*c.slippage_bps/10000))
        self.assertAlmostEqual(result['metrics']['net_pnl'],sum(t['pnl'] for t in result['trades']),places=4)
    def test_sl_tp_mode_holds_across_opposite_flips(self):
        c=Backtest(**{**self.config().model_dump(),'exit_mode':'sl_tp','target_points':1000})
        result=run_backtest(bars(),c)
        self.assertEqual(len(result['trades']),1)
        self.assertEqual(result['trades'][0]['reason'],'end_of_test')
        self.assertEqual(result['trades'][0]['side'],'LONG')

    def test_opposite_signal_mode_ignores_sl_tp(self):
        c=Backtest(**{**self.config().model_dump(),'exit_mode':'opposite_signal','sl_points':1,'target_points':1})
        result=run_backtest(bars(),c)
        self.assertTrue(any(t['reason']=='flip' for t in result['trades']))
        self.assertFalse(any(t['reason'] in ('stop_loss','target') for t in result['trades']))

    def test_costs_reduce_returns(self):
        cheap=run_backtest(bars(),self.config(fee_bps=0,slippage_bps=0))
        costly=run_backtest(bars(),self.config(fee_bps=20,slippage_bps=10))
        self.assertGreater(cheap['metrics']['net_pnl'],costly['metrics']['net_pnl'])
    def test_insufficient_history(self):
        with self.assertRaises(ValueError):run_backtest([],self.config())
    def test_risk_limits_and_contract_multiplier(self):
        blocked=run_backtest(bars(),self.config(max_notional=.1))
        self.assertEqual(blocked['trades'],[])
        c=self.config(fee_bps=0,slippage_bps=0)
        small=run_backtest(bars(),c,contract_value=.001)
        large=run_backtest(bars(),c,contract_value=.01)
        self.assertAlmostEqual(large['metrics']['net_pnl'],10*small['metrics']['net_pnl'],places=3)
    def test_sl_wins_when_both_levels_touch_and_gap_stops_slip(self):
        rows=bars();points=supertrend(rows,3,2)
        index=next(i+1 for i,p in enumerate(points[:-1]) if p['signal']==1)
        rows[index].update(high=rows[index]['open']+20,low=rows[index]['open']-20)
        c=Backtest(**{**self.config().model_dump(),'sl_points':5,'target_points':5,'slippage_bps':0})
        r=run_backtest(rows,c)
        self.assertEqual(r['trades'][0]['reason'],'stop_loss')
        self.assertAlmostEqual(r['trades'][0]['exit'],r['trades'][0]['entry']-5)
    def test_warmup_does_not_trade_before_start(self):
        result=run_backtest(bars(),self.config(),trade_start=25*60)
        self.assertTrue(all(t['entry_time']>=25*60 for t in result['trades']))
        self.assertTrue(all(p['time']>=25*60 for p in result['equity']))

class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://testserver')
        salt=bytes.fromhex('aabbccddeeff00112233445566778899')
        hashed=hashlib.scrypt(b'test-password',salt=salt,n=16384,r=8,p=1).hex()
        self.password_patch=patch.object(main,'PASSWORD_HASH',salt.hex()+':'+hashed)
        self.password_patch.start();self.addCleanup(self.password_patch.stop)
        main.LOGIN_ATTEMPTS.clear()
    async def asyncTearDown(self):await self.client.aclose()
    async def login(self):return await self.client.post('/api/login',json={'username':'admin','password':'test-password'})
    async def test_private_routes_require_login(self):
        for path in ('snapshot','logs','runs','presets','export/logs','session','products','market-tickers','strategy/settings'):
            self.assertEqual((await self.client.get('/api/'+path)).status_code,401)
    async def test_login_cookie_logout_and_forgery(self):
        r=await self.login();self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie']);self.assertIn('Secure',r.headers['set-cookie'])
        self.assertEqual((await self.client.get('/api/session')).status_code,200)
        self.assertEqual((await self.client.post('/api/logout')).status_code,200)
        self.assertEqual((await self.client.get('/api/session')).status_code,401)
        self.client.cookies.set('tokeniq_session','admin:9999999999:nonce.forged')
        self.assertEqual((await self.client.get('/api/session')).status_code,401)
    async def test_csrf_and_login_limit(self):
        await self.login()
        r=await self.client.post('/api/strategy/control',headers={'origin':'https://evil.invalid'},json={'symbol':'BTCUSD','action':'start'})
        self.assertEqual(r.status_code,403)
        for _ in range(8):self.assertEqual((await self.client.post('/api/login',json={'username':'admin','password':'wrong'})).status_code,401)
        self.assertEqual((await self.client.post('/api/login',json={'username':'admin','password':'wrong'})).status_code,429)
    async def test_csv_numeric_losses_and_formula_escaping(self):
        await self.login()
        with patch.object(main,'snapshot',new=AsyncMock(return_value={'errors':{},'fills':[{'pnl':-5.5,'symbol':'=BAD()'}]})):
            r=await self.client.get('/api/export/fills');self.assertEqual(r.status_code,200)
            self.assertIn("-5.5,'=BAD()",r.text)
    async def test_market_tickers_filter_invalid_prices_and_cache_public_data(self):
        await self.login()
        main.MARKET_TICKERS.update(time=0,data=None)
        quotes=[{'symbol':'BTCUSD','close':'80000.1','ltp_change_24h':'-1.2','timestamp':123},
                {'symbol':'ADAUSD','close':'nan','ltp_change_24h':'0'},
                {'symbol':'UNLISTED','close':'12','ltp_change_24h':'3'}]
        with patch.object(exchange,'products',new=AsyncMock(return_value=[PRODUCT,{**PRODUCT,'symbol':'ADAUSD'}])),patch.object(exchange,'request',new=AsyncMock(return_value=quotes)) as request:
            response=await self.client.get('/api/market-tickers')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['tickers'],[{'symbol':'BTCUSD','price':80000.1,'change_24h':-1.2,'tick_size':'0.1','timestamp':123}])
            self.assertEqual((await self.client.get('/api/market-tickers')).status_code,200)
            self.assertEqual(request.call_count,1)
        main.MARKET_TICKERS.update(time=0,data=None)

    async def test_products_include_contract_metadata_for_live_pnl(self):
        await self.login()
        with patch.object(exchange,'products',new=AsyncMock(return_value=[{**PRODUCT,'notional_type':'vanilla'}])):
            response=await self.client.get('/api/products')
            self.assertEqual(response.status_code,200)
            product=response.json()['products'][0]
            self.assertEqual(product['notional_type'],'vanilla')
            self.assertEqual(product['contract_value'],'0.001')

    async def test_market_ticker_includes_every_available_instrument(self):
        await self.login()
        main.MARKET_TICKERS.update(time=0,data=None)
        self.addCleanup(lambda:main.MARKET_TICKERS.update(time=0,data=None))
        products=[{**PRODUCT,'symbol':f'COIN{i}USD','id':i} for i in range(20)]
        quotes=[{'symbol':p['symbol'],'close':'123.45','ltp_change_24h':'1.2'} for p in products]
        with patch.object(exchange,'products',new=AsyncMock(return_value=products)),patch.object(exchange,'request',new=AsyncMock(return_value=quotes)):
            response=await self.client.get('/api/market-tickers')
            self.assertEqual(response.status_code,200)
            self.assertEqual([q['symbol'] for q in response.json()['tickers']],[p['symbol'] for p in products])

    async def test_invalid_backtest_never_calls_exchange(self):
        await self.login()
        with patch.object(exchange,'request',new=AsyncMock()) as mock:
            r=await self.client.post('/api/backtest',json={'start':'2026-01-01','end':'2026-02-01','resolution':'bad'})
            self.assertEqual(r.status_code,422);mock.assert_not_called()
    async def test_one_minute_backtest_date_limit_and_successful_result(self):
        await self.login()
        start = 1767225600  # 2026-01-01 UTC
        history = [{**row,'time':start+row['time']} for row in bars()]
        history = [{'time':start-i*60,'open':100,'high':102,'low':98,'close':100} for i in range(1,501)]+history
        history += [{'time':stamp,'open':150,'high':152,'low':148,'close':150} for stamp in range(start+len(bars())*60,start+3*86400,60)]
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)) as product, patch.object(exchange,'history',new=AsyncMock(return_value=history)) as candles:
            invalid = await self.client.post('/api/backtest',json={'start':'2026-01-01','end':'2026-01-07','resolution':'1m'})
            self.assertEqual(invalid.status_code,400)
            product.assert_not_called()
            candles.assert_not_called()
            response = await self.client.post('/api/backtest',json={'start':'2026-01-01','end':'2026-01-04','resolution':'1m','atr_period':3,'multiplier':2,'sl_points':0,'target_points':0})
            self.assertEqual(response.status_code,200,response.text)
            result = response.json()
            self.assertGreater(result['metrics']['total_trades'],0)
            self.assertGreater(len(result['equity']),0)
            self.assertEqual(result['config']['resolution'],'1m')
            candles.assert_awaited_once_with('BTCUSD','1m',start-500*60,start+3*86400)
            saved = (await self.client.get('/api/runs')).json()
            self.assertTrue(any(run['id']==result['id'] for run in saved))
    async def test_settings_persist_and_reject_production(self):
        await self.login()
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)):
            r=await self.client.post('/api/strategy/settings',json=StrategyConfig().model_dump())
            self.assertEqual(r.status_code,200)
        r=await self.client.get('/api/strategy/settings')
        self.assertEqual(r.json()['strategies'][0]['config']['atr_period'],10)
        with patch.dict(os.environ,{'DELTA_ENV':'india'}):
            with self.assertRaisesRegex(ValueError,'testnet'):await exchange.request('/v2/orders',private=True,method='POST',body={})

class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'engine.db'
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;return c
        self.engine=Engine(db,lambda *args:None)
        self.config=StrategyConfig()
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)):
            await self.engine.save(self.config)
    async def test_external_position_is_not_adopted_or_closed(self):
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)),patch.object(self.engine,'position',new=AsyncMock(return_value={'size':100})),patch.object(exchange,'request',new=AsyncMock()) as request:
            with self.assertRaisesRegex(ValueError,'existing'):await self.engine.control('BTCUSD','start')
            with self.assertRaisesRegex(ValueError,'outside'):await self.engine.control('BTCUSD','close')
            request.assert_not_called()
    async def test_uncertain_order_is_never_submitted_twice(self):
        r=self.engine.record('BTCUSD')
        with patch.object(exchange,'request',new=AsyncMock(side_effect=TimeoutError)) as request:
            with self.assertRaises(TimeoutError):await self.engine.submit(r,PRODUCT,'buy',1)
            persisted=self.engine.record('BTCUSD')
            self.assertTrue(persisted['state']['pending'])
            with self.assertRaisesRegex(ValueError,'reconciliation'):await self.engine.submit(persisted,PRODUCT,'buy',1)
            self.assertEqual(request.call_count,1)
    async def test_forward_uses_shared_green_and_red_flips(self):
        await self.check_forward_mode('both')

    async def test_forward_sl_tp_holds_on_opposite_signal(self):
        await self.check_forward_mode('sl_tp')

    async def test_forward_opposite_signal_ignores_point_exits(self):
        await self.check_forward_mode('opposite_signal')

    async def check_forward_mode(self,mode):
        config=StrategyConfig(atr_period=3,multiplier=2,exit_mode=mode,sl_points=0,target_points=1000 if mode=='sl_tp' else 0)
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)):
            await self.engine.save(config)
        r=self.engine.record('BTCUSD');r['enabled']=True
        source=bars();points=supertrend(source,3,2)
        flips=[i for i,p in enumerate(points) if p['signal']]
        now=int(time.time());seconds=300
        current_size=0;submitted=[]
        async def position(p):return {'size':current_size,'entry_price':'100'}
        async def submit(record,p,side,size,reduce_only=False):
            nonlocal current_size
            submitted.append((side,reduce_only))
            current_size=0 if reduce_only else (size if side=='buy' else -size)
            return {'size':size,'unfilled_size':0}
        async def request(path,params=None,private=False,**kwargs):
            if path=='/v2/wallet/balances':return [{'asset_symbol':'USD','balance':'10000','available_balance':'10000'}]
            if path=='/v2/positions/margined':return []
            if 'leverage' in path:return {'leverage':1}
            if 'tickers' in path:return {'close':'100','timestamp':time.time()*1000000}
            raise AssertionError(path)
        async def protect(record,p,c,pos):
            record['state']['position']={'size':int(pos['size']),'entry':pos['entry_price']}
        with patch.object(self.engine,'protect',new=protect),patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)),patch.object(exchange,'request',new=request),patch.object(self.engine,'position',new=position),patch.object(self.engine,'submit',new=submit),patch.object(self.engine,'active_orders',new=AsyncMock(return_value=[])):
            for index in flips:
                rows=[{**row,'time':now-seconds*(index+1-i)} for i,row in enumerate(source[:index+1])]
                self.engine.buffers.pop('BTCUSD',None)
                r['state']['last_bar']=rows[-1]['time']-seconds
                with patch.object(exchange,'history',new=AsyncMock(return_value=rows)):
                    await self.engine.step(r)
        if mode=='sl_tp':
            self.assertEqual(submitted,[('buy',False)])
            return
        self.assertEqual(submitted[0],('buy',False))
        self.assertIn(('sell',True),submitted)
        self.assertIn(('sell',False),submitted)
        self.assertIn(('buy',True),submitted)

    async def test_rejected_order_clears_intent_without_retry(self):
        r=self.engine.record('BTCUSD')
        with patch.object(exchange,'request',new=AsyncMock(side_effect=exchange.ExchangeError(400,'insufficient_margin'))) as request:
            with self.assertRaisesRegex(ValueError,'insufficient_margin'):await self.engine.submit(r,PRODUCT,'buy',1)
            self.assertIsNone(self.engine.record('BTCUSD')['state']['pending'])
            self.assertEqual(request.call_count,1)

    async def test_restart_disables_entries_and_preserves_position(self):
        r=self.engine.record('BTCUSD');r['enabled']=True;r['state']['owned']=True;self.engine.persist(r)
        restarted=Engine(self.engine.db,lambda *args:None)
        self.assertFalse(restarted.record('BTCUSD')['enabled'])
        self.assertTrue(restarted.record('BTCUSD')['state']['owned'])
    async def test_cannot_change_open_strategy_settings(self):
        r=self.engine.record('BTCUSD');r['state']['owned']=True;self.engine.persist(r)
        with self.assertRaisesRegex(ValueError,'Pause and close'):await self.engine.save(self.config)
    async def test_external_reversal_is_not_closed(self):
        r=self.engine.record('BTCUSD');r['state'].update(owned=True,position={'size':1});self.engine.persist(r)
        with patch.object(self.engine,'position',new=AsyncMock(return_value={'size':-1})),patch.object(self.engine,'submit',new=AsyncMock()) as submit:
            with self.assertRaisesRegex(ValueError,'externally'):await self.engine.close(r,PRODUCT)
            submit.assert_not_called()

    async def test_unfilled_close_prevents_reversal(self):
        r=self.engine.record('BTCUSD');r['state']['owned']=True;self.engine.persist(r)
        with patch.object(self.engine,'position',new=AsyncMock(return_value={'size':1})),patch.object(self.engine,'submit',new=AsyncMock()) as submit:
            with self.assertRaisesRegex(ValueError,'not fully filled'):await self.engine.close(r,PRODUCT)
            self.assertTrue(r['state']['owned']);self.assertEqual(submit.call_count,1)
    async def test_bracket_failure_emergency_close_path(self):
        r=self.engine.record('BTCUSD');r['state']['owned']=True;self.engine.persist(r)
        with patch.object(self.engine,'active_orders',new=AsyncMock(return_value=[])),patch.object(exchange,'request',new=AsyncMock(return_value=None)):
            with self.assertRaisesRegex(ValueError,'protection'):await self.engine.protect(r,PRODUCT,self.config,{'size':1,'entry_price':'80000'})

if __name__=='__main__':unittest.main()
