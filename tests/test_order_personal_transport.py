"""Platform reads used by the personal worker must not touch shared order DBs."""
import pytest

from erp.domains.orders import service


@pytest.mark.parametrize('platform,days,expected', [('wb',90,30),('ozon',90,90)])
def test_order_fetch_preserves_platform_parameters_and_closes(monkeypatch,platform,days,expected):
    calls=[]
    class Client:
        def __init__(self,config): calls.append(('config',config))
        async def orders(self,**kwargs): calls.append(('orders',kwargs)); return [{'id':1}]
        async def postings(self,**kwargs): calls.append(('postings',kwargs)); return [{'id':1}]
        async def aclose(self): calls.append(('closed',))
    monkeypatch.setattr(service,'WildberriesOrderClient',Client)
    monkeypatch.setattr(service,'OzonClient',Client)
    monkeypatch.setattr(service,'manager',lambda *_:pytest.fail('platform fetch accessed shared database'))
    assert service.fetch_platform_orders({'platform':platform},{'days':days,'status':'awaiting_packaging'})==[{'id':1}]
    assert calls[1]==(('orders',{'days':expected}) if platform=='wb' else ('postings',{'days':expected,'status':'awaiting_packaging'}))
    assert calls[-1]==('closed',)


def test_order_fetch_closes_after_platform_failure(monkeypatch):
    closed=[]
    class Client:
        def __init__(self,config): pass
        async def orders(self,**kwargs): raise TimeoutError('platform unavailable')
        async def aclose(self): closed.append(True)
    monkeypatch.setattr(service,'WildberriesOrderClient',Client)
    with pytest.raises(TimeoutError):service.fetch_platform_orders({'platform':'wb'},{'days':1})
    assert closed==[True]


def test_image_fetch_matches_exact_offer_and_never_updates_database(monkeypatch):
    closed=[]
    class Client:
        def __init__(self,config): pass
        def find_card(self,offer):
            return {'vendorCode':offer if offer=='exact' else 'wrong','photos':[{'big':'https://images.example/card.jpg'}]}
        def close(self): closed.append(True)
    monkeypatch.setattr(service,'_WbContentClient',Client)
    monkeypatch.setattr(service,'manager',lambda *_:pytest.fail('image fetch accessed shared database'))
    assert service.fetch_platform_images({'platform':'wb'},['exact','alias'])=={'exact':'https://images.example/card.jpg'}
    assert closed==[True]


def test_image_fetch_stops_when_personal_lease_is_lost(monkeypatch):
    calls=[]
    def check():raise RuntimeError('lease lost')
    monkeypatch.setattr(service,'_WbContentClient',lambda *_:calls.append('opened'))
    with pytest.raises(RuntimeError,match='lease lost'):
        service.fetch_platform_images({'platform':'wb'},['offer'],check)
    assert calls==[]
