"""coordinator_core.source_edit_gate.gate -- base-vs-candidate test-regression gate.

`run_gate(repo_root, edited_files, *, restore_originals=..., reapply_stripped=...)`
answers "did this edit make any test newly fail?" for a repo whose caller has
already applied a source edit to `repo_root` before calling in.

The gate owns its own test type -- a computed "source-edit tier" -- and needs
NO per-repo declaration. It:

  1. Detects a runner PER SELECTED TEST FILE (`runner.find_runner_root`),
     walking up from each file's own directory to the nearest pytest
     (pyproject.toml `[tool.pytest.ini_options]`, `pytest.ini`, or
     `conftest.py`) or vitest (`package.json` dependency) marker -- a
     monorepo subpackage's own config, not just `repo_root`'s. Selected files
     are grouped by `(runner, root_dir)` and each group runs from its own
     root with its own config. Any file with no detectable runner anywhere
     between it and `repo_root` -> `verdict="indeterminate"` for the whole
     gate call (an edited file with no detectable runner is refused, not
     silently passed).
  2. Selects the test files that could regress (`selection.select_test_files`)
     from `edited_files` (repo-relative paths of files the caller already
     wrote), computed against the CANDIDATE tree (the edit is already on
     disk, but selection only reads import graphs and string literals, which
     is exactly what the edit might have changed -- see the trade-off note in
     `selection.py`; this module recomputes selection identically before and
     after restore is not needed because the file SET being edited, not test
     content, drives selection, and `edited_files` is fixed for the whole
     call). Zero selected candidates -> nothing can regress for this edit ->
     `verdict="pass"` immediately (no spawn on either side). An optional
     `file_bytes` mapping (`edited_rel -> (original_bytes, edited_bytes)`)
     drives `selection.classify_edit`'s comment-only narrowing -- see
     `selection.py`'s module docstring; omitted, every edited file
     classifies as `"code"` (the pre-existing, import-rule-applying
     behaviour), so a caller that cannot supply both byte versions loses
     nothing it had before.
  3. Runs the CANDIDATE side (`runner.run_selected`) against the already-
     edited tree.
  4. Calls `restore_originals()` -- the caller's callback that puts the tree
     back to its pre-edit bytes.
  5. Runs the SAME selected files again -- the BASE run.
  6. Diffs the two structured reports for newly-broken node ids (see
     `_new_failures`).
  6a. A non-empty diff is NOT taken at face value -- `_confirm_new_failures`
      re-runs the same groups SERIALLY (no xdist) on both candidate and base
      bytes before condemning an edit, because a parallel-run new-failure can
      be xdist worker-boundary flakiness rather than a real regression. Only
      an id that fails serially on candidate AND passes serially on base
      counts as a confirmed regression; one that passes serially on
      candidate is recorded as `unconfirmed` (flaky-under-parallel) and does
      NOT fail the gate; one that fails on both sides is pre-existing and is
      silently dropped, not counted at all.
  7. On a clean diff, or a diff with no CONFIRMED regression
     (`verdict="pass"`), calls `reapply_stripped()`. On a CONFIRMED
     `"fail"`, or `"indeterminate"`, it does NOT -- the tree stays at the
     restored (pre-edit) bytes.

Base and candidate deliberately share ONE real tree (no scratch clone, no
`git worktree`/`git archive`/`git stash`) -- a clone base is not environment-
symmetric with the real-tree candidate run (sibling checkouts, gitignored
`.venv`/`node_modules`, LFS content and submodules would be base-only-absent).

Command execution never uses `shell=True`; interpreter resolution is
`runner.py`'s job (`sys.executable` for pytest, `shutil.which` for vitest) --
this module never resolves or spawns anything itself, and never scrapes
stdout/stderr text for pass/fail signal, only the two structured report
formats `runner.py` parses.

Negative-spec:
  - Does NOT clone, `git worktree`, `git archive`, or `git stash`.
  - Does NOT read or write `coordinator.local.md` -- there is no
    `source_edit_gate_cmd`/`source_edit_gate_report_format` declaration path;
    it was deleted, with no fallback and no compat shim.
  - Does NOT retry a CLEAN (no-new-failure) run for flakiness -- each side
    runs exactly once in the parallel pass. A non-empty diff DOES get one
    serial confirmation re-run per side (see step 6a) before it can fail the
    gate; the confirmation itself is never retried a third time.
  - Does NOT decide what to do with a `"fail"`/`"indeterminate"` verdict
    beyond leaving the tree at restored bytes.

Spec backlink: docs/plans/2026-09-27-source-edit-test-guardrail.md (gate
redesign -- computed tier, no per-repo declaration).
"""

