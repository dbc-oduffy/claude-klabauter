"""coordinator_core.ops.dispatch_emit.reverify_delivery -- re-run only the delivery-verifier
stage against HEAD and supersede a frozen FAIL.

`emit-dispatch-workflow --plan P --reverify-delivery RUN_RECORD` emits a one-stage Workflow
script; `record` turns its returned result into an append-only delivery-verdict record under
`state/delivery-verdicts/<YYYY-MM>/` that names the run record it supersedes.
`latest_delivery_supersession` is what `review_stamp.mint` reads in place of the frozen verdict;
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

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ops.review_mint.compose import _agent_call_literal
from coordinator_core.ops.review_mint.execute_review import (
    _DELIVERY_VERIFIER_AGENT_TYPE,
    _DELIVERY_VERIFIER_HOST_NATIVE_TYPE,
    _DELIVERY_VERIFIER_ROLE_PREAMBLE,
    CRITERION_JUDGE_PHASE_TITLE,
    _agent_opts_for,
    _schema_literal,
    compose_criterion_judge,
    delivery_supersession_clause,
    resolve_operative_criterion_for_plan,
)
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.dispatch_emit.wake_digest import stage_schema_literal
from coordinator_core.ops.workflow_scaffold import _js_string_literal

VERDICT_DIR = Path("state") / "delivery-verdicts"
RECORD_KIND = "delivery-verdict"
_PHASE = "Delivery re-verify"
_CRITERION_STATUSES = ("met", "not_met", "indeterminate")
_TESTS_PHASE = "Tests re-run"
_TESTS_STATUSES = ("pass", "fail", "error")


class ReverifyRefused(ValueError):
    """No script or record was produced; the message names why."""


def _frontmatter(path: Path) -> Optional[Dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return None
    split = split_frontmatter(text)
    if split is None:
        return None
    try:
        data = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


def _delivery_block(record: dict) -> Optional[dict]:
    for holder in (record, record.get("inline_review"), record.get("review")):
        block = holder.get("delivery") if isinstance(holder, dict) else None
        if isinstance(block, dict):
            return block
    return None


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


def prior_unbacked_claims(record_path: Path) -> tuple[Dict[str, Any], List[dict]]:
    """`(record, claims)` of a run record's frozen delivery FAIL, each claim `{claim, anchor}`.
    A delivery PASS whose criterion is `not_met`/`indeterminate`, or whose tests
    are `fail`/`error`, returns `(record, [])`: there is
    nothing to re-check claim by claim, but the verdict and criterion are stale. Raises
    `ReverifyRefused` when the record is unreadable, or when delivery PASSed and nothing else
    is stale (nothing to supersede)."""
    record = _frontmatter(record_path)
    if record is None:
        raise ReverifyRefused(f"reverify-delivery: cannot read run record {record_path}")
    delivery = _delivery_block(record)
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
) -> str:
    """A Workflow script holding one delivery-verifier agent call, briefed with the prior FAIL's
    unbacked claims and told to verify them at `head_sha`, then the roster's criterion judge
    (when declared) against the same HEAD."""
    review = parse_execute_review(fragment)
    agent = next((a for a in review.review_wave if a.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE), None)
    if agent is None:
        raise ReverifyRefused("reverify-delivery: the review roster declares no delivery-verifier")
    claim_lines = "\n".join(
        f"- {c['claim']} [lacked: {c.get('anchor')}]" for c in claims
    )
    base = run_base_sha or "run_base_sha"
    criterion = resolve_operative_criterion_for_plan(plan_path, repo_root)
    if claims:
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
    prompt = (
        f"{_DELIVERY_VERIFIER_ROLE_PREAMBLE}\n\n"
        f"{lead}"
        f"{delivery_supersession_clause(criterion).lstrip()}{chr(10) if criterion and criterion.superseded else ''}"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {base}\n"
        f"head_sha: {head_sha}\n"
        f"supersedes: {run_record_rel}\n"
        f"prior_unbacked_claims:\n{claim_lines or '(none)'}"
    )
    call = _agent_call_literal(
        _DELIVERY_VERIFIER_HOST_NATIVE_TYPE,
        prompt,
        _PHASE,
        schema=True,
        as_arrow=False,
        agent_opts=_agent_opts_for(agent, emitted_agent_type=_DELIVERY_VERIFIER_HOST_NATIVE_TYPE),
        schema_literal=_schema_literal(agent.schema, stage_schemas),
    )
    judge_call = compose_criterion_judge(
        review,
        stage_schemas=stage_schemas,
        plan_path=plan_path,
        run_base_sha=base,
        falsifier=None,
        criterion=criterion,
        prompt_head=f"Judge at HEAD {head_sha}: follow-up commits may have fixed what the prior run's judge saw.",
    )
    phases = [_js_string_literal(_PHASE)]
    tests_lines = ""
    tests_field = ""
    if rerun_tests:
        phases.append(_js_string_literal(_TESTS_PHASE))
        tests_prompt = (
            f"Run the plan's scoped tests at HEAD {head_sha}: {plan_path}, run_base_sha {base}. "
            "Run Python as `python3`, falling back to `python` when `python3` is absent. "
            "Report raw evidence; do not gate. Write your record and return sidecar_path -- required."
        )
        tests_call = _agent_call_literal(
            review.prep.agent_type,
            tests_prompt,
            _TESTS_PHASE,
            schema=True,
            as_arrow=False,
            agent_opts=_agent_opts_for(review.prep),
            schema_literal=stage_schema_literal("test_result"),
        )
        tests_lines = (
            f"  phase({_js_string_literal(_TESTS_PHASE)});\n"
            f"  const _tests = await {tests_call};\n"
        )
        tests_field = (
            ", tests: _tests ? { status: _tests.status ?? null, run: _tests.tests_run ?? null, "
            "failed: _tests.tests_failed ?? null, sidecar: _tests.sidecar_path ?? null } : null"
        )
    judge_lines = ""
    criterion_field = ""
    if judge_call is not None:
        phases.append(_js_string_literal(CRITERION_JUDGE_PHASE_TITLE))
        judge_lines = (
            f"  phase({_js_string_literal(CRITERION_JUDGE_PHASE_TITLE)});\n"
            f"  const _judge = await {judge_call};\n"
        )
        criterion_field = (
            ", criterion: _judge ? { status: _judge.status ?? null, "
            "observation: _judge.observation ?? null } : null"
        )
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
    }
    return (
        "// Runs inside the Workflow runner; a top-level `return` is legal there.\n"
        f"{meta}\n"
        f"  phase({_js_string_literal(_PHASE)});\n"
        f"  const _delivery = await {call};\n"
        f"{tests_lines}"
        f"{judge_lines}"
        f"  return {{ reverify_delivery: {json.dumps(ident, sort_keys=True)}, "
        "verdict: _delivery?.verdict ?? null, "
        "claims_unbacked: (_delivery?.claims_unbacked ?? []).map(c => ({ claim: c && c.claim, anchor: c && c.anchor }))"
        f"{criterion_field}{tests_field} }};\n"
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
) -> str:
    """Exclusive-create the superseding delivery-verdict record; returns its repo-relative path."""
    if verdict not in ("PASS", "FAIL"):
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
    if criterion is not None:
        status = criterion.get("status")
        if status not in _CRITERION_STATUSES:
            raise ReverifyRefused(f"reverify-delivery: criterion status {status!r} is not one of {_CRITERION_STATUSES}")
        fm["criterion"] = {"status": status, "observation": criterion.get("observation"), "sidecar": None}
    if tests is not None:
        status = tests.get("status")
        if status not in _TESTS_STATUSES:
            raise ReverifyRefused(f"reverify-delivery: tests status {status!r} is not one of {_TESTS_STATUSES}")
        fm["tests"] = {
            "status": status,
            "run": tests.get("run"),
            "failed": tests.get("failed"),
            "sidecar": tests.get("sidecar"),
        }
    if foreign_claims is not None:
        fm["foreign_claims"] = list(foreign_claims)
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False) + "---\n")
    declare_write(str(target))
    return rel.as_posix()


def settle_tests_sidecar(repo_root: Path, tests: Optional[dict]) -> bool:
    """Write the re-run verdict into the test-runner sidecar as `test_verdict` (and `run`/`failed`
    when absent), leaving `status` -- the run-report lifecycle -- alone. A sidecar that already
    carries `test_verdict`, is absent, or has no frontmatter is left alone; returns whether it
    was rewritten."""
    if not tests or tests.get("status") not in _TESTS_STATUSES or not tests.get("sidecar"):
        return False
    path = Path(str(tests["sidecar"]))
    if not path.is_absolute():
        path = repo_root / path
    try:
        raw = path.read_bytes().decode("utf-8")
    except OSError:
        return False
    crlf = "\r\n" in raw
    norm = raw.replace("\r\n", "\n")
    split = split_frontmatter(norm)
    fm = _frontmatter(path)
    if split is None or fm is None or "test_verdict" in fm:
        return False
    verdict = "errored" if tests["status"] == "error" else tests["status"]
    add = f"test_verdict: {verdict}\n"
    for key in ("run", "failed"):
        if key not in fm and tests.get(key) is not None:
            add += f"{key}: {tests[key]}\n"
    base = split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n"
    out = norm.replace(split.fm_text, base + add, 1)
    path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
    return True


def _newest_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    base = repo_root / VERDICT_DIR
    if not base.is_dir():
        return None
    best: Optional[tuple] = None
    for path in base.glob("*/*.md"):
        fm = _frontmatter(path)
        if not fm or fm.get("kind") != RECORD_KIND or fm.get("supersedes") != run_record_rel:
            continue
        if not isinstance(fm.get("delivery"), dict):
            continue
        key = (str(fm.get("recorded_at") or ""), path.name)
        if best is None or key > best[0]:
            best = (key, fm)
    return best[1] if best else None


def latest_delivery_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `delivery` block of the newest delivery-verdict record superseding `run_record_rel`,
    else `None`, with the record's `head_sha` (the HEAD it verified) added when it has one.
    Newest is by `recorded_at`."""
    fm = _newest_supersession(repo_root, run_record_rel)
    if not fm:
        return None
    head = fm.get("head_sha")
    return {**fm["delivery"], "head_sha": str(head)} if head else fm["delivery"]


