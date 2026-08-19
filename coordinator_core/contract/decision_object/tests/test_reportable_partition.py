"""Census oracle for AC3 (docs/plans/2026-08-15-judgment-points-that-gate-
nothing-stop-being-questions.md, chunk C1): walks every `build_judgment_point`
call site in `coordinator_core/` that this plan's mechanism can reach,
constructs each builder with representative inputs, and asserts the
resulting set of gate-nothing recommendation-carrying ids equals a
checked-in expected set. The count lives here, never in prose (Anti-scope:
"Do NOT hand-count the census into prose") -- a new gate-nothing point
appearing anywhere fails this test with a message naming the id and its
file, per AC3.

Construction pattern: reuses the SAME representative-input shapes already
hand-verified by `coordinator_core/ceremony_common/_phantom_sweep_providers.py`
(the fleet-wide phantom-resolves-id guard's own per-package sweep registry)
rather than re-deriving them -- but keeps the FULL `directives`/
`judgment_points` dicts that guard's own `_collect` reduces to bare id sets,
since this oracle needs `recommendation` and per-disposition `resolves`
content, not just ids.

Static-vs-execution disagreement (plan's "The site census, and why two
earlier counts were both wrong"): a purely static sweep over-counts (misses
that some `resolves` lists are computed by a resolver that returns real ids
once populated); a purely execution-observed sweep under-counts (misses
points that only emit under a non-default input, e.g. `readers_clean_ops`'s
EM-environment drift points). This oracle is neither -- it constructs each
builder directly (bypassing ambient session/repo state) across the input
variants each source package's own tests already use to reach every
resolves-bearing branch, so it is confirmed-by-construction rather than by
either kind of passive observation.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coordinator_core.contract.decision_object.judgment import (
    _directive_axis,
    _gates_nothing,
    partition_reportable,
)


# ---------------------------------------------------------------------------
# Out-of-scope-by-architecture exclusions (plan's Anti-scope + Problem
# section "The site census, and why two earlier counts were both wrong").
# These `workstream_complete` points build `resolves` through a RESOLVER
# (`review_partition_resolves_ids`, `lesson_capture_resolves_ids`,
# `memo_flip_resolves_ids`) that returns real directive ids once its
# `decisions` slice is populated, and `[]` only when that slice is empty --
# an honest "this dispatches nothing", not a phantom/gate-nothing id. A
# single observation under an empty `decisions` slice (this oracle's
# `decisions={}` variant, added to exercise C1(c)'s `jp-coverage-verdict`
# determination) would misclassify all five as gate-nothing, reproducing
# the exact over-count this plan's Problem section documents. Excluded here
# by name, not by re-deriving resolver internals -- mirrors
# `phantom_resolves_sweep.py`'s own `no_directive_backing_ids`/
# `dynamic_suffix_bases` named-exemption pattern.
# ---------------------------------------------------------------------------
_RESOLVER_BACKED_OUT_OF_SCOPE_IDS = frozenset(
    {
        "review-partition-strategy",
        "review-dispatch-vehicle-choice",
        "reviewer-count-on-oracle-disagreement",
        "lesson-worth-capturing",
        "memo-resolution-attribution",
    }
)


def _is_gate_nothing_on_directive_axis(
    point: dict[str, Any], directives: list[dict[str, Any]]
) -> bool:
    """Same "gate-nothing" computation `partition_reportable` makes
    internally (directive-membership + the `depends_on` precondition), but
    INDEPENDENT of the `reportable` marker -- this oracle needs the
    directive-axis fact on its own to build the three-way ledger (a point
    can be gate-nothing on this axis and still be `asked`, if it is marked
    `reportable=False` or left unclassified `None`). Delegates to
    `judgment._gates_nothing`/`_directive_axis` directly (Review:
    code-reviewer -- was a from-scratch reimplementation that could drift
    from the production computation) rather than reimplementing it inline."""
    from coordinator_core.contract.apply_base import normalize_depends_on

    directive_ids, depended_on_ids = _directive_axis(directives, normalize_depends_on)
    return _gates_nothing(point, directive_ids, depended_on_ids)


def _gate_nothing_recommendation_carrying(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    source_label: str,
    acc: dict[str, dict[str, Any]],
) -> None:
    """Three-way-ledger accumulation (C1b correction): records every
    gate-nothing, recommendation-carrying point regardless of its
    `reportable` marking (unlike the pre-C1b version, which only walked
    `partition_reportable`'s `reported` output -- that undercounted once
    demotion started requiring an explicit opt-in). Each entry carries its
    contributing source file(s) and its `reportable` classification
    (`True`/`False`/`None`), so the test below can assert every such point
    is in exactly one of the reportable/action-class buckets."""
    for jp in judgment_points:
        if jp.get("recommendation") is None:
            continue
        if jp["id"] in _RESOLVER_BACKED_OUT_OF_SCOPE_IDS:
            continue
        if not _is_gate_nothing_on_directive_axis(jp, directives):
            continue
        entry = acc.setdefault(jp["id"], {"sources": set(), "reportable": jp.get("reportable")})
        entry["sources"].add(source_label)


class _FakeMonkeyPatch:
    """A `pytest.MonkeyPatch`-shaped `setattr`/`undo` pair, usable both as a
    real pytest fixture (passed straight through) and standalone where a
    reused provider helper (`pickup_assemble_variants`) expects that exact
    surface but this module wants to control the undo point itself."""

    def __init__(self) -> None:
        self._calls: list[tuple[Any, str, bool, Any]] = []

    def setattr(self, obj: Any, name: str, value: Any) -> None:
        had = hasattr(obj, name)
        prior = getattr(obj, name, None)
        self._calls.append((obj, name, had, prior))
        setattr(obj, name, value)

    def undo(self) -> None:
        for obj, name, had, prior in reversed(self._calls):
            if had:
                setattr(obj, name, prior)
            else:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
        self._calls = []


def _sweep_workday_complete(acc: dict[str, set[str]]) -> None:
    from coordinator_core.workday_complete import brief as wd_brief

    open_day_goals_variants = (
        {"today": [], "stale": []},
        {"today": [{"goal_id": "g1", "text": "ship it"}], "stale": []},
    )
    dirty_tree_variants = ({"ambiguous": False}, {"ambiguous": True, "evidence": "x"})
    decisions = {"day_goal_closeout": {"g1": "done"}}
    for open_day_goals in open_day_goals_variants:
        for dirty_tree_verdict in dirty_tree_variants:
            directives = wd_brief._build_directives(decisions, open_day_goals, dirty_tree_verdict)
            judgment_points = wd_brief._build_judgment_points(open_day_goals, dirty_tree_verdict)
            _gate_nothing_recommendation_carrying(directives, judgment_points, "workday_complete/brief.py", acc)


def _sweep_workweek_complete(acc: dict[str, set[str]]) -> None:
    from coordinator_core.workweek_complete import brief as ww_brief

    directives = ww_brief._build_directives()
    judgment_points = ww_brief._build_judgment_points()
    _gate_nothing_recommendation_carrying(directives, judgment_points, "workweek_complete/brief.py", acc)


def _sweep_baton_assemble(acc: dict[str, set[str]]) -> None:
    from coordinator_core import baton_assemble as ba

    lineage_variants = [
        ("handoff", {"deliverable_id": "d1", "output_path": "state/handoffs/x.md", "artifact_path": "docs/plans/p.md", "predecessor": None}),
        ("handoff", {"deliverable_id": "d1", "output_path": "state/handoffs/x.md", "artifact_path": "docs/plans/p.md", "predecessor": "state/handoffs/prev.md"}),
        ("spinoff", {"deliverable_id": "d1", "output_path": "state/handoffs/x.md", "artifact_path": "docs/plans/p.md", "origin_handoff": "state/handoffs/o.md", "origin_handoff_id": "o1", "origin_session": "s1", "origin_plan_id": "p1"}),
    ]
    dirty_tree_attribution_variants = (
        {"degraded": False, "mine": [], "residue_count": 0},
        {"degraded": False, "mine": ["a.txt"], "residue_count": 2},
        {"degraded": True, "evidence": "probe unavailable"},
    )
    for kind, lineage in lineage_variants:
        directives = ba._build_directives(kind, lineage)
        for dirty_tree_attribution in dirty_tree_attribution_variants:
            judgment_points = ba._build_judgment_points(kind, dirty_tree_attribution)
            _gate_nothing_recommendation_carrying(directives, judgment_points, "baton_assemble/__init__.py", acc)


def _sweep_merge_assemble(acc: dict[str, set[str]]) -> None:
    from coordinator_core import merge_assemble as ma

    directives = ma.build_directives(Path("repo"), tag_prefix="v", proposed_tag="v1.2.3")
    judgment_points = ma.build_judgment_points()
    _gate_nothing_recommendation_carrying(directives, judgment_points, "merge_assemble/__init__.py", acc)


def _sweep_review_assemble(acc: dict[str, set[str]], tmp_path: Path) -> None:
    from coordinator_core.review_assemble import residue as residue_mod

    content_root = tmp_path / "review-assemble-content-root"
    residue_dir = content_root / "skills" / "review" / "residue"
    residue_dir.mkdir(parents=True)
    (residue_dir / "010-shared.md").write_text(
        "---\nsegment_id: shared-seg\nsurface: shared\nclass: protected\norder: 0\n---\nShared.\n",
        encoding="utf-8",
    )
    (residue_dir / "020-plan.md").write_text(
        "---\nsegment_id: plan-seg\nsurface: plan\nclass: droppable\norder: 1\n---\nPlan.\n",
        encoding="utf-8",
    )
    (residue_dir / "030-diff.md").write_text(
        "---\nsegment_id: diff-seg\nsurface: diff\nclass: droppable\norder: 2\n---\nDiff.\n",
        encoding="utf-8",
    )
    mp = _FakeMonkeyPatch()
    mp.setattr(residue_mod, "resolve_content_root", lambda: str(content_root))
    repo_root = tmp_path / "review-assemble-repo"
    repo_root.mkdir()
    call_variants = [
        {"artifact_arg": None, "explicit_surface": "plan"},
        {"artifact_arg": None, "explicit_surface": "diff"},
        {"artifact_arg": None, "explicit_surface": None},
    ]
    for variant in call_variants:
        decision_object = residue_mod.brief(
            variant["artifact_arg"], repo_root=repo_root, explicit_surface=variant["explicit_surface"],
        )
        _gate_nothing_recommendation_carrying(
            decision_object["directives"], decision_object["judgment_points"], "review_assemble/residue.py", acc
        )
    mp.undo()


def _sweep_workstream_complete(acc: dict[str, set[str]], tmp_path: Path) -> None:
    from coordinator_core import workstream_complete as wsc
    from coordinator_core.ops.ceremony.wsc_disposition import (
        LEGACY_PREDECESSOR_CONSUMED,
        SINGLE_SESSION,
    )
    from coordinator_core.workstream_complete.test_workstream_complete import (
        _gate,
        _patch_gate,
        _write_handoff,
        _write_plan,
    )

    (tmp_path / "archive").mkdir(parents=True, exist_ok=True)
    plan_slug = "reportable-partition-census-governing-plan"
    _write_plan(tmp_path, plan_slug)
    _write_handoff(tmp_path, "state/handoffs/x.md", f"docs/plans/{plan_slug}.md")

    decisions = {
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
        "memo_dispositions": [{"path": "state/memo-outbox/x.md", "decision": "actioned"}],
        "review": {
            "sha_range": "a..b",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "diff_loc": 10,
        },
        "review_partition": {
            "range": "aaaaaaa..bbbbbbb",
            "slices": [{"slice_id": "s1", "paths": ["coordinator/tests/contract-test.py"]}],
            "integrator_spec_tsv": "state/review-trail/contract-test-spec.tsv",
        },
        "orientation_cache_exists": True,
        "pinboard_note": "contract-test pinboard note",
        "scratch_candidates": ["state/scratch/contract-test-scratch-file.md"],
        "unattributable_files": ["state/scratch/contract-test-unattributable-file.md"],
        "flags": ["contract-test flagged item"],
    }
    gate_variants = (
        _gate(
            LEGACY_PREDECESSOR_CONSUMED,
            consumed_handoff="state/handoffs/x.md",
            consumed_handoff_paths=(),
        ),
        _gate(SINGLE_SESSION, consumed_handoff_paths=()),
    )

    # (C2 redo, docs/plans/2026-08-15-judgment-points-that-gate-nothing-
    # stop-being-questions.md) `wsc.brief()`'s returned `judgment_points[]`
    # is now POST-`partition_reportable` (asked-only) -- a `reportable=True`
    # point is demoted out of it into `narration` before `brief()` ever
    # returns. This census's own purpose is the PRE-partition set (every
    # gate-nothing recommendation-carrying point regardless of its
    # `reportable` marking), so a spy on `partition_reportable` captures its
    # raw input before delegating through unchanged, and that captured list
    # -- not `decision_object["judgment_points"]` -- is what gets fed to
    # `_gate_nothing_recommendation_carrying` below.
    from coordinator_core.contract.decision_object.judgment import (
        partition_reportable as _real_partition_reportable,
    )

    def _brief_and_capture_pre_partition_jps(decisions_payload: dict[str, Any]) -> dict[str, Any]:
        captured: list[dict[str, Any]] = []

        def _spy(judgment_points, directives):
            captured.extend(judgment_points)
            return _real_partition_reportable(judgment_points, directives)

        mp = _FakeMonkeyPatch()
        mp.setattr(wsc, "partition_reportable", _spy)
        try:
            decision_object = wsc.brief(decisions=decisions_payload, repo_root=tmp_path)
        finally:
            mp.undo()
        return {"directives": decision_object["directives"], "judgment_points": captured}

    for gate in gate_variants:
        mp = _FakeMonkeyPatch()
        _patch_gate(mp, gate)
        decision_object = _brief_and_capture_pre_partition_jps(decisions)
        _gate_nothing_recommendation_carrying(
            decision_object["directives"], decision_object["judgment_points"], "workstream_complete", acc
        )
        mp.undo()

    # K-001 (state/kill-ledger.md): this second sweep used to exist solely to
    # reach `jp-coverage-verdict`'s fallback branch (`decisions["review"]`
    # ABSENT), the one path `build_coverage_judgment_point` -- now removed --
    # took that the populated-`decisions` sweep above never exercised. Kept
    # (not deleted with that builder) because `decisions={}` also changes
    # which OTHER preserved judgment points fire (e.g. governing-plan/
    # completion-cluster gating in `_build_preserved_judgment_points`) --
    # this sweep still covers whatever gate-nothing points are reachable only
    # on that branch, there just happens to be no coverage-specific one left.
    for gate in gate_variants:
        mp = _FakeMonkeyPatch()
        _patch_gate(mp, gate)
        decision_object = _brief_and_capture_pre_partition_jps({})
        _gate_nothing_recommendation_carrying(
            decision_object["directives"], decision_object["judgment_points"], "workstream_complete", acc
        )
        mp.undo()


def _sweep_consolidate_assemble(acc: dict[str, set[str]], tmp_path: Path) -> None:
    from coordinator_core import consolidate_assemble as ca

    wt_path = str(tmp_path / "consolidate-wt")

    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        while args and args[0].startswith("-"):
            args = args[1:]
        if args[:2] == ["config", "user.email"]:
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
        if args[:2] == ["rev-parse", "--verify"]:
            ok = args[2] == "main"
            return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
        if args[0] == "branch":
            return SimpleNamespace(returncode=0, stdout="* current\n  main\n  stale\n", stderr="")
        if args[0] == "log" and args[1] == "-1":
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[0] == "log" and args[1] == "--oneline":
            return SimpleNamespace(returncode=0, stdout="abc123 a commit\n", stderr="")
        if args[0] == "show":
            return SimpleNamespace(returncode=0, stdout="1 file changed\n", stderr="")
        if args[0] == "worktree" and args[1] == "list":
            stdout = (
                f"worktree {tmp_path}\nHEAD abc\nbranch refs/heads/current\n\n"
                f"worktree {wt_path}\nHEAD def\nbranch refs/heads/wt-branch\n"
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "status":
            dirty = str(cwd) == wt_path
            return SimpleNamespace(returncode=0, stdout="M x\n" if dirty else "", stderr="")
        raise AssertionError(f"consolidate_assemble census sweep: unexpected git call: {args}")

    decision_object = ca.brief(repo_root=tmp_path, run_git=run_git)
    _gate_nothing_recommendation_carrying(
        decision_object["directives"], decision_object["judgment_points"], "consolidate_assemble/__init__.py", acc
    )


def _sweep_backlog_grind_assemble(acc: dict[str, set[str]]) -> None:
    # Mirrors `_phantom_sweep_providers.sweep_backlog_grind_assemble`'s own
    # process-global-flag snapshot/restore around the `orient_assemble` ->
    # `cc_invoke.py` transitive import (see that function's docstring).
    had_var = "COORDINATOR_CORE_LAZY_OPS" in os.environ
    prior_value = os.environ.get("COORDINATOR_CORE_LAZY_OPS")
    had_attr = hasattr(sys, "_coordinator_core_lazy_ops")
    prior_attr = getattr(sys, "_coordinator_core_lazy_ops", None)
    try:
        from coordinator_core import backlog_grind_assemble as bga
        from coordinator_core.backlog_grind_assemble import CADENCES, brief as bga_brief
    finally:
        if had_var:
            os.environ["COORDINATOR_CORE_LAZY_OPS"] = prior_value  # type: ignore[assignment]
        else:
            os.environ.pop("COORDINATOR_CORE_LAZY_OPS", None)
        if had_attr:
            setattr(sys, "_coordinator_core_lazy_ops", prior_attr)
        else:
            try:
                delattr(sys, "_coordinator_core_lazy_ops")
            except AttributeError:
                pass

    mp = _FakeMonkeyPatch()
    mp.setattr(
        bga,
        "resolve_operator_config",
        lambda: {
            "settings_home": "/fake/settings-home",
            "claude_klabauter_bin": "/fake/settings-home/bin",
            "claude_klabauter_root": "/fake/claude-klabauter-root",
            "doe_root": "/fake/doe-root",
        },
    )
    for cadence in CADENCES:
        result_obj = bga_brief(cadence)
        do = result_obj.decision_object
        _gate_nothing_recommendation_carrying(do["directives"], do["judgment_points"], "backlog_grind_assemble", acc)
    mp.undo()


def _sweep_pickup_assemble(acc: dict[str, set[str]], tmp_path: Path) -> None:
    from coordinator_core.ceremony_common._phantom_sweep_providers import pickup_assemble_variants

    mp = _FakeMonkeyPatch()
    variants = pickup_assemble_variants(mp, tmp_path)
    for variant_name, brief_result in variants:
        decision_object = brief_result.decision_object
        _gate_nothing_recommendation_carrying(
            decision_object["directives"],
            decision_object["judgment_points"],
            f"pickup_assemble/__init__.py [{variant_name}]",
            acc,
        )
    # NOTE, load-bearing (report to EM, not silently swallowed): none of
    # `pickup_assemble_variants`'s representative inputs reach the
    # reply-closure verdict branch (`__init__.py`'s `open`/`unknown` arms
    # building `j-reply-closure`) -- that id is therefore UNCLASSIFIED by
    # this census, neither `asked` nor `reported`. C6 (not C1) owns settling
    # `j-reply-closure` "by construction, not by reading" per the plan, and
    # must add a pickup variant reaching that branch to bring it under this
    # same census. Also unswept here: `j-reply-closure` is built through
    # PICKUP'S OWN forked `build_judgment_point(id, question, evidence,
    # dispositions, recommendation, *, ...)` constructor
    # (`pickup_assemble/__init__.py:6462`), not the contract constructor
    # this module's `partition_reportable` was written against -- whether
    # that forked constructor's output is shaped compatibly is itself part
    # of what C6 settles.


def _sweep_orient_assemble_readers_clean_ops(acc: dict[str, set[str]]) -> None:
    # `readers_branch_reconcile.py` / `readers_health_reaper.py` /
    # `reader_result.py` are NOT swept dynamically here: every
    # `build_judgment_point` call site in those three files passes a
    # LITERAL `recommendation=None` (verified by direct reading, all 6
    # sites) -- structurally excluded from "recommendation-carrying"
    # regardless of asked/reported classification, so there is no branch to
    # construct.
    from coordinator_core.orient_assemble import readers_clean_ops as roco

    mp = _FakeMonkeyPatch()
    # C5's TEST NOTE (load-bearing): `j-em-env-effort`/`j-em-env-model`
    # emit ONLY when effort drifts off `medium` or the model off Opus. An
    # execution sweep under default/ambient conditions observes zero of
    # them and wrongly concludes they don't exist -- force BOTH drift
    # conditions rather than relying on ambient session state.
    mp.setattr(roco, "_resolve_effort", lambda proj, user_claude: ("high", "test-fixture"))
    mp.setattr(roco, "_resolve_transcript", lambda explicit, user_claude, session_id: "fake-transcript.jsonl")
    mp.setattr(roco, "_latest_model", lambda path: "claude-sonnet-5")
    result = roco._read_em_environment()
    # This reader builds no `directives[]` of its own (C5's scope: it feeds
    # `judgment_points[]` only; `__init__.py`'s cadence dispatch owns
    # wiring `directives[]` and is explicitly NOT this chunk's write scope
    # per the plan) -- an empty `directives` list is the correct input, not
    # a stand-in for a real one.
    _gate_nothing_recommendation_carrying([], result.judgment_points, "orient_assemble/readers_clean_ops.py", acc)
    mp.undo()


# ---------------------------------------------------------------------------
# The checked-in three-way ledger (AC2/AC3, C1b correction: "the census
# becomes a three-way ledger... every gate-nothing point is in exactly one
# of: marked reportable (demoted), or marked action-class (keeps asking,
# decided rather than overlooked)"). Computed by running every sweep above
# and reading back each point's own `reportable` marker -- not hand-counted.
#
# `merge_assemble`, `baton_assemble`, `consolidate_assemble`,
# `backlog_grind_assemble`, `review_assemble` each contribute ZERO: every
# `build_judgment_point` call site in those five packages passes a literal
# `recommendation=None` EXCEPT `backlog_grind_assemble.directives.
# build_commit_readiness_gate`, whose `resolves` parameter is a REQUIRED,
# non-defaulted `Sequence[str]` every caller wires to the real directive id
# it gates (own docstring) -- it always gates something, by construction,
# never reaching the gate-nothing set.
# ---------------------------------------------------------------------------

#: `reportable=True` -- acknowledgement-class, demoted into narration.
EXPECTED_REPORTABLE_IDS: frozenset[str] = frozenset(
    {
        # orient_assemble/readers_clean_ops.py (2, matches plan's "orient 2")
        "j-em-env-effort",
        "j-em-env-model",
        # workstream_complete (C2 redo, docs/plans/2026-08-15-judgment-points-
        # that-gate-nothing-stop-being-questions.md): pure recall/backstop-
        # scan/labeling points where the EM merely notes the answer -- no
        # distinct action follows from either disposition, same shape as the
        # plan's own motivating "archived ancestor batons" example.
        "cross-cutting-check",
        "inline-waiver-recognition",
    }
)

#: `reportable=False` -- action-class, explicitly decided to keep asking
#: (dispatch-decision points: the EM dispatches a worker off the answer,
#: with no directive and no gate -- premise-finding sidecar's channel 3).
EXPECTED_ACTION_CLASS_IDS: frozenset[str] = frozenset(
    {
        # workday_complete/brief.py (4, matches plan's "workday-complete 4")
        "jp_step4b_analyst_dispatch",
        "jp_step4c_observer_dispatch",
        "jp_step4_5_clustering_dispatch",
        "jp_step4e_health_ledger_new_rows",
        # workweek_complete/brief.py (both of its 2). extend-span widens what
        # Rule-5 treats as already reviewed and no directive applies it, so it
        # is a `pm-scoped-tradeoff` the EM acts on, not one it merely records.
        "jp_step4_triage_dispatch",
        "jp_step7_rule5_already_reviewed_span",
        # workstream_complete (C2 redo): each answer here obligates the EM to
        # DO something concrete (a disk write, a commit/stash, a dispatch
        # retry, a PM escalation, a widened review) with no directive behind
        # it -- premise-finding sidecar's channel 3.
        "enablement-vs-opportunistic-deferral",
        "finding-tradeoff-escalation-check",
        "flag-severity-classification",
        "governing-spec-identification",
        "lesson-scope-classification",
        "orientation-doc-row-updates",
        "plan-doc-content-update",
        "plan-vs-reality-reconcile",
        "predecessor-distill-fate",
        "quota-retry-vs-escalate",
        "scratch-disposition-per-file",
        "shallow-row3-waive-check",
        "shared-schema-touch-check",
        "unattributable-file-disposition",
    }
)

#: C2 redo (docs/plans/2026-08-15-judgment-points-that-gate-nothing-stop-
#: being-questions.md) classified all 17 previously-deferred workstream-
#: complete ids above (into EXPECTED_REPORTABLE_IDS/EXPECTED_ACTION_CLASS_IDS)
#: -- the deferred allowlist is now empty and REMOVED rather than kept as a
#: standing empty constant (plan instruction: "if it does [end up empty],
#: delete it and say so").
EXPECTED_GATE_NOTHING_RECOMMENDATION_CARRYING_IDS: frozenset[str] = (
    EXPECTED_REPORTABLE_IDS | EXPECTED_ACTION_CLASS_IDS
)


def test_census_of_gate_nothing_recommendation_carrying_judgment_points(monkeypatch, tmp_path):
    """AC3: walks every `build_judgment_point` call site this plan's
    mechanism can reach, constructs each builder, and asserts the resulting
    gate-nothing recommendation-carrying id set equals the checked-in
    three-way ledger above -- every id present must be in EXACTLY one of
    (reportable, action-class, the named deferred-workstream-complete
    allowlist), never in neither. A new gate-nothing point appearing
    anywhere outside all three fails this test with a message naming the id
    and its file/classification -- the artifact AC2 requires: "adding a new
    one without classifying it fails a test." """
    acc: dict[str, dict[str, Any]] = {}

    _sweep_workday_complete(acc)
    _sweep_workweek_complete(acc)
    _sweep_baton_assemble(acc)
    _sweep_merge_assemble(acc)
    _sweep_review_assemble(acc, tmp_path)
    _sweep_workstream_complete(acc, tmp_path)
    _sweep_consolidate_assemble(acc, tmp_path)
    _sweep_backlog_grind_assemble(acc)
    _sweep_pickup_assemble(acc, tmp_path)
    _sweep_orient_assemble_readers_clean_ops(acc)

    computed_ids = frozenset(acc)
    unexpected = computed_ids - EXPECTED_GATE_NOTHING_RECOMMENDATION_CARRYING_IDS
    missing = EXPECTED_GATE_NOTHING_RECOMMENDATION_CARRYING_IDS - computed_ids

    assert not unexpected, (
        "New gate-nothing recommendation-carrying judgment point(s) found, not yet "
        "classified into EXPECTED_REPORTABLE_IDS/EXPECTED_ACTION_CLASS_IDS: "
        + ", ".join(
            f"{jid!r} (built by {sorted(acc[jid]['sources'])}, "
            f"reportable={acc[jid]['reportable']!r})"
            for jid in sorted(unexpected)
        )
    )
    assert not missing, (
        "Previously-classified gate-nothing recommendation-carrying judgment point(s) no "
        f"longer reproduced by this census (id now gates something, lost its recommendation, "
        f"or its builder changed shape) -- update the checked-in ledger if this is "
        f"intentional: {sorted(missing)}"
    )

    # AC2's "cannot rot" property, made explicit: a point in the ledger
    # must carry the `reportable` marker matching its bucket -- a mismatch
    # means the builder's marking drifted from this test's expectation
    # without either being updated together.
    mismatched_reportable = [
        jid
        for jid in sorted(EXPECTED_REPORTABLE_IDS & computed_ids)
        if acc[jid]["reportable"] is not True
    ]
    mismatched_action_class = [
        jid
        for jid in sorted(EXPECTED_ACTION_CLASS_IDS & computed_ids)
        if acc[jid]["reportable"] is not False
    ]
    assert not mismatched_reportable, (
        "Expected reportable=True (acknowledgement-class, demoted) but the builder "
        f"marked otherwise: {mismatched_reportable}"
    )
    assert not mismatched_action_class, (
        "Expected reportable=False (action-class, explicitly decided) but the builder "
        f"marked otherwise: {mismatched_action_class}"
    )

    # The AC2 fail-loud property itself: any gate-nothing recommendation-
    # carrying point must be classified (True or False) -- `None` here
    # means someone added a new gate-nothing point (or removed a
    # `reportable=` kwarg) without ever deciding which bucket it belongs
    # in. No deferred allowlist remains (C2 redo emptied and removed it).
    unclassified = [
        jid
        for jid in sorted(computed_ids)
        if acc[jid]["reportable"] is None
    ]
    assert not unclassified, (
        "Gate-nothing recommendation-carrying judgment point(s) with no explicit "
        "reportable=True/False classification (AC2: adding one without classifying "
        f"it must fail this test): "
        + ", ".join(f"{jid!r} (built by {sorted(acc[jid]['sources'])})" for jid in unclassified)
    )


