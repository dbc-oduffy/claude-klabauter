"""rulings.coverage: prose-only, dangling, covered, superseded excluded, no spawn."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.rulings.coverage import _handler, coverage
from coordinator_core.rulings import store
from coordinator_core.rulings.contract import Ruling


def _ruling(rid, **kw) -> Ruling:
    base = dict(id=rid, pm_turn="state/sizings/x.yaml R1", scope=("repo",), text="t", recorded="2026-11-21")
    base.update(kw)
    return Ruling(**base)


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_present.py").write_text("", encoding="utf-8")
    store.append(tmp_path, _ruling("r-prose"))
    store.append(tmp_path, _ruling("r-cov", enforcement="tests/test_present.py"))
    store.append(tmp_path, _ruling("r-dangle", enforcement="tests/test_absent.py"))
    store.append(tmp_path, _ruling("r-old", status="superseded", superseded_by="r-cov"))
    return tmp_path


def test_buckets(root):
    assert coverage(root) == {
        "covered": [{"id": "r-cov", "enforcement": "tests/test_present.py"}],
        "prose_only": ["r-prose"],
        "dangling": [{"id": "r-dangle", "enforcement": "tests/test_absent.py"}],
    }


def test_superseded_with_null_enforcement_is_not_prose_only(root):
    assert "r-old" not in coverage(root)["prose_only"]


def test_empty_register(tmp_path):
    assert coverage(tmp_path) == {"covered": [], "prose_only": [], "dangling": []}


def test_escaping_enforcement_path_is_dangling(tmp_path):
    (tmp_path / ".git").mkdir()
    store.append(tmp_path, _ruling("r-esc", enforcement="../outside.py"))
    assert coverage(tmp_path)["dangling"] == [{"id": "r-esc", "enforcement": "../outside.py"}]


def test_dr_prose_only_owed_reads_as_prose_only(tmp_path):
    (tmp_path / ".git").mkdir()
    d = tmp_path / "docs" / "decisions"
    d.mkdir(parents=True)
    (d / "DR-900-x.md").write_text(
        "---\ntitle: T\nstatus: accepted\ndeciders:\n  - \"x (PM)\"\nenforcement: prose-only-owed\n---\nbody\n",
        encoding="utf-8",
    )
    assert "DR-900-x" in coverage(tmp_path)["prose_only"]


def test_handler_default_arguments(root):
    reply = _handler({}, repo_root=root)
    assert reply["prose_only"] == ["r-prose"]
    assert [d["id"] for d in reply["dangling"]] == ["r-dangle"]


def test_handler_requires_repo_root():
    with pytest.raises(ValueError):
        _handler({})
