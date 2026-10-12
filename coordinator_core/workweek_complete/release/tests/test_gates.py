"""Tests for the gate run: export of one commit, probe, gate order, removal, d0 labelling."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.git.run import GitResult
from coordinator_core.workweek_complete.release import gates
from coordinator_core.workweek_complete.release.contract import GATE_IDS

REPO_ROOT = Path(__file__).resolve().parents[4]
CSN = REPO_ROOT / "coordinator" / "bin" / "lib" / "coordinator_safe_name.py"
FAST = "validate-fast-and-packageability"
NUL = bytes([0])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "coordinator" / "bin" / "lib").mkdir(parents=True)
    (r / "coordinator" / "bin" / "lib" / "coordinator_safe_name.py").write_bytes(CSN.read_bytes())
    (r / "tracked.txt").write_text("one\n")
    (r / ".gitignore").write_text("scratch/\n")
    subprocess.run(["git", "init", "-q", str(r)], check=True)
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "core.autocrlf", "false")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "c1")
    return r


def _sha(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


class FakeRun:
    def __init__(self, *, probe_outside=False, exit_codes=None, raise_for=None):
        self.calls: list[tuple[list[str], str, dict]] = []
        self.probe_outside = probe_outside
        self.exit_codes = exit_codes or {}
        self.raise_for = raise_for

    def __call__(self, argv, *, cwd, env):
        self.calls.append((list(argv), cwd, dict(env)))
        if "-c" in argv:
            where = Path(cwd).parent / "elsewhere" if self.probe_outside else Path(cwd)
            return SimpleNamespace(returncode=0, stdout=str(where / "coordinator_core" / "__init__.py"))
        if self.raise_for and self.raise_for in " ".join(argv):
            raise OSError("cannot start")
        for key, code in self.exit_codes.items():
            if key in " ".join(argv):
                return SimpleNamespace(returncode=code, stdout="")
        return SimpleNamespace(returncode=0, stdout="")


@pytest.fixture
def d0_present(tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    entry = plugin / "tests" / "plugin-ecosystem"
    entry.mkdir(parents=True)
    (entry / "run.js").write_text("")
    (plugin / ".claude-plugin").mkdir()
    (plugin / ".claude-plugin" / "plugin.json").write_text('{"version": "9.9.9"}')
    from coordinator_core.subagent_sandbox import provision_report

    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: str(plugin))
    monkeypatch.setattr(gates.shutil, "which", lambda name: "NODE")
    return plugin


def _gate_dir(repo: Path) -> Path:
    return repo / "scratch" / "release-gate"


def test_export_holds_exactly_the_commit_files(repo):
    sha = _sha(repo)
    (repo / "tracked.txt").write_text("modified\n")
    (repo / "staged.txt").write_text("s\n")
    _git(repo, "add", "staged.txt")
    (repo / "untracked.txt").write_text("u\n")
    (repo / "later.txt").write_text("l\n")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "c2")
    out = gates.export_commit(repo, sha)
    assert out == (_gate_dir(repo) / sha).resolve()
    names = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
    assert names == set(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    assert (out / "tracked.txt").read_text() == "one\n"


def test_stale_directories_are_swept_including_other_commits(repo):
    sha = _sha(repo)
    stale = _gate_dir(repo) / ("a" * 40)
    stale.mkdir(parents=True)
    (stale / "x").write_text("x")
    same = _gate_dir(repo) / sha
    same.mkdir()
    (same / "old.txt").write_text("old")
    out = gates.export_commit(repo, sha)
    assert not stale.exists()
    assert not (out / "old.txt").exists()


def test_destination_outside_scratch_raises(repo, monkeypatch):
    monkeypatch.setattr(gates, "_RELEASE_GATE_DIR", ("elsewhere", "release-gate"))
    with pytest.raises(ValueError):
        gates.export_commit(repo, _sha(repo))
    with pytest.raises(ValueError):
        gates.export_commit(repo, "../../" + "a" * 34)


def test_failed_archive_raises_and_leaves_nothing(repo):
    def failing(args, **kw):
        return GitResult(returncode=128, stdout="", stderr="fatal", timed_out=False)

    with pytest.raises(RuntimeError):
        gates.export_commit(repo, _sha(repo), run_git=failing)
    assert not _gate_dir(repo).exists() or not any(_gate_dir(repo).iterdir())
    with pytest.raises(RuntimeError):
        gates.run_gates(repo, _sha(repo), run=FakeRun(), run_git=failing)
    assert not _gate_dir(repo).exists() or not any(_gate_dir(repo).iterdir())


def test_timed_out_archive_raises(repo):
    def slow(args, **kw):
        return GitResult(returncode=-1, stdout="", stderr="", timed_out=True)

    with pytest.raises(RuntimeError, match="timed out"):
        gates.export_commit(repo, _sha(repo), run_git=slow)


def test_all_green_records_in_order_name_the_commit(repo, d0_present):
    sha = _sha(repo)
    result = gates.run_gates(repo, sha, run=FakeRun())
    assert [r.gate for r in result] == list(GATE_IDS)
    assert all(r.status == "ran" and r.exit_code == 0 for r in result)
    assert all(r.tested_sha == sha for r in result)
    assert not (_gate_dir(repo) / sha).exists()


def test_every_run_argv_is_interpreter_and_export_script_with_gate_env(repo, d0_present):
    sha = _sha(repo)
    fake = FakeRun()
    gates.run_gates(repo, sha, run=fake)
    export = (_gate_dir(repo) / sha).resolve()
    fast = [c for c in fake.calls if FAST in " ".join(c[0])]
    assert len(fast) == 1
    for argv, cwd, env in fake.calls:
        assert Path(cwd) == export
        assert env["GIT_CEILING_DIRECTORIES"] == str(export.parent)
        assert env["COORDINATOR_ENGINE_ROOT"] == str(export)
        assert "COORDINATOR_FAST_TEST_CMD" not in env
        if argv[0] != "NODE":
            assert argv[0] == sys.executable
    fast_argv = fast[0][0]
    assert Path(fast_argv[1]).resolve().is_relative_to(export)
    assert fast_argv[2:] == ["fast", "--repo-root", str(export)]


def test_probe_outside_export_starts_no_gate(repo):
    fake = FakeRun(probe_outside=True)
    result = gates.run_gates(repo, _sha(repo), run=fake)
    assert [(r.gate, r.status) for r in result] == [(GATE_IDS[0], "not-started")]
    assert len(fake.calls) == 1


def test_failing_first_gate_ends_the_run(repo):
    real = gates.run_git

    def selective(args, **kw):
        if "ls-tree" in args:
            return GitResult(returncode=128, stdout="", stderr="", timed_out=False)
        return real(args, **kw)

    fake = FakeRun()
    result = gates.run_gates(repo, _sha(repo), run=fake, run_git=selective)
    assert [(r.gate, r.status) for r in result] == [("d6", "not-started")]
    assert len(fake.calls) == 1


def test_illegal_path_gives_nonzero_d6_and_stops(repo, d0_present):
    real = gates.run_git

    def fake_git(args, **kw):
        if "ls-tree" in args:
            return GitResult(0, "", "", False, b"ok.txt" + NUL + b"dir/bad:name.txt" + NUL)
        return real(args, **kw)

    fake = FakeRun()
    result = gates.run_gates(repo, _sha(repo), run=fake, run_git=fake_git)
    assert [(r.gate, r.exit_code) for r in result] == [("d6", 1)]
    assert len(fake.calls) == 1


def test_clean_tree_gives_d6_zero(repo, d0_present):
    result = gates.run_gates(repo, _sha(repo), run=FakeRun())
    assert (result[0].gate, result[0].status, result[0].exit_code) == ("d6", "ran", 0)


def test_d0_absent_is_not_present_and_unbound(repo, monkeypatch):
    from coordinator_core.subagent_sandbox import provision_report

    monkeypatch.setattr(provision_report, "resolve_plugin_root", lambda: None)
    fake = FakeRun()
    result = gates.run_gates(repo, _sha(repo), run=fake)
    d0 = result[1]
    assert (d0.gate, d0.status, d0.bound_to_candidate) == ("d0", "not-present", False)
    assert not any("--test" in c[0] for c in fake.calls)
    assert result[2].gate == FAST


def test_d0_names_plugin_root_and_version_and_summary_lists_it_apart(repo, d0_present):
    result = gates.run_gates(repo, _sha(repo), run=FakeRun())
    d0 = result[1]
    assert d0.bound_to_candidate is False
    assert str(d0_present) in d0.detail and "9.9.9" in d0.detail
    assert "NOT bound" in d0.detail
    assert [r.gate for r in result.bound()] == ["d6", FAST]
    assert [r.gate for r in result.unbound()] == ["d0"]
    text = result.summary()
    assert "d0" not in text.split("NOT bound to candidate:")[0]
    assert "d0" in text.split("NOT bound to candidate:")[1]


def test_red_fast_gate_is_a_red_record(repo, d0_present):
    result = gates.run_gates(repo, _sha(repo), run=FakeRun(exit_codes={FAST: 1}))
    assert (result[-1].gate, result[-1].status, result[-1].exit_code) == (FAST, "ran", 1)


def test_d0_red_ends_the_run(repo, d0_present):
    result = gates.run_gates(repo, _sha(repo), run=FakeRun(exit_codes={"--test": 1}))
    assert [(r.gate, r.exit_code) for r in result] == [("d6", 0), ("d0", 1)]


def test_child_that_cannot_start_is_not_started_and_export_removed(repo, d0_present):
    sha = _sha(repo)
    result = gates.run_gates(repo, sha, run=FakeRun(raise_for=FAST))
    assert (result[-1].gate, result[-1].status, result[-1].exit_code) == (FAST, "not-started", None)
    assert not (_gate_dir(repo) / sha).exists()


def test_export_removed_when_a_file_is_read_only(repo, d0_present):
    sha = _sha(repo)

    class Lock(FakeRun):
        def __call__(self, argv, *, cwd, env):
            os.chmod(Path(cwd) / "tracked.txt", stat.S_IREAD)
            return super().__call__(argv, cwd=cwd, env=env)

    result = gates.run_gates(repo, sha, run=Lock())
    assert not (_gate_dir(repo) / sha).exists()
    assert result.leftover == ""


def test_no_worktree_in_any_argv(repo, d0_present):
    seen: list[list[str]] = []
    real = gates.run_git

    def spy(args, **kw):
        seen.append(list(args))
        return real(args, **kw)

    fake = FakeRun()
    gates.run_gates(repo, _sha(repo), run=fake, run_git=spy)
    flat = [t for a in seen + [c[0] for c in fake.calls] for t in a]
    assert "worktree" not in flat
