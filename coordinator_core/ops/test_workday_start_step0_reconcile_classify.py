"""Fast-tier, zero-spawn tests for
coordinator_core.ops.workday_start_step0_reconcile's failed-merge classifier.

Purpose: pins the `_classify_failed_merge` mapping and `main`'s probe-before-
abort ordering with `_run` and the two probes monkeypatched — no fixture
repo, no `git` spawn, collected by `fast_test_cmd`.

NO `pytestmark` here deliberately — this module must spawn nothing and be
collected by the fast tier, unlike its `cadence`-marked sibling
`test_workday_start_step0_reconcile.py`.

Spec: docs/plans/2026-09-06-engine-publish-lag-hook-gen-forwarder-regen.md (C4)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.git_state import IndexParseError
from coordinator_core.ops import workday_start_step0_reconcile as mod
from coordinator_core.ops.workday_start_step0_reconcile import (
    _classify_failed_merge,
    main,
)


@pytest.mark.parametrize(
    "merge_in_progress,index_readable,expected",
    [
        (True, False, "RECONCILE-CONFLICT"),
        (True, True, "RECONCILE-MERGE-COMMIT-REFUSED"),
        (False, True, "RECONCILE-MERGE-NOT-STARTED"),
        (False, False, "RECONCILE-MERGE-NOT-STARTED"),
    ],
)
def test_classify_failed_merge_design_table(merge_in_progress, index_readable, expected):
    assert _classify_failed_merge(merge_in_progress, index_readable) == expected


def _completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


def _wire_up_to_failed_no_ff(monkeypatch, calls, *, current="work/x/2026-01-01", order=None):
    """Monkeypatch `_run` to walk `main` through fetch/show-current/is-
    ancestor/ff-merge (all succeeding-up-to-the-point-of-a-failed --no-ff
    merge) while recording every call's argv[0] (post-"git") onto `calls`.
    If `order` is given, an `"abort"` entry is appended to it when the
    `git merge --abort` call fires, so a caller sharing `order` with the two
    probe monkeypatches can assert relative ordering across both `_run` and
    the probes.
    """

    def _run(args, env=None):
        calls.append(list(args))
        if args[:1] == ["fetch"]:
            return _completed(0)
        if args[:2] == ["branch", "--show-current"]:
            return _completed(0, stdout=f"{current}\n")
        if args[:1] == ["merge-base"]:
            return _completed(1)  # not an ancestor -> needs reconcile
        if args[:2] == ["merge", "--ff-only"]:
            return _completed(1)  # ff fails -> falls through to --no-ff
        if args[:2] == ["merge", "--no-ff"]:
            return _completed(1)  # conflict/refusal/not-started
        if args[:2] == ["merge", "--abort"]:
            if order is not None:
                order.append("abort")
            return _completed(0)
        raise AssertionError(f"unexpected _run call: {args}")

    monkeypatch.setattr(mod, "_run", _run)


def test_ordering_probes_run_strictly_before_abort(monkeypatch, capsys):
    """This plan's named falsifier (AC8): must FAIL if the two probes are
    moved after the abort in `main`'s body — i.e. swapping the probe reads
    and the `_run(["merge", "--abort"])` call in the source must turn this
    test red."""
    order: list[str] = []
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls, order=order)

    def _merge_in_progress(repo_root):
        order.append("_merge_in_progress")
        return True

    def _index_readable(repo_root):
        order.append("_index_readable")
        return False

    monkeypatch.setattr(mod, "_merge_in_progress", _merge_in_progress)
    monkeypatch.setattr(mod, "_index_readable", _index_readable)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")

    rc = main([])
    assert rc == 3

    # `order` interleaves both probes AND the `merge --abort` `_run` call
    # (appended by `_wire_up_to_failed_no_ff` itself) in the sequence `main`
    # actually invoked them in — this is the falsifier: swapping the probe
    # reads and the abort call in `main`'s source reorders this list and
    # turns this assertion red.
    assert order == ["_merge_in_progress", "_index_readable", "abort"]


def test_failure_path_spawns_run_exactly_once_the_abort(monkeypatch):
    """AC7/AC8 spawn budget: the discrimination itself (probes) issues no
    `_run` call — only the unconditional `git merge --abort` does, exactly
    once, on the failure path."""
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: False)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")

    rc = main([])
    assert rc == 3

    abort_calls = [c for c in calls if c[:2] == ["merge", "--abort"]]
    assert len(abort_calls) == 1


def test_index_parse_error_is_caught_and_classifies_as_conflict(monkeypatch, capsys):
    """AC3b: a probe stub raising `coordinator_core.git.git_state.
    IndexParseError` must be caught inside `_index_readable` and must
    classify (RECONCILE-CONFLICT — the conservative arm), never propagate
    out of `main`."""
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)

    def _raising_read_index(repo, *, fresh=False):
        raise IndexParseError("malformed index")

    monkeypatch.setattr(mod, "read_index", _raising_read_index)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")

    rc = main([])
    out = capsys.readouterr().out
    assert rc == 3
    assert "RECONCILE-CONFLICT" in out


def test_root_resolution_from_subdirectory_matches_root(monkeypatch):
    """AC3a: both probes resolve against the walked repo root
    (`show_toplevel()`), not `Path.cwd()` directly — classification from a
    subdirectory must be identical to running at the root."""
    seen_roots: list[str] = []
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)

    def _merge_in_progress(repo_root):
        seen_roots.append(str(repo_root))
        return True

    monkeypatch.setattr(mod, "_merge_in_progress", _merge_in_progress)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: False)
    # show_toplevel() is walked regardless of cwd -- simulate it resolving
    # the repo root even though `main` never spawns a process to get it.
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo/root")

    rc = main([])
    assert rc == 3
    assert seen_roots == [str(Path("/repo/root"))]


def test_all_three_arms_return_3_with_unchanged_conflict_text(monkeypatch, capsys):
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")

    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: False)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-CONFLICT branch=work/x/2026-01-01" in captured.out
    assert "Branch Reconciliation Decision" in captured.err


def test_commit_refused_arm_negates_abc_route(monkeypatch, capsys):
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: True)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: True)

    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-MERGE-COMMIT-REFUSED branch=work/x/2026-01-01" in captured.out
    assert "NOT the A/B/C" in captured.err


def test_not_started_arm_negates_abc_route(monkeypatch, capsys):
    calls: list[list[str]] = []
    _wire_up_to_failed_no_ff(monkeypatch, calls)
    monkeypatch.setattr(mod, "show_toplevel", lambda: "/repo")
    monkeypatch.setattr(mod, "_merge_in_progress", lambda repo_root: False)
    monkeypatch.setattr(mod, "_index_readable", lambda repo_root: True)

    rc = main([])
    captured = capsys.readouterr()
    assert rc == 3
    assert "RECONCILE-MERGE-NOT-STARTED branch=work/x/2026-01-01" in captured.out
    assert "NOT the A/B/C" in captured.err
