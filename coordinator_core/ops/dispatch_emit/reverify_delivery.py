"""coordinator_core.ops.dispatch_emit.reverify_delivery -- re-run only the delivery-verifier
stage against HEAD and supersede a frozen FAIL.

`emit-dispatch-workflow --plan P --reverify-delivery RUN_RECORD` emits a one-stage Workflow
script; `record` turns its returned result into an append-only delivery-verdict record under
`state/delivery-verdicts/<YYYY-MM>/` that names the run record it supersedes.
`verdict_supersession.latest_delivery_supersession` is what `review_stamp.mint` reads in place of the frozen verdict;
the original run record is never edited.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.completion_receipts.test_verdict import (
    TestVerdictRefused,
    bind_sidecar,
    record_test_verdict,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.delivery_credit import ancestor_refs, coded_row_landings
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ops.review_mint.compose import _agent_call_literal, with_sidecar_clause
from coordinator_core.ops.review_mint.execute_review import (
    _DELIVERY_VERIFIER_AGENT_TYPE,
    _DELIVERY_VERIFIER_HOST_NATIVE_TYPE,
    _DELIVERY_VERIFIER_ROLE_PREAMBLE,
    CRITERION_JUDGE_PHASE_TITLE,
    _agent_opts_for,
    _host_native,
    _schema_literal,
    compose_criterion_judge,
    criterion_judge_parts,
    delivery_supersession_clause,
    em_owned_clause,
    owner_repo_clause,
    owner_repo_roots,
    resolve_operative_criterion_for_plan,
)
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.dispatch_emit.verdict_supersession import (  # noqa: F401 -- re-exported for callers of this module
    DIFF_SHA_KEY,
    RECORD_KIND,
    REJUDGE_KIND,
    VERDICT_DIR,
    _frontmatter,
    latest_criterion_supersession,
    latest_delivery_supersession,
    latest_foreign_claims_supersession,
    latest_tests_supersession,
)
from coordinator_core.ops.dispatch_emit.wake_digest import stage_schema_literal
from coordinator_core.ops.workflow_scaffold import _js_string_literal

# Writes per-session subagent-share sidecars and a one-shot workflow script, not stamped artifacts.
GENERATES = []

_PHASE = "Delivery re-verify"
_CRITERION_STATUSES = ("met", "not_met", "indeterminate")
_TESTS_PHASE = "Tests re-run"
_TESTS_STATUSES = ("pass", "fail", "error")
_VERIFY_PHASE = "Verify"

_TESTS_FIELD = (
    ", tests: _tests ? { status: _tests.status ?? null, run: _tests.tests_run ?? null, "
    "failed: _tests.tests_failed ?? null, sidecar: _tests.sidecar_path ?? null, "
    "baseline_method: _tests.baseline_method ?? null, baseline: _tests.baseline ?? null } : null"
)
_CRITERION_FIELD = (
    ", criterion: _judge ? { status: _judge.status ?? null, "
    "observation: _judge.observation ?? null } : null"
)


def _sidecar(call: str, plan_path: str, label: str) -> str:
    """Append the reverify sidecar clause, keyed by the plan stem, to one finished agent call."""
    return with_sidecar_clause(call, "reverify", Path(plan_path.replace("\\", "/")).stem or "run", label)


def _tests_prompt(head_sha: str, plan_path: str, base: str, *, write_record: bool = False) -> str:
    """The scoped-test brief. `write_record` adds the test-runner's sidecar clause, which an agent
    without Write cannot honour."""
    return (
        f"Run the plan's scoped tests at HEAD {head_sha}: {plan_path}, run_base_sha {base}. "
        "Run Python as `python3`, falling back to `python` when `python3` is absent. "
        "Report raw evidence; do not gate. "
        + (
            "Write your record and return sidecar_path -- required. "
            "The engine stamps agent_type and target_plan on it at record time.\n"
            if write_record
            else "You have no Write tool: return it under `test` and omit sidecar_path.\n"
        )
        + f"plan_path: {plan_path}"
    )

#: The verifier has no Write tool; `record` persists its returned verdict.
_NO_SIDECAR_NOTE = "Return the verdict as your structured result only; the engine records it. Write no sidecar."
_DELIVERY_SIDECAR_AGENT_TYPE = "coordinator:delivery-verifier"


class ReverifyRefused(ValueError):
    """No script or record was produced; the message names why."""


def _delivery_block(record: dict) -> Optional[dict]:
    for holder in (record, record.get("inline_review"), record.get("review")):
        block = holder.get("delivery") if isinstance(holder, dict) else None
        if isinstance(block, dict):
            return block
    return None


def prep_of(repo_root: Path, record: dict) -> Optional[dict]:
    """The run record's `prep` block: inline, else its `prep_sidecar` frontmatter."""
    prep = record.get("prep")
    if isinstance(prep, dict):
        return prep
    rel = record.get("prep_sidecar")
    return _frontmatter(repo_root / rel) if rel else None


def load_delivery(repo_root: Optional[Path], record: dict, prep: Optional[dict] = None) -> Optional[dict]:
    """The one loader of a run record's frozen delivery block: the record's own frontmatter
    first, else the sidecar `prep.whole_diff_sidecars.delivery` names. `prep` is a caller's
    already-resolved prep block; `repo_root` None skips the sidecar fallback."""
    block = _delivery_block(record)
    if block is not None or repo_root is None:
        return block
    prep = prep if isinstance(prep, dict) else prep_of(repo_root, record)
    sidecars = prep.get("whole_diff_sidecars") if isinstance(prep, dict) else None
    rel = sidecars.get("delivery") if isinstance(sidecars, dict) else None
    fm = _frontmatter(repo_root / rel) if rel else None
    if isinstance(fm, dict) and fm.get("verdict") is None and fm.get("delivery_verdict") is not None:
        fm = {**fm, "verdict": fm["delivery_verdict"]}
    return fm


def _criterion_unsettled(record: dict) -> bool:
    """True when the run record's frozen criterion status is `not_met`, `indeterminate` or `not_run`."""
    for holder in (record, record.get("inline_review"), record.get("review")):
        block = holder.get("criterion") if isinstance(holder, dict) else None
        if isinstance(block, dict) and block.get("status") in ("not_met", "indeterminate", "not_run"):
            return True
    return False


def _tests_stale(record: dict) -> bool:
    """True when the run record's frozen build/test status is `fail`, `error` or `not_run`."""
    for holder in (record, record.get("inline_review"), record.get("review")):
        block = holder.get("tests") if isinstance(holder, dict) else None
        if isinstance(block, dict) and block.get("status") in ("fail", "error", "not_run"):
            return True
    return False


def _unsettled(record: dict) -> bool:
    return _criterion_unsettled(record) or _tests_stale(record)


def _null_slot(repo_root: Optional[Path], record: dict) -> bool:
    """True when the run's review ran (a `prep` block exists) but its delivery slot holds no verdict."""
    delivery = load_delivery(repo_root, record)
    if delivery is not None and delivery.get("verdict") is not None:
        return False
    prep = prep_of(repo_root, record) if repo_root is not None else record.get("prep")
    return isinstance(prep, dict)


