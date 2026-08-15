"""
coordinator_core.workstream_complete.test_workstream_complete_contract — per-field
CONSUMES_MANIFEST <-> emitted-directives conformance test for the
`workstream-complete` computed-skill engine (AC2/D-5,
docs/plans/2026-07-26-workstream-complete-computed-frontage.md, chunk C1).

Purpose: per D-5, this file is authored BEFORE the assembler expansion
(C2a-C2i/C3) lands, as the regression net the build is held to rather than
a test retrofitted after the fact. It is EXPECTED RED until C3 wires
`CONSUMES_MANIFEST` and folds the submodule directives into `build_directives`
-- a bare `AttributeError: module ... has no attribute 'CONSUMES_MANIFEST'`
is the correct, legitimate red state today, not a defect in this file. C3's
own task body ("Turns C1 green") names this file as its own acceptance
oracle.

Mirrors `coordinator_core.workday_complete.test_workday_complete_contract`
and `coordinator_core.workweek_complete.test_workweek_complete_contract`
(same plan family, AC10-shaped precedent): per-field assertions in BOTH
directions, one `assert` per manifest row inside a loop -- never a
catch-all `set(...) == set(...)` blob or a `gates.*` row -- so a single
dropped or orphaned CLI name fails on its own line, not folded into one
aggregate diff that hides which entry actually drifted.

Direction 1 (phantom-verb guard, AC15c): every `directives[].cli` this
module can emit is a `CONSUMES_MANIFEST` member.
Direction 2 (dead-census guard): every `CONSUMES_MANIFEST` member is named
by >=1 emitted directive, OR is a documented dispatched-worker-only
exception (mirrors workday's `coordinator-queue-append` exception --
`_DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS` below is empty today because no
current census row is worker-only-invoked; extend it by name, never
blanket-exempt, if C2*/C3 lands one).

Coverage caveat, stated explicitly rather than silently assumed: this file
is authored before C2a-C2i exist, so `_all_emittable_directive_clis` can
only sweep the THREE conditional axes already documented on today's
pre-conversion `build_directives` (governing_plan_slug presence,
chain-terminal-vs-single-session disposition, review-fields presence).
Most of the ~35 new directives C2a-C2i add are expected to be unconditional
scans/shell-conversions (D-3/D-4) and so appear on every sweep leg
regardless; if C3 lands a genuinely new conditional axis this sweep does
not know to flip, direction 2 will legitimately flag the affected manifest
entry until either the sweep's `_RICH_DECISIONS` payload is widened to
supply whatever key gates it, or it is added to the exception set by name.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_workstream_complete_contract.py -q
Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md § AC2, D-5, chunk C1
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# Documented consumes-manifest members that are never a `directives[].cli`
# value because they are invoked by a DISPATCHED WORKER rather than the
# assembler itself. Empty today -- no current census row is worker-only
# invoked (unlike workday's `coordinator-queue-append`). Extend by NAME,
# with a one-line reason, if C2d's review-dispatch shell (or any other
# submodule) turns out to name a CLI that only a dispatched review worker
# invokes -- never widen this to a blanket exemption.
_DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS: frozenset[str] = frozenset()

_REVIEW_FIELDS_PRESENT = {
    "sha_range": "a..b",
    "reviewer": "code-reviewer",
    "scope": "chain",
    "verdict": "ok",
    "diff_loc": 10,
}


def _gate(
    disposition: str,
    consumed_handoff: str = "",
    diagnostics: list[str] | None = None,
    consumed_handoff_paths: tuple[str, ...] | None = None,
) -> "wsc.SessionShapeGate":
    # C3B widened SessionShapeGate with a plural `consumed_handoff_paths`
    # field. Default it from the scalar `consumed_handoff` when the caller
    # doesn't supply the plural form explicitly, mirroring
    # `resolve_disposition`'s own "scalar == plural[0], or empty" contract.
    if consumed_handoff_paths is None:
        consumed_handoff_paths = (consumed_handoff,) if consumed_handoff else ()
    return wsc.SessionShapeGate(
        sid="contract-test-sid",
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=diagnostics or [],
        consumed_handoff_paths=consumed_handoff_paths,
    )


#: Fixed governing-plan slug + session id `_rich_decisions`/`_seed_disk_
#: fixtures` share -- the sid must match `_gate`'s own hardcoded
#: `"contract-test-sid"` (the sidecar-fold directive globs `state/subagent-
#: share/<sid>/<plan_slug>.*.md`, so the two have to agree).
_GOVERNING_PLAN_SLUG = "contract-test-plan-slug"
_SESSION_ID = "contract-test-sid"


def _seed_disk_fixtures(tmp_path: Path) -> None:
    """C2a-C2i widened this sweep's live manifest surface far past the
    three axes the module docstring's original Coverage caveat named
    (governing_plan_slug presence, disposition, review-fields presence) --
    several of the ~35 new directives are gated on real ON-DISK state a
    bare `decisions` key can't fake (`resolve_governing_plan` verifies the
    plan file exists before resolving a slug; `completion_archive_
    predicate` checks for a real `archive/` dir or `state/workstreams/`;
    `compute_run_report_sidecar_gate` globs a real sidecar directory) --
    see this file's own module docstring negative-spec, which named these
    exact members as the expected residual red pre-widening. This helper
    seeds that disk state once per sweep so every conditional axis a
    submodule actually gates on gets a chance to fire, closing branch (a)
    of the dispatch brief's Failure 2 (widen the sweep, not the manifest).
    """
    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / f"{_GOVERNING_PLAN_SLUG}.md").write_text(
        "# contract-test governing plan\n\nSynthetic fixture for the manifest-coverage sweep.\n",
        encoding="utf-8",
    )

    sidecar_dir = tmp_path / "state" / "subagent-share" / _SESSION_ID
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (sidecar_dir / f"{_GOVERNING_PLAN_SLUG}.wave1.md").write_text(
        "---\nstatus: complete\n---\n\nSynthetic run-report sidecar for the manifest-coverage sweep.\n",
        encoding="utf-8",
    )


def _rich_decisions(*, governing_plan_slug: bool, review_present: bool, tmp_path: Path) -> dict:
    """The most generous `decisions` payload this file can construct from
    what is documented today (see module docstring's Coverage caveat) --
    every decision key the current `build_directives` reads, plus the ones
    named in the C2a-C2i task bodies that plausibly gate a new directive
    (subject/prose/stage_paths, deleted/kept paths, msg_file), all set to a
    synthetic non-empty value so a presence-gated directive fires.

    Widened past the original 3-axis sweep (see `_seed_disk_fixtures`'s
    docstring): the block below adds the C2a-C2i decision keys that gate
    `coordinator-lesson-add`/`coordinator-queue-append` (a lesson list with
    a universal-scoped entry), `coordinator-fold-execution-record` (a
    plan_path pointing at the seeded governing plan), and the C2d
    review-dispatch cluster (`freeze-review-diff.py`/`fan-out-integrator.py`/
    `scan_unresolved_ubt_records.py`/`classify-dispatch-shape.py`) -- all
    unconditional (not gated behind the two documented axes) because none
    of C2a-C2i's own gates depend on `governing_plan_slug`/`review_present`
    the way the pre-existing `d-claim-plan-execution-lock`/`d-write-trail`
    pair does; `archive-stamp-cli` and `coordinator-harvest-deferrals` need
    no new decision key at all -- they were only ever gated on the governing
    plan FILE actually existing on disk, which `_seed_disk_fixtures` now
    provides on the `governing_plan_slug=True` leg.
    """
    decisions: dict = {
        "subject": "contract-test commit subject",
        "prose": "contract-test prose",
        "stage_paths": ["state/scratch/contract-test.md"],
        "deleted_paths": ["state/scratch/deleted.md"],
        "kept_entries": ["state/scratch/kept.md"],
        "msg_file": str(tmp_path / "msg.txt"),
        "lessons": [
            {
                "title": "contract-test lesson",
                "body": "contract-test lesson body",
                "scope": "universal",
                "queue_title": "contract-test queue title",
                "queue_body": "contract-test queue body",
                "surface": "coordinator/tests/contract-test.py",
                "proposed_action": "contract-test proposed action",
                "change_kind": "wiki-append",
            }
        ],
        "plan_path": f"docs/plans/{_GOVERNING_PLAN_SLUG}.md",
        "orientation_cache_exists": True,
        "pinboard_note": "contract-test pinboard note",
        "review_partition": {
            "range": "aaaaaaa..bbbbbbb",
            "slices": [{"slice_id": "s1", "paths": ["coordinator/tests/contract-test.py"]}],
            "integrator_spec_tsv": "state/review-trail/contract-test-spec.tsv",
        },
        "ubt_check": {"applies": True, "since_sha": "aaaaaaa"},
        "classify_dispatch_plan_file": f"docs/plans/{_GOVERNING_PLAN_SLUG}.md",
    }
    if governing_plan_slug:
        decisions["governing_plan_slug"] = _GOVERNING_PLAN_SLUG
    if review_present:
        decisions["review"] = dict(_REVIEW_FIELDS_PRESENT)
    return decisions


def _all_emittable_directive_clis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> set[str]:
    """Union of every `cli` value `wsc.brief` can emit across the
    documented conditional axes -- see the module docstring's Coverage
    caveat for what this sweep does and does not know about. A single
    combined call would under-report a directive that only appears on one
    side of its own axis, making a live-but-conditional CLI name look like
    a dead manifest entry.

    Calls `_seed_disk_fixtures` once up front -- the disk-gated members
    (governing plan file, archive dir, run-report sidecar) are static
    fixtures shared across every leg of the sweep below, not per-leg state.
    """
    _seed_disk_fixtures(tmp_path)
    clis: set[str] = set()
    dispositions = (
        _gate("chain-terminal", consumed_handoff="state/handoffs/contract-test.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    )
    for gate in dispositions:
        for governing_plan_slug in (False, True):
            for review_present in (False, True):
                monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root, gate=gate: gate)
                decisions = _rich_decisions(
                    governing_plan_slug=governing_plan_slug,
                    review_present=review_present,
                    tmp_path=tmp_path,
                )
                decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
                clis.update(d["cli"] for d in decision_object["directives"])
    return clis


def test_manifest_has_no_duplicate_entries() -> None:
    manifest = wsc.CONSUMES_MANIFEST
    assert len(manifest) == len(set(manifest)), (
        f"CONSUMES_MANIFEST carries a duplicate entry: {manifest!r}"
    )


def test_every_emitted_directive_cli_is_a_manifest_member(monkeypatch, tmp_path) -> None:
    """No directive names a CLI CONSUMES_MANIFEST doesn't enumerate."""
    manifest = set(wsc.CONSUMES_MANIFEST)
    for cli in sorted(_all_emittable_directive_clis(monkeypatch, tmp_path)):
        assert cli in manifest, (
            f"directive emits CLI {cli!r}, which is absent from CONSUMES_MANIFEST "
            "-- a phantom verb reaching the apply half unlisted (AC15c)"
        )


def test_every_manifest_entry_is_named_by_at_least_one_directive(monkeypatch, tmp_path) -> None:
    """No CONSUMES_MANIFEST row is a dead census entry no directive
    exercises, UNLESS it's a documented dispatched-worker-only member (one
    assertion per manifest field either way, per AC2's own text -- no
    blanket exemption, each named exception gets checked by name)."""
    emitted = _all_emittable_directive_clis(monkeypatch, tmp_path)
    for cli in wsc.CONSUMES_MANIFEST:
        if cli in _DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS:
            assert cli not in emitted, (
                f"{cli!r} is documented as dispatched-worker-only but now "
                "also appears as a directives[] cli -- update the negative-"
                "spec/exception set, this is no longer purely worker-invoked"
            )
            continue
        assert cli in emitted, (
            f"CONSUMES_MANIFEST names {cli!r}, but no directive ever emits it "
            "across this file's documented sweep axes -- either a dead "
            "census row, a directive silently dropped, or a new conditional "
            "axis this sweep does not yet know to flip (see module "
            "docstring's Coverage caveat -- widen _rich_decisions or the "
            "gate sweep, or add a named dispatched-worker-only exception)"
        )


@pytest.mark.real_home
def test_brief_envelope_preflight_consumes_manifest_matches_module_constant() -> None:
    """The 8-key envelope's `preflight.consumes_manifest` field (what the
    surface actually announces it consumes) must be byte-identical to the
    module's own `CONSUMES_MANIFEST` constant -- catches a `brief()` author
    hand-copying a stale list into the envelope instead of deriving it from
    the one source of truth. Mirrors workday/workweek's identically-named
    counterpart test.

    `real_home`: `brief()` resolves the real machine-local registry via
    `resolve_operator_config`, which the suite-root HOME quarantine
    (`conftest.py`) would otherwise blank out. This is a live-tree parity
    oracle by design (does the envelope really carry what the module claims
    it consumes) -- read-only, never fails destructively."""
    decision_object = wsc.brief()
    assert decision_object["preflight"]["consumes_manifest"] == list(wsc.CONSUMES_MANIFEST)
