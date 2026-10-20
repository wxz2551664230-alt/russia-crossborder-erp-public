"""Validated batch overrides and current showcase image selection for Ozon."""
from decimal import Decimal, InvalidOperation
from erp.core.errors import ConflictError
from erp.modules.ozon_follow.legacy.services.pricing import OZON_RFBS_COMMISSION_TARIFFS

PRICING_FIELDS = {
 'target_cost_profit_margin': (0, 10000), 'pricing_advertising_rate': (0, 99.99),
 'pricing_return_rate': (0, 99.99), 'pricing_sticker_fee': (0, 100000),
 'pricing_operating_cost': (0, 100000), 'pricing_logistics_service_rate': (0, 99.99),
 'pricing_price_multiplier': (0.01, 100), 'pricing_old_price_multiplier': (0.01, 100),
}

def pricing_profile(snapshot, requirements):
 profile={**(snapshot.get('pricing') or {}),**(snapshot.get('pricing_profile') or {}),**(snapshot.get('ozon_follow_pricing') or {})}
 overrides=requirements.get('pricing_overrides', {})
 if not isinstance(overrides, dict) or set(overrides)-set(PRICING_FIELDS)-{'pricing_category_module','pricing_product_category'}:
  raise ConflictError('本批次定价参数无效')
 for key,value in overrides.items():
  if key in {'pricing_category_module','pricing_product_category'}:
   if not isinstance(value,str) or not value.strip() or len(value)>200:raise ConflictError('利润计费类目无效')
   profile[key]=value.strip()
   continue
  try: number=Decimal(str(value))
  except (InvalidOperation, ValueError):raise ConflictError('定价参数必须为有效数值')
  low,high=PRICING_FIELDS[key]
  if not number.is_finite() or not Decimal(str(low))<=number<=Decimal(str(high)):
   raise ConflictError('定价参数超出允许范围：'+key)
  profile[key]=float(number)
 if overrides.get('pricing_category_module') or overrides.get('pricing_product_category'):
  module=profile.get('pricing_category_module','');category=profile.get('pricing_product_category','')
  if category not in OZON_RFBS_COMMISSION_TARIFFS.get(module,{}):raise ConflictError('请选择利润计费模块及该模块下的商品类目')
 return profile

def publication_images(facts, requirements):
 images=[x for x in facts.get('assets',[]) if (x.get('role') in {'main','additional','mockup','sku'} or (not x.get('role') and facts.get('source_type')!='pod')) and str(x.get('oss_url','')).startswith('https://')]
 # Source artwork/production files must never be published as POD showcase images.
 selection=requirements.get('image_selection',{})
 if not isinstance(selection,dict):raise ConflictError('图片选择参数无效')
 ids=selection.get(facts.get('product_id',''))
 if ids is None:return images[:15]
 if not isinstance(ids,list) or not 1<=len(ids)<=15 or len(set(ids))!=len(ids):
  raise ConflictError('每个产品请选择1至15张不重复的展示图片')
 index={x['asset_id']:x for x in images}
 if any(identity not in index for identity in ids):raise ConflictError('所选图片已失效、属于其他规格或为生产原图，请刷新图片选择')
 return [{**index[identity],'role':'main' if position==0 else 'additional','position':position} for position,identity in enumerate(ids)]


def require_options_client(requirements, version):
 if not (requirements.get('pricing_overrides') or requirements.get('image_selection')):return
 import re
 match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)',str(version or ''))
 if not match or tuple(map(int,match.groups()))<(0,6,53):
  raise ConflictError('本批次使用自定义定价或图片顺序，请先升级执行电脑至0.6.53或更新版本')


def resolve_pod_image_template(facts, requirements):
    """Freeze WB-style filename rules to this SKU's own image IDs before enqueue."""
    result = dict(requirements)
    rules = result.pop('pod_image_template', None)
    if rules is None or facts.get('source_type') != 'pod':
        return result
    if not isinstance(rules, list) or not 1 <= len(rules) <= 15 or any(not isinstance(key, str) or not key for key in rules) or len(set(rules)) != len(rules):
        raise ConflictError('POD 图片模板请选择1至15个不重复的图片名称')
    images = [x for x in facts.get("assets", []) if x.get("role") in {"main", "additional", "mockup", "sku"} and str(x.get("oss_url", "")).startswith("https://")]
    from pathlib import PurePosixPath
    index = {}
    for item in images:
        key = PurePosixPath(str(item.get('name') or '').replace('\\', '/')).name
        if key in index:
            raise ConflictError('POD 展示图片名称重复，请先整理图库：' + str(facts.get('sku', '')))
        index[key] = item['asset_id']
    missing = [key for key in rules if key not in index]
    if missing:
        raise ConflictError('POD 图片模板无法匹配 ' + str(facts.get('sku', '')) + '：' + '、'.join(missing) + '；请按同类目选择产品')
    result['image_selection'] = {facts['product_id']: [index[key] for key in rules]}
    return result
