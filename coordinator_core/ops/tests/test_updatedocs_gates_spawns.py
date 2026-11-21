"""
coordinator_core.ops.tests.test_updatedocs_gates

Tests for the "updatedocs.gates" op and its gate battery
(coordinator_core.ops.updatedocs_gates).

Import guard: coordinator_core.ops.updatedocs_gates MUST be imported at module
load time so @register_op("updatedocs.gates") fires and populates _REGISTRY.

Coverage:
  (a) registry assertion — op name present in _REGISTRY after import
  (b) GateVerdict.UNAVAILABLE fires (not CLEAN) when an external CLI is absent
      — the core Pattern-A distinction this op exists to preserve
  (c) 11g-plugin-wiki: exit 0 with a nonzero "missing-bundled" count in stdout
      produces CONTRADICTION, never CLEAN — the memo's own worked example
  (d) 11h-skill-anchor-links: exit 1 (dead anchors) vs exit 2 (could not
      check) never collapse into the same verdict
  (e) rollup() is a pure function — severity determines "halt", not gate id
  (f) fresh-scaffold-probe fires on the 3-axis-AND fresh repo, not otherwise
  (g) distill-threshold fire/no-fire arithmetic preserved from the ported
      update-docs-probes.py logic
  (h) end-to-end: the "updatedocs.gates" op dispatch returns a verdict array
      + rollup for a `params["gates"]` subset
  (i) unknown gate id in params["gates"] raises ValueError (no silent skip)

Spec backlink: cross-repo/inbox/2026-08-06-coordinator-content-repo-em-updatedocs-gates-
  structured-verdicts.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — fires @register_op("updatedocs.gates") as a side-effect.
# ---------------------------------------------------------------------------
import coordinator_core.ops.updatedocs_gates as udg  # noqa: F401


pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# (a) registry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (b) UNAVAILABLE vs CLEAN — a missing CLI must never read as "clean"
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (c) 11g-plugin-wiki — CONTRADICTION, not CLEAN, on exit 0 + nonzero warnings
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (d) 11h-skill-anchor-links — exit 1 vs exit 2 never collapse
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (e) rollup() — pure function over the verdict array
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (f) fresh-scaffold-probe — 3-axis AND
# ---------------------------------------------------------------------------


def _git_repo_with_tasks(tmp_path, files, ignore_tasks: bool = False):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    if ignore_tasks:
        (tmp_path / ".gitignore").write_text("tasks/\n")
    return tmp_path / "tasks"


def test_tasks_markdown_found_when_untracked_nested_one_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tasks = _git_repo_with_tasks(tmp_path, ["tasks/a/b/note.md"])
    calls = []
    real = udg.run_git

    def counting(argv, *a, **k):
        calls.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(udg, "run_git", counting)
    assert udg._tasks_holds_markdown(tmp_path, tasks) is True
    assert len(calls) == 1 and "ls-files" in calls[0]


def test_tasks_markdown_found_when_gitignored(tmp_path: Path) -> None:
    tasks = _git_repo_with_tasks(tmp_path, ["tasks/note.md"], ignore_tasks=True)
    assert udg._tasks_holds_markdown(tmp_path, tasks) is True


def test_tasks_markdown_absent_when_only_other_files(tmp_path: Path) -> None:
    tasks = _git_repo_with_tasks(tmp_path, ["tasks/a/log.txt", "tasks/b/data.json"])
    assert udg._tasks_holds_markdown(tmp_path, tasks) is False


# ---------------------------------------------------------------------------
# (g) distill-threshold arithmetic
# ---------------------------------------------------------------------------


_ADMISSIBLE_BODY = "weighty body\n" * 1000


def _cite_in_daily_log(root, paths) -> None:
    """An unharvested daily log naming ``paths``: the citation that admits a plan."""
    log = root / "archive" / "daily-summaries" / "2026-11-02.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(f"- shipped {p}\n" for p in paths), encoding="utf-8")


def _implemented_plans(root, n, *, status: str = "implemented") -> None:
    """``n`` plans heavy enough to clear the weight floor, all cited by a daily log."""
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (plans / f"2026-11-01-p{i}.md").write_text(
            f"---\nstatus: {status}\n---\n{_ADMISSIBLE_BODY}", encoding="utf-8"
        )
    if status == "implemented":
        _cite_in_daily_log(root, [f"docs/plans/2026-11-01-p{i}.md" for i in range(n)])


def _empty_log(root) -> None:
    log = root / "state" / "distillation-log.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("# Columns: run | path | disposition | fate\n", encoding="utf-8")


def _committed_repo_with_plans(root: Path, monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    from coordinator_core import run_watermark
    from coordinator_core.win_portability import no_console_passthrough_kwargs

    monkeypatch.setattr(run_watermark, "registry_get", lambda _key: None)
    _empty_log(root)
    _implemented_plans(root, n)
    for argv in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "-m", "seed"],
    ):
        subprocess.run(["git", *argv], cwd=root, check=True, **no_console_passthrough_kwargs())


def _drain_distill_runs(root: Path) -> None:
    from coordinator_core import run_watermark
    from coordinator_core.ops.distill_scope import PM_RULING_2026_08_06_COHORT_SPECS, compute_scope

    for i in range(10):
        result = compute_scope(
            root, run_id=f"drain-{i}", cohort_specs=list(PM_RULING_2026_08_06_COHORT_SPECS), admit=True
        )
        run_dir = root / "state" / "scratch" / "artifact-distillation" / f"drain-{i}"
        run_watermark.stage(run_dir, result.watermark)
        run_watermark.promote(root, run_dir, "distill")
        if result.counts["scannable"] == 0:
            return
    raise AssertionError("distill run never drained")


def test_distill_threshold_clears_after_a_drained_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The stored watermark covers every admitted file: nothing is owed, so the gate no longer fires on the count."""
    _committed_repo_with_plans(tmp_path, monkeypatch, 55)
    assert udg._gate_distill_threshold(tmp_path, tmp_path, {}).detail["total"] == udg._DISTILL_THRESHOLD_TOTAL
    _drain_distill_runs(tmp_path)
    result = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.CLEAN
    assert result.detail["total"] == 0


