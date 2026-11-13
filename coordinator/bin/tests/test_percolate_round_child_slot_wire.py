"""percolate-round hands its publish slot to a real publish.py-side child, keyed by mirror identity.

Real: bare origin, live and scratch clones (the DR-445 dest-root override), the round's own
`_cmd_round_default`, its slot lease on origin, the child process and its slot gate
(`publish.py::_hold_publish_slots`). Stubbed: the child's remainder past the gate -- publish.py
needs a registered target and a source repo; the child here is a short interpreter that runs
publish.py's own gate against the repo root publish.py derives from `dest`.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_child_slot_wire.py -q
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.percolate import publish_lease
from coordinator_core.percolate import publish_slot as slot_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
_NC = no_console_creationflags()

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

_CHILD = """
import importlib.util, os, sys
from contextlib import ExitStack
from pathlib import Path
spec = importlib.util.spec_from_file_location("publish_child", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)
dest = Path(sys.argv[2])
root = m._dest_repo_root(dest) or dest
inherited = os.environ.pop("PUBLISH_SLOT_INHERITED", "")
with ExitStack() as stack:
    gate = m._hold_publish_slots([root], stack, inherited=inherited)
    sys.exit(gate.exit_code or 0)
"""


def _load_round():
    spec = importlib.util.spec_from_file_location("percolate_round_child_slot", _BIN_DIR / "percolate-round.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_round()


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True,
        env={**os.environ, **_GIT_ENV}, check=True, **_NC,
    ).stdout.strip()


class _Stop(Exception):
    """Raised by the harness once the child has reported, to unwind the round."""


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path / "klabauter.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, **_NC)
    subprocess.run(["git", "init", "-q", str(seed)], check=True, **_NC)
    (seed / "coordinator_core").mkdir()
    (seed / "coordinator_core" / "a.txt").write_text("a\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", str(origin), "HEAD:refs/heads/candidate")
    live, clone = tmp_path / "live", tmp_path / "scratch"
    for path in (live, clone):
        subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True, **_NC)
        _git(path, "checkout", "-q", "candidate")
    return live, clone


def _drive(tmp_path, live, clone, monkeypatch, *, strip_token: bool):
    """Run the real `_cmd_round_default` under the dest-root override; returns
    (round rc or None when unwound by _Stop, child rc, child stderr)."""
    dest, override = _mod._resolve_dest_override(str(live / "coordinator_core"), str(clone))
    assert override is not None
    seen = {}

    def run(cmd, *, timeout, **kwargs):
        if str(_mod._PUBLISH) not in [str(c) for c in cmd]:
            return _mod.subprocess.CompletedProcess(cmd, 1, "", "stop")
        env = dict(kwargs["env"])
        if strip_token:
            env.pop(slot_mod.PUBLISH_SLOT_INHERITED_ENV, None)
            env["COORDINATOR_SESSION_ID"] = "foreign-session-child"
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH")]))
        child = subprocess.run(
            [sys.executable, "-c", _CHILD, str(_mod._PUBLISH), dest],
            capture_output=True, text=True, env=env, timeout=timeout, **_NC,
        )
        seen["rc"], seen["err"] = child.returncode, child.stderr
        if child.returncode == 0:
            raise _Stop
        return child

    monkeypatch.setattr(_mod, "_run", run)
    args = _mod._build_parser().parse_args(["claude-klabauter", "--no-publish"])
    work = tmp_path / "work"
    work.mkdir()
    try:
        rc = _mod._cmd_round_default(
            args, "claude-klabauter", str(tmp_path), str(tmp_path), dest, work, override=override
        )
    except _Stop:
        rc = None
    return rc, seen["rc"], seen["err"]


def test_child_confirms_the_rounds_slot_under_the_dest_root_override(tmp_path, repos, monkeypatch):
    live, clone = repos
    rc, child_rc, child_err = _drive(tmp_path, live, clone, monkeypatch, strip_token=False)
    assert child_rc == 0, child_err
    assert "inherited" in child_err and "BUSY" not in child_err
    assert rc is None
    assert publish_lease.read(clone, "origin") is None


def test_round_releases_its_slot_when_the_child_exits_75(tmp_path, repos, monkeypatch):
    live, clone = repos
    rc, child_rc, child_err = _drive(tmp_path, live, clone, monkeypatch, strip_token=True)
    assert child_rc == 75 and "BUSY" in child_err
    assert rc == _mod._EXIT_LOCK_BUSY
    assert publish_lease.read(clone, "origin") is None
