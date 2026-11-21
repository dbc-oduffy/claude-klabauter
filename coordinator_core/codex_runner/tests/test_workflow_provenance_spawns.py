"""Accounting uses actual exec event fields, keeping broker lineage separate."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture(autouse=True)
def scratch_usage_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COORDINATOR_SETTINGS_HOME', str(tmp_path / 'settings'))
    monkeypatch.setenv('COORDINATOR_LOCK_ROOT', str(tmp_path / 'locks'))
    (tmp_path / 'codex-home').mkdir()
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))


def complete_receipt():
    return {
        'native_thread_id': '01a1b826-0012-72a0-a4d0-eae0ae828f4c',
        'broker_root_session_id': '01a1b63b-acfb-78d1-86f2-a74ad6ca4572',
        'broker_fire_id': 'fire-a', 'broker_request_id': 1, 'backend': 'codex',
        'model': 'unknown', 'effort': 'unknown', 'native_turn_id': 'unknown',
        'native_total_usage': 'unknown',
        'usage_events': [{'input_tokens': 100, 'cached_input_tokens': 60,
                          'cache_write_input_tokens': 0, 'output_tokens': 12,
                          'reasoning_output_tokens': 5}],
    }


def test_kernel_lock_deduplicates_across_real_processes(tmp_path: Path) -> None:
    script = ('import json; from coordinator_core.codex_runner.workflow import _append_canonical_usage; '
              f'print(json.dumps(_append_canonical_usage({complete_receipt()!r})))')
    processes = [subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, env=dict(os.environ)) for _ in range(3)]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert sorted(row['status'] for row in results) == ['appended', 'duplicate', 'duplicate']


_SCHEMA = {'type': 'object', 'properties': {'a': {'type': 'integer'}}, 'required': ['a']}


# Recorded agy 2026-11 stream-json (init/step_update/result) carries no model field; the
# model-bearing shapes below are synthesised for the CLI reporting one.
_AGY_RESULT = {'event': 'result', 'result': {'status': 'SUCCESS', 'response': '{"a": 2}'}}


# Shapes recorded from real CLI runs 2026-11-10: grok --output-format json (indented envelope whose
# only model evidence is the modelUsage key) and agy stream-json (no model; the agy --log-file does).
_GROK_REAL = {'text': '{"a": 1}', 'stopReason': 'end_turn', 'sessionId': 's', 'usage': {'input_tokens': 5},
              'modelUsage': {'grok-4.6-build': {'inputTokens': 5, 'modelCalls': 1}}}
_AGY_REAL = [
    {'event': 'init', 'conversation_id': 'c', 'init': {'cwd': '/x', 'tools': ['run_command'], 'permission_mode': 'request-review'}},
    {'event': 'step_update', 'step_update': {'conversation_id': 'c', 'step_index': 2, 'state': 'DONE', 'step_type': 'agent_response'}},
    _AGY_RESULT]
_AGY_LOG = ('I1110 11:51:22.688106 1 printmode.go:208] Print mode: starting (promptLength=0, model="", conversationID="")\n'
            'I1110 11:51:22.691619 1 model_config_manager.go:375] Propagating selected model override to backend: label="Gemini 4 Argon (Low)"\n')
