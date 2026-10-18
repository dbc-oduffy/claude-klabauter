"""`plan-spine-check` BASENAME-COLLISION advisory: a new `writes` path whose basename is tracked elsewhere."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_collision", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_collision"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


@pytest.fixture(autouse=True)
def _no_width(monkeypatch):
    monkeypatch.setattr(psc, "_ENGINE_WIDTH_FNS_OVERRIDE", None)
    monkeypatch.setattr(psc, "_compute_width", lambda path: (None, "stubbed"))


@pytest.fixture
def repo(tmp_path):
    from coordinator_core.git import ls_files

    ls_files._tracked_files_cached.cache_clear()
    git = ["git", "-C", str(tmp_path)]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([*git, "init", "-q"], check=True, creationflags=flags)
    (tmp_path / "tools/x").mkdir(parents=True)
    (tmp_path / "tools/x/closure_job.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run([*git, "add", "tools/x/closure_job.py"], check=True, creationflags=flags)
    yield tmp_path
    ls_files._tracked_files_cached.cache_clear()


def _plan(repo: Path, writes: list) -> Path:
    path = repo / "plan.md"
    rows = "- id: C1\n  title: t\n  writes: [" + ", ".join(writes) + "]\n"
    path.write_text(
        "---\ntitle: t\n---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "```\n",
        encoding="utf-8",
    )
    return path


def _collisions(report):
    return [a for a in report["advisories"] if "BASENAME-COLLISION" in a["error"]]


def test_new_file_with_tracked_basename_emits_one_advisory(repo):
    found = _collisions(psc.check_plan(_plan(repo, ["game_head/closure_job.py"])))
    assert len(found) == 1
    assert found[0]["class"] == "advisory"
    assert "game_head/closure_job.py" in found[0]["error"]
    assert "tools/x/closure_job.py" in found[0]["error"]
    assert "reuse or justify" in found[0]["error"]


def test_existing_path_and_unique_basename_emit_none(repo):
    report = psc.check_plan(_plan(repo, ["tools/x/closure_job.py", "game_head/unique_thing.py"]))
    assert _collisions(report) == []


def test_advisory_never_fails_the_check(repo):
    plan = _plan(repo, ["game_head/closure_job.py"])
    for for_execution in (False, True):
        report = psc.check_plan(plan, for_execution=for_execution)
        assert not [r for r in report["rows"] if "BASENAME-COLLISION" in r["error"]]
        assert len(_collisions(report)) == 1


def test_exempt_basenames_never_collide(repo):
    (repo / "pkg").mkdir()
    (repo / "pkg/__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "pkg/__init__.py"],
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    from coordinator_core.git import ls_files

    ls_files._tracked_files_cached.cache_clear()
    assert _collisions(psc.check_plan(_plan(repo, ["other/__init__.py"]))) == []


def test_dot_slash_prefixed_existing_path_is_not_new(repo):
    assert _collisions(psc.check_plan(_plan(repo, ["./tools/x/closure_job.py"]))) == []


def test_one_listing_per_check_regardless_of_rows(repo, monkeypatch):
    from coordinator_core.git import ls_files

    calls = []
    real = ls_files.tracked_files
    monkeypatch.setattr(ls_files, "tracked_files", lambda *a, **k: calls.append(a) or real(*a, **k))
    plan = _plan(repo, ["a/closure_job.py", "b/closure_job.py", "c/other.py"])
    assert len(_collisions(psc.check_plan(plan))) == 2
    assert len(calls) == 1
