"""Build only US FBM offers and schema-permitted, non-buyable parents."""
import copy
import hashlib
import json
from jsonschema import Draft201909Validator, validators, ValidationError
from erp.core.errors import AppError
from erp.integrations.amazon import US_MARKETPLACE as MARKET

COPY_FIELDS={'item_name','bullet_point','product_description','generic_keyword'}
PROTECTED={'brand','externally_assigned_product_identifier','supplier_declared_has_product_identifier_exemption',
    'merchant_suggested_asin','purchasable_offer','fulfillment_availability','merchant_shipping_group',
    'parentage_level','child_parent_sku_relationship','variation_theme','country_of_origin','list_price'}
USER_PROTECTED=PROTECTED-{'country_of_origin','list_price'}

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def resolve(schema, node):
    node=copy.deepcopy(node)
    if isinstance(node,dict) and '$ref' in node:
        ref=node.pop('$ref')
        if not ref.startswith('#/'):
            raise AppError('类目包含未缓存的外部定义，请重新读取类目',code='amazon_schema_invalid',status_code=409)
        part=schema
        for key in ref[2:].split('/'):part=part[key.replace('~1','/').replace('~0','~')]
        node={**resolve(schema,part),**node}
    return node

def choices(schema, attr, field='value'):
    node=resolve(schema,(schema.get('properties') or {}).get(attr,{}))
    item=resolve(schema,node.get('items') or {})
    prop=resolve(schema,(item.get('properties') or {}).get(field,{}))
    deprecated=prop.get('$lifecycle',{}).get('enumDeprecated',[]) or []
    return [v for v in prop.get('enum',[]) if v not in deprecated]

def schema_summary(definition):
    s=definition['schema']; props=s.get('properties',{})
    shipping=resolve(s,props.get('merchant_shipping_group',{}))
    shipping_value=resolve(s,resolve(s,shipping.get('items',{})).get('properties',{}).get('value',{}))
    return {'product_type':definition['product_type'],'display_name':definition.get('display_name'),
        'checksum':definition['checksum'],'parentage':definition['parentage'],
        'variation_themes':choices(s,'variation_theme','name'),
        'shipping_templates':choices(s,'merchant_shipping_group'),
        'shipping_template_labels':shipping_value.get('enumNames',[]),
        'required':s.get('required',[]),
        'schema_definitions':s.get('$defs',{}),
        'fields':[{'key':k,'title':v.get('title',k),'description':v.get('description',''),
                   'schema':v,'choices':choices(s,k)} for k,v in props.items()]}

def _unique(validator, count, instance, schema):
    if not isinstance(instance,list):return
    selectors=schema.get('selectors') or []
    if not selectors:return
    def key(row,path):
        for p in path.split('.'):
            if not isinstance(row,dict):return None
            row=row.get(p)
        return row
    unique={json.dumps([key(row,p) for p in selectors],sort_keys=True) for row in instance}
    # Both custom validators share grouping, not ordinary whole-object uniqueness.
    if 'minUniqueItems' in schema and len(unique)<schema['minUniqueItems']:
        yield ValidationError('唯一选择项数量不足')
    if 'maxUniqueItems' in schema and len(unique)>schema['maxUniqueItems']:
        yield ValidationError('唯一选择项数量过多')

def _utf8_length(validator, count, instance, schema):
    if not isinstance(instance,str):return
    length=len(instance.encode('utf-8'))
    if 'maxUtf8ByteLength' in schema and length>schema['maxUtf8ByteLength']:
        yield ValidationError('文本超过 UTF-8 字节上限')
    if 'minUtf8ByteLength' in schema and length<schema['minUtf8ByteLength']:
        yield ValidationError('文本少于 UTF-8 字节下限')

AmazonValidator=validators.extend(Draft201909Validator,{'minUniqueItems':_unique,'maxUniqueItems':_unique,
    'maxUtf8ByteLength':_utf8_length,'minUtf8ByteLength':_utf8_length})

