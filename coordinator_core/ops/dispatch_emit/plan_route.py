"""coordinator_core.ops.dispatch_emit.plan_route -- the plain `--plan` emit, and nothing else.

`main(argv)` serves exactly `--plan P --out X.workflow.mjs [--force] [--repo-root R]` (`--out`
defaults beside the plan). It writes the same script and receipt bytes, prints the same reply
and exits with the same codes as `cli.main` does for that argv, without importing `cli`, `op`
or any other route's module at module level.

Negative spec:
  - Does NOT accept any other flag: an unknown one is a usage error (2), never a silent ignore.
    A plan too wide for one workflow is handed to `cli.main` (function-local import), which
    owns the sibling split.
  - Does NOT answer the dirty-write-set check with a spawn when the index and HEAD prove the
    write set clean; any answer the in-process read cannot give falls back to
    `guard_against_dirty_write_set`, which keeps the one implementation of the refusal.
  - Does NOT write a second receipt format: the script bytes come from `emit.emit_script`, the
    receipt from `emission_receipt._write_emission_receipt`.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import PathEscapeError
from coordinator_core.git import git_index, git_state
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import admission
from coordinator_core.ops.dispatch_emit.delivery_credit import rows_backed_before_base
from coordinator_core.ops.dispatch_emit.emission_receipt import (
    _load_review_inputs,
    _receipt_session_id,
    _write_emission_receipt,
    emission_receipt_path,
)
from coordinator_core.ops.dispatch_emit.emit import (
    NoWavesError,
    check_agent_types_resolve,
    emit_script,
    resolve_agent_type_host,
)
from coordinator_core.ops.dispatch_emit.pathspec import _declared_paths
from coordinator_core.ops.dispatch_emit.sibling_split import WidthOverCapError
from coordinator_core.ops.dispatch_emit.spine_read import read_spine
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN
from coordinator_core.telemetry import op_latency

EXIT_OK = 0
EXIT_DATA_ERROR = 1
EXIT_USAGE = 2

OUT_SUFFIX = ".workflow.mjs"
BOX_TERMS_KEY = "dispatch.box_terms_path"
ACCEPTED_FLAGS = frozenset({"--plan", "--out", "--force", "--repo-root"})


class ForeignEmissionError(ValueError):
    """`out` already holds a different session's emission and `--force` is absent."""


def _usage(message: str) -> int:
    print(f"emit-dispatch-workflow: ERROR — {message}", file=sys.stderr)
    return EXIT_USAGE


def _parse(argv: "list[str]") -> "tuple[dict, Optional[str]]":
    """`({flag: value}, None)` or `({}, error)`. `--force` maps to True; `--flag=value` is accepted."""
    parsed: dict = {}
    items = iter(argv)
    for item in items:
        flag, eq, inline = item.partition("=")
        if flag not in ACCEPTED_FLAGS:
            return {}, f"unrecognized argument {item!r}; the plan route takes --plan --out [--force] [--repo-root]"
        if flag == "--force":
            if eq:
                return {}, "--force takes no value"
            parsed[flag] = True
            continue
        value = inline if eq else next(items, None)
        if value is None:
            return {}, f"{flag} needs a value"
        parsed[flag] = value
    return parsed, None


def _repo_root_for_plan(plan_path: str) -> Optional[Path]:
    """The nearest ancestor of the plan holding `.git`; None when the plan cannot be resolved."""
    try:
        resolved = Path(plan_path).resolve()
    except OSError:
        return None
    for candidate in resolved.parents:
        if (candidate / ".git").exists():
            return candidate
    return None


def _script_path_under(guarded_path: Path, root: str) -> Optional[str]:
    try:
        return guarded_path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None


def _terminal_commit_script_path(
    guarded_path: Path, repo_root: Optional[Path], plan_path: str, target_root: str
) -> Optional[str]:
    """Repo-root-relative script path for `dispatch.terminal_commit`: plan root first, `target_root` second."""
    plan_root = repo_root or _repo_root_for_plan(plan_path)
    if plan_root is not None:
        rel = _script_path_under(guarded_path, str(plan_root))
        if rel is not None:
            return rel
    return _script_path_under(guarded_path, target_root)


