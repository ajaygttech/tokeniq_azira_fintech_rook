const {test}=require('node:test');
const assert=require('node:assert/strict');
const ts=require('typescript');const fs=require('node:fs');const path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../app/backtestExport.ts'),'utf8');
const compiled={exports:{}};new Function('exports','module',ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText)(compiled.exports,compiled);
const {backtestReportCsv}=compiled.exports;
test('full report includes performance, settings, trades, IST dates and equity with a consistent CSV width',()=>{
 const csv=backtestReportCsv({id:4,source:'Delta Testnet',metrics:{return_pct:1.2,profit_factor:2,win_rate:60,max_drawdown:3},config:{quantity:100},trades:[{entry_time:1792886400,time:1792886700,pnl:-10}],equity:[{time:1792886700,equity:9990}]});
 assert.ok(csv.startsWith('\uFEFF'));assert.match(csv,/"metric","return_pct","1.2"/);assert.match(csv,/"setting","quantity","100"/);assert.match(csv,/"trade"/);assert.match(csv,/"equity"/);assert.match(csv,/entry_time_ist/);assert.match(csv,/05:30:00/);assert.match(csv,/,"-10"/);
 // Split only separators outside quoted cells, including formatted IST dates.
 const widths=csv.slice(1).split('\r\n').map(line=>line.split(/,(?=(?:[^"]*"[^"]*")*[^"]*$)/).length);assert.equal(new Set(widths).size,1);
});
test('CSV escapes commas, quotes and formula text while preserving negative numeric P&L and zero metrics',()=>{
 const csv=backtestReportCsv({id:0,metrics:{net_pnl:0,profit_factor:null},config:{name:'=SUM(A1)',description:'two, "quotes"'},trades:[{pnl:-2}],equity:[]});
 assert.match(csv,/"metric","net_pnl","0"/);assert.match(csv,/"metric","profit_factor",""/);assert.ok(csv.includes('"\'=SUM(A1)"'));assert.ok(csv.includes('"two, ""quotes"""'));assert.ok(csv.includes('"-2"'));
});
