"""Whole-process `.git` denial inventory for `sizing-assemble --tshirt XS --write`.

A codex workspace-write sandbox carries a DENY ACE on `.git`. `test_write_outside_git.py` simulates it
for in-process `os.*` writes only, with `write_back` stubbed. This harness widens the simulation to the
whole process and runs the real default call: every child is recorded (argv, cwd, stdin), a child with an
inherited stdin or a lock-taking git subcommand is refused, the warm-engine pipe connect is refused, and a
hang prints its stack. It is an inventory of the sites that would block or write under a real DENY ACE,
not a reproduction of the hang: each finding names `file::function` and the mechanism. No ACL is touched.
"""
from __future__ import annotations

import errno
import faulthandler
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REPO = Path(__file__).resolve().parents[2]
_SIZING_ASSEMBLE = _REPO / "coordinator" / "bin" / "sizing-assemble.py"
_HOST_GUARD = _REPO / "coordinator" / "bin" / "unimatrix-host-guard.py"
_SID = "e1e1e1e1-0000-4000-8000-090555000000"
_REL = "state/sizings/2026-11-10-denied-probe.yaml"
_WRITE_FLAGS = os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC
_HANG_DUMP_SECS = 5
_PROCESS_BUDGET_SECS = 0.5

#: git subcommands that never take `.git/index.lock`; anything else needs GIT_OPTIONAL_LOCKS=0.
_LOCKFREE_GIT = frozenset(
    {
        "rev-parse", "rev-list", "cat-file", "show-ref", "symbolic-ref", "for-each-ref", "merge-base",
        "ls-tree", "log", "show", "version", "--version", "var", "check-ignore", "hash-object",
        "name-rev", "describe", "ls-remote",
    }
)
#: The legacy-sidecar probe (`_acquire_legacy_sidecar`) catches the denial and proceeds without it; the
#: attempt is still counted in `denied_writes` and `_tree(git)` still pins that nothing lands.
_TOLERATED_DENIAL = ("coordinator_core/locked_write.py::_mkdir", "coordinator-locks")
_STDIN_OK = (subprocess.DEVNULL, subprocess.PIPE)


@dataclass
class Inventory:
    """Everything the denied call tried that a real DENY ACE would block or break."""

    spawns: list[dict] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    denied_writes: list[str] = field(default_factory=list)
    pipe_connects: list[str] = field(default_factory=list)

    def finding(self, site: str, mechanism: str) -> None:
        line = f"{site}: {mechanism}"
        if line not in self.findings:
            self.findings.append(line)


def _site() -> str:
    """`relpath::function` of the innermost frame in this repo's code that is not the harness itself."""
    for fr in reversed(traceback.extract_stack()[:-1]):
        try:
            p = Path(fr.filename).resolve()
        except OSError:
            continue
        if p == Path(__file__).resolve():
            continue
        try:
            rel = p.relative_to(_REPO).as_posix()
        except ValueError:
            continue
        return f"{rel}::{fr.name}"
    return "<outside-repo>"


def _git_subcommand(argv: list[str]) -> str | None:
    """The git subcommand of *argv*, skipping `-C <dir>`, `-c k=v` and other leading options."""
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif tok.startswith("-") and tok not in ("--version", "--help"):
            i += 1
        else:
            return tok
    return None


def _is_git(argv: list[str]) -> bool:
    return bool(argv) and Path(os.fsdecode(argv[0])).stem.lower() == "git"