def _refuse_unapproved_body(plan_path: str) -> None:
    from coordinator_core.frontmatter.primitives import (
        APPROVED_BODY_CHANGED,
        APPROVED_BODY_UNVERIFIABLE,
        check_approved_body,
    )

    try:
        text = Path(plan_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    state, message = check_approved_body(text)
    if state == APPROVED_BODY_CHANGED:
        raise ValueError(f"dispatch.emit: {Path(plan_path).name}: {message}")
    if state == APPROVED_BODY_UNVERIFIABLE:
        print(f"dispatch.emit: {Path(plan_path).name}: {message}", file=sys.stderr)


def _refuse_foreign_emission(output_path: Path, data: bytes, session_id: str) -> None:
    """Refuse to overwrite an existing emission with different bytes unless its receipt names `session_id`."""
    if not output_path.is_file():
        return
    existing = output_path.read_bytes()
    if existing == data:
        return
    receipt_path = emission_receipt_path(output_path)
    if session_id and receipt_path.is_file():
        try:
            recorded = json.loads(receipt_path.read_text(encoding="utf-8")).get("session_id")
        except (OSError, ValueError):
            recorded = None
        if recorded == session_id:
            return
    from datetime import datetime

    mtime = datetime.fromtimestamp(output_path.stat().st_mtime).isoformat(timespec="seconds")
    raise ForeignEmissionError(
        f"{output_path} already holds a DIFFERENT emission "
        f"(written {mtime}, {len(existing)} bytes; ours is {len(data)} bytes) "
        "-- refusing to overwrite. Another session emitted this plan against a "
        "different plan state; firing or resuming this path would run a wave "
        "map neither session generated. Coordinate with that session, name a "
        "different output_path, or pass force=true deliberately."
    )


def _norm(path: str) -> str:
    out = path.replace("\\", "/")
    while out.startswith("./"):
        out = out[2:]
    return out.rstrip("/")


def _write_set_provably_clean(plan: Path, repo: Path) -> bool:
    """True only when the index and HEAD prove every declared write path unmodified, unstaged and absent-or-tracked.

    Anything else -- a prefix write, a stat mismatch, a present untracked path, an index this reader
    refuses -- answers False and leaves the verdict to the git-status guard.
    """
    paths: set = set()
    for row in read_spine(plan):
        if row.writes_under:
            return False
        paths.update(_norm(p) for p in _declared_paths(row) if _norm(p))
    wanted = sorted(paths)
    if not wanted:
        return True
    try:
        verdicts = git_index.scoped_status(repo, wanted)
        identity = git_index.parse_index_identity(repo, wanted=wanted)
        head = git_state.head_blobs(repo, wanted)
    except Exception:
        return False
    for path in wanted:
        verdict = verdicts.get(path)
        if verdict == "clean":
            entry, blob = identity.get(path), head.get(path)
            if entry is None or blob is None or entry.mode != blob[0] or entry.sha != blob[1]:
                return False
        elif verdict == "untracked":
            if path in head or os.path.lexists(os.path.join(repo, path)):
                return False
        else:
            return False
    return True


def _installed_plugin_root() -> Optional[str]:
    from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root

    return resolve_plugin_root()


def _box_terms() -> "tuple[list, Optional[str], Optional[str]]":
    """`(terms, terms_path, error)`: the box's standing terms, which bind every brief this route emits."""
    from coordinator_core.machine_resolver import registry_get

    terms_path = registry_get(BOX_TERMS_KEY)
    if not terms_path:
        return [], None, None
    try:
        raw = Path(terms_path).read_text(encoding="utf-8")
    except OSError as exc:
        return [], terms_path, f"--box-terms {terms_path!r} unreadable: {exc}"
    terms = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not terms:
        return [], terms_path, f"--box-terms {terms_path!r} names no term"
    return terms, terms_path, None


def main(argv: "Optional[list[str]]" = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    parsed, error = _parse(argv)
    if error:
        return _usage(error)
    plan_arg = parsed.get("--plan")
    if not plan_arg:
        return _usage("--plan is required")
    out_arg = parsed.get("--out") or str(Path(plan_arg).parent / f"{Path(plan_arg).stem}{OUT_SUFFIX}")
    if not Path(out_arg).name.endswith(OUT_SUFFIX):
        return _usage(
            f"--out {out_arg!r} does not end {OUT_SUFFIX!r} -- refusing to write an emitted script "
            f"over a path that is not named as one. Name --out ending {OUT_SUFFIX!r}."
        )
    force = bool(parsed.get("--force"))
    repo_root = Path(parsed["--repo-root"]).resolve() if parsed.get("--repo-root") else None

    box_terms, box_terms_path, terms_error = _box_terms()
    if terms_error:
        return _usage(terms_error)
    preamble = (
        "Box terms (from the driver; binding):" + "".join(f"\n- {t}" for t in box_terms) if box_terms else None
    )

    from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import is_remote_venue

    cross_repo_approved = is_remote_venue()

    admission_record = admission.await_admission(
        Path.cwd(), hold_allowed=op_latency.execution_route() != op_latency.WARM_SERVER
    )
    if admission_record.get("verdict") not in ("admitted", "disabled"):
        print(
            "emit-dispatch-workflow: admission — "
            f"{admission_record.get('verdict')}; {admission_record.get('reasons')}; "
            f"waited {admission_record.get('waited_s')}s",
            file=sys.stderr,
        )

    try:
        regenerable_dirty = None
        guard_root = repo_root or _repo_root_for_plan(plan_arg)
        if guard_root is not None and not _write_set_provably_clean(Path(plan_arg), guard_root):
            from coordinator_core.ops.dispatch_emit.dirty_write_set import guard_against_dirty_write_set

            regenerable_dirty = guard_against_dirty_write_set(Path(plan_arg), guard_root)

        target_root = str(repo_root) if repo_root is not None else str(Path(out_arg).resolve().parent)
        guarded_path = contained_path(Path(out_arg), [Path(target_root)])
        if guarded_path is None:
            raise PathEscapeError(f"output_path escapes target_root: {out_arg!r} not under {target_root!r}")

        session_id = _receipt_session_id({})
        agent_type_host = resolve_agent_type_host(
            coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
            claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT") or _installed_plugin_root(),
        )

        _refuse_unapproved_body(plan_arg)
        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
        plan_findings: list = []
        try:
            script = emit_script(
                plan_arg,
                repo_root=repo_root or _repo_root_for_plan(plan_arg),
                session_id=session_id,
                review_roster_fragment=review_roster_fragment,
                review_stage_schemas=review_stage_schemas,
                agent_type_host=agent_type_host,
                preamble=preamble,
                box_terms=tuple(box_terms),
                script_path=_terminal_commit_script_path(guarded_path, repo_root, plan_arg, target_root),
                findings_out=plan_findings,
                cross_repo_approved=cross_repo_approved,
                credit_rows=rows_backed_before_base,
                auto_split=True,
            )
        except NoWavesError as empty:
            from coordinator_core.ops.dispatch_emit.spine_read import describe_exclusions

            dropped = describe_exclusions(plan_arg)
            if dropped:
                raise NoWavesError(f"{empty}; read_spine excluded: {dropped}") from empty
            raise

        check_agent_types_resolve(
            script,
            claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT"),
            agent_type_host=agent_type_host,
        )
        findings = [*run_checks(script), *plan_findings]
        error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
        warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

        data = script.encode("utf-8")
        if not force:
            _refuse_foreign_emission(guarded_path, data, session_id)
        guarded_path.parent.mkdir(parents=True, exist_ok=True)
        guarded_path.write_bytes(data)
        script_sha256 = hashlib.sha256(data).hexdigest()

        finding_dicts = [
            {
                "severity": f.severity.value,
                "code": f.code,
                "message": f.message,
                **({"line": f.line} if f.line is not None else {}),
            }
            for f in findings
        ]
        extras: dict = {"findings": finding_dicts}
        if box_terms:
            extras = {"box_terms": list(box_terms), "box_terms_path": box_terms_path, **extras}
        receipt = _write_emission_receipt(
            guarded_path, plan_arg, {"session_id": session_id}, extras=extras, sha256=script_sha256
        )
    except WidthOverCapError:
        from coordinator_core.ops.dispatch_emit import cli

        return cli.main(argv)
    except ValueError as exc:
        print(f"emit-dispatch-workflow: ERROR — {exc}", file=sys.stderr)
        return EXIT_DATA_ERROR

    result: dict = {
        "path": str(guarded_path),
        "receipt": receipt,
        "sha256": script_sha256,
        "ok": error_count == 0,
        "findings": finding_dicts,
        "error_count": error_count,
        "warn_count": warn_count,
        "admission": admission_record,
    }
    anchor_root = repo_root or _repo_root_for_plan(plan_arg)
    if anchor_root is not None:
        result["fire_args"] = {"repoRoot": Path(anchor_root).as_posix()}
    if regenerable_dirty:
        result["regenerable_dirty"] = regenerable_dirty
    print(json.dumps(result, indent=2, sort_keys=True))
    if result.get("fire_args"):
        print(
            f"\n  Workflow({{ scriptPath: {json.dumps(result['path'])}, args: "
            f"{json.dumps(result['fire_args'])} }})",
            file=sys.stderr,
        )
    else:
        print(f"\n  Workflow({{ scriptPath: {json.dumps(result['path'])} }})   # no args", file=sys.stderr)
    return EXIT_OK if result["ok"] else EXIT_DATA_ERROR


if __name__ == "__main__":
    sys.exit(main())
