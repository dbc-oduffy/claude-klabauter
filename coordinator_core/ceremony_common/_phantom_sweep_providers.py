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
(example-doctrine-repo `docs/plans/2026-07-26-review-skill-computed-residue.md`; its
C3/C4/C5/C13 rows landed here in `0859fb56`, the rest are outstanding)
will change that, and a static pin cannot notice its own premise going
stale. `sweep_review_assemble` below replaces it with a real dynamic
sweep — swept across surface variants, introspecting whatever `brief()`
actually emits rather than asserting today's shape — exactly the standing
this file's other providers hold.

Spec backlink: cross-repo/inbox/2026-07-27-… "Generalize seam guards
fleet-wide" dispatch (example-doctrine-repo, 2026-07-27).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from coordinator_core.ceremony_common.phantom_resolves_sweep import PhantomSweepResult


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


# ---------------------------------------------------------------------------
# workday_complete -- `_build_directives`/`_build_judgment_points` are pure
# functions over `(decisions, open_day_goals, dirty_tree_verdict)`; no disk
# or monkeypatching needed. Swept across both conditional axes: open-day-
# goals present/absent (gates `d_goal_close_day`/`jp_day_goal_closeout`) and
# dirty-tree ambiguous True/False (gates `d_step3_consolidate`'s
# `depends_on` and `jp_step2_5_dirty_tree_ambiguous`'s emission).
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# workweek_complete -- both builders are zero-arg and unconditional; no
# axis to vary. Included for completeness/regression-proofing even though
# static inspection (2026-07-27) shows every `build_disposition` call site
# here passes no `resolves` at all (every judgment point is advisory-only)
# -- a future edit that adds a `resolves` value is what this guards against.
# ---------------------------------------------------------------------------


def sweep_workweek_complete() -> PhantomSweepResult:
    from coordinator_core.workweek_complete import brief as ww_brief

    directives = ww_brief._build_directives()
    judgment_points = ww_brief._build_judgment_points()
    return _collect(directives, judgment_points)


# ---------------------------------------------------------------------------
# workstream_complete -- reuses the existing, already-verified sweep this
# guard was generalized FROM, rather than re-deriving it. That sweep needs
# `monkeypatch`/`tmp_path` (session-shape gate mocking + a real on-disk
# governing plan + handoff) -- this wrapper owns that fixture plumbing so
# the shared registry can call it with the uniform zero-arg signature every
# other provider uses.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# baton_assemble -- `_build_directives(kind, lineage, title=)`/`_build_
# judgment_points(kind, dirty_tree_attribution)` are pure over a caller-built
# `lineage` dict / attribution dict; `resolve_lineage`'s and `_compute_
# dirty_tree_attribution`'s own disk/git reads are bypassed entirely. Swept
# across both `kind`s, (for kind="handoff") both `predecessor` present/
# absent (gates `d6`/`handoff.supersede_predecessor`), AND (2026-07-31,
# case-c conditional-emission fix) all three `dirty_tree_attribution`
# shapes `_build_judgment_points` branches on -- `mine` non-empty, `mine`
# empty (jp NOT emitted), and `degraded=True` (unconditional fallback) --
# since that axis now also gates `j-dirty-tree-case-c`'s own emission.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# merge_assemble -- `build_directives(repo_root, tag_prefix=, proposed_
# tag=)`/`build_judgment_points()` need no real repo (`repo_root` is only
# `str()`'d into a directive's `args`) -- unconditional, no axis to vary.
# ---------------------------------------------------------------------------


def sweep_merge_assemble() -> PhantomSweepResult:
    from coordinator_core import merge_assemble as ma

    directives = ma.build_directives(Path("repo"), tag_prefix="v", proposed_tag="v1.2.3")
    judgment_points = ma.build_judgment_points()
    return _collect(directives, judgment_points)


