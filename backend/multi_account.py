"""Account-isolated execution lanes reuse the tested single-account engine."""
import asyncio,json,time
from backend.engine import Engine,InstanceConflict,utc_now
from backend.brokers import ExecutionSelection,WORK,active

class AccountEngine(Engine):
    def __init__(self,owner,account_id):
        self.owner=owner;self.account_id=account_id;self.db=owner.db
        self.lock=asyncio.Lock();self.buffers={}
        self.log=lambda message,level='INFO':owner.log(f'Client {self.client_name()}: {message}',level)
    def client_name(self):
        return next((a['client_name'] for a in self.owner.brokers.accounts() if a['id']==self.account_id),self.account_id)
    def records(self):
        with self.db() as c:
            rows=c.execute('SELECT i.*,e.enabled AS lane_enabled,e.state AS lane_state FROM broker_executions e JOIN strategy_instances i ON i.id=e.instance_id WHERE e.account_id=? AND i.id NOT IN (SELECT instance_id FROM strategy_instance_deletions)',(self.account_id,)).fetchall()
        result=[]
        for row in rows:
            r=dict(row);r.update(config=json.loads(r['config']),state=json.loads(r.pop('lane_state')),enabled=bool(r.pop('lane_enabled')),account_id=self.account_id,client_name=self.client_name());result.append(r)
        return result
    def persist(self,record):
        with self.db() as c:c.execute('UPDATE broker_executions SET enabled=?,state=?,updated_at=? WHERE instance_id=? AND account_id=?',(int(record['enabled']),json.dumps(record['state']),utc_now(),record['id'],self.account_id))
        self.owner.aggregate(record['id'])

