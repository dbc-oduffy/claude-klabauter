"""The door's hook-mode repo-root recursive-search pre-check.

`door_core.c :: door_repo_root_search_precheck` is a C port of the
git-grep-mappable subset of `bash_guards/rewrite_repo_root_search.py`, so the
rewrite is produced without the warm engine. The Python leg stays the source
of truth; these tests pin the port to it:

  * the shared shape fixture (rows with `door_ports` true must rewrite, every
    other row must never produce a *different* rewrite, and advise/none rows
    must produce nothing -- the door ports no advisory);
  * a differential run of the same commands through the real Python leg;
  * the note text, byte-equal to `REWRITE_NOTE`;
  * the hook-mode deadline constants and, on Windows, the door itself: a
    starved or wedged engine still yields the rewrite, and only for
    `hooks.preuse_bash_dispatch`.
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

from coordinator_core.bash_guards import rewrite_repo_root_search as leg
from coordinator_core.warm.tests.door_test_support import (
    DOOR_POSIX_C,
    DOOR_WINDOWS_C,
    FALLBACK_MARKER,
    WINDOWS_ONLY,
    door_under_default_name,
    make_stub_engine_root,
    pipe_name_for,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.warm_tier]

_DOOR_DIR = Path(__file__).resolve().parents[1]
_FIXTURE = Path(__file__).resolve().parents[3] / "bash_guards" / "tests" / "fixtures" / "repo_root_search_shapes.json"
_ENTRYPOINT = "hooks.preuse_bash_dispatch"
_MODE_ENV = "COORDINATOR_DOOR_STDIN_MODE"
_ROWS = json.loads(_FIXTURE.read_text(encoding="utf-8"))["rows"]
_ROOT_TOKEN = "{ROOT}"

_EXTRA_SHAPES = [
    "grep -rn foo .",
    "grep -rniw foo ./",
    "grep -r 'a b' .",
    "grep -rn \"quoted pattern\" .",
    "grep -rn it\\'s .",
    "grep -rl --include=*.py foo",
    "grep -rl --include=*.py --include=*.md foo .",
    "grep -rIl foo --exclude-dir=.git --exclude-dir=node_modules .",
    "grep -rn -e foo -e bar",
    "grep -rn -e -dash",
    "grep -rnE 'a|b' .",
    "grep -nr foo .",
    "grep -n -r foo .",
    "grep --recursive foo .",
    "grep -rn foo . | head -5",
    "grep -rn foo . | wc -l && echo done",
    "echo start; grep -rn foo .",
    "rg foo",
    "rg -n -i foo .",
    "rg -nE 'a|b' .",
    "rg -F 'a|b'",
    "rg -w foo",
    "grep -rn foo . bar",
    "grep -rn --color=always foo .",
    "grep -rn foo coordinator_core",
    "grep -rn \"$X\" .",
    "grep -rn foo . 2>/dev/null",
    "grep -rn 'unterminated .",
    "grep foo file.txt",
    "grep -rn café .",
    "grep -rn 'café x' .",
    "grep -n foo file.txt | grep -r bar .",
    "egrep -rn foo .",
    "fgrep -rn foo .",
    "find . -name '*.py'",
    "rg '\\d+' .",
    "rg -g '*.py' foo",
    "git grep foo",
    "echo grep -r foo .",
    "ls -la",
    "",
]


def _compiler() -> str:
    for name in ("clang", "cc", "gcc"):
        found = shutil.which(name)
        if found:
            return found
    pytest.skip("no C compiler")


@pytest.fixture(scope="module")
def selftest(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("precheck-selftest") / ("selftest.exe" if os.name == "nt" else "selftest")
    subprocess.run(
        [_compiler(), "-O1", "-std=c11", "-o", str(out),
         str(_DOOR_DIR / "door_core_selftest.c"), str(_DOOR_DIR / "door_core.c")],
        check=True, capture_output=True, **no_console_creationflags(),
    )
    return out


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("repo-with-git")
    (root / ".git").mkdir()
    return root


@pytest.fixture(scope="module")
def nonrepo(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("repo-without-git")


def _precheck(selftest: Path, cwd: Path, cmd: str):
    proc = subprocess.run(
        [str(selftest), "--precheck", str(cwd)], input=cmd.encode("utf-8"),
        capture_output=True, timeout=30, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    head, _, rest = proc.stdout.decode("utf-8").replace("\r\n", "\n").partition("\n")
    if head == "0":
        return None
    assert head == "1", proc.stdout
    return json.loads(rest)["hookSpecificOutput"]


def _python_rewrite(cmd: str, cwd: Path):
    rec = leg.check({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})
    if rec is not None and rec.verdict_kind == "rewrite":
        return rec.updated_input["command"]
    return None


def test_the_selftest_passes_with_the_precheck_checks(selftest):
    proc = subprocess.run([str(selftest)], capture_output=True, timeout=30, **no_console_creationflags())
    assert proc.returncode == 0, proc.stdout
    assert proc.stdout.startswith(b"PASS")


@pytest.mark.parametrize("row", [r for r in _ROWS if _ROOT_TOKEN not in r["cmd"]], ids=lambda r: r["cmd"])
def test_fixture_rows(selftest, repo, nonrepo, row):
    cwd = repo if row["cwd_is_repo_root"] else nonrepo
    hso = _precheck(selftest, cwd, row["cmd"])
    if row["expected"] != "rewrite" or not row["cwd_is_repo_root"]:
        assert hso is None, "the door ports no advisory and never rewrites outside a repo root"
        return
    if row["door_ports"]:
        assert hso is not None, "a ported row must rewrite without the engine"
    if hso is not None:
        assert hso["updatedInput"]["command"] == row["rewritten_cmd"]
        assert hso["hookEventName"] == "PreToolUse"


def test_a_leading_cd_is_left_to_the_engine(selftest, repo):
    assert _precheck(selftest, repo, f"cd {repo} && grep -rn foo .") is None


@pytest.mark.parametrize("cmd", _EXTRA_SHAPES, ids=lambda c: c or "<empty>")
def test_differential_against_the_python_leg(selftest, repo, cmd):
    hso = _precheck(selftest, repo, cmd)
    expected = _python_rewrite(cmd, repo)
    if hso is not None:
        assert expected == hso["updatedInput"]["command"], "the door rewrote a shape the Python leg does not, or differently"
    elif expected is not None and cmd.isascii():
        pytest.fail(f"Python leg rewrites {cmd!r} to {expected!r} but the door yielded; subset shrank")


def test_the_incident_commands_are_in_the_subset(selftest, repo):
    for cmd, want in (
        ("grep -rIl metahuman_reference . --exclude-dir=.git", "git grep -Il metahuman_reference -- ':(exclude,glob)**/.git/**'"),
        ("grep -rl offset_start --include=*.py .", "git grep -l offset_start -- ':(glob)**/*.py'"),
    ):
        hso = _precheck(selftest, repo, cmd)
        assert hso is not None and hso["updatedInput"]["command"] == want


def test_the_note_is_byte_equal_to_the_python_rewrite_note_and_names_example_retrieval_repo(selftest, repo):
    hso = _precheck(selftest, repo, "grep -rn foo .")
    assert hso["additionalContext"] == leg.REWRITE_NOTE
    assert "project-rag" in hso["additionalContext"]


def test_no_cwd_or_a_cwd_without_git_yields(selftest, repo, nonrepo):
    assert _precheck(selftest, nonrepo, "grep -rn foo .") is None
    assert _precheck(selftest, repo / "missing-dir", "grep -rn foo .") is None


def _payload(command: str, cwd: Path | None, **extra_tool_input) -> bytes:
    body = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, **extra_tool_input},
    }
    if cwd is not None:
        body["cwd"] = str(cwd)
    return json.dumps(body).encode("utf-8")


def _payload_precheck(selftest: Path, payload: bytes):
    proc = subprocess.run(
        [str(selftest), "--precheck-payload"], input=payload,
        capture_output=True, timeout=30, **no_console_creationflags(),
    )
    assert proc.returncode == 0, proc.stderr
    head, _, rest = proc.stdout.decode("utf-8").replace("\r\n", "\n").partition("\n")
    return None if head == "0" else json.loads(rest)["hookSpecificOutput"]


def test_the_payload_form_keeps_every_other_tool_input_key(selftest, repo):
    payload = _payload(
        "grep -rn 'café x' .", repo,
        description="find it", timeout=5000, run_in_background=False,
    )
    hso = _payload_precheck(selftest, payload)
    assert hso is not None
    assert {k: v for k, v in hso["updatedInput"].items() if k != "command"} == {
        "description": "find it", "timeout": 5000, "run_in_background": False,
    }
    assert hso["updatedInput"]["command"] == "git grep -n 'café x'"


def test_the_payload_form_only_answers_bash_with_a_cwd(selftest, repo):
    body = json.loads(_payload("grep -rn foo .", repo))
    body["tool_name"] = "Read"
    assert _payload_precheck(selftest, json.dumps(body).encode()) is None
    assert _payload_precheck(selftest, _payload("grep -rn foo .", None)) is None
    assert _payload_precheck(selftest, b"not json") is None
    assert _payload_precheck(selftest, _payload("ls", repo)) is None


def _define(source: Path, macro: str) -> int:
    match = re.search(rf"^#define\s+{macro}\s+(\d+)\s*$", source.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{macro} missing from {source.name}"
    return int(match.group(1))


def test_the_hook_deadline_sits_under_the_caller_timeout_in_both_doors():
    windows = _define(DOOR_WINDOWS_C, "DOOR_HOOK_READ_DEADLINE_MS")
    assert windows == _define(DOOR_POSIX_C, "DOOR_HOOK_READ_DEADLINE_MS")
    assert windows < 12_000, "DoE hooks.json timeout is 15 s and the caller's child budget 12 s"
    assert _define(DOOR_WINDOWS_C, "DOOR_READ_DEADLINE_MS") == 40_000
    assert _define(DOOR_POSIX_C, "DOOR_READ_DEADLINE_MS") == 40_000


def test_both_doors_arm_the_precheck_for_the_one_entrypoint_only():
    for source, literal in ((DOOR_WINDOWS_C, f'L"{_ENTRYPOINT}"'), (DOOR_POSIX_C, f'"{_ENTRYPOINT}"')):
        text = source.read_text(encoding="utf-8")
        assert text.count(literal) == 1, source.name
        assert "g_door_hook_mode && argc >= 2" in text


def _hook_env(root: Path) -> dict:
    env = dict(os.environ)
    env["COORDINATOR_DOOR_ENGINE_ROOT"] = str(root)
    env[_MODE_ENV] = "hook"
    return env


def _run_door(root: Path, repo_dir: Path, command: str, entrypoint: str = _ENTRYPOINT, timeout: float = 60):
    started = time.monotonic()
    proc = subprocess.run(
        [str(door_under_default_name(root)), entrypoint],
        input=_payload(command, repo_dir, description="d"), capture_output=True, env=_hook_env(root),
        cwd=str(repo_dir), timeout=timeout, **no_console_creationflags(),
    )
    return proc, time.monotonic() - started


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@WINDOWS_ONLY
def test_a_starved_engine_still_yields_the_rewrite(tmp_path, repo):
    root = make_stub_engine_root(tmp_path)
    proc, _ = _run_door(root, repo, "grep -rIl metahuman_reference . --exclude-dir=.git")

    assert proc.returncode == 0
    assert FALLBACK_MARKER.encode() not in proc.stdout
    hso = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert hso["updatedInput"]["command"] == "git grep -Il metahuman_reference -- ':(exclude,glob)**/.git/**'"
    assert hso["updatedInput"]["description"] == "d"
    assert "project-rag" in hso["additionalContext"]


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@WINDOWS_ONLY
@pytest.mark.parametrize(
    ("command", "entrypoint"),
    [
        pytest.param("echo hi", _ENTRYPOINT, id="not-a-search"),
        pytest.param("grep -rn foo .", "hooks.some_other_hook", id="other-entrypoint"),
        pytest.param("find . -name '*.py'", _ENTRYPOINT, id="advisory-shape"),
    ],
)
def test_everything_else_still_takes_the_cold_leg(tmp_path, repo, command, entrypoint):
    root = make_stub_engine_root(tmp_path)
    proc, _ = _run_door(root, repo, command, entrypoint)

    assert proc.returncode == 0
    body = json.loads(proc.stdout.decode("utf-8"))
    assert "updatedInput" not in body.get("hookSpecificOutput", {})
    assert "cold guard" in body["systemMessage"]


@pytest.mark.spawns_process
@pytest.mark.warm_tier
@pytest.mark.cadence
@WINDOWS_ONLY
def test_a_wedged_engine_is_bounded_by_the_hook_deadline_and_still_yields_the_rewrite(tmp_path, repo):
    from coordinator_core.warm.tests.test_door_read_deadline import _WedgedServer

    root = make_stub_engine_root(tmp_path)
    deadline_s = _define(DOOR_WINDOWS_C, "DOOR_HOOK_READ_DEADLINE_MS") / 1000.0
    wedge = _WedgedServer(pipe_name_for(root))
    try:
        proc, elapsed = _run_door(root, repo, "grep -rn foo .")
        other, other_elapsed = _run_door(root, repo, "echo hi")
    finally:
        wedge.close()

    assert deadline_s <= elapsed < 15
    assert proc.returncode == 0
    hso = json.loads(proc.stdout.decode("utf-8"))["hookSpecificOutput"]
    assert hso["updatedInput"]["command"] == "git grep -n foo"

    assert other_elapsed < 15
    body = json.loads(other.stdout.decode("utf-8"))
    assert "updatedInput" not in body.get("hookSpecificOutput", {})
    assert "did not run" in json.dumps(body)
