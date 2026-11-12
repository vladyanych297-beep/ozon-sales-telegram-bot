// Integer arithmetic mirrors the Python/Android contracts. No Ozon calls here.
export const METRICS = {sales: 'Продажи', stock: 'Остаток', in_transit: 'В пути', to_supply: 'К поставке', stock_days: 'Запас, дней'};
const integer = (v, min = 0, max = Number.MAX_SAFE_INTEGER) => Number.isSafeInteger(v) && v >= min && v <= max;
const cents = v => integer(v, -Number.MAX_SAFE_INTEGER);
const dateValid = v => typeof v === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(v) && Number.isFinite(Date.parse(v)) && new Date(v).toISOString().slice(0, 10) === v;
export function quantity(value) { return BigInt(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }
export function money(value) {
  if (value == null) return '—';
  const n = BigInt(value), absolute = n < 0n ? -n : n;
  return `${n < 0n ? '−' : ''}${quantity(absolute / 100n)},${String(absolute % 100n).padStart(2, '0')} ₽`;
}
export function supply(sales, days, stock, transit, salesDays) {
  const result = (BigInt(sales) * BigInt(days) + BigInt(salesDays) - 1n) / BigInt(salesDays) - BigInt(stock) - BigInt(transit);
  return result > 0n ? result : 0n;
}
export function stockDays(sales, stock, days) {
  if (!sales) return '—';
  return quantity((BigInt(stock) * BigInt(days) * 2n + BigInt(sales)) / (BigInt(sales) * 2n));
}
export function sortedClusters(rows) {
  // Server 0.7.6 merges FBO+FBS destination sales into FBO rows. Never show FBS inventory.
  return rows.filter(r => r.scheme === 'FBO').sort((a, b) => b.sales - a.sales || b.stock - a.stock || b.in_transit - a.in_transit || a.name.localeCompare(b.name, 'ru'));
}
export function parseCost(value) {
  const clean = value.trim().replace(',', '.');
  if (!/^\d+(?:\.\d{1,2})?$/.test(clean)) return null;
  const [rubles, fraction = ''] = clean.split('.');
  const result = BigInt(rubles) * 100n + BigInt(fraction.padEnd(2, '0'));
  return result <= 100000000000n ? Number(result) : null;
}
export function minimumPrice(cost, target, basis) {
  if (!integer(cost, 1, 100000000000) || !integer(target, 0, 100000000000) || basis?.complete !== true || !integer(basis.units, 1) || !integer(basis.overhead_units, 1) || !cents(basis.fixed_cents) || !cents(basis.overhead_cents)) return null;
  const fraction = value => {
    if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return null;
    const [whole, part=''] = value.split('.');
    return [BigInt(whole+part), 10n**BigInt(part.length)];
  };
  const c=fraction(basis.commission_rate), a=fraction(basis.acquiring_rate);
  if (!c || !a) return null;
  const scale=c[1]*a[1], remaining=scale-c[0]*a[1]-a[0]*c[1];
  if (remaining<=0n) return null;
  const n=BigInt(basis.units), h=BigInt(basis.overhead_units);
  const numerator=((BigInt(cost)+BigInt(target))*n*h+BigInt(basis.fixed_cents)*h+BigInt(basis.overhead_cents)*n)*scale;
  const denominator=100n*n*h*remaining;
  return numerator>0n ? (numerator+denominator-1n)/denominator : null;
}
export function rowProfit(row) {
  return row.cost_cents == null ? null : BigInt(row.revenue_cents) - BigInt(row.fees_cents) - BigInt(row.cost_cents) * BigInt(row.redeemed - row.returned);
}
export function margin(row) {
  const profit = rowProfit(row);
  if (profit == null || row.revenue_cents <= 0) return '—';
  const scaled = profit * 10000n, revenue = BigInt(row.revenue_cents);
  const rounded = ((scaled < 0n ? -scaled : scaled) + revenue / 2n) / revenue;
  return `${scaled < 0n ? '−' : ''}${quantity(rounded / 100n)},${String(rounded % 100n).padStart(2, '0')} %`;
}
export function profitSummary(report) {
  const goods = report.rows.reduce((sum, row) => sum + (rowProfit(row) ?? 0n), 0n);
  const complete = report.expenses_complete === true && cents(report.unbound_expenses_cents) && Array.isArray(report.unbound_expenses);
  return {goods, complete, expenses: complete ? BigInt(report.unbound_expenses_cents) : null,
    total: complete ? goods - BigInt(report.unbound_expenses_cents) : null,
    missing: report.rows.filter(row => row.cost_cents == null).length};
}
export function todayInZone(timezone = 'Europe/Moscow', now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(now);
  const value = key => parts.find(p => p.type === key).value;
  return `${value('year')}-${value('month')}-${value('day')}`;
}
export function daysRange(days, today = todayInZone()) {
  if (!integer(days, 1, 365) || !dateValid(today)) return null;
  return {from: new Date(Date.parse(today) - (days - 1) * 86400000).toISOString().slice(0, 10), to: today};
}
export function validRange(from, to, today = todayInZone()) {
  return dateValid(from) && dateValid(to) && from <= to && to <= today && (Date.parse(to) - Date.parse(from)) / 86400000 < 365;
}
export function presetRange(preset, today = todayInZone()) {
  if (!dateValid(today)) return null;
  if (/^\d+$/.test(String(preset))) return daysRange(Number(preset), today);
  const first = today.slice(0, 7) + '-01';
  if (preset === 'month') return {from: first, to: today};
  if (preset === 'previous') {
    const last = new Date(Date.parse(first) - 86400000).toISOString().slice(0, 10);
    return {from: last.slice(0, 7) + '-01', to: last};
  }
  return null;
}
export function validateReport(kind, data) {
  const invalid = () => { throw new Error('Сервер вернул неполный или неподдерживаемый отчёт. Предыдущий отчёт сохранён.'); };
  if (!data || !Array.isArray(data.rows) || !Number.isFinite(Date.parse(data.fetched_at)) || typeof data.stale !== 'boolean' || typeof data.timezone !== 'string') invalid();
  try { todayInZone(data.timezone); } catch { invalid(); }
  if (data.warning != null && typeof data.warning !== 'string') invalid();
  const ids = new Set();
  for (const row of data.rows) {
    if (!row || typeof row.product_id !== 'string' || !/^\d{1,80}$/.test(row.product_id) || ids.has(row.product_id) || typeof row.name !== 'string' || typeof row.offer_id !== 'string') invalid();
    ids.add(row.product_id);
    if (kind === 'forecast') {
      if (!Array.isArray(row.skus) || !row.skus.every(s => typeof s === 'string') || !['sales_30_days', 'stock', 'in_transit'].every(k => integer(row[k], 0, 1000000000000)) || !integer(row.to_supply) || !Array.isArray(row.clusters)) invalid();
      for (const c of row.clusters) if (!c || typeof c.name !== 'string' || !['FBO', 'FBS'].includes(c.scheme) || !['sales', 'stock', 'in_transit'].every(k => integer(c[k], 0, 1000000000000))) invalid();
    } else {
      if (!['ordered', 'redeemed', 'returned'].every(k => integer(row[k])) || (data.accounting_basis !== "accrual_date_v1" && (row.returned > row.redeemed || row.redeemed > row.ordered)) || !cents(row.revenue_cents) || !cents(row.fees_cents) || (row.cost_cents !== null && !integer(row.cost_cents, 0, 100000000000)) || (row.average_fee_cents !== null && !cents(row.average_fee_cents))) invalid();
    }
  }
  if (kind === 'forecast') {
    if (data.schema_version !== 1 || !integer(data.period_days, 1, 365) || !integer(data.sales_period_days, 1, 365) || !dateValid(data.sales_date_from) || !dateValid(data.sales_date_to) || (Date.parse(data.sales_date_to) - Date.parse(data.sales_date_from)) / 86400000 + 1 !== data.sales_period_days) invalid();
  } else {
    if (!validRange(data.date_from, data.date_to, '9999-12-31') || !integer(data.excluded_accruals)) invalid();
    if (data.expenses_complete === true) {
      if (!cents(data.unbound_expenses_cents) || !Array.isArray(data.unbound_expenses) || !data.unbound_expenses.every(e => e && typeof e.name === 'string' && cents(e.amount_cents)) || data.unbound_expenses.reduce((s, e) => s + BigInt(e.amount_cents), 0n) !== BigInt(data.unbound_expenses_cents)) invalid();
    }
  }
  return data;
}
