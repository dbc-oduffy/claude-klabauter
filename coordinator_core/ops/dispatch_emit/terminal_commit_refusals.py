"""Params parsing and refusal gates for dispatch.terminal_commit: halted params, budget, tests-not-run, landing integrity, dirt."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Sequence, Optional
from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit import review_floor
from coordinator_core.ops.dispatch_emit.land_run import foreign_halted_withheld, _recovery_backing
from coordinator_core.ops.dispatch_emit.ledger_recovery import RECOVERY_WITHHELD_PARAM
from coordinator_core.ops.dispatch_emit.ask_contract import StageManifest
from coordinator_core.ops.dispatch_emit.write_baseline import (
    prior_pass_written as _prior_pass_written,
    pre_existing_dirt as _pre_existing_dirt_paths,
    NO_CHANGE_REFUSAL,
    DIRTY_BEFORE_RUN_KEY,
    MEANT_TO_LAND_KEY,
    unwritten as _unwritten_paths,
)
from coordinator_core.ops.dispatch_emit.commit_request import plan_deliverable_id, CommitRequest
from coordinator_core.ops.dispatch_emit.worktree_snapshot import read_rel as _read_rel

from coordinator_core.ops.dispatch_emit.terminal_commit_closeout import _cited_sizing
from coordinator_core.ops.dispatch_emit.terminal_commit_checkpoints import _plan_checkpoint_touched
from coordinator_core.ops.dispatch_emit.terminal_commit_review import (
    _delivery_not_rostered,
    _review_recorded_edits,
)


def _launcher_run_digest(data: dict, path_raw: str, run_id: Optional[str]) -> dict:
    """The child digest a launcher envelope `{parent_id, runs: [{run_id, status, result|error}]}` holds for ``run_id``.

    ``run_id`` may be omitted only when the envelope holds exactly one run.
    """
    runs = [r for r in data["runs"] if isinstance(r, dict)]
    ids = [r.get("run_id") for r in runs]
    if run_id is None:
        if len(runs) != 1:
            raise ValueError(
                f"{path_raw!r} is a launcher envelope of {len(runs)} runs {ids}: pass params.run_id to select one"
            )
        chosen = runs[0]
    else:
        matches = [r for r in runs if r.get("run_id") == run_id]
        if len(matches) != 1:
            raise ValueError(f"{path_raw!r} is a launcher envelope with no single run {run_id!r}; runs: {ids}")
        chosen = matches[0]
    result = chosen.get("result")
    if chosen.get("status") != "ok" or not isinstance(result, dict):
        raise ValueError(
            f"{path_raw!r} launcher run {chosen.get('run_id')!r} has status {chosen.get('status')!r}, "
            f"no digest: {chosen.get('error')!r}"
        )
    return result


def _xs_zero_slice_review(digest: dict) -> Optional[dict]:
    """The zero-slice `inline_review` of an XS digest whose review never ran, else ``None``.

    A docs/state-only XS run skips review by design (`_reviewWave = []`; a code-editing one runs or
    halts `review-floor`, and `_review_floor_refusal` refuses this empty form for it), but a digest composed with an
    integration stage derives `inline_review` from that stage's result and so carries null;
    the bookkeeping-stem form below is what the same run emits without one.
    """
    review = digest.get("review")
    run_id = digest.get("run_id")
    if (
        digest.get("arm") != "xs"
        or not isinstance(run_id, str)
        or not run_id
        or not isinstance(review, dict)
        or review.get("wave") != []
        or any(review.get(k) is not None for k in ("prep", "delivery", "integration"))
    ):
        return None
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem

    return {
        "integration_stem": review_wave_bookkeeping_stem(run_id, None),
        "slices": 0,
        "fixes": 0,
        "wave_sidecar_paths": [],
        "wave_reviews": [],
        "prep": None,
        "delivery": None,
        "wave_not_run": [],
    }


def _halted_commit_params(digest: dict) -> Optional[dict]:
    """The `dispatch.terminal_commit` params of an XS digest that halted AT commit, else ``None``.

    Such a digest's `next_action` is `revise_and_refire` and carries no commit params; the run's
    script sits at its fixed scratch path and the ledger, incomplete ids and zero-slice review
    are in the digest itself.
    """
    halted = digest.get("halted_by")
    run_id = digest.get("run_id")
    review = _xs_zero_slice_review(digest)
    ledger = digest.get("ledger")
    items = ledger.get("items") if isinstance(ledger, dict) else None
    if (
        review is None
        or not isinstance(halted, dict)
        or halted.get("halted") != "commit"
        or not isinstance(items, list)
    ):
        return None
    keep = ("id", "status", "essential", "wired", "superseded_by", "withheld_by", "post_commit_acts",
            "met_by", "root_read", "remaining", "gaps_closed")
    verdict = {
        "items": [
            {"essential": True, **{k: i[k] for k in keep if k in i}}
            for i in items if isinstance(i, dict)
        ]
    }
    incomplete = digest.get("incomplete")
    return {
        "incomplete_chunks": [c for c in incomplete if isinstance(c, str)] if isinstance(incomplete, list) else [],
        "script_path": f"scratch/warp/{run_id}.workflow.mjs",
        "inline_review": review,
        "arm": "xs",
        "held": [],
        "ledger_verdict": verdict,
    }


def _params_from_task_output(path_raw: str, run_id: Optional[str] = None) -> dict:
    """`next_action.params` out of a Workflow task-output file, bare or `{summary, logs, result}`-wrapped.

    Same envelope unwrap as `land-wave.py :: _wave_result`: a dict `result` is the digest. A
    launcher envelope (`result.runs`) yields the digest of the run ``run_id`` selects. An XS digest
    whose `inline_review` is null gets the zero-slice form (`_xs_zero_slice_review`).
    Raises ValueError naming the file when no `next_action.params` dict is found.
    """
    path = Path(path_raw)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read params.task_output_path {path_raw!r}: {exc}") from exc
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("result"), dict):
        data = data["result"]
    if isinstance(data, dict) and "next_action" not in data and isinstance(data.get("runs"), list):
        data = _launcher_run_digest(data, path_raw, run_id)
    action = data.get("next_action") if isinstance(data, dict) else None
    found = action.get("params") if isinstance(action, dict) else None
    if not isinstance(found, dict):
        raise ValueError(
            f"{path_raw!r} carries no next_action.params: neither a digest nor a "
            "task-output envelope wrapping one"
        )
    if action.get("kind") == "revise_and_refire":
        recovered = _halted_commit_params(data)
        if recovered is not None:
            return recovered
    if found.get("inline_review") is None and action.get("kind") == "terminal_commit":
        synthesized = _xs_zero_slice_review(data)
        if synthesized is not None:
            found = {**found, "inline_review": synthesized}
    if isinstance(data, dict) and isinstance(data.get("arm"), str) and "arm" not in found:
        found = {**found, "arm": data["arm"]}
    return found


def _anchor_request(worktree_root: Path, params: dict, inline_review: Optional[dict]) -> Optional[CommitRequest]:
    """The zero-chunk request for a marker-less run whose review delivered PASS
    with no product file: it still owes the plan a fresh `Inline-Review`
    commit for `review_stamp` to anchor on. ``None`` for any other shape."""
    plan_rel = params.get("plan_path")
    if not isinstance(plan_rel, str) or not isinstance(inline_review, dict):
        return None
    delivery = inline_review.get("delivery")
    if _delivery_not_rostered(inline_review):
        delivery = inline_review.get("prep")
    elif not isinstance(delivery, dict) or delivery.get("verdict") != "PASS":
        return None
    if not isinstance(delivery, dict):
        return None
    files = delivery.get("product_files")
    if isinstance(files, bool) or not isinstance(files, int) or files != 0:
        return None
    plan_text = _read_rel(worktree_root, plan_rel)
    if plan_text is None:
        return None
    return CommitRequest(
        chunks=(), plan_path=plan_rel, deliverable_id=plan_deliverable_id(plan_text)
    )


def _new_file_rows(worktree_root: Path, absent: list, skip=None) -> Optional[list]:
    """`(added, 0, path)` rows for the untracked paths among ``absent``: one `ls-files --others`
    spawn, each file one row, its lines counted in-process (a NUL byte in the head means binary,
    zero lines). ``skip(rel)`` true leaves a path unread and unpriced. None when git could not
    answer."""
    listed = run_git(
        ["-C", str(worktree_root), "--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "--", *absent]
    )
    if not listed.ok:
        return None
    rows = []
    for rel in listed.stdout.splitlines():
        if skip is not None and skip(rel):
            continue
        try:
            data = (worktree_root / rel).read_bytes()
        except OSError:
            data = b""
        lines = 0 if b"\0" in data[:8000] else data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
        rows.append((lines, 0, rel))
    return rows


def _run_arm(worktree_root: Path, params: dict, request: CommitRequest) -> Optional[str]:
    """The run's arm: ``params['arm']`` (stamped from the digest), else the arm of the sizing the
    plan cites; None when neither resolves."""
    arm = params.get("arm")
    if isinstance(arm, str) and arm:
        return arm
    sizing = _cited_sizing(worktree_root, request.plan_path)
    if not sizing:
        return None
    from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused, load_sizing, resolve_arm

    try:
        return resolve_arm(load_sizing(worktree_root, sizing))
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return None


def _peer_adjusted_rows(
    worktree_root: Path, base: str, rows: list, request: CommitRequest
) -> Optional[list]:
    """``rows`` with each path a peer run committed ahead of this run's own work re-measured from
    that peer commit (``peer_path_bases``): one `git log` spawn, plus one numstat per distinct peer
    base. None when a re-measure fails. ``rows`` are already limited to the run's own paths."""
    from coordinator_core.ops.review_peer_bases import peer_path_bases

    plan_text = _read_rel(worktree_root, request.plan_path) if request.plan_path else None
    bases = peer_path_bases(
        worktree_root, base, None, [r[2] for r in rows], request.session_id or "",
        plan_deliverable_id(plan_text) if plan_text else None,
    )
    if not bases:
        return rows
    from coordinator_core.ops.dispatch_emit import xs_diff_budget

    out = [r for r in rows if r[2] not in bases]
    by_base: dict = {}
    for path, sha in bases.items():
        by_base.setdefault(sha, []).append(path)
    for sha, group in by_base.items():
        stat = run_git(
            ["-C", str(worktree_root), "--literal-pathspecs", "diff", "--numstat", "--no-renames", sha, "--", *group]
        )
        if not stat.ok:
            return None
        out.extend(xs_diff_budget.parse_numstat(stat.stdout))
    return out


