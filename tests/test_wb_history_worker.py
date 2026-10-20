import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace


def test_two_history_actions_share_one_cursor_scan(monkeypatch,tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client'/'desktop'))
    import wb_history_worker as worker
    calls=[]

    class Response:
        status_code=200
        def __init__(self,cards,cursor):self.cards=cards;self.cursor=cursor
        def raise_for_status(self):pass
        def json(self):return {'cards':self.cards,'cursor':self.cursor}

    class Session:
        def __init__(self):self.headers={}
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def post(self,url,json,timeout):
            calls.append(json['settings']['cursor'])
            if len(calls)==1:
                return Response([{'vendorCode':f'P-{i}','nmID':i+1} for i in range(100)],{'updatedAt':'2026-01-01','nmID':100})
            return Response([{'vendorCode':'P-100','nmID':101}],{})

    monkeypatch.setattr(worker.requests,'Session',Session)
    monkeypatch.setattr(worker.time,'sleep',lambda _:None)
    staged=[]
    class Client:
        def request(self,method,path,body,extra_headers):
            if path.endswith('/wb-history-snapshot'):
                return {'store_id':'store','config':{'store':{'secrets':{'token':'secret'}}}},{}
            staged.append(body)
            return {'index':body['index']},{}
    job={'attempts':1}
    first=worker.run(Client(),'/jobs/a',job,tmp_path,threading.Event())
    second=worker.run(Client(),'/jobs/b',job,tmp_path,threading.Event())
    assert len(calls)==2 and calls[0]=={'limit':100} and calls[1]['nmID']==100
    assert first==second and first['cards']==101 and first['chunks']==1
    assert len(staged)==2 and staged[0]['cards']==staged[1]['cards']
