"""Shrink-only corpus ratchet: no NEW unattributed process-time figure lands
under state/{bug,debt}-backlog, state/improvement-queue, state/baselines,
docs/research or docs/problems.

The pattern and name set below are an INDEPENDENT copy of C7's detector
(coordinator_core/write_guards/nudge_unattributed_process_time_figure.py),
not an import of it -- an independent copy is what makes this a check rather
than a tautology against the guard it is meant to catch drift from.

_GRANDFATHERED holds the files that already state an unattributed figure at
this row's fire HEAD (census row 4 sized this at 43 files at authoring; this
row's own recount at fire HEAD is the operative number, per this row's own
body). None of those rows is rewritten to attribute its figure -- figures
taken with the sanctioned primitive are sound, and rewriting history is not
this row's job. The register shrinks only: dropping a row without lowering
_PINNED_CEILING to match is a forgotten manual edit, never an observed state.
"""

import re
from pathlib import Path

_PREFIXES = (
    "state/bug-backlog",
    "state/debt-backlog",
    "state/improvement-queue",
    "state/baselines",
    "docs/research",
    "docs/problems",
)

_FIGURE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s?ms\s+(?:of\s+)?process\b"
    r"|process[ -]time[^.\n]{0,40}?\d+(?:\.\d+)?\s?ms",
    re.I,
)

_NAMED_INSTRUMENTS = re.compile(
    r"batched_process_time_ms"
    r"|batched_process_time_quantiles"
    r"|single_invocation_tree_process_time"
    r"|LiveTreeAccountant"
    r"|in_process_time_ms"
    r"|benchmarks\.measure"
)

_GRANDFATHERED: "frozenset[str]" = frozenset(
    {
        "docs/problems/2026-08-22-artifact-emit-cannot-be-earned-back-in-its-current-shape.md",
        "docs/problems/2026-08-23-two-upstream-claude-code-asks.md",
        "docs/problems/2026-08-26-the-session-directory-had-no-constructor.md",
        "docs/problems/2026-08-26-what-a-reinstall-on-the-mac-actually-hits.md",
        "docs/problems/2026-08-27-the-killed-op-surface-which-jobs-still-n.md",
        "docs/problems/2026-09-11-deliverable-rollup-is-over-the-kill-bar.md",
        "docs/research/2026-08-21-cli-trampoline-chain-cost.md",
        "docs/research/2026-08-23-track-touched-files-post-rebuild-cold-measurement.md",
        "docs/research/2026-08-26-18h35-warm-succession-workdir/prior-art-check.md",
        "docs/research/2026-08-27-fact-layer-hot-path-measured.md",
        "docs/research/2026-08-27-warm-cli-load-cost.md",
        "docs/research/2026-08-29-housekeeping-v2-target-shape.md",
        "docs/research/2026-08-31-generator-discovery-rebuild-sketch.md",
        "docs/research/2026-09-11-group-em-heartbeat-collision-rate-and-candidate-cost.md",
        "docs/research/spike-verdicts/2026-08-21-per-op-census-inside-the-brightline.md",
        "docs/research/spike-verdicts/2026-08-22-boot-backstop-without-a-history-walk.md",
        "docs/research/spike-verdicts/2026-08-22-posix-spawn-count-for-the-brightline-instrument.md",
        "docs/research/spike-verdicts/2026-08-22-where-the-track-touched-files-cost-actually-lives.md",
        "docs/research/spike-verdicts/2026-08-25-transcript-mtime-as-peer-liveness.md",
        "docs/research/spike-verdicts/2026-08-26-a-parameterized-provenance-query-at-the-front-insert-seam.md",
        "docs/research/spike-verdicts/2026-08-26-deriving-the-register-population-and-resolving-its-rows.md",
        "docs/research/spike-verdicts/2026-08-26-the-tree-is-a-repathing-and-python-can-compute-it.md",
        "docs/research/spike-verdicts/2026-08-26-where-the-commit-op-s-other-half-second-goes.md",
        "docs/research/spike-verdicts/2026-08-27-four-folded-bash-guards-on-the-per-call-guard-chain.md",
        "docs/research/spike-verdicts/2026-08-27-the-in-plane-archival-sweep-fires-warm.md",
        "docs/research/spike-verdicts/2026-08-29-guard-class-relay-commit-seam.md",
        "docs/research/spike-verdicts/2026-08-30-baton-ship-stamp-inside-a-500ms-close.md",
        "docs/research/spike-verdicts/2026-08-30-bounded-blocked-by-id-resolution-on-the-brief-path.md",
        "docs/research/spike-verdicts/2026-08-30-housekeeping-cycle-absorbs-a-terminal-plans-leg.md",
        "docs/research/spike-verdicts/2026-08-30-the-archive-stamp-verbs-at-their-floor.md",
        "docs/research/spike-verdicts/2026-08-30-warm-engine-owns-the-post-commit-push.md",
        "docs/research/spike-verdicts/2026-08-30-workflow-run-termination-detectable-from-disk.md",
        "docs/research/spike-verdicts/2026-08-31-generator-discovery-cache-rebuild.md",
        "docs/research/spike-verdicts/2026-09-01-harness-session-state-surface.md",
        "docs/research/spike-verdicts/2026-09-10-doctrine-enforcement-surfaces-c5-perf-windows-spike.md",
        "docs/research/spike-verdicts/2026-09-11-write-detector-mutation-harness-viability.md",
        "docs/research/spike-verdicts/2026-09-22-maintenance-reaper-after-daily-pack.md",
        "docs/research/spike-verdicts/2026-09-22-warm-server-child-inclusive-attribution.md",
        "docs/research/warm-engine-premise/warm-under-concurrency.md",
        "state/debt-backlog/2026-08-27-the-two-invoke-doctor-probes-spawn-the-s-27ab671d32b0.yaml",
        "state/debt-backlog/2026-08-28-the-registration-completeness-gate-canno-b8d85ea3a3f1.yaml",
        "state/debt-backlog/2026-09-03-fleet-reap-review-trail-rest-settle-spaw-248862aa6b0a.yaml",
        "state/improvement-queue/2026-08-23-wsc-tail-is-a-kill-bar-op-every-close-h-3f9a21c7b4e0.yaml",
        "state/improvement-queue/2026-08-26-no-guard-pins-carrier-import-order-and-t-a5dd73a1772f.yaml",
        "state/improvement-queue/2026-08-27-workstream-complete-never-writes-a-review-trail.yaml",
        "state/improvement-queue/2026-08-28-the-eol-drift-requirement-outlived-its-family-and-a-125ms-detector-answers-it.yaml",
        "state/improvement-queue/2026-08-30-nothing-detects-a-caller-still-dialling-ea8d5a3b55da.yaml",
    }
)