def _install_whole_process_denial(
    monkeypatch: pytest.MonkeyPatch, git: Path, *, run_non_git_children: bool = False
) -> Inventory:
    """Deny `.git` writes, record/refuse children, refuse the warm pipe connect."""
    inv = Inventory()
    git_s = os.path.normcase(os.path.realpath(str(git)))

    def under_git(path: object) -> bool:
        try:
            p = os.path.normcase(os.path.realpath(os.fsdecode(path)))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return p == git_s or p.startswith(git_s + os.sep)

    def refuse(path: object, what: str) -> PermissionError:
        shown = os.fsdecode(path)  # type: ignore[arg-type]
        inv.denied_writes.append(shown)
        site = _site()
        if (site, Path(shown).name) != _TOLERATED_DENIAL:
            inv.finding(site, f"{what} under .git: {shown}")
        return PermissionError(errno.EACCES, "Permission denied", shown)

    real_open, real_mkdir, real_replace = os.open, os.mkdir, os.replace

    def fake_open(path, flags, *args, **kwargs):
        if flags & _WRITE_FLAGS and under_git(path):
            raise refuse(path, "open-for-write")
        return real_open(path, flags, *args, **kwargs)

    def fake_mkdir(path, *args, **kwargs):
        if under_git(path):
            raise refuse(path, "mkdir")
        return real_mkdir(path, *args, **kwargs)

    def fake_replace(src, dst, *args, **kwargs):
        if under_git(dst):
            raise refuse(dst, "replace")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "mkdir", fake_mkdir)
    monkeypatch.setattr(os, "replace", fake_replace)

    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            argv = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
            argv = [os.fsdecode(x) for x in argv]
            stdin = kw["stdin"] if "stdin" in kw else (a[2] if len(a) > 2 else None)
            env = kw.get("env") or os.environ
            cwd = kw.get("cwd")
            inv.spawns.append(
                {"argv": argv, "cwd": None if cwd is None else os.fsdecode(cwd), "stdin": stdin, "site": _site()}
            )
            site = _site()
            label = " ".join(argv[:4])
            if stdin not in _STDIN_OK:
                inv.finding(site, f"child [{label}] spawned without stdin=DEVNULL/PIPE (inherits the host stdin)")
                raise PermissionError(errno.EACCES, "child inherits stdin", label)
            if _is_git(argv):
                sub = _git_subcommand(argv)
                if sub not in _LOCKFREE_GIT and env.get("GIT_OPTIONAL_LOCKS") != "0":
                    inv.finding(
                        site,
                        f"git {sub} without GIT_OPTIONAL_LOCKS=0 refreshes the index and takes .git/index.lock",
                    )
                    raise PermissionError(errno.EACCES, "Permission denied", str(git / "index.lock"))
            elif not run_non_git_children:
                inv.finding(site, f"child [{label}] spawned (not run by the harness)")
                raise OSError(errno.EPERM, "non-git child not run under the harness", label)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", RecordingPopen)

    import coordinator_core.warm.client as warm_client

    def refuse_pipe(endpoint: str):
        inv.pipe_connects.append(endpoint)
        inv.finding(_site(), f"warm-engine pipe connect to {endpoint}")
        raise FileNotFoundError(errno.ENOENT, "pipe connect refused by harness", endpoint)

    monkeypatch.setattr(warm_client, "_open_pipe", refuse_pipe)
    return inv


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL, **no_console_creationflags(),
    )


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.fixture()
def hang_dump():
    """A hang prints its stack after `_HANG_DUMP_SECS` instead of stalling; always cancelled."""
    faulthandler.dump_traceback_later(_HANG_DUMP_SECS, file=sys.__stderr__)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hang_dump) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "seed.txt").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", "seed.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    for name in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    monkeypatch.setenv("COORDINATOR_WARM", "1")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _load_cli(monkeypatch: pytest.MonkeyPatch):
    """`coordinator/bin/sizing-assemble.py` has a hyphenated name, so it is loaded by path; its
    `import lib` needs `coordinator/bin` on sys.path for the life of the call."""
    monkeypatch.syspath_prepend(str(_SIZING_ASSEMBLE.parent))
    monkeypatch.syspath_prepend(str(_SIZING_ASSEMBLE.parent / "lib"))
    import cc_invoke

    # claude-klabauter is never an engine to the ladder; the harness runs this checkout's code as the engine.
    monkeypatch.setattr(cc_invoke, "_resolve_engine_root", lambda *a, **k: str(_REPO))
    spec = importlib.util.spec_from_file_location("sizing_assemble_bin_under_denial", _SIZING_ASSEMBLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_default_call_runs_whole_under_a_denied_git(repo, monkeypatch):
    """Falsifier: red while the default call spawns a stdin-inheriting child, a lock-taking git, or a
    warm-pipe connect, or writes under `.git`; each finding names the call site and the mechanism."""
    cli = _load_cli(monkeypatch)
    git = repo / ".git"
    before = _tree(git)
    inv = _install_whole_process_denial(monkeypatch, git)

    out, err = io.StringIO(), io.StringIO()
    t0, w0 = time.process_time(), time.monotonic()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(
            ["--tshirt", "XS", "--intent", "denied-probe harness intent", "--write", _REL]
        )
    spent, wall = time.process_time() - t0, time.monotonic() - w0

    report = json.dumps(
        {"rc": rc, "process_secs": round(spent, 3), "wall_secs": round(wall, 3),
         "denied_write_attempts": len(inv.denied_writes), "findings": inv.findings,
         "spawns": [(s["argv"][:4], s["stdin"], s["site"]) for s in inv.spawns], "stderr": err.getvalue()[-600:]},
        indent=2, default=str,
    )
    assert inv.findings == [], report
    assert rc == 0, report
    assert spent < _PROCESS_BUDGET_SECS, report
    assert (repo / _REL).is_file(), report
    assert (repo / ".coordinator-local" / "coordinator-sessions" / _SID).is_dir(), report
    assert _tree(git) == before, report


def test_codex_host_guard_runs_whole_under_a_denied_git(repo, monkeypatch):
    """The codex PreToolUse entry on a valid payload: its child gets an explicit stdin pipe and writes nothing
    under `.git`."""
    spec = importlib.util.spec_from_file_location("unimatrix_host_guard_under_denial", _HOST_GUARD)
    guard_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_mod)
    git = repo / ".git"
    before = _tree(git)
    inv = _install_whole_process_denial(monkeypatch, git, run_non_git_children=True)
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"},
         "session_id": _SID, "cwd": str(repo)}
    )

    answer, state = guard_mod.guard("codex", payload, str(_REPO))

    report = json.dumps({"answer": answer, "state": state, "findings": inv.findings}, indent=2, default=str)
    assert inv.findings == [], report
    # The codex guard is spawn-free since 45c30f3a83; any child it does start still gets an explicit stdin pipe.
    assert all(s["stdin"] in _STDIN_OK for s in inv.spawns), report
    assert _tree(git) == before, report


