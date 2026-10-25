type Row = Record<string,any>;
type Mark = {price:number;time:number};

// Size is signed contracts; contract_value converts contracts to underlying units.
export function markPosition(position:Row, products:Row[], marks:Record<string,Mark>, now=Date.now()):Row {
  const product=products.find(p=>String(p.id)===String(position.product_id)) || products.find(p=>p.symbol===(position.product_symbol||position.symbol));
  const symbol=position.product_symbol||position.symbol||product?.symbol;
  const mark=marks[symbol];
  const size=Number(position.size),entry=Number(position.entry_price),unit=Number(product?.contract_value);
  if(product?.notional_type!=='vanilla'||!mark||!Number.isFinite(mark.time)||now-mark.time>=15000||mark.time>now+5000||!Number.isFinite(mark.price)||mark.price<=0||!Number.isFinite(size)||!Number.isFinite(entry)||entry<=0||!Number.isFinite(unit)||unit<=0)
    return {...position,pnl_source:'Exchange snapshot'};
  return {...position,mark_price:mark.price,unrealized_pnl:size*unit*(mark.price-entry),pnl_source:'WebSocket mark price'};
}