def _xs_over_budget_refusal(
    worktree_root: Path, params: dict, run_id: str, request: CommitRequest
) -> Optional[dict]:
    """The ``xs-over-budget`` refusal when an XS-arm run's own diff against its run base
    exceeds the XS budget (or cannot be measured), else None. Any other arm pays nothing.

    The measure is the chunks' declared paths and prefixes only (the pathspec on both git
    spawns), less bookkeeping paths, the plan and cited sizing, paths a live peer holds, and
    peer-committed hunks. An arm that cannot be resolved is not gated. A peer's commits or dirt
    elsewhere in the tree never count.

    Trap: `git diff <base>` is blind to untracked files, so an `ls-files --others` over the same
    pathspec prices them. An undeclared write is not measured.
    """
    from coordinator_core.ops.dispatch_emit import landing_integrity as _li
    from coordinator_core.ops.dispatch_emit import xs_diff_budget
    from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS

    if _run_arm(worktree_root, params, request) != ARM_XS:
        return None
    sizing = _cited_sizing(worktree_root, request.plan_path)
    exempt = [p for p in (request.plan_path, sizing) if p]
    bookkept = xs_diff_budget.BOOKKEEPING_PREFIXES
    base = _li.run_base_for(worktree_root, run_id, params)
    owned = sorted({p.rstrip("/") for c in request.chunks for p in (*c.paths, *c.prefixes) if p})
    rows = None
    if base is not None and not owned:
        rows = []
    elif base is not None:
        stat = run_git(
            ["-C", str(worktree_root), "--literal-pathspecs", "diff", "--numstat", "--no-renames", base, "--", *owned]
        )
        if stat.ok:
            tracked = xs_diff_budget.product_rows(xs_diff_budget.parse_numstat(stat.stdout), exempt)
            tracked = _peer_adjusted_rows(worktree_root, base, tracked, request)
            fresh = _new_file_rows(
                worktree_root, owned, lambda rel: rel.startswith(bookkept) or rel in exempt
            )
            rows = None if tracked is None or fresh is None else tracked + fresh
    if rows is not None:
        held = _peer_held(worktree_root, request.session_id, [r[2] for r in rows], run_id=run_id)
        rows = [r for r in rows if r[2] not in held]
    if rows is None:
        return _error(
            "XS run diff is unmeasurable (no readable run base or git diff failed); "
            f"resize {sizing or 'the sizing'} to S or larger and refire",
            refused=xs_diff_budget.REFUSED_XS_OVER_BUDGET,
        )
    v = xs_diff_budget.verdict(rows)
    if v is None:
        return None
    message = xs_diff_budget.resize_next_action(v, sizing)
    return _error(
        message,
        refused=xs_diff_budget.REFUSED_XS_OVER_BUDGET,
        measured={"files": v["files"], "lines": v["lines"]},
        next_action=message,
    )


