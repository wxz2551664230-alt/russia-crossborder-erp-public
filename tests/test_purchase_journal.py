import asyncio
from pathlib import Path
import pytest


@pytest.fixture
def journal(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'client/desktop'))
    import purchase_journal
    return purchase_journal


def test_done_write_recovers_result_without_duplicate_or_plaintext(tmp_path,journal):
    calls=[]
    async def invoke():calls.append(True);return {'id':'supplier-id','address':'PRIVATE_ADDRESS'}
    first=asyncio.run(journal.write_once(tmp_path,'task/group/create',{'content':b'image'},invoke,lambda:None))
    assert asyncio.run(journal.write_once(tmp_path,'task/group/create',{'content':b'image'},invoke,lambda:None))==first
    assert calls==[True]
    assert all(b'PRIVATE_ADDRESS' not in p.read_bytes() for p in tmp_path.iterdir())
    with pytest.raises(journal.PurchaseOutcomeUnknown):
        asyncio.run(journal.write_once(tmp_path,'task/group/create',{'content':b'other'},invoke,lambda:None))
    assert calls==[True]


def test_unknown_write_never_replays(tmp_path,journal):
    calls=[]
    async def invoke():calls.append(True);raise TimeoutError('private supplier response')
    for _ in range(2):
        with pytest.raises(journal.PurchaseOutcomeUnknown):
            asyncio.run(journal.write_once(tmp_path,'task/group/create',{},invoke,lambda:None))
    assert calls==[True]


def test_lease_loss_after_write_preserves_result(tmp_path,journal):
    checks=[];calls=[]
    def check():
        checks.append(True)
        if len(checks)==2:raise RuntimeError('lease lost')
    async def invoke():calls.append(True);return {'id':'known'}
    with pytest.raises(RuntimeError,match='lease lost'):
        asyncio.run(journal.write_once(tmp_path,'task/group/create',{},invoke,check))
    result=asyncio.run(journal.write_once(tmp_path,'task/group/create',{},invoke,lambda:None))
    assert result=={'id':'known'} and calls==[True]


def test_lease_loss_before_write_makes_no_intent_or_supplier_call(tmp_path,journal):
    def check():raise RuntimeError('lease lost')
    async def invoke():pytest.fail('Must not call supplier')
    with pytest.raises(RuntimeError):
        asyncio.run(journal.write_once(tmp_path,'task/group/create',{},invoke,check))
    assert list(tmp_path.iterdir())==[]


def test_concurrent_identical_writes_are_not_sent_twice(tmp_path,journal):
    calls=[]
    async def run():
        entered=asyncio.Event();release=asyncio.Event()
        async def invoke():
            calls.append(True);entered.set();await release.wait();return {'id':'only'}
        first=asyncio.create_task(journal.write_once(tmp_path,'same',{},invoke,lambda:None))
        await entered.wait()
        try:
            with pytest.raises(journal.PurchaseOutcomeUnknown):
                await journal.write_once(tmp_path,'same',{},invoke,lambda:None)
        finally:release.set()
        assert await first=={'id':'only'}
    asyncio.run(run())
    assert calls==[True]
