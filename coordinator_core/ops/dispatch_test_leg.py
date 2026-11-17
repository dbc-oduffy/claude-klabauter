"""dispatch.test_leg -- the review wave's model-free whole-diff test leg.

Reads the run's recorded test-runner verdict (the `tests-reverify` record `test_verdict.record`
writes) and returns a `whole-diff-review-result`; never runs a test. At most one git spawn, to date
the run base for the freshness floor.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ipc import CallerFacingValidationError, register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops.dispatch_emit.verdict_supersession import latest_tests_record
from coordinator_core.win_portability import no_console_creationflags

AGENT_TYPE = "dispatch.test_leg"
SIDECAR_DIR = Path(".coordinator-local") / "review-trail" / "test-leg"
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_GIT_TIMEOUT_S = 10


def _root(params: dict, repo_root: Optional[Path]) -> Path:
    if repo_root:
        return main_worktree_root(repo_root)
    return Path(params["repo_root"]) if params.get("repo_root") else Path.cwd()


def _plan_id(plan_file: Path) -> str:
    try:
        text = plan_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise CallerFacingValidationError(f"dispatch.test_leg: plan_path {str(plan_file)!r} is unreadable") from None
    if plan_file.suffix == ".json":
        # The review wave passes the run's manifest.json, whose plan_id is a JSON key, not frontmatter.
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        value = doc.get("plan_id") if isinstance(doc, dict) else None
        plan_id = value.strip() if isinstance(value, str) else ""
    else:
        split = split_frontmatter(text)
        plan_id = (read_fm_field_unquoted(split.fm_text, "plan_id") or "") if split else ""
    if not plan_id:
        raise CallerFacingValidationError(f"dispatch.test_leg: plan {str(plan_file)!r} carries no plan_id")
    return plan_id


def _committed_at(root: Path, sha: str) -> datetime:
    """Committer time of `sha`: the single git spawn."""
    try:
        proc = subprocess.run(
            ["git", "show", "-s", "--format=%cI", sha],
            cwd=str(root), capture_output=True, text=True, timeout=_GIT_TIMEOUT_S,
            **no_console_creationflags(),
        )
        return datetime.fromisoformat(proc.stdout.strip()).astimezone(timezone.utc)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raise CallerFacingValidationError(f"dispatch.test_leg: run_base_sha {sha!r} is not a commit here") from None


def _judge(record: Optional[dict]) -> "tuple[str, int, str]":
    """`(verdict, findings, note)` for the newest fresh tests record; `None` reads as unmeasured."""
    if record is None:
        return "APPROVED_WITH_NOTES", 1, "diff tests unmeasured: no recorded test verdict newer than the run base"
    tests, where = record.get("tests") or {}, record.get("record_path")
    status = tests.get("status")
    if status == "pass":
        return "APPROVED", 0, f"recorded tests passed ({tests.get('run')} run) in {where}"
    if status == "fail":
        failed = tests.get("failed")
        count = failed if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0 else 1
        return "REQUIRES_CHANGES", count, f"recorded tests failed ({count}) in {where}"
    return "APPROVED_WITH_NOTES", 1, f"diff tests unmeasured: recorded tests status {status!r} in {where}"


def _write_sidecar(root: Path, plan_id: str, sha: str, verdict: str, findings: int, note: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]", "-", plan_id)
    rel = SIDECAR_DIR / f"{stem}.{sha[:12].lower()}.md"
    fm = {"kind": "review-findings", "plan_id": plan_id, "agent_type": AGENT_TYPE, "verdict": verdict, "findings": findings}
    body = "---\n" + "\n".join(f"{k}: {json.dumps(v)}" for k, v in fm.items()) + f"\n---\n\n{note}\n"
    atomic_write_bytes(root / rel, body.encode("utf-8"), preserve_mode=False)
    return rel.as_posix()


@register_op("dispatch.test_leg")
def _test_leg(params: dict, repo_root: Optional[Path] = None) -> dict:
    """`dispatch.test_leg`: the run's recorded test verdict as a `whole-diff-review-result`; runs no test.

    Params: plan_path, run_base_sha, repo_root (optional). Returns the six-key
    `whole-diff-review-result`: failing recorded tests `REQUIRES_CHANGES` (findings = failures),
    passing and fresh `APPROVED`, no fresh record `APPROVED_WITH_NOTES` with one finding."""
    plan_path, sha = params.get("plan_path"), params.get("run_base_sha")
    if not isinstance(plan_path, str) or not plan_path:
        raise CallerFacingValidationError("dispatch.test_leg: plan_path must be a non-empty string")
    if not isinstance(sha, str) or not _SHA_RE.match(sha):
        raise CallerFacingValidationError("dispatch.test_leg: run_base_sha must be a hex commit sha")
    root = _root(params, repo_root)
    plan_file = Path(plan_path) if Path(plan_path).is_absolute() else root / plan_path
    plan_id = _plan_id(plan_file)
    verdict, findings, note = _judge(latest_tests_record(root, plan_id, _committed_at(root, sha)))
    return {
        "sidecar_path": _write_sidecar(root, plan_id, sha, verdict, findings, note),
        "agent_type": AGENT_TYPE,
        "verdict": verdict,
        "findings": findings,
        "rebuild_recommended": False,
        "em_may_think_differently": [],
    }
