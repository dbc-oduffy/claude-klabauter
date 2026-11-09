"""Role/lifecycle behavior, using the actual doctrine adapter and hook envelopes."""
from pathlib import Path

import pytest

from coordinator_core.host_hooks import session_context
from coordinator_core.session.agent_role import classify

SID = "00000000-0000-4000-8000-000000000001"
BASE = {"hook_event_name":"SessionStart", "session_id":SID, "source":"startup"}
@pytest.fixture
def plugin_root():
    from coordinator_core.content_root import read_content_root
    import os
    root = Path(os.environ.get("COORDINATOR_TEST_DOCTRINE_ROOT") or read_content_root())
    if not (root / 'hooks/scripts/codex-session-context.py').is_file():
        pytest.skip('installed doctrine lacks Codex context adapter')
    return root


@pytest.mark.parametrize('role', ['worker','workflow-agent','reviewer','unknown'])
def test_explicit_non_em_roles_do_not_inherit_human_em_context(tmp_path, role, plugin_root):
    payload = {**BASE, 'cwd':str(tmp_path)}
    result = session_context.context(payload, {'COORDINATOR_AGENT_ROLE':role}, plugin_root=plugin_root,
                                     observed={'native_main':True,'exec_worker':False})
    text = result['hookSpecificOutput']['additionalContext']
    assert 'You are the human-facing EM' not in text
    assert 'Coordinator audience: '+role in text


def test_verified_native_main_receives_codex_engine_authority(tmp_path, plugin_root):
    result = session_context.context({**BASE, 'cwd':str(tmp_path)}, {}, plugin_root=plugin_root,
                                     observed={'native_main':True,'exec_worker':False})
    text = result['hookSpecificOutput']['additionalContext']
    assert 'You are the human-facing EM' in text
    assert 'coordinator-invoke engine_ask.submit' in text
    assert 'Workflow({' not in text


def test_subagent_parent_id_never_becomes_em(tmp_path, plugin_root):
    payload = {**BASE, 'hook_event_name':'SubagentStart', 'agent_id':'child', 'cwd':str(tmp_path)}
    result = session_context.context(payload, {'COORDINATOR_AGENT_ROLE':'em'}, plugin_root=plugin_root,
                                     observed={'native_main':True,'exec_worker':False})
    assert result['hookSpecificOutput']['hookEventName'] == 'SubagentStart'
    assert 'You are the human-facing EM' not in result['hookSpecificOutput']['additionalContext']
    assert payload['session_id'] == SID


