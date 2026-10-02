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
    _agent_opts_for,
    _schema_literal,
)
from coordinator_core.ops.review_mint.roster import parse_execute_review
from coordinator_core.ops.workflow_scaffold import _js_string_literal

VERDICT_DIR = Path("state") / "delivery-verdicts"
RECORD_KIND = "delivery-verdict"
_PHASE = "Delivery re-verify"


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


def prior_unbacked_claims(record_path: Path) -> tuple[Dict[str, Any], List[dict]]:
    """`(record, claims)` of a run record's frozen delivery FAIL, each claim `{claim, anchor}`.
    Raises `ReverifyRefused` when the record is unreadable or its delivery verdict is not FAIL."""
    record = _frontmatter(record_path)
    if record is None:
        raise ReverifyRefused(f"reverify-delivery: cannot read run record {record_path}")
    delivery = _delivery_block(record)
    if delivery is None or delivery.get("verdict") != "FAIL":
        raise ReverifyRefused(
            f"reverify-delivery: {record_path} carries no delivery FAIL to re-verify"
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
) -> str:
    """A Workflow script holding exactly one delivery-verifier agent call, briefed with the
    prior FAIL's unbacked claims and told to verify them at `head_sha`."""
    review = parse_execute_review(fragment)
    agent = next((a for a in review.review_wave if a.agent_type == _DELIVERY_VERIFIER_AGENT_TYPE), None)
    if agent is None:
        raise ReverifyRefused("reverify-delivery: the review roster declares no delivery-verifier")
    claim_lines = "\n".join(
        f"- {c['claim']} [lacked: {c.get('anchor')}]" for c in claims
    )
    base = run_base_sha or "run_base_sha"
    prompt = (
        f"{_DELIVERY_VERIFIER_ROLE_PREAMBLE}\n\n"
        f"Re-verify delivery at HEAD. A prior verdict FAILed this run with the unbacked claims "
        f"below; follow-up commits may have delivered them. Check EACH claim against the tree at "
        f"HEAD {head_sha} (the diff to judge is `git diff {base}..{head_sha}` plus the files as "
        f"they stand at HEAD). Never judge from the original run's frozen diff: it predates the "
        f"fixes. Return FAIL with claims_unbacked listing every claim still unbacked at HEAD, "
        f"PASS only when all are backed.\n"
        f"plan_path: {plan_path}\n"
        f"run_base_sha: {base}\n"
        f"head_sha: {head_sha}\n"
        f"supersedes: {run_record_rel}\n"
        f"prior_unbacked_claims:\n{claim_lines}"
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
    meta = (
        "export const meta = {\n"
        "  name: 'reverify-delivery',\n"
        "  description: 'Re-run the delivery verifier at HEAD over a prior delivery FAIL.',\n"
        f"  phases: [{_js_string_literal(_PHASE)}],\n"
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
        f"  return {{ reverify_delivery: {json.dumps(ident, sort_keys=True)}, "
        "verdict: _delivery?.verdict ?? null, "
        "claims_unbacked: (_delivery?.claims_unbacked ?? []).map(c => ({ claim: c && c.claim, anchor: c && c.anchor })) };\n"
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
    target = repo_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "x", encoding="utf-8", newline="\n") as fh:
        fh.write("---\n" + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False) + "---\n")
    declare_write(str(target))
    return rel.as_posix()


def latest_delivery_supersession(repo_root: Path, run_record_rel: str) -> Optional[dict]:
    """The `delivery` block of the newest delivery-verdict record superseding `run_record_rel`,
    else `None`. Newest is by `recorded_at`."""
    base = repo_root / VERDICT_DIR
    if not base.is_dir():
        return None
    best: Optional[tuple] = None
    for path in base.glob("*/*.md"):
        fm = _frontmatter(path)
        if not fm or fm.get("kind") != RECORD_KIND or fm.get("supersedes") != run_record_rel:
            continue
        delivery = fm.get("delivery")
        if not isinstance(delivery, dict):
            continue
        key = (str(fm.get("recorded_at") or ""), path.name)
        if best is None or key > best[0]:
            best = (key, delivery)
    return best[1] if best else None


def emit_reverify(*, repo_root: Path, plan_path: str, run_record: str, out_path: str) -> dict:
    from coordinator_core.ops.dispatch_emit.op import _load_review_inputs
    from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted

    record_file = Path(run_record)
    if not record_file.is_absolute():
        record_file = repo_root / record_file
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
    )
    Path(out_path).write_text(script, encoding="utf-8", newline="\n")
    return {"path": out_path, "supersedes": rel, "claims": len(claims)}


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
        ident = result["reverify_delivery"]
        rel = record_delivery_verdict(
            repo_root=repo_root,
            supersedes=_run_rel(repo_root, Path(args.run_record)),
            plan_id=ident.get("plan_id"),
            head_sha=ident["head_sha"],
            verdict=result.get("verdict"),
            unbacked=result.get("claims_unbacked") or [],
            session_id=args.session_id,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"reverify-delivery: {exc}", file=sys.stderr)
        return 1
    print(rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
