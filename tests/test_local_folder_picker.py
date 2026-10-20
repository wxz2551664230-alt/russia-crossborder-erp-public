import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'client' / 'desktop'))
from local_picker import make_server  # noqa: E402


def test_local_identity_exposes_only_device_and_user_to_erp_origin(tmp_path, monkeypatch):
    import erp_desktop
    monkeypatch.setenv('RU_ERP_CLIENT_HOME', str(tmp_path))
    erp_desktop.save_profile({'server': 'https://erp.example.test', 'cookie': 'private-cookie', 'user': {'id': 'me'}})
    (tmp_path / 'device-id.txt').write_text('pc-test', encoding='ascii')
    server = make_server('https://erp.example.test', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f'http://127.0.0.1:{server.server_port}/status'
    try:
        request = urllib.request.Request(url, headers={'Origin': 'https://erp.example.test'})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.load(response) == {'ready': True, 'worker_id': 'pc-test', 'user_id': 'me'}
        request = urllib.request.Request(url, method='OPTIONS', headers={'Origin': 'https://erp.example.test'})
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 204 and 'GET' in response.headers['Access-Control-Allow-Methods']
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_browser_can_pick_only_from_logged_in_erp_origin():
    calls = []
    server = make_server('https://erp.example.test', port=0,
                         picker=lambda title, initial: calls.append((title, initial)) or 'D:\\Products')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/select-directory'
    try:
        health = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/status',
                                        headers={'Origin': 'https://erp.example.test'})
        with urllib.request.urlopen(health, timeout=2) as response:
            assert json.load(response) == {'ready': True}
        options = urllib.request.Request(url, method='OPTIONS', headers={
            'Origin': 'https://erp.example.test', 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Private-Network': 'true'})
        with urllib.request.urlopen(options, timeout=2) as response:
            assert response.status == 204
            assert response.headers['Access-Control-Allow-Private-Network'] == 'true'
            assert response.headers['Access-Control-Allow-Origin'] == 'https://erp.example.test'

        body = json.dumps({'title': '选择产品图片根目录', 'initial_dir': 'D:\\Old'}).encode()
        denied = urllib.request.Request(url, body, method='POST', headers={
            'Origin': 'https://unrelated.example.test', 'Content-Type': 'application/json'})
        try:
            urllib.request.urlopen(denied, timeout=2)
            assert False, 'foreign origin was accepted'
        except urllib.error.HTTPError as error:
            assert error.code == 403
        assert not calls

        accepted = urllib.request.Request(url, body, method='POST', headers={
            'Origin': 'https://erp.example.test', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(accepted, timeout=2) as response:
            assert json.load(response) == {'path': 'D:\\Products', 'cancelled': False}
        assert calls == [('选择产品图片根目录', 'D:\\Old')]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
