"""Minute-boundary regression: candle fetches may finish after a bar closes."""
import unittest
from unittest.mock import AsyncMock,patch
import test_fresh_flip as fresh
from backend import delta_client as exchange

NOW=fresh.NOW
class HistoryCutoffTests(unittest.IsolatedAsyncioTestCase):
    async def test_requested_end_is_the_closed_candle_cutoff_even_after_network_delay(self):
        rows=[{'time':NOW-300,'open':100,'high':102,'low':98,'close':100},{'time':NOW,'open':100,'high':102,'low':98,'close':101}]
        with patch.object(exchange,'request',new=AsyncMock(return_value=rows)),patch('backend.delta_client.time.time',return_value=NOW+301):
            result=await exchange.history('BTCUSD','5m',NOW-300,NOW+299)
        self.assertEqual([row['time'] for row in result],[NOW-300])

class CandleBoundaryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fresh.FreshFlipTests.asyncSetUp
    create=fresh.FreshFlipTests.create
    prepare=fresh.FreshFlipTests.prepare
    async def test_candle_closing_during_fetch_does_not_stop_instance(self):
        record=await self.prepare();self.clock=NOW+299
        rows=[{'time':NOW-300*(19-i),'open':100,'high':102,'low':98,'close':100,'atr':1,'supertrend':99,'direction':1,'signal':1 if i==19 else 0} for i in range(20)]
        async def history(*args):self.clock=NOW+300;return rows
        with patch.object(exchange,'history',new=history),patch('backend.engine.supertrend',side_effect=lambda candles,*args:sorted(candles,key=lambda row:row['time'])):
            await self.engine.step(record)
            await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(len(self.venue.submitted),1)
        self.assertEqual(record['state']['last_bar'],NOW)
    async def test_one_minute_boundary_race_remains_running_and_trades_once(self):
        from backend.strategy import StrategyConfig
        record=await self.prepare();await self.engine.control(record['id'],'stop')
        config=StrategyConfig(**{**record['config'],'resolution':'1m'})
        await self.engine.update_instance(record['id'],config,record['name']);await self.engine.control(record['id'],'start')
        record=self.engine.record(record['id']);record['state']['last_bar']=NOW-60;self.engine.persist(record);self.clock=NOW+59
        rows=[{'time':NOW-60*(19-i),'open':100,'high':102,'low':98,'close':100,'atr':1,'supertrend':99,'direction':1,'signal':1 if i==19 else 0} for i in range(20)]
        async def history(*args):self.clock=NOW+60;return rows
        with patch.object(exchange,'history',new=history),patch('backend.engine.supertrend',side_effect=lambda candles,*args:sorted(candles,key=lambda row:row['time'])):
            await self.engine.step(record);await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(len(self.venue.submitted),1)

    async def test_provisional_flip_is_not_buffered_then_closed_flip_executes_once(self):
        record=await self.prepare();self.clock=NOW+299
        rows=[{'time':NOW-300*(19-i),'open':100,'high':102,'low':98,'close':100,'atr':1,'supertrend':99,'direction':1,'signal':1 if i==19 else 0} for i in range(20)]
        with patch.object(exchange,'history',new=AsyncMock(return_value=rows)),patch('backend.engine.supertrend',side_effect=lambda candles,*args:sorted(candles,key=lambda row:row['time'])):
            await self.engine.step(record)
            self.assertTrue(record['enabled']);self.assertFalse(self.venue.submitted)
            self.assertNotIn(NOW,[row['time'] for row in self.engine.buffers[record['id']]])
            self.clock=NOW+300;await self.engine.step(record);await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(len(self.venue.submitted),1)

    async def test_no_closed_rows_waits_without_advancing_watermark_or_orders(self):
        record=await self.prepare();self.clock=NOW+299;before=record['state']['last_bar']
        future={'time':NOW,'open':100,'high':102,'low':98,'close':100}
        with patch.object(exchange,'history',new=AsyncMock(return_value=[future])):
            await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(record['state']['last_bar'],before)
        self.assertEqual(self.engine.buffers[record['id']],[]);self.assertEqual(self.venue.submitted,[])
        self.assertIn('Waiting',record['state']['status'])

    async def test_warmup_wait_does_not_stop_or_change_owned_position(self):
        record=await self.prepare();self.clock=NOW+300
        fresh.FreshFlipTests.own(self,record,-1)
        before=dict(record['state']['position'])
        rows=[{'time':NOW-300*i,'open':100,'high':102,'low':98,'close':100} for i in range(3)]
        with patch.object(exchange,'history',new=AsyncMock(return_value=rows)):
            await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(record['state']['position'],before)
        self.assertTrue(record['state']['owned']);self.assertEqual(self.venue.submitted,[])

    async def test_fetch_delay_does_not_make_an_expired_flip_eligible(self):
        record=await self.prepare();self.clock=NOW+299
        rows=[{'time':NOW-300*(19-i),'open':100,'high':102,'low':98,'close':100,'atr':1,'supertrend':99,'direction':1,'signal':1 if i==19 else 0} for i in range(20)]
        async def history(*args):self.clock=NOW+421;return rows
        with patch.object(exchange,'history',new=history),patch('backend.engine.supertrend',side_effect=lambda candles,*args:sorted(candles,key=lambda row:row['time'])):
            await self.engine.step(record)
        self.assertTrue(record['enabled']);self.assertEqual(self.venue.submitted,[])
        self.assertIn('Late candle',record['state']['status'])

class HistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_end_includes_only_fully_closed_bars(self):
        rows=[{'time':NOW,'open':100,'high':102,'low':98,'close':100},{'time':NOW+300,'open':100,'high':102,'low':98,'close':100}]
        with patch.object(exchange,'request',new=AsyncMock(return_value=rows)),patch('backend.delta_client.time.time',return_value=NOW+900):
            result=await exchange.history('BTCUSD','5m',NOW,NOW+300)
        self.assertEqual([row['time'] for row in result],[NOW])
    async def test_future_end_never_includes_still_open_bars(self):
        rows=[{'time':NOW-300,'open':100,'high':102,'low':98,'close':100},{'time':NOW,'open':100,'high':102,'low':98,'close':100}]
        with patch.object(exchange,'request',new=AsyncMock(return_value=rows)),patch('backend.delta_client.time.time',return_value=NOW+299):
            result=await exchange.history('BTCUSD','5m',NOW-300,NOW+900)
        self.assertEqual([row['time'] for row in result],[NOW-300])
