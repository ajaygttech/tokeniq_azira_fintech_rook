'use client';
import {useCallback,useEffect,useRef,useState,useId} from 'react';
import {Activity,Play,Pause,Settings2,X,Download,Loader2,Save,ArrowRight,Plus,Square,Trash2,FlaskConical} from 'lucide-react';
import {Area,AreaChart,CartesianGrid,ResponsiveContainer,Tooltip,XAxis,YAxis} from 'recharts';
import {backtestRange,defaultBacktestPeriod,fitBacktestRange,latestBacktestEnd,backtestInputTime,parseBacktestTime} from './backtestPeriod';
import {formatNumber as money,formatDate as date} from './format';
import {requestJson} from './request';
import {downloadBacktestReport} from './backtestExport';
import {instanceMtm,type InstanceMarket} from './instanceMtm';

type Row=Record<string,any>;
export const TIMEFRAMES=['1m','3m','5m','15m','30m','1h','2h','4h','6h','1d','1w'];
const EXIT_MODES=[
 {value:'sl_tp',label:'SL / Target points only',description:'Exit at your stop loss or target. Ignore opposite signals while a position is open.'},
 {value:'opposite_signal',label:'Opposite signal only',description:'Close and reverse on an opposite Supertrend signal. SL and target points are ignored.'},
 {value:'both',label:'SL / Target + opposite signal',description:'Use point-based SL/target and close-and-reverse on an opposite signal.'},
];
const exitLabel=(mode:string)=>EXIT_MODES.find(option=>option.value===mode)?.label||mode;
const defaults={symbol:'BTCUSD',resolution:'5m',atr_period:10,multiplier:3,quantity:1,exit_mode:'both',sl_points:100,target_points:200,max_notional:1000,daily_loss_limit:100};
const fields=[{key:'atr_period',label:'ATR period',min:2,step:1},{key:'multiplier',label:'ATR multiplier',min:.1,step:.1},{key:'quantity',label:'Quantity (contracts)',min:1,step:1},{key:'sl_points',label:'Stop loss (points)',min:0,step:'any'},{key:'target_points',label:'Target (points)',min:0,step:'any'},{key:'max_notional',label:'Max position value (USD)',min:.01,step:'any'},{key:'daily_loss_limit',label:'Daily account loss limit (USD)',min:.01,step:'any'}];
const api=requestJson;
const hasWork=(record:Row)=>!!(record.enabled||record.state.owned||record.state.pending||record.state.exit_requested||record.state.protection_ids?.length||record.state.protection_pending||record.state.needs_protection);

