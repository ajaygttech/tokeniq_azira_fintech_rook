import {markPosition} from './positions';
type Row=Record<string,any>;
export type InstanceMarket={positions:Row[];products:Row[];marks:Record<string,{price:number;time:number}>;available:boolean;snapshotTime:number};
export function instanceMtm(record:Row,market?:InstanceMarket,now=Date.now()):{value:number|null;source:string;size:number|null;entry:number|null;mark:number|null} {
 const unavailable={value:null as number|null,source:'Unavailable',size:null as number|null,entry:null as number|null,mark:null as number|null};
 if(record.executions?.length){
  const values=record.executions.map((lane:Row)=>instanceMtm({...record,executions:undefined,account_id:lane.account_id,state:lane.state},market,now));
  if(values.length===1)return values[0];
  if(values.some((value:ReturnType<typeof instanceMtm>)=>value.value===null))return unavailable;
  const held=values.filter((value:ReturnType<typeof instanceMtm>)=>value.source!=='Flat');
  if(!held.length)return {...unavailable,value:0,source:'Flat',size:0};
  return {...unavailable,value:values.reduce((sum:number,value:ReturnType<typeof instanceMtm>)=>sum+(value.value||0),0),size:values.reduce((sum:number,value:ReturnType<typeof instanceMtm>)=>sum+Math.abs(value.size||0),0),source:held.every((value:ReturnType<typeof instanceMtm>)=>value.source==='WebSocket mark price')?'WebSocket mark price':'Exchange snapshot'};
 }
 const state=record.state||{};
 if(!state.owned)return state.pending?unavailable:{...unavailable,value:0,source:'Flat',size:0};
 // Position ownership/size must be confirmed independently of the public price.
 if(!market?.available||!Number.isFinite(market.snapshotTime)||now-market.snapshotTime>=15000||market.snapshotTime>now+5000)return unavailable;
 const product=market.products.find(p=>p.symbol===record.symbol);
 if(!product||product.notional_type!=='vanilla')return unavailable;
 const expected=Number(state.position?.size);
 const position=market.positions.find(p=>String(p.product_id)===String(product.id)&&(!record.account_id||p.account_id===record.account_id));
 if(!Number.isFinite(expected)||!expected||!position||Number(position.size)!==expected)return unavailable;
 const valued=markPosition(position,market.products,market.marks,now);
 const raw=valued.unrealized_pnl;
 if(raw===null||raw===undefined||raw===''||!Number.isFinite(Number(raw)))return unavailable;
 const entry=Number(position.entry_price),mark=Number(valued.mark_price);
 return {value:Number(raw),source:valued.pnl_source as string,size:expected,
  entry:Number.isFinite(entry)&&entry>0?entry:null,mark:Number.isFinite(mark)&&mark>0?mark:null};
}
