"""Tests for `coordinator_core.git.eol_declared` -- the write-scoped EOL drift
detector that replaces the deleted `ops/eol/` family (kill-ledger K-064).

Every test here builds a REAL git repo with a real `.gitattributes` and real
drifted bytes. None of them mock `git ls-files --eol`, deliberately: the whole
premise under test is what git actually reports for a file whose declaration
and disk bytes disagree, and a mock of that call would be a mock of the only
fact in question. The repos are tiny (two files) and each test pays one or two
git spawns.
"""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core.git.eol_declared import (
    EXECUTABLE_SUFFIXES,
    Drift,
    executable_paths,
    find_declared_eol_drift,
    repair_declared_eol_drift,
)
from coordinator_core.win_portability import no_console_creationflags

#: Same pairing `test_ls_files_bytes.py` carries for the same reason: these
#: build real repos and spawn real git, so they are admitted by the spawn
#: ratchet and deselected from the per-commit tier.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        **no_console_creationflags(),
    )


def _git_out(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    """A repo pinning `*.cmd` to CRLF, with one correct launcher committed."""
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitattributes").write_bytes(b"*.cmd text eol=crlf\n*.sh text eol=lf\n")
    (root / "run.cmd").write_bytes(b"@echo off\r\necho hi\r\n")
    (root / "notes.md").write_bytes(b"# notes\n")
    _git(root, "add", ".gitattributes", "run.cmd", "notes.md")
    _git(root, "commit", "-qm", "init")
    return root


def _drift_to_lf(repo, name="run.cmd"):
    """Rewrite a CRLF-declared file to LF-only -- the invisible defect."""
    target = repo / name
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))


# --------------------------------------------------------------------------
# The filter-first budget property: no executable in the commit, no work.
# --------------------------------------------------------------------------


def test_executable_paths_selects_only_the_declared_classes():
    picked = executable_paths(
        ["a.cmd", "b.ps1", "c.sh", "d.bat", "e.py", "f.md", "g.diff", "h.patch"]
    )
    assert picked == ["a.cmd", "b.ps1", "c.sh", "d.bat"]


def test_executable_paths_is_case_insensitive_and_deduplicates():
    assert executable_paths(["A.CMD", "A.CMD", "b.Ps1"]) == ["A.CMD", "b.Ps1"]


def test_data_files_are_never_selected():
    """K-019's labour census found ALL 43 historical violations on data files
    under scratch dirs and zero on executables. Selecting them would re-adopt
    exactly the noise the census measured; the baton's anti-scope names it.
    """
    assert executable_paths(
        ["state/review-slices/x.diff", "state/subagent-share/y.patch", "z.sha"]
    ) == []


def test_no_executable_in_the_commit_spawns_nothing(repo, monkeypatch):
    """The budget case. Most commits carry no launcher, and those must not pay
    a process for this check."""
    import coordinator_core.git.eol_declared as mod

    def explode(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("spawned git for a commit with no executable path")

    monkeypatch.setattr(mod, "run_git", explode)
    assert find_declared_eol_drift(repo, ["notes.md", "a.py"]) == []


def test_one_spawn_for_many_executables(repo, monkeypatch):
    """Batched, never one-per-path -- the amplification class
    `test_no_unbatched_per_item_git_spawn` exists to catch."""
    import coordinator_core.git.eol_declared as mod

    calls = []
    real = mod.run_git

    def counting(args, **kwargs):
        calls.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(mod, "run_git", counting)
    find_declared_eol_drift(repo, ["run.cmd", "a.sh", "b.ps1", "c.bat", "notes.md"])
    assert len(calls) == 1
    assert calls[0].count("--") == 1


# --------------------------------------------------------------------------
# Detection.
# --------------------------------------------------------------------------


def test_the_drift_git_cannot_show_you_is_detected(repo):
    """The load-bearing case: a CRLF-declared launcher sitting LF on disk."""
    _drift_to_lf(repo)
    drifts = find_declared_eol_drift(repo, ["run.cmd"])
    assert drifts == [Drift(path="run.cmd", declared="crlf", on_disk="lf")]


def test_git_diff_stays_empty_for_the_drift_this_module_reports(repo):
    """The premise, pinned rather than asserted in prose: git's own content
    view shows NOTHING for a file this detector flags. If this test ever goes
    red because `git diff` grew an opinion, this module's justification is what
    changed, and that is worth a failure."""
    _drift_to_lf(repo)
    assert _git_out(repo, "diff", "--stat").strip() == ""
    assert find_declared_eol_drift(repo, ["run.cmd"])


def test_a_correct_launcher_is_not_a_finding(repo):
    assert find_declared_eol_drift(repo, ["run.cmd"]) == []


def test_an_undeclared_path_is_not_a_finding(tmp_path):
    """No `eol=` means nothing for the bytes to contradict."""
    root = tmp_path / "u"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "run.cmd").write_bytes(b"@echo off\n")
    _git(root, "add", "run.cmd")
    _git(root, "commit", "-qm", "init")
    assert find_declared_eol_drift(root, ["run.cmd"]) == []


