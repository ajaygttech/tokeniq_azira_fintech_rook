type Row=Record<string,any>;
/** One rectangular CSV: summary, settings, trades and equity retain their record type. */
export function backtestReportCsv(report:Row){
 const rows:Row[]=[];
 for(const [metric,value] of Object.entries(report.metrics||{}))rows.push({record_type:'metric',name:metric,value});
 for(const [name,value] of Object.entries(report.config||{}))rows.push({record_type:'setting',name,value});
 rows.push({record_type:'report',name:'run_id',value:report.id},{record_type:'report',name:'source',value:report.source||report.data?.label||'Legacy report'});
 for(const [name,value] of Object.entries(report.data||{}))rows.push({record_type:'coverage',name,value});
 const ist=(seconds:any)=>seconds==null?'':new Date(Number(seconds)*1000).toLocaleString('en-IN',{timeZone:'Asia/Kolkata',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
 for(const trade of report.trades||[])rows.push({...trade,entry_time_ist:ist(trade.entry_time),exit_time_ist:ist(trade.time),record_type:'trade'});
 for(const point of report.equity||[])rows.push({...point,record_type:'equity'});
 const columns=Array.from(new Set(['record_type','name','value',...rows.flatMap(row=>Object.keys(row))]));
 const cell=(value:any)=>{
  let text=value==null?'':typeof value==='object'?JSON.stringify(value):String(value);
  if(typeof value==='string'&&/^[\s]*[=+\-@]/.test(text))text="'"+text;
  return '"'+text.replace(/"/g,'""')+'"';
 };
 return '\uFEFF'+[columns.map(cell).join(','),...rows.map(row=>columns.map(key=>cell(row[key])).join(','))].join('\r\n');
}
export function downloadBacktestReport(report:Row){
 const url=URL.createObjectURL(new Blob([backtestReportCsv(report)],{type:'text/csv;charset=utf-8'}));
 const anchor=document.createElement('a');anchor.href=url;anchor.download=`tokeniq-backtest-${report.id}-report.csv`;anchor.click();
 setTimeout(()=>URL.revokeObjectURL(url),1000);
}
