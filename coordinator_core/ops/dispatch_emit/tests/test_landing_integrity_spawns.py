"""landing_integrity cases that spawn: a fresh interpreter and real tmp_path git repos."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.tests.test_landing_integrity import C, _w
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def test_recorded_base_branch_does_not_import_dispatch_emit_op(tmp_path):
    _w(tmp_path, "scratch/warp/r1/run-base", C + "\n")
    repo_root = Path(__file__).resolve().parents[4]
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(repo_root)!r})\n"
        "from pathlib import Path\n"
        "from coordinator_core.ops.dispatch_emit import landing_integrity as li\n"
        f"got = li.run_base_for(Path({str(tmp_path)!r}), 'r1', {{}})\n"
        f"assert got == {C!r}, got\n"
        "assert 'coordinator_core.ops.dispatch_emit.op' not in sys.modules\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True, text=True,
        **no_console_creationflags(),
    )
    assert done.returncode == 0, done.stderr


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


@pytest.fixture
def closure_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _w(tmp_path, "cc/__init__.py", "")
    _w(tmp_path, "cc/_probe.py", "def _old():\n    pass\n")
    _w(tmp_path, "cc/guard.py", "def f():\n    return 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def test_closure_incident_shape_lazy_import_of_worktree_only_package(closure_repo):
    _w(closure_repo, "cc/gm/__init__.py", "")
    _w(closure_repo, "cc/gm/rec.py", "X = 1\n")
    _w(closure_repo, "cc/guard.py", "def f():\n    from cc.gm.rec import X\n    return X\n")
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], [])
    assert [(f.path, f.reason) for f in out] == [("cc/guard.py", li.IMPORT_CLOSURE_UNTRACKED)]
    assert out[0].detail.startswith("2: ")


def test_closure_n1_shape_name_bound_only_by_modified_tracked_module(closure_repo):
    _w(closure_repo, "cc/_probe.py", "def _old():\n    pass\n\ndef _offer():\n    pass\n")
    _w(closure_repo, "cc/commit.py", "from cc._probe import _offer\n")
    out = li.import_closure_findings(closure_repo, ["cc/commit.py"], [])
    assert len(out) == 1 and out[0].reason == li.IMPORT_CLOSURE_UNTRACKED and "_offer" in out[0].detail


def test_closure_landed_text_without_the_import_yields_none(closure_repo):
    _w(closure_repo, "cc/gm/__init__.py", "")
    _w(closure_repo, "cc/gm/rec.py", "X = 1\n")
    _w(closure_repo, "cc/guard.py", "def f():\n    from cc.gm.rec import X\n    return X\n")
    landed = {"cc/guard.py": "def f():\n    return 2\n"}
    assert li.import_closure_findings(closure_repo, ["cc/guard.py"], [], landed.get) == []


def test_closure_clean_landing_and_deletion_of_imported_module(closure_repo):
    _w(closure_repo, "cc/guard.py", "from cc._probe import _old\n")
    assert li.import_closure_findings(closure_repo, ["cc/guard.py"], []) == []
    (closure_repo / "cc/_probe.py").unlink()
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], ["cc/_probe.py"])
    assert len(out) == 1 and "deleted by this commit" in out[0].detail


def test_closure_detector_exception_fails_closed(closure_repo, monkeypatch):
    from coordinator_core.authoring_leaks import import_closure

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(import_closure, "detect", boom)
    out = li.import_closure_findings(closure_repo, ["cc/guard.py"], [])
    assert len(out) == 1 and out[0].reason == li.IMPORT_CLOSURE_UNTRACKED and "boom" in out[0].detail


def _git_repo(tmp_path):
    def git(*a):
        return subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    return tmp_path, git


def test_gate_receipt_names_run_deleted_targets_and_stays_no_verdict(tmp_path):
    from coordinator_core.ops.dispatch_emit import terminal_commit

    root, git = _git_repo(tmp_path)
    (root / "test_gone.py").write_text("def test_g():\n    pass\n", encoding="utf-8")
    (root / "mod.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    base = git("rev-parse", "HEAD")
    git("rm", "-q", "test_gone.py")
    git("commit", "-q", "-m", "delete")
    record = {"run_id": "r1", "head": git("rev-parse", "HEAD"), "status": "no-tests", "tests": [],
              "tests_run": 0, "dropped": ["test_gone.py"], "covered": li.landing_fingerprint(root, ["mod.py"])}
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    params = {"run_base_sha": base}
    out = terminal_commit._landing_integrity_gate(root, ["mod.py"], [], None, params, "r1")
    assert out["targeted_tests_check"] == "no-verdict"
    assert "test_gone.py" in out["targeted_tests_note"]