_TEST_PATH = re.compile(r"(^|/)(tests?/|test_[^/]*$|[^/]*_test\.py$|[^/]*\.(test|spec)\.[jt]sx?$)")

_NOT_RUN_TEXT = re.compile(r"\bnot[ _]run\b", re.I)

REFUSED_TESTS_NOT_RUN = "tests-not-run"


def _tests_not_run_refusal(
    worktree_root: Path, params: dict, request: CommitRequest, inline_review: dict
) -> Optional[dict]:
    """The ``tests-not-run`` refusal when an XS-arm run declares a test file yet its
    ``inline_review.tests.status`` is ``not_run`` or its ledger verdict calls an item ``met`` on
    evidence that says the tests were not run, else None. Other arms and runs declaring no test
    file are not gated."""
    from coordinator_core.ops.dispatch_emit.sizing_fire import ARM_XS

    if _run_arm(worktree_root, params, request) != ARM_XS:
        return None
    declared = [p for c in request.chunks for p in (*c.paths, *c.prefixes) if p]
    if not any(_TEST_PATH.search(p) for p in declared):
        return None
    tests = inline_review.get("tests") if isinstance(inline_review.get("tests"), dict) else {}
    if tests.get("status") == "not_run":
        return _error(
            "inline_review.tests.status is not_run and the XS diff declares a test file; "
            "run the scoped tests (named test files only) and re-fire with their result",
            refused=REFUSED_TESTS_NOT_RUN,
        )
    verdict = params.get("ledger_verdict")
    items = verdict.get("items") if isinstance(verdict, dict) else None
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or item.get("status") != "met":
            continue
        text = " ".join(str(v) for k, v in item.items() if k != "id" and isinstance(v, (str, list)))
        if _NOT_RUN_TEXT.search(text):
            return _error(
                f"ledger item {item.get('id')!r} is met on evidence saying its tests were not run; "
                "run the scoped tests and re-judge",
                refused=REFUSED_TESTS_NOT_RUN,
            )
    return None