def validate_payload(body, definition):
    if body.get('productType')!=definition['product_type'] or body.get('requirements')!='LISTING':
        raise AppError('上架类目与人工选择不一致',code='amazon_payload_invalid',status_code=422)
    schema=definition['schema']
    # Amazon's metaschema is an annotation/custom vocabulary; use the compatible
    # 2019-09 validator directly. External references fail instead of fetching.
    from referencing import Registry
    def unavailable(uri):raise AppError('类目定义含未缓存引用，请刷新类目',code='amazon_schema_invalid',status_code=409)
    validator=AmazonValidator(schema,registry=Registry(retrieve=unavailable))
    errors=sorted(validator.iter_errors(body['attributes']),key=lambda e:str(list(e.path)))
    return [{'path':'.'.join(str(x) for x in e.path),'message':e.message} for e in errors[:80]]

def field(value, **extra):
    return [{'value':value,'marketplace_id':MARKET,**extra}]

def make_body(snapshot, item, content, definition, *, parent=False):
    props=definition['schema'].get('properties',{})
    user=copy.deepcopy(snapshot.get('attributes') or {})
    if set(user)&(USER_PROTECTED|COPY_FIELDS):
        raise AppError('补充属性不能改写文案、品牌、售价、库存或父子关系',code='amazon_payload_invalid',status_code=422)
    attributes={**user,**copy.deepcopy(item.get('attributes') or {})}
    if set(attributes)&(USER_PROTECTED|COPY_FIELDS):
        raise AppError('规格补充属性含受保护字段',code='amazon_payload_invalid',status_code=422)
    generated=content.get('attributes') or {}
    if set(generated)&PROTECTED or any(k not in props for k in generated):
        raise AppError('AI 不能改写身份、定价、库存或类目规则',code='amazon_ai_invalid',status_code=422)
    # AI may supply descriptive facts only; safety/compliance fields require
    # supplier evidence or a separately entered human value.
    allowed=COPY_FIELDS|{'color','pattern','style','style_name','size','size_name','material','fabric_type',
                        'item_shape','recommended_uses_for_product','special_feature','included_components'}
    if set(generated)-allowed:
        raise AppError('AI 属性包含需人工或供应商核实的字段',code='amazon_ai_invalid',status_code=422)
    attributes={**generated,**attributes}
    if parent:
        # Parent definitions omit child-only fields such as brand/material/color.
        # Shared human facts are still used by children, but only parent-schema
        # fields may be copied into the non-buyable container.
        attributes={k:v for k,v in attributes.items() if k in props}
    # Size comes from the exact supplier variant, not package dimensions or AI.
    import re
    size=re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[xX×*]\s*(\d+(?:\.\d+)?)\s*cm\s*',item.get('size',''))
    if size and 'item_length_width' in props and 'item_length_width' not in attributes:
        dims=sorted([float(size[1])/2.54,float(size[2])/2.54],reverse=True)
        attributes['item_length_width']=[{'length':{'value':round(dims[0],4),'unit':'inches'},
                                        'width':{'value':round(dims[1],4),'unit':'inches'},'marketplace_id':MARKET}]
    title=content.get('title')
    if not isinstance(title,str) or not title.strip() or len(title)>200:
        raise AppError('AI 英文标题缺失或超长',code='amazon_ai_invalid',status_code=422)
    attributes['item_name']=field(title.strip(),language_tag='en_US')
    if 'brand' in props:attributes['brand']=field(snapshot['brand'],language_tag='en_US')
    # Parent schemas can also require bullets and description. Parents have copy
    # but never an offer, inventory, shipping or product image payload.
    if not parent or 'bullet_point' in definition['schema'].get('required',[]) or 'product_description' in definition['schema'].get('required',[]):
        bullets=content.get('bullet_points') or []
        description=content.get('description') or ''
        if not isinstance(bullets,list) or not 1<=len(bullets)<=5 or any(not isinstance(b,str) or not b.strip() or len(b)>700 for b in bullets) or not isinstance(description,str) or not description.strip():
            raise AppError('AI 英文卖点或描述无效',code='amazon_ai_invalid',status_code=422)
        attributes['bullet_point']=[{'value':b,'language_tag':'en_US','marketplace_id':MARKET} for b in bullets]
        attributes['product_description']=field(description,language_tag='en_US')
    if not parent:
        if content.get('keywords') and 'generic_keyword' in props:
            if len(str(content['keywords']).encode())>249:
                raise AppError('英文后台关键词超过249字节',code='amazon_ai_invalid',status_code=422)
            attributes['generic_keyword']=field(content['keywords'],language_tag='en_US')
        attributes['supplier_declared_has_product_identifier_exemption']=field(True)
        attributes['purchasable_offer']=[{'currency':'USD','marketplace_id':MARKET,
            'our_price':[{'schedule':[{'value_with_tax':float(item['pricing']['price'])}]}]}]
        if 'list_price' in props and 'list_price' not in attributes:
            # Amazon explicitly permits zero when no suggested retail price is
            # known. Never invent a crossed-out price from our computed offer.
            list_item=resolve(definition['schema'],resolve(definition['schema'],props['list_price']).get('items',{}))
            list_props=list_item.get('properties',{})
            value_rule=resolve(definition['schema'],list_props.get('value',{}))
            if 'enter 0' in value_rule.get('description','').lower() and value_rule.get('minimum',0)<=0:
                list_value={'currency':'USD','value':0}
                if 'marketplace_id' in list_props:list_value['marketplace_id']=MARKET
                attributes['list_price']=[list_value]
        attributes['fulfillment_availability']=[{'fulfillment_channel_code':'DEFAULT',
            'quantity':snapshot['quantity'],'lead_time_to_ship_max_days':snapshot['handling_days']}]
        attributes['merchant_shipping_group']=field(snapshot['shipping_template'])
        attributes['condition_type']=field('new_new')
        images=item.get('images') or []
        if not images:raise AppError('没有可公开访问的上架图片',code='amazon_images_missing',status_code=422)
        for index,url in enumerate(images[:10]):
            k='main_product_image_locator' if index==0 else f'other_product_image_locator_{index}'
            if k in props:attributes[k]=[{'media_location':url,'marketplace_id':MARKET}]
    if snapshot.get('parent_sku'):
        theme=snapshot['variation_theme']
        if theme not in choices(definition['schema'],'variation_theme','name'):
            raise AppError('所选变体主题不在当前类目允许范围或已被弃用',code='amazon_variation_invalid',status_code=422)
        attributes['parentage_level']=field('parent' if parent else 'child')
        theme_item=resolve(definition['schema'],resolve(definition['schema'],props['variation_theme']).get('items',{}))
        theme_value={'name':theme}
        if 'marketplace_id' in theme_item.get('properties',{}):theme_value['marketplace_id']=MARKET
        attributes['variation_theme']=[theme_value]
        attributes['child_parent_sku_relationship']=[{'child_relationship_type':'variation','marketplace_id':MARKET,
            **({} if parent else {'parent_sku':snapshot['parent_sku']})}]
    # Single listings do not need parentage attributes when schema omits them.
    elif 'parentage_level' in props:attributes['parentage_level']=field('none')
    return {'productType':snapshot['product_type'],'requirements':'LISTING','attributes':attributes}

