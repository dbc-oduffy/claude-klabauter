"""`percolate-gate.py publish-readiness` — one invocation, every blocker.

WHAT THIS FILE PROTECTS. Five separate failures sit on the publish path, each
one invisible until the one before it is fixed: the engine root a publish tool
binds, an unset registry key, a registry value naming the deployed engine, a
branch with no upstream, and a remote that forbids ref creation. Discovering
them serially costs one full publish run each, against a shared clone. The
property under test is therefore NOT that any single check is clever -- each has
its own tests where its logic lives -- but that NO check short-circuits the
report: a FAIL early in the list must not hide what comes after it, because
hiding it is the defect.

Run: python -m pytest coordinator/bin/tests/test_percolate_publish_readiness.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent

_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_gate_readiness", _BIN_DIR / "percolate-gate.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, text=True, check=True, **_NO_CONSOLE,
    ).stdout.strip()


@pytest.fixture()
def gate():
    return _load_module()


def test_the_report_never_stops_at_the_first_failure(gate, monkeypatch, capsys):
    monkeypatch.setattr(gate, "_check_engine_root", lambda f: (f.append((gate._FAIL, "engine-root", "boom")), None)[1])
    args = gate._build_parser().parse_args(["publish-readiness", "claude-klabauter"])
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] engine-root" in out
    assert "NOT READY" in out


def test_a_worktree_that_is_the_deployed_engine_is_a_fail_with_the_fix(gate, monkeypatch, tmp_path):
    repo = tmp_path / "engine"
    repo.mkdir()
    _git(repo, "init", "-b", "main")

    import coordinator_core.engine_root as engine_root

    monkeypatch.setattr(engine_root, "is_published_engine_mirror", lambda root: True)
    findings: list = []
    assert gate._check_worktree(findings, [str(repo)]) is None
    verdict, name, detail = findings[0]
    assert (verdict, name) == (gate._FAIL, "mirror-worktree")
    assert "deployed engine mirror" in detail
    assert "machine-local set publish.mirrors" in detail


def test_rows_landing_in_two_worktrees_are_refused(gate, tmp_path):
    roots = []
    for name in ("a", "b"):
        repo = tmp_path / name
        repo.mkdir()
        _git(repo, "init", "-b", "main")
        roots.append(str(repo))
    findings: list = []
    assert gate._check_worktree(findings, roots) is None
    assert findings[0][0] == gate._FAIL
    assert "different worktrees" in findings[0][2]


def test_an_existing_remote_branch_needs_no_creation_permission(gate, tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    (origin / "seed").write_text("s\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "seed")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))

    findings: list = []
    gate._check_ref_creation(findings, str(clone), "main")
    assert findings[0][0] == gate._PASS
    assert "updates it" in findings[0][2]


def test_an_unanswerable_creation_question_warns_and_never_passes(gate, tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    (origin / "seed").write_text("s\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "seed")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    monkeypatch.setattr(gate, "_branch_rules_over_http", lambda slug, branch: None)

    findings: list = []
    gate._check_ref_creation(findings, str(clone), "branch-that-does-not-exist")
    verdict, _name, detail = findings[0]
    assert verdict == gate._WARN
    assert "would CREATE" in detail
    assert "--dry-run" in detail


def test_a_remote_that_forbids_creation_fails_and_names_the_allowed_branches(
    gate, tmp_path, monkeypatch
):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    _git(origin, "config", "user.email", "t@example.invalid")
    _git(origin, "config", "user.name", "t")
    (origin / "seed").write_text("s\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "seed")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "remote", "set-url", "origin", "https://github.com/owner/repo")
    monkeypatch.setattr(
        gate, "_branch_rules_over_http", lambda slug, branch: '[{"type": "creation"}]'
    )
    monkeypatch.setattr(gate, "_readiness_git", _fake_git_for_creation(gate, clone))

    findings: list = []
    gate._check_ref_creation(findings, str(clone), "new-branch")
    verdict, _name, detail = findings[0]
    assert verdict == gate._FAIL
    assert "FORBIDS ref creation" in detail
    assert "main" in detail


def _fake_git_for_creation(gate, clone: Path):
    real = gate._readiness_git

    class _Proc:
        def __init__(self, rc: int, out: str):
            self.returncode = rc
            self.stdout = out
            self.stderr = ""

    def _fake(repo, args, *, remote=False):
        if args[:1] == ["ls-remote"] and "--exit-code" in args:
            return _Proc(2, "")
        if args[:1] == ["ls-remote"]:
            return _Proc(0, "abc123\trefs/heads/main\ndef456\trefs/heads/candidate\n")
        return real(repo, args, remote=remote)

    return _fake
