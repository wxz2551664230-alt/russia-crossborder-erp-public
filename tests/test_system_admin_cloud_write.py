"""The existing system-management forms write shared data under admin auth."""
from deploy.pilot_gateway import allowed
from test_personal_job_controls import team


def test_admin_connection_store_and_setting_writes(team):
    clients,_=team
    admin,operator=clients['admin'],clients['owner']
    assert allowed('GET','/api/v1/settings')
    assert allowed('POST','/api/v1/connections')
    assert allowed('PATCH','/api/v1/stores/'+'a'*32)
    assert allowed('POST','/api/v1/stores/'+'a'*32+'/test')
    connection={'provider':'ai_chat','name':'isolated-admin-write',
                'base_url':'https://api.example.test','config':{'model':'sample'},
                'secrets':{'api_key':'private-test-key'},'enabled':True}
    assert operator.post('/api/v1/connections',json=connection).status_code==403
    created=admin.post('/api/v1/connections',json=connection)
    assert created.status_code==201,created.text
    connection_id=created.json()['id']
    assert 'private-test-key' not in created.text
    changed=admin.patch('/api/v1/connections/'+connection_id,
                        json={'config':{'model':'revised'}})
    assert changed.status_code==200,changed.text
    assert changed.json()['config']['model']=='revised'
    assert admin.post('/api/v1/connections/'+connection_id+'/test').json()['ok']
    stores={'platform':'wildberries','name':'isolated-store-write',
            'external_account_id':'isolated-account','connection_id':connection_id,
            'secrets':{'token':'private-wb-token'}}
    assert operator.post('/api/v1/stores',json=stores).status_code==403
    store=admin.post('/api/v1/stores',json=stores)
    assert store.status_code==201,store.text
    store_id=store.json()['id']
    assert 'private-wb-token' not in store.text
    updated=admin.patch('/api/v1/stores/'+store_id,json={'name':'revised-store'})
    assert updated.status_code==200,updated.text
    assert updated.json()['name']=='revised-store'
    assert operator.put('/api/v1/settings/copywriting',json={'value':{'test':1}}).status_code==403
    saved=admin.put('/api/v1/settings/copywriting',json={'value':{'test':1}})
    assert saved.status_code==200,saved.text
    assert admin.get('/api/v1/settings').json()['profiles']['copywriting']=={'test':1}
    assert admin.delete('/api/v1/stores/'+store_id).json()['deleted']
    assert admin.delete('/api/v1/connections/'+connection_id).json()['deleted']
