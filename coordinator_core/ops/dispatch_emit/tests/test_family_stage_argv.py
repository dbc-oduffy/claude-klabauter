"""The family-stage invoke argv the runner agent executes carries the dispatching session id."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, stage_family


def test_the_invoke_argv_composer_emits_the_session_id_flag_from_the_script_constant():
    js = ask_compose._family_run_js("'w:'", "'Execute'", "start-cmd ", "invoke-cmd --row ")
    start = js[js.index("const _famStart = (r) => ") : js.index("async function _familyRun(r)")]
    assert stage_family.SESSION_ID_FLAG == "--session-id"
    assert "typeof _SESSION_ID === 'string' && _SESSION_ID ? ' --session-id ' + JSON.stringify(_SESSION_ID) : ''" in start
    assert start.index("--session-id") < start.index("--writes")
