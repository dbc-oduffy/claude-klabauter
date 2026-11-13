"""test_commit_hooks_share_one_resolution.py -- a real `git commit` in a tmp_path repo with the
hooks installed spawns ONE `git diff --cached` and at most one `git rev-parse --show-toplevel`
across the pre-commit guards and prepare-commit-msg.

The test process runs the native `git commit` as a subprocess; `ceremony.commit_v2` runs no hooks
by construction, so a count through it would prove nothing. A Python `git` shim, first on the
hooks' PATH, logs every argv a hook spawns and then execs the real git. Git prepends its own
exec-path to a hook's PATH and that directory holds a `git`, so the commit runs with
GIT_EXEC_PATH pointed at the shim directory to put the shim ahead of it.

Declared, not excused: this file spawns real `git` and real hook processes because the property
under test is the per-commit spawn count --
coordinator_core/tests/test_no_new_spawning_tests.py Rule 2/4.

Set COMMIT_HOOKS_ENGINE_ROOT to run the same body against another export of the engine tree (the
baseline count).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
    pytest.mark.skipif(
        sys.platform == "win32", reason="the sh pre-commit and shebang git shim are POSIX-only here"
    ),
]

_NO_WINDOW = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
_ENGINE_ROOT = Path(
    os.environ.get("COMMIT_HOOKS_ENGINE_ROOT") or Path(__file__).resolve().parents[3]
).resolve()
_REAL_GIT = shutil.which("git") or "git"

_SHIM = """\
import json, os, sys
with open(os.environ["COUNT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
env = dict(os.environ)
env.pop("GIT_EXEC_PATH", None)
real = os.environ["COUNT_REAL_GIT"]
os.execve(real, [real] + sys.argv[1:], env)
"""

_GLOBAL_OPT_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def _subcommand(argv: list[str]) -> tuple[str, list[str]]:
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _GLOBAL_OPT_WITH_VALUE else 1
    return (argv[i], argv[i + 1 :]) if i < len(argv) else ("", [])


def _tally(log: Path) -> dict[str, int]:
    diff_cached = toplevel = 0
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            sub, rest = _subcommand(json.loads(line))
            if sub == "diff" and ("--cached" in rest or "--staged" in rest):
                diff_cached += 1
            elif sub == "rev-parse" and "--show-toplevel" in rest:
                toplevel += 1
    return {"diff_cached": diff_cached, "toplevel": toplevel}


def _real_git(args: list[str], cwd: Path, env: dict | None = None):
    return subprocess.run(
        [_REAL_GIT, *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
        **_NO_WINDOW,
    )


def _py(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _pre_commit_body(ran_log: Path, restage_file: str | None) -> str:
    py = _py(sys.executable)
    guards = [
        f'"{py}" "{_py(_ENGINE_ROOT / "coordinator/bin/pre_commit_corpus_artifact_guard.py")}" || exit 1',
        f'"{py}" -m coordinator_core.hooks.guard_phantom_staged_deletion_precommit || exit 1',
        f'"{py}" -m coordinator_core.hooks.guard_doctrine_surface_ratio_precommit || exit 1',
    ]
    lines = ["#!/bin/sh", f'echo pre-commit >> "{_py(ran_log)}"', *guards]
    if restage_file:
        lines.append(f'git add -- "{restage_file}" || exit 1')
    return "\n".join(lines) + "\n"


def _install_hooks(repo: Path, ran_log: Path, restage_file: str | None) -> None:
    sys.path.insert(0, str(_ENGINE_ROOT / "coordinator" / "bin" / "lib"))
    try:
        import git_hook_install as ghi
    finally:
        sys.path.pop(0)
    bin_dir = str(_ENGINE_ROOT / "coordinator" / "bin")
    rc = ghi.ensure_prepare_commit_msg_hook(
        bin_dir, root=str(repo), registry={"repos.claude_klabauter": str(_ENGINE_ROOT)}
    )
    assert rc == 0
    hooks = repo / ".git" / "hooks"
    assert (hooks / "prepare-commit-msg").is_file()
    pre = hooks / "pre-commit"
    pre.write_text(_pre_commit_body(ran_log, restage_file), encoding="utf-8")
    pre.chmod(0o755)


def _commit_env(tmp_path: Path, shim_dir: Path, log: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CORPUS_ARTIFACT_OK"):
        env.pop(var, None)
    env.update(
        PATH=str(shim_dir) + os.pathsep + env.get("PATH", ""),
        GIT_EXEC_PATH=str(shim_dir),
        COUNT_LOG=str(log),
        COUNT_REAL_GIT=_REAL_GIT,
        PYTHONPATH=str(_ENGINE_ROOT),
        COORDINATOR_SESSION_ID=str(uuid.uuid4()),
        GIT_AUTHOR_NAME="T",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="T",
        GIT_COMMITTER_EMAIL="t@example.com",
    )
    return env


@pytest.fixture()
def rig(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(["init", "-q"], repo)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    (repo / "b.txt").write_text("b\n", encoding="utf-8")
    _real_git(["add", "a.txt", "b.txt"], repo)
    _real_git(
        ["-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "-q", "-m", "initial"],
        repo,
    )
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(f"#!{sys.executable}\n{_SHIM}", encoding="utf-8")
    shim.chmod(0o755)
    log = tmp_path / "git-calls.jsonl"
    ran_log = tmp_path / "hooks-ran.log"

    class Rig:
        pass

    r = Rig()
    r.repo, r.log, r.ran_log = repo, log, ran_log
    r.env = _commit_env(tmp_path, shim_dir, log)
    r.install = lambda restage_file=None: _install_hooks(repo, ran_log, restage_file)
    return r


def _commit(rig, args: list[str]):
    return subprocess.run(
        [_REAL_GIT, "commit", "-q", *args],
        cwd=str(rig.repo),
        env=rig.env,
        capture_output=True,
        text=True,
        **_NO_WINDOW,
    )


def _assert_hooks_really_ran(rig, proc) -> None:
    assert proc.returncode == 0, proc.stderr
    assert rig.ran_log.read_text(encoding="utf-8").strip() == "pre-commit"
    body = _real_git(["log", "-1", "--format=%B"], rig.repo).stdout
    assert "Session-Id:" in body, "prepare-commit-msg did not run"
    assert rig.log.exists() and rig.log.read_text(encoding="utf-8").strip(), "shim saw no hook git spawn"
    assert not (rig.repo / ".git" / "hooks" / "commit-msg").exists(), "a commit-msg hook is installed"


def test_default_commit_resolves_staged_set_once(rig):
    rig.install()
    (rig.repo / "a.txt").write_text("a2\n", encoding="utf-8")
    _real_git(["add", "a.txt"], rig.repo)
    proc = _commit(rig, ["-m", "change a"])
    _assert_hooks_really_ran(rig, proc)
    counts = _tally(rig.log)
    assert counts["diff_cached"] == 1, counts
    assert counts["toplevel"] <= 1, counts


def test_pathspec_commit_on_a_temporary_index_resolves_staged_set_once(rig):
    rig.install()
    (rig.repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (rig.repo / "b.txt").write_text("b2\n", encoding="utf-8")
    _real_git(["add", "b.txt"], rig.repo)
    proc = _commit(rig, ["-m", "change a only", "--", "a.txt"])
    _assert_hooks_really_ran(rig, proc)
    counts = _tally(rig.log)
    assert counts["diff_cached"] == 1, counts
    assert counts["toplevel"] <= 1, counts
    assert _real_git(["show", "--name-only", "--format=", "HEAD"], rig.repo).stdout.split() == ["a.txt"]


def test_restaging_in_pre_commit_forces_a_fresh_resolution(rig):
    rig.install(restage_file="b.txt")
    (rig.repo / "a.txt").write_text("a2\n", encoding="utf-8")
    (rig.repo / "b.txt").write_text("b2\n", encoding="utf-8")
    _real_git(["add", "a.txt"], rig.repo)
    proc = _commit(rig, ["-m", "restage b in the hook"])
    _assert_hooks_really_ran(rig, proc)
    counts = _tally(rig.log)
    assert counts["diff_cached"] == 2, counts
    assert counts["toplevel"] <= 1, counts
    cached = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in (rig.repo / ".git").glob("coordinator-commit-resolution-*.json")
    ]
    assert cached and any("b.txt" in entry.get("raw", "") for entry in cached), (
        "no cache entry carries the restaged file: prepare-commit-msg read a stale set"
    )
    assert _real_git(["show", "--name-only", "--format=", "HEAD"], rig.repo).stdout.split() == [
        "a.txt",
        "b.txt",
    ]