def test_partition_reportable_precondition_refuses_to_demote_a_depended_on_point():
    """Pinned interface precondition: a point named in ANY directive's
    `depends_on` must never be classified `reported`, even when no
    disposition names a live directive id and even when explicitly marked
    `reportable=True` -- the `depends_on` guard trumps the marker."""
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )

    judgment_points = [
        build_judgment_point(
            {"disposition": "x", "rationale": "y"},
            id="jp-gates-nothing-but-depended-on",
            question="q",
            dispositions=[build_disposition("x", resolves=[])],
            evidence="e",
            reason="r",
            reportable=True,
        )
    ]
    directives = [
        {"id": "d1", "cli": "noop", "args": [], "depends_on": ["jp-gates-nothing-but-depended-on"]},
    ]
    asked, reported = partition_reportable(judgment_points, directives)
    assert [p["id"] for p in asked] == ["jp-gates-nothing-but-depended-on"]
    assert reported == []


def test_partition_reportable_does_not_swallow_a_phantom_resolves_id_into_reported():
    """A `resolves` id naming a directive absent from the envelope entirely
    is out of `partition_reportable`'s vocabulary per the plan's
    Definitional call -- verified here as: the base rule (gate-nothing AND
    explicitly marked `reportable=True`) classifies it `reported` (nothing
    more this predicate can do without duplicating
    `phantom_resolves_sweep.py`), and that guard -- not this function -- is
    what must catch an ACCIDENTAL instance before it ever reaches this
    predicate on a shipped envelope."""
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )
    from coordinator_core.ceremony_common.phantom_resolves_sweep import (
        PhantomSweepResult,
        assert_no_phantom_resolves_ids,
    )

    judgment_points = [
        build_judgment_point(
            {"disposition": "x", "rationale": "y"},
            id="jp-phantom",
            question="q",
            dispositions=[build_disposition("x", resolves=["d-does-not-exist"])],
            evidence="e",
            reason="r",
            reportable=True,
        )
    ]
    directives: list[dict[str, Any]] = []
    _asked, reported = partition_reportable(judgment_points, directives)
    assert [p["id"] for p in reported] == ["jp-phantom"]

    result = PhantomSweepResult(
        directive_ids=frozenset(),
        resolves_ids=frozenset({"d-does-not-exist"}),
        judgment_point_ids=frozenset({"jp-phantom"}),
    )
    try:
        assert_no_phantom_resolves_ids(result, package_name="test_reportable_partition")
    except AssertionError:
        pass
    else:
        raise AssertionError(
            "phantom_resolves_sweep.py did not fail the build on an id absent from "
            "directives[] -- the guard partition_reportable relies on to keep an "
            "accidental phantom id from reaching 'reported' is not doing its job"
        )