def _declared_reconcile(run_dir: Optional[Path], delivered: Sequence[str]) -> dict:
    """``renamed``/``undelivered`` of the run's caller-declared writes (``declared_writes.json``)
    against the landing set. Empty when the run declared none; no git spawn."""
    if run_dir is None:
        return {}
    try:
        declared = json.loads((run_dir / "declared_writes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(declared, list) or not declared:
        return {}
    from coordinator_core.ops.dispatch_emit.declared_writes_reconcile import reconcile

    out = reconcile([p for p in declared if isinstance(p, str)], delivered)
    reply = {"renamed": out["renamed"], "undelivered": out["undelivered"]}
    if out["undelivered"]:
        reply["undeclared"] = out["undeclared"]
    return reply


def _review_floor_refusal(
    request: CommitRequest, inline_review: object, prep_from_disk: bool, root: Path,
) -> Optional[dict]:
    """The ``review-floor`` refusal when the run's floor code set (``review_floor.floor_code_paths``)
    is non-empty and the review wave returned no sidecar-backed verdict (sidecars resolve against
    ``root``), else None. Arm-agnostic; a run declaring no code path pays nothing."""
    code = review_floor.floor_code_paths(request, inline_review, prep_from_disk)
    reason = review_floor.floor_reason(code, review_floor.verdict_count(inline_review, root))
    if reason is None:
        return None
    return _error(
        reason + "; refire so the review wave runs a reviewer, or resize the run",
        refused=review_floor.REFUSED_REVIEW_FLOOR,
        code_paths=code,
    )


def _error(message: str, **extra: object) -> dict:
    result: dict = {"committed": False, "sha": None, "error": message}
    result.update(extra)
    return result


def _merge_landing_plan(
    worktree_root: Path, all_paths: list, deleted_paths: list, *, product: set,
) -> Optional[dict]:
    """Delegates to ``merge_landing.landing_plan`` with no owner, so any open merge is treated as
    this route's own (the merge rule is unchanged here)."""
    from coordinator_core.ops.dispatch_emit import merge_landing

    return merge_landing.landing_plan(worktree_root, all_paths, deleted_paths, product=product)


def _land_merge_artifacts(
    commit_v2, repo_root: Path, artifacts: dict, message: str, merge_sha: str,
    session_id: Optional[str],
) -> dict:
    """One ordinary single-parent commit for the run artifacts the merge commit left out; the
    merge commit stands whatever this returns."""
    if not artifacts["paths"] and not artifacts["deleted"]:
        return {"committed": False, "nothing_to_commit": True}
    subject = (message.splitlines() or ["run artifacts"])[0]
    params: dict = {
        "paths": artifacts["paths"],
        "message": f"{subject} (run artifacts)\n\nFollows merge commit {merge_sha[:12]}.\n",
        "carry_own_left_behind": False,
        "carry_stopped_runs": False,
    }
    if artifacts["deleted"]:
        params["deleted_paths"] = artifacts["deleted"]
    if session_id is not None:
        params["session_id"] = session_id
    try:
        out = commit_v2(params, repo_root)
    except Exception as exc:  # the merge already landed; report, never raise
        return {"committed": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(out, dict):
        return {"committed": False, "error": f"unexpected commit_v2 reply: {out!r}"}
    return {k: out.get(k) for k in ("committed", "sha", "error", "refused") if k in out}


def _retest_covers_targeted_set(li, params: dict, fresh: Optional[dict]) -> bool:
    """True when ``fresh`` (a valid retest record) ran every file of the run's recorded targeted
    set, less the files the record lists under ``dropped``. No fresh record: False."""
    if fresh is None:
        return False
    from coordinator_core.ops.dispatch_emit import retest_targeted

    required = {
        f for f in retest_targeted._recorded_targeted_set(params, li.targeted_test_verdict(params))
    }
    dropped = {li._node_file(str(t)) for t in (fresh.get("dropped") or [])}
    ran = {li._node_file(t) for t in (fresh.get("tests") or []) if isinstance(t, str)}
    return (required - dropped) <= ran


_WAIVABLE_GUARDS = ("import-closure-untracked", "peer-hunks-co-declared")
_WAIVER_ROUTE = (
    "ask the G-EM to record a waiver: python -m coordinator_core.authz.landing_waiver record "
    "--ruling-ref <turn> --guard {guard} --path <path> --run-id <run>"
)


def _hands_on() -> bool:
    """True when the session's resolved interaction mode is ``hands-on`` (no waiver is read);
    an unresolvable mode is not hands-on."""
    try:
        from coordinator_core.session.mode_resolution import resolve_mode

        return resolve_mode("interaction_mode", "sizing-assemble-cli", env=None) == "hands-on"
    except Exception:
        return False


def _apply_landing_waiver(worktree_root: Path, findings: list, finding_cls) -> list:
    """Drop import-closure-untracked and peer-hunks-co-declared findings a live G-EM waiver covers
    (guard and every finding path; these findings carry no run id), consuming each matched waiver
    once; the remaining findings of those guards name the G-EM route. No process spawn."""
    from coordinator_core.authz import landing_waiver

    matched: dict = {}
    hands_on = _hands_on()
    for guard in _WAIVABLE_GUARDS:
        mine = [f for f in findings if f.reason == guard]
        if not mine or any(not f.path for f in mine):
            continue
        waiver = landing_waiver.matching_waiver(
            worktree_root, guard, {f.path: "" for f in mine}, hands_on=hands_on
        )
        if waiver is not None:
            matched[guard] = waiver
    kept = [f for f in findings if f.reason not in matched]
    if not kept:
        spent = [landing_waiver.consume(w) for w in {w.path: w for w in matched.values()}.values()]
        if not all(spent):
            kept = list(findings)
    else:
        kept = list(findings)
    out = []
    for f in kept:
        if f.reason in _WAIVABLE_GUARDS:
            f = finding_cls(f.path, f.reason, f"{f.detail} -- {_WAIVER_ROUTE.format(guard=f.reason)}")
        out.append(f)
    return out


def _landing_integrity_gate(
    worktree_root: Path, all_paths: list, deleted_paths: list, record_abs: Optional[Path],
    params: dict, run_id: Optional[str], plan: Optional[str] = None,
    *, residue_py: Sequence[str] = (), residue_known: bool = True, merge_open: bool = False,
    own_declared: Sequence[str] = (), session_id: Optional[str] = None,
    carried_paths: Optional[Sequence[str]] = None, minted_before: Optional[tuple] = None,
) -> dict:
    """Pre-commit integrity findings over the product landing set (unparseable ``.py``, an
    import-closure hit where a ``.py`` the commit deletes (or a top-level def/class it removes) is
    still imported by a residue or landing ``.py``: ``import-closure-untracked`` naming the importer,
    with ``residue_import_check: "no-snapshot"`` when ``residue_known`` is false; a
    blob dropping lines a foreign commit added since the run base, and the test verdict
    (a valid fresh retest verdict, else the carried one) naming a run-caused failure:
    ``targeted-tests-failed``, with ``targeted_tests_check`` always set and ``retested_tests`` on
    ``pass-on-retest``), and the run's review re-verify ending red on a test file that is landing
    or imports a landing module: ``review-reverify-failed``, with ``reverify_check`` always set
    (``absent`` | ``pass`` | ``fail-off-landing`` | ``fail``). A fresh ``pass-on-retest`` clears
    the re-verify findings only when every implicated test file is among the files that retest
    ran (its record's ``tests`` node ids); a retest record naming no files clears nothing, since a
    retest that never ran the red file is no evidence it went green. Returns ``{}``-like extras:
    ``refused`` + ``integrity`` on any finding, ``since_base_check: "no-base"`` when the run has
    no recorded base so the overlap arm was skipped. The co-declared peer-hunk arm tests only
    ``carried_paths`` (None: every landing path), and a fresh-token sibling minted at or after
    ``minted_before`` is junior and holds nothing."""
    from coordinator_core.ops.dispatch_emit import landing_integrity as li
    from coordinator_core.ops.dispatch_emit import landing_retest
    from coordinator_core.ops.dispatch_emit import landing_targeted_tests

    skip = set(deleted_paths)
    if record_abs is not None:
        try:
            skip.add(Path(record_abs).resolve().relative_to(Path(worktree_root).resolve()).as_posix())
        except ValueError:
            pass
    paths = [p for p in all_paths if p not in skip]
    findings = li.unparseable_py(worktree_root, paths)
    extra: dict = {}
    if not residue_known:
        # Fail-open by design: with no snapshot only landing importers are scanned; untested.
        extra["residue_import_check"] = "no-snapshot"
    try:
        from coordinator_core.authoring_leaks import import_closure

        findings += [
            landing_targeted_tests.Finding(f.path, li.IMPORT_CLOSURE_UNTRACKED, f"{f.line}: {f.detail}")
            for f in import_closure.residue_findings(worktree_root, residue_py, paths, deleted_paths)
        ]
    except Exception as exc:  # fail closed: an unreadable closure must not land
        findings.append(landing_targeted_tests.Finding(
            "", li.IMPORT_CLOSURE_UNTRACKED,
            f"residue import-closure detector failed: {type(exc).__name__}: {exc}",
        ))
    base = li.run_base_for(worktree_root, run_id, params)
    if merge_open:
        # A conflict resolution taking the other side drops this side's lines by design.
        extra["since_base_check"] = "skipped-merge"
    elif base is None:
        extra["since_base_check"] = "no-base"
    else:
        findings += li.since_base_findings(worktree_root, paths, base, run_id=run_id, plan=plan)
    # A declared landing path carries a co-declaring live run's hunks, or a reviewer's unresolved
    # out-of-ask finding names it: a path-level commit cannot split either off.
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    # Only a carried path can absorb a peer's hunks; `carried_paths` (None: every landing path).
    carried_set = None if carried_paths is None else set(carried_paths)
    contested = paths if carried_set is None else [p for p in paths if p in carried_set]
    from coordinator_core.ops.dispatch_emit import merge_ancestry_release

    peer_holds = list(merge_ancestry_release.release(
        worktree_root,
        peer_hunk_gate.co_declared_peer_holds(
            worktree_root, run_id, contested, own_declared, own_plan=plan, minted_before=minted_before,
        ),
    ))
    for index, (held_path, sibling) in enumerate(peer_holds):
        findings.append(landing_targeted_tests.Finding(
            held_path, peer_hunk_gate.PEER_HUNKS_CO_DECLARED,
            peer_hunk_gate.refusal_message(peer_holds, run_id or "", worktree_root) if index == 0
            else f"{sibling}: also declared by that unlanded run, whose hunks a path commit cannot split off",
        ))
    if run_id and session_id:
        for held_path, stem, line in peer_hunk_gate.out_of_ask_holds(
            worktree_root, session_id, run_id, paths,
        ):
            findings.append(landing_targeted_tests.Finding(held_path, peer_hunk_gate.OUT_OF_ASK_HUNKS, f"{stem}: {line}"))
    # Trap: ``inline_review.tests.status`` is already baseline-aware (a baseline-cleared run folds
    # to ``pass``), so a carried ``fail`` here is run-caused or a failed row verification.
    tested, tcheck = li.landing_test_gate(worktree_root, params, run_id, paths)
    findings += tested
    extra["targeted_tests_check"] = tcheck
    if tested.deleted_note:
        extra["targeted_tests_note"] = tested.deleted_note
    fresh_record = None
    if tcheck == "pass-on-retest":
        fresh_record = landing_retest.fresh_targeted_verdict(worktree_root, run_id) if run_id else None
        if fresh_record is not None:
            extra["retested_tests"] = fresh_record.get("tests")
    red = landing_targeted_tests.reverify_red_findings(worktree_root, params, paths)
    record = landing_targeted_tests.reverify_record(params)
    if record is None:
        extra["reverify_check"] = "absent"
    elif landing_targeted_tests.reverify_verdict(record) != "fail":
        extra["reverify_check"] = "pass"
    else:
        extra["reverify_check"] = "fail" if red else "fail-off-landing"
    if tcheck == "pass-on-retest" and _retest_covers_targeted_set(li, params, fresh_record):
        # A fresh passing retest of the whole targeted set supersedes a re-verify that never ran.
        red = [f for f in red if f.reason != landing_targeted_tests.REVIEW_REVERIFY_INDETERMINATE]
    if red and tcheck == "pass-on-retest":
        ran = {li._node_file(t) for t in (extra.get("retested_tests") or []) if isinstance(t, str)}
        if ran and all(f.path in ran for f in red):
            red = []
    findings += red
    findings = _apply_landing_waiver(worktree_root, findings, landing_targeted_tests.Finding)
    if findings:
        extra["refused"] = findings[0].reason
        extra["integrity"] = [
            {"path": f.path, "reason": f.reason, "detail": f.detail} for f in findings
        ]
    return extra


def _missing_run_outcome(params: object) -> Optional[dict]:
    """Refusal naming every run-outcome param the caller omitted, else ``None``.

    The emitted script's marker records what the run promised (chunks, paths),
    never how it ended: ``incomplete_chunks`` and the review-stage
    ``inline_review`` exist only in the run's result digest
    (``next_action.params``), so a bare ``script_path`` cannot supply them.
    Deriving them from chunk reports would land code the review wave never saw,
    which the deterministic-review ruling forbids. Checked once, ahead of the
    per-field validation, so the caller learns every gap in one reply.
    """
    if not isinstance(params, dict) or not isinstance(params.get("script_path"), str):
        return None
    absent = [
        name for name in ("incomplete_chunks", "inline_review") if params.get(name) is None
    ]
    if "incomplete_chunks" not in absent:
        return None
    return _error(
        f"params {absent} not passed. The run outcome is not derivable from "
        f"script_path {params['script_path']!r} (it carries only the commit-request marker: "
        "chunks and declared paths) and no params.task_output_path was given. Pass "
        "task_output_path=<the run's task-output file> (its next_action.params supplies "
        "incomplete_chunks and inline_review), or pass incomplete_chunks and inline_review "
        "from the digest's terminal_commit_cli line. A run that died before its review "
        "stage has no inline_review to pass: re-emit it review-only (emit-dispatch-workflow "
        "--inventory <inv> --review-only --rows <DONE ids> --run-base <sha>) and land from "
        "that run's terminal_commit_cli line",
        refused="missing-run-outcome",
        missing=absent,
    )


def _measured_rev_drift_refusal(
    run_dir: Path, worktree_root: Path, verdict: dict, ledger: object
) -> Optional[dict]:
    """A ``ledger-unmet`` refusal (reason ``measured-rev-drift``) when a ``met`` item stands on a
    measurement whose recorded blobs no longer match the worktree; None when nothing drifted, no
    item is ``met``, or the run recorded no measurement."""
    from coordinator_core.ops.dispatch_emit import measured_rev

    met = [
        str(i["id"]) for i in verdict.get("items") or []
        if isinstance(i, dict) and i.get("status") == "met" and i.get("id") is not None
    ]
    if not met:
        return None
    try:
        drifted = measured_rev.measured_rev_drift(run_dir, worktree_root)
    except (OSError, ValueError) as exc:
        reason, detail = "measured-rev-record-unreadable", f"measured-rev record unreadable ({exc})"
    else:
        if not drifted:
            return None
        reason = detail = "measured-rev-drift: " + ", ".join(drifted)
    return _error(
        f"the requirements are not met: {', '.join(met)}: {detail}. Re-measure and re-run the ledger check",
        refused="ledger-unmet",
        ledger_failures=[{"id": i, "reason": reason} for i in met],
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
        awaiting_operator=[],
    )


def _readable_manifest(manifest_path: Optional[Path]) -> Optional[StageManifest]:
    if manifest_path is None:
        return None
    try:
        return StageManifest.from_json(json.loads(Path(manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _pre_existing_dirt_refusal(
    worktree_root: Path, manifest: Optional[StageManifest], verdict: dict, ledger: object,
    plan_rel: Optional[str] = None, run_dir: Optional[Path] = None,
) -> Optional[dict]:
    """A ``ledger-unmet`` refusal (reason ``pre-existing-dirt``) when a ``met``/``post_commit`` item's
    claiming chunks are all non-kept and every declared write of them still equals the stage-time
    baseline: the run wrote none of it. Only paths present in the tree, not landed by a same-plan
    checkpoint (``plan_rel``) and not recorded as written by an earlier pass of the run
    (``run_dir``'s record files) are named as dirt; none left, no refusal. None otherwise, or when
    the manifest carries no baseline."""
    if manifest is None or not manifest.write_baseline:
        return None
    prior = frozenset()
    if run_dir is not None:
        from coordinator_core.ops.dispatch_emit.write_baseline import predecessor_withheld

        prior = _prior_pass_written(worktree_root, run_dir) | predecessor_withheld(
            worktree_root, _cited_sizing(worktree_root, plan_rel), run_dir
        )
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    failures: list = []
    for item in verdict.get("items") or []:
        if not isinstance(item, dict) or item.get("status") not in ("met", "post_commit"):
            continue
        iid = str(item.get("id"))
        rows = [r for r in manifest.rows if iid in r.register_rows]
        if not rows or any(r.kept_from or r.adopts_stranded for r in rows):
            continue
        writes = [p for r in rows for p in r.writes]
        if not writes or len(_unwritten_paths(worktree_root, manifest.write_baseline, writes)) != len(set(writes)):
            continue
        dirt = _pre_existing_dirt_paths(
            worktree_root, manifest.write_baseline, writes,
            _tc._plan_checkpoint_touched(worktree_root, plan_rel, writes) | prior,
        )
        if dirt:
            failures.append({"id": iid, "reason": "pre-existing-dirt: " + ", ".join(sorted(set(dirt)))})
    if not failures:
        return None
    return _error(
        "the requirements are not met: "
        + "; ".join(f"{f['id']}: {f['reason']}" for f in failures)
        + ". The run did not write these paths; land the dirt through a reviewed run, or workflow.revise {claim: true, add_paths, verbatim} (verbatim non-empty)",
        refused="ledger-unmet",
        ledger_failures=failures,
        ledger_items=len(ledger.items),
        all_of_pm_ask=ledger.all_of_pm_ask,
        awaiting_operator=[],
    )


def _live_claimed_inherited(root: Path, inherited: dict, claims: dict, chunk_declared: set) -> set:
    """Inherited paths a non-halted (live) sibling claims: inheritance overrides only a halted run's
    hold, so these stay withheld unless the request itself declares them."""
    if not inherited:
        return set()
    from coordinator_core.ops import warp_run_deliverable
    from coordinator_core.ops.dispatch_emit.sibling_claims import _norm as claim_norm

    out: set = set()
    for path in inherited:
        owner = claims.get(claim_norm(path))
        if path not in chunk_declared and owner is not None and not warp_run_deliverable._halted(root, owner):
            out.add(path)
    return out


def _drop_inherited_withheld(
    root: Path, withheld: dict, inherited: dict, run_id: Optional[str], plan_rel: Optional[str],
) -> dict:
    """``withheld`` minus the refire's inherited paths; a path another halted run still holds stays."""
    mine = [p for p in inherited if p in withheld]
    if not mine:
        return withheld
    from coordinator_core.ops.dispatch_emit import terminal_commit as _tc

    foreign = _tc.foreign_halted_withheld(root, run_id, plan_rel, mine)
    return {p: why for p, why in withheld.items() if p not in inherited or p in foreign}


def _no_change_refusal(
    worktree_root: Path, manifest: Optional[StageManifest], request: CommitRequest, inline_review: object,
    run_dir: Optional[Path] = None,
) -> Optional[dict]:
    """The ``no-change`` refusal when the run changed none of the product paths it meant to land:
    every declared path of every non-kept row chunk still equals the stage-time baseline and neither
    the review nor an earlier pass of the run (``run_dir``'s record files) recorded a write to
    them. None when the manifest has no baseline, a row adopts stranded dirt, or any path was
    written."""
    if manifest is None or not manifest.write_baseline or request.review_only or not request.chunks:
        return None
    rows = {r.id: r for r in manifest.rows}
    if any(r.adopts_stranded for r in rows.values()):
        return None
    meant = sorted({
        p for c in request.chunks if c.id in rows and not rows[c.id].kept_from for p in c.paths
    })
    if not meant:
        return None
    unchanged = set(_unwritten_paths(worktree_root, manifest.write_baseline, meant))
    if unchanged != set(meant) or _review_recorded_edits(inline_review) & set(meant):
        return None
    if run_dir is not None and _prior_pass_written(worktree_root, run_dir) & set(meant):
        return None
    from coordinator_core.git.git_state import IndexParseError, read_index
    from coordinator_core.ops.dispatch_emit import kept_work

    try:
        index = read_index(worktree_root)
    except (IndexParseError, OSError):
        index = {}
    dirty = [
        p for p in meant
        if manifest.write_baseline.get(p) is not None
        and (p not in index or not kept_work.matches_committed(worktree_root, p, manifest.write_baseline[p], index[p].sha))
    ]
    route = (
        "the dirt predates the run: land it through a reviewed run of its own"
        if dirty else "revise the ask"
    )
    return _error(
        f"the run changed none of the paths it meant to land: {', '.join(meant)}; {route}",
        refused=NO_CHANGE_REFUSAL,
        **{MEANT_TO_LAND_KEY: meant, DIRTY_BEFORE_RUN_KEY: dirty},
    )


def _recovery_withheld_param(params: dict) -> list:
    raw = params.get(RECOVERY_WITHHELD_PARAM)
    return [c for c in raw if isinstance(c, str)] if isinstance(raw, list) else []


def _recovery_withheld_reasons(run_dir: Path, ids: Sequence[str]) -> tuple[dict, list]:
    """(``{chunk: open-row reason}``, unbacked ids) for the chunks ``params.recovery_withheld`` names.
    A chunk is backed when the run dir's ``recovery.json`` records it ``exhausted`` (at the attempt
    cap), ``blocked`` or ``held``. The record is written by the re-verify agent: this catches a param
    the record does not support, not an agent misreporting its own attempts."""
    from coordinator_core.ops.dispatch_emit import ledger_recovery as lr

    try:
        chunks = json.loads((run_dir / lr.RECOVERY_RECORD_FILE).read_text(encoding="utf-8")).get("chunks")
    except (OSError, ValueError, AttributeError):
        chunks = None
    return _recovery_backing(chunks if isinstance(chunks, dict) else {}, ids)


def _peer_held(
    worktree_root: Path, session_id: Optional[str], paths: list, *, run_id: Optional[str] = None
) -> set:
    """The paths another session's touch claim, or a live peer run's declared writes, holds: a
    peer's in-flight edit sitting in one of this run's directories, not this run's residue.
    ``run_id`` keeps the caller's own run from holding its own declared paths. No git spawn."""
    if not paths:
        return set()
    held: set = set()
    if session_id:
        from coordinator_core.session import claim_index

        try:
            answer = claim_index.classify_paths(session_id, paths, cwd=str(worktree_root))
        except Exception:
            return set()
        held = {p for p, o in answer.by_path.items() if o.verdict == claim_index.OWNERSHIP_PEER}
    from coordinator_core.ops import warp_run_deliverable

    try:
        run_claims = warp_run_deliverable.live_run_write_claims(
            worktree_root, paths, exclude_session=session_id or "", cwd=str(worktree_root),
            exclude_run=run_id,
        )
    except Exception:
        run_claims = None
    return held | set(run_claims or ())


def _snapshot_unavailable_refusal(recorded_pc: dict) -> dict:
    """The refusal when recorded post-checkpoint hunks exist but the worktree snapshot could not be read,
    so their dirt cannot be checked."""
    return _error(
        "recorded post-checkpoint hunk(s) cannot be checked: the worktree snapshot is unavailable ("
        + ", ".join(sorted(recorded_pc)) + ")",
        refused="own-post-checkpoint-hunks-unverifiable",
        paths=sorted(recorded_pc),
    )


def _unclaimed_refusal(recorded_pc: dict, unclaimed: list) -> dict:
    """The one refusal for own post-checkpoint hunks outside a landing. Trap: the early zero-landing
    site and the pre-commit site must refuse with this single message and token."""
    return _error(
        "own post-checkpoint hunk(s) are dirty and outside this landing: "
        + ", ".join(f"{p} (recorded in {recorded_pc[p]})" for p in unclaimed)
        + ". Re-stage so the kept-review chunk carries them, or withhold each with a reason "
        "through workflow.revise withhold_paths",
        refused="own-post-checkpoint-hunks-unclaimed",
        paths=unclaimed,
    )
