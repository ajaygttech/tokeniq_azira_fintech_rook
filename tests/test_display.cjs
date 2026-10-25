const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const source = fs.readFileSync(path.join(__dirname, '../app/format.ts'), 'utf8');
const output = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText;
const compiled = {exports: {}};
new Function('exports', 'module', output)(compiled.exports, compiled);
const {timestampMillis, formatDate, formatNumber, depthLevels} = compiled.exports;

test('exchange seconds, milliseconds, microseconds and ISO timestamps display the same IST time', () => {
  const millis = Date.parse('2026-10-24T10:30:00Z');
  for (const value of [millis / 1000, millis, millis * 1000, String(millis), new Date(millis).toISOString()]) {
    assert.equal(timestampMillis(value), millis);
    assert.equal(formatDate(value), formatDate(millis));
  }
  assert.match(formatDate(millis), /04:00 pm/i);
});

test('missing amounts and invalid dates stay unavailable while zero is retained', () => {
  for (const value of [null, undefined, '', 'invalid']) assert.equal(formatDate(value), '—');
  for (const value of [null, undefined, '', NaN, Infinity]) assert.equal(formatNumber(value), '—');
  assert.equal(formatNumber(0), '0.00');
});

test('depth sorts by best price, merges duplicates and sums outwards from the spread', () => {
  const rows = [{price: 100, size: 3}, {price: 99, size: 2}, {price: 100, size: 1}];
  assert.deepEqual(depthLevels(rows, 'buy'), [{price: 100, size: 4, total: 4}, {price: 99, size: 2, total: 6}]);
  assert.deepEqual(depthLevels(rows, 'sell'), [{price: 99, size: 2, total: 2}, {price: 100, size: 4, total: 6}]);
});

test('invalid and zero depth levels cannot poison totals; only nearest seven levels display', () => {
  assert.deepEqual(depthLevels([null, {}, {price: Infinity, size: 1}, {price: 1, size: Infinity}, {price: 2, size: 0}, {price: -1, size: 3}], 'buy'), []);
  assert.deepEqual(depthLevels(undefined, 'sell'), []);
  const rows = Array.from({length: 10}, (_, i) => ({price: i + 1, size: 1}));
  const levels = depthLevels(rows, 'buy');
  assert.equal(levels.length, 7);
  assert.equal(levels[0].price, 10);
  assert.equal(levels.at(-1).total, 7);
});

const positionSource = fs.readFileSync(path.join(__dirname, '../app/positions.ts'), 'utf8');
const positionOutput = ts.transpileModule(positionSource, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
const positionModule = {exports:{}};
new Function('exports','module',positionOutput)(positionModule.exports,positionModule);
const {markPosition} = positionModule.exports;
const products = [{id:84,symbol:'BTCUSD',notional_type:'vanilla',contract_value:'0.001'}];
const position = {product_id:84,size:10,entry_price:100,unrealized_pnl:99};
test('websocket mark prices value signed contracts for each instrument',()=>{
  const marks={BTCUSD:{price:120,time:10000},ETHUSD:{price:999,time:10000}};
  assert.equal(markPosition(position,products,marks,11000).unrealized_pnl,0.2);
  assert.equal(markPosition({...position,size:-10},products,marks,11000).unrealized_pnl,-0.2);
  assert.equal(markPosition(position,products,{BTCUSD:{price:90,time:10000}},11000).unrealized_pnl,-0.1);
});
test('stale, missing, unsupported or invalid mark inputs retain exchange P&L',()=>{
  for(const marks of [{},{BTCUSD:{price:120,time:1000}},{BTCUSD:{price:NaN,time:20000}}])
    assert.equal(markPosition(position,products,marks,20000).unrealized_pnl,99);
  assert.equal(markPosition(position,[],{BTCUSD:{price:120,time:20000}},20000).unrealized_pnl,99);
  assert.equal(markPosition(position,[{...products[0],notional_type:'inverse'}],{BTCUSD:{price:120,time:20000}},20000).unrealized_pnl,99);
});
