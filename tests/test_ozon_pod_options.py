import pytest
from unittest.mock import patch
from sqlalchemy import select
from erp.core.database import SessionLocal
from erp.core.errors import ConflictError
from erp.models import CatalogProduct, Publication, PublicationStore, Store
from erp.services.publication_options import pricing_profile, publication_images
from erp.services.publications import _price

def test_batch_rates_change_actual_formula_without_mutating_store():
 snapshot={'pricing_profile':{'target_cost_profit_margin':30,'pricing_return_rate':10,'pricing_advertising_rate':0,'pricing_category_module':'家居与汽车用品','pricing_product_category':'住宅和花园'}}
 facts={'purchase_price_cny':25,'package_length_mm':300,'package_width_mm':200,'package_height_mm':30,'package_weight_g':300}
 base=_price(snapshot,facts,{})
 changed=_price(snapshot,facts,{'pricing_overrides':{'pricing_advertising_rate':15,'pricing_return_rate':20}})
 assert float(changed['price'])>float(base['price'])
 assert snapshot['pricing_profile']['pricing_advertising_rate']==0
 assert pricing_profile(snapshot,{'pricing_overrides':{'pricing_return_rate':0}})['pricing_return_rate']==0

@pytest.mark.parametrize('value',[{'pricing_return_rate':100},{'pricing_advertising_rate':-1},{'unknown':1},{'pricing_price_multiplier':0},{'pricing_return_rate':'NaN'}])
def test_invalid_overrides_are_rejected(value):
 with pytest.raises(ConflictError):pricing_profile({}, {'pricing_overrides':value})

def test_image_order_exclusion_and_foreign_asset_rejection():
 facts={'product_id':'p','assets':[{'asset_id':'a','role':'main','oss_url':'https://example/a'},{'asset_id':'b','role':'additional','oss_url':'https://example/b'},{'asset_id':'print','role':'artwork','oss_url':'https://example/print'}]}
 images=publication_images(facts,{'image_selection':{'p':['b','a']}})
 assert [x['asset_id'] for x in images]==['b','a'] and images[0]['role']=='main'
 assert [x['asset_id'] for x in publication_images(facts,{})]==['a','b']
 for ids in [['print'],['unknown'],['a','a'],[]]:
  with pytest.raises(ConflictError):publication_images(facts,{'image_selection':{'p':ids}})

def test_store_state_filters_before_pagination_and_distinguish_inflight(client):
 with SessionLocal() as d:
  s1=Store(id='s1',platform='ozon',name='one');s2=Store(id='s2',platform='ozon',name='two');d.add_all([s1,s2]);d.flush()
  for id in ['listed','working','failed','new']:d.add(CatalogProduct(id=id,sku=id,source_type='pod',name=id,status='ready'))
  d.flush()
  for id,status,external in [('listed','stock_pending','100'),('working','import_pending',''),('failed','failed','')]:
   p=Publication(id='pub-'+id,batch_id='batch',product_id=id);d.add(p);d.flush();d.add(PublicationStore(publication_id=p.id,store_id='s1',offer_id=id,status=status,external_product_id=external,idempotency_key=id))
  d.commit()
 def query(state,store='s1',size=10):return client.get('/api/v1/catalog/products',params={'ozon_store_id':store,'ozon_state':state,'page_size':size}).json()
 assert query('available',size=1)['total']==2
 assert {x['id'] for x in query('available')['items']}=={'failed','new'}
 assert {x['id'] for x in query('listed')['items']}=={'listed'}
 assert {x['id'] for x in query('processing')['items']}=={'working'}
 assert {x['id'] for x in query('failed')['items']}=={'failed'}
 assert query('available','s2')['total']==4
 assert query('unlisted')['total']==3
 assert client.get('/api/v1/catalog/products?ozon_state=listed').status_code==409


def test_new_options_require_compatible_worker_before_paid_preparation():
 from erp.services.publication_options import require_options_client
 for version in ['', '0.6.52']:
  with pytest.raises(ConflictError):require_options_client({'pricing_overrides':{'pricing_advertising_rate':10}},version)
 require_options_client({'image_selection':{'p':['a']}},'0.6.53')
 require_options_client({},'0.6.40')


