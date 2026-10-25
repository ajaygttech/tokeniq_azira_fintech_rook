"""Regression cases for Testnet chart parity, source isolation and range boundaries."""
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
import httpx
# Importing this module provisions the test-only SQLite database before main.
try:
    import test_backend as base
except ModuleNotFoundError:
    from tests import test_backend as base
PRODUCT = base.PRODUCT
from backend import main
from backend import delta_client as exchange
from backend.strategy import supertrend
from backend.backtest import Backtest, run_backtest, validate_history

FIXTURE = json.loads((Path(__file__).parent/'fixtures/delta_btcusd_5m.json').read_text())
ROWS = FIXTURE['candles']
START, END = 1792866600, 1792913700


def config(**kwargs):
    return Backtest(start='2026-10-25T00:00',end='2026-10-25T13:05',timezone='Asia/Kolkata',
                    exit_mode='opposite_signal',fee_bps=0,slippage_bps=0,**kwargs)


class ChartParityTests(unittest.TestCase):
    def test_captured_testnet_candle_and_supertrend_values(self):
        points = {row['time']:row for row in supertrend(ROWS,10,3)}
        candle = points[1792881000]  # 25 Oct, 04:00 IST, captured from Delta Testnet.
        self.assertEqual([candle[k] for k in ('open','high','low','close')], [80489,80516,80489,80516])
        self.assertEqual(round(candle['supertrend'],1),80343.5)
        self.assertEqual(candle['signal'],1)
        self.assertEqual(round(points[1792913400]['supertrend'],1),80667.9)
        self.assertEqual(points[1792913400]['direction'],-1)

    def test_signal_candle_is_distinct_from_following_open_fill(self):
        report = run_backtest(ROWS,config(),trade_start=START,trade_end=END)
        trade = next(t for t in report['trades'] if t['entry_signal_time']==1792881000)
        self.assertEqual(trade['entry_time'],1792881300)  # 04:05 IST
        self.assertEqual(trade['entry'],80516.5)
        self.assertEqual(trade['side'],'LONG')
        self.assertEqual(trade['exit_time_precision'],'open')
        self.assertEqual(trade['time'],trade['exit_signal_time']+300)

    def test_end_boundary_excludes_candle_opening_at_end(self):
        end = 1792886400  # 05:30 IST: the old report included this entire candle.
        report = run_backtest(ROWS,config(),trade_start=START,trade_end=end)
        self.assertEqual(report['indicator'][-1]['time'],end-300)
        self.assertTrue(all(row['time']<=end for row in report['equity']))
        final = report['trades'][-1]
        self.assertEqual(final['reason'],'end_of_test')
        self.assertEqual(final['time'],end)
        self.assertEqual(final['exit_bar_time'],end-300)
        self.assertEqual(final['exit_time_precision'],'close')
        self.assertEqual(final['exit'],next(r['close'] for r in ROWS if r['time']==end-300))

    def test_intrabar_stop_is_labeled_as_a_candle_not_an_exact_fill(self):
        c = Backtest(**{**config().model_dump(),'exit_mode':'both','sl_points':1,'target_points':1})
        report = run_backtest(ROWS,c,trade_start=START,trade_end=END)
        exits = [t for t in report['trades'] if t['reason'] in ('stop_loss','target')]
        self.assertTrue(exits)
        self.assertTrue(all(t['exit_time_precision']=='intrabar' and t['time']==t['exit_bar_time'] for t in exits))

    def test_ranges_interpret_ist_and_preserve_explicit_iso_offsets(self):
        c = config()
        expected = (1792866600,1792913700)
        self.assertEqual(c.time_bounds(END+86400),expected)
        c.start='2026-10-24T18:30:00Z'; c.end='2026-10-25T07:35:00+00:00'
        self.assertEqual(c.time_bounds(END+86400),expected)
        c.start='2026-10-25T00:00:00+05:30';c.end='2026-10-25T13:05:00+05:30'
        self.assertEqual(c.time_bounds(END+86400),expected)
        c = Backtest(start='2026-10-24',end='2026-10-25')
        self.assertEqual(c.time_bounds(END+86400),(1792800000,1792886400))

    def test_missing_or_truncated_bars_are_rejected_before_simulation(self):
        c=config()
        clean,selected,count=validate_history(ROWS,c,START,END)
        self.assertTrue(selected); self.assertGreaterEqual(count,10)
        for rows in ([r for r in ROWS if r['time']!=1792881000],ROWS[:-2],ROWS[500:]):
            with self.assertRaises(ValueError):validate_history(rows,c,START,END)
        with self.assertRaisesRegex(ValueError,'No complete candles'):
            validate_history(ROWS,c,END+3600,END+7200)
        with self.assertRaises(ValueError):
            c.start='invalid';c.time_bounds(END)


