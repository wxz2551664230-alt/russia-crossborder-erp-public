from erp.mcp_server import dispatch


def test_mcp_tools_are_read_only_and_reject_write_names():
    tools = dispatch({'id': 1, 'method': 'tools/list'})['result']['tools']
    assert len(tools) == 5
    assert all(tool['annotations']['readOnlyHint'] for tool in tools)
    result = dispatch({'id': 2, 'method': 'tools/call', 'params': {'name': 'execute_ads', 'arguments': {}}})
    assert result['result']['isError'] is True


def test_mcp_rejects_path_injection_without_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('must not access network')
    monkeypatch.setattr('erp.mcp_server.httpx.Client', forbidden)
    result = dispatch({'id': 3, 'method': 'tools/call', 'params': {'name': 'erp_get_product', 'arguments': {'id': '../../settings'}}})
    assert result['result']['isError'] is True