def test_pricing_category_override_is_validated_and_can_inherit_shop_category():
 p=pricing_profile({}, {'pricing_overrides':{'pricing_category_module':'家居与汽车用品','pricing_product_category':'住宅和花园'}})
 assert p['pricing_product_category']=='住宅和花园'
 with pytest.raises(ConflictError):pricing_profile({}, {'pricing_overrides':{'pricing_category_module':'家居与汽车用品','pricing_product_category':'不存在'}})


def test_pricing_options_are_readonly_publication_reference_data(client):
 r=client.get('/api/v1/publications/pricing-options')
 assert r.status_code==200 and r.json()['modules']
 assert set(r.json())=={'modules','stores'}
 from erp.core.access import permitted
 assert permitted({'role':'operator'},'GET','/publications/pricing-options')
 assert not permitted({'role':'operator'},'GET','/settings')


def test_pod_template_uses_each_products_own_assets_and_freezes_order():
 from erp.services.publication_options import resolve_pod_image_template
 rules={'pod_image_template':['side.jpg','main.jpg']}
 for identity in ['sample','other']:
  facts={'product_id':identity,'sku':identity,'source_type':'pod','assets':[{'asset_id':identity+'-main','name':'main.jpg','role':'main','oss_url':'https://example/'+identity+'/main'},{'asset_id':identity+'-side','name':'side.jpg','role':'additional','oss_url':'https://example/'+identity+'/side'},{'asset_id':'print','name':'print.jpg','role':'artwork','oss_url':'https://example/print'}]}
  requirements=resolve_pod_image_template(facts,rules)
  assert 'pod_image_template' not in requirements
  images=publication_images(facts,requirements)
  assert [x['asset_id'] for x in images]==[identity+'-side',identity+'-main']
  facts['assets']=images
  assert publication_images(facts,requirements)==images
  with pytest.raises(ConflictError):resolve_pod_image_template(facts,{'pod_image_template':['print.jpg']})
 assert rules=={'pod_image_template':['side.jpg','main.jpg']}
 assert resolve_pod_image_template({'source_type':'follow'},rules)=={}


def test_pod_category_is_scoped_and_applied_before_pagination(client):
 with SessionLocal() as db:
  db.add_all([CatalogProduct(id='p1',sku='p1',source_type='pod',status='ready',category='桌布'),CatalogProduct(id='p2',sku='p2',source_type='pod',status='ready',category='浴帘'),CatalogProduct(id='p3',sku='p3',source_type='pod',status='ready',category='桌布'),CatalogProduct(id='f1',sku='f1',source_type='follow',status='ready',category='其他')]);db.commit()
 result=client.get('/api/v1/catalog/products',params={'source_type':'pod','pod_category':'桌布','page_size':1}).json()
 assert result['total']==2 and len(result['items'])==1
 assert {x['label'] for x in result['pod_category_options']}=={'桌布','浴帘'}
 result=client.get('/api/v1/catalog/products',params={'source_type':'follow','pod_category':'桌布'}).json()
 assert result['total']==1 and result['pod_category_options']==[]


def test_pricing_options_show_original_values_without_secrets(client):
 from erp.models import SystemSetting
 from erp.core.jsonutil import dumps
 with SessionLocal() as db:
  db.add(Store(id='original-shop',platform='ozon',name='原值店'))
  db.add(SystemSetting(key='pricing',value_json=dumps({'pricing_advertising_rate':12,'pricing_return_rate':0,'pricing_category_module':'家居与汽车用品','pricing_product_category':'住宅和花园','secret':'must-not-leak'})))
  db.commit()
 result=client.get('/api/v1/publications/pricing-options')
 assert result.status_code==200
 store=result.json()['stores'][0]
 assert store['store_id']=='original-shop'
 assert store['values']['pricing_advertising_rate']==12
 assert store['values']['pricing_return_rate']==0
 assert store['values']['pricing_sticker_fee']==2.5
 assert 'secret' not in store['values'] and 'must-not-leak' not in result.text
