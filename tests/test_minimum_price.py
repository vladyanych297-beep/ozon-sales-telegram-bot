from decimal import Decimal
from types import SimpleNamespace

from ozon_forecast.minimum_price import minimum_rubles, price_bases


def basis(**extra):
    return dict(complete=True, units=14, fixed_cents=157848,
                overhead_cents=13680, overhead_units=41,
                commission_rate='0.47', acquiring_rate='0.01', **extra)


def test_minimum_and_one_ruble_less_with_exact_fractional_means():
    b = basis()
    for target in (0, 20000, 35123):
        price = minimum_rubles(12300, target, b)
        fixed = Decimal(b['fixed_cents']) / 14 + Decimal(13680) / 41
        assert Decimal(price)*100*Decimal('.52') - fixed - 12300 >= target
        assert Decimal(price-1)*100*Decimal('.52') - fixed - 12300 < target
    assert minimum_rubles(0, 20000, b) is None
    assert minimum_rubles(None, 20000, b) is None
    assert minimum_rubles(100, 20000, {**b, 'complete':False}) is None
    assert minimum_rubles(100, 20000, {**b, 'commission_rate':'1'}) is None


def test_successful_schemes_exclusions_and_no_double_logistics():
    money = lambda value: dict(amount=str(value), currency='RUB')
    types = dict(accrual_types=[dict(id=i,name=n) for i,n in [(1,'Acquiring'),(12,'CrossDock'),(32,'Logistic'),(69,'SaleCommission'),(74,'StarsMembership'),(95,'CustomerReviews')]])
    products=[SimpleNamespace(product_id=str(i), offer_id=o, skus=[str(i)]) for i,o in [(1,'A'),(2,'B'),(3,'KD001')]]
    def entry(i, scheme):
        return dict(unit_number=str(i), posting=dict(delivery_schema=scheme, products=[dict(sku=i,quantity=1,commission=dict(sale_commission=money(-50),sale_amount=money(100)),delivery=dict(total_accrued=money(-999)))]))
    entries=[entry(1,'Fbo'),entry(2,'Fbs'),entry(3,'Fbo'),dict(non_item_fee=dict(type_id=12,accrued=money(-10)))]
    groups=[dict(posting_number=str(i),accruals=[dict(sku=i,type_id=t,accrued=money(-v)) for t,v in [(69,50),(32,20),(74,5),(95,3),(1,1)]]) for i in (1,2,3)]
    prices=[dict(product_id=i,price=dict(marketing_seller_price=100,currency_code='RUB'),commissions=dict(sales_percent_fbo=47,sales_percent_fbs=52),acquiring=1) for i in (1,2,3)]
    result=price_bases(products,entries,groups,prices,types)
    assert result['1'][0]['fixed_cents']==2000  # never +999 day delivery
    assert result['1'][0]['overhead_cents']==1000
    assert result['1'][0]['overhead_units']==1
    assert result['2'][0]['overhead_cents']==0
    assert result['2'][0]['commission_rate']=='0.52'
    assert '3' not in result
    entries.append(dict(unit_number='1',posting=dict(products=[dict(sku=1,quantity=1,commission=dict(sale_commission=money(50),sale_amount=money(-100)))])))
    assert '1' not in price_bases(products,entries,groups,prices,types)
    groups[1]['accruals'].append(dict(sku=2,type_id=999,accrued=money(-5)))
    assert not price_bases(products,entries,groups,prices,types)['2'][0]['complete']
