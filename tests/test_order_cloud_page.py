from io import BytesIO
from urllib.parse import quote
from openpyxl import load_workbook
from sqlalchemy import select
from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.domains.orders import api
from erp.models import Store, IntegrationConnection
from test_pod_orders import orders, posting
from test_personal_job_controls import team


def test_original_page_import_details_tracking_export_and_roles(team,orders,monkeypatch):
    monkeypatch.setattr(api,'manager',lambda _:orders)
    clients,_=team;owner=clients['owner'];viewer=clients['viewer'];base='/api/v1/orders/pod'
    with SessionLocal() as db:
        store=Store(name='isolated',platform='ozon',enabled=True);db.add(store);db.commit();identity=store.id
    body={'store_id':identity,'postings':[posting('PAGE-TEST')]}
    assert viewer.post(base+'/import',json=body).status_code==403
    response=owner.post(base+'/import',json=body);assert response.status_code==200,response.text
    listing=viewer.get(base).json();assert listing['total']==1
    key=listing['orders'][0]['order_key'];path=base+'/'+quote(key,safe='')
    assert viewer.get(path).status_code==200
    recipient={name:'private-address' for name in orders.REQUIRED_ADDRESS_FIELDS}
    assert owner.put(path+'/recipient',json={'recipient':recipient}).status_code==200
    assert owner.get(path).json()['recipient']['firstname']=='private-address'
    tracking={'tracking_number':'YT123456789','tracking_company':'圆通速递'}
    assert viewer.post(path+'/manual-tracking',json=tracking).status_code==403
    response=owner.post(path+'/manual-tracking',json=tracking);assert response.status_code==200,response.text
    assert response.json()['workflow_status']=='manual_tracking'
    export=owner.post(base+'/tracking-export',json={'order_keys':[key]})
    assert export.status_code==200,export.text[:100]
    workbook=load_workbook(BytesIO(export.content))
    assert workbook.active[3][3].value=='YT123456789'
    assert owner.post(base+'/batch/preview',json={'order_keys':[key]}).status_code==409
    assert owner.post(base+'/manual-complete',json={'order_keys':[key]}).json()=={'completed':1}
    assert viewer.get(base+'/batch/groups').status_code==200
    assert viewer.get(base+'/product-options?search=POD').status_code==200
    assert owner.post(base+'/tms/batch-submit',json={'order_keys':[key],'confirmation':'CONFIRM_REAL_TMS'}).status_code==403


def test_shared_purchase_addresses_keep_encrypted_storage_and_crud(team,orders,monkeypatch):
    clients,_=team;owner=clients['owner'];viewer=clients['viewer'];base='/api/v1/orders/pod/addresses'
    address={k:'PRIVATE_ADDRESS' for k in ['country','region','city','street','firstname','lastname','mobile_phone']}
    created=owner.post(base,json={'name':'isolated address','address':address,'is_default':True})
    assert created.status_code==200,created.text
    identity=created.json()['items'][0]['id']
    assert viewer.get(base).json()['items'][0]['address']==created.json()['items'][0]['address']
    with SessionLocal() as db:
        row=db.scalar(select(IntegrationConnection).where(IntegrationConnection.provider=='procurement'))
        assert 'PRIVATE_ADDRESS' not in str(row.secrets_encrypted)
        assert vault.decrypt(row.secrets_encrypted)['saved_addresses'][0]['id']==identity
    assert viewer.delete(base+'/'+identity).status_code==403
    assert owner.delete(base+'/'+identity).json()=={'items':[]}