def test_lf_declaration_is_honoured_in_its_own_direction(repo):
    """`eol=lf` is a declaration too -- a CRLF `.sh` is the same defect
    mirrored, and a detector that only knows CRLF is half a detector."""
    (repo / "tool.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    _git(repo, "add", "tool.sh")
    _git(repo, "commit", "-qm", "add sh")
    (repo / "tool.sh").write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    assert find_declared_eol_drift(repo, ["tool.sh"]) == [
        Drift(path="tool.sh", declared="lf", on_disk="crlf")
    ]


def test_untracked_path_folds_to_no_finding(repo):
    """A detector on a commit path must never raise on a path git does not
    know; the commit is the caller's job, not this module's to fail."""
    assert find_declared_eol_drift(repo, ["never-added.cmd"]) == []


def test_reads_stdout_bytes_not_stdout(repo, monkeypatch):
    """Regression pin, 2026-08-30. The first cut read `result.stdout` under
    `binary=True`, where that view is empty by construction -- so the detector
    reported CLEAN for every commit and nothing failed. A silent-pass detector
    is worse than none, and this is the one line that decides it.
    """
    import coordinator_core.git.eol_declared as mod

    real = mod.run_git

    def blanked(args, **kwargs):
        return real(args, **kwargs)._replace(stdout="")

    monkeypatch.setattr(mod, "run_git", blanked)
    _drift_to_lf(repo)
    assert find_declared_eol_drift(repo, ["run.cmd"])


def test_a_failed_git_call_folds_to_no_finding(repo, monkeypatch):
    import coordinator_core.git.eol_declared as mod

    real = mod.run_git

    def failed(args, **kwargs):
        return real(args, **kwargs)._replace(returncode=128)

    monkeypatch.setattr(mod, "run_git", failed)
    _drift_to_lf(repo)
    assert find_declared_eol_drift(repo, ["run.cmd"]) == []


# --------------------------------------------------------------------------
# Repair.
# --------------------------------------------------------------------------


def test_repair_restores_the_declared_bytes(repo):
    original = (repo / "run.cmd").read_bytes()
    _drift_to_lf(repo)
    drifts = find_declared_eol_drift(repo, ["run.cmd"])
    assert repair_declared_eol_drift(repo, drifts) == ["run.cmd"]
    assert (repo / "run.cmd").read_bytes() == original
    assert find_declared_eol_drift(repo, ["run.cmd"]) == []


def test_repair_does_not_change_what_a_commit_would_carry(repo):
    """The property that makes repairing safe on a commit path: check-in
    normalization maps drifted and repaired bytes to the SAME blob, so a
    commit taken across the repair carries identical content. K-062 observed
    this by hand ("the corrected working copy hashes identically to the
    index"); it is the whole reason this is a repair and not a refusal.
    """
    before = _git_out(repo, "hash-object", "run.cmd")
    _drift_to_lf(repo)
    drifted = _git_out(repo, "hash-object", "run.cmd")
    repair_declared_eol_drift(repo, find_declared_eol_drift(repo, ["run.cmd"]))
    after = _git_out(repo, "hash-object", "run.cmd")
    assert before == drifted == after


def test_repair_does_not_double_the_carriage_return_on_mixed_input(repo):
    """The classic in-place-rewrite bug: expanding to CRLF without collapsing
    first turns an already-correct line into `\\r\\r\\n`. Mixed endings are the
    input that exposes it."""
    (repo / "run.cmd").write_bytes(b"@echo off\r\necho a\necho b\r\n")
    drifts = find_declared_eol_drift(repo, ["run.cmd"])
    assert drifts, "mixed endings must register as drift"
    repair_declared_eol_drift(repo, drifts)
    body = (repo / "run.cmd").read_bytes()
    assert b"\r\r" not in body
    assert body == b"@echo off\r\necho a\r\necho b\r\n"


def test_repair_of_an_unwritable_file_is_skipped_not_raised(repo, monkeypatch):
    _drift_to_lf(repo)
    drifts = find_declared_eol_drift(repo, ["run.cmd"])

    def deny(*_a, **_k):
        raise OSError("locked")

    monkeypatch.setattr("pathlib.Path.write_bytes", deny)
    assert repair_declared_eol_drift(repo, drifts) == []


def test_suffix_tuple_is_the_executable_classes_only():
    """A widening of this tuple is a scope decision, not a tidy-up -- the
    census that scoped it is cited in the module. Pinned so it cannot drift
    silently."""
    assert EXECUTABLE_SUFFIXES == (".cmd", ".ps1", ".sh", ".bat")