def prior_unbacked_claims(
    record_path: Path, repo_root: Optional[Path] = None
) -> tuple[Dict[str, Any], List[dict]]:
    """`(record, claims)` of a run record's frozen delivery FAIL, each claim `{claim, anchor}`.
    A delivery PASS whose criterion is `not_met`/`indeterminate`, or whose tests
    are `fail`/`error`, returns `(record, [])`: there is
    nothing to re-check claim by claim, but the verdict and criterion are stale. A null delivery
    slot on a run whose review ran returns `(record, [])` too: the verdict was never produced.
    Raises `ReverifyRefused` when the record is unreadable, or when delivery PASSed and nothing else
    is stale (nothing to supersede). `repo_root` enables the `whole_diff_sidecars` delivery fallback."""
    record = _frontmatter(record_path)
    if record is None:
        raise ReverifyRefused(f"reverify-delivery: cannot read run record {record_path}")
    owner = repo_root or _repo_of_record(record_path)
    delivery = load_delivery(owner, record)
    if _null_slot(owner, record):
        return record, []
    if delivery is not None and delivery.get("verdict") != "FAIL" and _unsettled(record):
        return record, []
    if delivery is None or delivery.get("verdict") != "FAIL":
        raise ReverifyRefused(
            f"reverify-delivery: {record_path} carries no delivery FAIL, unmet/not-run criterion or failed/not-run tests "
            "to re-verify"
        )
    items = delivery.get("unbacked")
    if not isinstance(items, list):
        items = delivery.get("claims_unbacked")
    claims = [
        {"claim": c.get("claim"), "anchor": c.get("anchor")}
        for c in (items if isinstance(items, list) else [])
        if isinstance(c, dict) and c.get("claim")
    ]
    if not claims:
        raise ReverifyRefused(
            f"reverify-delivery: {record_path} delivery FAIL names no unbacked claims"
        )
    return record, claims


#: The judge reads the tree as it stands when it runs; the emit-time HEAD is a floor, not the
#: commit to judge, so evidence a peer lands between emit and fire counts.
_JUDGE_NOW_HEAD = (
    "Judge the tree as it stands now (HEAD was {head_sha} at emit; later commits count): "
    "follow-up commits may have fixed what the prior run's judge saw."
)


