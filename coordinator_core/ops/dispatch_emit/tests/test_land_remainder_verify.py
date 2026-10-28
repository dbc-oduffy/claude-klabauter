"""verify_remainder: import closure inline, export retest through the detached child's verdict."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_remainder_contract as c
from coordinator_core.ops.dispatch_emit import land_remainder_verify as v

pytestmark = pytest.mark.spawns_process

RUN = "run-c2"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _w(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _w(root, "cc/__init__.py", "")
    _w(root, "cc/mod.py", "X = 1\n")
    _w(root, "scratch/.keep", "")
    _git(root, "add", "cc")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _base_export(root: Path) -> None:
    """The run's base export: the tree of the base commit, plus the run-base record."""
    warp = root / "scratch" / "warp" / RUN
    base = warp / v.BASE_EXPORT_NAME
    shutil.copytree(root / "cc", base / "cc")
    (warp / "run-base").write_text(_git(root, "rev-parse", "HEAD"), encoding="utf-8")


def _no_spawn(*_a, **_k):
    pytest.fail("must not spawn")


def test_untracked_importer_yields_import_closure_open(repo):
    _w(repo, "cc/gm/__init__.py", "")
    _w(repo, "cc/gm/rec.py", "Y = 1\n")
    _w(repo, "cc/guard.py", "def f():\n    from cc.gm.rec import Y\n    return Y\n")
    out = v.verify_remainder(repo, RUN, ["cc/guard.py"], spawn=_no_spawn)
    assert out["refused"] == c.REFUSED_IMPORT_CLOSURE_OPEN
    assert out["closure_open"] and "cc/guard.py" in out["closure_open"][0]


def test_closed_importer_without_tests_passes_without_a_child(repo):
    _w(repo, "cc/guard.py", "from cc.mod import X\n")
    out = v.verify_remainder(repo, RUN, ["cc/guard.py"], spawn=_no_spawn)
    assert "refused" not in out and out["retest"]["status"] == "no-tests"


def test_no_base_export_refuses_and_spawns_nothing(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    out = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=_no_spawn)
    assert out["refused"] == c.REFUSED_NO_BASE_EXPORT
    assert not v.verdict_path(repo, RUN).exists()


def test_no_verdict_spawns_the_child_once_and_refuses_pending(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    _base_export(repo)
    calls: list = []

    def spawn(engine, script, argv, **kw):
        calls.append((script, argv))
        return True

    first = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=spawn)
    again = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=_no_spawn)
    assert first["refused"] == again["refused"] == c.REFUSED_RETEST_PENDING
    assert len(calls) == 1 and calls[0][0].endswith("land_remainder_verify.py")
    assert "--child" in calls[0][1] and "cc/test_mod.py" in calls[0][1]


def test_spawn_failure_is_refused_not_pending(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    _base_export(repo)
    out = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=lambda *a, **k: False)
    assert out["refused"] == c.REFUSED_RETEST_FAILED
    assert out["retest"]["status"] == "error"


def test_failing_test_in_the_export_yields_retest_failed(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    assert False\n")
    _base_export(repo)
    code = v.run_child(repo, RUN, ["cc/test_mod.py"], run_pass=lambda *a: ("fail", "", [1], True))
    assert code == 1
    out = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=_no_spawn)
    assert out["refused"] == c.REFUSED_RETEST_FAILED and out["retest"]["status"] == "fail"
    assert not (repo / "scratch" / "warp" / RUN / "export").exists()


def test_passing_verdict_admits_and_changed_bytes_void_it(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    _base_export(repo)
    seen: list = []

    def run_pass(run, export_root, tests, log, deadline):
        seen.append((Path(export_root) / tests[0]).read_text(encoding="utf-8"))
        return "pass", "", [0], True

    assert v.run_child(repo, RUN, ["cc/test_mod.py"], run_pass=run_pass) == 0
    assert seen == ["def test_a():\n    pass\n"]
    ok = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=_no_spawn)
    assert "refused" not in ok and ok["retest"]["status"] == "pass"

    _w(repo, "cc/test_mod.py", "def test_a():\n    assert 1\n")
    assert v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=_no_spawn)["refused"] == c.REFUSED_NO_BASE_EXPORT
    (repo / "scratch" / "warp" / RUN / "run-base").unlink()
    _base_export(repo)
    calls: list = []
    stale = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=lambda *a, **k: calls.append(a) or True)
    assert stale["refused"] == c.REFUSED_RETEST_PENDING and len(calls) == 1


def test_stale_running_record_respawns(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    _base_export(repo)
    from coordinator_core.ops.dispatch_emit import landing_retest

    v._write_verdict(v.verdict_path(repo, RUN), {
        "status": "running", "deadline": "2000-01-01T00:00:00+00:00",
        "fingerprint": landing_retest.landing_fingerprint(repo, ["cc/test_mod.py"]),
    })
    calls: list = []
    out = v.verify_remainder(repo, RUN, ["cc/test_mod.py"], spawn=lambda *a, **k: calls.append(a) or True)
    assert out["refused"] == c.REFUSED_RETEST_PENDING and len(calls) == 1


def test_unsafe_run_id_is_rejected(repo):
    _w(repo, "cc/test_mod.py", "def test_a():\n    pass\n")
    with pytest.raises(ValueError):
        v.verify_remainder(repo, "../x", ["cc/test_mod.py"], spawn=_no_spawn)
