from pathlib import Path
import pytest


def test_idle_outage_recovers_without_replaying_business(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import local_worker as worker
    calls=[];executed=[]
    class FakeClient:
        def request(self,method,path,body):
            if path.endswith(('/register','/select')):return {},{}
            calls.append(path)
            if len(calls)==1:raise worker.TemporaryConnectionError('offline')
            if len(calls)==2:return {'id':'a'*32},{}
            raise ValueError('登录已失效')
    monkeypatch.setattr(worker,'Client',FakeClient)
    monkeypatch.setattr(worker,'profile_dir',lambda:tmp_path)
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    monkeypatch.setattr(worker,'execute_job',lambda client,base,job,root:executed.append(job['id']))
    with pytest.raises(ValueError,match='登录已失效'):worker.run()
    assert len(calls)==3 and executed==['a'*32]