def _minimal_envelope(**overrides: Any) -> dict[str, Any]:
    from coordinator_core.contract.decision_object.envelope import build_envelope

    return build_envelope(**overrides)


def test_emit_raises_on_an_unclassified_gate_nothing_recommendation_carrying_point():
    """AC2, runtime half: `_emit` raises `DecisionObjectError` when an
    envelope carries a recommendation-carrying judgment point that gates
    nothing on the directive axis and whose `reportable` is left `None`
    (unclassified) -- adding a new one without classifying it must fail,
    not just at census time (`test_census_of_gate_nothing_recommendation_
    carrying_judgment_points` above) but at the `_emit` chokepoint every
    `build_envelope` producer routes through."""
    from coordinator_core.contract.decision_object.envelope import (
        DecisionObjectError,
        _emit,
    )
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )

    jp = build_judgment_point(
        {"disposition": "x", "rationale": "y"},
        id="jp-unclassified-gate-nothing",
        question="q",
        dispositions=[build_disposition("x", resolves=[])],
        evidence="e",
        reason="r",
        # reportable left at its default (None) -- deliberately unclassified.
    )
    envelope = _minimal_envelope(judgment_points=[jp], directives=[])

    try:
        _emit(envelope)
    except DecisionObjectError as exc:
        assert "jp-unclassified-gate-nothing" in str(exc)
    else:
        raise AssertionError(
            "_emit did not raise on an unclassified gate-nothing "
            "recommendation-carrying judgment point"
        )


