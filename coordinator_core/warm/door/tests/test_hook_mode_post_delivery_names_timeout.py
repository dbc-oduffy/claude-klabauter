"""The door's hook mode answers a post-delivery failure with a hook envelope.

A hook op that is not the pre-checked entrypoint used to get the bare JSON-RPC
indeterminate body, which the hook dispatcher renders as "said nothing". Hook
mode now emits a systemMessage + additionalContext envelope naming the op and
the hook read deadline; the non-hook path keeps the JSON-RPC body.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    DOOR_POSIX_C,
    DOOR_WINDOWS_C,
    WINDOWS_ONLY,
    door_under_default_name,
    make_stub_engine_root,
    pipe_name_for,
)
from coordinator_core.win_portability import no_console_creationflags

_ENTRYPOINT = "hooks.submit_engine_ask"
_MODE_ENV = "COORDINATOR_DOOR_STDIN_MODE"
_DOOR_DIR = Path(__file__).resolve().parents[1]


def _deadline_ms(source: Path) -> int:
    match = re.search(r"^#define\s+DOOR_HOOK_READ_DEADLINE_MS\s+(\d+)\s*$", source.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, source.name
    return int(match.group(1))


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_answer_hook_mode_post_delivery_with_the_hook_envelope(source):
    text = source.read_text(encoding="utf-8")
    failure = text[text.index("static int emit_post_delivery_failure("):]
    failure = failure[: failure.index("\n}\n")]
    assert "if (g_door_hook_mode) return emit_hook_post_delivery();" in failure
    assert failure.index("g_precheck_armed") < failure.index("emit_hook_post_delivery") < failure.index("emit_indeterminate")
    assert "build_hook_post_delivery_envelope(&out, g_hook_op, DOOR_HOOK_READ_DEADLINE_MS" in text
    assert "!g_precheck_armed && !g_door_hook_mode" in text, "the server's indeterminate body must not pass through in hook mode"


def test_the_envelope_builder_is_a_pass_not_a_deny_and_does_not_claim_the_guard_did_not_run():
    core = (_DOOR_DIR / "door_core.c").read_text(encoding="utf-8")
    start = core.index("int build_hook_post_delivery_envelope(")
    body = re.sub(r'"\s*\n\s*"', "", core[start: core.index("\n}\n", start)])
    assert "permissionDecision" not in body
    assert "did not run" not in body
    assert "may have completed" in body
    assert "Check for an existing run record" in body


def _run_hook_door(root: Path, entrypoint: str, timeout: float = 30):
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env[_MODE_ENV] = "hook"
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}).encode()
    started = time.monotonic()
    proc = subprocess.run(
        [str(door_under_default_name(root)), entrypoint],
        input=payload, capture_output=True, env=env, timeout=timeout, **no_console_creationflags(),
    )
    return proc, time.monotonic() - started


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.cadence
@WINDOWS_ONLY
def test_a_wedged_engine_yields_a_hook_envelope_naming_the_op_and_the_deadline(tmp_path):
    from coordinator_core.warm.tests.test_door_read_deadline import _WedgedServer

    root = make_stub_engine_root(tmp_path)
    deadline_ms = _deadline_ms(DOOR_WINDOWS_C)
    wedge = _WedgedServer(pipe_name_for(root))
    try:
        proc, elapsed = _run_hook_door(root, _ENTRYPOINT)
    finally:
        wedge.close()

    assert deadline_ms / 1000.0 <= elapsed < 15
    body = json.loads(proc.stdout.decode("utf-8"))
    assert "jsonrpc" not in body and "error" not in body
    assert "permissionDecision" not in json.dumps(body)
    for text in (body["systemMessage"], body["hookSpecificOutput"]["additionalContext"]):
        assert _ENTRYPOINT in text
        assert f"{deadline_ms} ms" in text
        assert "may have completed" in text
    assert body["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
