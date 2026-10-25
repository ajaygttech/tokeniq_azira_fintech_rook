"""Fresh-flip and request-signature guarantees across client accounts."""
import asyncio,hashlib,hmac,json,os,unittest
from unittest.mock import patch,AsyncMock
import test_brokers as base
from backend import delta_client as exchange
from backend.brokers import ExecutionSelection
from backend.strategy import StrategyConfig

class AccountGuardTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=base.BrokerTests.asyncSetUp
    create=base.BrokerTests.create
    venue=base.BrokerTests.venue
    async def test_one_fresh_flip_per_account_and_no_old_start_signal(self):
        now=1792920000;clock=[now];original=self.request
        async def request(path,params=None,private=False,method='GET',body=None):
            if path.startswith('/v2/tickers/'):return {'close':100,'timestamp':clock[0]*1000000}
            if path.endswith('/orders/leverage'):return {'leverage':1}
            return await original(path,params,private,method,body)
        with patch.object(exchange,'request',new=request),patch('backend.engine.time.time',side_effect=lambda:clock[0]):
            record=await self.create(ExecutionSelection(mode='all'));await self.engine.control(record['id'],'start')
            async def step(opened):
                rows=[{'time':opened-300*(19-i),'open':100,'high':102,'low':98,'close':100,'atr':1,'supertrend':99,'direction':1,'signal':1 if i==19 else 0} for i in range(20)]
                with patch.object(exchange,'history',new=AsyncMock(return_value=rows)),patch('backend.engine.supertrend',return_value=rows):
                    for account in [self.alpha,self.beta]:
                        worker=self.engine.worker(account['id'])
                        with self.store.context(account['id']):await worker.step(worker.record(record['id']))
            await step(now-300)
            self.assertEqual(self.venue(self.alpha).submitted,[]);self.assertEqual(self.venue(self.beta).submitted,[])
            clock[0]+=300;await step(now);await step(now)
            self.assertEqual(len(self.venue(self.alpha).submitted),1);self.assertEqual(len(self.venue(self.beta).submitted),1)
    async def test_edit_preserves_each_account_day_loss_baseline(self):
        record=await self.create(ExecutionSelection(mode='all'));await self.engine.control(record['id'],'start');await self.engine.control(record['id'],'stop')
        for account in [self.alpha,self.beta]:
            worker=self.engine.worker(account['id']);lane=worker.record(record['id']);lane['state'].update(day='2026-10-25',day_balance=1000,daily_pnl=-45);worker.persist(lane)
        await self.engine.update_instance(record['id'],StrategyConfig(quantity=2),'Changed',ExecutionSelection(mode='all'))
        self.assertTrue(all(l['state']['day_balance']==1000 and l['state']['daily_pnl']==-45 for l in self.engine.lanes(record['id'])))

class TransportCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_requests_sign_with_the_selected_client_secret(self):
        captured=[]
        class Response:
            status_code=200
            def json(self):return {'success':True,'result':{}}
        class Client:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def request(self,method,url,headers,content):
                await asyncio.sleep(0)
                captured.append((method,url,dict(headers),content));return Response()
        async def send(key,secret):
            with exchange.account_credentials((key,secret)):await exchange.request('/v2/orders',private=True,method='POST',body={'size':1})
        with patch.dict(os.environ,{'DELTA_ENV':'testnet','DELTA_API_KEY':'fallback','DELTA_API_SECRET':'fallback'}),patch.object(exchange.httpx,'AsyncClient',new=Client),patch('backend.delta_client.time.time',return_value=123456):
            await asyncio.gather(send('client-a','secret-a'),send('client-b','secret-b'))
        self.assertEqual(len(captured),2)
        for method,url,headers,payload in captured:
            secret={'client-a':'secret-a','client-b':'secret-b'}[headers['api-key']]
            self.assertEqual(headers['signature'],hmac.new(secret.encode(),('POST123456/v2/orders'+payload).encode(),hashlib.sha256).hexdigest())
            self.assertTrue(url.startswith(exchange.BASE));self.assertNotIn('fallback',str(headers))
        self.assertIsNone(exchange._credentials.get())
