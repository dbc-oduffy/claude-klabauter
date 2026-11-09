"""MCP serving proof, not source-folder guessing, controls the UE solution label."""
import json

from coordinator_core.host_hooks.ue_route import REQUIRED_TOOLS, probe_example_retrieval_repo


def test_configured_connected_tools_and_serving_addon_are_required(tmp_path):
    config=tmp_path/'config.toml';config.write_text('[mcp_servers.project-rag]\nurl="http://localhost/mcp"\n')
    calls=[]
    class Response:
        headers={}
        def __init__(self, value):self.value=value
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,*args):return json.dumps(self.value).encode()
    def open(request,timeout):
        msg=json.loads(request.data);calls.append(msg['method'])
        if msg['method']=='tools/list':return Response({'result':{'tools':[{'name':n} for n in REQUIRED_TOOLS]}})
        if msg['method']=='tools/call':return Response({'result':{'structuredContent':{'engine_queryable':True,'source_root_path':'serve/addons/example-retrieval-repo-ue-addon/pinned/data'}}})
        return Response({'result':{}})
    assert probe_example_retrieval_repo(config_path=config,opener=open)['ready']
    assert calls==['initialize','notifications/initialized','tools/list','tools/call']


def test_installed_or_configured_without_serving_route_is_unverified(tmp_path):
    config=tmp_path/'config.toml';config.write_text('[mcp_servers.project-rag]\nurl="http://localhost/mcp"\n')
    def offline(*args,**kwargs):raise TimeoutError('offline')
    assert probe_example_retrieval_repo(config_path=config,opener=offline)['ready'] is False
    assert probe_example_retrieval_repo(config_path=tmp_path/'missing')['ready'] is False


def test_exhausted_probe_budget_performs_no_network(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[mcp_servers.project-rag]\nurl="http://localhost/mcp"\n')
    ticks = iter([0, 1])
    def forbidden(*args, **kwargs):
        raise AssertionError('network after budget exhausted')
    result = probe_example_retrieval_repo(config_path=config, budget=0.2, clock=lambda:next(ticks), opener=forbidden)
    assert result['ready'] is False and result['reason'] == 'budget_exhausted'
    assert result['stage'] == 'initialize' and result['error_type'] == 'TimeoutError'


def test_configured_tools_without_ue_capabilities_never_claim_solution(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[mcp_servers.project-rag]\nurl="http://localhost/mcp"\n')
    calls = []
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return b'{"result":{"tools":[]}}'
    def open(request, timeout):
        calls.append(json.loads(request.data)['method'])
        return Response()
    assert probe_example_retrieval_repo(config_path=config, opener=open)['ready'] is False
    assert 'tools/call' not in calls


def test_safe_stage_and_tool_evidence_never_reflect_credentials_or_claim_retrieval(tmp_path):
    config = tmp_path / 'config.toml'
    config.write_text('[mcp_servers.project-rag]\nurl="https://alice:route-secret@example.invalid/mcp?token=query-secret"\nhttp_headers={Authorization="Bearer header-secret"}\n')
    class Response:
        headers = {}
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return json.dumps(self.value).encode()
    def missing_tools(request, timeout):
        message = json.loads(request.data)
        return Response({'result': {'tools': [{'name': 'engine_domain_status'}]}} if message['method'] == 'tools/list' else {'result': {}})
    missing = probe_example_retrieval_repo(config_path=config, opener=missing_tools)
    assert missing['reason'] == 'required_tools_missing' and missing['stage'] == 'tools/list'
    assert missing['route'] == {'server': 'example-retrieval-repo', 'configured': True, 'transport': 'http'}
    assert missing['tools'] == ['engine_domain_status']
    assert missing['missing_tools'] == sorted(REQUIRED_TOOLS - {'engine_domain_status'})
    def timeout(*args, **kwargs):
        raise TimeoutError('https://alice:route-secret@example.invalid/?token=query-secret Bearer header-secret')
    failed = probe_example_retrieval_repo(config_path=config, opener=timeout)
    assert failed['reason'] == 'transport_timeout' and failed['stage'] == 'initialize'
    rejected = probe_example_retrieval_repo(config_path=config, opener=lambda *a, **k: Response({
        'error': {'code': -32600, 'message': 'Bearer header-secret https://alice:route-secret@example.invalid/'}}))
    assert rejected['reason'] == 'mcp_request_rejected' and rejected['stage'] == 'initialize'
    def serving(request, timeout):
        message = json.loads(request.data)
        if message['method'] == 'tools/list':
            return Response({'result': {'tools': [{'name': name} for name in REQUIRED_TOOLS]}})
        if message['method'] == 'tools/call':
            return Response({'result': {'structuredContent': {'engine_queryable': True,
                'source_root_path': 'https://alice:route-secret@example.invalid/example-retrieval-repo-ue-addon?token=query-secret'}}})
        return Response({'result': {}})
    hint = probe_example_retrieval_repo(config_path=config, opener=serving)
    assert hint['ready'] is True and hint['source_root'] == '(redacted source locator)'
    for result in (missing, failed, rejected, hint):
        assert result['evidence_scope'] == 'readiness_hint_only'
        assert result['ue_api_retrieval_verified'] is False and result['lsp_verified'] is False
        encoded = json.dumps(result)
        assert all(secret not in encoded for secret in ('alice', 'route-secret', 'query-secret', 'header-secret', 'Authorization'))
