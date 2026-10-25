'use client';
import { useEffect, useState } from 'react';

type Level = {price: string; size: string};
type Feed = {
  symbol: string;
  marks: Record<string,{price:number;time:number}>;
  status: 'connecting' | 'live' | 'reconnecting' | 'disconnected' | 'unsupported';
  price: number | null;
  lastTradeAt: number;
  lastMessageAt: number;
  lastBookAt: number;
  tradeCount: number;
  book: {buy: Level[]; sell: Level[]} | null;
};
const initial: Feed = {marks:{},symbol:'',status:'disconnected',price:null,lastTradeAt:0,lastMessageAt:0,lastBookAt:0,tradeCount:0,book:null};

// Public market channels only. No API credentials enter this connection.
export function useDeltaMarket(enabled: boolean, environment: string, symbol: string, positionSymbols: string[] = []) {
  const markSymbols=[...new Set(positionSymbols)].sort().join(',');
  const [feed,setFeed] = useState<Feed>(initial);
  useEffect(()=>{
    if (!enabled) {setFeed(initial);return;}
    const endpoints:Record<string,string> = {
      testnet:'wss://socket-ind-pub.testnet.deltaex.org',
    };
    if (!endpoints[environment]) {setFeed({...initial,status:'unsupported'});return;}
    let stopped=false, socket:WebSocket|null=null;
    let retry:ReturnType<typeof setTimeout>|undefined;
    let attempt=0, lastTraffic=Date.now(), lastMarket=0, dirty=true;
    let current:Feed={...initial,symbol,status:'connecting'};
    setFeed(current);
    function connect() {
      if(stopped)return;
      lastTraffic=Date.now(); lastMarket=0;
      current={...current,status:attempt?'reconnecting':'connecting',marks:{},price:null,book:null,lastTradeAt:0,lastBookAt:0};dirty=true;
      const ws=new WebSocket(endpoints[environment]);socket=ws;
      ws.onopen=()=>{
        if(stopped||socket!==ws)return;
        ws.send(JSON.stringify({type:'subscribe',payload:{channels:[...['trades','ob_l2'].map(name=>({name,symbols:[symbol]})),...(markSymbols?[{name:'mark_price',symbols:markSymbols.split(',').map(s=>'MARK:'+s)}]:[])]}}));
        ws.send(JSON.stringify({type:'enable_heartbeat'}));
      };
      ws.onmessage=event=>{
        if(stopped||socket!==ws)return;
        let message;
        try {message=JSON.parse(event.data);}catch{return;}
        lastTraffic=Date.now();
        if(message.type==='error') {ws.close();return;}
        if(message.type==='mark_price') {
          const market=String(message.sy||'').replace(/^MARK:/,''),price=Number(message.p),time=Number(message.ts)/1000;
          if(!markSymbols.split(',').includes(market)||!Number.isFinite(price)||price<=0||!Number.isFinite(time)||time<=0||Date.now()-time>15000||time>Date.now()+5000||time<(current.marks[market]?.time||0))return;
          current={...current,marks:{...current.marks,[market]:{price,time}}};dirty=true;return;
        }
        if(message.sy!==symbol)return;
        if(message.type==='trades') {
          const price=Number(message.p), timestamp=Number(message.t)/1000;
          if(!Number.isFinite(price)||price<=0||!Number.isFinite(timestamp)||timestamp<=0||timestamp<current.lastTradeAt)return;
          current={...current,price,lastTradeAt:timestamp,tradeCount:current.tradeCount+1};
        } else if(message.type==='ob_l2') {
          if(!Array.isArray(message.a)||!Array.isArray(message.b))return;
          const levels=(rows:unknown[])=>rows.filter((row):row is [string,string]=>Array.isArray(row)&&row.length>=2&&Number.isFinite(Number(row[0]))&&Number(row[0])>0&&Number.isFinite(Number(row[1]))&&Number(row[1])>0).map(row=>({price:String(row[0]),size:String(row[1])}));
          current={...current,book:{buy:levels(message.b),sell:levels(message.a)},lastBookAt:Date.now()};
        } else return;
        lastMarket=Date.now();attempt=0;
        current={...current,lastMessageAt:lastMarket,status:current.price!==null?'live':'connecting'};dirty=true;
      };
      ws.onerror=()=>ws.close();
      ws.onclose=event=>{
        if(stopped||socket!==ws)return;
        socket=null;
        current={...current,status:'reconnecting',marks:{},price:null,book:null};dirty=true;
        // Rate limiting gets a five minute cooldown; ordinary failures back off.
        const delay=event.code===4429||event.code===429?300000:Math.min(30000,1000*2**Math.min(attempt++,5))+Math.random()*500;
        retry=setTimeout(connect,delay);
      };
    }
    connect();
    // Publish at most four UI updates/second, retaining every received trade count.
    const publish=setInterval(()=>{if(dirty&&!stopped){dirty=false;setFeed({...current});}},250);
    const watchdog=setInterval(()=>{
      if(stopped||!socket)return;
      const now=Date.now();
      if(Object.values(current.marks).some(mark=>now-mark.time>=15000)){current={...current,marks:Object.fromEntries(Object.entries(current.marks).filter(([,mark])=>now-mark.time<15000))};dirty=true;}
      if(now-lastTraffic>35000||(lastMarket>0&&now-lastMarket>15000)||
        (current.lastTradeAt>0&&now-current.lastTradeAt>15000)||
        (current.lastBookAt>0&&now-current.lastBookAt>15000))socket.close();
    },1000);
    return()=>{stopped=true;clearTimeout(retry);clearInterval(publish);clearInterval(watchdog);socket?.close();};
  },[enabled,environment,symbol,markSymbols]);
  const current=feed.symbol===symbol?feed:initial;
  return {...current,book:Date.now()-current.lastBookAt<15000?current.book:null,live:current.status==='live'&&Date.now()-current.lastTradeAt<15000};
}
