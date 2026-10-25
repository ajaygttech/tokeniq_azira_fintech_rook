"""Lifecycle regression tests. Every exchange operation is mocked."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.engine import Engine, InstanceConflict
from backend.strategy import StrategyConfig
from backend import delta_client as exchange

PRODUCTS = {
    'BTCUSD': {'id':84,'symbol':'BTCUSD','contract_value':'0.001','tick_size':'0.1','trading_status':'operational'},
    'ETHUSD': {'id':85,'symbol':'ETHUSD','contract_value':'0.01','tick_size':'0.1','trading_status':'operational'},
}

class Venue:
    def __init__(self):
        self.positions = {84:0,85:0}
        self.orders = {}
        self.submitted = []
        self.deleted = []
        self.timeout_after_fill = False
        self.partial_closes = []
        self.cancel_error = False
        self.fill_on_cancel = 0

    async def product(self, symbol):
        return PRODUCTS[symbol]

    async def request(self, path, params=None, private=False, method='GET', body=None):
        if path == '/v2/positions':
            return {'size':self.positions[params['product_id']], 'entry_price':'100'}
        if path == '/v2/wallet/balances':
            return [{'asset_symbol':'USD','balance':'10000','available_balance':'10000'}]
        if path == '/v2/positions/margined':
            return []
        if path.startswith('/v2/orders/client_order_id/'):
            key = path.rsplit('/',1)[-1]
            order = next((row for row in self.orders.values() if row.get('client_order_id')==key), None)
            if order is None:
                raise exchange.ExchangeError(404,'order_not_found')
            return dict(order)
        if path == '/v2/orders' and method == 'GET':
            return [dict(row) for row in self.orders.values() if row['state'] in ('open','pending') and row['product_id']==int(params['product_ids'])]
        if path == '/v2/orders' and method == 'DELETE':
            if self.cancel_error:
                raise TimeoutError('simulated cancellation timeout')
            row = self.orders[body['id']]
            self.deleted.append(row['id'])
            if self.fill_on_cancel and not row.get('bracket_order'):
                fill = self.fill_on_cancel
                row['unfilled_size'] -= fill
                self.positions[row['product_id']] += fill if row['side']=='buy' else -fill
                self.fill_on_cancel = 0
            row['state'] = 'cancelled'
            return dict(row)
        if path == '/v2/orders' and method == 'POST':
            self.submitted.append(dict(body))
            size = body['size']
            filled = min(size,self.partial_closes.pop(0)) if body['reduce_only'] and self.partial_closes else size
            change = filled if body['side']=='buy' else -filled
            self.positions[body['product_id']] += change
            order = {**body,'id':100+len(self.submitted),'state':'closed','unfilled_size':size-filled,'average_fill_price':'100'}
            self.orders[order['id']] = order
            if self.timeout_after_fill:
                self.timeout_after_fill = False
                raise TimeoutError('simulated accepted order, lost response')
            return dict(order)
        raise AssertionError(f'Unexpected exchange operation: {method} {path}')

class InstanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'instances.db'
        def db():
            connection = sqlite3.connect(self.path)
            connection.row_factory = sqlite3.Row
            return connection
        self.db = db
        self.engine = Engine(db,lambda *args:None)
        self.venue = Venue()
        product_patch = patch.object(exchange,'product',new=self.venue.product)
        request_patch = patch.object(exchange,'request',new=self.venue.request)
        product_patch.start(); request_patch.start()
        self.addCleanup(product_patch.stop); self.addCleanup(request_patch.stop)

    async def create(self,symbol='BTCUSD',name='My instance',**config):
        return await self.engine.create_instance(StrategyConfig(symbol=symbol,**config),name)

    def own(self,record,size=1,protection_ids=None):
        record['state'].update(owned=True,position={'size':size,'entry':'100'},protection_ids=protection_ids or [])
        self.venue.positions[PRODUCTS[record['symbol']]['id']] = size
        self.engine.persist(record)

    async def test_create_is_stopped_and_sends_no_orders(self):
        first = await self.create()
        second = await self.create('ETHUSD','ETH instance',resolution='15m')
        self.assertNotEqual(first['id'],second['id'])
        self.assertTrue(all(not row['enabled'] and row['lifecycle']=='stopped' for row in self.engine.records()))
        self.assertEqual(self.venue.submitted,[])
        self.assertEqual(second['config']['resolution'],'15m')

    async def test_create_request_id_is_idempotent_and_rejects_changed_payload(self):
        key = str(uuid.uuid4())
        config = StrategyConfig()
        a,b = await asyncio.gather(self.engine.create_instance(config,'BTC',key),self.engine.create_instance(config,'BTC',key))
        self.assertEqual(a['id'],b['id']); self.assertEqual(len(self.engine.records()),1)
        with self.assertRaisesRegex(ValueError,'different settings'):
            await self.engine.create_instance(StrategyConfig(resolution='15m'),'BTC',key)

    async def test_multiple_drafts_same_symbol_are_separate_and_legacy_selector_rejects_ambiguity(self):
        a = await self.create(name='BTC fast')
        b = await self.create(name='BTC slow',resolution='15m')
        self.assertNotEqual(a['id'],b['id'])
        with self.assertRaisesRegex(ValueError,'by ID'):
            self.engine.record('BTCUSD')
        await self.engine.control(a['id'],'start')
        with self.assertRaisesRegex(ValueError,'Another instance'):
            await self.engine.control(b['id'],'start')
        self.assertEqual(self.venue.submitted,[])

    async def test_different_instruments_start_independently_and_duplicate_start_keeps_baseline(self):
        a,b = await self.create(),await self.create('ETHUSD','ETH')
        await asyncio.gather(self.engine.control(a['id'],'start'),self.engine.control(b['id'],'start'))
        before = self.engine.record(a['id'])
        after = await self.engine.control(a['id'],'start')
        self.assertTrue(after['enabled']); self.assertTrue(self.engine.record(b['id'])['enabled'])
        self.assertEqual(before['state']['started_at'],after['state']['started_at'])
        self.assertEqual(self.venue.submitted,[])

    async def test_stop_works_without_exchange_and_preserves_owned_position(self):
        a = await self.create()
        self.own(a,2,[501]); a['enabled']=True; self.engine.persist(a)
        with patch.object(exchange,'product',new=AsyncMock(side_effect=TimeoutError)),patch.object(exchange,'request',new=AsyncMock(side_effect=TimeoutError)) as request:
            stopped = await self.engine.control(a['id'],'stop')
            request.assert_not_called()
        self.assertFalse(stopped['enabled']); self.assertTrue(stopped['state']['owned'])
        self.assertEqual(stopped['state']['protection_ids'],[501]); self.assertEqual(self.venue.positions[84],2)

    async def test_exit_closes_only_selected_instance_and_its_tracked_orders(self):
        a,b = await self.create(),await self.create('ETHUSD','ETH')
        self.own(a,2,[501]); self.own(b,-3,[502])
        b['enabled']=True; self.engine.persist(b)
        for oid,pid in [(501,84),(502,85),(999,84)]:
            self.venue.orders[oid]={'id':oid,'product_id':pid,'state':'pending','bracket_order':True}
        exited = await self.engine.control(a['id'],'exit')
        self.assertEqual(exited['lifecycle'],'exited'); self.assertFalse(exited['state']['owned'])
        self.assertEqual(self.venue.positions,{84:0,85:-3})
        self.assertTrue(self.engine.record(b['id'])['enabled'])
        self.assertEqual(self.venue.deleted,[501]); self.assertEqual(self.venue.orders[999]['state'],'pending')
        self.assertEqual(len(self.venue.submitted),1)
        self.assertTrue(self.venue.submitted[0]['reduce_only'])
        await self.engine.control(a['id'],'exit')
        self.assertEqual(len(self.venue.submitted),1)

    async def test_inactive_instance_exit_never_closes_another_same_symbol_position(self):
        a,b = await self.create(name='owner'),await self.create(name='inactive')
        self.own(a,4)
        exited = await self.engine.control(b['id'],'exit')
        self.assertEqual(exited['lifecycle'],'exited'); self.assertEqual(self.venue.positions[84],4)
        self.assertEqual(self.venue.submitted,[])

    async def test_external_position_change_blocks_close_and_keeps_exit_pending(self):
        a = await self.create(); self.own(a,1); self.venue.positions[84]=-1
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exiting'); self.assertIn('externally',result['state']['error'])
        self.assertEqual(self.venue.submitted,[])
        with self.assertRaisesRegex(ValueError,'pending'):
            await self.engine.control(a['id'],'start')

    async def test_unknown_entry_is_reconciled_then_closed_without_duplicate_entry(self):
        a = await self.create(); self.venue.timeout_after_fill=True
        with self.assertRaises(TimeoutError):
            await self.engine.submit(a,PRODUCTS['BTCUSD'],'buy',1)
        self.assertTrue(self.engine.record(a['id'])['state']['pending'])
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exited'); self.assertEqual(self.venue.positions[84],0)
        self.assertEqual([row['reduce_only'] for row in self.venue.submitted],[False,True])

    async def test_exit_cancels_pending_entry_and_closes_any_racing_partial_fill(self):
        a = await self.create(quantity=2)
        a['state']['pending']={'client_order_id':'original','side':'buy','size':2,'reduce_only':False}
        self.engine.persist(a)
        self.venue.orders[77]={'id':77,'product_id':84,'state':'open','client_order_id':'original','side':'buy','size':2,'unfilled_size':2}
        self.venue.fill_on_cancel=1
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exited'); self.assertEqual(self.venue.deleted,[77])
        self.assertEqual(self.venue.positions[84],0)
        self.assertEqual([(row['size'],row['reduce_only']) for row in self.venue.submitted],[(1,True)])

    async def test_partial_close_keeps_exiting_and_retries_only_remaining_quantity(self):
        a = await self.create(quantity=3); self.own(a,3)
        self.venue.partial_closes=[1]
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exiting'); self.assertEqual(result['state']['position']['size'],2)
        await self.engine.step(self.engine.record(a['id']))
        self.assertEqual(self.engine.record(a['id'])['lifecycle'],'exited')
        self.assertEqual([row['size'] for row in self.venue.submitted],[3,2]); self.assertEqual(self.venue.positions[84],0)

    async def test_close_timeout_reconciles_before_retrying_and_does_not_duplicate_close(self):
        a = await self.create(); self.own(a,1); self.venue.timeout_after_fill=True
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exiting'); self.assertTrue(result['state']['pending'])
        await self.engine.step(self.engine.record(a['id']))
        self.assertEqual(self.engine.record(a['id'])['lifecycle'],'exited')
        self.assertEqual(len(self.venue.submitted),1)

    async def test_cancel_timeout_keeps_exit_pending_until_protection_is_confirmed_gone(self):
        a = await self.create(); self.own(a,1,[501])
        self.venue.orders[501]={'id':501,'product_id':84,'state':'pending','bracket_order':True}
        self.venue.cancel_error=True
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exiting'); self.assertEqual(self.venue.positions[84],0)
        self.venue.cancel_error=False
        await self.engine.step(self.engine.record(a['id']))
        self.assertEqual(self.engine.record(a['id'])['lifecycle'],'exited'); self.assertEqual(len(self.venue.submitted),1)

    async def test_unresolved_entry_remains_pending_across_restart_and_never_reposts(self):
        a = await self.create(); a['enabled']=True
        a['state']['pending']={'client_order_id':'missing','side':'buy','size':1,'reduce_only':False}
        self.engine.persist(a)
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(result['lifecycle'],'exiting')
        restarted = Engine(self.db,lambda *args:None)
        record = restarted.record(a['id'])
        self.assertFalse(record['enabled']); self.assertTrue(record['state']['exit_requested']); self.assertTrue(record['state']['pending'])
        with self.assertRaises(exchange.ExchangeError):await restarted.step(record)
        self.assertEqual(self.venue.submitted,[])

    async def test_unknown_bracket_creation_does_not_claim_exit_is_complete(self):
        a = await self.create(); self.own(a,1)
        a['state']['protection_pending']={'before':[],'expected':2}; self.engine.persist(a)
        result = await self.engine.control(a['id'],'exit')
        self.assertEqual(self.venue.positions[84],0); self.assertEqual(result['lifecycle'],'exiting')
        self.assertIn('Bracket creation',result['state']['error'])

    async def test_interrupted_entry_protection_attempts_safe_closure_on_failure(self):
        a = await self.create(); self.own(a,1)
        a['state']['needs_protection']=True; self.engine.persist(a)
        with patch.object(self.engine,'protect',new=AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):await self.engine.step(a)
        self.assertEqual(self.venue.positions[84],0); self.assertEqual(self.engine.record(a['id'])['lifecycle'],'exited')

    async def test_edit_preserves_id_and_baseline_and_cannot_mutate_active_instance(self):
        a = await self.create(); a['state'].update(day='2026-10-25',day_balance=1000); self.engine.persist(a)
        changed = await self.engine.update_instance(a['id'],StrategyConfig(resolution='15m'),'Renamed')
        self.assertEqual(changed['id'],a['id']); self.assertFalse(changed['enabled'])
        self.assertEqual(changed['state']['day_balance'],1000)
        self.own(changed,1)
        with self.assertRaisesRegex(ValueError,'Pause and close'):
            await self.engine.update_instance(a['id'],StrategyConfig(),'bad')

    async def test_start_baselines_a_current_flip_without_placing_an_entry(self):
        import time
        a = await self.create()
        await self.engine.control(a['id'], 'start')
        now = int(time.time())
        rows = [{'time':now-300*(15-index),'open':100,'high':102,'low':98,'close':100,
                 'direction':1,'signal':1,'supertrend':99,'atr':1} for index in range(15)]
        with patch.object(exchange,'history',new=AsyncMock(return_value=rows)),patch('backend.engine.supertrend',return_value=rows):
            await self.engine.step(self.engine.record(a['id']))
        saved = self.engine.record(a['id'])
        self.assertTrue(saved['enabled']); self.assertEqual(saved['state']['last_bar'],rows[-1]['time'])
        self.assertEqual(self.venue.submitted,[])

    async def test_definite_bracket_rejection_closes_without_leaving_an_unknown_bracket(self):
        a = await self.create(sl_points=5,target_points=10); self.own(a,1)
        a['state']['needs_protection']=True; self.engine.persist(a)
        async def request(path,params=None,private=False,method='GET',body=None):
            if path=='/v2/orders/bracket':
                raise exchange.ExchangeError(400,'invalid_stop_price')
            return await self.venue.request(path,params,private,method,body)
        with patch.object(exchange,'request',new=request):
            with self.assertRaises(exchange.ExchangeError):
                await self.engine.step(a)
        saved = self.engine.record(a['id'])
        self.assertFalse(saved['state']['owned']); self.assertFalse(saved['state']['exit_requested'])
        self.assertIsNone(saved['state']['protection_pending']); self.assertEqual(self.venue.positions[84],0)

    async def test_delete_stopped_instance_preserves_other_instances_and_never_calls_exchange(self):
        a = await self.create(name='Delete me')
        b = await self.create('ETHUSD', 'Keep me')
        self.engine.buffers[a['id']] = ['cached bars']
        with patch.object(exchange,'product',new=AsyncMock()) as product,patch.object(exchange,'request',new=AsyncMock()) as request:
            deleted = await self.engine.delete_instance(a['id'])
            product.assert_not_called();request.assert_not_called()
        self.assertEqual(deleted,a['id']);self.assertIsNone(self.engine.record(a['id']))
        self.assertEqual([row['id'] for row in self.engine.records()],[b['id']])
        self.assertNotIn(a['id'],self.engine.buffers)

    async def test_running_instance_cannot_be_deleted(self):
        a = await self.create()
        await self.engine.control(a['id'],'start')
        with self.assertRaisesRegex(InstanceConflict,'running instance'):
            await self.engine.delete_instance(a['id'])
        self.assertTrue(self.engine.record(a['id'])['enabled'])
        self.assertEqual(self.venue.submitted,[])

    async def test_stopped_instances_with_exposure_or_unresolved_work_cannot_be_deleted(self):
        for key,value in [('owned',True),('pending',{'client_order_id':'uncertain'}),('exit_requested',True),('protection_ids',[501]),('protection_pending',{'expected':2}),('needs_protection',True)]:
            with self.subTest(key=key):
                a = await self.create(name=key)
                a['state'][key]=value;self.engine.persist(a)
                with self.assertRaisesRegex(InstanceConflict,'position, tracked orders'):
                    await self.engine.delete_instance(a['id'])
                self.assertIsNotNone(self.engine.record(a['id']))
        self.assertEqual(self.venue.submitted,[])

    async def test_exit_then_delete_does_not_place_additional_orders(self):
        a = await self.create();self.own(a,1)
        await self.engine.control(a['id'],'exit')
        self.assertEqual(len(self.venue.submitted),1)
        await self.engine.delete_instance(a['id'])
        self.assertEqual(len(self.venue.submitted),1);self.assertIsNone(self.engine.record(a['id']))

    async def test_deleted_instance_stays_deleted_after_restart_and_create_retry(self):
        a = await self.create()
        await self.engine.delete_instance(a['id'])
        restarted=Engine(self.db,lambda *args:None)
        self.assertEqual(restarted.records(),[])
        with self.assertRaisesRegex(ValueError,'was deleted'):
            await restarted.create_instance(StrategyConfig(),a['name'],a['id'])
        with self.db() as connection:
            self.assertIsNotNone(connection.execute('SELECT deleted_at FROM strategy_instance_deletions WHERE instance_id=?',(a['id'],)).fetchone())
        self.assertEqual(self.venue.submitted,[])

    async def test_start_delete_race_cannot_delete_running_execution(self):
        a = await self.create()
        results=await asyncio.gather(self.engine.control(a['id'],'start'),self.engine.delete_instance(a['id']),return_exceptions=True)
        record=self.engine.record(a['id'])
        if record:
            self.assertTrue(record['enabled']);self.assertIsInstance(results[1],InstanceConflict)
        else:
            self.assertIsInstance(results[0],ValueError)
        self.assertEqual(self.venue.submitted,[])

    async def test_worker_skips_an_instance_deleted_while_waiting_for_its_lock(self):
        a=await self.create();a['enabled']=True
        with patch.object(self.engine,'records',return_value=[a]),patch.object(self.engine,'record',return_value=None),patch.object(self.engine,'step',new=AsyncMock()) as step,patch('backend.engine.asyncio.sleep',new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.engine.run()
            step.assert_not_called()

class MigrationTests(unittest.TestCase):
    def test_migration_preserves_settings_ownership_pending_intents_and_is_once_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'legacy.db'
            def db():
                connection=sqlite3.connect(path);connection.row_factory=sqlite3.Row;return connection
            state={'owned':True,'position':{'size':2},'pending':{'client_order_id':'old'},'protection_ids':[123]}
            with db() as connection:
                connection.execute('CREATE TABLE strategies(symbol TEXT PRIMARY KEY,config TEXT,enabled INTEGER,state TEXT)')
                connection.execute('INSERT INTO strategies VALUES(?,?,?,?)',('BTCUSD',StrategyConfig(quantity=2).model_dump_json(),1,json.dumps(state)))
            engine=Engine(db,lambda *args:None)
            record=engine.records()[0]
            self.assertFalse(record['enabled']);self.assertEqual(record['state'],state)
            self.assertEqual(record['config']['quantity'],2)
            again=Engine(db,lambda *args:None)
            self.assertEqual(len(again.records()),1);self.assertEqual(again.records()[0]['id'],record['id'])

class InstanceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from backend import main
        self.main=main
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'api.db'
        def db():
            connection=sqlite3.connect(self.path);connection.row_factory=sqlite3.Row;return connection
        self.engine=Engine(db,lambda *args:None)
        self.venue=Venue()
        for target,name,value in [(main,'engine',self.engine),(exchange,'product',self.venue.product),(exchange,'request',self.venue.request)]:
            item=patch.object(target,name,new=value);item.start();self.addCleanup(item.stop)
        main.app.dependency_overrides[main.authenticated]=lambda:'admin'
        self.addCleanup(main.app.dependency_overrides.clear)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='https://testserver')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def create(self,name='API instance'):
        response=await self.client.post('/api/strategy/instances',json={'name':name,'config':StrategyConfig().model_dump(),'request_id':str(uuid.uuid4())})
        self.assertEqual(response.status_code,200,response.text)
        return response.json()['instance']

    async def test_create_list_update_and_control_use_instance_ids(self):
        record=await self.create()
        self.assertFalse(record['enabled'])
        response=await self.client.get('/api/strategy/instances')
        self.assertEqual(response.json()['instances'][0]['id'],record['id'])
        response=await self.client.put('/api/strategy/instances/'+record['id'],json={'name':'Renamed','config':StrategyConfig(resolution='15m').model_dump()})
        self.assertEqual(response.status_code,200);self.assertEqual(response.json()['instance']['config']['resolution'],'15m')
        url='/api/strategy/instances/'+record['id']+'/control'
        self.assertTrue((await self.client.post(url,json={'action':'start'})).json()['instance']['enabled'])
        self.assertFalse((await self.client.post(url,json={'action':'stop'})).json()['instance']['enabled'])
        self.assertEqual((await self.client.post(url,json={'action':'exit'})).json()['instance']['lifecycle'],'exited')
        self.assertEqual(self.venue.submitted,[])

    async def test_pending_exit_returns_202_and_remains_observable(self):
        record=await self.create()
        record['state'].update(owned=True,position={'size':1,'entry':'100'})
        self.venue.positions[84]=1;self.engine.persist(record)
        self.venue.timeout_after_fill=True
        response=await self.client.post('/api/strategy/instances/'+record['id']+'/control',json={'action':'exit'})
        self.assertEqual(response.status_code,202)
        self.assertTrue(response.json()['instance']['state']['exit_requested'])
        response=await self.client.get('/api/strategy/instances')
        self.assertEqual(response.json()['instances'][0]['lifecycle'],'exiting')
        await self.engine.step(self.engine.record(record['id']))
        self.assertEqual(self.engine.record(record['id'])['lifecycle'],'exited')

    async def test_retried_api_create_does_not_add_another_instance(self):
        payload={'name':'Retry','config':StrategyConfig().model_dump(),'request_id':str(uuid.uuid4())}
        a,b=await asyncio.gather(self.client.post('/api/strategy/instances',json=payload),self.client.post('/api/strategy/instances',json=payload))
        self.assertEqual(a.json()['instance']['id'],b.json()['instance']['id'])
        self.assertEqual(len(self.engine.records()),1)

    async def test_unauthenticated_and_invalid_controls_never_reach_exchange(self):
        self.main.app.dependency_overrides.clear()
        self.assertEqual((await self.client.get('/api/strategy/instances')).status_code,401)
        self.main.app.dependency_overrides[self.main.authenticated]=lambda:'admin'
        unknown=str(uuid.uuid4())
        self.assertEqual((await self.client.post('/api/strategy/instances/'+unknown+'/control',json={'action':'start'})).status_code,404)
        self.assertEqual((await self.client.post('/api/strategy/instances/'+unknown+'/control',json={'action':'delete_all_positions'})).status_code,422)
        self.assertEqual((await self.client.post('/api/strategy/instances/BTCUSD/control',json={'action':'start'})).status_code,422)
        self.assertEqual(self.venue.submitted,[])

    async def test_delete_api_removes_only_stopped_instance_and_returns_404_on_repeat(self):
        a=await self.create('Delete me');b=await self.create('Keep me')
        url='/api/strategy/instances/'+a['id']
        response=await self.client.delete(url)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deleted_id'],a['id'])
        rows=(await self.client.get('/api/strategy/instances')).json()['instances']
        self.assertEqual([row['id'] for row in rows],[b['id']])
        self.assertEqual((await self.client.delete(url)).status_code,404)
        self.assertEqual(self.venue.submitted,[])

    async def test_delete_api_rejects_running_or_exposed_instance_with_409(self):
        a=await self.create()
        url='/api/strategy/instances/'+a['id']
        await self.client.post(url+'/control',json={'action':'start'})
        response=await self.client.delete(url)
        self.assertEqual(response.status_code,409);self.assertIn('running instance',response.json()['detail'])
        await self.client.post(url+'/control',json={'action':'stop'})
        row=self.engine.record(a['id']);row['state']['owned']=True;self.engine.persist(row)
        response=await self.client.delete(url)
        self.assertEqual(response.status_code,409);self.assertIn('Use Exit',response.json()['detail'])
        self.assertIsNotNone(self.engine.record(a['id']))

    async def test_delete_api_requires_login_and_checks_csrf_origin(self):
        import time
        a=await self.create()
        url='/api/strategy/instances/'+a['id']
        self.main.app.dependency_overrides.clear()
        self.assertEqual((await self.client.delete(url)).status_code,401)
        secret='instance-delete-test-secret'
        payload=f'admin:{int(time.time())+3600}:test'
        with patch.object(self.main,'SESSION_SECRET',secret):
            self.client.cookies.set('tokeniq_session',payload+'.'+self.main.sign(secret,payload))
            response=await self.client.delete(url,headers={'origin':'https://evil.invalid'})
            self.assertEqual(response.status_code,403)
        self.assertIsNotNone(self.engine.record(a['id']))

    async def test_delete_api_does_not_allow_replaying_an_old_create_request(self):
        a=await self.create()
        await self.client.delete('/api/strategy/instances/'+a['id'])
        response=await self.client.post('/api/strategy/instances',json={'name':a['name'],'config':a['config'],'request_id':a['id']})
        self.assertEqual(response.status_code,400);self.assertIn('was deleted',response.json()['detail'])
        self.assertEqual(self.engine.records(),[])