def test_emit_does_not_raise_on_a_reportable_true_gate_nothing_point():
    """Acknowledgement-class (`reportable=True`): `_emit` must NOT raise --
    this is the legal, demoted-into-narration case, not a defect."""
    from coordinator_core.contract.decision_object.envelope import _emit
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )

    jp = build_judgment_point(
        {"disposition": "x", "rationale": "y"},
        id="jp-reportable-true-gate-nothing",
        question="q",
        dispositions=[build_disposition("x", resolves=[])],
        evidence="e",
        reason="r",
        reportable=True,
    )
    envelope = _minimal_envelope(judgment_points=[jp], directives=[])

    result = _emit(envelope)
    assert result is envelope


def test_emit_does_not_raise_on_a_reportable_false_gate_nothing_point():
    """Action-class (`reportable=False`): `_emit` must NOT raise -- this is
    a gate-nothing point deliberately kept asking, not an unclassified one.
    Getting this wrong makes every workday/workweek/wsc ceremony throw on
    a legitimate, decided point."""
    from coordinator_core.contract.decision_object.envelope import _emit
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )

    jp = build_judgment_point(
        {"disposition": "x", "rationale": "y"},
        id="jp-reportable-false-gate-nothing",
        question="q",
        dispositions=[build_disposition("x", resolves=[])],
        evidence="e",
        reason="r",
        reportable=False,
    )
    envelope = _minimal_envelope(judgment_points=[jp], directives=[])

    result = _emit(envelope)
    assert result is envelope


