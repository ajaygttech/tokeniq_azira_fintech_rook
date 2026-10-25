export function timestampMillis(value: unknown): number {
  if (value === null || value === undefined || value === '') return NaN;
  const number = Number(value);
  if (!Number.isFinite(number)) return typeof value === 'string' ? Date.parse(value) : NaN;
  const magnitude = Math.abs(number);
  return magnitude < 1e11 ? number * 1000 : magnitude < 1e14 ? number : number / 1000;
}

export function formatDate(value: unknown): string {
  const millis = timestampMillis(value);
  return Number.isFinite(millis) ? new Date(millis).toLocaleString('en-IN', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  }) : '—';
}

export function formatNumber(value: unknown, decimals = 2): string {
  if (value === null || value === undefined || value === '' || !Number.isFinite(Number(value))) return '—';
  return Number(value).toLocaleString('en-US', {minimumFractionDigits: decimals, maximumFractionDigits: decimals});
}

export function formatPrice(value: unknown, tickSize?: unknown): string {
  const price = Number(value), tick = Number(tickSize);
  const decimals = Number.isFinite(tick) && tick > 0
    ? Math.min(8, Math.max(0, Math.ceil(-Math.log10(tick))))
    : price < .0001 ? 8 : price < 1 ? 6 : price < 100 ? 4 : 2;
  return formatNumber(value, decimals);
}

export type DepthLevel = {price: number; size: number; total: number};
export function depthLevels(rows: unknown, side: 'buy' | 'sell', limit = 7): DepthLevel[] {
  if (!Array.isArray(rows)) return [];
  const sizes = new Map<number, number>();
  for (const row of rows) {
    const price = Number(row?.price), size = Number(row?.size);
    if (Number.isFinite(price) && price > 0 && Number.isFinite(size) && size > 0) {
      sizes.set(price, (sizes.get(price) || 0) + size);
    }
  }
  let total = 0;
  return [...sizes].sort((a, b) => side === 'buy' ? b[0] - a[0] : a[0] - b[0]).slice(0, limit)
    .map(([price, size]) => ({price, size, total: total += size}));
}