def test_harness_refuses_each_class_it_inventories(repo, monkeypatch):
    """The inventory is not vacuous: an inherited stdin, a lock-taking git, a `.git` write and a pipe connect
    are each refused and named."""
    inv = _install_whole_process_denial(monkeypatch, repo / ".git")
    with pytest.raises(PermissionError):
        subprocess.run([sys.executable, "-c", "pass"])
    with pytest.raises(PermissionError):
        subprocess.run(["git", "-C", str(repo), "status"], stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], stdin=subprocess.DEVNULL, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "status"], stdin=subprocess.DEVNULL, capture_output=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    with pytest.raises(PermissionError):
        os.mkdir(repo / ".git" / "coordinator-sessions")
    import coordinator_core.warm.client as warm_client

    with pytest.raises(FileNotFoundError):
        warm_client._open_pipe("pipe-name")

    joined = "\n".join(inv.findings)
    assert "inherits the host stdin" in joined
    assert "git status without GIT_OPTIONAL_LOCKS=0" in joined and "index.lock" in joined
    assert "mkdir under .git" in joined
    assert inv.pipe_connects == ["pipe-name"]
    assert sum("rev-parse" in s["argv"] for s in inv.spawns) == 1


@pytest.mark.deliberate_wall_clock(reason="bounds that the host-guard process answers instead of hanging on its stdin or engine, a wait CPU time cannot see")
def test_codex_host_guard_cli_process_answers_within_wall(tmp_path):
    """The codex hook command as the host launches it: a real `unimatrix-host-guard.py --host codex` process
    given the payload on a closed stdin pipe answers one JSON line and exits 0 inside a bounded wall."""
    payload = json.dumps(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "echo hi"},
         "session_id": _SID, "cwd": str(tmp_path)}
    )
    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(_HOST_GUARD), "--host", "codex", "--engine-root", str(_REPO)],
        input=payload, capture_output=True, text=True, timeout=10, cwd=tmp_path, **no_console_creationflags(),
    )
    wall = time.monotonic() - t0
    report = json.dumps({"rc": proc.returncode, "wall": round(wall, 3), "out": proc.stdout, "err": proc.stderr})
    assert proc.returncode == 0, report
    assert json.loads(proc.stdout.strip()) == {}, report
    assert proc.stderr.strip() in ("neutral-unknown", "degraded"), report
    assert wall < 5, report