def test_emit_does_not_raise_on_a_depended_on_unclassified_point():
    """A point named in a directive's `depends_on` is legitimately `asked`
    regardless of its `reportable` marker -- `_emit` must not raise on it
    even when unclassified, matching `partition_reportable`'s own
    precondition (mirrored via `find_unclassified_gate_nothing_points`)."""
    from coordinator_core.contract.decision_object.envelope import _emit
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_judgment_point,
    )

    jp = build_judgment_point(
        {"disposition": "x", "rationale": "y"},
        id="jp-gates-nothing-but-depended-on",
        question="q",
        dispositions=[build_disposition("x", resolves=[])],
        evidence="e",
        reason="r",
    )
    directives = [
        {"id": "d1", "cli": "noop", "args": [], "depends_on": ["jp-gates-nothing-but-depended-on"]},
    ]
    envelope = _minimal_envelope(judgment_points=[jp], directives=directives)

    result = _emit(envelope)
    assert result is envelope


def test_emit_does_not_raise_on_a_non_recommendation_carrying_point():
    """`build_untrusted_gate_judgment_point` never carries a
    `recommendation` by construction -- it is out of AC2's vocabulary
    entirely and must not trip the invariant even when unclassified and
    gate-nothing."""
    from coordinator_core.contract.decision_object.envelope import _emit
    from coordinator_core.contract.decision_object.judgment import (
        build_disposition,
        build_untrusted_gate_judgment_point,
    )

    jp = build_untrusted_gate_judgment_point(
        id="jp-untrusted-gate-nothing",
        question="q",
        dispositions=[build_disposition("x", resolves=[])],
        evidence="e",
        reason="r",
    )
    envelope = _minimal_envelope(judgment_points=[jp], directives=[])

    result = _emit(envelope)
    assert result is envelope


