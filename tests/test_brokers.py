"""Broker identity, credential routing and independent execution ownership."""
import asyncio,json,os,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from backend.brokers import BrokerStore,AccountInput,ExecutionSelection
from backend.multi_account import MultiAccountEngine
from backend.engine import Engine
from backend.strategy import StrategyConfig
from backend import delta_client as exchange
from test_instances import Venue,PRODUCTS

class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'state.db'
        def db():
            c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;return c
        self.db=db
        self.keys={'alpha-key-123':('101',Venue()),'beta-key-123':('202',Venue()),'same-user-key':('101',Venue())}
        self.seen=[]
        async def request(path,params=None,private=False,method='GET',body=None):
            credentials=exchange._credentials.get();self.assertIsNotNone(credentials)
            key=credentials[0];self.seen.append((key,path,method))
            identity,venue=self.keys[key]
            if path=='/v2/users/trading_preferences':return {'user_id':identity}
            return await venue.request(path,params,private,method,body)
        self.request=request
        p=patch.object(exchange,'request',new=request);p.start();self.addCleanup(p.stop)
        p=patch.object(exchange,'product',new=Venue().product);p.start();self.addCleanup(p.stop)
        with patch.dict(os.environ,{'DELTA_API_KEY':'','DELTA_API_SECRET':''}):self.store=BrokerStore(db,Path(self.temp.name)/'.key')
        self.alpha=await self.store.add(AccountInput(client_name='Alpha',api_key='alpha-key-123',api_secret='alpha-secret-123'))
        self.beta=await self.store.add(AccountInput(client_name='Beta',api_key='beta-key-123',api_secret='beta-secret-123'))
        self.engine=MultiAccountEngine(db,lambda *args:None,self.store)
    async def create(self,execution=None):return await self.engine.create_instance(StrategyConfig(exit_mode='opposite_signal'),'Instance',str(uuid.uuid4()),execution)
    def venue(self,account):return self.keys[self.store.credentials(account['id'])[0]][1]
    async def test_first_primary_single_primary_after_switch_and_no_credentials_public(self):
        self.assertEqual([a['id'] for a in self.store.accounts() if a['is_primary']],[self.alpha['id']])
        self.store.primary(self.beta['id'])
        self.assertEqual([a['id'] for a in self.store.accounts() if a['is_primary']],[self.beta['id']])
        public=json.dumps(self.store.accounts());self.assertNotIn('secret',public);self.assertNotIn('alpha-key',public)
        with self.db() as c:raw=c.execute('SELECT credentials FROM broker_accounts LIMIT 1').fetchone()[0]
        self.assertNotIn('secret',raw);self.assertEqual((Path(self.temp.name)/'.key').stat().st_mode&0o777,0o600)
    async def test_duplicate_same_exchange_user_with_different_key_rejected(self):
        with self.assertRaisesRegex(ValueError,'already connected'):await self.store.add(AccountInput(client_name='Duplicate',api_key='same-user-key',api_secret='another-secret'))
        self.assertEqual(len(self.store.accounts()),2)
    async def test_primary_and_all_resolve_and_selected_fail_closed(self):
        self.assertEqual(len(self.store.resolve(ExecutionSelection())),1)
        self.assertEqual(len(self.store.resolve(ExecutionSelection(mode='all'))),2)
        self.assertEqual(self.store.resolve(ExecutionSelection(mode='selected',account_ids=[self.beta['id']]))[0]['id'],self.beta['id'])
        with self.assertRaises(ValueError):self.store.resolve(ExecutionSelection(mode='selected',account_ids=['unknown']))
    async def test_primary_start_pinned_primary_switch_does_not_move_ownership(self):
        r=await self.create();await self.engine.control(r['id'],'start')
        self.store.primary(self.beta['id']);r=self.engine.record(r['id'])
        self.assertEqual([l['account_id'] for l in r['executions']],[self.alpha['id']]);self.assertTrue(r['enabled'])
        await self.engine.control(r['id'],'stop');self.assertFalse(self.engine.record(r['id'])['enabled'])
    async def test_all_start_and_independent_order_exit_tracking(self):
        r=await self.create(ExecutionSelection(mode='all'));await self.engine.control(r['id'],'start')
        for a in [self.alpha,self.beta]:
            worker=self.engine.worker(a['id']);lane=worker.record(r['id'])
            with self.store.context(a['id']):await worker.submit(lane,PRODUCTS['BTCUSD'],'buy',1)
        self.assertEqual(self.venue(self.alpha).positions[84],1);self.assertEqual(self.venue(self.beta).positions[84],1)
        r=self.engine.record(r['id']);self.assertEqual(len(r['executions']),2);self.assertTrue(r['state']['owned'])
        await self.engine.control(r['id'],'exit')
        self.assertEqual(self.venue(self.alpha).positions[84],0);self.assertEqual(self.venue(self.beta).positions[84],0)
        self.assertFalse(self.engine.record(r['id'])['state']['owned'])
        self.assertNotEqual(self.venue(self.alpha).submitted[0]['client_order_id'],self.venue(self.beta).submitted[0]['client_order_id'])
    async def test_preflight_failure_pauses_all_without_orders(self):
        r=await self.create(ExecutionSelection(mode='all'));self.venue(self.beta).positions[84]=3
        with self.assertRaisesRegex(ValueError,'Beta'):await self.engine.control(r['id'],'start')
        self.assertTrue(all(not l['enabled'] for l in self.engine.lanes(r['id'])))
        self.assertEqual(self.venue(self.alpha).submitted,[])
    async def test_same_symbol_disjoint_accounts_allowed_overlapping_rejected(self):
        a=await self.create(ExecutionSelection(mode='selected',account_ids=[self.alpha['id']]))
        b=await self.create(ExecutionSelection(mode='selected',account_ids=[self.beta['id']]))
        await self.engine.control(a['id'],'start');await self.engine.control(b['id'],'start')
        c=await self.create(ExecutionSelection())
        with self.assertRaisesRegex(ValueError,'already controls'):await self.engine.control(c['id'],'start')
        self.assertTrue(self.engine.record(a['id'])['enabled']);self.assertTrue(self.engine.record(b['id'])['enabled'])
    async def test_cannot_reroute_or_delete_owned_lane_and_primary_does_not_exit_wrong_account(self):
        r=await self.create();await self.engine.control(r['id'],'start');worker=self.engine.worker(self.alpha['id']);lane=worker.record(r['id'])
        with self.store.context(self.alpha['id']):await worker.submit(lane,PRODUCTS['BTCUSD'],'buy',1)
        await self.engine.control(r['id'],'stop');self.store.primary(self.beta['id'])
        with self.assertRaisesRegex(ValueError,'Exit existing'):await self.engine.control(r['id'],'start')
        with self.assertRaises(ValueError):await self.engine.update_instance(r['id'],StrategyConfig(),'new',ExecutionSelection(mode='all'))
        with self.assertRaises(ValueError):await self.engine.delete_instance(r['id'])
        self.venue(self.beta).positions[84]=7
        await self.engine.control(r['id'],'exit')
        self.assertEqual(self.venue(self.alpha).positions[84],0);self.assertEqual(self.venue(self.beta).positions[84],7)
    async def test_restart_preserves_each_ownership_and_pauses_all(self):
        r=await self.create(ExecutionSelection(mode='all'));await self.engine.control(r['id'],'start')
        worker=self.engine.worker(self.alpha['id']);lane=worker.record(r['id'])
        with self.store.context(self.alpha['id']):await worker.submit(lane,PRODUCTS['BTCUSD'],'buy',1)
        restarted=MultiAccountEngine(self.db,lambda *args:None,self.store);r=restarted.record(r['id'])
        self.assertFalse(r['enabled']);self.assertTrue(r['state']['owned']);self.assertTrue(all(not l['enabled'] for l in r['executions']))
        self.assertEqual(self.venue(self.alpha).positions[84],1)
    async def test_context_credentials_are_isolated_between_async_requests(self):
        async def one(account):
            with self.store.context(account['id']):
                await asyncio.sleep(.01)
                return exchange._credentials.get()[0]
        self.assertEqual(await asyncio.gather(one(self.alpha),one(self.beta)),['alpha-key-123','beta-key-123']);self.assertIsNone(exchange._credentials.get())
    async def test_create_retry_cannot_change_execution_routing(self):
        r=await self.create()
        with self.assertRaisesRegex(ValueError,'different execution'):await self.engine.create_instance(StrategyConfig(exit_mode='opposite_signal'),'Instance',r['id'],ExecutionSelection(mode='all'))
    async def test_candle_journal_is_separate_for_each_execution_account(self):
        r=await self.create(ExecutionSelection(mode='all'));await self.engine.control(r['id'],'start')
        point={'time':1000,'open':100,'high':105,'low':95,'close':101,'atr':3,'supertrend':98,'direction':1,'signal':1}
        for a in [self.alpha,self.beta]:
            worker=self.engine.worker(a['id']);self.assertTrue(worker.log_candle(worker.record(r['id']),point,'SIGNAL',2000))
            self.assertFalse(worker.log_candle(worker.record(r['id']),point,'SIGNAL',2000))
        with self.db() as c:rows=c.execute('SELECT payload FROM strategy_candle_logs').fetchall()
        self.assertEqual({json.loads(row['payload'])['account_id'] for row in rows},{self.alpha['id'],self.beta['id']})
    async def test_partial_account_exit_failure_does_not_prevent_other_client_exit(self):
        r=await self.create(ExecutionSelection(mode='all'));await self.engine.control(r['id'],'start')
        for a in [self.alpha,self.beta]:
            w=self.engine.worker(a['id'])
            with self.store.context(a['id']):await w.submit(w.record(r['id']),PRODUCTS['BTCUSD'],'buy',1)
        original=self.venue(self.alpha).request
        async def fail_close(path,params=None,private=False,method='GET',body=None):
            if method=='POST':raise TimeoutError('lost close response')
            return await original(path,params,private,method,body)
        with patch.object(self.venue(self.alpha),'request',new=fail_close):await self.engine.control(r['id'],'exit')
        self.assertEqual(self.venue(self.beta).positions[84],0)
        remaining=self.engine.record(r['id']);self.assertTrue(remaining['state']['exit_requested']);self.assertFalse(remaining['enabled'])
        self.assertTrue(next(l for l in remaining['executions'] if l['account_id']==self.alpha['id'])['state']['pending'])
    async def test_legacy_migration_preserves_owned_position_and_pending_protection(self):
        with tempfile.TemporaryDirectory() as temp:
            dbfile=Path(temp)/'legacy.db'
            def db():
                c=sqlite3.connect(dbfile);c.row_factory=sqlite3.Row;return c
            old=Engine(db,lambda *args:None);record=await old.create_instance(StrategyConfig(),'Legacy')
            record['enabled']=True;record['state'].update(owned=True,position={'size':1,'entry':100},protection_ids=[123]);old.persist(record)
            with patch.dict(os.environ,{'DELTA_API_KEY':'alpha-key-123','DELTA_API_SECRET':'alpha-secret-123'}):store=BrokerStore(db,Path(temp)/'.key')
            upgraded=MultiAccountEngine(db,lambda *args:None,store);migrated=upgraded.record(record['id'])
            self.assertFalse(migrated['enabled']);self.assertTrue(migrated['state']['owned']);self.assertEqual(migrated['executions'][0]['state']['protection_ids'],[123]);self.assertEqual(migrated['executions'][0]['account_id'],'legacy-delta-testnet')
            again=MultiAccountEngine(db,lambda *args:None,store);self.assertEqual(len(again.lanes(record['id'])),1)

class BrokerApiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=BrokerTests.asyncSetUp
    async def test_endpoints_attribution_and_validation_do_not_expose_credentials(self):
        import httpx
        from unittest.mock import AsyncMock
        from backend import main
        with patch.object(main,'brokers',self.store),patch.object(main,'engine',self.engine),patch.object(main,'CACHE',{}),patch.object(exchange,'products',new=AsyncMock(return_value=list(PRODUCTS.values()))):
            main.app.dependency_overrides[main.authenticated]=lambda:'admin'
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://testserver') as client:
                    public=await client.get('/api/brokers');self.assertEqual(public.status_code,200);self.assertNotIn('api_secret',public.text);self.assertNotIn('alpha-key',public.text)
                    invalid=await client.post('/api/brokers',json={'client_name':'Client','api_key':'hide','api_secret':'HIDE'})
                    self.assertEqual(invalid.status_code,422);self.assertNotIn('hide',invalid.text);self.assertNotIn('HIDE',invalid.text)
                    created=await client.post('/api/strategy/instances',json={'request_id':str(uuid.uuid4()),'name':'All','config':StrategyConfig().model_dump(),'execution':{'mode':'all','account_ids':[]}})
                    self.assertEqual(created.status_code,200,created.text);self.assertEqual(created.json()['instance']['execution']['mode'],'all')
                    r=await client.get('/api/snapshot');self.assertEqual(r.status_code,200,r.text)
                    self.assertEqual({row['client_name'] for row in r.json()['balances']},{'Alpha','Beta'})
                    self.assertEqual(len({row['account_id'] for row in r.json()['balances']}),2)
            finally:main.app.dependency_overrides.clear()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://testserver') as client:self.assertEqual((await client.get('/api/brokers')).status_code,401)