# ---------------------------------------------------------------------------
# consolidate_assemble -- `brief()` takes an injectable `run_git`; swept
# with a fake covering every resolves-bearing branch: a `mine-stale`
# branch WITH unique commits (`j-absorb-<name>` -> `d-absorb-<name>`), a
# second worktree with unique (unreachable) work that is dirty
# (`j-worktree-dirty-<path>` -> `d-worktree-remove-<path>`), and `current`
# behind `main` (`j-behind-main`, resolves nothing itself but exercises the
# same sweep pass as the others for judgment_point_ids completeness).
# ---------------------------------------------------------------------------


def sweep_consolidate_assemble(tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core import consolidate_assemble as ca

    wt_path = str(tmp_path / "wt")

    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        # Pre-subcommand global flags (`--no-optional-locks`, adopted on the
        # read-only sites) sit BEFORE the subcommand, so every matcher below
        # would silently stop matching and fall through to the catch-all
        # AssertionError. Dispatch on the SUBCOMMAND, never on raw argv[0].
        # Only valueless global flags are stripped here — a value-taking one
        # (`-C <path>`) would need its argument dropped too, and this fake
        # passes cwd separately rather than via `-C`.
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
            # unreachable (dirty-unique-work path) for every ancestry probe
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if args[0] == "status":
            # dirty for the second worktree, clean for the primary
            dirty = str(cwd) == wt_path
            return SimpleNamespace(returncode=0, stdout="M x\n" if dirty else "", stderr="")
        raise AssertionError(f"consolidate_assemble sweep: unexpected git call: {args}")

    decision_object = ca.brief(repo_root=tmp_path, run_git=run_git)
    return _collect(decision_object["directives"], decision_object["judgment_points"])


# ---------------------------------------------------------------------------
# backlog_grind_assemble -- `brief(cadence)` reads real (but read-only)
# disk/queue state via its five reader families; no fixture seeding here
# -- swept against the actual live repo across all five cadences. This is
# a live-state smoke sweep (catches what today's real backlog/queue
# contents reach), NOT a synthetic full-branch-coverage sweep of every
# reader's every conditional -- an honest scope limit, not a silent gap:
# each reader degrades to an empty `ReaderResult` on an empty queue, which
# is itself a legitimate real state, never a mock.
# ---------------------------------------------------------------------------


def sweep_backlog_grind_assemble(monkeypatch: Any) -> PhantomSweepResult:
    import os
    import sys

    # `backlog_grind_assemble` transitively imports `orient_assemble` (for
    # `ReaderResult`), whose own `readers_branch_reconcile.py` dynamically
    # loads `coordinator/bin/workday-start-day-branch-resolve.py`, which
    # imports `cc_invoke.py` -- and THAT module arms lazy op registration
    # process-globally at import time. This sweep only needs to not leave the
    # process dirty for a sibling test/session that runs after it, so it
    # snapshots and restores the flag around the one import path that can
    # trigger the write.
    #
    # BOTH channels are restored. The env var was the channel until
    # 2026-07-28, when the in-process signal moved to
    # `sys._coordinator_core_lazy_ops` to stop children inheriting it (see
    # coordinator/bin/lib/cc_invoke.py); the env var remains a legitimate
    # operator override, so a value found there is still put back rather than
    # dropped. The import-time global write is no longer the unfixed defect
    # this comment used to report.
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

    # `brief()` calls `resolve_operator_config()` (AC5) -- stubbed exactly
    # like `test_backlog_grind_assemble.py`'s own autouse fixture, so this
    # sweep never depends on the invoking machine's real settings-home
    # layout (and plays correctly with the test-quarantine harness that
    # rejects a stale/nonexistent settings_home path).
    monkeypatch.setattr(
        bga,
        "resolve_operator_config",
        lambda: {
            "settings_home": "/fake/settings-home",
            "claude_klabauter_bin": "/fake/settings-home/bin",
            "claude_klabauter_root": "/fake/claude-klabauter-root",
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


# ---------------------------------------------------------------------------
# review_assemble -- `residue.brief(artifact_arg, *, repo_root=, explicit_
# surface=)` selects its surface via a precedence ladder (explicit ->
# artifact-shape inference -> diff-nonempty inference -> genuine
# ambiguity); the ONLY judgment point it ever emits
# (`review-assemble-residue-surface-ambiguous`) is reachable exclusively on
# the last rung. Swept across all three reachable call shapes so no single
# surface variant goes unswept: `explicit_surface="plan"`, `explicit_
# surface="diff"`, and the ambiguous shape (`artifact_arg=None`, `explicit_
# surface=None`, against a `repo_root` with no git history so the diff-
# nonempty inference rung also comes up empty and falls through to the
# judgment point). `resolve_content_root()` has no injection seam of its
# own (residue.py:393 calls it unconditionally) -- monkeypatched directly
# on the `residue` module namespace, exactly as `test_residue.py`'s own
# `_patch_content_root` helper does, pointed at a minimal on-disk fixture
# residue dir carrying one segment per `SEGMENT_SURFACES` value (`plan`,
# `diff`, `shared`) so every variant resolves a non-empty segment set
# (`brief()` fail-louds on an empty selection -- AC-14(a) in residue.py's
# own docstring). Deliberately introspects whatever `brief()` actually
# returns rather than asserting today's shape: this package emits
# `directives=[]` unconditionally today (residue.py never builds a
# directive), so `directive_ids`/`resolves_ids` are empty now -- but this
# provider makes no assumption that stays true, it just unions and hands
# back whatever is really there call-to-call.
# ---------------------------------------------------------------------------


def sweep_review_assemble(monkeypatch: Any, tmp_path: Path) -> PhantomSweepResult:
    from coordinator_core.review_assemble import residue as residue_mod

    content_root = tmp_path / "content-root"
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


# ---------------------------------------------------------------------------
# pickup_assemble -- the heaviest provider in this file, and the last
# `brief(`-defining package this generalization pass left in
# `test_phantom_resolves_id_sweep.py`'s `_DEFERRED_ALLOWLIST` (2026-07-27
# follow-up dispatch that closes that deferral). `brief()`'s judgment-point
# construction is deeply inline within its own classification dispatch
# (handoff/spinoff/memo) and its own `kind` sub-dispatch (ask/consult/
# proposal/fyi) -- no single call constructs every resolves-bearing shape,
# and every git read funnels through a module-private `_run_git` reading a
# REAL on-disk `.git` (no injectable `run_git` seam, unlike
# `consolidate_assemble` above). Reuses `pickup_assemble`'s own co-located
# test fixtures (`_init_repo`/`_seed_handoff`/`_seed_memo`) rather than
# inventing a new one or monkeypatching `_run_git` -- a real git repo under
# `tmp_path`, exercised through the real read-model, is strictly more
# faithful and no heavier to write.
#
# PER-DECISION-OBJECT CHECKING, not union-only (2026-07-27 dispatch
# decision 4): every other provider in this file unions across variants
# and returns one triple -- too weak here, because the handoff/spinoff
# live-claim stand-down bail (`__init__.py` ~5121-5176) builds ITS OWN
# decision object with `directives=[]`, and a union-shaped check would
# never notice a `resolves` id in THAT object being satisfied only by a
# DIFFERENT variant's directives (the main handoff path, same package).
# That exact shape was live here until this same dispatch fixed it (see
# the `resolves: []` amendment at that call site, and this provider's own
# `handoff-live-claim-bail` variant below, which asserts the fix holds).
# So this sweep checks EACH variant's own `(directive_ids, resolves_ids)`
# pair against ITSELF via `resolves_id_is_satisfiable` before folding it
# into the unioned return that `_PROVIDERS`'s uniform contract still
# expects.
# ---------------------------------------------------------------------------


def pickup_assemble_variants(monkeypatch: Any, tmp_path: Path) -> list[tuple[str, Any]]:
    """Builds the representative `(variant_name, BriefResult)` pairs
    `sweep_pickup_assemble` sweeps -- split out from that function (rather
    than inlined) so `test_phantom_resolves_id_sweep.py` can assert
    classification/kind COVERAGE directly (every variant name this
    function can ever produce, not just the unioned id-set its caller
    reduces down to) as its own explicit, named-regression test, per the
    2026-07-27 follow-up dispatch's own instruction: "so a future
    regression to partial coverage fails rather than passes."""
    from coordinator_core import pickup_assemble as pa
    from coordinator_core.test_pickup_assemble import _init_repo, _seed_handoff, _seed_memo

    repo = tmp_path / "repo"
    _init_repo(repo)

    variants: list[tuple[str, Any]] = []

    # -- handoff/spinoff branch (classification in ("handoff", "spinoff")) --

    _seed_handoff(repo, "h-normal.md")
    variants.append(("handoff-normal", pa.brief("state/handoffs/h-normal.md", repo_root=repo)))

    _seed_handoff(repo, "h-awaiting-gate.md", deployment_state="awaiting_gate")
    variants.append(("handoff-awaiting-gate", pa.brief("state/handoffs/h-awaiting-gate.md", repo_root=repo)))

    _seed_handoff(repo, "h-shipped.md", deployment_state="shipped")
    variants.append(("handoff-shipped", pa.brief("state/handoffs/h-shipped.md", repo_root=repo)))

    _seed_handoff(repo, "s-normal.md", kind="spinoff")
    variants.append(("spinoff-normal", pa.brief("state/handoffs/s-normal.md", repo_root=repo)))

    _seed_handoff(repo, "h-liveness.md")
    monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("handoff-liveness-fired", pa.brief("state/handoffs/h-liveness.md", repo_root=repo)))
    monkeypatch.undo()

    # The live-claim stand-down bail (dispatch brief's flagged finding,
    # `__init__.py` ~5121-5176) -- mirrors
    # `test_pickup_assemble.TestBriefLiveClaimRevalidateJudgmentPoint.
    # test_live_claim_stand_down_still_builds_revalidate_judgment_point`'s
    # own fixture exactly: a live foreign peer holds the claim AND
    # `compute_claim_grant` denies it (the self-claim/handover carve-out,
    # PM ruling 2026-07-24, only skips this bail when the grant is
    # `"granted"`), AND the liveness signal fires, so `live_claim_jp` is
    # actually built (a non-firing signal returns `None` -- nothing to
    # check here at all).
    _seed_handoff(repo, "h-live-claim.md")
    monkeypatch.setattr(pa, "compute_claim_gate", lambda *a, **k: {"fetch_state": "ok", "holder": "live-peer-sid"})
    monkeypatch.setattr(
        pa,
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
    monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("handoff-live-claim-bail", pa.brief("state/handoffs/h-live-claim.md", repo_root=repo)))
    monkeypatch.undo()

    # -- memo branch (classification == "memo"), all four `kind` values --

    for kind in ("ask", "consult", "proposal", "fyi"):
        name = f"m-{kind}.md"
        _seed_memo(repo, name, kind=kind)
        variants.append((f"memo-kind-{kind}", pa.brief(f"cross-repo/inbox/{name}", repo_root=repo)))

    # memo branch's liveness gate (`__init__.py` ~5407) -- the one class-(c)
    # dynamically-composed `resolves` in the whole package (recon §4/§5):
    # `resolves=[d["id"] for d in directives]`, always exactly that same
    # decision object's own directive ids, so it should never phantom --
    # swept anyway, so a future regression that de-syncs the two is caught.
    _seed_memo(repo, "m-liveness.md", kind="ask")
    monkeypatch.setattr(pa, "compute_liveness_signal", lambda *a, **k: True)
    variants.append(("memo-liveness-fired", pa.brief("cross-repo/inbox/m-liveness.md", repo_root=repo)))
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
