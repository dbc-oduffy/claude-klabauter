"""coordinator_core.ops.post_run_finish -- `post_run.finish`: the deterministic tail of a distill or lessons run.

MUTATING adapter that composes the `coordinator_core.post_run` steps over one run's scope, in
order: linkfix -> h1_normalize -> nav_index -> linkfix -> commit_step. The second linkfix pass
follows nav_index because new or rewritten `_index.md` pages carry links of their own; H1
changes move no paths. No LLM call, no archive, no per-file git spawn.

params:
    pipeline: "lessons" | "distill" (required).
    run_id: the run's identifier, named in the commit message (required).
    touched: repo-relative paths the run wrote (default []).
    evidence: repo-relative evidence files the run wrote, committed untransformed (default []).
    wiki_paths: repo-relative wiki roots (default ["docs/wiki"]).
    session_id: forwarded to commit_v2.
    result: distill only; the run result JSON handed to the p5 audit (default {}).
    porcelain: distill only; the `git status --porcelain --untracked-files=all` lines the caller already
        read (default []); a collapsed untracked-directory line would read as one unexplained path.

Distill runs call `post_run.p5_audit.run(result, porcelain)` before the commit: a non-empty
`unexplained` fails the finish with `exit_code` 1 and no commit, and the audit's `evidence` paths
join the commit's evidence set.

Residual links (unresolvable or ambiguous) never halt the commit: halting would leave a dirty
tree and a hand step. They come back in `residual` as an advisory. A `commit_v2` refusal is
returned as `exit_code` 1 with `commit.error`, never swallowed; the steps are idempotent, so a
rerun converges.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.post_run import h1_normalize, linkfix, nav_index, p5_audit
from coordinator_core.post_run.commit_step import commit_scoped
from coordinator_core.post_run.scope import PIPELINES, PostRunReport, PostRunScope, StepResult

DEFAULT_WIKI_PATHS: tuple[str, ...] = ("docs/wiki",)
DECISIONS_PATH = "docs/decisions"


def _str_list(params: dict, key: str) -> list[str]:
    raw = params.get(key)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)) or not all(isinstance(p, str) and p for p in raw):
        raise ValueError(f"{key} must be a list of non-empty strings")
    return list(raw)


def _rel(root: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _link_step(scope: PostRunScope, pages: list[Path], name: str) -> StepResult:
    result = linkfix.fix_to_fixpoint(pages, scope.wiki_roots, scope.repo_root / DECISIONS_PATH)
    return StepResult(
        name,
        tuple(_rel(scope.repo_root, p) for p in result.changed),
        tuple(f"{_rel(scope.repo_root, Path(r.page))} -> {r.target} ({r.reason})" for r in result.residual),
    )


def run_steps(scope: PostRunScope) -> tuple[PostRunReport, list[str]]:
    """Run the four transform steps; return the report and every path they rewrote or created."""
    root = scope.repo_root
    pages = [root / t for t in scope.touched if t.endswith(".md")]
    steps = [_link_step(scope, pages, "linkfix")]
    steps.append(StepResult(
        "h1_normalize",
        tuple(_rel(root, p) for p in h1_normalize.normalize_paths(
            [p for p in pages if any(r.resolve() in p.resolve().parents for r in scope.wiki_roots)])),
    ))
    nav = nav_index.rebuild(pages, scope.wiki_roots)
    steps.append(StepResult("nav_index", tuple(_rel(root, p) for p in nav.written)))
    steps.append(_link_step(scope, [*pages, *nav.written], "linkfix"))
    report = PostRunReport(scope.pipeline, tuple(steps))
    return report, list(report.changed)


def _audit(pipeline: str, params: dict, repo_root: Path) -> tuple[list[str], list[str]]:
    """Distill p5 audit: (unexplained paths, evidence paths); ([], []) off the distill path."""
    if pipeline != "distill":
        return [], []
    result = params.get("result")
    if result is None:
        result = {}
    if not isinstance(result, dict):
        raise ValueError("result must be an object")
    out = p5_audit.run(result, _str_list(params, "porcelain"), repo_root=repo_root)
    return list(out["unexplained"]), list(out["evidence"])


@register_op("post_run.finish")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "post_run.finish" handler; `repo_root` is the originating worktree. See module docstring."""
    if repo_root is None:
        raise ValueError("post_run.finish requires a resolved repo_root")
    pipeline = params.get("pipeline")
    if pipeline not in PIPELINES:
        raise ValueError(f"pipeline must be one of {PIPELINES}")
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    scope = PostRunScope.for_run(
        pipeline,
        Path(repo_root),
        _str_list(params, "wiki_paths") or DEFAULT_WIKI_PATHS,
        _str_list(params, "touched"),
        _str_list(params, "evidence"),
    )
    report, outputs = run_steps(scope)
    unexplained, audit_evidence = _audit(pipeline, params, scope.repo_root)
    if unexplained:
        return {
            "exit_code": 1,
            "pipeline": pipeline,
            "run_id": run_id.strip(),
            "error": "p5 audit: unexplained tree changes: " + ", ".join(unexplained),
            "unexplained": unexplained,
            "steps": [{"name": s.name, "changed": list(s.changed), "residual": list(s.residual)} for s in report.steps],
            "residual": list(report.residual),
            "commit": None,
        }
    commit = commit_scoped(
        scope.repo_root,
        scope.touched,
        outputs,
        (*scope.evidence, *audit_evidence),
        f"{pipeline}: post-run finish for {run_id.strip()}",
        session_id=params.get("session_id") if isinstance(params.get("session_id"), str) else None,
    )
    return {
        "exit_code": 0 if commit.ok else 1,
        "pipeline": pipeline,
        "run_id": run_id.strip(),
        "steps": [{"name": s.name, "changed": list(s.changed), "residual": list(s.residual)} for s in report.steps],
        "residual": list(report.residual),
        "commit": {
            "ok": commit.ok,
            "committed": commit.committed,
            "sha": commit.sha,
            "process_sha": commit.process_sha,
            "paths": commit.paths,
            "dropped_ignored": commit.dropped_ignored,
            "error": commit.error,
        },
    }
