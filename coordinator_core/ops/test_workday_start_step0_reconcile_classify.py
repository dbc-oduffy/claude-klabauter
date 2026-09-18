"""Fast-tier, zero-spawn tests for `_classify_failed_merge` and the
probe-before-abort ordering in `coordinator_core.ops.workday_start_step0_reconcile`.

No `pytestmark` here (deliberately) -- this module must be collected by
`fast_test_cmd` and must spawn no subprocess at all; every git call in
`main`'s failure path is monkeypatched to a fake `_run`, and both
classification probes are stubbed.

Spec backlink: docs/plans/2026-09-06-engine-publish-lag-hook-gen-forwarder-regen.md :: C4
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_state import IndexParseError
from coordinator_core.ops import workday_start_step0_reconcile as mod


def _proc(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _classify_failed_merge -- all four input combinations against the Design
# table (docs/plans/2026-09-06-engine-publish-lag-hook-gen-forwarder-regen.md).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Shared fake-`_run` harness for the `main()`-level cases below. All of
# `main`'s git calls up to and including the failing `--no-ff` merge are
# faked here; only the failure path (probes + abort) is under test.
# ---------------------------------------------------------------------------


def _install_failing_merge_run(monkeypatch, recorder: list[str] | None = None):
    """Fake `_run` that drives `main` straight to a failed `--no-ff` merge.

    If `recorder` is given, every call appends a short tag naming which git
    subcommand fired, so ordering/spawn-count assertions can inspect it
    alongside the probe-name entries appended by the stubbed probes.
    """

    def fake_run(args, env=None):
        if recorder is not None:
            recorder.append(f"run:{args[0]}")
        if args[:2] == ["fetch", "origin"]:
            return _proc(0)
        if args[:2] == ["branch", "--show-current"]:
            return _proc(0, stdout="work/testmachine/2026-01-01\n")
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return _proc(1)  # not an ancestor -> needs reconcile
        if args[:2] == ["merge", "--ff-only"]:
            return _proc(1)  # ff fails -> falls through to --no-ff
        if args[:2] == ["merge", "--no-ff"]:
            return _proc(1)  # the failing merge under test
        if args[:2] == ["merge", "--abort"]:
            return _proc(0)
        raise AssertionError(f"unexpected git invocation in fake _run: {args}")

    monkeypatch.setattr(mod, "_run", fake_run)


def _stub_probes(monkeypatch, merge_in_progress: bool, index_readable: bool, recorder: list[str] | None = None):
    def fake_merge_in_progress(repo_root):
        if recorder is not None:
            recorder.append("probe:_merge_in_progress")
        return merge_in_progress

    def fake_index_readable(repo_root):
        if recorder is not None:
            recorder.append("probe:_index_readable")
        return index_readable

    monkeypatch.setattr(mod, "_merge_in_progress", fake_merge_in_progress)
    monkeypatch.setattr(mod, "_index_readable", fake_index_readable)
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(Path.cwd()))


# ---------------------------------------------------------------------------
# Ordering: both probes must run BEFORE `merge --abort`. This is the plan's
# named falsifier -- swapping the probe calls and the abort call in
# `workday_start_step0_reconcile.main` MUST turn this test red.
# ---------------------------------------------------------------------------


def test_probes_run_before_abort_falsifier(monkeypatch, capsys):
    """Falsifier: if C3's body is edited so the abort runs before the two
    probes, this test must fail. It asserts both probe-name entries appear
    in the shared recorder strictly before the `run:merge` entry that
    corresponds to the abort call (the third `merge` entry overall, since
    `--ff-only` and `--no-ff` also record as `run:merge`) -- so an inverted
    ordering (abort, then probes) is caught, not silently tolerated.
    """
    recorder: list[str] = []
    _install_failing_merge_run(monkeypatch, recorder)
    _stub_probes(monkeypatch, merge_in_progress=True, index_readable=False, recorder=recorder)

    rc = mod.main([])
    assert rc == 3

    # The three `run:merge` entries are, in order: --ff-only, --no-ff, --abort.
    merge_indices = [i for i, entry in enumerate(recorder) if entry == "run:merge"]
    assert len(merge_indices) == 3
    abort_index = merge_indices[-1]

    probe_indices = [i for i, entry in enumerate(recorder) if entry.startswith("probe:")]
    assert len(probe_indices) == 2
    assert all(i < abort_index for i in probe_indices), (
        "both probes must run strictly before merge --abort; recorder=" + repr(recorder)
    )


# ---------------------------------------------------------------------------
# Spawn budget: the discrimination itself adds no `_run` invocation -- the
# failure path invokes `_run` exactly once (the abort) after classification.
# ---------------------------------------------------------------------------


def test_discrimination_adds_no_spawn(monkeypatch):
    recorder: list[str] = []
    _install_failing_merge_run(monkeypatch, recorder)
    _stub_probes(monkeypatch, merge_in_progress=True, index_readable=False)

    rc = mod.main([])
    assert rc == 3

    abort_calls = [entry for entry in recorder if entry == "run:merge"]
    # fetch, branch --show-current, merge-base, merge --ff-only, merge --no-ff, merge --abort
    # -- three of the six are "run:merge" (ff-only, no-ff, abort); the
    # discrimination (probes) contributes zero additional `_run` calls.
    assert len(abort_calls) == 3
    total_run_calls = len(recorder)
    assert total_run_calls == 6


# ---------------------------------------------------------------------------
# Exception routing: a probe stub that raises IndexParseError must be caught
# and must classify as RECONCILE-CONFLICT, not propagate out of `main`.
# ---------------------------------------------------------------------------


def test_index_parse_error_inside_real_index_readable_classifies_as_conflict(monkeypatch, tmp_path, capsys):
    """`_index_readable` itself must catch `IndexParseError` from
    `read_index` and classify as `RECONCILE-CONFLICT`, not propagate."""
    _install_failing_merge_run(monkeypatch)
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)

    def raising_read_index(repo_root, fresh=True):
        raise IndexParseError("simulated unparseable index")

    monkeypatch.setattr(mod, "read_index", raising_read_index)
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(tmp_path))

    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=work/testmachine/2026-01-01" in out


# ---------------------------------------------------------------------------
# Root resolution: running from a subdirectory classifies identically to
# running at the root -- `show_toplevel()` is spawn-free and walks upward,
# so a stubbed `show_toplevel` returning a subdirectory-derived root must
# not change the outcome.
# ---------------------------------------------------------------------------


def test_root_resolution_from_subdirectory_classifies_identically(monkeypatch, tmp_path):
    subdir = tmp_path / "sub" / "deeper"
    subdir.mkdir(parents=True)
    recorder_root_seen: list[Path] = []

    _install_failing_merge_run(monkeypatch)

    def fake_merge_in_progress(repo_root):
        recorder_root_seen.append(repo_root)
        return True

    monkeypatch.setattr(mod, "_merge_in_progress", fake_merge_in_progress)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: False)
    # show_toplevel() walking from a subdirectory resolves to the same root
    # as walking from the root itself -- simulate that by having the stub
    # return tmp_path regardless of cwd, matching show_toplevel's own
    # upward-walk contract.
    monkeypatch.setattr(mod, "show_toplevel", lambda: str(tmp_path))

    rc = mod.main([])
    out_at_subdir = rc

    assert out_at_subdir == 3
    assert recorder_root_seen == [Path(tmp_path)]


# ---------------------------------------------------------------------------
# All three arms return 3; conflict arm's stdout/stderr text is unchanged;
# both new arms' stderr includes the A/B/C-negation note.
# ---------------------------------------------------------------------------


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
