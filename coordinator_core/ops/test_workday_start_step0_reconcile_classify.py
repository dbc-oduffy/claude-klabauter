from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_state import IndexParseError, UnmergedIndexEntryError, read_index
from coordinator_core.ops import workday_start_step0_reconcile as mod


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.parametrize(
    "merge_in_progress,index_readable,expected",
    [
        (True, False, "RECONCILE-CONFLICT"),
        (True, True, "RECONCILE-MERGE-COMMIT-REFUSED"),
        (False, False, "RECONCILE-MERGE-NOT-STARTED"),
        (False, True, "RECONCILE-MERGE-NOT-STARTED"),
    ],
)
def test_classify_failed_merge_all_four_combinations(merge_in_progress, index_readable, expected):
    assert mod._classify_failed_merge(merge_in_progress, index_readable) == expected


def _install_failing_merge_run(monkeypatch, recorder: list[str] | None = None, staged_stdout: str = ""):

    def fake_run(args, env=None):
        if recorder is not None:
            recorder.append(f"run:{args[0]}")
        if args[:2] == ["fetch", "origin"]:
            return _proc(0)
        if args[:2] == ["branch", "--show-current"]:
            return _proc(0, stdout="work/testmachine/2026-01-01\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _proc(1)
        if args[:2] == ["diff", "--cached"]:
            return _proc(0, stdout=staged_stdout)
        if args[:2] == ["merge", "--ff-only"]:
            return _proc(1)
        if args[:2] == ["merge", "--no-ff"]:
            return _proc(1)
        if args[:2] == ["merge", "--abort"]:
            return _proc(0)
        raise AssertionError(f"unexpected git invocation in fake _run: {args}")

    monkeypatch.setattr(mod, "_run", fake_run)


def _stub_probes(monkeypatch, merge_in_progress: bool, index_readable: bool, recorder: list[str] | None = None):
    def fake_merge_in_progress(repo_root):
        if recorder is not None:
            recorder.append("probe:_merge_in_progress")
        return merge_in_progress

    def fake_index_state(repo_root):
        if recorder is not None:
            recorder.append("probe:_index_state")
        return "readable" if index_readable else "unparseable"

    monkeypatch.setattr(mod, "_merge_in_progress", fake_merge_in_progress)
    monkeypatch.setattr(mod, "_index_state", fake_index_state)
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(Path.cwd()))


def test_probes_run_before_abort_falsifier(monkeypatch, capsys):
    recorder: list[str] = []
    _install_failing_merge_run(monkeypatch, recorder)
    _stub_probes(monkeypatch, merge_in_progress=True, index_readable=False, recorder=recorder)

    rc = mod.main([])
    assert rc == 3

    merge_indices = [i for i, entry in enumerate(recorder) if entry == "run:merge"]
    assert len(merge_indices) == 3
    abort_index = merge_indices[-1]

    probe_indices = [i for i, entry in enumerate(recorder) if entry.startswith("probe:")]
    assert len(probe_indices) == 2
    assert all(i < abort_index for i in probe_indices), (
        "both probes must run strictly before merge --abort; recorder=" + repr(recorder)
    )


def test_discrimination_adds_no_spawn(monkeypatch):
    recorder: list[str] = []
    _install_failing_merge_run(monkeypatch, recorder)
    _stub_probes(monkeypatch, merge_in_progress=True, index_readable=False)

    rc = mod.main([])
    assert rc == 3

    abort_calls = [entry for entry in recorder if entry == "run:merge"]
    assert len(abort_calls) == 3
    total_run_calls = len(recorder)
    assert total_run_calls == 7


def test_staged_read_is_one_zero_flag_spawn_before_the_merges(monkeypatch):
    seen: list[list[str]] = []
    _install_failing_merge_run(monkeypatch)
    inner = mod._run

    def spy(args, env=None):
        seen.append(args)
        return inner(args, env=env)

    monkeypatch.setattr(mod, "_run", spy)
    _stub_probes(monkeypatch, merge_in_progress=True, index_readable=False)
    mod.main([])
    staged = [i for i, a in enumerate(seen) if a[:2] == ["diff", "--cached"]]
    merges = [i for i, a in enumerate(seen) if a[0] == "merge"]
    assert len(staged) == 1
    assert seen[staged[0]] == ["diff", "--cached", "--name-only", "-z"]
    assert staged[0] < merges[0]


