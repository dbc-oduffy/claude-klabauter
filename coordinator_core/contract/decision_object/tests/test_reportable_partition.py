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

from coordinator_core.contract.decision_object.judgment import partition_reportable


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


def _gate_nothing_recommendation_carrying(
    directives: list[dict[str, Any]],
    judgment_points: list[dict[str, Any]],
    source_label: str,
    acc: dict[str, set[str]],
) -> None:
    _asked, reported = partition_reportable(judgment_points, directives)
    for jp in reported:
        if jp.get("recommendation") is not None and jp["id"] not in _RESOLVER_BACKED_OUT_OF_SCOPE_IDS:
            acc.setdefault(jp["id"], set()).add(source_label)


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
        _gate("chain-terminal", consumed_handoff="state/handoffs/x.md", consumed_handoff_paths=()),
        _gate("single-session", consumed_handoff_paths=()),
    )
    for gate in gate_variants:
        mp = _FakeMonkeyPatch()
        _patch_gate(mp, gate)
        decision_object = wsc.brief(decisions=decisions, repo_root=tmp_path)
        _gate_nothing_recommendation_carrying(
            decision_object["directives"], decision_object["judgment_points"], "workstream_complete", acc
        )
        mp.undo()

    # C1(c)'s bounded determination needs `jp-coverage-verdict` built via the
    # branch where `decisions["review"]` is ABSENT -- the only path that
    # exercises `build_coverage_judgment_point`'s legacy fallback
    # (`write_trail_ids = ["d-write-trail"]` with no real `d-write-trail*`
    # directive built). The populated-`decisions` sweep above never reaches
    # this branch. See this file's own `test_jp_coverage_verdict_...` below
    # for the determination itself; swept into the census here too, since a
    # gate-nothing point reachable ONLY on this branch is still a real site.
    for gate in gate_variants:
        mp = _FakeMonkeyPatch()
        _patch_gate(mp, gate)
        decision_object = wsc.brief(decisions={}, repo_root=tmp_path)
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
# The checked-in expected set (AC3: "the number lives here, never in
# prose"). Computed by running every sweep above and reading back what
# `partition_reportable` actually classified -- not hand-counted.
#
# `merge_assemble`, `baton_assemble`, `consolidate_assemble`,
# `backlog_grind_assemble`, `review_assemble` each contribute ZERO: every
# `build_judgment_point` call site in those five packages passes a literal
# `recommendation=None` EXCEPT `backlog_grind_assemble.directives.
# build_commit_readiness_gate`, whose `resolves` parameter is a REQUIRED,
# non-defaulted `Sequence[str]` every caller wires to the real directive id
# it gates (own docstring) -- it always gates something, by construction,
# never reaching the `reported` branch.
# ---------------------------------------------------------------------------
EXPECTED_GATE_NOTHING_RECOMMENDATION_CARRYING_IDS: frozenset[str] = frozenset(
    {
        # workday_complete/brief.py (4, matches plan's "workday-complete 4")
        "jp_step4b_analyst_dispatch",
        "jp_step4c_observer_dispatch",
        "jp_step4_5_clustering_dispatch",
        "jp_step4e_health_ledger_new_rows",
        # workweek_complete/brief.py (2, matches plan's "workweek-complete 2")
        "jp_step4_triage_dispatch",
        "jp_step7_rule5_already_reviewed_span",
        # orient_assemble/readers_clean_ops.py (2, matches plan's "orient 2")
        "j-em-env-effort",
        "j-em-env-model",
        # workstream_complete (via judgments.py + __init__.py; resolver-backed
        # points excluded per _RESOLVER_BACKED_OUT_OF_SCOPE_IDS above)
        "cross-cutting-check",
        "enablement-vs-opportunistic-deferral",
        "finding-tradeoff-escalation-check",
        "flag-severity-classification",
        "governing-spec-identification",
        "inline-waiver-recognition",
        "jp-coverage-verdict",
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


def test_census_of_gate_nothing_recommendation_carrying_judgment_points(monkeypatch, tmp_path):
    """AC3: walks every `build_judgment_point` call site this plan's
    mechanism can reach, constructs each builder, and asserts the resulting
    gate-nothing recommendation-carrying id set equals the checked-in
    expected set above. A new one appearing anywhere fails with a message
    naming the id and its file -- the artifact that discharges the "a
    question that cannot change an outcome should not be asked" rule."""
    acc: dict[str, set[str]] = {}

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
        "New gate-nothing recommendation-carrying judgment point(s) found, not yet in "
        f"EXPECTED_GATE_NOTHING_RECOMMENDATION_CARRYING_IDS: "
        + ", ".join(f"{jid!r} (built by {sorted(acc[jid])})" for jid in sorted(unexpected))
    )
    assert not missing, (
        "Previously-classified gate-nothing recommendation-carrying judgment point(s) no "
        f"longer reproduced by this census (id now gates something, lost its recommendation, "
        f"or its builder changed shape) -- update the checked-in expected set if this is "
        f"intentional: {sorted(missing)}"
    )


def test_partition_reportable_precondition_refuses_to_demote_a_depended_on_point():
    """Pinned interface precondition: a point named in ANY directive's
    `depends_on` must never be classified `reported`, even when no
    disposition names a live directive id."""
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
    Definitional call -- verified here as: the base rule classifies it
    `reported` (nothing more this predicate can do without duplicating
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


def test_jp_coverage_verdict_d_write_trail_is_uncovered_by_the_existing_sweep():
    """C1(c) BOUNDED DETERMINATION: the plan's Definitional call claims
    `jp-coverage-verdict`'s literal `d-write-trail` is a deliberate
    back-compat dead reference, but `phantom_resolves_sweep.py` has no
    allowlist/exemption mechanism for it (`d-write-trail` is absent from
    `test_workstream_complete.py`'s own `_NO_DIRECTIVE_BACKING_RESOLVES_IDS`,
    the local guard `_sweep_directive_ids_and_resolves_ids`'s regression
    feeds).

    Settled by construction: `build_coverage_judgment_point`'s fallback
    branch (`write_trail_ids = ["d-write-trail"]`, taken when `d-coverage-
    gate` is present but no real `d-write-trail*` directive was built) DOES
    trip the guard when actually exercised -- proven below. The reason the
    guard has never failed on it in CI is that `_sweep_directive_ids_and_
    resolves_ids`'s `decisions` payload always populates `review` (a dict),
    so `build_write_trail_directives` always emits a real `d-write-trail`
    directive and the fallback branch is never reached by that sweep's
    axes -- a COVERAGE GAP in the existing sweep, not evidence the
    reference is safe. Verdict: the "deliberate back-compat dead reference"
    framing is not supported by anything that currently runs; `d-write-
    trail` is a latent defect already owned by `phantom_resolves_sweep.py`
    the day a sweep actually reaches that branch, not a documented
    exemption."""
    from coordinator_core.workstream_complete import build_coverage_judgment_point
    from coordinator_core.ceremony_common.phantom_resolves_sweep import (
        PhantomSweepResult,
        assert_no_phantom_resolves_ids,
    )

    directives = [{"id": "d-coverage-gate", "cli": "x", "args": [], "depends_on": None, "already_satisfied": False}]
    jp = build_coverage_judgment_point(gate=None, directives=directives)
    assert jp["id"] == "jp-coverage-verdict"

    resolves_ids: set[str] = set()
    for disposition in jp["dispositions"]:
        resolves_ids.update(disposition.get("resolves") or [])
    assert "d-write-trail" in resolves_ids

    result = PhantomSweepResult(
        directive_ids=frozenset(d["id"] for d in directives),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset([jp["id"]]),
    )
    raised = False
    try:
        assert_no_phantom_resolves_ids(result, package_name="jp_coverage_verdict_fallback_repro")
    except AssertionError:
        raised = True
    assert raised, (
        "expected phantom_resolves_sweep.py to fail on jp-coverage-verdict's fallback "
        "d-write-trail reference when no real d-write-trail directive backs it -- if this "
        "no longer raises, the 'deliberate back-compat dead reference' framing may have "
        "been made true by an allowlist added elsewhere; re-examine before trusting it"
    )
