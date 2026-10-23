"""residue_findings: residue and staged .py importers of a module or top-level name a commit removes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.authoring_leaks import import_closure
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root, check=True, capture_output=True, **no_console_creationflags(),
    )


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "cc/__init__.py", "")
    _write(tmp_path, "cc/_probe.py", "def _old():\n    pass\n")
    _write(tmp_path, "cc/mod.py", "def gone():\n    pass\n\n\ndef kept():\n    pass\n")
    _write(tmp_path, "cc/user.py", "from cc._probe import _old\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def _delete_probe(root):
    (root / "cc/_probe.py").unlink()


def _only(findings, path, line=1, tail="deleted by this commit"):
    assert len(findings) == 1, findings
    f = findings[0]
    assert (f.path, f.line) == (path, line)
    assert f.kind == "import_closure"
    assert f.detail.endswith(tail)


def test_tracked_dirty_residue_importer_is_found(repo):
    _write(repo, "cc/user.py", "from cc._probe import _old\nx = 1\n")
    _delete_probe(repo)
    _only(import_closure.residue_findings(repo, ["cc/user.py"], [], ["cc/_probe.py"]), "cc/user.py")


def test_untracked_residue_importer_is_found(repo):
    _write(repo, "cc/new.py", "from cc._probe import _old\n")
    _delete_probe(repo)
    _only(import_closure.residue_findings(repo, ["cc/new.py"], [], ["cc/_probe.py"]), "cc/new.py")


@pytest.mark.parametrize(
    "rel,text,line",
    [
        ("cc/x.py", "import cc._probe\n", 1),
        ("cc/x.py", "from . import _probe\n", 1),
        ("cc/x.py", "from ._probe import _old\n", 1),
        ("cc/x.py", "from cc import (\n    _probe,\n)\n", 1),
        ("cc/x.py", "def f():\n    from cc._probe import _old\n    return _old\n", 2),
    ],
)
def test_import_forms(repo, rel, text, line):
    _write(repo, rel, text)
    _delete_probe(repo)
    _only(import_closure.residue_findings(repo, [rel], [], ["cc/_probe.py"]), rel, line)


def test_no_finding_when_importer_is_deleted(repo):
    _delete_probe(repo)
    (repo / "cc/user.py").unlink()
    assert import_closure.residue_findings(repo, ["cc/user.py"], [], ["cc/_probe.py", "cc/user.py"]) == []


def test_no_finding_when_landing_importer_dropped_the_import(repo):
    _write(repo, "cc/user.py", "x = 1\n")
    _delete_probe(repo)
    assert import_closure.residue_findings(repo, [], ["cc/user.py"], ["cc/_probe.py"]) == []


def test_no_finding_for_non_first_party_target(repo):
    _write(repo, "cc/new.py", "from other._probe import _old\n")
    _delete_probe(repo)
    assert import_closure.residue_findings(repo, ["cc/new.py"], [], ["cc/_probe.py"]) == []


def test_no_finding_when_replaced_by_package(repo):
    _delete_probe(repo)
    _write(repo, "cc/_probe/__init__.py", "def _old():\n    pass\n")
    assert import_closure.residue_findings(repo, ["cc/user.py"], ["cc/_probe/__init__.py"], ["cc/_probe.py"]) == []


def test_committed_unmodified_importer_outside_both_sets_is_out_of_scope(repo):
    _delete_probe(repo)
    assert import_closure.residue_findings(repo, [], [], ["cc/_probe.py"]) == []


def test_landing_importer_with_unchanged_import_is_found(repo):
    _delete_probe(repo)
    _only(import_closure.residue_findings(repo, [], ["cc/user.py"], ["cc/_probe.py"]), "cc/user.py")


def test_tracked_residue_judged_by_its_head_bytes_too(repo):
    _write(repo, "cc/user.py", "x = 1\n")
    _delete_probe(repo)
    _only(import_closure.residue_findings(repo, ["cc/user.py"], [], ["cc/_probe.py"]), "cc/user.py")


def test_head_and_worktree_hit_yield_one_finding(repo):
    _write(repo, "cc/user.py", "from cc._probe import _old\n# dirty\n")
    _delete_probe(repo)
    assert len(import_closure.residue_findings(repo, ["cc/user.py"], [], ["cc/_probe.py"])) == 1


def _land_mod(root, text):
    _write(root, "cc/mod.py", text)
    _write(root, "cc/user.py", "from cc.mod import gone\nx = 1\n")


def test_removed_name_is_found(repo):
    _land_mod(repo, "def kept():\n    pass\n")
    out = import_closure.residue_findings(repo, ["cc/user.py"], ["cc/mod.py"], [])
    _only(out, "cc/user.py", tail="absent from HEAD")


def test_rebound_name_is_not_removed(repo):
    _write(repo, "cc/other.py", "def gone():\n    pass\n")
    _land_mod(repo, "from cc.other import gone\n\n\ndef kept():\n    pass\n")
    assert import_closure.residue_findings(repo, ["cc/user.py"], ["cc/mod.py", "cc/other.py"], []) == []


def test_name_never_defined_at_head_is_not_this_commits_break(repo):
    _land_mod(repo, "def kept():\n    pass\n")
    _write(repo, "cc/user.py", "from cc.mod import never_there\n")
    assert import_closure.residue_findings(repo, ["cc/user.py"], ["cc/mod.py"], []) == []


def test_indented_def_removal_is_not_a_removed_name(repo):
    _write(repo, "cc/mod.py", "class K:\n    def inner(self):\n        pass\n\n\ndef gone():\n    pass\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "k")
    _write(repo, "cc/mod.py", "class K:\n    pass\n\n\ndef gone():\n    pass\n")
    _write(repo, "cc/user.py", "from cc.mod import inner\n")
    assert import_closure.residue_findings(repo, ["cc/user.py"], ["cc/mod.py"], []) == []


def test_zero_spawns_and_no_residue_read_without_a_removal(repo, monkeypatch):
    _write(repo, "cc/user.py", "from cc._probe import _old\nx = 1\n")
    _delete_probe(repo)
    real = subprocess.Popen

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _only(import_closure.residue_findings(repo, ["cc/user.py"], [], ["cc/_probe.py"]), "cc/user.py")
    reads = []
    real_read = import_closure._read_text
    monkeypatch.setattr(import_closure, "_read_text", lambda p: (reads.append(p), real_read(p))[1])
    _write(repo, "cc/mod.py", "def gone():\n    pass\n\n\ndef kept():\n    pass\n\n\ndef added():\n    pass\n")
    assert import_closure.residue_findings(repo, ["cc/user.py"], ["cc/mod.py"], ["docs/a.md"]) == []
    assert [p for p in reads if Path(p).name == "user.py"] == []
    monkeypatch.setattr(subprocess, "Popen", real)
