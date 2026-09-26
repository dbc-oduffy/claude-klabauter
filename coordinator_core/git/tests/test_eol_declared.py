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
    target = repo / name
    target.write_bytes(target.read_bytes().replace(b"\r\n", b"\n"))


def test_executable_paths_selects_only_the_declared_classes():
    picked = executable_paths(
        [
            "a.cmd",
            "b.ps1",
            "c.sh",
            "d.bat",
            "e.py",
            "f.md",
            "g.diff",
            "h.patch",
            "state/review-slices/x.diff",
            "state/subagent-share/y.patch",
            "z.sha",
        ]
    )
    assert picked == ["a.cmd", "b.ps1", "c.sh", "d.bat"]


def test_executable_paths_is_case_insensitive_and_deduplicates():
    assert executable_paths(["A.CMD", "A.CMD", "b.Ps1"]) == ["A.CMD", "b.Ps1"]


def test_no_executable_in_the_commit_spawns_nothing(repo, monkeypatch):
    import coordinator_core.git.eol_declared as mod

    def explode(*_a, **_k):  # pragma: no cover - must never run
        raise AssertionError("spawned git for a commit with no executable path")

    monkeypatch.setattr(mod, "run_git", explode)
    assert find_declared_eol_drift(repo, ["notes.md", "a.py"]) == []


def test_one_spawn_for_many_executables(repo, monkeypatch):
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


def test_the_drift_git_cannot_show_you_is_detected(repo):
    _drift_to_lf(repo)
    drifts = find_declared_eol_drift(repo, ["run.cmd"])
    assert drifts == [Drift(path="run.cmd", declared="crlf", on_disk="lf")]


def test_git_diff_stays_empty_for_the_drift_this_module_reports(repo):
    _drift_to_lf(repo)
    assert _git_out(repo, "diff", "--stat").strip() == ""
    assert find_declared_eol_drift(repo, ["run.cmd"])


def test_a_correct_launcher_is_not_a_finding(repo):
    assert find_declared_eol_drift(repo, ["run.cmd"]) == []


def test_an_undeclared_path_is_not_a_finding(tmp_path):
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
    (repo / "tool.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    _git(repo, "add", "tool.sh")
    _git(repo, "commit", "-qm", "add sh")
    (repo / "tool.sh").write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    assert find_declared_eol_drift(repo, ["tool.sh"]) == [
        Drift(path="tool.sh", declared="lf", on_disk="crlf")
    ]


def test_untracked_path_folds_to_no_finding(repo):
    assert find_declared_eol_drift(repo, ["never-added.cmd"]) == []


def test_reads_stdout_bytes_not_stdout(repo, monkeypatch):
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


def test_repair_restores_the_declared_bytes(repo):
    original = (repo / "run.cmd").read_bytes()
    _drift_to_lf(repo)
    drifts = find_declared_eol_drift(repo, ["run.cmd"])
    assert repair_declared_eol_drift(repo, drifts) == ["run.cmd"]
    assert (repo / "run.cmd").read_bytes() == original
    assert find_declared_eol_drift(repo, ["run.cmd"]) == []


def test_repair_does_not_change_what_a_commit_would_carry(repo):
    before = _git_out(repo, "hash-object", "run.cmd")
    _drift_to_lf(repo)
    drifted = _git_out(repo, "hash-object", "run.cmd")
    repair_declared_eol_drift(repo, find_declared_eol_drift(repo, ["run.cmd"]))
    after = _git_out(repo, "hash-object", "run.cmd")
    assert before == drifted == after


def test_repair_does_not_double_the_carriage_return_on_mixed_input(repo):
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
    assert EXECUTABLE_SUFFIXES == (".cmd", ".ps1", ".sh", ".bat")


def test_binary_autodetected_file_is_not_treated_as_drift(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitattributes").write_bytes(b"*.cmd text=auto eol=crlf\n")
    (root / "run.cmd").write_bytes(b"\x00binary\x00payload\x00\n")
    _git(root, "add", ".gitattributes", "run.cmd")
    _git(root, "commit", "-qm", "init")

    eol_field = _git_out(root, "ls-files", "--eol", "run.cmd")
    assert "w/-text" in eol_field, "fixture must actually elicit -text from git"

    assert find_declared_eol_drift(root, ["run.cmd"]) == []


def test_a_tracked_symlink_is_never_repaired(repo):
    link = repo / "launch.cmd"
    target = repo / "outside.txt"
    target.write_bytes(b"do not touch")
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable (needs Developer Mode/elevation on Windows)")

    _git(repo, "add", "launch.cmd")
    _git(repo, "commit", "-qm", "add symlinked launcher")

    drift = Drift(path="launch.cmd", declared="crlf", on_disk="lf")
    assert repair_declared_eol_drift(repo, [drift]) == []
    assert target.read_bytes() == b"do not touch"


def test_record_regex_matches_a_blank_i_or_w_field():
    from coordinator_core.git.eol_declared import _RECORD

    record = "i/ w/lf attr/text eol=lf\tsome/path.sh"
    match = _RECORD.match(record)
    assert match is not None
    assert match.groups() == ("", "lf", "text eol=lf", "some/path.sh")

    record_blank_w = "i/lf w/ attr/text eol=lf\tsome/path.sh"
    match2 = _RECORD.match(record_blank_w)
    assert match2 is not None
    assert match2.groups() == ("lf", "", "text eol=lf", "some/path.sh")
