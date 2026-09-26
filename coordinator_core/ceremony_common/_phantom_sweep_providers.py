"""coordinator_core.ceremony_common._phantom_sweep_providers — per-package
representative-sweep adapters feeding `test_phantom_resolves_id_sweep.py`.

Why this file exists (rather than one universal sweep in `phantom_resolves_
sweep.py`): each assembler package's `brief()` is architecturally distinct
enough — different signatures, different disk/git dependence, different
conditional-emission axes — that a single generic sweep cannot honestly
construct valid input for all of them. `workstream_complete`'s own sweep
(`test_workstream_complete._sweep_directive_ids_and_resolves_ids`) already
proved this: it needed gate monkeypatching, disk fixtures (a real plan +
handoff), and a hand-widened `decisions` payload. This module holds one
adapter per covered package instead, each calling that package's OWN
`brief()`/pure-builder functions with representative, hand-verified inputs
— cheap where the builders are pure (no disk/mock needed: `workday_complete`,
`workweek_complete`, `baton_assemble`, `merge_assemble`), heavier where a
`run_git`-injectable seam exists (`consolidate_assemble`), or a real (but
read-only, empty-safe) disk/git read (`backlog_grind_assemble`,
`orient_assemble`).

Coverage note (2026-07-27, deferral closed): `pickup_assemble` was
DELIBERATELY NOT covered here through the 2026-07-27 generalization pass
(named in `test_phantom_resolves_id_sweep.py`'s `_DEFERRED_ALLOWLIST`) —
a same-day follow-up dispatch closed that deferral with
`sweep_pickup_assemble` below, the last entry in this file. `_DEFERRED_
ALLOWLIST` is now empty. `learn_lessons_assemble` and
`orient_assemble` are covered by the "verified resolves-free" static/
dynamic checks in `test_phantom_resolves_id_sweep.py` rather than a
provider here, since none of their `build_disposition` call sites ever
pass a non-empty `resolves` — there is no phantom-id risk to sweep, only a
claim to keep honest against regression.

Coverage note (2026-07-27, dynamic conversion): `review_assemble` was
previously in that same "verified resolves-free" bucket, checked by a
static source-text scan of `residue.py` asserting it always emits
`directives=[]` and never passes a non-empty `resolves=`. That pin was
honest only while `residue.py`'s only judgment point ever built a
`resolves`-free disposition — an approved, part-executed plan
(DoE-claude `docs/plans/2026-07-26-review-skill-computed-residue.md`; its
C3/C4/C5/C13 rows landed here in `0859fb56`, the rest are outstanding)
will change that, and a static pin cannot notice its own premise going
stale. `sweep_review_assemble` below replaces it with a real dynamic
sweep — swept across surface variants, introspecting whatever `brief()`
actually emits rather than asserting today's shape — exactly the standing
this file's other providers hold.

Spec backlink: cross-repo/inbox/2026-07-27-… "Generalize seam guards
fleet-wide" dispatch (DoE-claude, 2026-07-27).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coordinator_core.ceremony_common.phantom_resolves_sweep import PhantomSweepResult

GENERATES = []


def _collect(directives: list[dict[str, Any]], judgment_points: list[dict[str, Any]]) -> PhantomSweepResult:
    directive_ids = {d["id"] for d in directives}
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
    for jp in judgment_points:
        judgment_point_ids.add(jp["id"])
        for disposition in jp["dispositions"]:
            resolves_ids.update(disposition.get("resolves", []))
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


def sweep_workday_complete() -> PhantomSweepResult:
    from coordinator_core.workday_complete import brief as wd_brief

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
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
            result = _collect(directives, judgment_points)
            directive_ids |= result.directive_ids
            resolves_ids |= result.resolves_ids
            judgment_point_ids |= result.judgment_point_ids
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


def sweep_workweek_complete() -> PhantomSweepResult:
    from coordinator_core.workweek_complete import brief as ww_brief

    directives = ww_brief._build_directives()
    judgment_points = ww_brief._build_judgment_points()
    return _collect(directives, judgment_points)


def sweep_workstream_complete(monkeypatch: Any, tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core.workstream_complete.test_workstream_complete import (
        _sweep_directive_ids_and_resolves_ids,
    )

    directive_ids, resolves_ids, judgment_point_ids = _sweep_directive_ids_and_resolves_ids(monkeypatch, tmp_path)
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


def sweep_baton_assemble() -> PhantomSweepResult:
    from coordinator_core import baton_assemble as ba

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
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
            result = _collect(directives, judgment_points)
            directive_ids |= result.directive_ids
            resolves_ids |= result.resolves_ids
            judgment_point_ids |= result.judgment_point_ids
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


def sweep_merge_assemble() -> PhantomSweepResult:
    from coordinator_core import merge_assemble as ma

    directives = ma.build_directives(Path("repo"), tag_prefix="v", proposed_tag="v1.2.3")
    judgment_points = ma.build_judgment_points()
    return _collect(directives, judgment_points)


def sweep_consolidate_assemble(tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core import consolidate_assemble as ca

    wt_path = str(tmp_path / "wt")

    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        # AssertionError. Dispatch on the SUBCOMMAND, never on raw argv[0].
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
        if args[0] == "for-each-ref":
            rows = "".join(
                f"refs/heads/{n}\t{n}\tme@x\n" for n in ("current", "main", "stale")
            )
            return SimpleNamespace(returncode=0, stdout=rows, stderr="")
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
        raise AssertionError(f"consolidate_assemble sweep: unexpected git call: {args}")

    decision_object = ca.brief(repo_root=tmp_path, run_git=run_git)
    return _collect(decision_object["directives"], decision_object["judgment_points"])


def sweep_backlog_grind_assemble(monkeypatch: Any) -> PhantomSweepResult:
    # `COORDINATOR_CORE_LAZY_OPS` env var and the `sys._coordinator_core_lazy_
    from coordinator_core import backlog_grind_assemble as bga
    from coordinator_core.backlog_grind_assemble import CADENCES, brief as bga_brief

    monkeypatch.setattr(
        bga,
        "resolve_operator_config",
        lambda: {
            "settings_home": "/fake/settings-home",
            "claude_klabauter_bin": "/fake/settings-home/bin",
            "claude_klabauter_root": "/fake/claude-klabauter-live-root",
            "doe_root": "/fake/doe-root",
        },
    )

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
    for cadence in CADENCES:
        result_obj = bga_brief(cadence)
        do = result_obj.decision_object
        result = _collect(do["directives"], do["judgment_points"])
        directive_ids |= result.directive_ids
        resolves_ids |= result.resolves_ids
        judgment_point_ids |= result.judgment_point_ids
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


# residue dir carrying one segment per `SEGMENT_SURFACES` value (`plan`,


def sweep_review_assemble(monkeypatch: Any, tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core.review_assemble import residue as residue_mod

    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "review" / "residue"
    residue_dir.mkdir(parents=True)
    (residue_dir / "010-shared.md").write_text(
        "---\nsegment_id: shared-seg\nsurface: shared\nclass: protected\norder: 0\n---\nShared.\n",
        encoding="utf-8", newline="\n",
    )
    (residue_dir / "020-plan.md").write_text(
        "---\nsegment_id: plan-seg\nsurface: plan\nclass: droppable\norder: 1\n---\nPlan.\n",
        encoding="utf-8", newline="\n",
    )
    (residue_dir / "030-diff.md").write_text(
        "---\nsegment_id: diff-seg\nsurface: diff\nclass: droppable\norder: 2\n---\nDiff.\n",
        encoding="utf-8", newline="\n",
    )
    monkeypatch.setattr(residue_mod, "resolve_content_root", lambda: str(content_root))

    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    call_variants: list[dict[str, Any]] = [
        {"artifact_arg": None, "explicit_surface": "plan"},
        {"artifact_arg": None, "explicit_surface": "diff"},
        {"artifact_arg": None, "explicit_surface": None},
    ]

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
    for variant in call_variants:
        decision_object = residue_mod.brief(
            variant["artifact_arg"],
            repo_root=repo_root,
            explicit_surface=variant["explicit_surface"],
        )
        result = _collect(decision_object["directives"], decision_object["judgment_points"])
        directive_ids |= result.directive_ids
        resolves_ids |= result.resolves_ids
        judgment_point_ids |= result.judgment_point_ids
    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


# `test_phantom_resolves_id_sweep.py`'s `_DEFERRED_ALLOWLIST` (2026-07-27
# PER-DECISION-OBJECT CHECKING, not union-only (2026-07-27 dispatch
# DIFFERENT variant's directives (the main handoff path, same package).
# into the unioned return that `_PROVIDERS`'s uniform contract still


def pickup_assemble_variants(monkeypatch: Any, tmp_path: Path) -> list[tuple[str, Any]]:
    """Builds the representative `(variant_name, BriefResult)` pairs
    `sweep_pickup_assemble` sweeps -- split out from that function (rather
    than inlined) so `test_phantom_resolves_id_sweep.py` can assert
    classification/kind COVERAGE directly (every variant name this
    function can ever produce, not just the unioned id-set its caller
    reduces down to) as its own explicit, named-regression test, per the
    2026-07-27 follow-up dispatch's own instruction: "so a future
    regression to partial coverage fails rather than passes."""
    from coordinator_core import pickup_brief as pb
    from coordinator_core.test_pickup_assemble import _init_repo, _seed_handoff, _seed_memo

    repo = tmp_path / "repo"
    _init_repo(repo)

    variants: list[tuple[str, Any]] = []


    _seed_handoff(repo, "h-normal.md")
    variants.append(("handoff-normal", pb.brief("state/handoffs/h-normal.md", repo_root=repo)))

    _seed_handoff(repo, "h-awaiting-gate.md", deployment_state="awaiting_gate")
    variants.append(("handoff-awaiting-gate", pb.brief("state/handoffs/h-awaiting-gate.md", repo_root=repo)))

    _seed_handoff(repo, "h-shipped.md", deployment_state="shipped")
    variants.append(("handoff-shipped", pb.brief("state/handoffs/h-shipped.md", repo_root=repo)))

    _seed_handoff(repo, "s-normal.md", kind="spinoff")
    variants.append(("spinoff-normal", pb.brief("state/handoffs/s-normal.md", repo_root=repo)))

    _seed_handoff(repo, "h-liveness.md")
    monkeypatch.setattr(pb, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("handoff-liveness-fired", pb.brief("state/handoffs/h-liveness.md", repo_root=repo)))
    monkeypatch.undo()

    _seed_handoff(repo, "h-live-claim.md")
    monkeypatch.setattr(pb, "gates_claim", lambda *a, **k: {"fetch_state": "ok", "holder": "live-peer-sid"})
    monkeypatch.setattr(
        pb,
        "compute_claim_grant",
        lambda *a, **k: {
            "fetch_state": "ok",
            "holder": "live-peer-sid",
            "holder_live": True,
            "verdict": "denied",
            "reason": "held by a live peer",
            "claim_age_minutes": None,
            "drop_invocation": "",
        },
    )
    monkeypatch.setattr(pb, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("handoff-live-claim-bail", pb.brief("state/handoffs/h-live-claim.md", repo_root=repo)))
    monkeypatch.undo()

    # `_KIND_DISPOSITIONS` key -- `bug` added 2026-09-22: the disposition
    # bug-degrades-silently.yaml): `_KIND_DISPOSITIONS` gained a `friction`
    # `_KIND_DISPOSITIONS` entry (mirrors `fyi`'s non-premise-bearing

    for kind in ("ask", "consult", "proposal", "fyi", "bug", "friction"):
        name = f"m-{kind}.md"
        _seed_memo(repo, name, kind=kind)
        variants.append((f"memo-kind-{kind}", pb.brief(f"cross-repo/inbox/{name}", repo_root=repo)))

    _seed_memo(repo, "m-liveness.md", kind="ask")
    monkeypatch.setattr(pb, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("memo-liveness-fired", pb.brief("cross-repo/inbox/m-liveness.md", repo_root=repo)))
    monkeypatch.undo()

    return variants


def sweep_pickup_assemble(monkeypatch: Any, tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core.ceremony_common.phantom_resolves_sweep import resolves_id_is_satisfiable

    variants = pickup_assemble_variants(monkeypatch, tmp_path)

    directive_ids: set[str] = set()
    resolves_ids: set[str] = set()
    judgment_point_ids: set[str] = set()
    for variant_name, brief_result in variants:
        decision_object = brief_result.decision_object
        single = _collect(decision_object["directives"], decision_object["judgment_points"])
        for resolves_id in sorted(single.resolves_ids):
            assert resolves_id_is_satisfiable(resolves_id, single.directive_ids), (
                f"pickup_assemble variant {variant_name!r}: a judgment point disposition "
                f"resolves {resolves_id!r}, which names no directive THIS SAME decision "
                "object ever emits -- a same-pass phantom a union-only sweep (unlike this "
                "provider's per-variant check) would silently hide"
            )
        directive_ids |= single.directive_ids
        resolves_ids |= single.resolves_ids
        judgment_point_ids |= single.judgment_point_ids

    return PhantomSweepResult(
        directive_ids=frozenset(directive_ids),
        resolves_ids=frozenset(resolves_ids),
        judgment_point_ids=frozenset(judgment_point_ids),
    )


# Registered as REAL sweeps rather than `_VERIFIED_RESOLVES_FREE`: both do


def sweep_roadmap_planning_assemble() -> PhantomSweepResult:
    from coordinator_core import roadmap_planning_assemble as rpa

    decision_object = rpa.brief()
    return _collect(decision_object["directives"], decision_object["judgment_points"])


def sweep_sprint_planning_assemble() -> PhantomSweepResult:
    from coordinator_core import sprint_planning_assemble as spa

    decision_object = spa.brief(run_id="phantom-sweep", sprint_id="1")
    return _collect(decision_object["directives"], decision_object["judgment_points"])


# COMPLETE-sentinel disk scan via `ops.learn_lessons_cutoff.derive_cutoff`,


def sweep_learn_lessons_pipeline() -> PhantomSweepResult:
    from coordinator_core import learn_lessons_pipeline as llp

    envelope = llp.brief(Path("repo"), roots=[])
    return _collect(envelope["directives"], envelope["judgment_points"])
