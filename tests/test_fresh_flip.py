"""No historical/duplicate/expired signal orders; all venue operations mocked."""
import unittest
from unittest.mock import AsyncMock,patch
try:
    import test_instances as base
except ModuleNotFoundError:
    from tests import test_instances as base
from backend import delta_client as exchange

NOW=1792920000
class FreshFlipTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.InstanceTests.asyncSetUp
    create=base.InstanceTests.create
    own=base.InstanceTests.own

    async def prepare(self):
        self.clock=NOW
        self.delay_leverage=False;self.delay_close=False
        async def request(path,params=None,private=False,method='GET',body=None):
            if path.startswith('/v2/tickers/'):
                return {'close':100,'timestamp':self.clock*1000000}
            if path.endswith('/orders/leverage'):
                if self.delay_leverage:self.clock+=121
                return {'leverage':1}
            value=await self.venue.request(path,params,private,method,body)
            if self.delay_close and method=='POST' and body.get('reduce_only'):self.clock+=121
            return value
        p=patch.object(exchange,'request',new=request);p.start();self.addCleanup(p.stop)
        timer=patch('backend.engine.time.time',side_effect=lambda:self.clock);timer.start();self.addCleanup(timer.stop)
        r=await self.create(exit_mode='opposite_signal',resolution='5m',atr_period=3)
        await self.engine.control(r['id'],'start')
        r=self.engine.record(r['id']);r['state']['last_bar']=NOW-300;self.engine.persist(r)
        return r

    async def step(self,r,opened=NOW,signal=1):
        rows=[{'time':opened-300*(19-i),'open':100,'high':102,'low':98,'close':100,
               'atr':1,'supertrend':99,'direction':1 if signal>=0 else -1,'signal':signal if i==19 else 0} for i in range(20)]
        with patch.object(exchange,'history',new=AsyncMock(return_value=rows)),patch('backend.engine.supertrend',side_effect=lambda candles,*args:sorted(candles,key=lambda row:row['time'])):
            await self.engine.step(r)

    async def test_fresh_confirmed_flip_submits_exactly_once(self):
        r=await self.prepare();self.clock=NOW+300
        await self.step(r);await self.step(r)
        self.assertEqual(len(self.venue.submitted),1)
        self.assertEqual(self.venue.submitted[0]['side'],'buy')
        self.assertFalse(self.venue.submitted[0]['reduce_only'])

    async def test_start_and_resume_baseline_an_existing_flip_without_trading(self):
        r=await self.prepare();r['state']['last_bar']=None;self.clock=NOW+300
        await self.step(r);self.assertEqual(self.venue.submitted,[])
        await self.engine.control(r['id'],'stop');await self.engine.control(r['id'],'start')
        r=self.engine.record(r['id']);await self.step(r)
        self.assertEqual(self.venue.submitted,[])

    async def test_old_signal_before_start_is_rejected_even_with_a_stale_watermark(self):
        r=await self.prepare();r['state']['started_at']=NOW+301;self.clock=NOW+310
        await self.step(r)
        self.assertEqual(self.venue.submitted,[])
        self.assertIn('predates Start',r['state']['status'])

    async def test_missed_and_late_flips_never_replay(self):
        r=await self.prepare();r['state']['last_bar']=NOW-600;self.clock=NOW+300
        await self.step(r);self.assertIn('Missed candles',r['state']['status'])
        r['state']['last_bar']=NOW-300;self.clock=NOW+421
        await self.step(r);self.assertIn('Late candle',r['state']['status'])
        self.assertEqual(self.venue.submitted,[])

    async def test_unclosed_candle_cannot_advance_the_watermark_or_place_an_order(self):
        r=await self.prepare();self.clock=NOW+300;r['state']['last_bar']=NOW;before=r['state']['last_bar']
        await self.step(r,opened=NOW+300)
        self.assertTrue(r['enabled'])
        self.assertTrue(all(row['time']+300<=self.clock for row in self.engine.buffers[r['id']]))
        self.assertEqual(r['state']['last_bar'],before);self.assertEqual(self.venue.submitted,[])

    async def test_slow_margin_checks_cannot_turn_a_fresh_flip_into_a_late_entry(self):
        r=await self.prepare();self.clock=NOW+300;self.delay_leverage=True
        await self.step(r)
        self.assertEqual(self.venue.submitted,[])
        self.assertIn('window expired',r['state']['status'])
        self.assertEqual(r['state']['last_bar'],NOW)
        await self.step(r);self.assertEqual(self.venue.submitted,[])

    async def test_slow_position_read_prevents_an_expired_signal_from_closing_a_position(self):
        r=await self.prepare();self.own(r,-1);self.clock=NOW+300
        original=self.engine.position;calls=0
        async def position(product):
            nonlocal calls
            calls+=1
            value=await original(product)
            if calls==2:self.clock+=121
            return value
        with patch.object(self.engine,'position',new=position):await self.step(r)
        self.assertEqual(self.venue.submitted,[]);self.assertEqual(self.venue.positions[84],-1)
        self.assertIn('window expired',r['state']['status'])

    async def test_reversal_does_not_open_a_new_leg_if_the_signal_expires_during_close(self):
        r=await self.prepare();self.own(r,-1);self.clock=NOW+300;self.delay_close=True
        await self.step(r)
        self.assertEqual(len(self.venue.submitted),1)
        self.assertTrue(self.venue.submitted[0]['reduce_only']);self.assertEqual(self.venue.positions[84],0)
        self.assertIn('window expired',r['state']['status'])

    async def test_explicit_exit_is_not_blocked_by_signal_age(self):
        r=await self.prepare();self.own(r,-1);self.clock=NOW+10000
        result=await self.engine.control(r['id'],'exit')
        self.assertFalse(result['state']['exit_requested']);self.assertEqual(self.venue.positions[84],0)
        self.assertTrue(all(order['reduce_only'] for order in self.venue.submitted))