def validate_family(snapshot, bodies):
    if not snapshot.get('parent_sku'):return []
    parent=next((b for b in bodies if b['sku']==snapshot['parent_sku']),None)
    if not parent or any(k in parent['body']['attributes'] for k in ('purchasable_offer','fulfillment_availability')):
        raise AppError('父商品必须为不可购买的变体容器',code='amazon_variation_invalid',status_code=422)
    signatures=set()
    theme=snapshot['variation_theme'].upper()
    groups=[]
    for part in theme.split('/'):
        if part in {'SIZE','SIZE_NAME'}:groups.append({'size','size_name','shirt_size'})
        elif part in {'COLOR','COLOR_NAME'}:groups.append({'color'})
        elif part=='PATTERN':groups.append({'pattern'})
        elif part in {'STYLE','STYLE_NAME'}:groups.append({'style','style_name'})
        elif part=='NUMBER_OF_ITEMS':groups.append({'number_of_items'})
        else:raise AppError('此变体主题尚未建立可区分规则，暂不开放合并',code='amazon_variation_invalid',status_code=422)
    for row in bodies:
        if row['sku']==snapshot['parent_sku']:continue
        a=row['body']['attributes'];values={k:v for k,v in a.items() if any(k in g for g in groups)}
        key=digest(values)
        if any(not any(k in a for k in group) for group in groups) or key in signatures:
            raise AppError('子商品的规格、颜色或图案重复/缺失，不能建立可区分变体',code='amazon_variation_invalid',status_code=422)
        signatures.add(key)
    return []
