"""The Grok bridge leg resumes a bound ACP session without holding the router."""
import json
import sys
from pathlib import Path

import pytest

from coordinator_core.agent_pipe import bridge, roster


@pytest.mark.spawns_process
def test_grok_notice_routes_through_real_acp_transport(tmp_path, monkeypatch):
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path))
    wire = tmp_path / 'wire.jsonl'
    monkeypatch.setenv('FAKE_ACP_LOG', str(wire))
    monkeypatch.delenv('FAKE_ACP_SILENT', raising=False)
    monkeypatch.delenv('FAKE_ACP_NO_RESUME', raising=False)
    fake = Path(__file__).parent / 'fixtures' / 'fake_acp_agent.py'
    roster.bind('grok-worker', 'grok', 'sess-9', argv=[sys.executable, str(fake)])
    notice = bridge.make_message('workflow.fire', 'grok-worker', 'driver finished')
    result = bridge.route(notice)
    assert result.status == 'handed-off'
    result.wait(5)
    assert not result.handle.is_alive()
    assert not bridge.log_path().exists()
    rows = [json.loads(line) for line in wire.read_text(encoding='utf-8').splitlines()]
    resumed = next(row for row in rows if row.get('method') == 'session/resume')
    prompted = next(row for row in rows if row.get('method') == 'session/prompt')
    assert resumed['params']['sessionId'] == 'sess-9'
    assert prompted['params']['sessionId'] == 'sess-9'
    assert 'driver finished' in str(prompted['params']['prompt'])
