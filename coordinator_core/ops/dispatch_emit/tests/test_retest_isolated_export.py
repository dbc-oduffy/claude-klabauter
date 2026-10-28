"""``retest_targeted --isolated-export``: the detached child tests a HEAD export plus the run write set,
then the live tree, and records both on a real tmp git repo."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import landing_export as le
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import landing_retest
from coordinator_core.ops.dispatch_emit import retest_targeted
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN_ID = "run-iso"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TEST_SRC = "from mod import VALUE\n\n\ndef test_value():\n    assert VALUE == 1\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, creationflags=_NO_WINDOW)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "test_a.py").write_text(_TEST_SRC, encoding="utf-8")
    _git(root, "add", "mod.py", "test_a.py")
    _git(root, "commit", "-q", "-m", "init")
    # foreign edit outside the run's write set: red in the live tree only
    (root / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
    # the run's own edit, live only
    (root / "test_a.py").write_text(_TEST_SRC + "\n# run edit\n", encoding="utf-8")
    return root


def _task_output(tmp_path: Path, root: Path, *, manifest: bool) -> Path:
    params: dict = {"run_id": RUN_ID, "targeted_test_paths": ["test_a.py"]}
    if manifest:
        run_dir = root / "scratch" / "warp" / RUN_ID
        run_dir.mkdir(parents=True)
        (run_dir / "manifest.json").write_text(
            json.dumps({"rows": [{"id": "C1", "writes": ["test_a.py"]}, {"id": "C2", "writes": ["test_a.py"]}]}),
            encoding="utf-8",
        )
        script = run_dir / "run.workflow.mjs"
        script.write_text(f"{ASK_MANIFEST_MARKER}scratch/warp/{RUN_ID}/manifest.json\n", encoding="utf-8")
        params["script_path"] = f"scratch/warp/{RUN_ID}/run.workflow.mjs"
    out = tmp_path / "task.output"
    out.write_text(json.dumps({"next_action": {"params": params}}), encoding="utf-8")
    return out


def _pytest_run(seen: list):
    def _run(argv, cwd, *, log=None, timeout=None, append=False):
        seen.append((list(argv), Path(cwd)))
        cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *argv[2:]]
        with open(log, "ab" if append else "wb") as sink:
            return subprocess.run(
                cmd, cwd=str(cwd), stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            ).returncode

    return _run


@pytest.fixture
def git_spy(monkeypatch):
    spawned: list[list[str]] = []
    real_stream = git_run.run_git_stream
    real_popen = subprocess.Popen

    def _stream(args, **kw):
        spawned.append(list(args))
        return real_stream(args, **kw)

    class _Popen(real_popen):
        def __init__(self, args, *a, **kw):
            spawned.append([str(x) for x in (args if isinstance(args, (list, tuple)) else [args])])
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(git_run, "run_git_stream", _stream)
    monkeypatch.setattr(subprocess, "Popen", _Popen)
    return spawned


def _child(root: Path, out: Path, seen: list, **kw) -> int:
    return retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_pytest_run(seen), **kw,
    )


def test_export_passes_while_live_fails_with_manifest_write_set(tmp_path, monkeypatch, git_spy):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)
    seen: list = []
    rc = _child(root, out, seen, dirty=lambda r: [])
    record = json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))
    assert rc == 0 and record["status"] == "pass"
    assert record["export"]["status"] == "pass"
    assert record["live"]["status"] == "fail"
    assert any(t.endswith("test_value") for t in record["live"]["failed_tests"])
    iso = record["isolated_export"]
    assert iso["write_set_source"] == "manifest" and iso["write_set"] == ["test_a.py"]
    assert iso["root"] == le.run_export_root(root, RUN_ID).as_posix()
    assert Path(iso["root"]).parent.parent.name == "warp"
    assert [cwd for _, cwd in seen] == [Path(iso["root"]), root]
    assert not Path(iso["root"]).exists()
    assert not any(v in a for a in git_spy for v in ("read-tree", "checkout-index"))


def _no_exports(root: Path) -> bool:
    run_dir = root / "scratch" / "warp" / RUN_ID
    return not any((run_dir / n).exists() for n in ("export", "export-head", "export.base"))


def _record(root: Path) -> dict:
    return json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))


def test_failing_export_is_removed_and_is_the_top_level_status(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    (root / "test_a.py").write_text("def test_value():\n    assert False\n", encoding="utf-8")
    out = _task_output(tmp_path, root, manifest=True)
    rc = _child(root, out, [], dirty=lambda r: [])
    record = _record(root)
    assert rc == 1 and record["status"] == "fail" and record["export"]["status"] == "fail"
    assert record["isolated_export"]["root"] and record["log"]
    assert _no_exports(root)


def test_export_build_error_leaves_no_export(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)

    def _half_built(_root, export_root, _writes):
        export_root.mkdir(parents=True)
        (export_root / "half").write_text("x", encoding="utf-8")
        raise le.LandingExportError("disk")

    monkeypatch.setattr(le, "build_head_export", _half_built)
    rc = _child(root, out, [], dirty=lambda r: [])
    record = _record(root)
    assert rc == 2 and record["status"] == "error" and record["isolated_export"]["root"]
    assert _no_exports(root)


def test_deadline_expiry_leaves_no_export(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)
    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=lambda *a, **k: retest_targeted.LAUNCH_TIMED_OUT, dirty=lambda r: [],
    )
    record = _record(root)
    assert rc == 2 and record["status"] == "error" and record["isolated_export"]["root"] and record["log"]
    assert _no_exports(root)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX SIGTERM delivery")
def test_sigterm_mid_pass_leaves_no_export_and_keeps_the_record(tmp_path, monkeypatch):
    import os
    import signal

    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)
    before = signal.getsignal(signal.SIGTERM)

    def _term(argv, cwd, *, log=None, timeout=None, append=False):
        assert (Path(cwd) / "test_a.py").exists() and Path(cwd) != root
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("handler did not unwind")

    rc = retest_targeted.main(
        ["--from-task-output", str(out), "--repo-root", str(root), "--isolated-export", "--detached-child"],
        run=_term, dirty=lambda r: [],
    )
    record = _record(root)
    assert rc == 2 and record["status"] == "error" and "signal" in record["reason"]
    assert record["isolated_export"]["root"] and record["log"]
    assert _no_exports(root)
    assert signal.getsignal(signal.SIGTERM) == before


def test_child_start_sweeps_an_unreferenced_export(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)
    stale = root / "scratch" / "warp" / "run-stale" / "export"
    stale.mkdir(parents=True)
    (stale / "f.txt").write_text("x", encoding="utf-8")
    seen: list = []
    _child(root, out, seen, dirty=lambda r: [])
    assert not stale.exists()
    assert [cwd for _, cwd in seen][0] == le.run_export_root(root, RUN_ID)


def test_no_manifest_fails_closed_without_building_an_export(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=False)
    built: list = []
    monkeypatch.setattr(le, "build_head_export", lambda *a, **k: built.append(a))
    seen: list = []
    rc = _child(root, out, seen, dirty=lambda r: ["test_a.py"])
    record = json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))
    assert rc == 2 and record["status"] == "error"
    assert "landing path set not derivable" in record["reason"]
    assert "landing path set not derivable" in record["baseline_skipped"]["reason"]
    assert built == [] and seen == [] and "live" not in record and "export" not in record
    assert record["isolated_export"]["files"] == []
    assert not le.run_export_root(root, RUN_ID).exists()


def test_export_build_failure_is_an_error_with_no_live_run(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=True)

    def _boom(*a, **k):
        raise le.LandingExportError("no head")

    monkeypatch.setattr(le, "build_head_export", _boom)
    seen: list = []
    rc = _child(root, out, seen, dirty=lambda r: ["test_a.py"])
    record = json.loads(landing_retest.fresh_verdict_path(root, RUN_ID).read_text(encoding="utf-8"))
    assert rc == 2 and record["status"] == "error" and "no head" in record["reason"]
    assert seen == [] and "live" not in record


def test_flag_is_forwarded_to_the_detached_child_only_when_set(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_FAST_TEST_CMD", "pytest -q")
    root = _repo(tmp_path)
    out = _task_output(tmp_path, root, manifest=False)
    for flag in (True, False):
        spawned: list = []
        retest_targeted.main(
            ["--from-task-output", str(out), "--repo-root", str(root), *(["--isolated-export"] if flag else [])],
            dirty=lambda r: [], spawn=lambda *a, **k: spawned.append(a[2]) or True,
        )
        assert ("--isolated-export" in spawned[0]) is flag
        landing_retest.fresh_verdict_path(root, RUN_ID).unlink()


def _refs(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "for-each-ref", "--format=%(refname) %(objectname)"],
        check=True, capture_output=True, text=True, creationflags=_NO_WINDOW,
    ).stdout + subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, creationflags=_NO_WINDOW,
    ).stdout + (root / ".git" / "config").read_text()


def test_process_in_export_cannot_commit_into_the_enclosing_repo(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    export = root / "scratch" / "warp" / RUN_ID / "export"
    export.mkdir(parents=True)
    before = _refs(root)
    monkeypatch.setenv("GIT_DIR", str(root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(root))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@example.invalid")
    log = tmp_path / "git.log"
    rc = retest_targeted._run_argv(["git", "commit", "--allow-empty", "-q", "-m", "leak"], export, log=log)
    assert rc != 0
    assert _refs(root) == before
