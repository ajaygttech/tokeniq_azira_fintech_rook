'use client';
import { depthLevels, formatNumber as money, formatPrice } from './format';

type Book = {buy?: unknown; sell?: unknown};
export function MarketDepth({book, price, status, error, symbol='BTCUSD', tickSize}: {tickSize?:string; symbol?:string; book?: Book | null; price?: number | null; status: 'live' | 'updated' | 'stale' | 'unavailable'; error?: string}) {
  const buys = depthLevels(book?.buy, 'buy', 5), sells = depthLevels(book?.sell, 'sell', 5);
  const statusLabel = status === 'live' ? 'Live' : status === 'updated' ? 'Updated' : status === 'stale' ? 'Delayed' : 'Unavailable';
  function side(levels: typeof buys, type: 'buy' | 'sell') {
    return <div className={`depth-half ${type}`} aria-label={type === 'buy' ? 'Buy bids' : 'Sell offers'}>
      <h4>{type === 'buy' ? 'Buy orders' : 'Sell orders'}</h4>
      <div className="depth-columns"><span>Price</span><span>Quantity</span></div>
      {levels.length ? levels.map(level => <div className="depth-level" key={level.price}>
        <span>{formatPrice(level.price, tickSize)}</span><span>{money(level.size, 0)}</span>
      </div>) : <p className="depth-no-orders">No {type} offers</p>}
    </div>;
  }
  return <section className="panel depth-card" aria-label={symbol+" market depth"}>
    <div className="depth-heading"><h3>Market depth</h3><span className="tag muted">{symbol}</span></div>
    <div className="depth-price"><div><span>Last price</span><strong>{price == null ? '—' : '$' + formatPrice(price, tickSize)}</strong></div><span className={`depth-status ${status}`}><i/>{statusLabel}</span></div>
    {!buys.length && !sells.length ? <div className="empty"><strong>Depth unavailable</strong><span>{error || 'Waiting for exchange data.'}</span></div> : <>
      <div className="depth-comparison">{side(buys, 'buy')}{side(sells, 'sell')}</div>
      <div className="depth-units">Price in USD <span>·</span> Quantity in contracts</div>
    </>}
  </section>;
}
