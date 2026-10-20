import json
from io import BytesIO
from threading import Event, Thread

import pytest
import requests

from client.desktop.ozon_paid_transport import PaidCallUnknown, paid_transport


def response(status=200, body=b'{"ok":true}'):
    value = requests.Response()
    value.status_code = status
    value._content = body
    return value


def test_confirmed_response_replays_only_for_same_job_and_store(tmp_path):
    calls = []

    def send(kind, _method, _url, **_kwargs):
        calls.append(kind)
        return response()

    journal = tmp_path / 'paid-calls'
    request = paid_transport(send, journal, scope='job-a-store-1', lease=lambda: None)
    args = {'headers': {'Authorization': 'Bearer secret-key'},
            'json': {'model': 'model-a', 'messages': [{'content': 'new text'}]}}
    assert request('text', None, 'https://ai.example.invalid', **args).json()['ok']
    assert len(calls) == 1
    saved = next(journal.glob('*.json')).read_text(encoding='utf-8')
    assert 'secret-key' not in saved and 'new text' not in saved
    restarted = paid_transport(send, journal, scope='job-a-store-1', lease=lambda: None)
    assert restarted('text', None, 'https://ai.example.invalid', **args).json()['ok']
    assert len(calls) == 1
    other_store = paid_transport(send, journal, scope='job-a-store-2', lease=lambda: None)
    assert other_store('text', None, 'https://ai.example.invalid', **args).json()['ok']
    assert len(calls) == 2


def test_unknown_paid_call_retries_are_bounded_and_can_resume(tmp_path):
    calls = []

    def uncertain(*_args, **_kwargs):
        calls.append('sent')
        raise requests.ConnectionError('outcome unknown')

    journal = tmp_path / 'paid-calls'
    with pytest.raises(PaidCallUnknown):
        paid_transport(uncertain, journal, scope='job-b-store-1', lease=lambda: None)(
            'image', None, 'https://ai.example.invalid', json={'model': 'image'})
    assert len(calls) == 3
    with pytest.raises(PaidCallUnknown):
        paid_transport(uncertain, journal, scope='job-b-store-1', lease=lambda: None)(
            'image', None, 'https://ai.example.invalid', json={'model': 'image'})
    assert len(calls) == 6
    assert json.loads(next(journal.glob('*.json')).read_text())['state'] == 'pending'


def test_image_file_is_fingerprinted_without_consuming_original_stream(tmp_path):
    observed = []

    def send(_kind, _method, _url, **kwargs):
        observed.append(kwargs['files']['image'][1].read())
        return response(body=b'{"image":"done"}')

    journal = tmp_path / 'paid-calls'
    stream = BytesIO(b'123456')
    image = ('input.jpg', stream, 'image/jpeg')
    request = paid_transport(send, journal, scope='job-c-store-1', lease=lambda: None)
    assert request('image', None, 'https://ai.example.invalid', files={'image': image},
                   data={'model': 'image-a'}).json()['image'] == 'done'
    assert observed == [b'123456']
    stream.seek(0)
    replay = paid_transport(send, journal, scope='job-c-store-1', lease=lambda: None)
    assert replay('image', None, 'https://ai.example.invalid', files={'image': image},
                  data={'model': 'image-a'}).json()['image'] == 'done'
    assert observed == [b'123456']


def test_server_error_stays_unknown(tmp_path):
    request = paid_transport(lambda *_args, **_kwargs: response(503),
                             tmp_path / 'paid-calls', scope='job-d-store-1', lease=lambda: None)
    with pytest.raises(PaidCallUnknown):
        request('text', None, 'https://ai.example.invalid', json={'model': 'x'})


def test_two_executors_cannot_send_the_same_paid_call(tmp_path):
    entered, release = Event(), Event()
    calls = []

    def send(*_args, **_kwargs):
        calls.append('sent')
        entered.set()
        assert release.wait(5)
        return response()

    directory = tmp_path / 'paid-calls'
    first = paid_transport(send, directory, scope='job-e-store-1', lease=lambda: None)
    second = paid_transport(send, directory, scope='job-e-store-1', lease=lambda: None)
    thread = Thread(target=lambda: first('text', None, 'https://ai.example.invalid', json={'model': 'x'}))
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(PaidCallUnknown):
            second('text', None, 'https://ai.example.invalid', json={'model': 'x'})
        assert calls == ['sent']
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()

def test_pre_sender_failure_auto_recovers_without_duplicate_payment(tmp_path,monkeypatch):
    import client.desktop.ozon_paid_transport as transport
    monkeypatch.setattr(transport.time,'sleep',lambda _:None)
    attempts=[];sent=[]
    def original(kind,sender,url,**kwargs):
        attempts.append(1)
        if len(attempts)<3:raise RuntimeError('local preflight failed')
        return sender(url,**kwargs)
    def sender(*args,**kwargs):sent.append(1);return response()
    result=paid_transport(original,tmp_path,scope='auto-store',lease=lambda:None)('text',sender,'https://example.invalid',json={})
    assert result.status_code==200 and len(attempts)==3 and len(sent)==1


def test_response_survives_post_sender_accounting_failure(tmp_path):
    calls=[]
    def original(kind,sender,url,**kwargs):
        sender(url,**kwargs)
        raise RuntimeError('accounting failed after platform success')
    def sender(*args,**kwargs):calls.append(1);return response(body=b'{"saved":true}')
    request=paid_transport(original,tmp_path,scope='saved-store',lease=lambda:None)
    assert request('text',sender,'https://example.invalid',json={}).json()=={'saved':True}
    restarted=paid_transport(original,tmp_path,scope='saved-store',lease=lambda:None)
    assert restarted('text',sender,'https://example.invalid',json={}).json()=={'saved':True}
    assert len(calls)==1
