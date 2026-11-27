"""landing_integrity.committed_image_import_findings: one fail-closed committed-image import check (HEAD + landing blobs) on every land arm."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks.tests._git_fixture import git as _git, write as _write
from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

CALLER = "from pkg.b import new_fn\n"
B_HEAD = "def old_fn():\n    pass\n"
B_DIRT = B_HEAD + "\ndef new_fn():\n    pass\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/b.py", B_HEAD)
    _write(tmp_path, "pkg/a.py", "x = 1\n")
    _write(tmp_path, "pkg/gone.py", "def f():\n    pass\n")
    _write(tmp_path, "pkg/user.py", "from pkg.gone import f\n")
    _write(tmp_path, "top.py", "y = 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _reasons(found) -> list:
    return [f.reason for f in found]


def _blob_sha(root: Path, text: str) -> str:
    out = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=root, input=text.encode("utf-8"), check=True,
        capture_output=True, **no_console_creationflags(),
    )
    return out.stdout.decode().strip()


def test_a_name_only_a_tracked_worktree_edit_binds_is_lost_definition(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER)

    found = li.committed_image_import_findings(repo, ["pkg/a.py"], [], {"pkg/a.py": CALLER.encode()})

    assert _reasons(found) == [li.LOST_DEFINITION]
    assert found[0].path == "pkg/a.py" and "new_fn" in found[0].detail


def test_the_land_run_arm_keeps_the_pair_as_provenance_for_its_own_carry(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER)

    found = li.committed_image_import_findings(
        repo, ["pkg/a.py"], [], {"pkg/a.py": CALLER.encode()}, carries_pairs=True,
    )

    assert _reasons(found) == [li.IMPORT_CLOSURE_UNTRACKED]


def test_the_landing_blob_without_the_import_passes_whatever_the_worktree_holds(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER)

    assert li.committed_image_import_findings(repo, ["pkg/a.py"], [], {"pkg/a.py": b"x = 2\n"}) == []


def test_a_definer_landing_with_its_caller_passes(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER)
    blobs = {"pkg/a.py": CALLER.encode(), "pkg/b.py": B_DIRT.encode()}

    assert li.committed_image_import_findings(repo, ["pkg/a.py", "pkg/b.py"], [], blobs) == []


def test_no_snapshot_falls_back_to_head_importers_of_a_deleted_module(repo) -> None:
    (repo / "pkg" / "gone.py").unlink()

    fallback = li.committed_image_import_findings(repo, [], ["pkg/gone.py"], {}, residue_py=None)
    snapshot_empty = li.committed_image_import_findings(repo, [], ["pkg/gone.py"], {}, residue_py=())

    assert [(f.path, f.reason) for f in fallback] == [("pkg/user.py", li.LOST_DEFINITION)]
    assert snapshot_empty == []


def test_a_residue_snapshot_keeps_its_hit_provenance(repo) -> None:
    (repo / "pkg" / "gone.py").unlink()

    found = li.committed_image_import_findings(repo, [], ["pkg/gone.py"], {}, residue_py=["pkg/user.py"])

    assert [(f.path, f.reason) for f in found] == [("pkg/user.py", li.IMPORT_CLOSURE_UNTRACKED)]


def test_head_importer_candidates_are_the_py_files_beside_a_touched_module(repo) -> None:
    _write(repo, "pkg/deep/inner.py", "z = 1\n")
    _write(repo, "pkg/notes.txt", "n\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more")

    got = li.head_importer_candidates(repo, ["pkg/a.py"], ["pkg/gone.py"])

    assert got == ["top.py", "pkg/__init__.py", "pkg/b.py", "pkg/user.py"]


def test_the_check_spawns_nothing(repo, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("spawn")

    _write(repo, "pkg/b.py", B_DIRT)
    (repo / "pkg" / "gone.py").unlink()
    monkeypatch.setattr(subprocess, "Popen", boom)

    found = li.committed_image_import_findings(
        repo, ["pkg/a.py"], ["pkg/gone.py"], {"pkg/a.py": CALLER.encode()}, residue_py=None,
    )

    assert {f.reason for f in found} == {li.LOST_DEFINITION}


def test_a_detector_failure_is_a_finding_not_a_pass(repo, monkeypatch) -> None:
    from coordinator_core.authoring_leaks import import_closure

    def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(import_closure, "residue_findings", boom)

    found = li.committed_image_import_findings(repo, [], ["pkg/gone.py"], {}, residue_py=None)

    assert _reasons(found) == [li.IMPORT_CLOSURE_UNTRACKED]
    assert "down" in found[0].detail


def _gate(repo: Path, paths, deleted=(), **kw) -> dict:
    return terminal_commit._landing_integrity_gate(repo, list(paths), list(deleted), None, {}, "r1", **kw)


def test_the_terminal_gate_without_a_snapshot_refuses_a_deletion_a_head_importer_still_reads(repo) -> None:
    (repo / "pkg" / "gone.py").unlink()

    out = _gate(repo, [], ["pkg/gone.py"], residue_known=False)

    assert out["refused"] == li.LOST_DEFINITION, out
    assert out["residue_import_check"] == "head-importers"
    assert out["integrity"][0]["path"] == "pkg/user.py"


def test_the_terminal_gate_with_a_snapshot_still_passes_a_deletion_nothing_dirty_imports(repo) -> None:
    (repo / "pkg" / "gone.py").unlink()

    out = _gate(repo, [], ["pkg/gone.py"], residue_py=[], residue_known=True)

    assert "refused" not in out and "residue_import_check" not in out, out


def test_the_terminal_gate_judges_the_kept_blob_not_the_worktree(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", "x = 2\n")
    kept = {"pkg/a.py": _blob_sha(repo, CALLER)}

    out = _gate(repo, ["pkg/a.py"], landing_blobs=kept)

    assert out["refused"] == li.LOST_DEFINITION, out
    assert out["integrity"][0]["path"] == "pkg/a.py"


def test_the_terminal_gate_does_not_refuse_when_the_kept_blob_drops_the_import(repo) -> None:
    _write(repo, "pkg/b.py", B_DIRT)
    _write(repo, "pkg/a.py", CALLER)
    kept = {"pkg/a.py": _blob_sha(repo, "x = 2\n")}

    out = _gate(repo, ["pkg/a.py"], landing_blobs=kept)

    assert "refused" not in out, out
