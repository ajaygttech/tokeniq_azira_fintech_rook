"""Closed-candle audit cadence and durable deduplication; no real exchange calls."""
import json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from backend.engine import Engine
from backend.strategy import StrategyConfig,TIMEFRAMES,supertrend
from backend import delta_client as exchange

PRODUCT={'id':84,'symbol':'BTCUSD','contract_value':'.001','tick_size':'.1','trading_status':'operational'}
NOW=1792920000

class CandleLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'audit.db';self.events=[]
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;return c
        self.db=db
        self.engine=Engine(db,lambda message,level='INFO':self.events.append((message,level)))
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)):
            self.record=await self.engine.create_instance(StrategyConfig(atr_period=3),name='Five minute test')

    def journal(self):
        with self.db() as c:return [dict(row) for row in c.execute('SELECT * FROM strategy_candle_logs ORDER BY candle_time')]

    def point(self,timestamp=NOW-300):
        return {'time':timestamp,'open':100.,'high':105.,'low':99.,'close':104.,'atr':4.25,'supertrend':98.5,'direction':1,'signal':1}

    def test_durable_unique_key_preserves_numeric_values_and_instance_metadata(self):
        self.assertTrue(self.engine.log_candle(self.record,self.point(),'SIGNAL',NOW))
        self.assertFalse(self.engine.log_candle(self.record,self.point(),'SIGNAL',NOW))
        rows=self.journal();self.assertEqual(len(rows),1)
        payload=json.loads(rows[0]['payload'])
        for key in ('open','high','low','close','atr','supertrend','direction','signal'):
            self.assertEqual(payload[key],self.point()[key])
        self.assertEqual(payload['resolution'],'5m');self.assertEqual(payload['candle_close_time'],NOW)
        self.assertEqual(payload['instance_name'],'Five minute test');self.assertEqual(payload['environment'],'testnet')
        events=[message for message,level in self.events if level=='CANDLE']
        self.assertEqual(len(events),1)
        for text in ('Five minute test','BTCUSD','5m','O 100','H 105','L 99','C 104','ATR 4.25','ST 98.5','UP (GREEN)','Signal BUY','IST'):
            self.assertIn(text,events[0])

    def test_open_candle_is_never_logged(self):
        self.assertFalse(self.engine.log_candle(self.record,self.point(NOW),'WAIT',NOW))
        self.assertEqual(self.journal(),[])

    async def test_instances_and_changed_timeframes_have_independent_audits(self):
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)):
            other=await self.engine.create_instance(StrategyConfig(atr_period=3),name='Other instance')
        self.engine.log_candle(self.record,self.point(),'WAIT',NOW)
        self.engine.log_candle(other,self.point(),'WAIT',NOW)
        self.record['config']['resolution']='1m'
        self.engine.log_candle(self.record,self.point(),'WAIT',NOW)
        self.assertEqual(len(self.journal()),3)

    def test_restart_and_repeated_start_baseline_do_not_duplicate_the_bar(self):
        self.engine.log_candle(self.record,self.point(),'START BASELINE',NOW)
        restarted=Engine(self.db,lambda message,level='INFO':self.events.append((message,level)))
        record=restarted.record(self.record['id'])
        self.assertFalse(restarted.log_candle(record,self.point(),'START BASELINE',NOW))
        self.assertTrue(restarted.log_candle(record,self.point(NOW),'WAIT',NOW+300))
        self.assertEqual(len(self.journal()),2)

    async def exercise_cadence(self,resolution,gap=1):
        seconds=TIMEFRAMES[resolution]
        r=self.engine.record(self.record['id']);r['config']['resolution']=resolution;r['enabled']=True
        rows=[{'time':NOW-seconds*(30-i),'open':100,'high':102,'low':98,'close':100} for i in range(30)]
        async def request(path,*args,**kwargs):
            if path=='/v2/wallet/balances':return [{'asset_symbol':'USD','balance':10000,'available_balance':10000}]
            if path=='/v2/positions/margined':return []
            raise AssertionError('Unexpected exchange call: '+path)
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)),patch.object(exchange,'request',new=AsyncMock(side_effect=request)) as requests,patch.object(exchange,'history',new=AsyncMock(return_value=rows)) as history:
            with patch('backend.engine.time.time',return_value=NOW):
                await self.engine.step(r);await self.engine.step(r)
            self.assertEqual(len(self.journal()),1)  # one baseline, not 30 warmup bars or duplicate polls
            self.assertIn('START BASELINE',json.loads(self.journal()[0]['payload'])['decision'])
            more=[{'time':NOW+seconds*i,'open':100,'high':102,'low':98,'close':100} for i in range(gap)]
            history.return_value=rows+more
            with patch('backend.engine.time.time',return_value=NOW+seconds*gap):
                await self.engine.step(r);await self.engine.step(r)
            entries=self.journal();self.assertEqual(len(entries),1+gap)
            times=[row['candle_time'] for row in entries]
            self.assertTrue(all(b-a==seconds for a,b in zip(times,times[1:])))
            decisions=[json.loads(row['payload'])['decision'] for row in entries[1:]]
            self.assertTrue(all(('RESYNC' if gap>1 else 'WAIT') in decision for decision in decisions))
            self.assertTrue(all(call.args[0] in ('/v2/wallet/balances','/v2/positions/margined') for call in requests.await_args_list))

    async def test_one_minute_instance_emits_one_new_row_per_closed_minute(self):await self.exercise_cadence('1m')
    async def test_five_minute_instance_emits_one_new_row_per_closed_five_minutes(self):await self.exercise_cadence('5m')
    async def test_gap_logs_available_candles_without_submitting_historical_orders(self):await self.exercise_cadence('5m',gap=3)

    def test_error_trace_contains_instance_and_timeframe(self):
        self.engine.record_error(self.record,ValueError('Configured quantity exceeds maximum notional'))
        message,level=self.events[-1]
        self.assertEqual(level,'ERROR')
        for text in ('Five minute test','BTCUSD','5m','maximum notional'):self.assertIn(text,message)
