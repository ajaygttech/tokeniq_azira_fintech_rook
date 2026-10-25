"""Encrypted Delta Testnet account registry. Public records never contain credentials."""
import hashlib,json,os,sqlite3,time,uuid
from pathlib import Path
from cryptography.fernet import Fernet
from pydantic import BaseModel,ConfigDict,Field,model_validator
from typing import Literal
from backend import delta_client as exchange

class ExecutionSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode:Literal['primary','all','selected']='primary'
    account_ids:list[str]=Field(default_factory=list,max_length=50)
    @model_validator(mode='after')
    def valid_selection(self):
        if self.mode=='selected' and not self.account_ids:raise ValueError('Select at least one broker account')
        if len(set(self.account_ids))!=len(self.account_ids):raise ValueError('Duplicate account selection')
        if self.mode!='selected' and self.account_ids:raise ValueError('Account IDs apply only to selected-account mode')
        return self

class AccountInput(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    client_name:str=Field(min_length=1,max_length=80)
    broker:Literal['delta_testnet']='delta_testnet'
    api_key:str=Field(min_length=8,max_length=256,repr=False)
    api_secret:str=Field(min_length=8,max_length=512,repr=False)

WORK=('owned','pending','exit_requested','protection_ids','protection_pending','needs_protection')
def active(record):return bool(record['enabled'] or any(record['state'].get(k) for k in WORK))

class BrokerStore:
    def __init__(self,db,key_file):
        self.db=db;key_file=Path(key_file)
        # A separate durable random encryption key, never derived from an API credential.
        if not key_file.exists():
            key_file.parent.mkdir(parents=True,exist_ok=True)
            try:
                descriptor=os.open(key_file,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:pass
            else:
                with os.fdopen(descriptor,'wb') as f:f.write(Fernet.generate_key())
        self.cipher=Fernet(key_file.read_bytes())
        with db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS broker_accounts (id TEXT PRIMARY KEY,client_name TEXT NOT NULL,broker TEXT NOT NULL,credentials TEXT NOT NULL,key_hash TEXT NOT NULL UNIQUE,exchange_user_id TEXT UNIQUE,is_primary INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL,verified_at REAL)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS one_primary_broker ON broker_accounts(is_primary) WHERE is_primary=1')
            c.execute('CREATE TABLE IF NOT EXISTS broker_migrations (name TEXT PRIMARY KEY)')
            if not c.execute("SELECT 1 FROM broker_migrations WHERE name='environment_account'").fetchone():
                key,secret=os.environ.get('DELTA_API_KEY',''),os.environ.get('DELTA_API_SECRET','')
                if key and secret:
                    c.execute('INSERT OR IGNORE INTO broker_accounts VALUES (?,?,?,?,?,?,1,?,NULL)',('legacy-delta-testnet','Existing Testnet account','delta_testnet',self.encrypt(key,secret),self.fingerprint(key),None,time.time()))
                c.execute("INSERT INTO broker_migrations VALUES ('environment_account')")
    def fingerprint(self,key):return hashlib.sha256(key.encode()).hexdigest()
    def encrypt(self,key,secret):return self.cipher.encrypt(json.dumps([key,secret]).encode()).decode()
    def accounts(self):
        with self.db() as c:
            return [dict(r) for r in c.execute('SELECT id,client_name,broker,is_primary,created_at,verified_at FROM broker_accounts ORDER BY is_primary DESC,created_at,id')]
    def credentials(self,identifier):
        with self.db() as c:row=c.execute('SELECT credentials FROM broker_accounts WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('Broker account not found')
        return tuple(json.loads(self.cipher.decrypt(row['credentials'].encode())))
    def context(self,identifier):return exchange.account_credentials(self.credentials(identifier))
    async def identity(self,credentials):
        with exchange.account_credentials(credentials):
            details=await exchange.request('/v2/users/trading_preferences',private=True)
            wallet=await exchange.request('/v2/wallet/balances',private=True)
        if not isinstance(details,dict) or not details.get('user_id') or not isinstance(wallet,list):raise ValueError('Unable to verify Delta Testnet account identity')
        return str(details['user_id'])
    async def verify(self,identifier):
        identity=await self.identity(self.credentials(identifier))
        try:
            with self.db() as c:c.execute('UPDATE broker_accounts SET exchange_user_id=?,verified_at=? WHERE id=?',(identity,time.time(),identifier))
        except sqlite3.IntegrityError:raise ValueError('This Delta account is already connected under another client') from None
        return next(a for a in self.accounts() if a['id']==identifier)
    async def add(self,body):
        # Resolve imported account identity too: different keys for one account must not fan out twice.
        with self.db() as c:unverified=[r['id'] for r in c.execute('SELECT id FROM broker_accounts WHERE exchange_user_id IS NULL')]
        for identifier in unverified:await self.verify(identifier)
        identity=await self.identity((body.api_key,body.api_secret))
        identifier=str(uuid.uuid4())
        try:
            with self.db() as c:
                c.execute('BEGIN IMMEDIATE')
                primary=not c.execute('SELECT 1 FROM broker_accounts LIMIT 1').fetchone()
                c.execute('INSERT INTO broker_accounts VALUES (?,?,?,?,?,?,?,?,?)',(identifier,body.client_name,body.broker,self.encrypt(body.api_key,body.api_secret),self.fingerprint(body.api_key),identity,int(primary),time.time(),time.time()))
        except sqlite3.IntegrityError:raise ValueError('This Delta Testnet account or API key is already connected') from None
        return next(a for a in self.accounts() if a['id']==identifier)
    def primary(self,identifier):
        with self.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute('SELECT 1 FROM broker_accounts WHERE id=?',(identifier,)).fetchone():raise ValueError('Broker account not found')
            c.execute('UPDATE broker_accounts SET is_primary=0 WHERE is_primary=1')
            c.execute('UPDATE broker_accounts SET is_primary=1 WHERE id=?',(identifier,))
    def resolve(self,selection):
        selection=selection if isinstance(selection,ExecutionSelection) else ExecutionSelection(**selection)
        accounts=self.accounts()
        if selection.mode=='primary':accounts=[a for a in accounts if a['is_primary']]
        elif selection.mode=='selected':
            wanted=set(selection.account_ids);accounts=[a for a in accounts if a['id'] in wanted]
            if len(accounts)!=len(wanted):raise ValueError('A selected broker account no longer exists')
        if not accounts:raise ValueError('Add a Delta Testnet broker account before starting an instance')
        return accounts
