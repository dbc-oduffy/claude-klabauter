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
only sweep the FOUR conditional axes already documented on today's
pre-conversion `build_directives` (governing_plan_slug presence,
chain-terminal-vs-single-session disposition, review-fields presence,
and lesson-capture engine-stamp reachability -- the fourth added when
C11/AC15 made the lesson-capture directives conditional on
`_lesson_capture_reachable()`, which is monkeypatched over both legs
rather than left to whatever this clone's stamp state happens to be).
Most of the ~35 new directives C2a-C2i add are expected to be unconditional
scans/shell-conversions (D-3/D-4) and so appear on every sweep leg
regardless; if C3 lands a genuinely new conditional axis this sweep does
not know to flip, direction 2 will legitimately flag the affected manifest
entry until either the sweep's `_RICH_DECISIONS` payload is widened to
supply whatever key gates it, or it is added to the exception set by name.

A fifth axis (docs/plans/2026-09-07-directive-resolution-reaches-a-plugin-
local-cli.md, AC15): `_all_emittable_directive_clis` monkeypatches
`wsc._plugin_cli_reachable` to `True` unconditionally — the same
`monkeypatch.setattr` shape this sweep already uses for
`_lesson_capture_reachable`/`compute_session_shape_gate` — so the two
plugin-local barewords (`baton-chain-closure`, `plan-reversibility-
eligibility`) are always in the emitted set, on every box, regardless of
whether this box's own DoE ladder resolves. Without this axis,
`test_every_manifest_entry_is_named_by_at_least_one_directive` would fail
FOREVER on a box with no DoE clone (the fleet floor and the measured cloud
container) the moment those two names joined `CONSUMES_MANIFEST` — a
`cadence`-gate red weeks later, not a wave-window one. The prior revision
of this plan's own risk record read the `coordinator-lesson-add`/
`coordinator-queue-append` prose above as an existing machine-state
exemption being "extended" by this pair; that premise is false of this
tree (`_DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS` is its one exemption
channel, and it stays empty) — this axis, not that exemption, is the fix.

Run scoped only:
    python3 -m pytest coordinator_core/workstream_complete/test_workstream_complete_contract.py -q
Spec backlink: docs/plans/2026-07-26-workstream-complete-computed-frontage.md § AC2, D-5, chunk C1
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# value because they are invoked by a DISPATCHED WORKER rather than the
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
    if consumed_handoff_paths is None:
        consumed_handoff_paths = (consumed_handoff,) if consumed_handoff else ()
    return wsc.SessionShapeGate(
        sid="contract-test-sid",
        disposition=disposition,
        consumed_handoff=consumed_handoff,
        diagnostics=diagnostics or [],
        consumed_handoff_paths=consumed_handoff_paths,
    )


_GOVERNING_PLAN_SLUG = "contract-test-plan-slug"
_SESSION_ID = "contract-test-sid"


def _seed_disk_fixtures(tmp_path: Path) -> None:
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
        # _KEY_GOVERNING_PLAN_PATH`, a key this sweep did not previously
        "governing_plan_path": f"docs/plans/{_GOVERNING_PLAN_SLUG}.md",
        "orientation_cache_exists": True,
        "pinboard_note": "contract-test pinboard note",
        "review_partition": {
            "range": "aaaaaaa..bbbbbbb",
            "slices": [{"slice_id": "s1", "paths": ["coordinator/tests/contract-test.py"]}],
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
    _seed_disk_fixtures(tmp_path)
    monkeypatch.setattr(wsc, "_plugin_cli_reachable", lambda: True)
    clis: set[str] = set()
    dispositions = (
        _gate("chain-terminal", consumed_handoff="state/handoffs/contract-test.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    )
    for gate in dispositions:
        for governing_plan_slug in (False, True):
            for review_present in (False, True):
                for lesson_reachable in (False, True):
                    monkeypatch.setattr(wsc, "compute_session_shape_gate", lambda root, gate=gate: gate)
                    monkeypatch.setattr(
                        wsc,
                        "_lesson_capture_reachable",
                        lambda reachable=lesson_reachable: reachable,
                    )
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


def test_every_manifest_entry_is_named_with_the_plugin_ladder_unresolvable(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "coordinator_core.ceremony_common.cli_dispatch.resolve_plugin_cli_script_root",
        lambda: None,
    )
    emitted = _all_emittable_directive_clis(monkeypatch, tmp_path)
    for name in ("baton-chain-closure", "plan-reversibility-eligibility"):
        assert name in wsc.CONSUMES_MANIFEST
        assert name in emitted


def test_dispatched_worker_only_exemption_set_stays_empty() -> None:
    assert _DISPATCHED_WORKER_ONLY_MANIFEST_MEMBERS == frozenset()


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
