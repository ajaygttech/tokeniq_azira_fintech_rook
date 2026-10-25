import asyncio,json,unittest
from contextlib import nullcontext
from unittest.mock import patch,AsyncMock
import test_backend
from backend import main
from backend.read_cache import ReadCache
from backend.strategy import StrategyConfig
class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_markets_share_private_reads_and_load_market_in_parallel(self):
        class Brokers:
            def accounts(self):return [{'id':'client-a','client_name':'Alpha'}]
            def context(self,identifier):return nullcontext()
        calls=[];all_started=asyncio.Event()
        async def delta(path,params=None,private=False):
            calls.append((path,private))
            if len(calls)==8:all_started.set()
            await asyncio.wait_for(all_started.wait(),1)
            return [] if private else {'close':100}
        products=[{**test_backend.PRODUCT,'symbol':s,'id':i} for i,s in enumerate(['BTCUSD','ETHUSD'])]
        with patch.object(main,'brokers',Brokers()),patch.object(main,'DASHBOARD_READS',ReadCache()),patch.object(main,'delta',new=delta),patch.object(main.exchange,'products',new=AsyncMock(return_value=products)):
            results=await asyncio.gather(main.snapshot('BTCUSD','admin'),main.snapshot('ETHUSD','admin'))
            self.assertEqual(sum(private for _,private in calls),4);self.assertEqual(len(calls),8)
            self.assertEqual([r['symbol'] for r in results],['BTCUSD','ETHUSD']);self.assertTrue(all(not r['errors'] for r in results))
    async def test_one_failed_section_does_not_hide_other_sections(self):
        class Brokers:
            def accounts(self):return [{'id':'client-b','client_name':'Beta'}]
            def context(self,identifier):return nullcontext()
        async def delta(path,params=None,private=False):
            if path=='/v2/wallet/balances':raise asyncio.TimeoutError()
            if path=='/v2/positions/margined':return [{'product_id':84,'size':1}]
            return [] if private else {'close':100}
        with patch.object(main,'brokers',Brokers()),patch.object(main,'DASHBOARD_READS',ReadCache()),patch.object(main,'delta',new=delta),patch.object(main.exchange,'products',new=AsyncMock(return_value=[test_backend.PRODUCT])):
            result=await main.snapshot('BTCUSD','admin');self.assertIn('balances',result['errors']);self.assertNotIn('positions',result['errors']);self.assertEqual(result['positions'][0]['client_name'],'Beta')
    async def test_archive_summaries_exclude_heavy_data_and_single_report_retains_it(self):
        config=StrategyConfig().model_dump();report={'metrics':{'net_pnl':12,'return_pct':1},'data':{'label':'Delta Testnet'},'source':'Testnet','equity':[{'time':1,'equity':100}], 'trades':[{'pnl':12}],'indicator':[{'close':10}]}
        with main.db() as c:identifier=c.execute('INSERT INTO runs(time,config,result) VALUES (?,?,?)',('2026-10-25',json.dumps(config),json.dumps(report))).lastrowid
        rows=await main.runs(summary=True,user='admin');row=next(r for r in rows if r['id']==identifier)
        self.assertNotIn('equity',row);self.assertNotIn('trades',row);self.assertNotIn('indicator',row)
        full=await main.run_report(identifier,'admin');self.assertEqual(full['trades'],report['trades']);self.assertEqual(full['equity'],report['equity'])
