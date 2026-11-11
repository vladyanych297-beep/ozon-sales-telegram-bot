import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {quantity,money,supply,stockDays,sortedClusters,parseCost,rowProfit,margin,profitSummary,presetRange,daysRange,todayInZone,validRange,validateReport} from '../ozon_forecast/web/model.mjs';

// Synthetic values only. Never use a seller's financial response as a fixture.
const row = (extra={}) => ({product_id:'1',offer_id:'TEST-1',name:'Тестовый товар',ordered:10,redeemed:8,returned:2,revenue_cents:90000,fees_cents:10000,cost_cents:5000,average_fee_cents:1000,...extra});
const report = (extra={}) => ({date_from:'2026-01-01',date_to:'2026-01-02',timezone:'Europe/Moscow',fetched_at:'2026-01-02T18:00:00+03:00',stale:false,excluded_accruals:0,expenses_complete:true,unbound_expenses_cents:3000,unbound_expenses:[{name:'Расход',amount_cents:4000},{name:'Возврат расхода',amount_cents:-1000}],rows:[row()],...extra});
test('supply matches server ceil formula, selected sales period and large counts', () => {
  assert.equal(supply(30,20,3,2,15),35n);
  assert.equal(supply(1,29,1,0,30),0n);
  assert.equal(supply(0,30,10,10,30),0n);
  assert.equal(supply(10**12,365,0,0,1),365000000000000n);
  assert.equal(stockDays(30,5,15),'3'); // 2.5 rounds up, same as Android.
  assert.equal(stockDays(0,100,30),'—');
  assert.equal(stockDays(100,0,30),'0');
});
test('formatting and cost entry keep exact cents and zero', () => {
  assert.equal(quantity(1234567),'1 234 567');
  assert.equal(money(-100050),'−1 000,50 ₽');
  assert.equal(money(900719925474099123n),'9 007 199 254 740 991,23 ₽');
  assert.equal(parseCost('0'),0);
  assert.equal(parseCost('10,29'),1029);
  assert.equal(parseCost('1000000000.00'),100000000000);
  for(const text of ['','-1','1.001','1e3','1000000000.01','NaN']) assert.equal(parseCost(text),null,text);
});
test('cluster sort uses sales, stock, transit, hides FBS inventory and does not mutate', () => {
  const rows=[{name:'А',scheme:'FBO',sales:1,stock:0,in_transit:9},{name:'Б',scheme:'FBO',sales:1,stock:1,in_transit:0},{name:'В',scheme:'FBO',sales:1,stock:1,in_transit:2},{name:'Г',scheme:'FBO',sales:2,stock:0,in_transit:0},{name:'Склад продавца',scheme:'FBS',sales:99,stock:100,in_transit:1}];
  assert.deepEqual(sortedClusters(rows).map(r=>r.name),['Г','В','Б','А']);
  assert.equal(rows[0].name,'А');
});
test('financial total includes zero cost, excludes missing costs, deducts all net expenses', () => {
  const data=report({rows:[row(),row({product_id:'2',cost_cents:0}),row({product_id:'3',cost_cents:null})]});
  assert.equal(rowProfit(data.rows[0]),50000n);
  assert.equal(rowProfit(data.rows[1]),80000n);
  assert.equal(rowProfit(data.rows[2]),null);
  assert.deepEqual(profitSummary(data),{goods:130000n,expenses:3000n,total:127000n,complete:true,missing:1});
  assert.equal(margin(row()),'55,56 %');
  assert.equal(margin(row({cost_cents:20000})),'−44,44 %');
  assert.equal(margin(row({revenue_cents:0})),'—');
});
test('old report never supplies a complete earnings total', () => {
  const data=report(); delete data.expenses_complete; delete data.unbound_expenses; delete data.unbound_expenses_cents;
  validateReport('profit',data);
  assert.equal(profitSummary(data).complete,false);
  assert.equal(profitSummary(data).total,null);
});
test('dates include today in server timezone, leap days, invalid and future ranges', () => {
  assert.equal(todayInZone('Europe/Moscow',new Date('2026-10-19T22:30:00Z')),'2026-10-20');
  assert.deepEqual(daysRange(1,'2026-10-20'),{from:'2026-10-20',to:'2026-10-20'});
  assert.deepEqual(daysRange(2,'2024-03-01'),{from:'2024-02-29',to:'2024-03-01'});
  assert.equal(validRange('2026-01-01','2026-12-31','2026-12-31'),true);
  assert.equal(validRange('2026-01-01','2027-01-01','2027-01-01'),false);
  assert.equal(validRange('2026-02-30','2026-03-01'),false);
  assert.equal(validRange('2026-10-21','2026-10-21','2026-10-20'),false);
  assert.equal(daysRange(0),null);
});
test('malformed or inconsistent report rejected before replacing saved data', () => {
  validateReport('profit',report());
  for(const extra of [{rows:[row(),row()]},{rows:[row({cost_cents:undefined})]},{rows:[row({returned:99})]},{unbound_expenses_cents:0},{rows:[row({fees_cents:1.1})]},{stale:undefined}]) assert.throws(()=>validateReport('profit',report(extra)));
});
test('forecast validates actual returned periods and accepts a large supply count', () => {
  const data={schema_version:1,period_days:365,sales_period_days:1,sales_date_from:'2026-01-01',sales_date_to:'2026-01-01',timezone:'Europe/Moscow',fetched_at:'2026-01-01T18:00:00+03:00',stale:true,rows:[{product_id:'1',offer_id:'TEST',name:'Тест',skus:['100'],sales_30_days:1000000000000,stock:0,in_transit:0,to_supply:365000000000000,clusters:[]}]};
  validateReport('forecast',data);
  assert.throws(()=>validateReport('forecast',{...data,sales_period_days:30}));
  assert.throws(()=>validateReport('forecast',{...data,rows:[{...data.rows[0],stock:-1}]}));
});
test('service worker only intercepts shell files; no API or POST requests', () => {
  const handlers={}, location={href:'https://example.test/web/sw.js',origin:'https://example.test'};
  const context={URL,Set,self:{location,addEventListener:(name,fn)=>handlers[name]=fn}};
  vm.runInNewContext(fs.readFileSync(new URL('../ozon_forecast/web/sw.js',import.meta.url),'utf8'),context);
  for(const [path,method] of [['/api/v1/profitability','GET'],['/api/v1/profitability/cost','POST'],['/api/v1/forecast','GET'],['/web/.env','GET'],['/web/app.mjs?secret=test','GET'],['https://other.test/web/app.css','GET']]) {
    let intercepted=false;
    handlers.fetch({request:{url:new URL(path,location.href).href,method},respondWith:()=>intercepted=true});
    assert.equal(intercepted,false,path);
  }
});

test('quick periods include today and handle month/year/leap boundaries', () => {
  assert.deepEqual(presetRange('7','2026-01-03'), {from:'2025-12-28',to:'2026-01-03'});
  assert.deepEqual(presetRange('month','2026-01-03'), {from:'2026-01-01',to:'2026-01-03'});
  assert.deepEqual(presetRange('previous','2026-01-03'), {from:'2025-12-01',to:'2025-12-31'});
  assert.deepEqual(presetRange('previous','2024-03-01'), {from:'2024-02-01',to:'2024-02-29'});
  assert.equal(presetRange('unknown','2026-01-03'), null);
});