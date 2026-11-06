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
from coordinator_core.warm.tests.test_door_read_deadline_posix import runtime_base  # noqa: F401 -- fixture
from coordinator_core.win_portability import no_console_creationflags

pytestmark = pytest.mark.complete

_ENTRYPOINT = "hooks.submit_engine_ask"
_MODE_ENV = "COORDINATOR_DOOR_STDIN_MODE"
_DOOR_DIR = Path(__file__).resolve().parents[1]


def _live_read_ms() -> int:
    header = _DOOR_DIR / "door_core.h"
    match = re.search(r"^#define\s+DOOR_HOOK_LIVE_READ_MS\s+(\d+)\s*$", header.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, header.name
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
    # A wedged pipe is open and alive, so the door holds it to the live bound, not the
    # dead-peer DOOR_HOOK_READ_DEADLINE_MS.
    deadline_ms = _live_read_ms()
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
        assert f"{deadline_ms}ms" in text
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
    # One wedge per call: _WedgedServer accepts exactly one connection, so a shared one sends
    # the second call down the unreachable path instead of the wedged one.
    results = []
    for command in ("git -C . push origin main", 'git commit -m "git push later"'):
        wedge = _WedgedServer(pipe_name_for(root))
        try:
            results.append(_run_hook_door(root, _PRECHECK_ENTRYPOINT, command=command)[0])
        finally:
            wedge.close()
    pushed, mentioned = results

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


@pytest.mark.parametrize("source", [DOOR_WINDOWS_C, DOOR_POSIX_C], ids=lambda p: p.name)
def test_both_doors_log_every_fail_open_and_bound_the_cold_guard(source):
    text = source.read_text(encoding="utf-8")
    assert "door-fail-open.log" in text
    for fn in ("static int emit_post_delivery_failure(", "static int emit_hook_pass_loudly("):
        start = text.index(fn)
        assert "door_log_fail_open(" in text[start: text.index("\n}\n", start)], fn
    cold = text[text.index("static int hook_fall_through("):]
    cold = cold[: cold.index("\n}\n")]
    assert "door_hook_read_deadline_ms()" in cold
    assert cold.index("door_hook_read_deadline_ms()") < cold.index("cold guard returned no verdict in time")
    assert "if (advisory) return 0;" not in cold, "an advisory fail-open must be logged"


# --- POSIX door: warm answers under the bar; a stalled or cold engine fails open fast and is logged ---

_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="drives the POSIX door")
_BAR_MS = 500
_LIVE_MS = 8000
_ALLOW_REPLY = (
    '{"jsonrpc":"2.0","id":1,"result":{"stdout":"{\\"hookSpecificOutput\\":{\\"hookEventName\\":'
    '\\"PreToolUse\\",\\"permissionDecision\\":\\"allow\\"}}\\n","stderr":"","exit_code":0}}\n'
).encode()


@pytest.fixture(scope="module")
def posix_door(tmp_path_factory) -> Path:
    compiler = next((shutil.which(c) for c in ("cc", "clang", "gcc") if shutil.which(c)), None)
    if compiler is None or os.name == "nt":
        pytest.skip("no C compiler or not POSIX")
    out = tmp_path_factory.mktemp("posix-door") / "coordinator-invoke"
    subprocess.run(
        [compiler, "-O2", "-std=c11", '-DPYTHON_BIN="python3"', '-DBUILD_ENGINE_ROOT=""', "-o", str(out),
         str(_DOOR_DIR / "door_posix.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


def _posix_hook_run(door: Path, root: Path, runtime_base: Path, settings: Path, command: str = "echo hi"):
    settings.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update(
        COORDINATOR_DOOR_ENGINE_ROOT=str(root),
        COORDINATOR_WARM_RUNTIME_BASE=str(runtime_base),
        COORDINATOR_SETTINGS_HOME=str(settings),
        COORDINATOR_DOOR_STDIN_MODE="hook",
    )
    started = time.monotonic()
    proc = subprocess.run(
        [str(door), "hooks.preuse_bash_dispatch"], input=_payload(command), capture_output=True, env=env,
        cwd=str(root), timeout=30, **no_console_creationflags(),
    )
    return proc, (time.monotonic() - started) * 1000.0


def _fail_open_lines(settings: Path) -> list[str]:
    log = settings / "state" / "door-fail-open.log"
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _slow_cold_guard(root: Path) -> None:
    (root / "coordinator" / "bin" / "coordinator-invoke.py").write_text("import time\ntime.sleep(20)\n", encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_warm_engine_answers_a_hook_read_under_the_bar_and_logs_nothing(posix_door, tmp_path, runtime_base):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    sock = posix._socket_path_for(runtime_base, root)
    posix._make_socket_dir(sock)
    server = posix._ReplyingServer(sock, _ALLOW_REPLY)
    try:
        proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, tmp_path / "settings")
    finally:
        server.close()

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert elapsed_ms < _BAR_MS
    assert _fail_open_lines(tmp_path / "settings") == []


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_live_engine_that_never_answers_fails_open_only_at_the_live_bound_and_the_stall_is_logged(
    posix_door, tmp_path, runtime_base
):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    sock = posix._socket_path_for(runtime_base, root)
    posix._make_socket_dir(sock)
    server = posix._WedgedServer(sock)
    try:
        proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, tmp_path / "settings")
    finally:
        server.close()

    body = json.loads(proc.stdout.decode("utf-8"))
    assert "command allowed" in body["systemMessage"]
    assert f"{_LIVE_MS}ms" in body["systemMessage"]
    assert _LIVE_MS * 0.9 <= elapsed_ms < _LIVE_MS + 2000
    (line,) = _fail_open_lines(tmp_path / "settings")
    assert f"deadline_ms={_LIVE_MS}" in line and "op=hooks.preuse_bash_dispatch" in line
    assert f"no response within {_LIVE_MS}ms" in line and "elapsed_ms=" in line


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_cold_engine_whose_cold_guard_overruns_fails_open_at_the_bar_and_logs(posix_door, tmp_path, runtime_base):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    _slow_cold_guard(root)
    proc, elapsed_ms = _posix_hook_run(posix_door, root, runtime_base, tmp_path / "settings")

    body = json.loads(proc.stdout.decode("utf-8"))
    assert "guard did not run" in body["systemMessage"]
    assert elapsed_ms < _BAR_MS + 1500
    (line,) = _fail_open_lines(tmp_path / "settings")
    assert "cold guard" in line and f"deadline_ms={_BAR_MS}" in line


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@_POSIX_ONLY
def test_a_cold_guard_overrun_still_denies_an_irreversible_command(posix_door, tmp_path, runtime_base):
    from coordinator_core.warm.tests import test_door_read_deadline_posix as posix

    root = posix._make_stub_engine_root(tmp_path)
    _slow_cold_guard(root)
    proc, _ = _posix_hook_run(posix_door, root, runtime_base, tmp_path / "settings", command="git push origin main")

    deny = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert deny["permissionDecision"] == "deny"
    assert len(_fail_open_lines(tmp_path / "settings")) == 1
