/** Keep gateway HTML and interrupted responses out of user-facing JSON errors. */
export async function requestJson(path:string,body?:unknown,method='POST'){
 const controller=new AbortController();
 const timeout=setTimeout(()=>controller.abort(),path==='backtest'||path==='brokers'||path.startsWith('brokers/')||path.endsWith('/control')?90000:20000);
 try{
  const response=await fetch('/api/'+path,{signal:controller.signal,method:body===undefined&&method==='POST'?'GET':method,headers:body===undefined?undefined:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  const text=await response.text();
  let data:any;
  try{data=JSON.parse(text)}catch{throw new Error(`Service temporarily unavailable (HTTP ${response.status}). Refresh to check the latest status.`)}
  if(!response.ok)throw new Error(typeof data?.detail==='string'?data.detail:Array.isArray(data?.detail)?data.detail.map((d:any)=>`${d.loc?.slice(1).join('.')}: ${d.msg}`).join('; '):`Request failed (HTTP ${response.status}). Please try again.`);
  return data;
 }catch(error:any){
  if(error.name==='AbortError')throw new Error(path==='backtest'?'Backtest request timed out. Check saved reports before running again.':path.includes('strategy/instances')?'Request timed out. Check the instance status before retrying.':'Connection timed out. Refresh to reconnect.');
  throw error;
 }finally{clearTimeout(timeout)}
}
