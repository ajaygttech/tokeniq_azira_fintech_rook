'use client';
import {useEffect,useState} from 'react';
import {Pause,Play,TrendingUp} from 'lucide-react';
import {useTickerPrices,TICKER_SYMBOLS} from './hooks/useTickerPrices';
import {formatNumber,formatPrice} from './format';

const MARKET_NAMES:Record<string,string>={BTCUSD:'Bitcoin',ETHUSD:'Ethereum',XAUTUSD:'Gold',SOLUSD:'Solana'};

type Quote={symbol:string;price:number;change_24h:number|null;tick_size:string};

export function MarketTicker({selected,onSelect}:{selected:string;onSelect:(symbol:string)=>void}){
 const [quotes,setQuotes]=useState<Quote[]>([]),[paused,setPaused]=useState(false),[error,setError]=useState('');
 useEffect(()=>{
  let alive=true,pending=false;
  const load=async()=>{
   if(pending||document.hidden)return;pending=true;const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),10000);
   try{
    const response=await fetch('/api/market-tickers',{signal:controller.signal});
    if(!response.ok)throw new Error();
    const data=await response.json();
    if(alive){setQuotes(data.tickers);setError('')}
   }catch{if(alive)setError('Market prices unavailable')}finally{pending=false;clearTimeout(timeout)}
  };
  load();const timer=setInterval(load,15000);
  return()=>{alive=false;clearInterval(timer)};
 },[]);
 const symbols=Array.from(new Set([...TICKER_SYMBOLS,...quotes.map(q=>q.symbol)]));
 const liveQuotes=useTickerPrices(symbols);
 const markets=symbols.map(symbol=>{const rest=quotes.find(q=>q.symbol===symbol),live=liveQuotes[symbol];return {symbol,price:live?.price??rest?.price,change_24h:live?.change_24h??rest?.change_24h??null,tick_size:rest?.tick_size,live:!!live}});
 const pinnedMarkets=TICKER_SYMBOLS.map(symbol=>markets.find(q=>q.symbol===symbol)!);
 const liveCount=markets.filter(q=>q.live).length;
 const allLive=liveCount===markets.length;
 const items=(duplicate:boolean)=><div className="market-ticker-group" aria-hidden={duplicate||undefined}>{markets.map(q=><button
  key={q.symbol} type="button" className={'market-ticker-quote'+(selected===q.symbol?' selected':'')}
  tabIndex={duplicate?-1:0} aria-label={'View '+q.symbol+' chart'} aria-pressed={selected===q.symbol}
  onClick={()=>onSelect(q.symbol)}>
  <i className={q.live?'live-dot':'offline-dot'} title={q.live?'Live price':'Updating price'}/><strong>{q.symbol}</strong><span>{q.price===undefined?'—':'$'+formatPrice(q.price,q.tick_size)}</span>
  <small className={q.change_24h===null?'':q.change_24h>=0?'positive':'negative'}>{q.change_24h===null?'—':(q.change_24h>=0?'+':'')+formatNumber(q.change_24h)+'%'}</small>
 </button>)}</div>;
 return <><nav className="market-navigation" aria-label="Choose a market"><span className="market-navigation-label">Markets</span><div className="market-navigation-items">{pinnedMarkets.map(q=><button key={q.symbol} type="button" className={'market-navigation-item'+(selected===q.symbol?' selected':'')} aria-pressed={selected===q.symbol} onClick={()=>onSelect(q.symbol)}><span className="market-navigation-name"><strong>{q.symbol}</strong><span>{MARKET_NAMES[q.symbol]}</span></span><span className="market-navigation-price"><i className={q.live?'live-dot':'offline-dot'} title={q.live?'Live price':'Updating price'}/><b>{q.price===undefined?'—':'$'+formatPrice(q.price,q.tick_size)}</b><small>{q.live?'LTP':q.price===undefined?'Loading':'Updated'}</small></span></button>)}</div></nav><section className={'market-ticker'+(paused?' paused':'')+(error&&!liveCount?' delayed':'')} aria-label="Market prices">
  <div className="market-ticker-label"><TrendingUp size={14}/><span>{allLive?'LIVE MARKETS':'MARKETS'}</span></div>
  <div className="market-ticker-window"><div className="market-ticker-track" style={{animationDuration:Math.max(35,markets.length*7)+'s'}}>{items(false)}{items(true)}</div></div>
  <span className="market-ticker-delayed" title={allLive?'Prices are live':error||'Updating market prices'}>{allLive?'LIVE':liveCount?'UPDATING':'CONNECTING'}</span>
  <button type="button" className="market-ticker-pause" aria-label={paused?'Resume market ticker':'Pause market ticker'} onClick={()=>setPaused(!paused)}>{paused?<Play size={13}/>:<Pause size={13}/>}</button>
 </section></>;
}