export function StrategyWorkspace({mode,visible,drawer,onClose,onUpdate,overview=false,onCreated,onManage,highlightId,resetDraftKey=0,draftInstance,market,report,onBacktest}:{
 mode:'forward'|'backtest';visible:boolean;drawer:boolean;onClose:()=>void;onUpdate:()=>void;
 onBacktest?:()=>void;report?:Row|null;overview?:boolean;onCreated?:(id:string)=>void;onManage?:(record?:Row)=>void;draftInstance?:Row|null;market?:InstanceMarket;highlightId?:string|null;resetDraftKey?:number;
}){
 const [view,setView]=useState(mode),[config,setConfig]=useState<Row>({...defaults}),[products,setProducts]=useState<Row[]>([]),[records,setRecords]=useState<Row[]>([]);
 const [name,setName]=useState(''),[editingId,setEditingId]=useState<string|null>(null),[selectedId,setSelectedId]=useState<string|null>(null);
 const [message,setMessage]=useState(''),[failed,setFailed]=useState(false),[busy,setBusy]=useState(''),[result,setResult]=useState<Row|null>(null),[period,setPeriod]=useState(()=>defaultBacktestPeriod('5m')),[confirmExit,setConfirmExit]=useState<string|null>(null),[confirmDelete,setConfirmDelete]=useState<string|null>(null),[auditTime,setAuditTime]=useState('');
 const [recordsLoaded,setRecordsLoaded]=useState(false);
 const [accounts,setAccounts]=useState<Row[]>([]),[execution,setExecution]=useState<Row>({mode:'primary',account_ids:[]});
 const [reportOpen,setReportOpen]=useState(false);
 const [referencePrices,setReferencePrices]=useState<Row>({});
 const [,setMtmClock]=useState(0);
 const hasPosition=records.some(record=>record.state.owned||record.state.pending);
 const exitModeName=useId();
 const refreshing=useRef<Promise<Row[]>|null>(null);
 const panel=useRef<HTMLDivElement>(null),actionLock=useRef(false),requestNumber=useRef(0),createRequest=useRef<{fingerprint:string,id:string}|null>(null);
 const refresh=useCallback(async(force=false)=>{
  if(!force&&refreshing.current)return refreshing.current;
  const request=++requestNumber.current;
  const task=api('strategy/instances').then(response=>{if(request===requestNumber.current){setRecords(response.instances);setRecordsLoaded(true)}return response.instances as Row[]});
  refreshing.current=task;
  try{return await task}finally{if(refreshing.current===task)refreshing.current=null}
 },[]);
 useEffect(()=>{
  if(!visible)return;
  refresh(true).catch(error=>{setMessage(error.message);setFailed(true)});
  const timer=setInterval(()=>{if(!document.hidden)refresh().catch(()=>{})},4000);
  return()=>{clearInterval(timer);++requestNumber.current};
 },[visible,refresh,overview]);
 useEffect(()=>{
  if(!visible||overview)return;
  let current=true;setProducts([]);
  api('brokers').then(response=>{if(current)setAccounts(response.accounts)}).catch(error=>{if(current){setMessage(error.message);setFailed(true)}});
  api('products')
   .then(response=>{if(current)setProducts(response.products)})
   .catch(error=>{if(current){setMessage(error.message);setFailed(true)}});
  return()=>{current=false};
 },[visible,overview,view]);
 useEffect(()=>{
  if(!visible||overview)return;
  let current=true;
  api('market-tickers').then(response=>{if(current)setReferencePrices(Object.fromEntries((response.tickers||[]).filter((row:Row)=>Number.isFinite(Number(row.price))&&Number(row.price)>0).map((row:Row)=>[row.symbol,Number(row.price)])))}).catch(()=>{});
  return()=>{current=false};
 },[visible,overview,config.symbol]);
 useEffect(()=>{
  if(!visible||!hasPosition)return;
  const timer=setInterval(()=>setMtmClock(value=>value+1),1000);
  return()=>clearInterval(timer);
 },[visible,hasPosition]);
 useEffect(()=>{setView(mode);setMessage('');setFailed(false);if(visible&&mode==='backtest')setReportOpen(!!report)},[mode,visible,report]);
 useEffect(()=>{if(panel.current)panel.current.scrollTop=0},[reportOpen]);
 useEffect(()=>{if(visible&&mode==='backtest'&&report){setResult(report);setConfig({...defaults,...report.config});setPeriod(previous=>({...previous,...Object.fromEntries(Object.keys(previous).filter(key=>report.config[key]!==undefined).map(key=>[key,report.config[key]]))}));setAuditTime('')}},[report,visible,mode]);
 useEffect(()=>{if(highlightId)setSelectedId(highlightId)},[highlightId]);
 useEffect(()=>{if(resetDraftKey){if(draftInstance)useSettings(draftInstance,true);else newInstance();}},[resetDraftKey,draftInstance]);
 useEffect(()=>{setPeriod(previous=>{const start=fitBacktestRange(previous.start,previous.end,config.resolution,previous.timezone);return start===previous.start?previous:{...previous,start}})},[config.resolution]);
 useEffect(()=>{
  if(!visible||!drawer)return;
  const previous=document.activeElement as HTMLElement;
  const overflow=document.body.style.overflow;document.body.style.overflow='hidden';
  panel.current?.focus();
  function key(event:KeyboardEvent){
   if(event.key==='Escape')onClose();
   if(event.key==='Tab'&&panel.current){
    const nodes=panel.current.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),a[href]');
    const first=nodes[0],last=nodes[nodes.length-1];
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}
   }
  }
  document.addEventListener('keydown',key);
  return()=>{document.removeEventListener('keydown',key);document.body.style.overflow=overflow;previous?.focus()};
 },[visible,drawer,onClose]);
 const selected=records.find(record=>record.id===selectedId)||records[0];
 const editing=records.find(record=>record.id===editingId);
 const locked=!!editing&&hasWork(editing);
 const product=products.find(item=>item.symbol===config.symbol);
 const referencePrice=Number(referencePrices[config.symbol]??(result&&result.config.symbol===config.symbol?result.indicator?.at(-1)?.close:NaN));
 const estimatedNotional=Number(config.quantity)*Number(product?.contract_value)*referencePrice;
 const hasSizingEstimate=Number.isFinite(estimatedNotional)&&estimatedNotional>0;
 const range=backtestRange(period.start,period.end,config.resolution,Date.now(),period.timezone);
 const latestEnd=latestBacktestEnd(config.resolution,period.timezone);
 const resultIsStale=!!result&&Object.entries({...config,...period}).some(([key,value])=>result.config?.[key]!==value);
 const auditRows:Row[]=result?.indicator||[];
 const auditCandle=auditTime?auditRows.find(row=>row.time*1000===parseBacktestTime(auditTime,'Asia/Kolkata')):auditRows.at(-1);
 const running=records.filter(record=>record.enabled).length;
 function newInstance(){setEditingId(null);setName('');setConfig({...defaults});setExecution({mode:'primary',account_ids:[]});createRequest.current=null;setMessage('New instance draft. Saving does not start trading.');setFailed(false)}
 function requestDelete(record:Row){
  setConfirmExit(null);setConfirmDelete(null);
  if(record.enabled){setMessage('Cannot delete a running instance. Stop it first, then Exit any open position or pending orders.');setFailed(true);return}
  if(hasWork(record)){setMessage('Cannot delete this instance while a position, tracked orders or exit reconciliation remains. Use Exit and wait for completion.');setFailed(true);return}
  setMessage('');setFailed(false);setConfirmDelete(record.id);
 }
 function useSettings(record:Row,edit=false){setExecution(record.execution||{mode:'primary',account_ids:[]});setConfig({...defaults,...record.config});setName(edit?record.name:`${record.name} copy`.slice(0,80));setEditingId(edit?record.id:null);setSelectedId(record.id);createRequest.current=null;setFailed(false);setMessage(edit?'Editing a stopped instance. Saving keeps it stopped.':'Settings copied into a new draft. Save to create a separate stopped instance.')}
 async function act(action:string,record?:Row){
  if(actionLock.current)return;
  if(action==='run'&&range.error){setMessage(range.error);setFailed(true);return}
  actionLock.current=true;setBusy(action+(record?':'+record.id:''));setMessage('');setFailed(false);
  let createdId:string|null=null;
  try{
   if(action==='save'){
    let response;
    if(editingId){response=await api(`strategy/instances/${editingId}`,{name,config,execution},'PUT')}
    else{
     const fingerprint=JSON.stringify({name,config,execution});
     if(createRequest.current?.fingerprint!==fingerprint)createRequest.current={fingerprint,id:crypto.randomUUID()};
     response=await api('strategy/instances',{name,config,execution,request_id:createRequest.current.id});
    }
    setSelectedId(response.instance.id);createRequest.current=null;
    if(!editingId)createdId=response.instance.id;
    setMessage(editingId?'Instance settings updated. It remains stopped.':'Instance created and stopped. It is available on Overview.');
   }else if(action==='run'){
    const response=await api('backtest',{...config,...period});setResult(response);setAuditTime('');setReportOpen(true);setMessage('Backtest completed and saved. No forward instance was started.');
   }else if(action==='delete'&&record){
    await api(`strategy/instances/${record.id}`,undefined,'DELETE');
    setConfirmDelete(null);setConfirmExit(null);
    setSelectedId(current=>current===record.id?null:current);
    if(editingId===record.id){setEditingId(null);setName('');setConfig({...defaults});setExecution({mode:'primary',account_ids:[]});createRequest.current=null}
    setMessage('Instance deleted. Other instances are unchanged.');
   }else if(record){
    const response=await api(`strategy/instances/${record.id}/control`,{action});
    ++requestNumber.current;
    setRecords(current=>current.map(item=>item.id===response.instance.id?response.instance:item));
    setConfirmExit(null);setSelectedId(record.id);
    setMessage(response.instance.state.exit_requested?'Exit is pending. The engine will reconcile outstanding orders and retry closure until confirmed.':action==='start'?'Instance is running. Its first entry waits for a fresh confirmed Supertrend flip.':action==='stop'?'Instance stopped. New entries are disabled; existing protection remains active.':'Instance exited. Its owned position is flat and tracked orders are cleared.');
   }
   await refresh(true).catch(()=>{});onUpdate();
   if(createdId)onCreated?.(createdId);else if(action==='save')onClose();
  }catch(error:any){setMessage(error.message||'Request failed. Refresh instance status before retrying.');setFailed(true);await refresh(true).catch(()=>{})}
  finally{actionLock.current=false;setBusy('')}
 }
 if(!visible)return null;
 const instanceManager=<section className="panel">
      <div className="panel-head"><h3>{overview?'Strategy instances':'Saved instances'} <span className="tag">{recordsLoaded?records.length:'—'}</span></h3><div className="inline-actions"><span className={'tag '+(running?'green':'muted')}>{recordsLoaded?`${running} running`:'Loading…'}</span>{overview&&onBacktest&&<button className="secondary" onClick={onBacktest}><FlaskConical size={14}/> Historical backtest</button>}{overview&&onManage&&<button className="secondary" onClick={()=>onManage()}><Plus size={14}/> Create instance</button>}</div></div>
      <p className="instance-manager-note">Each action uses that instance’s saved settings. Different instruments can run together; only one instance can control a given instrument at a time.</p>
      {records.length?<div className="instance-list">{records.map(record=>{
       const exiting=!!record.state.exit_requested;
       const targets=record.execution_accounts;
       const blocked=records.some(other=>other.id!==record.id&&other.symbol===record.symbol&&hasWork(other)&&(!targets||!other.executions||other.executions.some((lane:Row)=>hasWork(lane)&&targets.includes(lane.account_id))));
       const lifecycle=record.lifecycle||'stopped';
       const starting=busy==='start:'+record.id&&!record.enabled;
       const mtm=instanceMtm(record,market);
       const status=starting?'Checking Testnet account and arming this instance…':record.state.status||'Stopped; ready to start.';
       return <article key={record.id} className={'instance-card '+(selected?.id===record.id?'selected':'')} data-instance-id={record.id}>
        <div className="instance-card-header"><div><h3>{record.name}</h3><p>{record.symbol} · {record.config.resolution} · ATR {record.config.atr_period} × {record.config.multiplier}</p></div><span className={'tag '+(record.enabled?'green':exiting?'amber':lifecycle==='attention'?'red':'muted')}>{starting?'STARTING':lifecycle.toUpperCase()}</span></div>
        <p className="instance-status">{status}</p><div className="instance-account-tags">{record.executions?.length?record.executions.map((lane:Row)=><span key={lane.account_id} className={'tag '+(lane.enabled?'green':lane.state.error?'red':'muted')} title={lane.state.error||lane.state.status}>{lane.client_name} · {lane.enabled?'Running':lane.state.owned?'Position open':'Stopped'}</span>):record.execution&&<span className="tag">{record.execution.mode==='all'?'Primary + all accounts':record.execution.mode==='selected'?'Selected accounts':'Primary account'}</span>}</div>
        {record.state.error&&<p className="error" role="alert">{record.state.error}</p>}
        <div className="instance-mtm" aria-label={`MTM ${record.name}`}><div><small>Open position MTM · USD</small><strong className={mtm.value===null?'':mtm.value>=0?'positive':'negative'}>{mtm.value===null?'—':`${mtm.value<0?'-':mtm.value>0?'+':''}$${money(Math.abs(mtm.value))}`}</strong></div><div className="instance-mtm-source"><span className={'tag '+(mtm.source==='WebSocket mark price'?'green':'muted')}>{mtm.source==='WebSocket mark price'?'WEBSOCKET':mtm.source==='Exchange snapshot'?'SNAPSHOT':mtm.source.toUpperCase()}</span><small>{mtm.source==='Flat'?(record.enabled?'Waiting for a fresh signal':'No open position'):mtm.value===null?'Waiting for confirmed position / price data':record.executions?.length>1?`${mtm.size} contracts across ${record.executions.length} accounts`:`${mtm.size} contracts · Entry ${money(mtm.entry)}${mtm.mark?` · Mark ${money(mtm.mark)}`:''}`}</small></div></div>
        {record.state.owned&&<p className="tiny-note">Strategy-owned position: {record.executions?.length>1?'tracked per account':(record.state.position?.size??'reconciling')+' contracts'}. Stop preserves existing exchange protection.</p>}
        {blocked&&!hasWork(record)&&<p className="tiny-note">Another instance controls {record.symbol} on a selected account. Exit it before starting this one.</p>}
        <div className="execution-actions">
         <button className="primary" disabled={!!busy||record.enabled||exiting||!!record.state.pending||blocked} onClick={()=>act('start',record)} aria-label={`Start ${record.name}`}>{starting?<Loader2 className="spin" size={14}/>:<Play size={14}/>} {starting?'Starting…':'Start'}</button>
         <button className="secondary" disabled={!!busy||!record.enabled||exiting} onClick={()=>act('stop',record)} aria-label={`Stop ${record.name}`}><Pause size={14}/> Stop</button>
         <button className="secondary danger" disabled={!!busy||exiting||lifecycle==='exited'} onClick={()=>{setConfirmDelete(null);setConfirmExit(record.id)}} aria-label={`Exit ${record.name}`}>{exiting?<Loader2 className="spin" size={14}/>:<Square size={14}/>} {exiting?'Exiting…':'Exit'}</button>
        </div>
        {confirmExit===record.id&&<div className="close-confirm" role="group" aria-label={`Confirm exit ${record.name}`}><p>Exit <strong>{record.name}</strong>? This stops new entries, reconciles pending orders and closes its strategy-owned {record.symbol} position at market. Other instances and manual positions are not closed.</p><button className="secondary danger" disabled={!!busy} onClick={()=>act('exit',record)}>Confirm exit</button><button className="text-button" disabled={!!busy} onClick={()=>setConfirmExit(null)}>Cancel</button></div>}
        {confirmDelete===record.id&&<div className="close-confirm" role="group" aria-label={`Confirm delete ${record.name}`}><p>Delete <strong>{record.name}</strong> from saved instances? Only stopped instances with no open position or pending orders can be deleted. This does not place or close trades.</p><button className="secondary danger" disabled={!!busy} onClick={()=>act('delete',record)}>Confirm delete</button><button className="text-button" disabled={!!busy} onClick={()=>setConfirmDelete(null)}>Cancel</button></div>}
        <div className="instance-tools">

         {overview&&onBacktest&&<button className="secondary" onClick={onBacktest}><FlaskConical size={14}/> Historical backtest</button>}{overview&&onManage&&<button className="text-button" disabled={!!busy} onClick={()=>onManage(record)}>Manage settings</button>}
         <button className="text-button danger" disabled={!!busy} onClick={()=>requestDelete(record)} aria-label={`Delete ${record.name}`}><Trash2 size={12}/> Delete</button>
        </div>
       </article>
      })}</div>:!recordsLoaded?<div className="empty" role="status"><strong>{failed?'Instances unavailable':'Loading instances…'}</strong><span>{failed?'Retrying the connection.':'Fetching your saved instances.'}</span></div>:<div className="empty"><strong>No instances saved</strong><span>Use Create instance to save a configuration. It will remain stopped until you press Start.</span></div>}
     </section>;
 if(overview)return <section className="instance-home" aria-label="Home strategy instances">{message&&<div className={'strategy-message '+(failed?'error':'')} role={failed?'alert':'status'}>{message}</div>}{instanceManager}</section>;
 const content=<div className={'strategy-workspace workspace-dialog '+(view==='forward'?'instance-dialog':reportOpen?'backtest-dialog report-dialog':'backtest-dialog setup-dialog')} ref={panel} tabIndex={-1} role={drawer?'dialog':undefined} aria-modal={drawer||undefined} aria-label={view==='backtest'?'Historical backtest':editingId?'Edit instance':'Create instance'}>
  <div className="strategy-desk-header"><div><span className="eyebrow">{view==='backtest'?'TEST & REVIEW':'SUPERTREND · TESTNET'}</span><h2>{view==='backtest'?'Historical backtest':editingId?'Edit instance':'Create instance'}</h2><p>{view==='backtest'?'Configure a test. Review performance and export your report here.':'Save your settings. Start the instance from Overview when ready.'}</p></div><button className="icon-button" onClick={onClose} aria-label="Close dialog"><X/></button></div>
  {message&&<div className={'strategy-message '+(failed?'error':'')} role={failed?'alert':'status'}>{message}</div>}
  {view==='backtest'&&<div className="strategy-mode" aria-label="Backtest view"><button className={!reportOpen?'active':''} onClick={()=>setReportOpen(false)}>Settings</button><button className={reportOpen?'active':''} disabled={!result} onClick={()=>setReportOpen(true)}>Report</button></div>}
  <div className="strategy-desk-grid">
   {(view==='forward'||!reportOpen)&&<section className="panel">
    <div className="panel-head"><h3><Settings2 size={17}/>{view==='forward'?(editingId?'Edit instance settings':'Create an instance'):'Backtest settings'}</h3>{view==='forward'&&<button className="text-button" disabled={!!busy} onClick={newInstance}><Plus size={14}/> New</button>}</div>
    <form className="padded" onSubmit={event=>{event.preventDefault();act(view==='forward'?'save':'run')}}>
     <fieldset disabled={!!busy||(view==='forward'&&locked)}>
      {view==='backtest'&&<>
      <label className="saved-instance-picker">Saved instance<select disabled={!!busy||!records.length} value={selected?.id||''} onChange={event=>setSelectedId(event.target.value)}>{!records.length&&<option value="">No saved instances</option>}{records.map(record=><option key={record.id} value={record.id}>{record.name} · {record.symbol}</option>)}</select></label>
      <button type="button" className="secondary full" disabled={!!busy||!selected} onClick={()=>{if(selected){setConfig({...defaults,...selected.config});setMessage('Saved instance settings copied into the backtest. Forward execution is unchanged.');setFailed(false)}}}>Use saved instance settings <ArrowRight size={15}/></button>
      </>}
      {view==='forward'&&<label>Instance name<input value={name} maxLength={80} onChange={event=>setName(event.target.value)} placeholder={`Supertrend · ${config.symbol} · ${config.resolution}`} autoComplete="off"/></label>}
      {view==='forward'&&<fieldset className="execution-picker"><legend>Order execution accounts</legend><label>Execute on<select value={execution.mode} onChange={event=>setExecution({mode:event.target.value,account_ids:[]})}><option value="primary">Primary account</option><option value="all">Primary + all accounts</option><option value="selected">Selected accounts</option></select></label>{!accounts.length&&<p className="error">Add a Delta Testnet account from Brokers before creating an instance.</p>}{execution.mode==='selected'?<div className="account-checkboxes">{accounts.map(account=><label key={account.id}><input type="checkbox" checked={execution.account_ids.includes(account.id)} onChange={event=>setExecution({...execution,account_ids:event.target.checked?[...execution.account_ids,account.id]:execution.account_ids.filter((id:string)=>id!==account.id)})}/><span>{account.client_name}{account.is_primary?' · Primary':''}</span></label>)}</div>:<p className="tiny-note">{execution.mode==='all'?accounts.map(account=>account.client_name).join(' · '):accounts.find(account=>account.is_primary)?.client_name||'No primary account'}</p>}<p className="tiny-note">Quantity and risk limits apply separately to each account. Accounts are fixed at Start. Exit existing positions before changing execution targets.</p></fieldset>}
      {view==='backtest'&&<><span className="tag">DELTA TESTNET ONLY</span><p className="tiny-note">Backtests use the same Testnet market as forward instances. Compare with the Delta demo Traded Price chart using identical settings.</p></>}
      <label>Instrument<select value={config.symbol} onChange={event=>setConfig({...config,symbol:event.target.value})}>{!products.length&&<option value={config.symbol}>{config.symbol}</option>}{products.map(item=><option key={item.id} value={item.symbol}>{item.symbol}{item.symbol==='USOONUSD'?' · Oil ETF':item.symbol==='XAUTUSD'||item.symbol==='PAXGUSD'?' · Gold':''}</option>)}</select></label>
      <label>Timeframe<select value={config.resolution} onChange={event=>setConfig({...config,resolution:event.target.value})}>{TIMEFRAMES.map(frame=><option key={frame}>{frame}</option>)}</select></label>
      <fieldset className="exit-mode-picker"><legend>Exit mode · same rules for instances and backtests</legend>{EXIT_MODES.map(option=><label className="exit-mode-option" key={option.value}><input type="radio" name={exitModeName} value={option.value} checked={config.exit_mode===option.value} onChange={()=>setConfig({...config,exit_mode:option.value})}/><span><strong>{option.label}</strong><small>{option.description}</small></span></label>)}</fieldset>
      <p className="tiny-note">{config.exit_mode==='opposite_signal'?'Point-based SL/target are disabled for this mode.':'SL and target use price points measured from the entry price; 0 disables that level.'} Daily loss limits still apply{view==='backtest'?'; any remaining position closes at the end of the test.':'.'}</p>
      <div className="form-grid">{fields.filter(field=>config.exit_mode!=='opposite_signal'||!['sl_points','target_points'].includes(field.key)).map(field=><label key={field.key}>{field.label}<input required type="number" min={field.min} step={field.step} value={config[field.key]} onChange={event=>setConfig({...config,[field.key]:event.target.value===''?'':Number(event.target.value)})}/></label>)}</div>
      {hasSizingEstimate&&<div aria-label="Position sizing check"><p className="tiny-note">Estimated position value: <strong>${money(estimatedNotional)}</strong> = {config.quantity} contracts × {product?.contract_value} units × ${money(referencePrice)} reference price, before fees/slippage.</p>{estimatedNotional>Number(config.max_notional)&&<p className="error" role="status">Your ${money(config.max_notional)} maximum position value is below this estimate and can block entries. Review the quantity or limit before running.</p>}{view==='backtest'&&estimatedNotional>period.capital&&<p className="error" role="status">This estimate also exceeds the ${money(period.capital)} initial capital. Backtests require the full position value under the 1× collateral model.</p>}</div>}
     </fieldset>
     <p className="tiny-note">Max position value limits total exposure before leverage; it is separate from margin.</p>
     <p className="tiny-note">1 contract = {product?.contract_value||'—'} underlying units. Points are price units; 0 disables that SL or target. Daily loss uses account USD equity and a UTC day boundary.</p>
     {view==='forward'?<>
      <button className="primary full" type="submit" disabled={!!busy||locked||!products.length||!accounts.length||(execution.mode==='selected'&&!execution.account_ids.length)}>{busy==='save'?<Loader2 className="spin" size={16}/>:<Save size={16}/>} {editingId?'Save instance settings':'Create instance'}</button>
      <p className="tiny-note">Saving never starts trading. Start, Stop, Exit and Delete are on Overview.</p>
      {locked&&<p className="tiny-note">This instance has active execution, a position or unresolved orders. Exit it before editing; you can create a new draft instead.</p>}
     </>:<>
      <label>Range timezone<select value={period.timezone} disabled={!!busy} onChange={event=>{
       const timezone=event.target.value as typeof period.timezone;
       setPeriod({...period,timezone,start:backtestInputTime(parseBacktestTime(period.start,period.timezone),timezone),end:backtestInputTime(parseBacktestTime(period.end,period.timezone),timezone)});
      }}><option value="Asia/Kolkata">IST · UTC+05:30</option><option value="UTC">UTC</option></select></label>
      <div>{(['start','end'] as const).map(key=><div key={key}>
       <label>{key==='start'?'Start date':'End date'} ({period.timezone==='UTC'?'UTC':'IST'})<input required type="date" max={latestEnd.slice(0,10)} aria-describedby="backtest-range-info" disabled={!!busy} value={period[key].slice(0,10)} onChange={event=>setPeriod({...period,[key]:event.target.value+'T'+(period[key].split('T')[1]||'00:00')})}/></label>
       <label>{key==='start'?'Start time':'End time'} ({period.timezone==='UTC'?'UTC':'IST'})<input required type="time" step="60" aria-describedby="backtest-cutoff-info" disabled={!!busy} value={period[key].split('T')[1]||'00:00'} onChange={event=>setPeriod({...period,[key]:period[key].slice(0,10)+'T'+event.target.value})}/></label>
      </div>)}</div>
      <p id="backtest-cutoff-info" className="tiny-note">Times default to 12:00 AM. End time sets the data cutoff; 12:00 AM is the beginning of the selected end date.</p>
      <p className="tiny-note">Data cutoff: <strong>{Number.isFinite(parseBacktestTime(period.end,period.timezone))?new Date(parseBacktestTime(period.end,period.timezone)).toLocaleString('en-IN',{timeZone:period.timezone,day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit',hour12:true}):'—'} {period.timezone==='UTC'?'UTC':'IST'}</strong></p>
      <button type="button" className="text-button" disabled={!!busy} onClick={()=>{const end=latestBacktestEnd(config.resolution,period.timezone);setPeriod({...period,end,start:fitBacktestRange(period.start,end,config.resolution,period.timezone)})}}>Use latest available time</button>
      <p id="backtest-range-info" className={range.error?'error':'tiny-note'} role={range.error?'alert':undefined}>{range.error||`Up to ${range.candles.toLocaleString('en-IN')} candles · Maximum ${range.maxDays} full days at ${config.resolution}. Only bars fully closed by End time are included.`}</p>
      {range.candles>5000&&<button type="button" className="secondary full" onClick={()=>setPeriod({...period,start:fitBacktestRange(period.start,period.end,config.resolution,period.timezone)})}>Use {range.maxDays}-day range</button>}
      <div className="form-grid">{[{key:'capital',label:'Initial capital (USD)'},{key:'fee_bps',label:'Fee (bps / side)'},{key:'slippage_bps',label:'Slippage (bps)'}].map(field=><label key={field.key}>{field.label}<input type="number" required min={0} step="any" value={(period as Row)[field.key]} onChange={event=>setPeriod({...period,[field.key]:Number(event.target.value)})}/></label>)}</div>
      <button type="submit" className="primary full" disabled={!!busy||!products.length||!!range.error}>{busy==='run'?<Loader2 className="spin" size={16}/>:<Play size={16}/>} {busy==='run'?'Running backtest…':'Run backtest'}</button>

     </>}
    </form>
   </section>}
   {view==='backtest'&&reportOpen&&<div className="backtest-report">
    {result?<>
     {resultIsStale&&<div className="close-confirm" role="status" aria-label="Backtest settings changed"><p><strong>Settings changed — run the backtest again.</strong><br/>The report below still uses {exitLabel(result.config.exit_mode)}. Your selected settings apply to the next run.</p><button className="primary" disabled={!!busy||!products.length||!!range.error} onClick={()=>act('run')}>Run with current settings</button></div>}
     <div className="report-actions"><h3>Backtest report <span className="tag">#{result.id}</span></h3><button className="secondary" onClick={()=>downloadBacktestReport(result)}><Download size={14}/> Export report CSV</button></div>
     <section className="panel padded" aria-label="Backtest data coverage"><h3>{result.source}</h3><p className="description">{result.config.symbol} · {result.config.resolution} · Supertrend {result.config.atr_period} × {result.config.multiplier} · {exitLabel(result.config.exit_mode)}{result.config.exit_mode!=='opposite_signal'?` · SL ${result.config.sl_points} points · Target ${result.config.target_points} points`:''}</p><p className="tiny-note">Covered (IST): {date(result.data?.first_candle)} → {date(result.data?.closed_through)} · {result.candles??'—'} closed candles · {result.data?.warmup_candles??'—'} warmup bars.</p><p className="tiny-note">Signals confirm at candle close. Entries fill at the next candle open. Compare the same market, Traded Price chart, timeframe and ATR settings.</p></section>
     {!!result.diagnostics?.blocked_entries&&<section className="panel padded" aria-label="Blocked backtest entries"><h3>Entry checks · {result.diagnostics.blocked_entries} blocked</h3><p className="description">{result.diagnostics.signals} Supertrend flip signals were detected. {result.diagnostics.blocked_entries} entry attempts were blocked by the saved risk settings.</p>{!!result.diagnostics.blocked_by_reason.max_position_value&&<p className="tiny-note"><strong>Maximum position value:</strong> {result.diagnostics.blocked_by_reason.max_position_value} entries exceeded the ${money(result.config.max_notional)} cap. At {result.config.quantity} contracts, blocked entries required ${money(result.diagnostics.min_blocked_notional)}–${money(result.diagnostics.max_blocked_notional)} total position value.</p>}{!!result.diagnostics.blocked_by_reason.insufficient_capital&&<p className="tiny-note"><strong>Insufficient capital:</strong> {result.diagnostics.blocked_by_reason.insufficient_capital} entries exceeded available simulated capital. This backtest uses 1× collateral.</p>}{!!result.diagnostics.blocked_by_reason.daily_loss_limit&&<p className="tiny-note"><strong>Daily loss limit:</strong> {result.diagnostics.blocked_by_reason.daily_loss_limit} entries were paused after the daily loss cap was reached.</p>}<p className="tiny-note">Review quantity, position limit and capital, then run again.</p></section>}
     <div className="report-metrics" aria-label="Performance summary">{[{label:'Net P&L',value:'$'+money(result.metrics.net_pnl)},{label:'ROI',value:money(result.metrics.return_pct)+'%'},{label:'Win rate',value:money(result.metrics.win_rate)+'%'},{label:'Profit factor',value:result.metrics.profit_factor==null?'—':money(result.metrics.profit_factor,3)},{label:'Max drawdown',value:money(result.metrics.max_drawdown)+'%'},{label:'Closed trades',value:result.metrics.total_trades}].map(metric=><div className="stat" key={metric.label}><span>{metric.label}</span><h2>{metric.value}</h2></div>)}</div>
     <section className="panel"><div className="panel-head"><h3>Equity · {result.config.symbol}</h3><span className="tag">RUN #{result.id}</span></div><div style={{width:'100%',height:260}}><ResponsiveContainer><AreaChart data={result.equity}><CartesianGrid stroke="#28322d" vertical={false}/><XAxis dataKey="time" tickFormatter={value=>new Date(value*1000).toLocaleDateString('en-IN',{timeZone:'Asia/Kolkata'})} minTickGap={60}/><YAxis domain={['auto','auto']}/><Tooltip labelFormatter={value=>date(value)}/><Area type="linear" dataKey="equity" stroke="#baeb6c" fill="#baeb6c22" isAnimationActive={false}/></AreaChart></ResponsiveContainer></div><div className="panel-foot">Win rate {money(result.metrics.win_rate)}% · Fees {money(result.metrics.fees,4)} · Profit factor {result.metrics.profit_factor==null?'— (no losing trades)':money(result.metrics.profit_factor,3)}</div></section>
     <section className="panel"><div className="panel-head"><h3>Trades & exits</h3><a className="text-button" href={'/api/export/backtest?run_id='+result.id}><Download size={14}/> CSV</a></div><div className="table-scroll"><table><thead><tr><th>Entry time (IST)</th><th>Exit time (IST)</th><th>Side</th><th>Entry price</th><th>Exit price</th><th>P&L</th><th>Reason</th></tr></thead><tbody>{result.trades.map((trade:Row,index:number)=><tr key={index}><td>{date(trade.entry_time)}<br/><small className="tiny-note">Signal candle: {date(trade.entry_signal_time)}</small></td><td>{date(trade.time)}<br/><small className="tiny-note">{trade.exit_time_precision==='intrabar'?'During this candle':trade.exit_time_precision==='close'?'Candle close':'Candle open'}</small></td><td><span className={'tag '+(trade.side==='LONG'?'green':'red')}>{trade.side}</span></td><td>{money(trade.entry)}</td><td>{money(trade.exit)}</td><td className={trade.pnl>=0?'positive':'negative'}>{money(trade.pnl,4)}</td><td>{trade.reason}</td></tr>)}</tbody></table>{!result.trades.length&&<div className="empty"><strong>{result.diagnostics?.blocked_entries?'Entries blocked by risk limits':result.diagnostics?.signals===0?'No Supertrend flips in this period':'No trades executed'}</strong><span>{result.diagnostics?.blocked_entries?`${result.diagnostics.signals} flips detected; ${result.diagnostics.blocked_entries} entries blocked. See Entry checks for the exact limits.`:'Check the selected dates and strategy settings.'}</span></div>}</div></section>
     <details className="report-audit"><summary>Candle & Supertrend audit</summary><section className="panel" aria-label="Candle and Supertrend comparison"><div className="panel-head"><h3>Candle & Supertrend comparison</h3></div><div className="padded"><label>Compare candle (IST)<select value={auditCandle?.time??''} onChange={event=>setAuditTime(backtestInputTime(Number(event.target.value)*1000,'Asia/Kolkata'))}>{[...auditRows].reverse().map(row=><option key={row.time} value={row.time}>{date(row.time)}</option>)}</select></label><p className="tiny-note">Most recent {auditRows.length} bars from this run. Chart labels show the candle’s opening time.</p></div>{auditCandle&&<div className="table-scroll"><table><thead><tr><th>Open</th><th>High</th><th>Low</th><th>Close</th><th>ATR</th><th>Supertrend</th><th>Trend / signal</th></tr></thead><tbody><tr><td>{money(auditCandle.open)}</td><td>{money(auditCandle.high)}</td><td>{money(auditCandle.low)}</td><td>{money(auditCandle.close)}</td><td>{money(auditCandle.atr,4)}</td><td>{money(auditCandle.supertrend,4)}</td><td className={auditCandle.direction===1?'positive':'negative'}>{auditCandle.direction===1?'Uptrend':auditCandle.direction===-1?'Downtrend':'Warming up'}{auditCandle.signal?' · FLIP':''}</td></tr></tbody></table></div>}</section>
     </details><p className="tiny-note">Intrabar SL/TP timestamps identify the candle; OHLC does not reveal the exact fill time. {result.assumptions} Forward uses actual fills and account equity; historical results cannot reproduce latency, funding or other account positions.</p>
    </>:<section className="panel lab-empty"><Activity size={44}/><h2>Test the same Supertrend rules.</h2><p>Use any saved instance or explore a new setup.<br/>Actual exchange candles, long and short trades,<br/>SL/target exits, fees and slippage.</p><span className="tag">UP TO 5,000 BARS + ATR WARMUP</span></section>}
   </div>}
  </div>
 </div>;
 return drawer?<div className="strategy-overlay" onMouseDown={event=>{if(event.target===event.currentTarget)onClose()}}>{content}</div>:content;
}