def latest_criterion_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `criterion` block of that same newest record; `None` when there is no record or it
    predates criterion re-judging."""
    fm = _newest_supersession(repo_root, run_record_rel)
    criterion = fm.get("criterion") if fm else None
    return criterion if isinstance(criterion, dict) and criterion.get("status") else None


def latest_tests_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `tests` block of that same newest record; `None` when there is no record or it
    predates test re-running."""
    fm = _newest_supersession(repo_root, run_record_rel)
    tests = fm.get("tests") if fm else None
    return tests if isinstance(tests, dict) and tests.get("status") else None


def latest_foreign_claims_supersession(repo_root: Path, run_record_rel: str) -> Optional[List[str]]:
    """The still-live `foreign_claims` of that same newest record; `None` for an old-shape record
    (the frozen `prep.foreign_claims` then stands)."""
    fm = _newest_supersession(repo_root, run_record_rel)
    claims = fm.get("foreign_claims") if fm else None
    return [str(c) for c in claims] if isinstance(claims, list) else None


_SHA_RE = re.compile(r"\b[0-9a-f]{7,40}\b")


def _claim_held(path: str, cwd: str) -> bool:
    """True when a live session claims `path`, or the ledger cannot answer."""
    from coordinator_core.session import claim_index, liveness

    try:
        claimants = claim_index.lookup([path], cwd=cwd).get(path, [])
        if claim_index.UNANSWERABLE in claimants:
            return True
        return any(liveness.session_live(sid, cwd) for sid in claimants)
    except Exception:  # noqa: BLE001 - an unanswerable ledger must keep the claim blocking
        return True


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
    prep = record.get("prep")
    if not isinstance(prep, dict):
        rel = record.get("prep_sidecar")
        prep = _frontmatter(repo_root / rel) if rel else None
    claims = prep.get("foreign_claims") if isinstance(prep, dict) else None
    return claims if isinstance(claims, list) else None


