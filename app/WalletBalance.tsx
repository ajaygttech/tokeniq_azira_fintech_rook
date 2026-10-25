'use client';
import { useState } from 'react';
import { Wallet, ChevronDown, X } from 'lucide-react';
type Balance = {account_id?:string;client_name?:string;asset_symbol?:string;asset_id?:number;balance?:string;available_balance?:string;blocked_margin?:string;balance_inr?:string;available_balance_inr?:string};
function amount(value:unknown,symbol:string){
  if(value===undefined||value===null||value===''||!Number.isFinite(Number(value)))return '—';
  const prefix=symbol==='USD'?'$':symbol==='INR'?'₹':'';
  return prefix+Number(value).toLocaleString('en-IN',{minimumFractionDigits:2,maximumFractionDigits:['USD','INR','REF_USD'].includes(symbol)?2:8});
}
export function WalletBalance({balances,error,stale}:{balances:Balance[];error?:string;stale?:boolean}){
  const [selected,setSelected]=useState('USD'),[expanded,setExpanded]=useState(false);
  const grouped=Array.from(new Set(balances.map(b=>b.asset_symbol))).map(symbol=>{
    const rows=balances.filter(b=>b.asset_symbol===symbol);const merged:Balance={asset_symbol:symbol};
    for(const key of ['balance','available_balance','blocked_margin','balance_inr','available_balance_inr'] as const){
      const values=rows.map(row=>row[key]);
      merged[key]=values.every(value=>value!==undefined&&value!==null&&value!==''&&Number.isFinite(Number(value)))?String(values.reduce((sum,value)=>sum+Number(value),0)):undefined;
    }return merged;
  });
  const wallet=grouped.find(b=>b.asset_symbol===selected)||grouped.find(b=>b.asset_symbol==='USD')||grouped[0];
  const symbol=wallet?.asset_symbol||'USD';
  const unavailable=!!error||!wallet;
  const nonzero=balances.filter(b=>Number(b.balance)!==0&&Number.isFinite(Number(b.balance))).length;
  return <div className="stat wallet-stat"><div className="wallet-card-head"><span><Wallet size={15}/> Wallet balance</span>{wallet&&<select aria-label="Wallet currency" value={symbol} onChange={e=>setSelected(e.target.value)}>{grouped.map(b=><option key={b.asset_symbol||b.asset_id} value={b.asset_symbol}>{b.asset_symbol}</option>)}</select>}</div><h2 data-testid="wallet-balance">{unavailable?'—':amount(wallet.balance,symbol)}{!['USD','INR'].includes(symbol)&&<small> {symbol}</small>}</h2><div className="wallet-breakdown"><span>Available <b data-testid="wallet-available">{unavailable?'—':amount(wallet.available_balance,symbol)}</b></span><span>Blocked <b>{unavailable?'—':amount(wallet.blocked_margin,symbol)}</b></span></div><div className="wallet-card-bottom"><small>{stale?'Last balance · connection interrupted':unavailable?'Wallet unavailable':symbol!=='INR'&&wallet.balance_inr!==undefined?`${amount(wallet.balance_inr,'INR')} · exchange value`:nonzero?'Native currency balance':'No funds in this wallet'}</small><button className="text-button" onClick={()=>setExpanded(true)} disabled={!balances.length}>Details <ChevronDown size={11}/></button></div>{expanded&&<div className="wallet-overlay" role="presentation" onClick={()=>setExpanded(false)}><section className="panel wallet-dialog" role="dialog" aria-modal="true" aria-label="Wallet balances by asset" onClick={e=>e.stopPropagation()} onKeyDown={e=>{if(e.key==='Escape')setExpanded(false)}}><div className="panel-head"><div><h3><Wallet size={17}/> Wallet balances</h3><p>Exchange-reported amounts · currencies are kept separate</p></div><button autoFocus className="icon-button" aria-label="Close wallet details" onClick={()=>setExpanded(false)}><X size={16}/></button></div><div className="table-scroll"><table><thead><tr><th>Client</th><th>Asset</th><th>Total balance</th><th>Available</th><th>Blocked margin</th><th>INR value</th></tr></thead><tbody>{balances.map(b=>{const s=b.asset_symbol||String(b.asset_id);return <tr key={(b.account_id||'')+s}><td>{b.client_name||'—'}</td><td><b>{s}</b>{s==='REF_USD'&&<small className="referral-label">Referral wallet</small>}</td><td>{amount(b.balance,s)}</td><td>{amount(b.available_balance,s)}</td><td>{amount(b.blocked_margin,s)}</td><td>{s==='INR'?amount(b.balance,'INR'):amount(b.balance_inr,'INR')}</td></tr>})}</tbody></table></div><div className="panel-foot">{error|| (stale?'These are the last received balances. Reconnect to get current amounts.':'Balances refresh from your Delta account every 5 seconds. Account totals are combined per currency; client balances are shown separately above.')}</div></section></div>}</div>
}
