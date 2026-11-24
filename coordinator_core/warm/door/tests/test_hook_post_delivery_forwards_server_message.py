"""A server error on a delivered hook call reaches the advisory whole, not clipped or replaced.

Pins the door_core unit path (parse the error `message`, format the detail, build the
heap-sized advisory) on every OS, and the Windows door end to end against a stub server.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.warm.tests.door_test_support import (
    ReplyingServer,
    WINDOWS_ONLY,
    door_under_default_name,
    make_stub_engine_root,
    pipe_name_for,
)
from coordinator_core.win_portability import no_console_creationflags

_DOOR_DIR = Path(__file__).resolve().parents[1]
_MESSAGE_CAP = 2000

_HARNESS = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "door_core.h"
int main(void) {
    static char json[1 << 16];
    size_t n = fread(json, 1, sizeof json, stdin);
    result_fields_t rf;
    int have_error = 0;
    long code = 0;
    buf_t msg;
    if (!buf_init(&msg, 16)) return 2;
    parse_response_envelope_msg(json, n, &rf, &have_error, &code, &msg);
    char detail[DOOR_SERVER_ERROR_DETAIL_CAP];
    door_format_server_error_detail(detail, sizeof detail, code, &msg);
    buf_t out;
    if (!buf_init(&out, 64)) return 2;
    build_hook_engine_failure_allow_envelope(&out, detail, "PreToolUse");
    printf("%d\n", have_error);
    fwrite(out.data, 1, out.len, stdout);
    return 0;
}
'''


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    compiler = next((shutil.which(c) for c in ("clang", "cc", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    work = tmp_path_factory.mktemp("forward-message")
    main_c = work / "harness_main.c"
    main_c.write_text(_HARNESS, encoding="utf-8")
    out = work / ("harness.exe" if os.name == "nt" else "harness")
    subprocess.run(
        [compiler, "-O1", "-std=c11", f"-I{_DOOR_DIR}", "-o", str(out), str(main_c), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


def _advisory(harness: Path, message: str, code: int = -32603) -> dict:
    envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": message}})
    proc = subprocess.run([str(harness)], input=envelope.encode(), capture_output=True, timeout=30,
                          **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    flag, _, body = proc.stdout.decode("utf-8").partition("\n")
    assert flag == "1"
    return json.loads(body)


def test_a_600_char_server_message_reaches_the_advisory_whole(harness):
    message = "targets path refused: " + "x" * 570 + " END"
    assert len(message) > 590
    body = _advisory(harness, message)
    for text in (body["systemMessage"], body["hookSpecificOutput"]["additionalContext"]):
        assert f"server error -32603: {message}" in text
        assert text.endswith("; guard not evaluated, command allowed.")
    assert "permissionDecision" not in json.dumps(body)


def test_the_message_is_bounded_at_the_ipc_caller_facing_cap_and_the_suffix_survives(harness):
    body = _advisory(harness, "m" * 5000)
    text = body["systemMessage"]
    assert "m" * _MESSAGE_CAP in text and "m" * (_MESSAGE_CAP + 1) not in text
    assert text.endswith("command allowed.")


def test_escaped_characters_in_the_message_decode_before_they_are_reported(harness):
    body = _advisory(harness, 'path "a\\b" not found\nline2')
    assert 'server error -32603: path "a\\b" not found\nline2' in body["systemMessage"]


@WINDOWS_ONLY
@pytest.mark.spawns_process
@pytest.mark.warm_tier
def test_the_windows_door_forwards_the_server_message_in_hook_mode(tmp_path):
    message = "targets path refused: " + "y" * 580
    root = make_stub_engine_root(tmp_path)
    reply = (json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "message": message}}) + "\n").encode()
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env["COORDINATOR_DOOR_STDIN_MODE"] = "hook"
    payload = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}).encode()
    server = ReplyingServer(pipe_name_for(root), reply)
    try:
        proc = subprocess.run(
            [str(door_under_default_name(root)), "hooks.posttool_dispatch"],
            input=payload, capture_output=True, env=env, timeout=30, **no_console_creationflags(),
        )
    finally:
        server.close()
    body = json.loads(proc.stdout.decode("utf-8"))
    assert f"server error -32603: {message}" in body["systemMessage"]
    assert "does not prove" not in body["systemMessage"]
