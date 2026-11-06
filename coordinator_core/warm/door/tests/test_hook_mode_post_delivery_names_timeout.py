"""The door's hook mode answers a post-delivery failure with a one-line hook envelope.

A non-irreversible hook payload gets a systemMessage + additionalContext allow
advisory naming the failure detail, never the bare JSON-RPC indeterminate body
(the hook dispatcher renders that as "said nothing"); an irreversible payload
is denied; the non-hook path keeps the JSON-RPC body.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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
    assert "if (g_door_hook_mode) return emit_hook_post_delivery(detail);" in failure
    assert failure.index("g_precheck_armed") < failure.index("emit_hook_post_delivery") < failure.index("emit_indeterminate")
    assert "door_engine_failure_allow_envelope(&out, detail, event_name)" in text
    assert "!g_precheck_armed && !g_door_hook_mode" in text, "the server's indeterminate body must not pass through in hook mode"


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_the_engine_failure_builder_is_a_pass_not_a_deny_and_one_line(source):
    text = source.read_text(encoding="utf-8")
    start = text.index("static int door_engine_failure_allow_envelope(")
    body = re.sub(r'"\s*\n\s*"', "", text[start: text.index("\n}\n", start)])
    assert "permissionDecision" not in body
    assert "guard not evaluated, command allowed." in body


_MATCHER_MAIN = r'''
#include <stdio.h>
#include <stdlib.h>
#include "door_core.h"
int main(int argc, char **argv) {
    static char buf[1 << 16];
    size_t n = fread(buf, 1, sizeof buf, stdin);
    if (argc > 1) {
        char reason[640];
        door_irreversible_deny_reason(reason, sizeof reason, argv[1]);
        puts(reason);
        return 0;
    }
    printf("%d\n", door_payload_is_irreversible(buf, n));
    return 0;
}
'''


@pytest.fixture(scope="module")
def matcher(tmp_path_factory) -> Path:
    compiler = next((shutil.which(c) for c in ("clang", "cc", "gcc") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C compiler")
    work = tmp_path_factory.mktemp("irreversible-matcher")
    main_c = work / "matcher_main.c"
    main_c.write_text(_MATCHER_MAIN, encoding="utf-8")
    out = work / ("matcher.exe" if os.name == "nt" else "matcher")
    subprocess.run(
        [compiler, "-O1", "-std=c11", f"-I{_DOOR_DIR}", "-o", str(out), str(main_c), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


def _payload(command: str, tool: str = "Bash", event: str = "PreToolUse") -> bytes:
    return json.dumps({"hook_event_name": event, "tool_name": tool, "tool_input": {"command": command}}).encode()


def _matches(matcher: Path, payload: bytes) -> bool:
    proc = subprocess.run([str(matcher)], input=payload, capture_output=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip() == b"1"


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin main",
        "git -C E:/repo push",
        "git -c user.name=x push --force",
        "FOO=1 BAR=2 git push",
        "echo ok && git push",
        "make; git push origin x | cat",
        "false || git push",
        "p4 submit -d x",
        "p4 -c ws submit",
        "P4 shelve -c 5",
        "gh pr merge 12 --squash",
        "gh release create v1",
        "git branch -D old",
        "git branch --delete --force old",
        "git stash drop",
        "git stash clear",
    ],
)
def test_an_irreversible_external_command_is_matched(matcher, command):
    assert _matches(matcher, _payload(command))


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "git push later"',
        "git commit -m 'run git push; then p4 submit'",
        "echo git push",
        "git status",
        "git branch -d merged",
        "git stash list",
        "git log --oneline push",
        "gh pr view 12",
        "p4 sync",
        "git pushd",
        "",
    ],
)
def test_a_command_that_only_mentions_an_irreversible_verb_is_not_matched(matcher, command):
    assert not _matches(matcher, _payload(command))


def test_only_a_pretooluse_bash_payload_is_matched(matcher):
    assert not _matches(matcher, _payload("git push", tool="Read"))
    assert not _matches(matcher, _payload("git push", event="PostToolUse"))
    assert not _matches(matcher, b"not json")
    assert not _matches(matcher, json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}).encode())


def test_the_deny_reason_carries_the_detail_and_the_retry_instruction(matcher):
    proc = subprocess.run([str(matcher), "read deadline 10000 ms"], input=b"", capture_output=True, timeout=30,
                          **no_console_creationflags())
    reason = proc.stdout.decode("utf-8").strip()
    assert reason == (
        "coordinator: the engine gave no usable answer after delivery (read deadline 10000 ms); an "
        "irreversible-external command is not run unguarded. Retry once warm answers."
    )


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_deny_an_irreversible_command_ahead_of_both_post_delivery_branches(source):
    text = source.read_text(encoding="utf-8")
    failure = text[text.index("static int emit_post_delivery_failure("):]
    failure = failure[: failure.index("\n}\n")]
    assert "door_payload_is_irreversible(g_hook_payload, g_hook_payload_len)" in failure
    assert failure.index("door_payload_is_irreversible") < failure.index("g_precheck_armed")
    assert failure.index("emit_hook_deny(reason)") < failure.index("emit_hook_post_delivery")


def _run_hook_door(root: Path, entrypoint: str, timeout: float = 30, command: str | None = None):
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env[_MODE_ENV] = "hook"
    payload = (
        _payload(command) if command is not None
        else json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {}}).encode()
    )
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
def test_a_wedged_engine_yields_a_one_line_allow_naming_the_deadline(tmp_path):
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
    assert proc.stdout.decode("utf-8").count("\n") == 1
    for text in (body["systemMessage"], body["hookSpecificOutput"]["additionalContext"]):
        assert f"{deadline_ms // 1000}s" in text
        assert "command allowed" in text
    assert body["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


_PRECHECK_ENTRYPOINT = "hooks.preuse_bash_dispatch"


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.cadence
@WINDOWS_ONLY
def test_a_wedged_engine_denies_git_push_and_passes_a_commit_message_that_mentions_it(tmp_path):
    from coordinator_core.warm.tests.test_door_read_deadline import _WedgedServer

    root = make_stub_engine_root(tmp_path)
    wedge = _WedgedServer(pipe_name_for(root))
    try:
        pushed, _ = _run_hook_door(root, _PRECHECK_ENTRYPOINT, command="git -C . push origin main")
        mentioned, _ = _run_hook_door(root, _PRECHECK_ENTRYPOINT, command='git commit -m "git push later"')
    finally:
        wedge.close()

    deny = json.loads(pushed.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"
    assert "irreversible-external command is not run unguarded" in deny["permissionDecisionReason"]
    assert "Retry once warm answers." in deny["permissionDecisionReason"]

    body = json.loads(mentioned.stdout.decode("utf-8"))
    assert "permissionDecision" not in json.dumps(body)
    assert "command allowed" in json.dumps(body)


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_bound_the_advisory_detail_so_the_suffix_survives(source):
    text = source.read_text(encoding="utf-8")
    start = text.index("static int emit_hook_post_delivery(")
    body = text[start: text.index("\n}\n", start)]
    assert 'snprintf(detail, sizeof detail, "%.200s"' in body