from __future__ import annotations

import posixpath
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from coordinator_core.win_portability import no_console_creationflags

from .gate_report import TestReport
from .runner import detect_runner, find_runner_root, run_selected
from .selection import select_test_files

__all__ = ["GateResult", "run_gate"]


@dataclass(frozen=True)
class GateResult:
    new_failures: tuple = ()
    verdict: str = "indeterminate"  # "pass" | "fail" | "indeterminate"
    unconfirmed: tuple = ()
    # Machine-readable cause for `verdict="indeterminate"` -- one of
    # "no-runner-marker" (repo_root itself has no detectable pytest/vitest
    # marker), "no-runner-for-selected-file" (a selected file has no
    # detectable runner anywhere between it and repo_root -- `detail` carries
    # the offending repo-relative paths), "candidate-run-crashed-or-empty"
    # (the candidate-side run crashed, produced an unparseable report, or
    # reported zero collected tests for a non-empty selection), or
    # "base-run-crashed-or-empty" (same, but the base-side run). `None` for
    # every non-indeterminate verdict -- a `"pass"`/`"fail"` never needs one.
    reason: str | None = None
    # Free-form detail for `reason` (e.g. the group `(runner, root)` keys or
    # file list that triggered it) -- always a tuple, never raises on an
    # unpicklable/unhashable payload since callers only ever pass plain
    # strings/tuples of strings.
    detail: tuple = ()


def _new_failures(base: TestReport, candidate: TestReport) -> tuple:
    """Regression rule: a node id newly failing at candidate, OR present and
    passing/skippable at base and vanished (absent from candidate) at candidate, OR
    present and PASSING at base and turned into a skip at candidate, counts as a
    regression. A node id already failing at base staying failed at candidate is NOT
    new (the base-red/candidate-red subset case), and a node id already skipped at
    base staying skipped at candidate is NOT new (no change happened).

    The "vanished" and "turned into a skip" halves are deliberately asymmetric in which
    base statuses they cover: vanishing entirely (the source edit deleted the test, or
    the run crashed partway) is suspicious regardless of whether the test was passing
    or already skipped at base, so both are checked. Turning into a skip is only
    suspicious when it represents a REGRESSION from an actual pass -- a skip staying a
    skip is a no-op, not a new failure mode, so only base-`passed` is checked there."""

    new: set = set()
    for node_id, cand_status in candidate.items():
        base_status = base.get(node_id)
        if cand_status == "failed" and base_status != "failed":
            new.add(node_id)
    for node_id, base_status in base.items():
        if base_status == "failed":
            continue
        cand_status = candidate.get(node_id)
        if cand_status is None:
            new.add(node_id)  # vanished -- regression regardless of base passed/skipped
        elif base_status == "passed" and cand_status == "skipped":
            new.add(node_id)  # passed -> skip is a regression; skip -> skip is not
    return tuple(sorted(new))