def _head_sha(repo_root: Path) -> str:
    proc = run_git(["rev-parse", "HEAD"], cwd=str(repo_root), timeout=30)
    if proc.returncode != 0:
        raise ReverifyRefused(f"reverify-delivery: git rev-parse HEAD failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _run_rel(repo_root: Path, record_path: Path) -> str:
    resolved = record_path if record_path.is_absolute() else repo_root / record_path
    try:
        return resolved.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _commit_credit_note(repo_root: Optional[Path], plan_path: str, base: str) -> str:
    """Brief text crediting the plan's `coded` rows: those at an ancestor of `base` are backed by
    construction; the rest name the plan's own commits, so a peer's commit in `base..HEAD` is not
    judged as the plan's; refs whose ancestry git could not read are named apart, never as the
    plan's own. Two git spawns total, three when a no-op row is re-keyed; empty when the spine names no coded SHA."""
    if repo_root is None:
        return ""
    by_ref, landed = coded_row_landings(plan_path, repo_root=repo_root, before=base)
    if not by_ref:
        return ""
    ok, unknown = ancestor_refs(repo_root, list(by_ref), base)
    before = [i for ref, ids in by_ref.items() if ref in ok for i in ids]
    own = [ref for ref in by_ref if ref not in ok and ref not in unknown]
    note = ""
    rekeyed = [f"{i} at {landed[i][:10]}" for i in before if i in landed]
    if rekeyed:
        note += (
            "Rows " + ", ".join(rekeyed) + " landed before this run's base; count them backed.\n"
        )
    if unknown:
        note += (
            "Ancestry of " + ", ".join(sorted(unknown)) + " to run_base_sha could not be read; "
            "decide it by the ancestry rule below and never judge them against base..HEAD.\n"
        )
    if before:
        note += (
            "Delivered before this run's base: rows " + ", ".join(before) + " are `coded` at an "
            "ancestor of run_base_sha; count them backed.\n"
        )
    if own:
        note += (
            "The plan's own commits are the `coded` rows' disposition_refs: " + ", ".join(own)
            + ". Judge those commits (`git show <sha>`), never the raw base..HEAD range: other "
            "sessions' commits and uncommitted edits in the shared tree are not this plan's "
            "delivery and are never a claim against it.\n"
        )
    if note:
        note += (
            "Ancestry rule: `git merge-base --is-ancestor <sha> <base>`; exit 0 is an ancestor, "
            "exit 1 is not, any other exit is unknown. Never infer non-ancestry from a sha's "
            "absence in a log range.\n"
        )
    return note


def _row_evidence_path(plan_path: str, repo_root: Optional[Path]) -> Optional[str]:
    """The plan's `evidence-append` sidecar as a repo-relative path, or None when it holds nothing.

    A claim backed by recorded evidence lives there, outside the plan body and outside any
    diff, so a verifier not pointed at it reports the claim unbacked."""
    from coordinator_core.ops.plan_tasks_mutate import evidence_sidecar_path, has_recorded_evidence

    plan_file = Path(plan_path)
    if not plan_file.is_absolute() and repo_root is not None:
        plan_file = Path(repo_root) / plan_file
    if not has_recorded_evidence(plan_file):
        return None
    return str(Path(plan_path).with_name(evidence_sidecar_path(plan_file).name)).replace("\\", "/")


def _register_evidence(plan_path: str, repo_root: Optional[Path]):
    """The plan's `JudgeEvidence` (register rows, PM words), or None when the plan is unreadable."""
    from coordinator_core.ops.requirement_register import plan_judge_evidence

    return plan_judge_evidence(plan_path, Path(repo_root) if repo_root is not None else Path.cwd())


def compose_reverify_script(
    *,
    fragment: dict,
    stage_schemas: dict,
    plan_path: str,
    run_record_rel: str,
    plan_id: Optional[str],
    run_base_sha: Optional[str],
    head_sha: str,
    claims: List[dict],
    rerun_tests: bool = False,
    repo_root: Optional[Path] = None,
    host_degraded: bool = False,
    diff_sha: Optional[str] = None,
) -> str:
    """A Workflow script holding one delivery-verifier agent call, briefed with the prior FAIL's
    unbacked claims and told to verify them at `head_sha`, then the roster's criterion judge
    (when declared) against the same HEAD. `diff_sha` (a null-slot run's terminal commit) makes the
    verifier judge `run_base..diff_sha` and is carried in the result ident."""
    review = parse_execute_review(fragment)
    agent = next((a for a in review.review_wave if a.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE), None)
    # A roster naming no verifier is delivery-exempt (review_stamp mint reads an absent verdict
    # as not-rostered), so the script still re-runs tests and the judge, with no delivery leg.
    claim_lines = "\n".join(
        f"- {c['claim']} [lacked: {c.get('anchor')}]" for c in claims
    )
    base = run_base_sha or "run_base_sha"
    criterion = resolve_operative_criterion_for_plan(plan_path, repo_root)
    if diff_sha:
        lead = (
            "Verify delivery for a run whose delivery verifier never returned a verdict. The reviewed "
            f"diff is `git diff {base}..{diff_sha}` (the run's terminal commit {diff_sha}); judge that "
            "diff, not HEAD, and verify the plan's deliverables against it. Return FAIL with "
            "claims_unbacked listing every claim unbacked, PASS only when all are backed.\n"
        )
    elif claims:
        lead = (
            "Re-verify delivery at HEAD. A prior verdict FAILed this run with the unbacked claims "
            "below; follow-up commits may have delivered them. Check EACH claim against the tree at "
            f"HEAD {head_sha} (the diff to judge is `git diff {base}..{head_sha}` plus the files as "
            "they stand at HEAD). Never judge from the original run's frozen diff: it predates the "
            "fixes. Return FAIL with claims_unbacked listing every claim still unbacked at HEAD, "
            "PASS only when all are backed.\n"
        )
    else:
        lead = (
            "Re-verify delivery at HEAD. The prior verdict PASSed but the run's exit criterion was "
            "not met; follow-up commits may have changed what is delivered. Verify the plan's "
            f"deliverables against the tree at HEAD {head_sha} (the diff to judge is "
            f"`git diff {base}..{head_sha}` plus the files as they stand at HEAD). Return FAIL with "
            "claims_unbacked listing every claim unbacked at HEAD, PASS only when all are backed.\n"
        )
    lead += _commit_credit_note(repo_root, plan_path, base)
    owned = _em_owned_rows(plan_path, repo_root)
    lead += em_owned_clause(owned).lstrip() + (chr(10) if owned else "")
    evidence_path = _row_evidence_path(plan_path, repo_root)
    if evidence_path:
        lead += (
            f"Row evidence recorded through evidence-append is in {evidence_path}; a claim it "
            "records is backed there, not only by the diff.\n"
        )
    prompt = (
        f"{_DELIVERY_VERIFIER_ROLE_PREAMBLE}\n\n"
        f"{lead}"
        f"{delivery_supersession_clause(criterion).lstrip()}{chr(10) if criterion and criterion.superseded else ''}"
        f"{_NO_SIDECAR_NOTE}\n"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {base}\n"
        f"head_sha: {head_sha}\n"
        f"supersedes: {run_record_rel}\n"
        f"prior_unbacked_claims:\n{claim_lines or '(none)'}"
    )
    owner_rows = _owner_gate_rows(plan_path, repo_root)
    engine_rows = _engine_rows(plan_path, repo_root)
    if owner_rows or engine_rows:
        prompt += owner_repo_clause(owner_rows + engine_rows, owner_repo_roots())
    call = agent and _sidecar(
        _agent_call_literal(
            _DELIVERY_VERIFIER_HOST_NATIVE_TYPE,
            prompt,
            _PHASE,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(agent, emitted_agent_type=_DELIVERY_VERIFIER_HOST_NATIVE_TYPE),
            schema_literal=_schema_literal(agent.schema, stage_schemas),
        ),
        plan_path,
        f"delivery-{_DELIVERY_VERIFIER_HOST_NATIVE_TYPE}",
    )
    judge_kwargs = dict(
        stage_schemas=stage_schemas,
        plan_path=plan_path,
        run_base_sha=base,
        falsifier=None,
        criterion=criterion,
        prompt_head=_JUDGE_NOW_HEAD.format(head_sha=head_sha) + owner_repo_clause(engine_rows, {}),
        host_degraded=host_degraded,
        evidence_path=evidence_path,
        evidence=_register_evidence(plan_path, repo_root),
        em_owned_rows=owned,
    )
    judge_call = compose_criterion_judge(review, sidecar_workflow="reverify", **judge_kwargs)
    phases = [_js_string_literal(_PHASE)] if call else []
    tests_lines = ""
    tests_field = ""
    tests_ident: dict[str, str] = {}
    # A degraded host runs the judge as general-purpose, which has Write: keep the two legs there.
    verify_merge = rerun_tests and judge_call is not None and not host_degraded
    judge_lines = ""
    criterion_field = ""
    if verify_merge:
        parts = criterion_judge_parts(review, **judge_kwargs)
        test_schema = json.loads(stage_schema_literal("test_result"))
        test_schema["required"] = [k for k in test_schema["required"] if k != "sidecar_path"]
        verify_schema = {
            "type": "object",
            "properties": {"test": test_schema, "judge": parts.schema},
            "required": ["judge"],
            "additionalProperties": False,
        }
        verify_prompt = (
            "Verify in two steps, in order, in one run.\n\n"
            "Step 1 -- scoped tests. " + _tests_prompt(head_sha, plan_path, base)
            + "\n\nStep 2 -- criterion verdict. Only once the step 1 result is in, rule the criterion below. "
            "A failing, errored or degraded test result is evidence for your verdict, never the verdict itself. "
            "Return the verdict under `judge`.\n\n"
            + parts.prompt
        )
        verify_call = _sidecar(
            _agent_call_literal(
                parts.agent_type,
                verify_prompt,
                _VERIFY_PHASE,
                schema=True,
                as_arrow=False,
                agent_opts=_agent_opts_for(review.judge, emitted_agent_type=parts.agent_type),
                schema_literal=json.dumps(verify_schema, sort_keys=True),
            ),
            plan_path,
            f"verify-{parts.agent_type}",
        )
        phases.append(_js_string_literal(_VERIFY_PHASE))
        tests_lines = (
            f"  phase({_js_string_literal(_VERIFY_PHASE)});\n"
            f"  const _verify = await {verify_call};\n"
            "  const _tests = _verify.test ?? null;\n"
            "  const _judge = _verify.judge ?? null;\n"
        )
        tests_field = _TESTS_FIELD
        criterion_field = _CRITERION_FIELD
        tests_ident["tests_agent_type"] = parts.agent_type
    elif rerun_tests:
        phases.append(_js_string_literal(_TESTS_PHASE))
        tests_prompt = _tests_prompt(head_sha, plan_path, base, write_record=True)
        tests_type, tests_role = _host_native(review.prep.agent_type, host_degraded)
        tests_ident["tests_agent_type"] = tests_type
        tests_call = _sidecar(
            _agent_call_literal(
                tests_type,
                tests_role + tests_prompt,
                _TESTS_PHASE,
                schema=True,
                as_arrow=False,
                agent_opts=_agent_opts_for(review.prep),
                schema_literal=stage_schema_literal("test_result"),
            ),
            plan_path,
            f"tests-{tests_type}",
        )
        tests_lines = (
            f"  phase({_js_string_literal(_TESTS_PHASE)});\n"
            f"  const _tests = await {tests_call};\n"
        )
        tests_field = _TESTS_FIELD
    if judge_call is not None and not verify_merge:
        phases.append(_js_string_literal(CRITERION_JUDGE_PHASE_TITLE))
        judge_lines = (
            f"  phase({_js_string_literal(CRITERION_JUDGE_PHASE_TITLE)});\n"
            f"  const _judge = await {judge_call};\n"
        )
        criterion_field = _CRITERION_FIELD
    meta = (
        "export const meta = {\n"
        "  name: 'reverify-delivery',\n"
        "  description: 'Re-run the delivery verifier and criterion judge at HEAD over a prior delivery FAIL.',\n"
        f"  phases: [{', '.join(phases)}],\n"
        "};\n"
    )
    ident = {
        "supersedes": run_record_rel,
        "plan_id": plan_id,
        "head_sha": head_sha,
        "plan_path": plan_path,
        **({"diff_sha": diff_sha} if diff_sha else {}),
        **tests_ident,
    }
    return (
        "// Runs inside the Workflow runner; a top-level `return` is legal there.\n"
        f"{meta}\n"
        + (f"  phase({_js_string_literal(_PHASE)});\n  const _delivery = await {call};\n" if call else "  const _delivery = null;\n")
        + f"{tests_lines}"
        f"{judge_lines}"
        f"  return {{ reverify_delivery: {json.dumps(ident, sort_keys=True)}, "
        "verdict: _delivery?.verdict ?? null, "
        "claims_unbacked: (_delivery?.claims_unbacked ?? []).map(c => ({ claim: c && c.claim, anchor: c && c.anchor }))"
        f"{criterion_field}{tests_field} }};\n"
    )


def _refuse_unless_zero_claim_settled(
    unbacked: List[dict], criterion: Optional[dict], tests: Optional[dict]
) -> None:
    """A null verdict is recordable only as a delivery-exempt record: the result names no
    delivery claim, its criterion is `met` and its tests `pass`. Anything else raises with the
    remedy that clears it."""
    if unbacked:
        raise ReverifyRefused(
            "reverify-delivery: result verdict None carries unbacked claims; "
            "re-emit with --reverify-delivery so the delivery verifier returns PASS or FAIL"
        )
    criterion_status = (criterion or {}).get("status")
    tests_status = (tests or {}).get("status")
    if criterion_status != "met" or tests_status != "pass":
        raise ReverifyRefused(
            "reverify-delivery: result verdict None records only with criterion status 'met' and "
            f"tests status 'pass' (got criterion {criterion_status!r}, tests {tests_status!r}); "
            "settle the criterion with `review-stamp rejudge` and the tests with a passing "
            "test-runner sidecar passed as --build-test, then mint"
        )


def record_delivery_verdict(
    *,
    repo_root: Path,
    supersedes: str,
    plan_id: Optional[str],
    head_sha: str,
    verdict: Optional[str],
    unbacked: List[dict],
    session_id: str = "",
    criterion: Optional[dict] = None,
    tests: Optional[dict] = None,
    foreign_claims: Optional[List[str]] = None,
    diff_sha: Optional[str] = None,
) -> str:
    """Exclusive-create the superseding delivery-verdict record; returns its repo-relative path.
    `diff_sha` binds the record to the reviewed terminal commit a null-slot re-run judged."""
    if verdict is None:
        _refuse_unless_zero_claim_settled(unbacked, criterion, tests)
    elif verdict not in ("PASS", "FAIL"):
        raise ReverifyRefused(f"reverify-delivery: result verdict {verdict!r} is neither PASS nor FAIL")
    now = datetime.now(timezone.utc)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", Path(supersedes).stem)
    rel = VERDICT_DIR / now.strftime("%Y-%m") / f"{stem}.{now.strftime('%Y%m%dT%H%M%S%fZ')}.md"
    fm = {
        "kind": RECORD_KIND,
        "supersedes": supersedes,
        "plan_id": plan_id,
        "head_sha": head_sha,
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": session_id,
        "delivery": {"verdict": verdict, "unbacked": unbacked if verdict == "FAIL" else []},
    }
    if diff_sha:
        fm[DIFF_SHA_KEY] = diff_sha
    if criterion is not None:
        status = criterion.get("status")
        if status not in _CRITERION_STATUSES:
            raise ReverifyRefused(f"reverify-delivery: criterion status {status!r} is not one of {_CRITERION_STATUSES}")
        fm["criterion"] = {"status": status, "observation": criterion.get("observation"), "sidecar": None}
    if tests is not None:
        status = tests.get("status")
        # The re-verify workflow emits `pass-with-skips`; every reader takes the record's status as
        # the terminal verdict, so it lands as `pass` and the skip as its own field.
        skipped = status == "pass-with-skips"
        if skipped:
            status = "pass"
        if status not in _TESTS_STATUSES:
            raise ReverifyRefused(f"reverify-delivery: tests status {status!r} is not one of {_TESTS_STATUSES}")
        fm["tests"] = {
            "status": status,
            "run": tests.get("run"),
            "failed": tests.get("failed"),
            "sidecar": tests.get("sidecar"),
        }
        for key in ("baseline_method", "baseline"):
            if tests.get(key) is not None:
                fm["tests"][key] = tests[key]
        if skipped:
            fm["tests"]["skipped"] = True
    if foreign_claims is not None:
        fm["foreign_claims"] = list(foreign_claims)
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False) + "---\n")
    declare_write(str(target))
    return rel.as_posix()


