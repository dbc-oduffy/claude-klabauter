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
    # Probe through the loader itself, so the skip and the loader agree on every
    # supported layout (a hand-rolled published-layout probe once hid the nested miss).
    try:
        session_context.load_adapter(root)
    except ValueError:
        pytest.skip('doctrine root lacks Codex context adapter')
    return root


@pytest.mark.parametrize('layout', ['hooks/scripts', 'coordinator/hooks/scripts'])
def test_adapter_loads_from_published_and_authoring_layouts(tmp_path, layout):
    adapter = tmp_path / layout / 'codex-session-context.py'
    adapter.parent.mkdir(parents=True)
    adapter.write_text('MARK = 1', encoding='utf-8')
    assert session_context.load_adapter(tmp_path).MARK == 1


def test_missing_adapter_names_both_probed_paths(tmp_path):
    with pytest.raises(ValueError, match='coordinator'):
        session_context.load_adapter(tmp_path)


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


