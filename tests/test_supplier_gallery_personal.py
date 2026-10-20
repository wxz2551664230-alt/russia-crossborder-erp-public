"""Supplier gallery remote reads stay on the initiating computer."""
import threading
from pathlib import Path

from erp.core.database import SessionLocal
from erp.core.security import vault
from erp.domains.pod.models import PodRemoteGalleryAsset
from erp.models import IntegrationConnection, Job
from test_personal_job_controls import team


def test_gallery_sync_original_pc_and_frozen_results(team):
    clients,worker=team
    owner=clients['owner']
    assert owner.post('/api/v1/execution-devices/local/register',json={
        'id':worker,'name':'original PC','supplier_gallery_version':1}).status_code==200
    with SessionLocal() as db:
        db.add(IntegrationConnection(provider='hicustom',name='gallery-test',enabled=True,
                  base_url='https://supplier.example.test',secrets_encrypted=vault.encrypt({
                      'app_key':'test-key','app_secret':'test-secret'})))
        a=PodRemoteGalleryAsset(supplier='hicustom',gallery_code='remote-present',protected=True)
        b=PodRemoteGalleryAsset(supplier='hicustom',gallery_code='remote-missing',protected=True)
        c=PodRemoteGalleryAsset(supplier='s2b',gallery_code='unsupported',protected=True)
        db.add_all([a,b,c]);db.commit()
        ids=(a.id,b.id,c.id)
    response=owner.post('/api/v1/pod/supplier-gallery/sync')
    assert response.status_code==202,response.text
    job_id=response.json()['job']['id']
    base=f'/api/v1/execution-devices/local/{worker}'
    claimed=owner.post(base+'/claim').json()
    assert claimed['id']==job_id and claimed['attempts']==1
    endpoint=base+'/jobs/'+job_id
    headers={'X-Worker-Attempt':'1'}
    assert clients['other'].get(endpoint+'/supplier-gallery-snapshot',headers=headers).status_code==403
    snapshot=owner.get(endpoint+'/supplier-gallery-snapshot',headers=headers)
    assert snapshot.status_code==200,snapshot.text
    assert snapshot.json()['connection']['secrets']['app_key']=='test-key'
    assert len(snapshot.json()['targets'])==3
    facts=[{'id':ids[0],'exists':True},{'id':ids[1],'exists':False},
           {'id':ids[2],'exists':None}]
    assert owner.post(endpoint+'/supplier-gallery-result',json={'facts':facts},
                      headers={'X-Worker-Attempt':'2'}).status_code==409
    assert owner.post(endpoint+'/supplier-gallery-result',json={'facts':facts[:-1]},
                      headers=headers).status_code==409
    assert clients['other'].post(endpoint+'/supplier-gallery-result',json={'facts':facts},
                      headers=headers).status_code==403
    result=owner.post(endpoint+'/supplier-gallery-result',json={'facts':facts},headers=headers)
    assert result.status_code==200,result.text
    assert result.json()=={'checked':2,'missing':1,'unsupported_skipped':1}
    assert owner.post(endpoint+'/supplier-gallery-result',json={'facts':facts},
                      headers=headers).json()==result.json()
    assert owner.post(endpoint+'/complete',json={'result':result.json()},
                      headers=headers).status_code==200
    with SessionLocal() as db:
        assert db.get(Job,job_id).status=='completed'
        assert not db.get(PodRemoteGalleryAsset,ids[0]).remote_deleted
        assert db.get(PodRemoteGalleryAsset,ids[1]).remote_deleted
        assert not db.get(PodRemoteGalleryAsset,ids[2]).remote_deleted


def test_gallery_worker_journals_facts_without_credentials(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client'/'desktop'))
    from supplier_gallery_worker import run
    from erp.domains.pod.integrations.hicustom import HicustomClient
    calls=[]
    def exists(self,code):
        calls.append(code)
        return code=='present'
    monkeypatch.setattr(HicustomClient,'gallery_exists',exists)
    class Client:
        def request(self,method,path,body=None,*,extra_headers=None):
            if path.endswith('/supplier-gallery-snapshot'):
                return {'targets':[{'id':'b'*32,'supplier':'hicustom','gallery_code':'present'}],
                        'connection':{'provider':'hicustom','connection_id':'c'*32,
                          'base_url':'https://supplier.example.test','config':{},
                          'secrets':{'app_key':'test-key','app_secret':'test-secret'}}},{}
            assert path.endswith('/supplier-gallery-result')
            assert body=={'facts':[{'id':'b'*32,'exists':True}]}
            return {'checked':1,'missing':0,'unsupported_skipped':0},{}
    endpoint='/execution-devices/local/pc-'+'d'*32+'/jobs/'+'a'*32
    job={'id':'a'*32,'type':'pod_supplier_gallery_sync','attempts':1}
    assert run(Client(),endpoint,job,tmp_path,threading.Event())['checked']==1
    assert run(Client(),endpoint,job,tmp_path,threading.Event())['checked']==1
    assert calls==['present']
    assert 'test-secret' not in (tmp_path/'supplier-gallery-tasks'/('a'*32)/'facts.json').read_text()


def test_worker_accepts_server_completed_result_after_old_heartbeat(tmp_path,monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client'/'desktop'))
    import local_worker,supplier_gallery_worker
    result={'checked':1,'missing':0,'unsupported_skipped':0}
    def finished(_client,_endpoint,_job,_root,lost):
        lost.set()
        return result
    monkeypatch.setattr(supplier_gallery_worker,'run',finished)
    class Client:
        def get(self,path):
            return {'status':'completed','steps':[{'details':result}]}
        def request(self,method,path,body=None,*,extra_headers=None):
            assert path.endswith('/complete') and body=={'result':result}
            return {'status':'completed'},{}
    local_worker.execute_job(Client(),'/execution-devices/local/pc-'+'d'*32,
                             {'id':'a'*32,'type':'pod_supplier_gallery_sync','attempts':1},tmp_path)
