"""Tests for coordinator_core.git.residue_classes that spawn a real git process."""

import subprocess

import pytest

from coordinator_core.git import residue_classes as rc
from coordinator_core.git.tests.test_residue_classes import _dirty
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(cwd, *args):
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, **no_console_creationflags()
    )


def _repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README").write_text("x")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_dirty_residue_filters_sorts_and_excludes(tmp_path):
    root = _repo(tmp_path)
    _dirty(root, "state/sizings/a.yaml", "docs/plans/p.seam.yaml", "src/x.py", "docs/plans/p.md")
    out = rc.dirty_engine_residue(root, [])
    assert out == {"engine_residue": ["docs/plans/p.seam.yaml", "state/sizings/a.yaml"]}
    out = rc.dirty_engine_residue(root, ["state/sizings/a.yaml"])
    assert out == {"engine_residue": ["docs/plans/p.seam.yaml"]}


def test_absent_root_is_not_a_gap(tmp_path):
    root = _repo(tmp_path)
    _dirty(root, "state/sizings/a.yaml")
    assert rc.dirty_engine_residue(root, []) == {"engine_residue": ["state/sizings/a.yaml"]}


def test_cap_reports_total(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(rc, "RESIDUE_CAP", 2)
    _dirty(root, *[f"state/sizings/{i}.yaml" for i in range(3)])
    out = rc.dirty_engine_residue(root, [])
    assert len(out["engine_residue"]) == 2 and out["engine_residue_total"] == 3