def test_non_spool_staged_path_commits_nothing_and_names_it(monkeypatch, capsys):
    recorder: list[str] = []
    _install_failing_merge_run(
        monkeypatch, recorder, staged_stdout="state/block-discharge/a.jsonl\0src/x.py\0"
    )
    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-MERGE-NOT-STARTED branch=" in captured.out
    assert "SPOOL-COMMITTED" not in captured.out
    assert captured.err.split("STAGED-PATHS\n", 1)[1].splitlines()[:2] == [
        "state/block-discharge/a.jsonl",
        "src/x.py",
    ]
    assert "run:merge" not in recorder


# ---------------------------------------------------------------------------
# Exception routing: a probe stub that raises IndexParseError must be caught
# and must classify as RECONCILE-CONFLICT, not propagate out of `main`.
# ---------------------------------------------------------------------------


def test_index_parse_error_inside_real_index_readable_classifies_as_conflict(monkeypatch, tmp_path, capsys):
    """`_index_state` itself must catch `IndexParseError` from
    `read_index` and classify as `RECONCILE-CONFLICT`, not propagate; an
    unparseable index still prints the splitIndex caveat."""
    _install_failing_merge_run(monkeypatch)
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)

    def raising_read_index(repo_root, fresh=True):
        raise IndexParseError("simulated unparseable index")

    monkeypatch.setattr(mod, "read_index", raising_read_index)
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(tmp_path))

    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=work/testmachine/2026-01-01" in captured.out
    assert "core.splitIndex" in captured.err


def test_unmerged_entry_conflict_prints_no_splitindex_caveat(monkeypatch, tmp_path, capsys):
    _install_failing_merge_run(monkeypatch)
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)

    def raising_read_index(repo_root, fresh=True):
        raise UnmergedIndexEntryError("entry 'a' is unmerged (stage 2)")

    monkeypatch.setattr(mod, "read_index", raising_read_index)
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(tmp_path))

    rc = mod.main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=" in captured.out
    assert "Branch Reconciliation Decision" in captured.err
    assert "core.splitIndex" not in captured.err


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_real_conflicted_repo_raises_unmerged_subclass(tmp_path):
    def git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("base\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "other")
    (tmp_path / "f.txt").write_text("other\n")
    git("commit", "-q", "-am", "other")
    git("checkout", "-q", "main")
    (tmp_path / "f.txt").write_text("main\n")
    git("commit", "-q", "-am", "main")
    assert git("merge", "other", check=False).returncode != 0

    with pytest.raises(UnmergedIndexEntryError):
        read_index(tmp_path, fresh=True)
    assert mod._index_state(tmp_path) == "unmerged"


def test_root_resolution_from_subdirectory_classifies_identically(monkeypatch, tmp_path):
    subdir = tmp_path / "sub" / "deeper"
    subdir.mkdir(parents=True)
    recorder_root_seen: list[Path] = []

    _install_failing_merge_run(monkeypatch)

    def fake_merge_in_progress(repo_root):
        recorder_root_seen.append(repo_root)
        return True

    monkeypatch.setattr(mod, "_merge_in_progress", fake_merge_in_progress)
    monkeypatch.setattr(mod, "_index_state", lambda repo_root: "unparseable")
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(tmp_path))

    rc = mod.main([])
    out_at_subdir = rc

    assert out_at_subdir == 3
    assert recorder_root_seen == [Path(tmp_path)]


@pytest.mark.parametrize(
    "merge_in_progress,index_readable,expected_outcome,expects_abc_negation",
    [
        (True, False, "RECONCILE-CONFLICT", False),
        (True, True, "RECONCILE-MERGE-COMMIT-REFUSED", True),
        (False, False, "RECONCILE-MERGE-NOT-STARTED", True),
    ],
)
def test_all_three_arms_exit_3_with_expected_text(
    monkeypatch, capsys, merge_in_progress, index_readable, expected_outcome, expects_abc_negation
):
    _install_failing_merge_run(monkeypatch)
    _stub_probes(monkeypatch, merge_in_progress=merge_in_progress, index_readable=index_readable)

    rc = mod.main([])
    captured = capsys.readouterr()

    assert rc == 3
    assert f"{expected_outcome} branch=work/testmachine/2026-01-01" in captured.out

    if expected_outcome == "RECONCILE-CONFLICT":
        assert "Branch Reconciliation Decision" in captured.err
    if expects_abc_negation:
        assert "not the A/B/C" in captured.err or "not an A/B/C" in captured.err
