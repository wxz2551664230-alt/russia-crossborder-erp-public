from contextlib import ExitStack
import pytest
from fastapi.testclient import TestClient
from deploy.pilot_gateway import app
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.models import Job, WorkerNode
from erp.core.jsonutil import dumps, loads
from erp.services.accounts import create_user

@pytest.fixture
def team(monkeypatch):
    settings=get_settings()
    monkeypatch.setattr(settings,'multiuser_enabled',True)
    monkeypatch.setattr(settings,'runtime_mode','server')
    monkeypatch.setattr(settings,'session_cookie_secure',False)
    with SessionLocal() as db:
        for name,role in [('owner','operator'),('other','operator'),('admin','admin'),('viewer','viewer')]:
            create_user(db,username=name,display_name=name,password='isolated-control-password',role=role)
    with ExitStack() as stack:
        clients={name:stack.enter_context(TestClient(app)) for name in ['owner','other','admin','viewer']}
        for name,client in clients.items():
            assert client.post('/api/v1/auth/login',json={'username':name,'password':'isolated-control-password'}).status_code==200
        worker='pc-'+'a'*32
        assert clients['owner'].post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'original PC'}).status_code==200
        yield clients,worker

def submit(client):
    r=client.post('/api/v1/execution-devices/probe');assert r.status_code==202,r.text
    return r.json()['job_id']

@pytest.mark.parametrize('action',['pause','resume','cancel','retry'])
def test_other_accounts_cannot_control_shared_tasks(team,action):
    clients,worker=team
    job=submit(clients['owner'])
    for name in ['other','admin','viewer']:
        r=clients[name].post(f'/api/v1/jobs/{job}/{action}')
        assert r.status_code==403,r.text
    with SessionLocal() as db:assert db.get(Job,job).status=='queued'

def test_pause_resume_cancel_retry_keep_original_device_and_attempt_fencing(team):
    clients,worker=team;client=clients['owner'];job=submit(client)
    control=f'/api/v1/jobs/{job}/'
    base='/api/v1/execution-devices/local/'+worker
    report=base+'/jobs/'+job+'/'
    assert client.post(control+'pause').json()['status']=='paused'
    assert client.post(base+'/claim').json() is None
    assert client.post(control+'resume').json()['target_worker_id']==worker
    assert client.post(base+'/claim').json()['attempts']==1
    assert client.post(control+'pause').json()['pause_requested']
    assert client.post(report+'heartbeat',headers={'X-Worker-Attempt':'1'}).status_code==409
    assert client.get(control[:-1]).json()['status']=='paused'
    assert client.post(control+'resume').json()['status']=='queued'
    assert client.post(base+'/claim').json()['attempts']==2
    assert client.post(report+'complete',headers={'X-Worker-Attempt':'1'},json={'result':{'ok':True}}).status_code==409
    assert client.post(control+'cancel').json()['cancel_requested']
    assert client.post(report+'complete',headers={'X-Worker-Attempt':'2'},json={'result':{'ok':True}}).status_code==409
    assert client.get(control[:-1]).json()['status']=='cancelled'
    assert client.post(control+'retry').json()['target_worker_id']==worker
    assert client.post(base+'/claim').json()['attempts']==3
    assert client.post(report+'complete',headers={'X-Worker-Attempt':'3'},json={'result':{'ok':True}}).json()['status']=='completed'
    assert client.post(control+'retry').status_code==409

def test_unadapted_historical_jobs_cannot_be_restarted(team):
    clients,_=team;client=clients['owner'];job=submit(client)
    with SessionLocal() as db:
        row=db.get(Job,job);row.type='pod_native_composition';row.status='failed';db.commit()
    assert client.post(f'/api/v1/jobs/{job}/retry').status_code==409


def test_manual_procurement_retry_grants_one_claim_without_automatic_retry(team):
    clients,worker=team;client=clients['owner'];job=submit(client)
    with SessionLocal() as db:
        row=db.get(Job,job)
        row.type='pod_purchase_submit';row.status='failed'
        row.attempts=1;row.max_attempts=1;row.error_code='local_execution_failed'
        device=db.get(WorkerNode,worker)
        device.capabilities_json=dumps(loads(device.capabilities_json,[])+['pod_purchase_submit'])
        db.commit()
    result=client.post(f'/api/v1/jobs/{job}/retry')
    assert result.status_code==200,result.text
    with SessionLocal() as db:
        row=db.get(Job,job)
        assert row.status=='queued' and row.attempts==1 and row.max_attempts==2
    claimed=client.post(f'/api/v1/execution-devices/local/{worker}/claim').json()
    assert claimed['id']==job and claimed['attempts']==2
    assert client.post(f'/api/v1/execution-devices/local/{worker}/claim').json() is None


def test_successful_local_completion_clears_previous_attempt_error(team):
    clients,worker=team;client=clients['owner'];job=submit(client)
    base=f'/api/v1/execution-devices/local/{worker}'
    assert client.post(base+'/claim').json()['id']==job
    with SessionLocal() as db:
        row=db.get(Job,job)
        row.error_code='local_execution_failed'
        row.error_message='previous attempt failed'
        db.commit()
    result=client.post(base+f'/jobs/{job}/complete',
                       headers={'X-Worker-Attempt':'1'},json={'result':{'ok':True}})
    assert result.status_code==200,result.text
    assert result.json()['status']=='completed'
    assert result.json()['error_code']==''
    assert result.json()['error_message']==''

def test_diagnostic_task_never_reads_platform_encryption_keys(team,monkeypatch):
    from erp.core.security import vault
    def forbidden(*args,**kwargs):raise AssertionError('diagnostic tasks need no secrets')
    monkeypatch.setattr(vault,'encrypt',forbidden)
    clients,_=team
    submit(clients['owner'])

def test_paused_client_returns_without_reporting_failure(monkeypatch,tmp_path):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import local_worker
    requests=[]
    class Client:
        def get(self,path):return {'status':'paused'}
        def request(self,*args,**kwargs):requests.append(args);raise AssertionError('No failure/complete may be sent')
    def interrupted(*args):raise ValueError('stopped while reading')
    monkeypatch.setattr(local_worker,'restore',interrupted)
    local_worker.execute_job(Client(),'/local/test',{'id':'a'*32,'type':'pod_library_restore','attempts':1},tmp_path)
    assert requests==[]

def test_unknown_completion_is_read_back_without_resending(monkeypatch,tmp_path):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import local_worker
    result={'files':[], 'folder':str(tmp_path)}
    sent=[]
    class Client:
        def get(self,path):return {'status':'completed','steps':[{'details':result}]}
        def request(self,method,path,*args,**kwargs):
            sent.append(path)
            assert path.endswith('/complete')
            raise ValueError('reply lost after commit')
    monkeypatch.setattr(local_worker,'restore',lambda *args:result)
    local_worker.execute_job(Client(),'/local/test',{'id':'a'*32,'type':'pod_library_restore','attempts':1},tmp_path)
    assert len(sent)==1
