import pytest
from erp.core.errors import AppError
from erp.domains.orders import api


@pytest.mark.parametrize('platform,secrets', [('wb', {}), ('wb', {'wildberries_token': ' '}),
    ('ozon', {'ozon_client_id': '123'}), ('ozon', {'ozon_api_key': 'private'})])
def test_missing_credentials_reject_before_queue(monkeypatch, platform, secrets):
    monkeypatch.setattr(api, 'store_config', lambda *args: {'platform': platform, 'store_name': 'XSZ', **secrets})
    def forbidden(*args, **kwargs):
        pytest.fail('Must not enqueue an unexecutable task')
    monkeypatch.setattr(api, 'enqueue_job', forbidden)
    for handler, body in [(api.sync, api.SyncOrders(store_id='store')),
                          (api.sync_images, api.SyncImages(store_id='store'))]:
        with pytest.raises(AppError) as caught:
            handler(body, None)
        assert caught.value.code == 'order_store_credentials_missing'
        assert 'XSZ' in str(caught.value)
        assert 'private' not in str(caught.value)


def test_valid_credentials():
    api.validate_sync_credentials({'platform': 'wb', 'store_name': 'X', 'wildberries_token': 'token'})
    api.validate_sync_credentials({'platform': 'ozon', 'store_name': 'X', 'ozon_client_id': '123', 'ozon_api_key': 'key'})
