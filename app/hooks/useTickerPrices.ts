'use client';
import {useEffect,useState} from 'react';

export const TICKER_SYMBOLS=['BTCUSD','ETHUSD','XAUTUSD','SOLUSD'];
type Quote={price:number;time:number;change_24h?:number};

export function useTickerPrices(symbols:string[]=TICKER_SYMBOLS){
 const [quotes,setQuotes]=useState<Record<string,Quote>>({});
 const symbolKey=Array.from(new Set(symbols)).sort().join(',');
 useEffect(()=>{
  const subscribedSymbols=symbolKey?symbolKey.split(','):[];
  if(!subscribedSymbols.length)return;
  let stopped=false,socket:WebSocket|null=null,retry:ReturnType<typeof setTimeout>|undefined,attempt=0,lastTraffic=Date.now();
  let current:Record<string,Quote>={},dirty=false;
  function connect(){
   if(stopped)return;
   current={};dirty=true;lastTraffic=Date.now();
   const ws=new WebSocket('wss://socket-ind-pub.testnet.deltaex.org');socket=ws;
   ws.onopen=()=>{
    if(stopped||socket!==ws)return;
    ws.send(JSON.stringify({type:'subscribe',payload:{channels:['trades','ticker'].map(name=>({name,symbols:subscribedSymbols}))}}));
    ws.send(JSON.stringify({type:'enable_heartbeat'}));
   };
   ws.onmessage=event=>{
    if(stopped||socket!==ws)return;
    let message;try{message=JSON.parse(event.data)}catch{return}
    lastTraffic=Date.now();
    if(message.type==='error'){ws.close();return}
    const time=Number(message.type==='trades'?message.t:message.ts)/1000;
    if(!Number.isFinite(time)||Date.now()-time>15000||time>Date.now()+5000)return;
    const update=(symbol:string,price:number,change?:number)=>{
     if(!subscribedSymbols.includes(symbol)||!Number.isFinite(price)||price<=0||time<(current[symbol]?.time||0))return;
     current={...current,[symbol]:{...current[symbol],price,time,...(change!==undefined&&Number.isFinite(change)?{change_24h:change}:{})}};dirty=true;attempt=0;
    };
    if(message.type==='trades')update(message.sy,Number(message.p));
    if(message.type==='ticker'&&Array.isArray(message.d))for(const row of message.d){
     const open=Number(row.ohlc?.[0]),close=Number(row.ohlc?.[3]);
     update(row.s,close,open>0?(close/open-1)*100:undefined);
    }
   };
   ws.onerror=()=>ws.close();
   ws.onclose=event=>{
    if(stopped||socket!==ws)return;
    socket=null;current={};dirty=true;
    const delay=[4429,429].includes(event.code)?300000:Math.min(30000,1000*2**Math.min(attempt++,5))+Math.random()*500;
    retry=setTimeout(connect,delay);
   };
  }
  connect();
  const publish=setInterval(()=>{
   const fresh=Object.fromEntries(Object.entries(current).filter(([,q])=>Date.now()-q.time<15000));
   if(Object.keys(fresh).length!==Object.keys(current).length){current=fresh;dirty=true}
   if(dirty){setQuotes(current);dirty=false}
  },250);
  const watchdog=setInterval(()=>{if(socket&&Date.now()-lastTraffic>35000)socket.close()},1000);
  return()=>{stopped=true;clearTimeout(retry);clearInterval(publish);clearInterval(watchdog);socket?.close()};
 },[symbolKey]);
 return quotes;
}