_PINNED_CEILING = 47


def _repo_root() -> Path:
    # coordinator_core/tests/<this file> -> repo root is two parents up.
    return Path(__file__).resolve().parents[2]


def _offending_files(root: Path) -> "frozenset[str]":
    offenders = set()
    for prefix in _PREFIXES:
        base = root / prefix
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in (".yaml", ".md", ".json"):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if _FIGURE_PATTERN.search(text) and not _NAMED_INSTRUMENTS.search(text):
                offenders.add(path.relative_to(root).as_posix())
    return frozenset(offenders)


def test_no_new_unattributed_process_time_figure_outside_the_frozen_inventory():
    """The gate. A file stating a process-time figure that names none of the
    sanctioned primitives, outside the grandfathered set, fails here."""
    offending = _offending_files(_repo_root())
    new = sorted(offending - _GRANDFATHERED)
    assert not new, (
        "these files state a process-time figure that names no sanctioned "
        "instrument -- measure with `python -m coordinator_core.benchmarks."
        "measure -- <argv>` and paste its line, or drop the figure:\n"
        + "\n".join(f"  {path}" for path in new)
    )


def test_the_register_is_shrink_only():
    """The ratchet. Adding a grandfather row costs what raising a budget
    costs: this literal must move too, in the same diff."""
    assert len(_GRANDFATHERED) == _PINNED_CEILING, (
        f"the grandfather register holds {len(_GRANDFATHERED)} rows, not the "
        f"pinned {_PINNED_CEILING}. It shrinks only. A new unattributed figure "
        f"does not join this list -- it is measured with the sanctioned "
        f"primitive instead. A register smaller than its ceiling means a row "
        f"was removed without lowering the ceiling to match -- do that in the "
        f"same diff."
    )


def test_the_detector_fires_on_a_synthetic_unattributed_file(tmp_path):
    """Negative self-test. A synthetic unattributed figure under a tmp copy
    of one prefix must make the check fail -- the detector is not vacuous."""
    root = tmp_path
    target_dir = root / "docs" / "research"
    target_dir.mkdir(parents=True)
    (target_dir / "synthetic-unattributed-figure.md").write_text(
        "the write took 250ms of process time, no instrument named.\n",
        encoding="utf-8",
    )
    offending = _offending_files(root)
    new = offending - _GRANDFATHERED
    assert new, "the synthetic unattributed file was not detected -- the pattern is broken"