class QuantityDiagnosticsTests(unittest.TestCase):
    def run_qty(self, quantity, limit=1000, capital=10000, daily=100000):
        cfg=Backtest(**{**config().model_dump(),'quantity':quantity,'max_notional':limit,'capital':capital,'daily_loss_limit':daily})
        return run_backtest(ROWS,cfg,.001,.1,START,END)

    def test_100_contracts_reports_the_1000_dollar_cap_instead_of_missing_signals(self):
        result=self.run_qty(100)
        self.assertEqual(result['trades'],[])
        diagnostic=result['diagnostics']
        self.assertGreater(diagnostic['signals'],0)
        self.assertEqual(diagnostic['blocked_entries'],diagnostic['entry_candidates'])
        self.assertEqual(diagnostic['blocked_by_reason']['max_position_value'],diagnostic['blocked_entries'])
        self.assertEqual(diagnostic['blocked_by_reason']['insufficient_capital'],0)
        self.assertGreater(diagnostic['min_blocked_notional'],8000)
        for entry in diagnostic['examples']:
            self.assertEqual(entry['reasons'],['max_position_value'])
            self.assertEqual(entry['max_notional'],1000)
            self.assertGreater(entry['required_notional'],entry['max_notional'])
        self.assertEqual(result['metrics']['fees'],0)

    def test_quantity_scales_pnl_when_explicit_limit_and_capital_allow_it(self):
        small=self.run_qty(1,limit=10000)
        large=self.run_qty(100,limit=10000)
        self.assertGreater(len(large['trades']),0)
        self.assertEqual(large['diagnostics']['blocked_entries'],0)
        self.assertEqual(len(small['trades']),len(large['trades']))
        for one,hundred in zip(small['trades'],large['trades']):
            self.assertEqual(one['entry_time'],hundred['entry_time'])
            self.assertEqual(one['reason'],hundred['reason'])
            self.assertAlmostEqual(hundred['pnl'],one['pnl']*100,places=5)

    def test_capital_and_daily_limit_blockers_are_distinguished(self):
        capital=self.run_qty(100,limit=10000,capital=1000)
        self.assertGreater(capital['diagnostics']['blocked_by_reason']['insufficient_capital'],0)
        self.assertEqual(capital['diagnostics']['blocked_by_reason']['max_position_value'],0)
        halted=self.run_qty(100,limit=10000,daily=.000001)
        self.assertGreater(halted['diagnostics']['blocked_by_reason']['daily_loss_limit'],0)

    def test_no_signals_is_not_mislabeled_as_a_risk_block(self):
        cfg=Backtest(**{**config().model_dump(),'quantity':100})
        rows=[{'time':i*300,'open':100,'high':101,'low':99,'close':100} for i in range(30)]
        result=run_backtest(rows,cfg)
        self.assertEqual(result['diagnostics']['signals'],0)
        self.assertEqual(result['diagnostics']['blocked_entries'],0)


class TestnetDataTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_transport_is_always_public_testnet_get(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(200,json={'success':True,'result':ROWS})
        with patch.object(exchange.httpx,'AsyncHTTPTransport',return_value=httpx.MockTransport(handler)):
            rows=await exchange.history('BTCUSD','5m',START,END)
        self.assertTrue(rows)
        for request in seen:
            self.assertEqual(request.method,'GET')
            self.assertEqual(request.url.host,'cdn-ind.testnet.deltaex.org')
            self.assertEqual(request.url.path,'/v2/history/candles')
            self.assertFalse({'api-key','signature','timestamp','authorization'}.intersection(request.headers))


class BacktestSourceAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = base.AuthTests.asyncSetUp
    asyncTearDown = base.AuthTests.asyncTearDown
    login = base.AuthTests.login
    async def test_source_range_provenance_persists_and_execution_transport_is_untouched(self):
        await self.login()
        body=config().model_dump()
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)) as product,patch.object(exchange,'history',new=AsyncMock(return_value=ROWS)) as history,patch.object(exchange,'request',new=AsyncMock()) as signed:
            response=await self.client.post('/api/backtest',json=body)
            self.assertEqual(response.status_code,200,response.text)
            report=response.json()
            self.assertEqual(report['engine_version'],2)
            self.assertEqual(report['data']['source'],'testnet')
            self.assertEqual(report['data']['timezone'],'Asia/Kolkata')
            self.assertEqual(report['data']['requested_start'],1792866600)
            self.assertEqual(report['data']['requested_end'],1792913700)
            self.assertLess(report['data']['last_candle'],1792913700)
            self.assertEqual(report['data']['closed_through'],1792913700)
            product.assert_awaited_once_with('BTCUSD')
            history.assert_awaited_once_with('BTCUSD','5m',1792866600-500*300,1792913700)
            signed.assert_not_awaited()
            runs=(await self.client.get('/api/runs')).json()
            self.assertTrue(any(run['id']==report['id'] and run['data']['source']=='testnet' for run in runs))
            csv=(await self.client.get('/api/export/backtest',params={'run_id':report['id']})).text
            for name in ('entry_signal_time','entry_time','exit_time_precision','data_source'):
                self.assertIn(name,csv)

    async def test_invalid_source_timezone_or_missing_data_does_not_create_a_run(self):
        await self.login()
        body=config().model_dump()
        before=len((await self.client.get('/api/runs')).json())
        with patch.object(exchange,'product',new=AsyncMock(return_value=PRODUCT)) as product,patch.object(exchange,'history',new=AsyncMock(return_value=ROWS[:-10])):
            for field,value in [('data_source','india'),('timezone','Europe/Berlin')]:
                response=await self.client.post('/api/backtest',json={**body,field:value})
                self.assertEqual(response.status_code,422)
                product.assert_not_awaited()
            response=await self.client.post('/api/backtest',json=body)
            self.assertEqual(response.status_code,400)
        self.assertEqual(len((await self.client.get('/api/runs')).json()),before)

