"""US supplier freight facts and configurable estimates; vendor reads run on the PC."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING

from sqlalchemy import select

from erp.core.config import get_settings
from erp.core.errors import AppError, ConflictError
from erp.core.jsonutil import dumps, loads
from erp.core.task_identity import task_identity
from erp.models import IntegrationConnection, Job, SystemSetting, utc_now
from erp.serializers import job_dict
from erp.services.jobs import enqueue_job
from erp.domains.pod.integrations.common import load_supplier_connection
from erp.domains.pod import us_supplier_catalog as catalog

JOB_TYPE = 'pod_supplier_us_freight_quote'
DEFAULT_RULE = {'range_position_percent': 50, 'buffer_percent': 5,
                'buffer_cny': 0, 'rounding_cny': 1}
RULE_KEY = 'amazon_us_freight'


def number(value, label, minimum=Decimal('0'), maximum=Decimal('1000000')):
    try:
        if isinstance(value, bool) or value is None: raise ValueError()
        result = Decimal(str(value))
        if not result.is_finite() or result < minimum or result > maximum: raise ValueError()
        return result
    except (ValueError, InvalidOperation):
        raise AppError(f'{label}格式或范围无效', code='us_freight_invalid', status_code=422) from None


def validate_rule(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_RULE):
        raise AppError('美国运费规则字段无效', code='us_freight_invalid', status_code=422)
    result = {**DEFAULT_RULE, **value}
    for field in ('range_position_percent', 'buffer_percent'):
        number(result[field], field, maximum=Decimal('100'))
    number(result['buffer_cny'], '固定缓冲')
    number(result['rounding_cny'], '取整单位', minimum=Decimal('0.01'), maximum=Decimal('100'))
    return {field: float(number(value, field)) for field, value in result.items()}


def rule(session):
    value = session.get(SystemSetting, RULE_KEY)
    return validate_rule(loads(value.value_json, {}) if value else {})


def estimate(lower, upper, profile, override=None):
    low, high = number(lower, '运费下限'), number(upper, '运费上限')
    if low > high:
        raise AppError('运费下限不能高于上限', code='us_freight_invalid', status_code=422)
    profile = validate_rule(profile)
    midpoint = low + (high - low) * number(profile['range_position_percent'], '取值位置') / 100
    buffered = midpoint * (1 + number(profile['buffer_percent'], '缓冲比例') / 100) + number(profile['buffer_cny'], '固定缓冲')
    unit = number(profile['rounding_cny'], '取整单位', minimum=Decimal('0.01'))
    calculated = (buffered / unit).to_integral_value(rounding=ROUND_CEILING) * unit
    chosen = number(override, '手动运费') if override is not None else calculated
    return {'minimum_cny': str(low), 'maximum_cny': str(high), 'range_value_cny': str(midpoint),
            'buffered_cny': str(buffered), 'calculated_cny': str(calculated),
            'selected_cny': str(chosen), 'manual': override is not None, 'rule': profile}


def context(session, key, variant_id):
    detail = catalog.composition_detail(session, key)
    variant = next((v for v in detail['stock_info'] if int(v.get('id') or 0) == variant_id), None)
    if not variant: raise ConflictError('请选择该美国商品的真实规格')
    # S2B prices are CNY, confirmed by the user and the supplier UI on 2026-10-01.
    if detail.get('currency') and str(detail['currency']).upper() != 'CNY':
        raise ConflictError('供应商价格币种与人民币规则不一致，请先核对')
    purchase = number(variant.get('price'), '所选规格拿货价')
    params = {'basic_product_id': int(detail['id']), 'platform': 99, 'country': 'US', 'num': 1}
    for field in ('weight', 'length', 'width', 'height'):
        amount = number(variant.get(field), f'包装{field}', minimum=Decimal('0.01'))
        # Weight is grams; package dimensions are cm in S2B's detail schema.
        params[field] = str(amount)
    connection = load_supplier_connection(session, 's2b')
    facts = {'product_key': key, 'variant_id': variant_id, 'purchase_cny': str(purchase),
             'params': params, 'connection_id': connection.connection_id,
             'connection_config': connection.config, 'connection_base_url': connection.base_url,
             'credentials_revision': hashlib.sha256(session.get(IntegrationConnection, connection.connection_id).secrets_encrypted.encode()).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {'market': 'US', 'kind': 'freight', 'product_key': key, 'variant_id': variant_id,
            'purchase_cny': str(purchase), 'params': params, 'fingerprint': fingerprint,
            'connection_ids': {'s2b': connection.connection_id}}


def _cached(session, request):
    _, data = catalog.read(session)
    value = (data.get('freight_quotes') or {}).get(request['fingerprint'])
    if not value: return None
    try:
        fetched = datetime.fromisoformat(value['fetched_at'])
        if fetched.tzinfo is None: fetched = fetched.replace(tzinfo=timezone.utc)
        if fetched < utc_now() - timedelta(days=7): return None
    except (ValueError, TypeError, KeyError): return None
    return value


def preview(session, key, variant_id, override=None):
    request = context(session, key, variant_id)
    quote = _cached(session, request)
    profile = rule(session)
    if not quote:
        return {'available': False, 'message': '尚无有效的 GOFO 报价，请手动读取运费',
                'rule': profile, 'currency': 'CNY', 'purchase_cny': request['purchase_cny']}
    result = estimate(quote['minimum_cny'], quote['maximum_cny'], profile, override)
    return {'available': True, 'currency': 'CNY', 'purchase_cny': request['purchase_cny'],
            'purchase_and_freight_cny': str(Decimal(request['purchase_cny']) + Decimal(result['selected_cny'])),
            'quote': quote, 'freight': result,
            'basis': '国家级报价区间估算，不是实际订单平均值；未包含平台佣金、广告、退货等费用'}


def create_job(session, key, variant_id, refresh=False):
    if not get_settings().multiuser_enabled: raise ConflictError('请使用多人模式的本机执行端读取运费')
    request = context(session, key, variant_id)
    catalog._lock_connection(session, request['connection_ids']['s2b'])
    request = context(session, key, variant_id)
    if not refresh and _cached(session, request):
        return {'cached': True, 'job': None, 'preview': preview(session, key, variant_id)}
    active = session.scalars(select(Job).where(Job.type == JOB_TYPE,
        Job.status.in_(['queued', 'running', 'retrying', 'paused']))).all()
    actor = task_identity.get() or {}
    for job in active:
        value = loads(job.payload_json, {})
        if value.get('fingerprint') != request['fingerprint']: continue
        if job.created_by == actor.get('id') and job.target_worker_id == actor.get('worker_id'):
            return {'cached': False, 'job': job_dict(job), 'reused': True}
        raise ConflictError('该规格已有运费读取任务，请等待云端报价，避免重复请求')
    # A terminal read can be explicitly refreshed; every job keeps its own receipt.
    import uuid
    job = enqueue_job(session, job_type=JOB_TYPE, payload=request,
        config_snapshot=dumps({'market': 'US', 'provider': 's2b', 'read_only': True}),
        idempotency_key=f'{JOB_TYPE}:{uuid.uuid4().hex}', max_attempts=1,
        steps=[('freight', '读取 S2B 美国 GOFO 单件运费区间')])
    return {'cached': False, 'job': job_dict(job), 'reused': False}


def select_gofo(options):
    if not isinstance(options, list) or len(options) > 1000: raise ConflictError('运费渠道结果无效')
    candidates = []
    for item in options:
        if not isinstance(item, dict): raise ConflictError('运费渠道结果无效')
        name = str(item.get('name') or item.get('en_name') or '')
        # Platform-provided labels (e.g. Temu's 0.10) are not an Amazon freight charge.
        if 'GOFO' not in name.upper() or any(x in name.upper() for x in ('TEMU', 'SHEIN', 'TIKTOK', '在线下单')): continue
        if item.get('currency') and str(item['currency']).upper() != 'CNY':
            raise ConflictError('GOFO 运费币种不是人民币')
        low, high = number(item.get('min_amount'), 'GOFO 运费下限', minimum=Decimal('0.01')), number(item.get('max_amount'), 'GOFO 运费上限', minimum=Decimal('0.01'))
        if low > high: raise ConflictError('GOFO 运费区间无效')
        candidates.append({'channel_id': str(item.get('logistics_id') or item.get('id') or ''),
            'channel_name': name, 'minimum_cny': str(low), 'maximum_cny': str(high),
            'day_from': item.get('day_from'), 'day_to': item.get('day_to')})
    if len(candidates) != 1:
        raise ConflictError('未找到唯一的 GOFO 自付运费渠道，请核对供应商报价；不会自动替换为其他物流')
    return candidates[0]


def accept(session, job, result):
    from erp.services.catalog_personal_publications import _public_result
    _public_result(result)
    request = loads(job.payload_json, {})
    if job.type != JOB_TYPE or result.get('market') != 'US' or result.get('kind') != 'freight' or result.get('fingerprint') != request.get('fingerprint'):
        raise ConflictError('运费回执与原任务不一致')
    catalog._lock_connection(session, request['connection_ids']['s2b'])
    actual = context(session, request['product_key'], request['variant_id'])
    if actual != request: raise ConflictError('商品规格或供应商连接已变化，请重新读取运费')
    selected = select_gofo(result.get('options'))
    snapshot, data = catalog.read(session)
    quote = {**selected, 'currency': 'CNY', 'fetched_at': utc_now().isoformat(),
             'product_key': request['product_key'], 'variant_id': request['variant_id'],
             'quantity': 1, 'country': 'US', 'fingerprint': request['fingerprint']}
    cache = data.setdefault('freight_quotes', {})
    # Keep one current quote per specification, rather than accumulating old facts.
    for key, value in list(cache.items()):
        if value.get('product_key') == quote['product_key'] and value.get('variant_id') == quote['variant_id']: del cache[key]
    cache[request['fingerprint']] = quote
    snapshot.payload_json = dumps(data)
    session.flush()
    return {'product_key': request['product_key'], 'variant_id': request['variant_id'],
            'quote': quote, 'errors': []}
