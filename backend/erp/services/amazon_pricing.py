"""Versioned US referral tariffs and expected FBM profit, in Decimal USD.

Rates are reviewed source data, never AI output or editable account settings.
Return assumption: full refund, no salvage; outbound POD cost and ads remain.
"""
from decimal import Decimal as D, ROUND_HALF_UP
from erp.core.errors import AppError
from erp.domains.pod.us_freight import number

SOURCE = 'https://sell.amazon.com/pricing'
REFUND_SOURCE = 'https://sellercentral.amazon.com/help/hub/reference/external/G200336920?locale=en_us'
VERSION = 'US-2026-10-02'
DEFAULTS = {'return_rate_percent': 10, 'cost_profit_percent': 50,
            'advertising_percent': 10, 'usd_cny': 6.6, 'other_cost_cny': 0,
            'return_shipping_cny': 0, 'buyer_shipping_usd': 0, 'selling_plan': 'professional'}
# Whole-price thresholds differ from progressive tiers. POD-relevant categories
# are explicit; unreviewed categories cannot fall back silently to 15%.
TARIFFS = {
    'home_kitchen': ('家居与厨房', 'flat', [(None, '0.15')], '0.30'),
    'office': ('办公用品', 'flat', [(None, '0.15')], '0.30'),
    'clothing': ('服装与配饰', 'whole', [(15, '0.05'), (20, '0.10'), (None, '0.17')], '0.30'),
    'electronics_accessories': ('电子配件', 'progressive', [(100, '0.15'), (None, '0.08')], '0.30'),
    'computers': ('电脑', 'flat', [(None, '0.08')], '0.30'),
    'consumer_electronics': ('消费电子', 'flat', [(None, '0.08')], '0.30'),
    'baby': ('婴儿用品', 'whole', [(10, '0.08'), (None, '0.15')], '0.30'),
    'beauty_health': ('美妆健康与个人护理', 'whole', [(10, '0.08'), (None, '0.15')], '0.30'),
    'bags': ('背包手袋与行李箱', 'flat', [(None, '0.15')], '0.30'),
    'footwear': ('鞋类', 'flat', [(None, '0.15')], '0.30'),
    'sports': ('运动与户外', 'flat', [(None, '0.15')], '0.30'),
    'toys': ('玩具与游戏', 'flat', [(None, '0.15')], '0.30'),
    'pets': ('宠物用品（不含兽医饮食）', 'flat', [(None, '0.15')], '0.30'),
    'tools': ('工具与家居装修', 'flat', [(None, '0.15')], '0.30'),
    'garden': ('庭院与花园', 'flat', [(None, '0.15')], '0.30'),
    'automotive': ('汽车与动力运动', 'flat', [(None, '0.12')], '0.30'),
    'industrial': ('商业工业与科学用品', 'flat', [(None, '0.12')], '0.30'),
    'furniture': ('家具', 'progressive', [(200, '0.15'), (None, '0.10')], '0.30'),
    'jewelry': ('珠宝', 'progressive', [(250, '0.20'), (None, '0.05')], '0.30'),
    'watches': ('手表', 'progressive', [(1500, '0.16'), (None, '0.03')], '0.30'),
    'mattresses': ('床垫', 'flat', [(None, '0.15')], '0.30'),
    'eyewear': ('眼镜', 'flat', [(None, '0.15')], '0.30'),
    'musical': ('乐器与影音制作', 'flat', [(None, '0.15')], '0.30'),
    'everything_else': ('其他（仅无专门收费类目时）', 'flat', [(None, '0.15')], '0.30'),
}

def options():
    return {'version': VERSION, 'source': SOURCE, 'refund_source': REFUND_SOURCE,
            'items': [{'key': k, 'label': v[0], 'mode': v[1],
                       'tiers': [{'up_to_usd': x, 'rate_percent': str(D(y)*100)} for x,y in v[2]],
                       'minimum_usd': v[3]} for k,v in TARIFFS.items()]}

def profile(value):
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise AppError('定价字段无效；佣金只允许使用官方费率表', code='amazon_pricing_invalid', status_code=422)
    result = {**DEFAULTS, **value}
    for k in ('return_rate_percent', 'advertising_percent'):
        number(result[k], k, maximum=D('99.99'))
    number(result['cost_profit_percent'], '成本利润率', maximum=D('1000'))
    number(result['usd_cny'], '美元兑人民币', minimum=D('0.01'), maximum=D('1000'))
    for k in ('other_cost_cny', 'return_shipping_cny', 'buyer_shipping_usd'): number(result[k], k)
    if result['selling_plan'] not in ('professional','individual'):
        raise AppError('销售计划无效', code='amazon_pricing_invalid', status_code=422)
    return {k: (v if k=='selling_plan' else float(number(v,k))) for k,v in result.items()}