def test_emit_does_not_raise_on_the_five_resolver_backed_points_under_empty_decisions():
    """2026-08-15 C10 follow-up (EM ruling): the five resolver-backed
    points -- whose `resolves` is populated from a `decisions` slice, not
    authored statically -- must NOT trip `_emit`'s invariant even when
    built under an empty `decisions` slice (the exact condition that made
    `resolves` come back `[]` and previously triggered a false positive).
    `resolves_computed=True` is what makes `_emit` tolerate them; this is
    the regression this dispatch's own run caught."""
    from coordinator_core.contract.decision_object.envelope import _emit
    from coordinator_core.workstream_complete.judgments import (
        build_lesson_worth_capturing_judgment_point,
        build_memo_resolution_attribution_judgment_point,
        build_review_dispatch_vehicle_choice_judgment_point,
        build_review_partition_strategy_judgment_point,
        build_reviewer_count_on_oracle_disagreement_judgment_point,
    )

    points = [
        build_lesson_worth_capturing_judgment_point(capture_resolves_ids=[]),
        build_memo_resolution_attribution_judgment_point(
            resolved_resolves_ids=[], attribution_signals=[]
        ),
        build_review_partition_strategy_judgment_point(partition_resolves_ids=[]),
        build_reviewer_count_on_oracle_disagreement_judgment_point(
            partition_resolves_ids=[]
        ),
        build_review_dispatch_vehicle_choice_judgment_point(partition_resolves_ids=[]),
    ]
    envelope = _minimal_envelope(judgment_points=points, directives=[])

    result = _emit(envelope)
    assert result is envelope