def _list_tracked_files(repo_root: str) -> list:
    out = subprocess.run(
        ["git", "-C", repo_root, "ls-files"],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return [f for f in out.stdout.splitlines() if f]


def _root_repo_relative(repo_root: str, root: str) -> str:
    rr = Path(repo_root).resolve()
    r = Path(root).resolve()
    if r == rr:
        return ""
    return r.relative_to(rr).as_posix()


def _rel_to_root(test_rel: str, root_repo_rel: str) -> str:
    if not root_repo_rel:
        return test_rel
    return posixpath.relpath(test_rel, root_repo_rel)


def _group_selected_by_runner_root(repo_root: str, selected: list) -> dict:
    """Groups selected (repo-relative) test files by the nearest
    `(runner, root_dir)` each resolves to, per `runner.find_runner_root`."""
    groups: dict = {}
    for test_rel in selected:
        runner, root = find_runner_root(repo_root, test_rel)
        groups.setdefault((runner, root), []).append(test_rel)
    return groups


def _run_groups(repo_root: str, groups: dict, *, parallel: bool = True) -> tuple:
    """Runs each `(runner, root)` group from its own root with its own
    config, merging structured reports into one `TestReport` keyed by
    `<group-root>::<node_id>` (namespaced so two subpackages' identically-named
    node ids never collide when merged). Returns `(merged_report, ok)`;
    `ok=False` means at least one group crashed, produced no parseable
    report, or -- for a non-empty selection -- reported zero collected tests,
    which is indistinguishable from a mis-rooted invocation that verified
    nothing and MUST NOT be read as a pass.

    `parallel=False` (the confirmation re-run, see `_confirm_new_failures`)
    forces every group serial (no xdist) -- each group still uses its own
    caller-owned basetemp via `run_selected`, unchanged."""
    merged: TestReport = {}
    for (runner, root), test_files in groups.items():
        root_repo_rel = _root_repo_relative(repo_root, root)
        rel_files = [_rel_to_root(f, root_repo_rel) for f in test_files]
        report = run_selected(root, rel_files, runner, parallel=parallel)
        if report is None:
            return {}, False
        if rel_files and not report:
            # Non-empty selection, zero collected -- indeterminate, never an
            # empty pass (a mis-rooted config can silently under-collect).
            return {}, False
        prefix = root_repo_rel or "."
        for node_id, status in report.items():
            merged[f"{prefix}::{node_id}"] = status
    return merged, True


def _file_texts_from_bytes(file_bytes: dict | None) -> dict | None:
    """Decodes an optional `edited_rel -> (original_bytes, edited_bytes)`
    mapping into the `edited_rel -> (original_text, edited_text)` shape
    `selection.select_test_files` wants. A file whose bytes don't decode as
    UTF-8 on either side is simply omitted -- `classify_edit`'s own
    `file_texts`-absent default (`"code"`) covers it, never a hard failure."""
    if not file_bytes:
        return None
    file_texts: dict = {}
    for rel, (orig, edited) in file_bytes.items():
        try:
            file_texts[rel] = (orig.decode("utf-8"), edited.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return file_texts


def _confirm_new_failures(
    repo_root: str,
    groups: dict,
    candidate_ids: tuple,
    *,
    restore_originals: Callable[[], None],
    reapply_stripped: Callable[[], None],
) -> tuple:
    """Confirms each parallel-run new-failure id against a SERIAL (no xdist)
    re-run on both sides before letting it fail the gate -- xdist's own
    worker-boundary flakiness (a test that only misbehaves under parallel
    scheduling) must not discard a valid edit.

    Re-applies the candidate bytes, reruns `groups` serially, restores the
    base bytes, reruns `groups` serially again, and classifies each
    `candidate_ids` entry against those two confirmation reports:

      - candidate confirm `"failed"` AND base confirm `"passed"` -> a
        confirmed regression.
      - candidate confirm `"passed"` (it passed when re-run serially) ->
        `"unconfirmed"` (flaky-under-parallel), not a regression.
      - candidate confirm `"failed"` AND base confirm NOT `"passed"` (already
        failing/absent at base too) -> pre-existing, silently dropped -- not
        a regression, not `"unconfirmed"`.
      - candidate confirm status ABSENT (not even collected in the
        confirmation run -- e.g. a "vanished" id surfaced by the parallel
        diff that was never a real candidate-side failure) -> dropped
        silently, same as pre-existing -- absence is not evidence the id
        "passed when re-run serially".

    Ends with the tree at BASE bytes (the last call here is always
    `restore_originals()`) regardless of outcome -- the caller decides
    whether to call `reapply_stripped()` from there.

    A crash/unparseable confirmation run on either side is NOT re-litigated
    with a THIRD run -- it forfeits the benefit of the doubt and every
    candidate id is treated as confirmed, matching the gate's pre-existing
    fail-closed posture for an indeterminate rerun."""
    reapply_stripped()
    candidate_confirm, candidate_ok = _run_groups(repo_root, groups, parallel=False)
    restore_originals()
    base_confirm, base_ok = _run_groups(repo_root, groups, parallel=False)

    if not candidate_ok or not base_ok:
        return tuple(candidate_ids), ()

    confirmed: set = set()
    unconfirmed: set = set()
    for node_id in candidate_ids:
        cand_status = candidate_confirm.get(node_id)
        base_status = base_confirm.get(node_id)
        if cand_status == "failed" and base_status == "passed":
            confirmed.add(node_id)
        elif cand_status == "passed":
            unconfirmed.add(node_id)
        # else: failed on both sides (pre-existing), or the id wasn't even
        # collected in the confirmation run at all (`cand_status is None` --
        # e.g. a "vanished" id from the parallel diff that was never a real
        # candidate-side failure to begin with) -- dropped silently, not
        # `"unconfirmed"`: that label means "passed when re-run serially",
        # which an absent status is not evidence of either way.
    return tuple(sorted(confirmed)), tuple(sorted(unconfirmed))


def run_gate(
    repo_root: str,
    edited_files: list,
    *,
    restore_originals: Callable[[], None],
    reapply_stripped: Callable[[], None],
    file_bytes: dict | None = None,
) -> GateResult:
    if detect_runner(repo_root) is None:
        # Coarse readiness gate: repo_root itself carries no recognized-runner
        # marker at all -- refused outright, without a git spawn, matching
        # the pre-monorepo contract. A repo_root marker existing is enough to
        # pass this gate; which nearest marker actually RUNS each selected
        # file is still resolved per-file below (a subpackage's own config
        # can and does win over repo_root's).
        restore_originals()
        return GateResult(verdict="indeterminate", reason="no-runner-marker", detail=(repo_root,))

    all_files = _list_tracked_files(repo_root)
    file_texts = _file_texts_from_bytes(file_bytes)
    selected = select_test_files(repo_root, list(edited_files), all_files, file_texts=file_texts)
    if not selected:
        # Nothing importable/mentionable was found for this edit -- nothing
        # can regress, so there is nothing to run on either side.
        reapply_stripped()
        return GateResult(new_failures=(), verdict="pass")

    groups = _group_selected_by_runner_root(repo_root, selected)
    if any(runner is None for (runner, _root) in groups):
        restore_originals()
        no_runner_files = tuple(
            f for (runner, _root), files in groups.items() if runner is None for f in files
        )
        return GateResult(
            verdict="indeterminate", reason="no-runner-for-selected-file", detail=no_runner_files,
        )

    candidate_report, candidate_ok = _run_groups(repo_root, groups)
    restore_originals()
    if not candidate_ok:
        return GateResult(
            verdict="indeterminate",
            reason="candidate-run-crashed-or-empty",
            detail=tuple(f"{runner}:{root}" for (runner, root) in groups),
        )

    base_report, base_ok = _run_groups(repo_root, groups)
    if not base_ok:
        # Either side crashed, produced no parseable report, or reported zero
        # collected tests for a non-empty selection -- nothing to diff, and
        # treating that as "0 tests, nothing changed" would silently pass a
        # run that never actually verified anything. `restore_originals()`
        # already ran above; no `reapply_stripped()` call.
        return GateResult(
            verdict="indeterminate",
            reason="base-run-crashed-or-empty",
            detail=tuple(f"{runner}:{root}" for (runner, root) in groups),
        )

    new_failures = _new_failures(base_report, candidate_report)
    if new_failures:
        confirmed, unconfirmed = _confirm_new_failures(
            repo_root,
            groups,
            new_failures,
            restore_originals=restore_originals,
            reapply_stripped=reapply_stripped,
        )
        if confirmed:
            # Tree is already at base bytes -- `_confirm_new_failures`'s last
            # call is always `restore_originals()`.
            return GateResult(new_failures=confirmed, verdict="fail", unconfirmed=unconfirmed)
        # No confirmed regression -- a parallel-only flake, or a false
        # positive from the diff. Tree is at base bytes; reapply candidate.
        reapply_stripped()
        return GateResult(new_failures=(), verdict="pass", unconfirmed=unconfirmed)

    reapply_stripped()
    return GateResult(new_failures=(), verdict="pass")
