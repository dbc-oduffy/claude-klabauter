"""brief_inputs: per-field values, one root resolution, no spawn, degradation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.git import repo_root as repo_root_mod
from coordinator_core.ops.doc_registry import resolve_doc_registry_config
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workweek_complete import brief_inputs as bi

pytestmark = pytest.mark.spawns_process

_README = "# Title\n\nSee `nonexistent/missing_file.py` and `coordinator_core/engine_root.py`.\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "coordinator.local.md").write_text(
        "---\n"
        "human_facing_docs: [README.md, MISSING.md]\n"
        "doc_staleness_commits: 0\n"
        "doc_staleness_days: 0\n"
        "---\n",
        encoding="utf-8",
    )
    (root / "README.md").write_text(_README, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    (root / "other.txt").write_text("x", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "two")
    monkeypatch.chdir(root)
    monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(ENGINE_CHECKOUT_ROOT))
    return root


def test_repo_root_is_the_fixture_root(repo):
    assert Path(bi.compute_brief_inputs().repo_root) == repo


def test_fixture_exercises_the_nonempty_branches(repo):
    got = bi.compute_brief_inputs()
    reasons = {f["token"] for f in got.doc_verify_findings_by_doc["README.md"]}
    assert "nonexistent/missing_file.py" in reasons


def test_repo_root_resolved_once_per_call(repo, monkeypatch):
    calls = []
    real = repo_root_mod.git_common_dir

    def counting(cwd):
        calls.append(cwd)
        return real(cwd)

    monkeypatch.setattr(repo_root_mod, "git_common_dir", counting)
    bi.compute_brief_inputs()
    assert len(calls) == 1


def test_repo_root_resolution_and_verify_spawn_nothing(repo, monkeypatch):
    spawned = []
    real_init = subprocess.Popen.__init__

    def hook(self, args, *a, **kw):
        spawned.append(args)
        real_init(self, args, *a, **kw)

    monkeypatch.setattr(subprocess.Popen, "__init__", hook)
    root = bi._resolve_repo_root(None)
    registry = resolve_doc_registry_config(root)
    findings = bi._doc_verify_findings_by_doc(root, registry)
    assert findings
    assert spawned == []


def test_no_backslash_in_repo_root(repo):
    assert "\\" not in bi.compute_brief_inputs().repo_root


def test_outside_a_repo_every_field_is_empty(tmp_path, monkeypatch):
    bare = tmp_path / "nowhere"
    bare.mkdir()
    monkeypatch.chdir(bare)
    monkeypatch.setattr(repo_root_mod, "git_common_dir", lambda cwd: None)
    got = bi.compute_brief_inputs()
    assert got.repo_root is None
    assert got.doc_verify_findings_by_doc == {}
    assert got.version_recommendation is None


def test_compute_brief_inputs_never_runs_the_register_stall_scan(repo, monkeypatch):
    from coordinator_core.ops import requirement_register

    calls = []
    monkeypatch.setattr(
        requirement_register, "stall_report", lambda *a, **kw: calls.append(a)
    )
    bi.compute_brief_inputs()
    assert calls == []


def test_degraded_values_on_injected_failure(repo, monkeypatch, capsys):
    from coordinator_core.ops import doc_content_verify
    from coordinator_core.workweek_complete.release import version_rule

    def boom(*a, **kw):
        raise RuntimeError("injected")

    monkeypatch.setattr(doc_content_verify, "verify_doc_on_disk", boom)
    monkeypatch.setattr(version_rule, "recommend_level", boom)

    got = bi.compute_brief_inputs()
    err = capsys.readouterr().err

    assert got.doc_verify_findings_by_doc == {}
    assert got.version_recommendation is None
    assert "doc-verify findings unavailable, degrading to zero findings: injected" in err


def test_registry_failure_degrades_both_doc_signals(repo, monkeypatch, capsys):
    from coordinator_core.ops import doc_registry

    def boom(root):
        raise RuntimeError("registry down")

    monkeypatch.setattr(doc_registry, "resolve_doc_registry_config", boom)
    got = bi.compute_brief_inputs()
    assert got.doc_verify_findings_by_doc == {}
    assert "registry down" in capsys.readouterr().err


def test_brief_inputs_names_exactly_the_three_consumer_fields():
    import dataclasses

    assert {f.name for f in dataclasses.fields(bi.BriefInputs)} == {
        "repo_root",
        "doc_verify_findings_by_doc",
        "version_recommendation",
    }


def test_compute_brief_inputs_imports_no_doc_staleness_and_spawns_nothing(repo):
    import json
    import os
    import sys

    probe = (
        "import json, subprocess, sys\n"
        "spawned = []\n"
        "real = subprocess.Popen.__init__\n"
        "def hook(self, args, *a, **kw):\n"
        "    spawned.append(str(args))\n"
        "    real(self, args, *a, **kw)\n"
        "subprocess.Popen.__init__ = hook\n"
        "from coordinator_core.workweek_complete import brief_inputs as bi\n"
        "bi.compute_brief_inputs()\n"
        "print(json.dumps({'spawned': spawned,"
        " 'stale': 'coordinator_core.ops.doc_staleness' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(ENGINE_CHECKOUT_ROOT)},
        **no_console_creationflags(),
    )
    out = json.loads(done.stdout.strip().splitlines()[-1])
    assert out == {"spawned": [], "stale": False}