def test_distill_threshold_dates_a_noop_run_from_the_watermark(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that disposes nothing writes no log header; the promoted watermark still dates it."""
    from coordinator_core import run_watermark
    from coordinator_core.ops.distill_scope import compute_scope

    _committed_repo_with_plans(tmp_path, monkeypatch, 0)
    (tmp_path / "state" / "distillation-log.md").write_text(
        "# Columns: run | path | disposition | fate\n\n## Run 2026-07-22-23h55\n", encoding="utf-8"
    )
    stale = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert stale.detail["days_ago"] > 14
    result = compute_scope(tmp_path, run_id="noop-0", cohort_specs=[], admit=True)
    run_dir = tmp_path / "state" / "scratch" / "artifact-distillation" / "noop-0"
    run_watermark.stage(run_dir, result.watermark)
    run_watermark.promote(tmp_path, run_dir, "distill")
    fresh = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert fresh.detail["days_ago"] in (0, 1)
    assert fresh.verdict == udg.GateVerdict.CLEAN


def test_distill_threshold_still_fires_on_admitted_files_past_the_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files dated past a drained watermark's cursor are owed; at the threshold the gate fires."""
    _committed_repo_with_plans(tmp_path, monkeypatch, 5)
    _drain_distill_runs(tmp_path)
    plans = tmp_path / "docs" / "plans"
    for i in range(55):
        (plans / f"2026-11-02-q{i}.md").write_text(f"---\nstatus: implemented\n---\n{_ADMISSIBLE_BODY}", encoding="utf-8")
    _cite_in_daily_log(tmp_path, [f"docs/plans/2026-11-02-q{i}.md" for i in range(55)])
    result = udg._gate_distill_threshold(tmp_path, tmp_path, {})
    assert result.verdict == udg.GateVerdict.FINDING
    assert result.detail["total"] == udg._DISTILL_THRESHOLD_TOTAL


# ---------------------------------------------------------------------------
# (h) end-to-end op dispatch
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (j) _run interpreter selection — regression guard for the P1 os.access(X_OK)
# defect: a .py CLI MUST always get the explicit sys.executable prefix,
# regardless of its own executable bit (a checked-out .py never needs +x —
# os.access(X_OK) on Windows would report True for ANY file and mask this).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (k) _run timeout — a hung CLI must surface as UNAVAILABLE, never CLEAN,
# and must never raise TimeoutExpired out of _run.
# ---------------------------------------------------------------------------


# The 11i / 11j sweeps live in test_updatedocs_sweeps.py (updatedocs.sweeps).


# ---------------------------------------------------------------------------
# (m) regex-canary — a parse-shape drift on the sole CONTRADICTION-detection
# mechanism must surface as UNAVAILABLE, never silently default to CLEAN.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (n) Windows extensionless-CLI exec resolution — `_windows_exec_argv`
# precedence (P1: CreateProcess does not honour a shebang; the fix mirrors
# the .cmd/.ps1/.py launcher-shim shape gen-launcher-shim.py already
# generates for every bin/ entrypoint). Monkeypatches `udg.sys.platform` so
# the branch is exercised deterministically regardless of the host OS this
# suite happens to run on.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# (j) The four bucket-2 doc-index / prune gates
#
# One UNAVAILABLE test per gate, each asserting explicitly that the verdict is
# not CLEAN. The 2026-09-02 boundary audit found "engine unreachable"
# indistinguishable from "found nothing" at nine of ten fallback sites in the
# ceremony this file serves; these four are where a tenth would be added, so
# the distinction is pinned per gate rather than once for the group.
# ---------------------------------------------------------------------------

_BUCKET2_GATES = (
    "docs-readme-index-drift",
    "directory-md-staleness",
    "plans-prune-candidates",
    "archive-memo-prune-candidates",
)


# ---------------------------------------------------------------------------
# directory-md-staleness takes its index path from the repo, not from claude-klabauter.
#
# The gate hardcoded `coordinator_core/DIRECTORY.md`, so it read UNAVAILABLE
# forever on every consumer of this ceremony except claude-klabauter — including
# coordinator-content-repo, whose index is `./DIRECTORY.md`. Reported by coordinator-content-repo-3f,
# state/bug-backlog/2026-09-02-directory-md-staleness-hardcodes-claude_klabauters-index-path.yaml.
# ---------------------------------------------------------------------------

_INDEX_BODY = "# DIRECTORY\n\nLast refreshed: 2026-09-02\n"


# ---------------------------------------------------------------------------
# (l) CLI resolution is platform-aware
#
# The deployed coordinator settings tree publishes each bin/ entrypoint as a
# native forwarder (`verify-skill-anchor-links.exe`) while this module names
# them the way the authoring repo writes them (extensionless, or `.py`). An
# exact-name resolver reported "CLI not found" for four gates whose binaries
# were present, which reads UNAVAILABLE — the verdict reserved for "could not
# look" — from a box where the gate could have run. These pin resolution
# against a platform-blind resolver, in both directions: Windows must find the
# PATHEXT form, POSIX must never invent one.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# In-process default route: check-rag-state spawns nothing.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# repomap-gate names why the generator produced no result.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Bounded default reply; plan sidecars are not plans.
# ---------------------------------------------------------------------------
