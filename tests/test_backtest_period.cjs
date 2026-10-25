const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/backtestPeriod.ts'), 'utf8');
const compiled = {exports:{}};
new Function('exports', 'module', ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText)(compiled.exports, compiled);
const {backtestRange, fitBacktestRange, defaultBacktestPeriod, parseBacktestTime, backtestInputTime} = compiled.exports;
const now = Date.parse('2026-10-25T10:00:00Z');

test('default dates run within the limit for every supported timeframe', () => {
 for (const resolution of ['1m','3m','5m','15m','30m','1h','2h','4h','6h','1d','1w']) {
  const {start,end,timezone} = defaultBacktestPeriod(resolution,now);
  const range = backtestRange(start,end,resolution,now,timezone);
  assert.equal(range.error,'');
  assert.ok(start.endsWith('T00:00'));assert.ok(end.endsWith('T00:00'));
  assert.ok(range.candles > 0 && range.candles <= 5000);
 }
});

test('switching six days from 5m to 1m preserves end date and fits three days', () => {
 assert.equal(backtestRange('2026-10-18','2026-10-24','5m',now).error,'');
 assert.equal(backtestRange('2026-10-18','2026-10-24','1m',now).candles,8640);
 const start = fitBacktestRange('2026-10-18','2026-10-24','1m');
 assert.equal(start,'2026-10-21');
 assert.equal(backtestRange(start,'2026-10-24','1m',now).candles,4320);
 assert.equal(backtestRange(start,'2026-10-24','1m',now).error,'');
 assert.equal(fitBacktestRange(start,'2026-10-24','1m'),start);
});

test('invalid dates are explained before submitting', () => {
 for (const [start,end] of [['','2026-10-24'],['2026-10-24','2026-10-24'],['2026-10-24','2026-10-23'],['2026-10-24','2026-10-26']]) {
  assert.ok(backtestRange(start,end,'1m',now).error);
 }
 assert.match(backtestRange('2026-10-18','2026-10-24','1m',now).error,/3 full days/);
});

test('IST inputs are independent of the browser timezone and preserve explicit ISO offsets', () => {
 const epoch = Date.parse('2026-10-25T07:35:00Z');
 assert.equal(parseBacktestTime('2026-10-25T13:05','Asia/Kolkata'),epoch);
 assert.equal(parseBacktestTime('2026-10-25T07:35','UTC'),epoch);
 assert.equal(parseBacktestTime('2026-10-25T07:35Z','Asia/Kolkata'),epoch);
 assert.equal(backtestInputTime(epoch,'Asia/Kolkata'),'2026-10-25T13:05');
 assert.equal(backtestInputTime(epoch,'UTC'),'2026-10-25T07:35');
 const period=defaultBacktestPeriod('5m',epoch);
 assert.equal(period.data_source,'testnet');assert.equal(period.timezone,'Asia/Kolkata');
 assert.equal(period.end,'2026-10-25T00:00');
 assert.ok(period.start.endsWith('T00:00'));
 assert.equal(backtestRange('2026-10-25T13:00','2026-10-25T13:05','5m',epoch,'Asia/Kolkata').candles,1);
 assert.equal(fitBacktestRange('2026-10-18T13:05','2026-10-25T13:05','1m','Asia/Kolkata'),'2026-10-22T13:05');
});