def referral(category, total):
    if category not in TARIFFS:
        raise AppError('请选择已核对的亚马逊官方收费类目', code='amazon_fee_category_required', status_code=422)
    _, mode, tiers, minimum = TARIFFS[category]
    total = number(total, '成交总价')
    if mode=='progressive':
        amount, start = D(0), D(0)
        for end, rate in tiers:
            end = D(end) if end is not None else total
            amount += max(D(0), min(total,end)-start)*D(rate)
            start=end
    else:
        rate=next(rate for end,rate in tiers if end is None or total<=D(end))
        amount=total*D(rate)
    return max(D(minimum),amount).quantize(D('.01'), rounding=ROUND_HALF_UP)

def calculate(purchase_cny, freight_cny, category, overrides=None):
    p=profile(overrides or {}); fx=D(str(p['usd_cny']))
    r=D(str(p['return_rate_percent']))/100; a=D(str(p['advertising_percent']))/100
    m=D(str(p['cost_profit_percent']))/100
    purchase=number(purchase_cny,'拿货价'); freight=number(freight_cny,'GOFO运费')
    base=(purchase+freight+D(str(p['other_cost_cny'])))/fx
    cost=base+r*D(str(p['return_shipping_cny']))/fx
    if cost<=0: raise AppError('总成本必须大于零',code='amazon_pricing_invalid',status_code=422)
    plan=D('.99') if p['selling_plan']=='individual' else D(0)
    def components(s):
        fee=referral(category,s)
        refund=min(D(5),fee*D('.2')).quantize(D('.01'), rounding=ROUND_HALF_UP)
        expected_fee=(1-r)*fee+r*refund
        ads=a*s; loss=r*s
        return fee,refund,expected_fee,ads,loss,s-loss-expected_fee-ads-plan-cost
    # Whole-price clothing/health tiers introduce jumps. Search each tier
    # separately; a global bisection would miss cheaper valid prices below jumps.
    if category not in TARIFFS: referral(category,0)
    bounds=[int(D(t)*100) for t,_ in TARIFFS[category][2] if t is not None]+[10000000]
    shipping=D(str(p['buyer_shipping_usd']))
    lo=max(1,int(shipping*100)+1); target=m*cost; chosen=None
    # Rounded referral fees can change by a cent; round target upward and
    # require a one-cent cushion before bisection, then inspect the nearby cents.
    for hi in bounds:
        if hi<lo:continue
        if components(D(lo)/100)[-1]>=target:
            chosen=lo; break
        if components(D(hi)/100)[-1]>=target+D('.02'):
            left,right=lo,hi
            while left<right:
                mid=(left+right)//2
                if components(D(mid)/100)[-1]>=target+D('.02'): right=mid
                else: left=mid+1
            start=max(lo,left-100)
            chosen=next((v for v in range(start,left+1) if components(D(v)/100)[-1]>=target),left)
            break
        # A solution close to the upper boundary must not be missed.
        tail=next((v for v in range(max(lo,hi-100),hi+1) if components(D(v)/100)[-1]>=target),None)
        if tail is not None: chosen=tail; break
        lo=hi+1
    if chosen is None: raise AppError('当前退货、广告及利润参数无法得到有效售价',code='amazon_pricing_unsolvable',status_code=422)
    price=D(chosen)/100; fee,refund,expected_fee,ads,loss,profit=components(price)
    money=lambda x: str(x.quantize(D('.01'), rounding=ROUND_HALF_UP))
    return {'currency':'USD','price':money(price-shipping),'customer_total_usd':money(price),'purchase_cny':str(purchase),
            'freight_cny':str(freight),'base_cost_cny':money(base*fx),'expected_cost_usd':money(cost),
            'referral_fee_usd':money(fee),'effective_referral_percent':str(fee/price*100),
            'refund_administration_usd':money(refund),'expected_commission_usd':money(expected_fee),
            'advertising_usd':money(ads),'expected_refund_usd':money(loss),'per_item_plan_fee_usd':money(plan),
            'expected_profit_usd':money(profit),'cost_profit_percent_actual':str(profit/cost*100),
            'fee_category':category,'fee_category_label':TARIFFS[category][0],
            'fee_version':VERSION,'fee_source':SOURCE,'refund_source':REFUND_SOURCE,'parameters':p,
            'assumptions':'成交总额=商品售价+配送模板买家运费；全额退款、无残值回收；广告按成交总额计提，拿货及出库成本不退；不含销售税与专业计划月租，其他成本可单列。'}
