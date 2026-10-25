const DAY = 86400000;
export const MAX_BACKTEST_CANDLES = 5000;
const SECONDS: Record<string, number> = {'1m':60,'3m':180,'5m':300,'15m':900,'30m':1800,'1h':3600,'2h':7200,'4h':14400,'6h':21600,'1d':86400,'1w':604800};
export type BacktestTimezone = 'Asia/Kolkata' | 'UTC';

// datetime-local has no offset: always interpret it in the chosen report timezone,
// regardless of the browser's own timezone. Explicit ISO offsets are preserved.
export function parseBacktestTime(value: string, timezone: BacktestTimezone = 'UTC') {
 if (!value) return NaN;
 if (/(Z|[+-]\d{2}:\d{2})$/.test(value)) return Date.parse(value);
 const local = value.length === 10 ? value + 'T00:00' : value;
 return Date.parse(local + (timezone === 'Asia/Kolkata' ? '+05:30' : 'Z'));
}
export function backtestInputTime(millis: number, timezone: BacktestTimezone = 'Asia/Kolkata') {
 if (!Number.isFinite(millis)) return '';
 return new Date(millis + (timezone === 'Asia/Kolkata' ? 330 * 60000 : 0)).toISOString().slice(0,16);
}
export function latestBacktestEnd(resolution: string, timezone: BacktestTimezone, now = Date.now()) {
 // Minute precision is sufficient for every supported timeframe. Coverage is
 // reported from actual closed bars, including daily/weekly exchange alignment.
 const millis = Math.floor(now / 60000) * 60000;
 return backtestInputTime(millis, timezone);
}
export function backtestRange(start: string, end: string, resolution: string, now = Date.now(), timezone: BacktestTimezone = 'UTC') {
 const from = parseBacktestTime(start, timezone), to = parseBacktestTime(end, timezone), seconds = SECONDS[resolution];
 const maxDays = Math.floor(MAX_BACKTEST_CANDLES * seconds / 86400);
 const candles = Math.ceil((to - from) / (seconds * 1000));
 const error = !Number.isFinite(from) || !Number.isFinite(to) || !seconds ? 'Choose a valid start and end time.'
  : from >= to ? 'End time must be after the start time.'
  : to > now ? 'Choose an end time in the past.'
  : candles > MAX_BACKTEST_CANDLES ? `${resolution} supports up to ${maxDays} full days. Shorten the date range to run the backtest.` : '';
 return {candles, maxDays, error};
}
export function fitBacktestRange(start: string, end: string, resolution: string, timezone: BacktestTimezone = 'UTC') {
 const range = backtestRange(start, end, resolution, Date.now(), timezone);
 const millis = parseBacktestTime(end, timezone);
 return range.candles > MAX_BACKTEST_CANDLES && Number.isFinite(millis)
  ? backtestInputTime(millis - range.maxDays * DAY, timezone).slice(0, end.length === 10 ? 10 : 16) : start;
}
export function defaultBacktestPeriod(resolution: string, now = Date.now()) {
 const timezone: BacktestTimezone = 'Asia/Kolkata';
 const end = latestBacktestEnd(resolution, timezone, now).slice(0,10) + 'T00:00';
 const days = Math.max(6, Math.ceil(SECONDS[resolution] / 86400) * 2);
 const start = backtestInputTime(parseBacktestTime(end, timezone) - days * DAY, timezone);
 return {start:fitBacktestRange(start,end,resolution,timezone),end,timezone:timezone as BacktestTimezone,data_source:'testnet',capital:10000,fee_bps:5,slippage_bps:2};
}
