import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest
import uvicorn

from erp.core.config import get_settings
from erp.core.database import SessionLocal
from erp.main import app
from erp.services.accounts import create_user

CLIENT_PATH = Path(__file__).resolve().parents[1] / "client/desktop/erp_desktop.py"
spec = importlib.util.spec_from_file_location("desktop_pilot", CLIENT_PATH)
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


@pytest.fixture
def shared_server(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "multiuser_enabled", True)
    monkeypatch.setattr(get_settings(), "runtime_mode", "server")
    monkeypatch.setattr(get_settings(), "session_cookie_secure", False)
    monkeypatch.setenv("RU_ERP_CLIENT_HOME", str(tmp_path / "new-computer"))
    with SessionLocal() as db:
        create_user(db, username="desktop-admin", display_name="Pilot admin", password="pilot-test-only-password", role="admin")
        create_user(db, username="desktop-viewer", display_name="Pilot viewer", password="pilot-test-only-password", role="viewer")
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True); thread.start()
    try:
        for _ in range(100):
            if server.started: break
            time.sleep(0.05)
        assert server.started
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True; thread.join(timeout=10); sock.close()
        assert not thread.is_alive()


def logged_client(server):
    desktop.login(server, "desktop-admin", "pilot-test-only-password")
    return desktop.Client()


def test_remembered_password_is_windows_encrypted_and_can_be_removed(tmp_path, monkeypatch):
    monkeypatch.setenv('RU_ERP_CLIENT_HOME', str(tmp_path))
    desktop.remember_login('https://erp.example.test', 'user', 'isolated-secret-password', True)
    raw = (tmp_path / 'remembered-login.dat').read_bytes()
    assert b'isolated-secret-password' not in raw
    assert desktop.remembered_login()['password'] == 'isolated-secret-password'
    desktop.remember_login('https://erp.example.test', 'user', '', False)
    assert desktop.remembered_login() == {}


def test_auto_login_only_reauthenticates_expired_session(monkeypatch):
    calls = []
    class ExpiredClient:
        def get(self, path): raise desktop.SessionExpired('expired')
    monkeypatch.setattr(desktop, 'Client', ExpiredClient)
    monkeypatch.setattr(desktop, 'remembered_login', lambda: {
        'server': 'https://erp.example.test', 'username': 'user', 'password': 'test-only'})
    monkeypatch.setattr(desktop, 'login', lambda *args, **kwargs: calls.append((args, kwargs)) or {'id': 'user'})
    assert desktop.restore_login() == {'id': 'user'}
    assert len(calls) == 1 and calls[0][1]['remember_password'] is True
    class OfflineClient:
        def get(self, path): raise desktop.TemporaryConnectionError('offline')
    monkeypatch.setattr(desktop, 'Client', OfflineClient)
    with pytest.raises(desktop.TemporaryConnectionError): desktop.restore_login()
    assert len(calls) == 1


def test_bad_password_is_not_reported_as_expired_session(shared_server):
    with pytest.raises(ValueError, match="账号或密码不正确") as error:
        desktop.login(shared_server, "desktop-admin", "wrong-test-password")
    assert "登录已失效" not in str(error.value)
    assert not (desktop.profile_dir() / "session.dat").exists()
    client = logged_client(shared_server)
    assert client.get('/auth/session')['user']['username'] == 'desktop-admin'


def product(client):
    return client.request("POST", "/catalog/products", {"sku": "DESKTOP-PILOT", "source_type": "self_selected", "name": "Shared pilot product", "notes": "Original internal note"})[0]


