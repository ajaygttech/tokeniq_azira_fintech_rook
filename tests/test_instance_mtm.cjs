const {test}=require('node:test');const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),ts=require('typescript');
const cache={};
function load(name){
 if(cache[name])return cache[name];
 const output=ts.transpileModule(fs.readFileSync(path.join(__dirname,'../app',name+'.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
 const mod={exports:{}};new Function('exports','module','require',output)(mod.exports,mod,spec=>spec.startsWith('./')?load(spec.slice(2)):require(spec));
 return cache[name]=mod.exports;
}
const {instanceMtm}=load('instanceMtm');
const now=1792918000000;
const record={symbol:'BTCUSD',state:{owned:true,position:{size:100,entry:80000}}};
const market={available:true,snapshotTime:now-1000,products:[{id:84,symbol:'BTCUSD',notional_type:'vanilla',contract_value:'.001'},{id:85,symbol:'ETHUSD',notional_type:'vanilla',contract_value:'.01'}],positions:[{product_id:84,size:100,entry_price:80000,unrealized_pnl:7,mark_price:80070},{product_id:85,size:100,entry_price:100,unrealized_pnl:999}],marks:{BTCUSD:{price:80100,time:now-50},ETHUSD:{price:500,time:now-50}}};
test('instance MTM uses signed owned contracts, instrument units and WebSocket mark price',()=>{
 const long=instanceMtm(record,market,now);assert.equal(long.value,10);assert.equal(long.source,'WebSocket mark price');
 const short=instanceMtm({...record,state:{owned:true,position:{size:-100}}},{...market,positions:[{...market.positions[0],size:-100}]},now);
 assert.equal(short.value,-10);
 const eth=instanceMtm({symbol:'ETHUSD',state:{owned:true,position:{size:100}}},market,now);assert.equal(eth.value,400);
});
test('unowned instances stay flat even if account has a position on the same symbol',()=>{
 assert.equal(instanceMtm({symbol:'BTCUSD',state:{}},market,now).value,0);
 assert.equal(instanceMtm({symbol:'BTCUSD',state:{pending:{}}},market,now).value,null);
});
test('stale, disconnected or invalid mark data falls back to a fresh exchange snapshot',()=>{
 for(const marks of [{},{BTCUSD:{price:80100,time:now-15000}},{BTCUSD:{price:NaN,time:now}},{BTCUSD:{price:80100,time:now+5001}},{BTCUSD:{price:80100,time:NaN}}]){
  const result=instanceMtm(record,{...market,marks},now);assert.equal(result.value,7);assert.equal(result.source,'Exchange snapshot');
 }
});
test('unconfirmed ownership, changed size and stale/unavailable snapshots hide MTM instead of fabricating zero',()=>{
 for(const overrides of [{available:false},{snapshotTime:now-15000},{snapshotTime:NaN},{snapshotTime:now+5001},{positions:[]},{positions:[{...market.positions[0],size:99}]},{products:[]},{products:[{...market.products[0],notional_type:'inverse'}]}])
  assert.equal(instanceMtm(record,{...market,...overrides},now).value,null);
});
test('missing snapshot P&L stays unavailable and a genuine zero remains valid',()=>{
 for(const raw of [null,undefined,'',NaN])assert.equal(instanceMtm(record,{...market,marks:{},positions:[{...market.positions[0],unrealized_pnl:raw}]},now).value,null);
 assert.equal(instanceMtm(record,{...market,marks:{},positions:[{...market.positions[0],unrealized_pnl:0}]},now).value,0);
});

test('multiple clients on the same instrument use their own entry/size and sum verified WebSocket MTM',()=>{
 const lanes=[{account_id:'alpha',state:{owned:true,position:{size:100}}},{account_id:'beta',state:{owned:true,position:{size:-50}}}];
 const positions=[{...market.positions[0],account_id:'beta',size:-50,entry_price:80050},{...market.positions[0],account_id:'alpha',size:100,entry_price:80000}];
 const result=instanceMtm({...record,executions:lanes},{...market,positions},now);
 assert.equal(result.value,7.5);assert.equal(result.source,'WebSocket mark price');
 assert.equal(instanceMtm({...record,executions:lanes},{...market,positions:positions.slice(1)},now).value,null);
});
