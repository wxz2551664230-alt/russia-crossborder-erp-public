import base64,copy,hashlib,io,json,os,threading,uuid
from pathlib import Path
import pytest
from PIL import Image
from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.core.jsonutil import dumps
from erp.models import Job,JobStep,IntegrationConnection
from erp.domains.pod.desktop_composition import columns
from erp.domains.pod.ai_design import service
from test_personal_job_controls import team


def seed():
    cid=uuid.uuid4().hex
    with SessionLocal() as db:
        job=Job(type='pod_ai_design_plan',payload_json=dumps({'batch_id':cid}),idempotency_key=cid);db.add(job);db.flush()
        step=JobStep(job_id=job.id,key='plan',name='plan',position=1);db.add(step);db.flush()
        connections=[]
        for provider in ('ai_chat','ai_image','oss'):
            c=IntegrationConnection(provider=provider,name='isolated',base_url='https://isolated.invalid',config_json=dumps({'prefix':'hicustom-products'}));db.add(c);db.flush()
            value=columns(c);value.pop('secrets_encrypted');value['secrets']={'api_key':'test-only'};connections.append(value)
        manifest={'id':cid,'status':'planning','references':[],'outputs':[],'config':{'planner_connection_id':connections[0]['id'],'image_connection_id':connections[1]['id'],'planner_model':'isolated','image_model':'isolated','brief':'Test design','product_type':'runner','count':1,'size':'256x256','upscale_scale':0,'output_format':'png','quality':'high','background':'opaque'}}
        manifest['config']['aspect_ratio']='1:1'
        result={'manifest':manifest,'job':columns(job),'steps':[columns(step)],'connections':connections};db.rollback();return result


def run_private(monkeypatch,tmp_path,snapshot):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import ai_worker
    class Cloud:
        def request(self,method,path,body=None,**kwargs):return (copy.deepcopy(snapshot) if path.endswith('ai-snapshot') else {},{})
    old=dict(os.environ)
    try:return ai_worker.run(Cloud(),'/task',snapshot['job'],tmp_path,threading.Event())
    finally:os.environ.clear();os.environ.update(old);get_settings.cache_clear()


def test_original_planning_and_generation_resume_without_paid_replay(monkeypatch,tmp_path):
    snap=seed();calls=[]
    plan={'items':[{'index':1,'title':'test','prompt':'blue floral pattern','selected':True}]}
    image=io.BytesIO();Image.new('RGB',(20,24),'blue').save(image,'PNG');content=image.getvalue()
    def request(kind,*args,**kwargs):
        calls.append(kind)
        return {'choices':[{'message':{'content':json.dumps(plan)}}]} if kind=='text' else {'data':[{'b64_json':base64.b64encode(content).decode()}]}
    monkeypatch.setattr(service,'_request',request)
    result=run_private(monkeypatch,tmp_path,snap)
    assert result['plan']['items'][0]['prompt']=='blue floral pattern'
    assert run_private(monkeypatch,tmp_path,snap)==result and calls==['text']
    snap['manifest'].update(status='queued',plan=result['plan']);snap['job'].update(id=uuid.uuid4().hex,type='pod_ai_design_generate')
    snap['steps'][0].update(id=uuid.uuid4().hex,job_id=snap['job']['id'],key='generate')
    class OSS:
        def upload_artwork(self,path,digest):return {'oss_bucket':'test','oss_key':path.name,'oss_url':'https://test.invalid/'+path.name,'sha256':digest,'size':path.stat().st_size}
    monkeypatch.setattr('erp.domains.pod.integrations.oss.PodOssService.from_session',lambda *args:OSS())
    result=run_private(monkeypatch,tmp_path,snap)
    assert result['outputs'][0]['artwork']['width']==20
    assert run_private(monkeypatch,tmp_path,snap)==result and calls==['text','image']


def test_unknown_paid_request_is_never_repeated(monkeypatch,tmp_path):
    snap=seed();calls=[]
    def failed(*args,**kwargs):calls.append(1);raise OSError('unknown transport outcome')
    monkeypatch.setattr(service,'_request',failed)
    for _ in range(2):
        with pytest.raises(Exception) as error:run_private(monkeypatch,tmp_path,snap)
        assert error.value.code=='ai_write_outcome_unknown'
    assert len(calls)==1


def test_cloud_upscale_requires_selected_runtime_and_owner_lease(team):
    clients,worker=team;owner=clients['owner']
    data=io.BytesIO();Image.new('RGB',(8,8),'red').save(data,'PNG')
    assert clients['viewer'].post('/api/v1/pod/ai-design/uploads',files={'image':('x.png',data.getvalue(),'image/png')}).status_code==403
    staged=owner.post('/api/v1/pod/ai-design/uploads',files={'image':('x.png',data.getvalue(),'image/png')});assert staged.status_code==201
    values={'scale':'2','uploaded_inputs':json.dumps([staged.json()['id']])}
    assert owner.post('/api/v1/pod/ai-design/upscale',data=values).status_code==409
    owner.post('/api/v1/execution-devices/local/register',json={'id':worker,'name':'PC','ai_version':1,'upscale_ready':True})
    queued=owner.post('/api/v1/pod/ai-design/upscale',data=values);assert queued.status_code==202,queued.text
    identity=queued.json()['job']['id'];base='/api/v1/execution-devices/local/'+worker
    assert owner.post(base+'/claim').json()['id']==identity
    path=base+'/jobs/'+identity+'/ai-source/'+staged.json()['id']
    for name in ('other','admin','viewer'):assert clients[name].get(path,headers={'X-Worker-Attempt':'1'}).status_code==403
    assert owner.get(path,headers={'X-Worker-Attempt':'2'}).status_code==409
    assert owner.get(path,headers={'X-Worker-Attempt':'1'}).content==data.getvalue()
    batch=queued.json()['batch']['id']
    assert clients['other'].post('/api/v1/pod/ai-design/batches/'+batch+'/generate').status_code==403
    # Invalid completion must leave both the receipt and batch unchanged.
    endpoint=base+'/jobs/'+identity
    rejected=owner.post(endpoint+'/complete',headers={'X-Worker-Attempt':'1'},json={'result':{'outputs':[]}})
    assert rejected.status_code==409
    assert owner.get('/api/v1/jobs/'+identity).json()['status']=='running'
    assert owner.get('/api/v1/pod/ai-design/batches/'+batch).json()['outputs']==[]