def test_installed_style_client_login_mcp_write_and_shared_readback(shared_server):
    client = logged_client(shared_server); row = product(client)
    encrypted = (desktop.profile_dir() / "session.dat").read_bytes()
    assert b"pilot-test-only-password" not in encrypted and client.profile["cookie"].encode() not in encrypted
    assert desktop.search_products(client, "Shared pilot")["items"][0]["id"] == row["id"]
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "erp_save_product_notes", "arguments": {"id": row["id"], "expected_notes": row["notes"], "notes": "Reviewed by local client"}}},
    ]
    result = subprocess.run([sys.executable, "-I", str(CLIENT_PATH), "mcp"], input="\n".join(json.dumps(message) for message in messages) + "\n", encoding="utf-8", capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(responses[1]["result"]["tools"]) == 11
    assert responses[2]["result"]["isError"] is False
    assert client.profile["cookie"] not in result.stdout
    other = desktop.Client(dict(client.profile))
    assert other.get(f'/catalog/products/{row["id"]}')["notes"] == "Reviewed by local client"


def test_notes_conflict_and_repeated_request_preserve_other_edits(shared_server):
    client = logged_client(shared_server); row = product(client)
    desktop.save_notes(client, row["id"], row["notes"], "First change")
    assert desktop.save_notes(client, row["id"], row["notes"], "First change")["verified"]
    with pytest.raises(ValueError, match="409"):
        desktop.save_notes(client, row["id"], row["notes"], "Stale overwrite")
    assert client.get(f'/catalog/products/{row["id"]}')["notes"] == "First change"


def test_viewer_and_logged_out_client_cannot_write(shared_server):
    admin = logged_client(shared_server); row = product(admin)
    desktop.login(shared_server, "desktop-viewer", "pilot-test-only-password")
    viewer = desktop.Client()
    with pytest.raises(ValueError, match="403"):
        desktop.save_notes(viewer, row["id"], row["notes"], "Forbidden")
    viewer.request("POST", "/auth/logout")
    with pytest.raises(ValueError, match="登录已失效"):
        viewer.get("/auth/session")


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://example.com/path", "https://example.com?token=bad"])
def test_client_rejects_unsafe_server_addresses(url):
    with pytest.raises(ValueError): desktop.server_url(url)


def test_mcp_exposes_only_scoped_write_and_validates_paths(monkeypatch):
    tools = desktop.mcp_dispatch({"id": 1, "method": "tools/list"})["result"]["tools"]
    assert [tool["name"] for tool in tools if not tool["annotations"]["readOnlyHint"]] == ["erp_save_product_notes", "erp_execute_action", "erp_stage_local_images", "erp_codex_handoff"]
    with pytest.raises(ValueError): desktop.product_id("../../settings")
    result = desktop.mcp_dispatch({"id": 2, "method": "tools/call", "params": {"name": "erp_save_product_notes", "arguments": {"id": "product", "expected_notes": "", "notes": "x", "purchase_price_cny": 0}}})
    assert result["result"]["isError"]


def test_generic_codex_action_discovery_preview_execute_and_readback(shared_server):
    client = logged_client(shared_server); row = product(client)
    listing = client.get('/codex-actions')
    assert listing['contract'] == 1
    assert [item['id'] for item in listing['actions']] == ['catalog.notes.compare_save', 'pod.ai.upscale.staged', 'pod.artwork.import.staged', 'pod.composition.batch', 'ozon.prepare.codex', 'wb.publish.codex_api', 'ozon.submit.reviewed', 'ozon.stock.add', 'ozon.promotion.add', 'wb.stock.add', 'wb.price.set']
    arguments = {'product_id': row['id'], 'expected_notes': row['notes'], 'notes': 'Generic action verified'}
    preview = client.request('POST', '/codex-actions/catalog.notes.compare_save/preview', {'arguments': arguments})[0]
    assert preview['effect']['sku'] == row['sku']
    assert client.get(f'/catalog/products/{row["id"]}')['notes'] == row['notes']
    result = client.request('POST', '/codex-actions/catalog.notes.compare_save/execute', {'preview_token': preview['preview_token']})[0]
    assert result['result']['notes'] == arguments['notes']
    assert client.get(f'/catalog/products/{row["id"]}')['notes'] == arguments['notes']
    with pytest.raises(ValueError, match='409'):
        client.request('POST', '/codex-actions/catalog.notes.compare_save/execute', {'preview_token': preview['preview_token']})


def test_generic_codex_action_rejects_unregistered_paths_and_other_user_preview(shared_server):
    owner = logged_client(shared_server); row = product(owner)
    args = {'product_id': row['id'], 'expected_notes': row['notes'], 'notes': 'New note'}
    with pytest.raises(ValueError, match='404'):
        owner.request('POST', '/codex-actions/stores.delete/preview', {'arguments': args})
    with pytest.raises(ValueError, match='400'):
        owner.request('POST', '/codex-actions/catalog.notes.compare_save/preview', {'arguments': {**args, 'price': 0}})
    preview = owner.request('POST', '/codex-actions/catalog.notes.compare_save/preview', {'arguments': args})[0]
    desktop.login(shared_server, 'desktop-viewer', 'pilot-test-only-password')
    viewer = desktop.Client()
    with pytest.raises(ValueError, match='403'):
        viewer.request('POST', '/codex-actions/catalog.notes.compare_save/execute', {'preview_token': preview['preview_token']})
    assert owner.get(f'/catalog/products/{row["id"]}')['notes'] == row['notes']


def test_pilot_gateway_exposes_only_codex_action_protocol():
    from deploy.pilot_gateway import allowed
    assert allowed('GET', '/api/v1/codex-actions')
    assert allowed('POST', '/api/v1/codex-actions/catalog.notes.compare_save/preview')
    assert allowed('POST', '/api/v1/codex-actions/catalog.notes.compare_save/execute')
    assert not allowed('POST', '/api/v1/codex-actions/../../settings/execute')


def test_generic_codex_upscale_stages_images_and_enqueues_on_same_computer(shared_server, tmp_path):
    from PIL import Image
    client = logged_client(shared_server)
    worker_id = 'pc-' + 'a' * 32
    client.request('POST', '/execution-devices/local/register', {
        'id': worker_id, 'name': 'Test computer', 'ai_version': 1, 'upscale_ready': True})
    image = tmp_path / 'design.png'
    Image.new('RGB', (20, 20), 'blue').save(image)
    staged = desktop.stage_local_images(client, [str(image)])
    assert staged['count'] == 1 and staged['staged'][0]['name'] == 'design.png'
    arguments = {'upload_ids': [staged['staged'][0]['upload_id']], 'scale': 2}
    preview = client.request('POST', '/codex-actions/pod.ai.upscale.staged/preview', {'arguments': arguments})[0]
    assert preview['effect']['worker_id'] == worker_id and preview['effect']['count'] == 1
    assert preview['effect']['images'][0]['sha256'] == staged['staged'][0]['sha256']
    first = client.request('POST', '/codex-actions/pod.ai.upscale.staged/execute', {'preview_token': preview['preview_token']})[0]
    repeated = client.request('POST', '/codex-actions/pod.ai.upscale.staged/execute', {'preview_token': preview['preview_token']})[0]
    job = first['result']['job']
    assert job['id'] == repeated['result']['job']['id']
    assert job['type'] == 'pod_ai_design_upscale' and job['target_worker_id'] == worker_id
    assert client.get('/jobs/' + job['id'])['status'] == 'queued'


def test_generic_codex_upscale_preview_fails_after_computer_switch(shared_server, tmp_path):
    from PIL import Image
    client = logged_client(shared_server)
    first_id, second_id = 'pc-' + 'b' * 32, 'pc-' + 'c' * 32
    client.request('POST', '/execution-devices/local/register', {
        'id': first_id, 'name': 'First', 'ai_version': 1, 'upscale_ready': True})
    image = tmp_path / 'design.png'; Image.new('RGB', (20, 20)).save(image)
    staged = desktop.stage_local_images(client, [str(image)])
    preview = client.request('POST', '/codex-actions/pod.ai.upscale.staged/preview', {
        'arguments': {'upload_ids': [staged['staged'][0]['upload_id']], 'scale': 4}})[0]
    client.request('POST', '/execution-devices/local/register', {
        'id': second_id, 'name': 'Second', 'ai_version': 1, 'upscale_ready': True})
    with pytest.raises(ValueError, match='409'):
        client.request('POST', '/codex-actions/pod.ai.upscale.staged/execute', {'preview_token': preview['preview_token']})


def test_generic_codex_imports_staged_artwork_into_selected_category(shared_server, tmp_path):
    from PIL import Image
    client = logged_client(shared_server)
    worker_id = 'pc-' + 'd' * 32
    client.request('POST', '/execution-devices/local/register', {
        'id': worker_id, 'name': 'Artwork computer', 'artwork_version': 1})
    category = client.request('POST', '/pod/artworks/categories', {'name': 'Square designs'})[0]['category']
    image = tmp_path / 'square.png'; Image.new('RGB', (32, 32), 'green').save(image)
    upload_id = desktop.stage_local_images(client, [str(image)])['staged'][0]['upload_id']
    preview = client.request('POST', '/codex-actions/pod.artwork.import.staged/preview', {
        'arguments': {'upload_ids': [upload_id], 'category_id': category['id']}})[0]
    assert preview['effect']['category_name'] == 'Square designs'
    first = client.request('POST', '/codex-actions/pod.artwork.import.staged/execute', {
        'preview_token': preview['preview_token']})[0]
    repeated = client.request('POST', '/codex-actions/pod.artwork.import.staged/execute', {
        'preview_token': preview['preview_token']})[0]
    assert first['result']['accepted'] == 1
    assert first['result']['job_ids'] == repeated['result']['job_ids']
    job = client.get('/jobs/' + first['result']['job_ids'][0])
    assert job['type'] == 'pod_artwork_upload' and job['target_worker_id'] == worker_id


def test_generic_codex_composition_keeps_original_worker_and_deduplicates_submit(shared_server, monkeypatch):
    import hashlib
    from erp.models import Asset
    from erp.domains.pod.models import PodArtwork
    from erp.domains.pod import api as pod_api
    client = logged_client(shared_server)
    worker_id = 'pc-' + 'e' * 32
    client.request('POST', '/execution-devices/local/register', {
        'id': worker_id, 'name': 'Composition computer', 'composition_version': 1})
    with SessionLocal() as db:
        asset = Asset(name='design.png', local_path='', sha256=hashlib.sha256(b'image').hexdigest(),
                      size_bytes=5, mime_type='image/png', oss_url='https://assets.example.test/design.png',
                      oss_bucket='test', oss_key='design.png')
        db.add(asset); db.flush()
        artwork = PodArtwork(asset_id=asset.id, name='design', sha256=asset.sha256, mime_type='image/png')
        db.add(artwork); db.commit(); artwork_id = artwork.id
    monkeypatch.setattr(pod_api, '_prepare_composition_payload', lambda _db, base: {
        **base, 'variant_snapshot': {}, 'config_snapshot': {'connection_ids': {}}})
    arguments = {'supplier': 'hicustom', 'product_type_id': 1, 'product_name': 'Isolated product',
                 'default_color_id': 1, 'default_view_id': 1, 'selected_size_ids': [2],
                 'placements': [{'view_id': 1, 'width': 100, 'height': 100, 'top_x': 0, 'top_y': 0,
                                 'artwork_id': artwork_id}], 'artwork_ids': [artwork_id]}
    preview = client.request('POST', '/codex-actions/pod.composition.batch/preview', {'arguments': arguments})[0]
    assert preview['effect']['artwork_count'] == 1 and preview['effect']['prevent_duplicate']
    first = client.request('POST', '/codex-actions/pod.composition.batch/execute', {'preview_token': preview['preview_token']})[0]
    again = client.request('POST', '/codex-actions/pod.composition.batch/execute', {'preview_token': preview['preview_token']})[0]
    assert first['result']['job_ids'] == again['result']['job_ids']
    job = client.get('/jobs/' + first['result']['job_ids'][0])
    assert job['type'] == 'pod_native_composition' and job['target_worker_id'] == worker_id


def test_generic_codex_workflow_reads_are_fixed_and_paginated(shared_server):
    client = logged_client(shared_server)
    assert 'items' in desktop.workflow_data(client, 'stores')
    assert 'categories' in desktop.workflow_data(client, 'artworks', {'page': 1, 'page_size': 10})
    with pytest.raises(ValueError):
        desktop.workflow_data(client, 'stores', {'path': '../../settings'})
    with pytest.raises(ValueError):
        desktop.workflow_data(client, 'pod_product_type', {'product_key': '../../settings'})
    with pytest.raises(ValueError):
        desktop.workflow_data(client, 'ai_batch', {'batch_id': '../../settings'})


def test_installed_codex_handoff_uses_only_original_local_bridge(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls = []
    def fake_run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=0, stdout='{"job_id":"' + 'a' * 32 + '","saved":true}', stderr='')
    monkeypatch.setattr(subprocess, 'run', fake_run)
    output = tmp_path / 'copy.json'; output.write_text('{}', encoding='utf-8')
    result = desktop.codex_handoff('submit', 'a' * 32, 'b' * 32, str(output))
    assert result['saved'] is True
    assert calls[0][0][1].endswith('codex_bridge.py')
    assert calls[0][0][2:] == ['submit', '--job', 'a' * 32, '--store', 'b' * 32, '--file', str(output)]
    assert calls[0][1]['stdin'] == subprocess.DEVNULL
    with pytest.raises(ValueError): desktop.codex_handoff('delete', 'a' * 32)
    with pytest.raises(ValueError): desktop.codex_handoff('submit', 'a' * 32, 'b' * 32, str(tmp_path / 'absent.json'))


def test_ozon_codex_prepare_uses_one_original_operation_for_repeated_receipt(shared_server, monkeypatch):
    from erp.api.routes import publications
    client = logged_client(shared_server)
    worker_id = 'pc-' + 'f' * 32
    client.request('POST', '/execution-devices/local/register', {
        'id': worker_id, 'name': 'Listing computer', 'codex_generation_version': 3,
        'catalog_ozon_version': 3})
    calls = []
    monkeypatch.setattr(publications, 'preview_publications', lambda _request, _db: {
        'items': [{'sku': 'ISOLATED-1', 'store_id': 'a' * 32}], 'count': 1})
    monkeypatch.setattr(publications, 'create_publications', lambda _request, _db: (
        calls.append('create') or {'batch_id': 'b' * 32, 'publication_count': 1}))
    args = {'product_ids': ['c' * 32], 'store_ids': ['a' * 32],
            'generation_provider': 'codex', 'image_policy': 'reuse'}
    preview = client.request('POST', '/codex-actions/ozon.prepare.codex/preview', {'arguments': args})[0]
    first = client.request('POST', '/codex-actions/ozon.prepare.codex/execute', {'preview_token': preview['preview_token']})[0]
    second = client.request('POST', '/codex-actions/ozon.prepare.codex/execute', {'preview_token': preview['preview_token']})[0]
    assert calls == ['create'] and first == second
