import {METRICS, quantity, money, supply, stockDays, sortedClusters, parseCost, rowProfit, margin, profitSummary, todayInZone, daysRange, validRange, validateReport, presetRange} from './model.mjs';
import {readReport, writeReport} from './storage.mjs';

const $ = id => document.getElementById(id);
const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const states = {forecast: {report:null, cached:false, busy:false}, profit: {report:null, cached:false, busy:false}};
let savingCost = false, editingId = null, waitingWorker = null;
let metrics = new Set(Object.keys(METRICS));
try {
  const saved = JSON.parse(localStorage.getItem('varanez-metrics'));
  if (Array.isArray(saved)) metrics = new Set(saved.filter(key => key in METRICS));
} catch { /* Browser storage can be unavailable; defaults remain usable. */ }
const dateLabel = value => value.split('-').reverse().join('.');
function timestamp(value, timezone = 'Europe/Moscow') {
  return new Intl.DateTimeFormat('ru-RU', {timeZone: timezone, dateStyle:'short', timeStyle:'short'}).format(new Date(value));
}
function storageWarning(message = 'Не удалось сохранить отчёт на устройстве. Он доступен в этом окне; просмотр после закрытия без сети не гарантирован.') {
  $('storage-warning').textContent = message; $('storage-warning').hidden = false;
}
function message(id, text) { $(id).textContent = text; $(id).hidden = !text; }
function valueMetric(label, value, nonzero = false) {
  return `<div><dt>${escape(label)}</dt><dd${nonzero ? ' class="nonzero"' : ''}>${escape(value)}</dd></div>`;
}
function reportMeta(kind) {
  const {report:r, cached} = states[kind];
  const from = kind === 'forecast' ? r.sales_date_from : r.date_from;
  const to = kind === 'forecast' ? r.sales_date_to : r.date_to;
  const aged = Date.now() - Date.parse(r.fetched_at) > 300000;
  const stale = cached || r.stale || aged || !navigator.onLine;
  return `<div class="report-meta"><strong>${kind === 'forecast' ? 'Продажи' : 'Отчёт'}: ${dateLabel(from)} — ${dateLabel(to)}</strong>${kind === 'forecast' ? ` · прогноз ${quantity(r.period_days)} дн.` : ''}<br>Данные получены ${escape(timestamp(r.fetched_at, r.timezone))} · ${escape(r.timezone)}${stale ? '<div class="notice warning">Сохранённый отчёт. Свежесть данных не подтверждена — нажмите «Обновить» при наличии сети.</div>' : ''}${r.warning ? `<div class="notice warning">${escape(r.warning)}</div>` : ''}${r.cost_saved_at ? `<br>Себестоимость сохранена ${escape(timestamp(r.cost_saved_at, r.timezone))}.` : ''}</div>`;
}
function empty(title, text) { return `<div class="empty"><strong>${escape(title)}</strong>${escape(text)}</div>`; }
function renderForecast() {
  const {report:r} = states.forecast;
  $('forecast-tools').hidden = !r;
  if (!r) {
    $('forecast-report').innerHTML = empty('Начните с периода продаж', 'Выберите периоды и нажмите «Обновить». Здесь появятся ваши товары и план поставок.');
    return;
  }
  const sum = key => r.rows.reduce((total, row) => total + BigInt(row[key]), 0n);
  $('forecast-report').innerHTML = `${reportMeta('forecast')}<dl class="overview">${valueMetric('К поставке, шт.', quantity(sum('to_supply')))}${valueMetric('Продажи, шт.', quantity(sum('sales_30_days')))}${valueMetric('Остаток FBO, шт.', quantity(sum('stock')))}</dl>`;
  renderForecastList();
}
function renderForecastList() {
  const r = states.forecast.report;
  if (!r) return;
  const open = new Set([...$('forecast-list').querySelectorAll('details[open]')].map(el => el.dataset.id));
  const query = $('forecast-search').value.trim().toLocaleLowerCase('ru');
  const only = $('supply-only').checked;
  const rows = r.rows.filter(row => (!only || row.to_supply > 0) && `${row.name} ${row.offer_id} ${row.skus.join(' ')}`.toLocaleLowerCase('ru').includes(query));
  $('forecast-count').textContent = `Товары · ${quantity(rows.length)} из ${quantity(r.rows.length)}`;
  $('forecast-list').innerHTML = rows.map(row => {
    const clusters = sortedClusters(row.clusters);
    return `<details class="product" data-id="${row.product_id}"${open.has(row.product_id) ? ' open' : ''}><summary><div><h3>${escape(row.name)}</h3><p class="article">${escape(row.offer_id)} · SKU ${escape(row.skus.join(', '))}</p><span class="expand-label">Кластеры: ${quantity(clusters.length)} <span class="chevron" aria-hidden="true">⌄</span></span></div><div class="product-aside"><strong>${quantity(row.to_supply)}</strong><small>к поставке, шт.</small></div></summary><div class="product-body"><dl class="metrics total-metrics">${valueMetric('Продажи FBO + FBS', quantity(row.sales_30_days), row.sales_30_days !== 0)}${valueMetric('Остаток FBO', quantity(row.stock), row.stock !== 0)}${valueMetric('В пути FBO', quantity(row.in_transit), row.in_transit !== 0)}${valueMetric('Запас, дней', stockDays(row.sales_30_days, row.stock, r.sales_period_days))}</dl>${clusters.map(c => {
      const values = {sales:quantity(c.sales), stock:quantity(c.stock), in_transit:quantity(c.in_transit), to_supply:quantity(supply(c.sales,r.period_days,c.stock,c.in_transit,r.sales_period_days)), stock_days:stockDays(c.sales,c.stock,r.sales_period_days)};
      return `<section class="cluster"><h4>${escape(c.name)}</h4><dl class="metrics">${Object.keys(METRICS).filter(key => metrics.has(key)).map(key => valueMetric(METRICS[key], values[key], ['sales','stock','in_transit'].includes(key) && c[key] !== 0)).join('')}</dl></section>`;
    }).join('')}${!clusters.length ? '<p class="hint">В отчёте нет данных по кластерам для этого товара.</p>' : !metrics.size ? '<p class="hint">Показатели скрыты. Выберите их в настройках над списком.</p>' : ''}<p class="hint">Запас = текущий остаток ÷ средние продажи за день. «—» означает, что продаж не было.</p></div></details>`;
  }).join('') || empty('Товаров не найдено', 'Измените поиск или отключите фильтр «Только к поставке».');
}
function renderProfit() {
  const {report:r} = states.profit;
  $('profit-tools').hidden = !r;
  if (!r) {
    $('profit-report').innerHTML = empty('Узнайте результат за период', 'Нажмите «Обновить», чтобы получить начисления и списания. Себестоимость общая для всех пользователей.');
    return;
  }
  const total = profitSummary(r);
  $('profit-report').innerHTML = `${reportMeta('profit')}${r.accounting_basis !== 'order_cash_v2' ? '<div class="notice warning">Сохранён отчёт с другим принципом расчёта. Нажмите «Обновить».</div>' : ''}<section class="profit-overview" aria-label="Заработано за период"><p class="eyebrow">ЗАРАБОТАНО ЗА ПЕРИОД</p><p class="earned">${total.complete ? money(total.total) : 'Не рассчитано'}</p><div class="profit-breakdown"><div class="breakdown-row"><span>Прибыль товаров</span><strong>${money(total.goods)}</strong></div><div class="breakdown-row"><span>Общие расходы Ozon</span><strong>${money(total.expenses)}</strong></div></div>${total.complete ? `<details class="expense-detail"><summary>Расходы по видам · ${quantity(r.unbound_expenses.length)}</summary>${r.unbound_expenses.map(e => `<div class="breakdown-row"><span>${escape(e.name)}</span><strong>${money(e.amount_cents)}</strong></div>`).join('') || '<p>Общих расходов за период нет.</p>'}</details><p class="hint">Общие расходы вычтены полностью по дате начисления за период. Возвраты расходов уменьшают сумму.</p>` : '<div class="notice warning">Неполный отчёт: общие расходы не загружены. Нажмите «Обновить» — итог пока не рассчитан.</div>'}<p class="hint">Себестоимость задана: ${quantity(r.rows.length-total.missing)} из ${quantity(r.rows.length)}. Без себестоимости: ${quantity(total.missing)}. Прибыль этих товаров не включена. Поиск не влияет на итог.</p><p class="hint">Налоги и внешние расходы не включены.</p></section><details class="metric-settings"><summary>Как считаются показатели</summary><p class="hint">В карточках удержания только с точной связью с заказом и SKU. Исключено из карточек строк начислений: ${quantity(r.excluded_accruals)}. Общие расходы учитываются отдельно, без повторного вычитания начислений заказа.</p><p class="hint">Выкуп и возврат определяются по начислению и возврату комиссии за продажу. У свежих заказов начисления ещё могут отсутствовать. Заказы отбираются по дате создания; начисления по ним учитываются до дня загрузки, включая операции после конца выбранного периода. Средние удержания — на заказанную единицу. Порог стоимости = средние удержания + себестоимость.</p></details>`;
  renderProfitList();
}
function renderProfitList() {
  const r = states.profit.report;
  if (!r) return;
  const query = $('profit-search').value.trim().toLocaleLowerCase('ru');
  const rows = r.rows.filter(row => `${row.name} ${row.offer_id}`.toLocaleLowerCase('ru').includes(query));
  $('profit-count').textContent = `Товары · ${quantity(rows.length)} из ${quantity(r.rows.length)}`;
  $('profit-list').innerHTML = rows.map(row => {
    const profit = rowProfit(row), threshold = row.cost_cents == null || row.average_fee_cents == null ? null : BigInt(row.cost_cents) + BigInt(row.average_fee_cents);
    return `<article class="product profit-card"><h3>${escape(row.name)}</h3><p class="article">${escape(row.offer_id)}</p><dl class="metrics">${valueMetric('Заказано', quantity(row.ordered))}${valueMetric('Выкуплено', quantity(row.redeemed))}${valueMetric('Возвращено', quantity(row.returned))}${valueMetric('Выручка с возвратами', money(row.revenue_cents))}${valueMetric('Оплата покупателей', money(row.buyer_paid_cents))}${valueMetric('Доплаты Ozon', money(row.ozon_subsidy_cents))}${valueMetric('Удержания Ozon', money(row.fees_cents))}${valueMetric('Средние удержания / шт.', money(row.average_fee_cents))}</dl><button class="cost-button" data-cost="${row.product_id}"${states.profit.busy || savingCost ? ' disabled' : ''}><span>Себестоимость: <strong>${row.cost_cents == null ? 'не задана' : money(row.cost_cents)}</strong></span><span>Изменить ↗</span></button><p class="hint">Порог стоимости: <strong>${money(threshold)}</strong></p><div class="result-line"><div><span>Прибыль</span><strong class="${profit == null ? '' : profit < 0n ? 'negative' : 'positive'}">${profit == null ? 'Задайте себестоимость' : money(profit)}</strong></div><div><span>Маржа</span><strong>${margin(row)}</strong></div></div></article>`;
  }).join('') || empty('Товаров не найдено', 'Измените название или артикул в поиске.');
}
function syncControls() {
  for (const kind of ['forecast','profit']) {
    const form = $(`${kind}-form`);
    form.querySelector('button[type=submit]').disabled = states[kind].busy || savingCost;
    form.querySelector('button[type=submit]').textContent = states[kind].busy ? 'Обновляем…' : 'Обновить';
    form.querySelectorAll('input').forEach(input => { input.disabled = states[kind].busy || savingCost; });
    $(`${kind}-loading`).hidden = !states[kind].busy;
    $(`${kind}-panel`).setAttribute('aria-busy', String(states[kind].busy));
  }
  syncPeriod();
  document.querySelectorAll('[data-cost]').forEach(el => {el.disabled = savingCost || states.profit.busy;});
}
function currentRange() {
  const today = todayInZone(states.profit.report?.timezone ?? 'Europe/Moscow');
  return document.querySelector('[name=mode]:checked').value === 'days' ? daysRange(Number($('profit-days').value), today) : {from:$('date-from').value, to:$('date-to').value};
}
function syncPeriod() {
  document.querySelectorAll('[data-period]').forEach(button => {button.disabled = states.profit.busy || savingCost;});
  const dates = document.querySelector('[name=mode]:checked').value === 'dates';
  $('profit-days-field').hidden = dates; $('profit-dates-fields').hidden = !dates;
  $('profit-days').disabled = dates || states.profit.busy || savingCost;
  $('date-from').disabled = $('date-to').disabled = !dates || states.profit.busy || savingCost;
  const today = todayInZone(states.profit.report?.timezone ?? 'Europe/Moscow');
  $('date-from').max = $('date-to').max = today;
  const range = currentRange();
  $('profit-range').textContent = range && validRange(range.from, range.to, today) ? `${dateLabel(range.from)} — ${dateLabel(range.to)} · обе даты включены` : 'Выберите от 1 до 365 дней, не позднее сегодня.';
}
$('period-presets').addEventListener('click', event => {
  const button = event.target.closest('[data-period]');
  if (!button || states.profit.busy || savingCost) return;
  const preset = button.dataset.period;
  const range = presetRange(preset, todayInZone(states.profit.report?.timezone ?? 'Europe/Moscow'));
  if (!range) return;
  const days = /^\d+$/.test(preset);
  document.querySelector(`[name=mode][value=${days ? 'days' : 'dates'}]`).checked = true;
  if (days) $('profit-days').value = preset;
  $('date-from').value = range.from; $('date-to').value = range.to;
  syncPeriod();
});
async function request(path, options = {}, timeout = 210000) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {...options, cache:'no-store', credentials:'same-origin', signal:controller.signal, headers:{Accept:'application/json', ...options.headers}});
    let body;
    try { body = await response.json(); } catch { throw new Error('Сервер вернул неожиданный ответ. Сохранённый отчёт доступен ниже.'); }
    if (!response.ok) {
      const error = new Error(typeof body.detail === 'string' ? body.detail.slice(0,600) : `Ошибка сервера (${response.status}). Повторите по кнопке «Обновить».`);
      error.status = response.status; throw error;
    }
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Сервер не ответил вовремя. Последний отчёт сохранён. Повторите обновление позже.');
    if (error instanceof TypeError) throw new Error('Не удалось связаться с сервером. Проверьте подключение. Последний отчёт сохранён.');
    throw error;
  } finally { clearTimeout(timer); }
}
async function profitabilityReport(path) {
  const params = new URL(path,location.origin).searchParams;
  const query = new URLSearchParams({date_from:params.get('date_from'),date_to:params.get('date_to')});
  if (params.get('full_reload') === 'true') query.set('full_reload','true');
  let started;
  try {started = await request(`/api/v1/profitability/jobs?${query}`,{method:'POST'},20000);}
  catch (error) {
    // Backward compatibility for a 0.7.6 server; only after explicit Refresh.
    if (error.status === 404 || error.status === 405) return request(path,{},1500000);
    throw error;
  }
  if (typeof started.job_id !== 'string' || !/^[a-f0-9]{32}$/.test(started.job_id)) throw new Error('Сервер не подтвердил фоновую загрузку. Предыдущий отчёт сохранён.');
  const deadline = Date.now()+1800000;
  while (Date.now()<deadline) {
    const status = await request(`/api/v1/profitability/jobs/${started.job_id}`,{},20000);
    if (status.status === 'complete') return status.report;
    if (status.status === 'error') throw new Error(typeof status.error === 'string' ? status.error : 'Не удалось сформировать отчёт.');
    if (status.status !== 'running') throw new Error('Неизвестный статус загрузки. Повторите обновление позже.');
    $('profit-loading').lastElementChild.textContent = `${typeof status.progress === 'string' ? status.progress : 'Получаем заказы и начисления…'} При ограничениях Ozon сервер подождёт и продолжит.`;
    await new Promise(resolve => setTimeout(resolve,3000));
  }
  throw new Error('Загрузка продолжается дольше ожидаемого. Повторите «Обновить» позже — сервер сохраняет промежуточный результат.');
}
async function refresh(kind, path) {
  const state = states[kind];
  if (state.busy || savingCost) return;
  state.busy = true; message(`${kind}-error`, ''); syncControls();
  if (kind === 'profit') $('profit-loading').lastElementChild.textContent = 'Запускаем загрузку начислений, списаний и общих расходов…';
  try {
    const report = validateReport(kind, await (kind === 'profit' ? profitabilityReport(path) : request(path)));
    state.report = report; state.cached = false;
    try { await writeReport(kind, report); } catch { storageWarning(); }
  } catch (error) { state.cached = true; message(`${kind}-error`, error.message); }
  finally { state.busy = false; syncControls(); kind === 'forecast' ? renderForecast() : renderProfit(); }
}
$('forecast-form').addEventListener('submit', event => {
  event.preventDefault();
  const sales = Number($('sales-days').value), days = Number($('forecast-days').value);
  if (![sales,days].every(n => Number.isInteger(n) && n >= 1 && n <= 365)) { message('forecast-error','Укажите целое число дней от 1 до 365.'); return; }
  refresh('forecast', `/api/v1/forecast?days=${days}&sales_days=${sales}`);
});
$('profit-form').addEventListener('submit', event => {
  event.preventDefault(); syncPeriod();
  const range = currentRange(), today = todayInZone(states.profit.report?.timezone ?? 'Europe/Moscow');
  if (!range || !validRange(range.from,range.to,today)) { message('profit-error','Выберите от 1 до 365 дней, не позднее сегодня.'); return; }
  refresh('profit', `/api/v1/profitability?date_from=${range.from}&date_to=${range.to}&refresh=true&full_reload=${event.submitter?.name === 'full-reload'}`);
});
$('profit-form').addEventListener('input', syncPeriod);
document.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => {
  const tab = button.dataset.tab;
  document.querySelectorAll('[data-tab]').forEach(el => { if (el === button) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current'); });
  $('forecast-panel').hidden = tab !== 'forecast'; $('profit-panel').hidden = tab !== 'profit';
  document.title = `${tab === 'forecast' ? 'План поставок' : 'Рентабельность'} · VaraneZ`;
}));
$('forecast-search').addEventListener('input',renderForecastList);
$('supply-only').addEventListener('change',renderForecastList);
$('profit-search').addEventListener('input',renderProfitList);
$('metrics').innerHTML = Object.entries(METRICS).map(([key,label]) => `<label><input type="checkbox" value="${key}"${metrics.has(key) ? ' checked' : ''}>${label}</label>`).join('');
$('metrics').addEventListener('change', () => {
  metrics = new Set([...$('metrics').querySelectorAll('input:checked')].map(input => input.value));
  try {localStorage.setItem('varanez-metrics', JSON.stringify([...metrics]));} catch { /* Preference only. */ }
  renderForecastList();
});
$('profit-list').addEventListener('click', event => {
  const button = event.target.closest('[data-cost]');
  if (!button || states.profit.busy || savingCost) return;
  const row = states.profit.report.rows.find(r => r.product_id === button.dataset.cost);
  editingId = row.product_id;
  $('cost-product').textContent = `${row.name} · ${row.offer_id}`;
  $('cost-value').value = row.cost_cents == null ? '' : `${Math.floor(row.cost_cents/100)},${String(row.cost_cents%100).padStart(2,'0')}`;
  message('cost-error',''); $('cost-dialog').showModal(); $('cost-value').focus();
});
$('cost-cancel').addEventListener('click', () => { if (!savingCost) $('cost-dialog').close(); });
$('cost-dialog').addEventListener('cancel', event => { if (savingCost) event.preventDefault(); });
$('cost-form').addEventListener('submit', async event => {
  event.preventDefault();
  const cost = parseCost($('cost-value').value), id = editingId;
  if (cost == null) {message('cost-error','Введите сумму от 0 до 1 000 000 000 ₽, не более двух знаков после запятой.'); return;}
  if (savingCost || states.profit.busy) return;
  savingCost = true; message('cost-error',''); syncControls();
  $('cost-save').disabled = $('cost-cancel').disabled = $('cost-value').disabled = true;
  $('cost-save').textContent = 'Сохраняем на сервере…';
  try {
    const result = await request('/api/v1/profitability/cost',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({product_id:id,cost_cents:cost})},20000);
    if (result.product_id !== id || result.cost_cents !== cost) throw new Error('Сервер не подтвердил значение. Повторите сохранение.');
    const report = states.profit.report;
    report.rows.find(row => row.product_id === id).cost_cents = cost;
    report.cost_saved_at = new Date().toISOString();
    try {await writeReport('profit',report);} catch {storageWarning();}
    $('cost-dialog').close(); renderProfit();
  } catch (error) {message('cost-error',`Сохранение не подтверждено. ${error.message}`);}
  finally {
    savingCost = false; $('cost-save').disabled = $('cost-cancel').disabled = $('cost-value').disabled = false;
    $('cost-save').textContent = 'Сохранить для всех'; syncControls();
  }
});
for (const id of ['install-open','install-footer']) $(id).addEventListener('click', () => $('install-dialog').showModal());
$('install-close').addEventListener('click', () => $('install-dialog').close());
function networkState() {
  $('offline').hidden = navigator.onLine;
  if (!navigator.onLine) {states.forecast.cached = states.profit.cached = true; renderForecast(); renderProfit();}
}
window.addEventListener('online',networkState); window.addEventListener('offline',networkState);
document.addEventListener('visibilitychange', () => {if (!document.hidden) {syncPeriod(); renderForecast(); renderProfit();}});
// Age the visible timestamp without refreshing reports or touching open cards.
setInterval(() => {
  if (!document.hidden) for (const kind of ['forecast','profit']) {
    const meta = $(`${kind}-report`).querySelector('.report-meta');
    if (meta && states[kind].report) meta.outerHTML = reportMeta(kind);
  }
},60000);
// Keep dialogs inside the visible area when the iPhone software keyboard opens.
function viewport() {document.documentElement.style.setProperty('--visible-height',`${window.visualViewport?.height ?? innerHeight}px`);}
window.visualViewport?.addEventListener('resize',viewport); viewport();
async function initialize() {
  for (const kind of ['forecast','profit']) {
    try {
      const saved = await readReport(kind);
      if (saved && !states[kind].report) {
        states[kind].report = validateReport(kind,saved.report); states[kind].cached = true;
        const r = states[kind].report;
        if (kind === 'forecast') {$('sales-days').value=r.sales_period_days; $('forecast-days').value=r.period_days;}
        else {$('date-from').value=r.date_from; $('date-to').value=r.date_to; document.querySelector('[name=mode][value=dates]').checked=true;}
      }
    } catch {storageWarning('Сохранённый отчёт недоступен. Нажмите «Обновить» при наличии сети; новый отчёт появится после успешной загрузки.');}
  }
  const initial = daysRange(30);
  if (!$('date-from').value) {$('date-from').value=initial.from; $('date-to').value=initial.to;}
  syncControls(); renderForecast(); renderProfit(); networkState();
  // Installing/updating the shell never requests reports or contacts Ozon.
  if ('serviceWorker' in navigator && window.isSecureContext) {
    try {
      const registration = await navigator.serviceWorker.register('./sw.js',{scope:'./',updateViaCache:'none'});
      const ready = worker => {waitingWorker=worker; $('sw-update').hidden=false;};
      if (registration.waiting) ready(registration.waiting);
      registration.addEventListener('updatefound', () => {
        const worker=registration.installing;
        worker?.addEventListener('statechange', () => {if (worker.state === 'installed' && navigator.serviceWorker.controller) ready(worker);});
      });
    } catch {storageWarning('Офлайн-запуск интерфейса не настроен. Проверьте HTTPS и откройте приложение ещё раз при наличии сети.');}
  }
}
$('sw-apply').addEventListener('click', () => {
  if (savingCost || states.forecast.busy || states.profit.busy || $('cost-dialog').open) {storageWarning('Завершите обновление или редактирование себестоимости, затем установите новую версию интерфейса.'); return;}
  navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(),{once:true});
  waitingWorker?.postMessage('ACTIVATE');
});
renderForecast(); renderProfit();
initialize();
