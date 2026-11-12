"""Forward price inputs, separate from historical accounting profit.

Only read endpoints are used. Amounts are integer kopecks; fractional means
are represented as totals/divisors so clients can use exact arithmetic.
"""
from collections import defaultdict
from decimal import Decimal, ROUND_CEILING

from .profitability import rubles
from ozon_sales_bot.ozon import OzonApiError

EXCLUDED_OFFERS = frozenset({'KD001', 'HR001', 'SK001'})
FIXED = frozenset({'Drop-Off', 'Drop-Off Agent', 'LastMile', 'LastMileCourier',
    'LastMilePickUpPoint', 'Logistic', 'DeliveryToHandoverPlaceByOzon'})
IGNORED = frozenset({'StarsMembership', 'CustomerReviews', 'PackageCost',
    'PackingFee', 'ItemPacking', 'ItemSealing', 'PackmanCisPacking'})


def minimum_rubles(cost_cents, target_cents, basis):
    """Reference formula used to verify both client implementations."""
    if not cost_cents or cost_cents < 0 or target_cents < 0 or not basis.get('complete'):
        return None
    n = basis['units']; overhead_n = basis.get('overhead_units', 1)
    rate = Decimal(basis['commission_rate']) + Decimal(basis['acquiring_rate'])
    if n <= 0 or overhead_n <= 0 or not 0 <= rate < 1:
        return None
    fixed = Decimal(basis['fixed_cents']) / n
    overhead = Decimal(basis.get('overhead_cents', 0)) / overhead_n
    return int(((cost_cents + target_cents + fixed + overhead) / (100 * (1-rate))).to_integral_value(rounding=ROUND_CEILING))


def price_bases(products, entries, posting_groups, prices, types):
    names = {str(t['id']): t['name'] for t in types['accrual_types']}
    by_sku = {str(s): p for p in products for s in p.skus}
    sales = {}; returns = set(); crossdock = 0
    for entry in entries:
        posting = entry.get('posting') or {}
        scheme = {'Fbo':'FBO', 'Fbs':'FBS'}.get(posting.get('delivery_schema'))
        number = entry.get('unit_number')
        for item in posting.get('products', []):
            commission = item.get('commission')
            if not commission or not number: continue
            key = (number, str(item.get('sku')))
            amount = rubles(commission.get('sale_commission'))
            revenue = rubles(commission.get('sale_amount'))
            if amount > 0 or revenue < 0:
                returns.add(key); continue
            if revenue > 0 and scheme:
                q = item.get('quantity')
                if not isinstance(q, int) or isinstance(q, bool) or q <= 0:
                    raise OzonApiError('Ozon: некорректное количество для минимальной цены')
                if key in sales:
                    # Repeated sale adjustments cannot safely become extra units.
                    returns.add(key)
                else: sales[key] = (q, scheme)
        extra = entry.get('non_item_fee') or {}
        if names.get(str(extra.get('type_id'))) == 'CrossDock':
            crossdock -= rubles(extra.get('accrued'))
    fees = defaultdict(list)
    seen_groups = set()
    for group in posting_groups:
        number = group.get('posting_number')
        if not number or number in seen_groups:
            raise OzonApiError('Ozon: повторная или пустая детализация отправления')
        seen_groups.add(number)
        for fee in group.get('accruals', []):
            key = (number, str(fee.get('sku')))
            fees[key].append(fee)
            if names.get(str(fee.get('type_id'))) == 'SaleCommission' and rubles(fee.get('accrued')) > 0:
                returns.add(key)
    totals = {}; fbo_units = 0
    for key, (q, scheme) in sales.items():
        if key in returns or key[1] not in by_sku: continue
        product = by_sku[key[1]]
        if product.offer_id in EXCLUDED_OFFERS: continue
        target = totals.setdefault((product.product_id, scheme), dict(units=0, fixed_cents=0, complete=True))
        target['units'] += q
        if scheme == 'FBO': fbo_units += q
        seen_sale = False; seen_logistics = False
        for fee in fees[key]:
            name = names.get(str(fee.get('type_id')))
            amount = -rubles(fee.get('accrued'))
            if name == 'SaleCommission': seen_sale = True
            elif name in FIXED:
                target['fixed_cents'] += amount
                seen_logistics = True
            elif name == 'Acquiring' or name in IGNORED:
                pass  # The current acquiring estimate replaces historical fees.
            elif amount:
                target['complete'] = False  # Unknown percent/fixed nature: no invented price.
        target['complete'] &= seen_sale and seen_logistics
    price_map = {str(p['product_id']): p for p in prices}
    result = {}
    for (pid, scheme), total in totals.items():
        total['complete'] &= total['fixed_cents'] > 0
        p = price_map.get(pid, {}); price = p.get('price') or {}
        try:
            current = Decimal(str(price['marketing_seller_price']))
            commission = Decimal(str(p['commissions']['sales_percent_'+scheme.lower()])) / 100
            acquiring = Decimal(str(p['acquiring'])) / current
            if price.get('currency_code') != 'RUB' or current <= 0 or not 0 <= commission+acquiring < 1:
                raise ValueError
        except (KeyError, ValueError, ArithmeticError):
            total['complete'] = False; commission = acquiring = Decimal(0)
        result.setdefault(pid, []).append(dict(total, scheme=scheme,
            commission_rate=str(commission), acquiring_rate=str(acquiring),
            overhead_cents=crossdock if scheme == 'FBO' else 0,
            overhead_units=fbo_units if scheme == 'FBO' and fbo_units else 1,
            acquiring_estimated=True))
    return result


async def load_price_bases(service, products, entries, types=None):
    """Use complete posting fees once; never add day delivery to them again."""
    if not products or (types is not None and not any(t.get('name') == 'SaleCommission' for t in types.get('accrual_types', []))):
        return {}
    service.progress = 'Минимальная цена: актуальные комиссии…'
    prices = []; cursor = ''; seen = set()
    while True:
        data = await service.finance_post('/v5/product/info/prices',
            dict(filter={'visibility':'ALL'}, limit=1000, cursor=cursor))
        if not isinstance(data.get('items'), list): raise OzonApiError('Ozon: нет данных о комиссиях')
        prices.extend(data['items'])
        next_cursor = data.get('cursor')
        if not next_cursor or len(prices) >= data.get('total', data.get('total_items', len(prices))): break
        if next_cursor in seen: raise OzonApiError('Ozon: повторная страница цен')
        seen.add(next_cursor); cursor = next_cursor
    types = types or await service.finance_post('/v1/finance/accrual/types', {})
    numbers = sorted({e['unit_number'] for e in entries if e.get('posting') and e.get('unit_number')})
    groups = []
    for start in range(0, len(numbers), 200):
        service.progress = f'Минимальная цена: услуги по отправлениям {start} из {len(numbers)}'
        data = await service.finance_post('/v1/finance/accrual/postings', {'posting_numbers':numbers[start:start+200]})
        if not isinstance(data.get('posting_accruals'), list): raise OzonApiError('Ozon: нет полной детализации услуг')
        groups.extend(data['posting_accruals'])
    return price_bases(products, entries, groups, prices, types)
