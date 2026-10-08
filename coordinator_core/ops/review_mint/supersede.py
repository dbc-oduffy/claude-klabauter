"""coordinator_core.ops.review_mint.supersede -- the stranded-run superseding
review record writer (D8 of the completion-receipts plan).

``record_superseding_review`` builds the review bookkeeping record with
``bookkeep_wave`` and copies it, append-only, to the durable
``state/superseding-reviews/<YYYY-MM>/<record_stem>.md`` with the keys
``kind``, ``plan_id``, ``commit_range`` and ``supersedes`` added. The record
names its commit range explicitly, so a peer's commit inside the range never
trips a Session-Id attribution refusal; ``review_stamp.mint`` reads it.

Refusals: ``head`` not an ancestor of HEAD, ``base`` not an ancestor of
``head``, ``base == head``, or an existing record at the target path. At most
two argv-only git spawns of its own; ``bookkeep_wave``'s spawns are additional.

DR-208 five-question affirmation (MUTATING):
  1. Writes, deletes, or reorders any state file, queue, or git object?  YES.
     Creates the superseding record; bookkeep_wave also writes its record
     and stamps plan_id onto each wave sidecar.
  2. Writes into rag's relational store?                                 No.
  3. Opens any file for write (including sentinel creation)?             YES.
     Exclusive create of the superseding record.
  4. Mutates shared mutable state outside its own module?                YES.
     review_stamp.mint reads the record it writes.
  5. Persistent state changes observable across process boundaries?      YES.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.completion_receipts.verdict import _count
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ops.review_mint.share_stages import ShareStageMissing, assemble_from_share
from coordinator_core.ops.review_mint.wave_bookkeeping import (
    bookkeep_wave,
    review_wave_bookkeeping_stem,
)

# Writes per-session subagent-share sidecars, gitignored and unstamped.
GENERATES = []


_GIT_TIMEOUT_SECS = 30


class SupersedeRefused(ValueError):
    """The superseding record was refused; no superseding record was written."""


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    proc = run_git(
        ["merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo_root),
        timeout=_GIT_TIMEOUT_SECS,
    )
    if proc.returncode not in (0, 1):
        raise SupersedeRefused(
            f"git merge-base --is-ancestor {ancestor} {descendant} failed: "
            f"{proc.stderr.strip()}"
        )
    return proc.returncode == 0


def _resolve_plan_id(repo_root: Path, plan: str) -> str:
    """``plan`` as a ``pln-`` id: a ``pln-`` string passes through; anything
    else is a plan file path (repo-root-relative or absolute) whose
    frontmatter ``plan_id`` is returned. ``review_stamp.mint`` matches the
    record's ``plan_id`` against the plan's own, so a path stored verbatim
    leaves the run unstampable."""
    if plan.startswith("pln-"):
        return plan
    plan_path = Path(plan)
    if not plan_path.is_absolute():
        plan_path = repo_root / plan_path
    try:
        text = plan_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError as exc:
        raise SupersedeRefused(f"plan {plan!r} is neither a pln- id nor a readable plan file: {exc}")
    split = split_frontmatter(text)
    plan_id = read_fm_field_unquoted(split.fm_text, "plan_id") if split is not None else None
    if not plan_id or not str(plan_id).startswith("pln-"):
        raise SupersedeRefused(f"plan file {plan!r} carries no pln- plan_id in its frontmatter")
    return str(plan_id)


def _write_range_prep(repo_root: Path, session_id: str, plan: str, base: str, head: str) -> Path:
    """Write the prep sidecar for an ad-hoc review of ``base..head``: the
    fields ``share_stages`` reads off the emitted workflow's prep stage, with
    ``product_files`` counted over the range. One git spawn."""
    proc = run_git(["diff", "--name-only", f"{base}..{head}"], cwd=str(repo_root), timeout=_GIT_TIMEOUT_SECS)
    files = [line for line in proc.stdout.splitlines() if line.strip()]
    share = repo_root / ".coordinator-local" / "subagent-share" / session_id
    share.mkdir(parents=True, exist_ok=True)
    target = share / f"{review_wave_bookkeeping_stem(plan, session_id)}.range-prep.md"
    fm = {
        "agent_type": "engine:range-prep",
        "plan_id": plan,
        "run_base_sha": base,
        "head_sha": head,
        "product_files": len(files),
        "whole_diff_sidecars": {},
    }
    atomic_write_bytes(
        target,
        ("---\n" + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False) + "---\n").encode("utf-8"),
    )
    declare_write(str(target))
    return target


def _refuse_empty_prep(repo_root: Path, prep_sidecar: Optional[str]) -> None:
    """Refuse a readable prep sidecar that reviews zero files; ``review_stamp.mint`` would refuse it later."""
    if not prep_sidecar:
        return
    path = Path(prep_sidecar)
    if not path.is_absolute():
        path = repo_root / path
    if not path.is_file():
        return
    split = split_frontmatter(path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n"))
    try:
        fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
    except yaml.YAMLError:
        fm = {}
    fm = fm if isinstance(fm, dict) else {}
    if max(_count(fm.get("slice_files")), _count(fm.get("product_files"))) == 0:
        raise SupersedeRefused(
            f"prep sidecar {prep_sidecar} records no product_files: the review covers zero files; "
            "restamp the prep with product_files (count or list) for the reviewed range"
        )


def record_superseding_review(
    *,
    repo_root: Path,
    plan: str,
    commit_range: Dict[str, str],
    wave_sidecar_paths: List[Path],
    prep_sidecar: Optional[str],
    stage_returns: Optional[Dict[str, Any]],
    session_id: str,
    supersedes: Optional[str] = None,
    from_share: bool = False,
    judge_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Write the superseding record; returns ``{record_path, record}`` with
    ``record_path`` repo-root-relative. ``plan`` is a ``pln-`` id or a plan
    file path. ``from_share`` assembles ``prep_sidecar``, the wave sidecars
    and ``stage_returns`` from the session's plan-scoped sidecars
    (``share_stages``), ignoring the three hand-supplied arguments, and adds
    ``used`` (stage -> sidecar paths) to the result. Raises
    ``SupersedeRefused``."""
    plan_stem = None if plan.startswith("pln-") else Path(plan).stem
    plan = _resolve_plan_id(repo_root, plan)
    base = str(commit_range.get("base") or "")
    head = str(commit_range.get("head") or "")
    if not base or not head:
        raise SupersedeRefused("commit_range needs both base and head")
    if base == head:
        raise SupersedeRefused("commit_range base equals head: empty range")
    if not _is_ancestor(repo_root, head, "HEAD"):
        raise SupersedeRefused(f"head {head} is not an ancestor of HEAD")
    if not _is_ancestor(repo_root, base, head):
        raise SupersedeRefused(f"base {base} is not an ancestor of head {head}")

    used = None
    if from_share:
        def assemble() -> Dict[str, Any]:
            return assemble_from_share(
                repo_root=repo_root, session_id=session_id, plan_id=plan, plan_stem=plan_stem, head=head,
                judge_result=judge_result,
            )

        try:
            try:
                assembled = assemble()
            except ShareStageMissing as exc:
                # A post-run review has no emitted prep stage; the range itself is the prep.
                if exc.stage != "prep":
                    raise
                _write_range_prep(repo_root, session_id, plan, base, head)
                assembled = assemble()
        except (ShareStageMissing, ValueError) as exc:
            raise SupersedeRefused(str(exc))
        prep_sidecar = assembled["prep_sidecar"]
        wave_sidecar_paths = assembled["wave_sidecar_paths"]
        stage_returns = assembled["stage_returns"]
        used = assembled["used"]

    _refuse_empty_prep(repo_root, prep_sidecar)

    record_stem = review_wave_bookkeeping_stem(plan, session_id) + ".superseding"
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    rel = Path("state") / "superseding-reviews" / month / f"{record_stem}.md"
    target = repo_root / rel
    if target.exists():
        raise SupersedeRefused(f"superseding record already exists: {rel.as_posix()}")

    record = bookkeep_wave(
        [Path(p) for p in wave_sidecar_paths],
        repo_root=repo_root,
        session_id=session_id,
        plan_id=plan,
        prep_sidecar=prep_sidecar,
        record_stem=record_stem,
        stage_returns=stage_returns,
        plan_stem=plan_stem,
    )
    src = Path(record["sidecar_path"])
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
    split = split_frontmatter(text)
    fm = (yaml.safe_load(split.fm_text) or {}) if split is not None else {}
    fm = {
        "kind": "superseding-review",
        "plan_id": plan,
        "commit_range": {"base": base, "head": head},
        "supersedes": supersedes,
        **{k: v for k, v in fm.items() if k not in ("kind", "plan_id", "commit_range", "supersedes")},
    }
    body = text.split("\n---\n", 1)[1] if "\n---\n" in text else ""
    out = (
        "---\n"
        + yaml.safe_dump(fm, default_flow_style=False, sort_keys=False).strip()
        + "\n---\n"
        + body
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
    except FileExistsError:
        raise SupersedeRefused(f"superseding record already exists: {rel.as_posix()}")
    declare_write(str(target))
    result = {"record_path": rel.as_posix(), "record": fm}
    if used is not None:
        result["used"] = used
    return result


@register_op("review_mint.record_superseding_review")
def _record_superseding_review_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: plan, commit_range {base, head}, wave_sidecar_paths,
    prep_sidecar, stage_returns, session_id, optional supersedes, optional
    from_share (assemble the three from the share dir; refused naming the
    first missing stage)."""
    root = main_worktree_root(repo_root) if repo_root else Path(params.get("repo_root") or ".")
    try:
        result = record_superseding_review(
            repo_root=root,
            plan=params["plan"],
            commit_range=params["commit_range"],
            wave_sidecar_paths=[Path(p) for p in params.get("wave_sidecar_paths") or []],
            prep_sidecar=params.get("prep_sidecar"),
            stage_returns=params.get("stage_returns"),
            session_id=params["session_id"],
            supersedes=params.get("supersedes"),
            from_share=bool(params.get("from_share")),
            judge_result=params.get("judge_result"),
        )
    except SupersedeRefused as exc:
        return {"status": "refused", "reason": str(exc)}
    return {"status": "recorded", **result}


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(prog="record-superseding-review")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--wave-sidecar", action="append", default=[])
    parser.add_argument("--prep-sidecar", default=None)
    parser.add_argument("--stage-returns-json", default=None)
    parser.add_argument(
        "--from-share", action="store_true",
        help="assemble prep, reviewer, delivery, tests and judge returns from this session's "
        "plan-scoped sidecars; excludes --wave-sidecar, --prep-sidecar, --stage-returns-json",
    )
    parser.add_argument(
        "--judge-result", default=None,
        help="with --from-share: the exit-criterion-judge's returned terminal-judge-result JSON "
        "(inline or a file path); the judge writes no sidecar",
    )
    parser.add_argument("--supersedes", default=None)
    parser.add_argument("--repo-root", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.from_share and (args.wave_sidecar or args.prep_sidecar or args.stage_returns_json):
        print(
            "record-superseding-review: --from-share excludes --wave-sidecar, --prep-sidecar "
            "and --stage-returns-json",
            file=sys.stderr,
        )
        return 2
    try:
        stage_returns = json.loads(args.stage_returns_json) if args.stage_returns_json else None
    except json.JSONDecodeError as exc:
        print(f"record-superseding-review: --stage-returns-json is not JSON: {exc}", file=sys.stderr)
        return 2
    judge_result = None
    if args.judge_result:
        raw = args.judge_result
        try:
            if not raw.lstrip().startswith("{"):
                raw = Path(raw).read_text(encoding="utf-8")
            judge_result = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"record-superseding-review: --judge-result unreadable: {exc}", file=sys.stderr)
            return 2

    repo_root = Path(args.repo_root) if args.repo_root else Path.cwd()
    try:
        result = record_superseding_review(
            repo_root=repo_root,
            plan=args.plan,
            commit_range={"base": args.base, "head": args.head},
            wave_sidecar_paths=[Path(p) for p in args.wave_sidecar],
            prep_sidecar=args.prep_sidecar,
            stage_returns=stage_returns,
            session_id=args.session_id,
            supersedes=args.supersedes,
            from_share=args.from_share,
            judge_result=judge_result,
        )
    except SupersedeRefused as exc:
        print(f"record-superseding-review: refused: {exc}", file=sys.stderr)
        return 1
    for stage, paths in (result.get("used") or {}).items():
        print(f"record-superseding-review: used {stage}: {', '.join(paths)}", file=sys.stderr)
    print(result["record_path"])
    return 0