#: Exactly the census's own `_RESOLVER_BACKED_OUT_OF_SCOPE_IDS` set -- kept
#: as a SEPARATE pin (not derived from that constant) so a change to either
#: one is visible as a diff to the other, not silently kept in sync.
_EXPECTED_RESOLVES_COMPUTED_IDS: frozenset[str] = frozenset(
    {
        "lesson-worth-capturing",
        "memo-resolution-attribution",
        "review-partition-strategy",
        "reviewer-count-on-oracle-disagreement",
        "review-dispatch-vehicle-choice",
    }
)


def test_resolves_computed_marker_is_pinned_to_exactly_the_resolver_backed_five():
    """`resolves_computed=True` is legal only on a point whose `resolves`
    is genuinely resolver-populated -- not a general escape hatch a future
    author can reach for to silence a real unclassified point. Pinned via
    static grep over the constructor source (not execution): every
    `resolves_computed=True` call-site keyword must sit inside one of
    exactly these five `id="..."` builder bodies, and every one of the
    five must actually carry the marker.

    Deliberately a SEPARATE assertion from the census's own
    `_RESOLVER_BACKED_OUT_OF_SCOPE_IDS` exclusion (see this module's
    top-of-file comment and `find_unclassified_gate_nothing_points`'s
    docstring): the census asserts the five are excluded by identity: this
    test asserts the runtime marker's placement is exactly as narrow. A
    sixth resolver-backed point carrying the marker but missing from the
    census list is still visible -- it fails the census test's `missing`
    branch, not silently absorbed here.
    """
    import re

    judgments_path = (
        Path(__file__).resolve().parents[3] / "workstream_complete" / "judgments.py"
    )
    source = judgments_path.read_text(encoding="utf-8")

    # Every top-level `def build_..._judgment_point(...)` body, sliced from
    # its own `def` line to the next `def` line (or EOF).
    def_starts = [m.start() for m in re.finditer(r"^def build_\w+\(", source, re.MULTILINE)]
    def_starts.append(len(source))

    carrying_ids: set[str] = set()
    for i in range(len(def_starts) - 1):
        body = source[def_starts[i] : def_starts[i + 1]]
        if "resolves_computed=True" not in body:
            continue
        id_match = re.search(r'id="([^"]+)"', body)
        assert id_match, (
            "a build_..._judgment_point body carries resolves_computed=True "
            "but this test could not find its id=\"...\" literal -- "
            f"body starts: {body[:120]!r}"
        )
        carrying_ids.add(id_match.group(1))

    assert carrying_ids == _EXPECTED_RESOLVES_COMPUTED_IDS, (
        "resolves_computed=True must be spelled on exactly the five resolver-backed "
        f"ids: extra={sorted(carrying_ids - _EXPECTED_RESOLVES_COMPUTED_IDS)} "
        f"missing={sorted(_EXPECTED_RESOLVES_COMPUTED_IDS - carrying_ids)}"
    )


