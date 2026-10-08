"""plan_chain phase-1 checks, in-process: completeness baseline, spine check, gates, falsifier, targets.

``run`` returns the first ``Halt`` or ``None``; nothing after a halt runs. No subprocess is spawned.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from coordinator_core.ops.plan_chain.contract import ChainManifest, Halt, halt

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_FALSIFIER_AT = "prime_exit_criterion.falsifier"


def _bin_lib_on_path() -> None:
    lib_dir = str(_ENGINE_ROOT / "coordinator" / "bin" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def _plan_completeness_lib():
    _bin_lib_on_path()
    import plan_completeness

    return plan_completeness


def _write_baseline(repo_root: Path, plan_path: Path) -> str | None:
    """Build and write the completeness sidecar as ``cmd_generate`` does; a reason string on failure."""
    pc = _plan_completeness_lib()
    result = pc.build_ledger(str(repo_root), str(plan_path))
    if result.status != "located":
        return f"plan-completeness {result.status}: {plan_path.name}"
    document = result.document
    document["generated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    from coordinator_core.session.machinery_paths import plan_sidecars_dir

    stem = Path(document["plan"]).name
    if stem.endswith(".md"):
        stem = stem[: -len(".md")]
    out_dir = Path(plan_sidecars_dir(str(repo_root)))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{stem}.completeness.md").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    return None


def _check_plan(plan_path: Path) -> dict[str, Any]:
    """``check_plan(path, for_execution=True)`` from ``coordinator/bin/plan-spine-check.py``."""
    _bin_lib_on_path()
    path = _ENGINE_ROOT / "coordinator" / "bin" / "plan-spine-check.py"
    spec = importlib.util.spec_from_file_location("plan_spine_check_for_chain", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.check_plan(plan_path, for_execution=True)


def _spine_exclusions(plan_path: Path) -> list[dict[str, Any]]:
    from coordinator_core.ops.dispatch_emit.spine_read import read_spine

    exclusions: list[dict[str, Any]] = []
    read_spine(plan_path, exclusions=exclusions)
    return exclusions


def _falsifier_defect(plan_path: Path, repo_root: Path) -> str | None:
    from coordinator_core.execute_plan_assemble.falsifier_shape import goal_falsifier_defect
    from coordinator_core.frontmatter.primitives import split_frontmatter
    import yaml

    split = split_frontmatter(plan_path.read_text(encoding="utf-8"))
    if split is None:
        return None
    fm = yaml.safe_load(split.fm_text)
    if not isinstance(fm, dict):
        return None
    return goal_falsifier_defect(fm, repo_root)


def _plan_targets(plan_path: Path, repo_root: Path) -> list[str]:
    from coordinator_core.ops.review_findings_ledger import plan_targets

    return plan_targets(plan_path, repo_root)


_SESSION_ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?P<repo>[a-z0-9][a-z0-9-]*)-[0-9a-f]{2})$"
)
_GATE_ADDRESS_KEYS = ("owner_repo", "holder", "target")


def _gate_entries(plan_path: Path) -> list[dict[str, Any]]:
    """Uncleared ``external_gate`` mappings, row-level and frontmatter; unreadable plan -> []."""
    from coordinator_core.frontmatter.primitives import split_frontmatter
    from coordinator_core.ops.dispatch_emit.spine_read import load_frontmatter_doc, load_rows_memo

    try:
        source = plan_path.read_text(encoding="utf-8")
        lists = [row.get("external_gate") for row in load_rows_memo(source).rows]
        split = split_frontmatter(source)
        doc = load_frontmatter_doc(split.fm_text) if split is not None else None
        if isinstance(doc, dict):
            lists.append(doc.get("external_gate"))
    except (OSError, ValueError, yaml.YAMLError):
        return []
    return [
        e
        for entries in lists
        if isinstance(entries, list)
        for e in entries
        if isinstance(e, dict) and e.get("cleared") is not True
    ]


def _session_is_dead(sid: str, root: Path) -> bool:
    """True only on a positive not-live; an unreadable roster is not a verdict."""
    from coordinator_core.session import liveness

    try:
        roster = liveness.live_session_ids(str(root))
        if not roster:
            return False
        return sid not in roster and not liveness.session_live(sid, str(root))
    except Exception:
        return False


def _dead_session_gate(plan_path: Path, root: Path) -> str | None:
    """A refusal for a gate addressed to a dead session instead of a repo."""
    for entry in _gate_entries(plan_path):
        for key in _GATE_ADDRESS_KEYS:
            value = entry.get(key)
            match = _SESSION_ID_RE.match(value) if isinstance(value, str) else None
            if match is None or not _session_is_dead(value, root):
                continue
            repo = entry.get("repo") or entry.get("owner_repo") or match.group("repo")
            if repo == value:
                repo = match.group("repo")
            target = f"repo {repo}" if repo else "the owning repo"
            return (
                f"external_gate {key} {value} is addressed to a dead session; "
                f"re-address it to {target}"
            )
    return None


def _spine_failed(reason: str) -> Halt:
    return halt("spine-check-failed", reason)


def _spine_verdict_failure(report: dict[str, Any]) -> str | None:
    """A failure reason, ignoring the falsifier finding: ``falsifier-owed`` reports that one."""
    verdict = report.get("verdict")
    if verdict in ("VALID", "LEGACY", "NO-SPINE"):
        return None
    rows = report.get("rows") or []
    other = [r for r in rows if r.get("at") != _FALSIFIER_AT]
    if verdict == "INVALID" and rows and not other:
        return None
    detail = report.get("detail") or "; ".join(str(r.get("error", r)) for r in other)
    return f"plan-spine-check {verdict}: {detail}"


def run(manifest: ChainManifest, plan_path: str | Path, *, repo_root: str | Path) -> Halt | None:
    plan = Path(plan_path)
    root = Path(repo_root)
    if not plan.is_absolute():
        plan = root / plan

    reason = _write_baseline(root, plan)
    if reason is not None:
        return _spine_failed(reason)

    failure = _spine_verdict_failure(_check_plan(plan))
    if failure is not None:
        return _spine_failed(failure)

    try:
        exclusions = _spine_exclusions(plan)
    except ValueError as exc:
        return _spine_failed(f"spine unreadable: {exc}")
    dead = _dead_session_gate(plan, root)
    if dead is not None:
        return halt("external-gate-uncleared", dead)
    gated = [e["id"] for e in exclusions if e.get("reason") == "external_gate"]
    if gated:
        return halt(
            "external-gate-uncleared",
            f"row(s) {', '.join(str(g) for g in gated)} carry an uncleared external_gate blocking execution",
        )

    defect = _falsifier_defect(plan, root)
    if defect is not None:
        return halt("falsifier-owed", defect)

    try:
        _plan_targets(plan, root)
    except ValueError as exc:
        return _spine_failed(f"review-findings-ledger targets --from-plan: {exc}")
    return None
