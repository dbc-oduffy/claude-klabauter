"""An unreadable worktree status never refuses the clean export: the export holds HEAD bytes for every
undeclared file, so the dirty-import pre-check is advisory when git cannot answer."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import test_reach, worktree_snapshot


def test_unreadable_status_skips_the_check_with_an_advisory(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(worktree_snapshot, "changed_python_paths", lambda root: None)

    cov._check_overlay_imports(tmp_path, ["pkg/mod.py"])

    err = capsys.readouterr().err
    assert "advisory" in err and "status unreadable" in err


def test_a_declared_import_of_a_name_missing_at_head_still_refuses(tmp_path, monkeypatch):
    """The one refusal that stays: the run would test code the landing commit never contains."""
    monkeypatch.setattr(worktree_snapshot, "changed_python_paths", lambda root: {"pkg/mod.py", "pkg/dep.py"})
    monkeypatch.setattr(cov, "_head_sources", lambda root, rels: {r: b"x = 1\n" for r in rels})
    monkeypatch.setattr(test_reach, "_records_for", lambda root, rel: [])
    monkeypatch.setattr(test_reach, "_resolved_imports", lambda rel, recs: [("pkg.dep", ("new_name",))])

    with pytest.raises(cov.CoTenantDependency):
        cov._check_overlay_imports(tmp_path, ["pkg/mod.py"])