class MultiAccountEngine(Engine):
    def __init__(self,db,log,brokers):
        super().__init__(db,log)
        self.brokers=brokers;self.workers={}
        with db() as c:
            if 'execution' not in [r['name'] for r in c.execute('PRAGMA table_info(strategy_instances)')]:c.execute("ALTER TABLE strategy_instances ADD COLUMN execution TEXT NOT NULL DEFAULT '{\"mode\":\"primary\",\"account_ids\":[]}'")
            c.execute("CREATE TABLE IF NOT EXISTS broker_executions(instance_id TEXT NOT NULL,account_id TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL,PRIMARY KEY(instance_id,account_id))")
            if not c.execute("SELECT 1 FROM engine_migrations WHERE name='broker_lanes_v1'").fetchone():
                if any(a['id']=='legacy-delta-testnet' for a in brokers.accounts()):
                    for row in c.execute('SELECT id,state FROM strategy_instances').fetchall():c.execute('INSERT OR IGNORE INTO broker_executions VALUES (?,?,0,?,?)',(row['id'],'legacy-delta-testnet',row['state'],utc_now()))
                c.execute("INSERT INTO engine_migrations VALUES ('broker_lanes_v1')")
            for row in c.execute('SELECT instance_id,account_id,state FROM broker_executions WHERE enabled=1').fetchall():
                state=json.loads(row['state']);state.update(last_bar=None,status='Stopped after server restart; protection preserved')
                c.execute('UPDATE broker_executions SET enabled=0,state=? WHERE instance_id=? AND account_id=?',(json.dumps(state),row['instance_id'],row['account_id']))
        for record in super().records():self.aggregate(record['id'])
    def worker(self,account_id):
        if account_id not in self.workers:self.workers[account_id]=AccountEngine(self,account_id)
        return self.workers[account_id]
    def lanes(self,identifier):
        with self.db() as c:rows=c.execute('SELECT account_id,enabled,state FROM broker_executions WHERE instance_id=? ORDER BY account_id',(identifier,)).fetchall()
        names={a['id']:a['client_name'] for a in self.brokers.accounts()}
        return [{'account_id':r['account_id'],'client_name':names.get(r['account_id'],'Unavailable account'),'enabled':bool(r['enabled']),'state':json.loads(r['state'])} for r in rows]
    def aggregate(self,identifier):
        lanes=self.lanes(identifier)
        if not lanes:return
        with self.db() as c:
            row=c.execute('SELECT state FROM strategy_instances WHERE id=?',(identifier,)).fetchone()
            if not row:return
            state=json.loads(row['state'])
            for k in WORK:state[k]=any(l['state'].get(k) for l in lanes)
            state['position']=lanes[0]['state'].get('position') if len(lanes)==1 else None
            state['error']='; '.join(f'{l["client_name"]}: {l["state"]["error"]}' for l in lanes if l['state'].get('error')) or None
            running=sum(l['enabled'] for l in lanes)
            state['status']=(f'{running}/{len(lanes)} accounts running · fresh flips only' if running else 'Stopped · '+('Exit pending' if state.get('exit_requested') else 'No new entries'))
            if state['error']:state['status']='Account attention required · '+state['error']
            for k in ('direction','supertrend','atr','candle_time','checked_at'):state[k]=lanes[0]['state'].get(k)
            if all(l['state'].get('exited_at') for l in lanes):state['exited_at']=lanes[0]['state']['exited_at'];state['status']='Exited · all account positions flat and tracked orders cleared'
            else:state.pop('exited_at',None)
            c.execute('UPDATE strategy_instances SET enabled=?,state=?,updated_at=? WHERE id=?',(int(running>0),json.dumps(state),utc_now(),identifier))
    def records(self):
        records=super().records()
        for record in records:
            record['execution']=json.loads(record['execution']);record['executions']=self.lanes(record['id'])
            try:record['execution_accounts']=[a['id'] for a in self.brokers.resolve(record['execution'])]
            except ValueError:record['execution_accounts']=[]
        return records
    def editable(self,record):return not active(record) and all(not active(l) for l in self.lanes(record['id']))
    async def create_instance(self,config,name='',request_id=None,execution=None):
        selection=execution or ExecutionSelection()
        if not isinstance(selection,ExecutionSelection):selection=ExecutionSelection(**selection)
        async with self.lock:
            if self.brokers.accounts() or selection.mode=='selected':self.brokers.resolve(selection)
            previous=self.record(str(request_id)) if request_id else None
            if previous and previous['execution']!=selection.model_dump():raise ValueError('This create request already used different execution accounts')
            record=await self._create_instance(config,name,request_id)
            with self.db() as c:c.execute('UPDATE strategy_instances SET execution=? WHERE id=?',(selection.model_dump_json(),record['id']))
            return self.record(record['id'])
    async def update_instance(self,identifier,config,name='',execution=None):
        selection=execution or ExecutionSelection()
        if not isinstance(selection,ExecutionSelection):selection=ExecutionSelection(**selection)
        async with self.lock:
            record=self.record(identifier)
            if not record:raise ValueError('Instance not found')
            if self.brokers.accounts() or selection.mode=='selected':self.brokers.resolve(selection)
            if not self.editable(record):raise ValueError('Stop and Exit every account before changing settings or execution accounts')
            await self._update_instance(record,config,name)
            with self.db() as c:
                c.execute('UPDATE strategy_instances SET execution=? WHERE id=?',(selection.model_dump_json(),identifier))
                for lane in self.lanes(identifier):
                    state={key:lane['state'][key] for key in ('day','day_balance','daily_pnl') if key in lane['state']}
                    state['status']='Settings saved; stopped'
                    c.execute('UPDATE broker_executions SET enabled=0,state=? WHERE instance_id=? AND account_id=?',(json.dumps(state),identifier,lane['account_id']))
            self.aggregate(identifier)
            for worker in self.workers.values():worker.buffers.pop(identifier,None)
            return self.record(identifier)
    async def save(self,config):
        previous=self.record(config.symbol)
        return await self.update_instance(previous['id'],config,execution=previous['execution']) if previous else await self.create_instance(config)
    async def control(self,identifier,action):
        action={'pause':'stop','close':'exit'}.get(action,action)
        async with self.lock:
            record=self.record(identifier)
            if not record:raise ValueError('Save an instance first')
            lanes=self.lanes(record['id'])
            if action=='start':
                if record['enabled']:return record
                targets=self.brokers.resolve(record['execution']);wanted={a['id'] for a in targets}
                if any(active(l) and l['account_id'] not in wanted for l in lanes):raise ValueError('Exit existing account execution before changing targets or primary account')
                # Keep account equity baselines across stopped starts; remove only flat deselected lanes.
                with self.db() as c:
                    for lane in lanes:
                        if lane['account_id'] not in wanted:c.execute('DELETE FROM broker_executions WHERE instance_id=? AND account_id=?',(record['id'],lane['account_id']))
                    for account in targets:c.execute('INSERT OR IGNORE INTO broker_executions VALUES (?,?,0,?,?)',(record['id'],account['id'],json.dumps({'status':'Waiting for Start'}),utc_now()))
                started=[]
                try:
                    # No execution loop can run while preflight holds this coordinator lock.
                    for account in targets:
                        with self.brokers.context(account['id']):await self.worker(account['id']).control(record['id'],'start')
                        started.append(account['id'])
                except Exception as exc:
                    for lane in self.lanes(record['id']):
                        lane['enabled']=False;lane['state']['status']='Start cancelled; all accounts remain paused'
                        self.worker(lane['account_id']).persist({**record,**lane})
                    self.log(f'{record["name"]}: account Start preflight failed; all accounts paused','ERROR')
                    raise ValueError(f'{account["client_name"]}: {exc}' if isinstance(exc,ValueError) else f'{account["client_name"]}: Exchange unavailable; all accounts remain paused') from None
            elif action in ('stop','exit'):
                if not lanes:
                    if any(record['state'].get(k) for k in WORK):raise ValueError('Legacy ownership requires its original broker credentials before Exit')
                    record['enabled']=False;record['state']['status']='Stopped; no new entries';self.persist(record)
                for lane in lanes:
                    try:
                        with self.brokers.context(lane['account_id']):await self.worker(lane['account_id']).control(record['id'],action)
                    except Exception as exc:self.worker(lane['account_id']).record_error(self.worker(lane['account_id']).record(record['id']),exc)
            else:raise ValueError('Unknown instance action')
            self.aggregate(record['id']);return self.record(record['id'])
    async def run(self):
        while True:
            for account in self.brokers.accounts():
                worker=self.worker(account['id'])
                for lane in worker.records():
                    if not active(lane):continue
                    async with self.lock:
                        lane=worker.record(lane['id'])
                        if lane is None:continue
                        try:
                            with self.brokers.context(account['id']):await worker.step(lane)
                        except asyncio.CancelledError:raise
                        except Exception as exc:worker.record_error(lane,exc)
            await asyncio.sleep(3)