_BOOKKEEPING_GLOB = ".coordinator-local/subagent-share/*/*.review-wave-bookkeeping.md (plan_id: <plan_id>)"


def _plan_id_of(plan_path: str) -> Optional[str]:
    fm = _frontmatter(Path(plan_path))
    return str(fm["plan_id"]) if fm and fm.get("plan_id") else None


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
        block = _delivery_block(fm)
        if block and (block.get("verdict") == "FAIL" or _unsettled(fm)):
            return path
    return None


def emit_reverify(*, repo_root: Path, plan_path: str, run_record: str, out_path: str) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _load_review_inputs
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

    blank = not run_record.strip()
    record_file = Path(run_record)
    if not blank and not record_file.is_absolute():
        record_file = repo_root / record_file
    if blank or _frontmatter(record_file) is None:
        # Blank, or not a run record (a task .output, say): resolve the plan's own bookkeeping.
        plan_id = _plan_id_of(plan_path)
        found = _bookkeeping_record(repo_root, plan_id)
        if found is None:
            glob = _BOOKKEEPING_GLOB.replace("<plan_id>", plan_id or "<plan_id>")
            what = "no run record given" if blank else f"cannot read run record {record_file}"
            raise ReverifyRefused(
                f"reverify-delivery: {what}, and no {glob} carries this plan's delivery FAIL"
            )
        record_file = found
    record, claims = prior_unbacked_claims(record_file)
    fragment, schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    prep = record.get("prep") if isinstance(record.get("prep"), dict) else {}
    commit_range = record.get("commit_range") if isinstance(record.get("commit_range"), dict) else {}
    plan_id = record.get("plan_id")
    if not plan_id:
        split = split_frontmatter(Path(plan_path).read_text(encoding="utf-8", errors="replace"))
        plan_id = read_fm_field_unquoted(split.fm_text, "plan_id") if split else None
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
    )
    Path(out_path).write_text(script, encoding="utf-8", newline="\n")
    from coordinator_core.ops.dispatch_emit.op import _write_emission_receipt

    receipt = _write_emission_receipt(
        Path(out_path), plan_path, {}, extras={"route": "reverify-delivery", "supersedes": rel}
    )
    return {"path": out_path, "supersedes": rel, "claims": len(claims), "receipt": receipt}


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


def main(argv: "Optional[list[str]]" = None) -> int:
    parser = argparse.ArgumentParser(prog="reverify-delivery")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="write the superseding delivery-verdict from the Workflow result")
    rec.add_argument("--run-record", required=True)
    rec.add_argument("--result-json", required=True, help="the Workflow's returned JSON, or a path to it")
    rec.add_argument("--repo-root", default=None)
    rec.add_argument("--session-id", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    raw = args.result_json
    try:
        if not raw.lstrip().startswith("{"):
            raw = Path(raw).read_text(encoding="utf-8")
        result = json.loads(raw)
        if "reverify_delivery" not in result:
            result = _unwrap_task_output(result)
        ident = result["reverify_delivery"]
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
            foreign_claims=(
                None if frozen is None else live_foreign_claims(repo_root, frozen, ident["head_sha"])
            ),
        )
        settle_tests_sidecar(repo_root, result.get("tests") or None)
    except (OSError, ValueError, KeyError) as exc:
        print(f"reverify-delivery: {exc}", file=sys.stderr)
        return 1
    print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