# ---------------------------------------------------------------------------
# Regression pin: `jp-review-scale`'s resolved branch (2026-08-15, live
# incident reported mid-`/workstream-complete` by a peer repo's EM).
# `build_review_scale_judgment_point`'s RESOLVED branch builds a
# recommendation-carrying point (`acknowledge-scale`, `resolves=[]`) that
# gates nothing on the directive axis (DR-068 keeps `d-run-wsc-tail` free of
# any dependency edge on it) -- exactly the shape `_emit`'s
# `find_unclassified_gate_nothing_points` backstop refuses when
# `reportable` is left unset. The three UNRESOLVED branches build
# `build_untrusted_gate_judgment_point` points instead, which never carry a
# `recommendation` by construction and are therefore always out of that
# backstop's vocabulary regardless of any `reportable` marker -- so this
# defect could only ever fire on the resolved branch, and it fired
# precisely when a chain-terminal decision transitioned from unresolved
# (working) to resolved (raising): an EM discharging the review gate by
# writing review-trail records is what flips `decide_review_scale` from
# unresolved to resolved on rows 5/6, so compliance with
# PARTITION-MANDATORY is what broke `brief()`/`apply()`, with no
# `--decisions` escape since the raise happens inside `_emit` before
# decisions are ever consulted.
# ---------------------------------------------------------------------------

from coordinator_core import workstream_complete as _wsc_regression
from coordinator_core.contract.decision_object.envelope import _emit as _regression_emit
from coordinator_core.workstream_complete.directives_review import decide_review_scale

_REVIEW_SCALE_RESOLVED_SMALL: dict[str, Any] = dict(
    gross_loc=10,
    code_loc=10,
    commit_count=1,
    surface_count=1,
    executor_dispatched=False,
    shared_schema_touched=False,
)


#: Row 6 and the `verdict_presence` tri-state these regressions originally
#: drove through are gone (state/kill-ledger.md K-007, 2026-08-19). The
#: CONTRACT they guard is untouched — a resolved chain-terminal review-scale
#: point must not make `_emit` raise — so they are re-driven through the
#: surviving rows rather than deleted: row 4 for the resolved
#: partition-mandatory state row 6 used to supply, row 5 for resolved
#: non-mandatory, and an unresolved row-4 input for the unresolved state.
_REVIEW_SCALE_RESOLVED_BIG: dict[str, Any] = dict(
    gross_loc=600,
    code_loc=600,
    commit_count=9,
    surface_count=5,
    executor_dispatched=False,
    shared_schema_touched=False,
)


def test_resolved_chain_terminal_review_scale_point_survives_emit():
    """AC-1/regression proper: a RESOLVED `ReviewScaleDecision` on a
    chain-terminal partition-mandatory row must produce a judgment point
    that `_emit` accepts -- asserted on the envelope emitting successfully
    (never raising `DecisionObjectError`), not merely on the `reportable`
    key's presence: the key is today's implementation detail, the contract
    is that the envelope does not raise. Row 4 since K-007; row 6 before."""
    decision = decide_review_scale(
        **_REVIEW_SCALE_RESOLVED_BIG,
        chain_disposition="predecessor-consumed",
    )
    assert decision.resolved is True
    assert decision.row == 4
    assert decision.partition_mandatory is True

    jp = _wsc_regression.build_review_scale_judgment_point(decision, chain_terminal=True)
    assert jp is not None
    assert jp["id"] == "jp-review-scale"
    assert jp.get("recommendation") is not None

    envelope = _minimal_envelope(judgment_points=[jp], directives=[])
    result = _regression_emit(envelope)
    assert result is envelope


def test_unresolved_review_scale_branches_are_not_marked_reportable():
    """AC-2/asymmetry: the UNRESOLVED branch must NOT be marked
    `reportable=True` -- a blanket `reportable=True` would also make
    `_emit` stop raising and would be the wrong fix (the branch carries
    real dispositions the EM must still act on, not a pure
    acknowledgement). This test fails if someone "fixes" the defect that
    way. The pending/unreadable variants died with the verdict store
    (K-007); the generic untrusted-gate branch they flanked is the one
    that survives, and it is the one this asymmetry was always about."""
    unresolved_decision = decide_review_scale(
        **{**_REVIEW_SCALE_RESOLVED_SMALL, "code_loc": None},
        chain_disposition="predecessor-consumed",
    )
    assert unresolved_decision.resolved is False

    chain_jp = _wsc_regression.build_review_scale_judgment_point(
        unresolved_decision, chain_terminal=True
    )
    generic_jp = _wsc_regression.build_review_scale_judgment_point(
        unresolved_decision, chain_terminal=False
    )

    for jp in (chain_jp, generic_jp):
        assert jp is not None
        assert jp.get("recommendation") is None
        assert jp.get("reportable") is not True


def test_review_scale_transition_from_unresolved_to_resolved_never_starts_raising():
    """AC-3/the actual bug: the SAME envelope shape, moving from unresolved
    (working) to resolved (the state DISCHARGING the review gate produces)
    must not turn a working `brief()`-shaped envelope into a raising one --
    this is the exact state transition the live incident reproduced:
    `brief()` succeeded while `jp-review-scale` was unresolved, the EM then
    wrote review-trail records that discharged the review gate, and the
    NEXT close's `decide_review_scale` call resolved the same chain-terminal
    row and raised."""
    unresolved_decision = decide_review_scale(
        **{**_REVIEW_SCALE_RESOLVED_SMALL, "code_loc": None},
        chain_disposition="predecessor-consumed",
    )
    unresolved_jp = _wsc_regression.build_review_scale_judgment_point(
        unresolved_decision, chain_terminal=True
    )
    unresolved_envelope = _minimal_envelope(judgment_points=[unresolved_jp], directives=[])
    assert _regression_emit(unresolved_envelope) is unresolved_envelope

    resolved_decision = decide_review_scale(
        **_REVIEW_SCALE_RESOLVED_BIG,
        chain_disposition="predecessor-consumed",
    )
    resolved_jp = _wsc_regression.build_review_scale_judgment_point(
        resolved_decision, chain_terminal=True
    )
    resolved_envelope = _minimal_envelope(judgment_points=[resolved_jp], directives=[])
    # This is the regression: pre-fix, this call raised `DecisionObjectError`
    # for exactly the reason a discharged review gate produces a resolved
    # decision -- i.e. the transition an EM's compliance triggers.
    assert _regression_emit(resolved_envelope) is resolved_envelope