def settle_tests_sidecar(
    repo_root: Path,
    tests: Optional[dict],
    *,
    plan_path: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> bool:
    """Write the re-run verdict into the test-runner sidecar through `record_test_verdict`
    (`test_verdict`, `target_plan`, `agent_type`). When the writer refuses (already verdicted,
    contradictory counts, foreign owner), fall back to `bind_sidecar`: binding alone, `status`
    untouched. Returns whether the sidecar was rewritten."""
    if not tests or tests.get("status") not in (*_TESTS_STATUSES, "pass-with-skips") or not tests.get("sidecar"):
        return False
    result = {
        "status": tests["status"],
        "tests_run": tests.get("run"),
        "tests_failed": tests.get("failed"),
        "sidecar_path": str(tests["sidecar"]),
    }
    for key in ("baseline_method", "baseline"):
        if tests.get(key) is not None:
            result[key] = tests[key]
    try:
        record_test_verdict(repo_root, result, plan_path=plan_path, agent_type=agent_type)
    except TestVerdictRefused:
        return bind_sidecar(repo_root, str(tests["sidecar"]), plan_path=plan_path, agent_type=agent_type)
    return True


def persist_delivery_sidecar(
    *,
    repo_root: Path,
    plan_path: Optional[str],
    session_id: str,
    head_sha: str,
    verdict: str,
    unbacked: List[dict],
) -> Optional[str]:
    """Write the verifier's returned verdict into a provisioned `delivery-verifier` run-report in
    the session share dir, `agent_type` and `target_plan` stamped here: the verifier has no Write
    tool. Exclusive-create per `(plan, head)`; returns the repo-relative path, or `None` when the
    result names no plan or a segment is unsafe."""
    from coordinator_core.session.machinery_paths import share_dir
    from coordinator_core.subagent_sandbox.provision_report import _build_doc_text, _sanitize_segment

    stem = _sanitize_segment(Path(plan_path).stem) if plan_path else None
    session = _sanitize_segment(session_id or "unknown-session")
    if stem is None or session is None:
        return None
    now = datetime.now(timezone.utc)
    doc = _build_doc_text(
        _DELIVERY_SIDECAR_AGENT_TYPE, now.isoformat(), "run-report",
        lead_session_id=session_id or None, target_plan=plan_path,
    )
    split = split_frontmatter(doc)
    if split is None:
        return None
    add = {"verdict": verdict, "head_sha": head_sha, "claims_unbacked": unbacked if verdict == "FAIL" else []}
    fm_text = split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n"
    fm_text += yaml.safe_dump(add, default_flow_style=False, sort_keys=False)
    doc = doc.replace(split.fm_text, fm_text, 1) + f"\n# Delivery verdict: {verdict}\n"
    target = Path(share_dir(str(repo_root), session)) / f"{stem}.delivery-reverify.{head_sha[:12]}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(doc)
    except FileExistsError:
        pass
    else:
        declare_write(str(target))
    return target.relative_to(repo_root).as_posix() if target.is_relative_to(repo_root) else target.as_posix()


_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _claim_held(path: str, cwd: str) -> bool:
    """True when a live session holds a blocking (write or unknown-kind) claim on `path`, or the
    ledger cannot answer. A read-kind touch never holds."""
    from coordinator_core.session import claim_index, liveness, touch_record

    try:
        found = claim_index.lookup([path], cwd=cwd)
        claimants = found.get(path, [])
        if claim_index.UNANSWERABLE in claimants:
            return True
        kinds = (found.recorded_kind or {}).get(path, {})
        return any(
            touch_record.kind_blocks_a_peer_commit(kinds.get(sid)) and liveness.session_live(sid, cwd)
            for sid in claimants
        )
    except Exception:  # noqa: BLE001 - an unanswerable ledger must keep the claim blocking
        return True


def live_held_claims(repo_root: Path, frozen: List[Any]) -> List[str]:
    """The subset of `frozen` (`"<path> ..."` strings) whose path a live session still holds a
    blocking claim on, answered by one `claim_index.lookup` over every path. An unanswerable
    ledger returns every claim (fail closed)."""
    from coordinator_core.session import claim_index, liveness, touch_record

    claims = [str(c) for c in frozen]
    if not claims:
        return []
    cwd = str(repo_root)
    paths = sorted({c.split(" ", 1)[0] for c in claims})
    try:
        found = claim_index.lookup(paths, cwd=cwd)
        live_by_sid: Dict[str, bool] = {}
        held = set()
        for path in paths:
            claimants = found.get(path, [])
            if claim_index.UNANSWERABLE in claimants:
                return claims
            kinds = (found.recorded_kind or {}).get(path, {})
            for sid in claimants:
                if not touch_record.kind_blocks_a_peer_commit(kinds.get(sid)):
                    continue
                if sid not in live_by_sid:
                    live_by_sid[sid] = bool(liveness.session_live(sid, cwd))
                if live_by_sid[sid]:
                    held.add(path)
                    break
    except Exception:  # noqa: BLE001 - an unanswerable ledger must keep the claims blocking
        return claims
    return [c for c in claims if c.split(" ", 1)[0] in held]


def live_foreign_claims(repo_root: Path, frozen: List[Any], head_sha: str) -> List[str]:
    """The subset of `frozen` (`"<path> ..."` strings) still blocking at `head_sha`: its path is
    held by a live session, or a commit it names is not an ancestor of `head_sha`."""
    cwd = str(repo_root)
    live: List[str] = []
    for claim in frozen:
        text = str(claim)
        path = text.split(" ", 1)[0]
        if _claim_held(path, cwd):
            live.append(text)
            continue
        for sha in _SHA_RE.findall(text):
            proc = run_git(["merge-base", "--is-ancestor", sha, head_sha], cwd=cwd, timeout=30)
            if proc.returncode != 0:
                live.append(text)
                break
    return live


def _frozen_foreign_claims(repo_root: Path, record: dict) -> Optional[List[Any]]:
    prep = prep_of(repo_root, record)
    claims = prep.get("foreign_claims") if isinstance(prep, dict) else None
    return claims if isinstance(claims, list) else None


_BOOKKEEPING_GLOB = ".coordinator-local/subagent-share/*/*.review-wave-bookkeeping.md (plan_id: <plan_id>)"


def _plan_id_of(plan_path: str) -> Optional[str]:
    fm = _frontmatter(Path(plan_path))
    return str(fm["plan_id"]) if fm and fm.get("plan_id") else None


def _is_run_record(fm: Optional[Dict[str, Any]]) -> bool:
    """True for run-record frontmatter; a completion receipt carries plan keys but no delivery."""
    if not fm or fm.get("schema") == "completion-receipt":
        return False
    return any(k in fm for k in ("delivery", "prep", "commit_range", "inline_review", "review"))


def _has_reverifiable(repo_root: Path, fm: Dict[str, Any]) -> bool:
    block = load_delivery(repo_root, fm)
    if _null_slot(repo_root, fm):
        return True
    return bool(block) and (block.get("verdict") == "FAIL" or _unsettled(fm))


def _terminal_of(repo_root: Path, plan_id: str, plan_path: Optional[str]) -> Optional[tuple[str, Path]]:
    """`(terminal_sha, integration_sidecar_path)` of the run's `Inline-Review` commit; `None` when none resolves."""
    from coordinator_core.ops.review_stamp import MintRefusal, _resolve_terminal_commit

    try:
        sha, path, _data = _resolve_terminal_commit(
            repo_root, plan_id, Path(plan_path) if plan_path else None
        )
    except MintRefusal:
        return None
    return sha, path


def _trailer_record(repo_root: Path, plan_id: str, plan_path: Optional[str]) -> Optional[Path]:
    found = _terminal_of(repo_root, plan_id, plan_path)
    return found[1] if found else None


def resolve_delivery_in_force(
    repo_root: Path,
    plan_id: Optional[str],
    plan_path: Optional[str] = None,
    run_record: Optional[str] = None,
) -> tuple[Optional[Path], Optional[dict]]:
    """`(run_record_path, delivery_block)` in force for a plan: the given run record when it is
    one, else the `Inline-Review: applies <stem>` record carrying a re-verifiable delivery, else
    the plan's review-wave bookkeeping record. The block is the newest superseding verdict's,
    else the record's frozen one. `(None, None)` when nothing resolves."""
    path: Optional[Path] = None
    if run_record and run_record.strip():
        given = Path(run_record)
        given = given if given.is_absolute() else repo_root / given
        if _is_run_record(_frontmatter(given)):
            path = given
    if path is None and plan_id:
        cand = _trailer_record(repo_root, plan_id, plan_path)
        fm = _frontmatter(cand) if cand else None
        if cand is not None and fm and _has_reverifiable(repo_root, fm):
            path = cand
    if path is None:
        path = _bookkeeping_record(repo_root, plan_id)
    if path is None:
        return None, None
    block = latest_delivery_supersession(repo_root, _run_rel(repo_root, path))
    if block is None:
        fm = _frontmatter(path)
        block = load_delivery(repo_root, fm) if fm else None
    return path, block


def _bookkeeping_record(repo_root: Path, plan_id: Optional[str]) -> Optional[Path]:
    """The newest review-wave bookkeeping record for `plan_id` carrying a delivery FAIL."""
    if not plan_id:
        return None
    share = repo_root / ".coordinator-local" / "subagent-share"
    candidates = sorted(
        share.glob("*/*.review-wave-bookkeeping.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        fm = _frontmatter(path)
        if not fm or str(fm.get("plan_id")) != plan_id:
            continue
        block = load_delivery(repo_root, fm)
        if _null_slot(repo_root, fm) or (block and (block.get("verdict") == "FAIL" or _unsettled(fm))):
            return path
    return None


def emit_reverify(
    *, repo_root: Path, plan_path: str, run_record: str, out_path: str, agent_type_host: Optional[str] = None
) -> dict:
    from coordinator_core.ops.dispatch_emit.emit import _AGENT_TYPE_HOST_DEGRADED
    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

    blank = not run_record.strip()
    record_file = Path(run_record)
    if not blank and not record_file.is_absolute():
        record_file = repo_root / record_file
    if not blank and record_file.is_absolute() and _is_run_record(_frontmatter(record_file)):
        owner = _repo_of_record(record_file)
        if owner != repo_root.resolve():
            repo_root = owner
    if blank or not _is_run_record(_frontmatter(record_file)):
        # Blank, or not a run record (a task .output or completion receipt): resolve from the plan.
        plan_id = _plan_id_of(plan_path)
        found = resolve_delivery_in_force(repo_root, plan_id, plan_path)[0]
        if found is None:
            glob = _BOOKKEEPING_GLOB.replace("<plan_id>", plan_id or "<plan_id>")
            what = "no run record given" if blank else f"cannot read run record {record_file}"
            raise ReverifyRefused(
                f"reverify-delivery: {what}, and no {glob} carries this plan's delivery FAIL"
            )
        record_file = found
    record, claims = prior_unbacked_claims(record_file, repo_root)
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    raw_prep = record.get("prep")
    prep = raw_prep if isinstance(raw_prep, dict) else {}
    raw_range = record.get("commit_range")
    commit_range = raw_range if isinstance(raw_range, dict) else {}
    plan_id = record.get("plan_id")
    if not plan_id:
        split = split_frontmatter(Path(plan_path).read_text(encoding="utf-8", errors="replace"))
        plan_id = read_fm_field_unquoted(split.fm_text, "plan_id") if split else None
    diff_sha = None
    if _null_slot(repo_root, record):
        terminal = _terminal_of(repo_root, str(plan_id), plan_path) if plan_id else None
        if terminal is None:
            raise ReverifyRefused(
                f"reverify-delivery: {record_file} has a null delivery slot and no Inline-Review "
                "terminal commit resolves for the plan; nothing to bind the re-run verdict to"
            )
        diff_sha = terminal[0]
    rel = _run_rel(repo_root, record_file)
    script = compose_reverify_script(
        fragment=fragment,
        stage_schemas=schemas,
        plan_path=plan_path,
        run_record_rel=rel,
        plan_id=plan_id,
        run_base_sha=prep.get("run_base_sha") or commit_range.get("base"),
        head_sha=_head_sha(repo_root),
        claims=claims,
        rerun_tests=_tests_stale(record),
        repo_root=repo_root,
        host_degraded=agent_type_host == _AGENT_TYPE_HOST_DEGRADED,
        diff_sha=diff_sha,
    )
    Path(out_path).write_text(script, encoding="utf-8", newline="\n")
    from coordinator_core.ops.dispatch_emit.emission_receipt import _write_emission_receipt

    receipt = _write_emission_receipt(
        Path(out_path), plan_path, {}, extras={"route": "reverify-delivery", "supersedes": rel}
    )
    return {"path": out_path, "supersedes": rel, "claims": len(claims), "receipt": receipt}


def _rejudge_judge_kwargs(
    stage_schemas: dict,
    plan_path: str,
    run_base_sha: Optional[str],
    head_sha: str,
    repo_root: Optional[Path],
    host_degraded: bool,
) -> dict:
    """The judge-call keywords the rejudge script and the Agent-tool brief both render from."""
    return {
        "stage_schemas": stage_schemas,
        "plan_path": plan_path,
        "run_base_sha": run_base_sha or "run_base_sha",
        "falsifier": None,
        "criterion": resolve_operative_criterion_for_plan(plan_path, repo_root),
        "prompt_head": _JUDGE_NOW_HEAD.format(head_sha=head_sha)
        + owner_repo_clause(_engine_rows(plan_path, repo_root), {}),
        "host_degraded": host_degraded,
        "evidence_path": _row_evidence_path(plan_path, repo_root),
        "evidence": _register_evidence(plan_path, repo_root),
        "em_owned_rows": _em_owned_rows(plan_path, repo_root),
    }


def _rejudge_ident(
    plan_id: Optional[str], head_sha: str, plan_path: str, run_base_sha: Optional[str]
) -> dict:
    return {
        "rejudge": True, "plan_id": plan_id, "head_sha": head_sha, "plan_path": plan_path,
        "run_base_sha": run_base_sha,
    }


def compose_rejudge_script(
    *,
    fragment: dict,
    stage_schemas: dict,
    plan_path: str,
    plan_id: Optional[str],
    run_base_sha: Optional[str],
    head_sha: str,
    repo_root: Optional[Path] = None,
    host_degraded: bool = False,
) -> str:
    """A Workflow script holding only the roster's criterion judge, run at HEAD; its returned
    `criterion` is what `record` turns into a `criterion-rejudge` record."""
    review = parse_execute_review(fragment)
    call = compose_criterion_judge(
        review,
        **_rejudge_judge_kwargs(
            stage_schemas, plan_path, run_base_sha, head_sha, repo_root, host_degraded
        ),
    )
    if call is None:
        raise ReverifyRefused("rejudge: the review roster declares no criterion judge")
    ident = _rejudge_ident(plan_id, head_sha, plan_path, run_base_sha)
    return (
        "// Runs inside the Workflow runner; a top-level `return` is legal there.\n"
        "export const meta = {\n"
        "  name: 'rejudge-criterion',\n"
        "  description: 'Run only the criterion judge at HEAD over the exit criterion of the plan.',\n"
        f"  phases: [{_js_string_literal(CRITERION_JUDGE_PHASE_TITLE)}],\n"
        "};\n\n"
        f"  phase({_js_string_literal(CRITERION_JUDGE_PHASE_TITLE)});\n"
        f"  const _judge = await {call};\n"
        f"  return {{ reverify_delivery: {json.dumps(ident, sort_keys=True)}, "
        "criterion: _judge ? { status: _judge.status ?? null, observation: _judge.observation ?? null, "
        "register_rows: _judge.register_rows ?? null } : null };\n"
    )


def recorded_unmet_criterion(
    repo_root: Path, plan_id: Optional[str], plan_path: Optional[str] = None
) -> Optional[dict]:
    """`{status, run_base_sha}` when the delivery verdict in force for a plan records its exit
    criterion `not_met`/`indeterminate` (the newest superseding verdict's, else the run record's
    frozen one); `None` when nothing resolves or the criterion is not unmet."""
    path, _block = resolve_delivery_in_force(repo_root, plan_id, plan_path)
    fm = _frontmatter(path) if path else None
    if not fm:
        return None
    criterion = latest_criterion_supersession(repo_root, _run_rel(repo_root, path))
    if criterion is None:
        for holder in (fm, fm.get("inline_review"), fm.get("review")):
            block = holder.get("criterion") if isinstance(holder, dict) else None
            if isinstance(block, dict) and block.get("status"):
                criterion = block
                break
    status = criterion.get("status") if criterion else None
    if status not in ("not_met", "indeterminate"):
        return None
    prep = prep_of(repo_root, fm)
    return {"status": status, "run_base_sha": prep.get("run_base_sha") if isinstance(prep, dict) else None}


def _refuse_open_rows(plan_path: str) -> None:
    """Raise unless every spine row is `coded` or deferred (spun off, backlogged, wont_do, voided)."""
    from coordinator_core.ops.dispatch_emit.spine_read import (
        NON_DISPATCHABLE_DISPOSITIONS,
        SpineReadError,
        load_rows_memo,
        with_canonical_disposition,
    )

    try:
        result = load_rows_memo(Path(plan_path).read_text(encoding="utf-8", errors="replace"))
        rows = [with_canonical_disposition(r) for r in result.rows]
    except (SpineReadError, OSError, AttributeError, TypeError) as exc:
        raise ReverifyRefused(f"rejudge: {plan_path} task spine unreadable: {exc}") from exc
    open_ids = [
        str(r.get("id")) for r in rows
        if isinstance(r, dict) and r.get("disposition") not in NON_DISPATCHABLE_DISPOSITIONS
    ]
    if not rows or open_ids:
        named = ", ".join(open_ids) if open_ids else "no spine rows"
        raise ReverifyRefused(f"rejudge: {plan_path} has rows not coded: {named}")


def _hand_worked_run_base(repo_root: Path, plan_path: str, fm: Any) -> Optional[str]:
    """Where the plan's work started: its `execution_authorized_sha`, else the commit that
    added the plan file (one git read); `None` when neither resolves."""
    authorized = fm.get("execution_authorized_sha") if isinstance(fm, dict) else None
    if authorized:
        return str(authorized)
    rel = _run_rel(repo_root, Path(plan_path).resolve())
    # An untracked plan makes `git log` walk all history; the index lookup bounds that.
    if run_git(["ls-files", "--error-unmatch", "--", rel], cwd=str(repo_root), timeout=30).returncode != 0:
        return None
    proc = run_git(
        ["log", "--diff-filter=A", "--max-count=1", "--format=%H", "--", rel],
        cwd=str(repo_root), timeout=30,
    )
    sha = proc.stdout.strip() if proc.returncode == 0 else ""
    return sha or None


def _register_gate_demotes(repo_root: Path, plan_path: str, criterion: dict) -> bool:
    """True when `terminal_commit._register_gate` would demote this met stamp criterion to
    `not_met`; the gate runs on a copy, so nothing is written."""
    from coordinator_core.ops.dispatch_emit.terminal_commit import _register_gate

    try:
        plan_rel = Path(plan_path).resolve().relative_to(Path(repo_root).resolve()).as_posix()
    except ValueError:
        return False
    return _register_gate(repo_root, plan_rel, {"criterion": dict(criterion)}) is not None


def _rejudge_subject(repo_root: Path, plan_path: str) -> "tuple[Optional[str], Optional[str]]":
    """`(plan_id, run_base_sha)` of a plan eligible for a criterion rejudge; raises
    `ReverifyRefused` for one that is not."""
    text = Path(plan_path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    split = split_frontmatter(text)
    try:
        fm = (yaml.safe_load(split.fm_text) or {}) if split else {}
    except yaml.YAMLError:
        fm = {}
    stamp = fm.get("review_stamp") if isinstance(fm, dict) else None
    criterion = stamp.get("criterion") if isinstance(stamp, dict) else None
    plan_id = str(fm.get("plan_id") or "") or None if isinstance(fm, dict) else None
    if isinstance(criterion, dict) and (
        criterion.get("status") != "met" or _register_gate_demotes(repo_root, plan_path, criterion)
    ):
        run_base_sha = stamp.get("run_base_sha")
    else:
        recorded = recorded_unmet_criterion(repo_root, plan_id, plan_path)
        if recorded is not None:
            run_base_sha = recorded.get("run_base_sha")
        elif stamp is None:
            _refuse_open_rows(plan_path)
            run_base_sha = _hand_worked_run_base(repo_root, plan_path, fm)
        else:
            raise ReverifyRefused(
                f"rejudge: {plan_path} carries no unmet review_stamp criterion and no recorded "
                "delivery verdict with an unmet exit criterion to re-judge"
            )
    return plan_id, run_base_sha


def emit_rejudge_brief(
    *, repo_root: Path, plan_path: str, agent_type_host: Optional[str] = None
) -> dict:
    """The criterion judge as an Agent-tool brief `{agent_type, model, prompt, schema, ident}`,
    under the refusals `emit_rejudge` applies; writes nothing and fires nothing. Its prompt and
    schema are the ones the rejudge script embeds."""
    from coordinator_core.ops.dispatch_emit.emit import _AGENT_TYPE_HOST_DEGRADED
    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN

    plan_id, run_base_sha = _rejudge_subject(repo_root, plan_path)
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    head_sha = _head_sha(repo_root)
    parts = criterion_judge_parts(
        parse_execute_review(fragment),
        **_rejudge_judge_kwargs(
            schemas, plan_path, run_base_sha, head_sha, repo_root,
            agent_type_host == _AGENT_TYPE_HOST_DEGRADED,
        ),
    )
    if parts is None:
        raise ReverifyRefused("rejudge: the review roster declares no criterion judge")
    return {
        "agent_type": parts.agent_type,
        "model": parts.model,
        "prompt": parts.prompt,
        "schema": parts.schema,
        "ident": _rejudge_ident(plan_id, head_sha, plan_path, run_base_sha),
    }


def emit_rejudge(
    *, repo_root: Path, plan_path: str, out_path: str, agent_type_host: Optional[str] = None
) -> dict:
    """Emit the judge-only script for a plan whose `review_stamp` records an unmet criterion,
    whose delivery verdict records one, or that carries no `review_stamp` and has every row coded."""
    from coordinator_core.ops.dispatch_emit.emit import _AGENT_TYPE_HOST_DEGRADED
    from coordinator_core.ops.dispatch_emit.emission_receipt import _load_review_inputs, _write_emission_receipt
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN

    plan_id, run_base_sha = _rejudge_subject(repo_root, plan_path)
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    script = compose_rejudge_script(
        fragment=fragment,
        stage_schemas=schemas,
        plan_path=plan_path,
        plan_id=plan_id,
        run_base_sha=run_base_sha,
        head_sha=_head_sha(repo_root),
        repo_root=repo_root,
        host_degraded=agent_type_host == _AGENT_TYPE_HOST_DEGRADED,
    )
    Path(out_path).write_text(script, encoding="utf-8", newline="\n")
    receipt = _write_emission_receipt(Path(out_path), plan_path, {}, extras={"route": "rejudge-criterion"})
    return {"path": out_path, "receipt": receipt}


def record_criterion_rejudge(
    *,
    repo_root: Path,
    plan_id: Optional[str],
    head_sha: str,
    criterion: Optional[dict],
    session_id: str = "",
    run_base_sha: Optional[str] = None,
) -> str:
    """Exclusive-create the `criterion-rejudge` record from the judge's returned `criterion`;
    returns its repo-relative path. Carries no delivery block, so delivery supersession
    never reads it."""
    status = (criterion or {}).get("status")
    if not plan_id or status not in _CRITERION_STATUSES:
        raise ReverifyRefused(
            f"rejudge: needs a plan_id and a criterion status in {_CRITERION_STATUSES}, got {status!r}"
        )
    try:
        judged_head = _head_sha(repo_root)
    except ReverifyRefused:
        judged_head = head_sha
    now = datetime.now(timezone.utc)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", plan_id)
    rel = VERDICT_DIR / now.strftime("%Y-%m") / f"{stem}.rejudge.{now.strftime('%Y%m%dT%H%M%S%fZ')}.md"
    fm = {
        "kind": REJUDGE_KIND,
        "plan_id": plan_id,
        "head_sha": judged_head,
        "emitted_head_sha": head_sha,
        "run_base_sha": run_base_sha,
        "recorded_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "session_id": session_id,
        "criterion": {"status": status, "observation": criterion.get("observation"), "sidecar": None},
    }
    rows = criterion.get("register_rows")
    if isinstance(rows, list):
        # review_stamp.rejudge runs terminal_commit's register gate over these.
        fm["criterion"]["register_rows"] = [r for r in rows if isinstance(r, dict)]
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False) + "---\n")
    declare_write(str(target))
    return rel.as_posix()


def _unwrap_task_output(result: Any) -> dict:
    """The result inside a Workflow task-output wrapper (`result`/`output`/`return_value`,
    possibly JSON text); anything else raises."""
    if isinstance(result, dict):
        for key in ("result", "output", "return_value", "returnValue"):
            inner = result.get(key)
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except ValueError:
                    continue
            if isinstance(inner, dict) and "reverify_delivery" in inner:
                return inner
    raise ValueError("result carries no reverify_delivery payload, bare or in a task-output wrapper")


def _em_owned_rows(plan_path: str, repo_root: Optional[Path]) -> List[str]:
    """The plan's EM-owned rows as the emit run saw them; empty when the spine cannot be read,
    since a re-verify must still run against a plan whose spine has since changed shape."""
    from coordinator_core.ops.dispatch_emit.spine_read import em_owned_rows, read_spine

    plan_file = Path(plan_path)
    if not plan_file.is_absolute() and repo_root is not None:
        plan_file = Path(repo_root) / plan_file
    exclusions: list = []
    try:
        read_spine(plan_file, exclusions=exclusions, schema_preflight=False)
    except Exception:
        return []
    return em_owned_rows(exclusions)


def _owner_gate_rows(plan_path: str, repo_root: Optional[Path]) -> List[dict]:
    """``{id, owner_repo, closure_key?}`` for each uncleared execution-blocking ``external_gate``
    entry naming an ``owner_repo``, row-level or frontmatter; empty when the spine is unreadable."""
    from coordinator_core.ops.dispatch_emit.spine_read import (
        _frontmatter_external_gates,
        _has_uncleared_execution_gate,
        load_rows_memo,
    )

    plan_file = Path(plan_path)
    if not plan_file.is_absolute() and repo_root is not None:
        plan_file = Path(repo_root) / plan_file
    try:
        source = plan_file.read_text(encoding="utf-8")
        raw_rows = list(load_rows_memo(source).rows)
        row_ids = {r.get("id") for r in raw_rows if isinstance(r.get("id"), str)}
        fm_gates = _frontmatter_external_gates(source, row_ids)
    except Exception:
        return []
    out: List[dict] = []
    for raw in raw_rows:
        row_id = raw.get("id")
        own = raw.get("external_gate")
        entries = list(own) if isinstance(own, list) else []
        entries.extend(fm_gates.get(row_id, ()))
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("owner_repo"):
                continue
            if not _has_uncleared_execution_gate({"external_gate": [entry]}):
                continue
            item = {"id": row_id, "owner_repo": entry["owner_repo"]}
            if entry.get("closure_key"):
                item["closure_key"] = str(entry["closure_key"])
            out.append(item)
    return out


def _engine_rows(plan_path: str, repo_root: Optional[Path]) -> List[dict]:
    """Engine-plane rows of the plan at `plan_path` (its raw spine rows, its sizing's register
    rows, its evidence sidecar; `repo_root` as cwd), minus rows on the gated owner path; empty
    when the plan is unreadable."""
    from coordinator_core.ops.dispatch_emit.engine_owned_rows import engine_owner_rows
    from coordinator_core.ops.dispatch_emit.spine_read import load_rows_memo

    plan_file = Path(plan_path)
    if not plan_file.is_absolute() and repo_root is not None:
        plan_file = Path(repo_root) / plan_file
    try:
        raw_rows = [r for r in load_rows_memo(plan_file.read_text(encoding="utf-8")).rows if isinstance(r, dict)]
    except Exception:
        return []
    gated = {g["id"] for g in _owner_gate_rows(plan_path, repo_root)}
    raw_rows = [r for r in raw_rows if r.get("id") not in gated]
    evidence = _register_evidence(plan_path, repo_root)
    evidence_rel = _row_evidence_path(plan_path, repo_root)
    evidence_text = ""
    if evidence_rel:
        evidence_file = Path(evidence_rel)
        if not evidence_file.is_absolute() and repo_root is not None:
            evidence_file = Path(repo_root) / evidence_file
        try:
            evidence_text = evidence_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            evidence_text = ""
    cwd = repo_root if repo_root is not None else _repo_of_cwd()
    return engine_owner_rows(raw_rows, list(evidence.rows) if evidence else [], evidence_text, cwd)


def _repo_of_record(record: Path) -> Path:
    """The repo owning a run record: the parent of its `.coordinator-local` (or `.git`) ancestor;
    the checkout enclosing the cwd when the record sits under neither."""
    start = record.resolve()
    for parent in start.parents:
        if parent.name == ".coordinator-local" or (parent / ".git").exists():
            return parent.parent if parent.name == ".coordinator-local" else parent
    return _repo_of_cwd()


def _repo_of_cwd() -> Path:
    """The checkout enclosing the cwd; the cwd itself outside any. A verdict written relative to
    a subdirectory lands where `review-stamp mint` never reads it."""
    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            return candidate
    return cwd


def _json_arg(raw: str) -> dict:
    """A JSON object given inline or as a file path."""
    if not raw.lstrip().startswith("{"):
        raw = Path(raw).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(prog="reverify-delivery")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="write the superseding delivery-verdict from the Workflow result")
    rec.add_argument("--run-record", default=None, help="required except for a judge-only rejudge result")
    rec.add_argument("--result-json", default=None, help="the Workflow's returned JSON, or a path to it")
    rec.add_argument(
        "--judge-json", default=None,
        help="an Agent-dispatched criterion judge's returned JSON, or a path to it; needs --ident",
    )
    rec.add_argument(
        "--ident", default=None,
        help="the `ident` of `emit-dispatch-workflow --rejudge --as-agent-brief` (or the whole brief), "
        "as JSON or a path to it",
    )
    rec.add_argument("--repo-root", default=None)
    rec.add_argument("--session-id", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.judge_json is not None:
            if args.result_json is not None or args.run_record or args.ident is None:
                raise ValueError("--judge-json takes --ident and excludes --result-json and --run-record")
            ident = _json_arg(args.ident)
            ident = ident.get("ident", ident)
            if not ident.get("rejudge"):
                raise ValueError("--ident is not a rejudge ident")
            judged = _json_arg(args.judge_json)
            print(record_criterion_rejudge(
                repo_root=Path(args.repo_root) if args.repo_root else _repo_of_cwd(),
                plan_id=ident.get("plan_id"),
                head_sha=ident["head_sha"],
                criterion={k: judged.get(k) for k in ("status", "observation", "register_rows")},
                session_id=args.session_id,
                run_base_sha=ident.get("run_base_sha"),
            ))
            return 0
        if args.result_json is None:
            raise ValueError("--result-json or --judge-json is required")
        result = _json_arg(args.result_json)
        if "reverify_delivery" not in result:
            result = _unwrap_task_output(result)
        ident = result["reverify_delivery"]
        if ident.get("rejudge"):
            print(record_criterion_rejudge(
                repo_root=Path(args.repo_root) if args.repo_root else _repo_of_cwd(),
                plan_id=ident.get("plan_id"),
                head_sha=ident["head_sha"],
                criterion=result.get("criterion"),
                session_id=args.session_id,
                run_base_sha=ident.get("run_base_sha"),
            ))
            return 0
        if not args.run_record:
            raise ValueError("--run-record is required for a delivery re-verify result")
        repo_root = Path(args.repo_root) if args.repo_root else _repo_of_record(Path(args.run_record))
        run_record = _frontmatter(Path(args.run_record)) or {}
        frozen = _frozen_foreign_claims(repo_root, run_record)
        rel = record_delivery_verdict(
            repo_root=repo_root,
            supersedes=_run_rel(repo_root, Path(args.run_record)),
            plan_id=ident.get("plan_id"),
            head_sha=ident["head_sha"],
            verdict=result.get("verdict"),
            unbacked=result.get("claims_unbacked") or [],
            session_id=args.session_id,
            criterion=result.get("criterion") or None,
            tests=result.get("tests") or None,
            foreign_claims=live_foreign_claims(repo_root, frozen or [], ident["head_sha"]),
            diff_sha=ident.get("diff_sha"),
        )
        settle_tests_sidecar(
            repo_root,
            result.get("tests") or None,
            plan_path=ident.get("plan_path"),
            agent_type=ident.get("tests_agent_type"),
        )
        if result.get("verdict") is not None:
            persist_delivery_sidecar(
                repo_root=repo_root,
                plan_path=ident.get("plan_path"),
                session_id=args.session_id,
                head_sha=ident["head_sha"],
                verdict=result["verdict"],
                unbacked=result.get("claims_unbacked") or [],
            )
    except (OSError, ValueError, KeyError) as exc:
        print(f"reverify-delivery: {exc}", file=sys.stderr)
        return 1
    print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
