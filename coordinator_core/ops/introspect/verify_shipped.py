"""
coordinator_core.ops.introspect.verify_shipped — unified shipped-state verdict.

Purpose: answer "has this ref/SHA (and optionally, the plan or handoff it belongs to)
actually shipped?" by combining three independent signals that today live in three
separate, unconnected parts of this repo:

  1. Git ancestry — is the resolved SHA an ancestor of ``origin/main``
     (``coordinator_core.ops.emit.envelope.sha_on_origin_main`` et al.).
  2. Live frontmatter — does the named plan/handoff's OWN frontmatter say "shipped",
     re-derived directly from disk (NOT the emission pipeline's already-joined
     ``HandoffSummary``/``PlanSummary`` dicts — see Anti-scope in the spec backlink).
  3. ``state/cockpit-emission.json`` — an advisory, potentially-stale cached snapshot,
     cross-checked but never authoritative.

The interesting part is not the git-subprocess reuse (already shipped, reused not
rewritten) — it's keeping DISAGREEMENT between signals 1 and 2 visible in the returned
``verdict`` instead of silently collapsing it to a boolean, which is exactly the defect
class (WFVALIDATE's 3-pass SHA reconciliation, QSUB03's `git log --all` misdiagnosis of an
already-shipped commit) this module exists to prevent.

Negative-spec:
  - Does NOT let ``emission_snapshot`` participate in the verdict computation — it is a
    point-in-time cache and can be stale relative to live git/frontmatter by design.
  - Does NOT import `coordinator_core.ops.emit.deliverable_status`'s `_handoff_phase`/
    `_plan_phase` — those operate on the emission pipeline's cross-joined summary dicts,
    a different input shape than a raw single-file frontmatter read. The shipped-specific
    precedence rule is re-derived locally (`_frontmatter_shipped_status` below).
  - Does NOT unify with `coordinator_core.reconcile.commit_reality` — that module answers
    a different question (did an OPEN HANDOFF's scope get shipped by SOME commit) for a
    different caller.
  - Is NOT a `@register_op` — pure library function, no IPC caller today.

Spec backlink: state/handoffs/2026-07-25_000823_shipped-state-verifier.md
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.emit.envelope import (
    check_origin_main_reachable,
    fetch_origin_main,
    resolve_ref,
    sha_on_origin_main,
)

Verdict = Literal["shipped", "not_shipped", "disagreement", "indeterminate"]


@dataclass(frozen=True)
class ShipVerdict:
    """The combined answer to "has ``ref_or_sha`` shipped?".

    Fields mirror the spec's `ShipVerdict` shape verbatim (state/handoffs/
    2026-07-25_000823_shipped-state-verifier.md § Specification). Frozen —
    callers get an immutable snapshot of the three-signal read, never a
    mutable object a later step could accidentally poison.
    """

    ref_or_sha: str
    resolved_sha: Optional[str]
    git_on_main: Optional[bool]
    frontmatter_status: Optional[str]
    emission_snapshot: Optional[dict]
    verdict: Verdict
    evidence: list[str] = field(default_factory=list)


def _resolve_state_root() -> Optional[Path]:
    """Resolve ``<claude-klabauter-repo-root>/state`` for the leg-3 emission-snapshot read.

    Delegates to the same canonical claude-klabauter-root resolver
    ``coordinator_core.ops.emit.envelope._resolve_central_state_root`` wraps
    (``coordinator_core.engine_root.coordinator_engine_root``) rather than hardcoding
    ``state/cockpit-emission.json`` relative to cwd. Returns None (never raises) on any
    resolution failure — leg 3 is advisory-only; an unresolvable root degrades to
    ``emission_snapshot=None``, the same as a missing file.

    Tests monkeypatch this function directly to point at a tmp dir holding a fixture
    ``cockpit-emission.json``, rather than depending on the real, mutable file this
    resolves to in a live checkout.
    """
    try:
        from coordinator_core.engine_root import coordinator_engine_root

        return Path(coordinator_engine_root()) / "state"
    except Exception:
        return None


def _frontmatter_shipped_status(fm: dict, record_kind: str) -> str:
    """Binary shipped/not_shipped read of one plan-or-handoff's LIVE frontmatter dict.

    Mirrors (does NOT import — see module docstring Anti-scope) the shipped-specific
    precedence `coordinator_core.ops.emit.deliverable_status._handoff_phase`/
    `_plan_phase` apply to already-joined summary dicts: `shipped_sha` set > the
    kind-specific "shipped" marker (`deployment_state=="shipped"` for a handoff,
    `status=="implemented"` for a plan) > otherwise not_shipped. Collapsed to a binary
    answer since this module's `ShipVerdict.frontmatter_status` has no in-between states
    (unlike `deliverable_status`'s richer in-progress/planned/abandoned phase enum).
    """
    if fm.get("shipped_sha") is not None:
        return "shipped"
    if record_kind == "handoff":
        if (fm.get("deployment_state") or "") == "shipped":
            return "shipped"
    else:
        if (fm.get("status") or "") == "implemented":
            return "shipped"
    return "not_shipped"


def _record_kind_for_path(path: Path) -> str:
    """Classify a plan/handoff path by its containing directory (`state/handoffs/` vs
    everything else, which is treated as a plan) so `_frontmatter_shipped_status` reads
    the right kind-specific field (`deployment_state` vs `status`)."""
    if "handoffs" in path.parts:
        return "handoff"
    return "plan"


def _read_frontmatter_status(plan_path: str, repo_root: Path) -> tuple[Optional[str], list[str]]:
    """Leg 2: read `plan_path` off live disk and derive its shipped/not_shipped status.

    Returns (status_or_None, evidence_lines). status is None when the path doesn't
    exist or doesn't parse as valid frontmatter — the caller treats that as
    `indeterminate`, per spec.
    """
    candidate = Path(plan_path)
    resolved = candidate if candidate.is_absolute() else (repo_root / candidate)
    if not resolved.is_file():
        return None, [f"frontmatter: {plan_path} does not exist on disk"]
    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        return None, [f"frontmatter: {plan_path} unreadable ({exc})"]
    parsed = parse_frontmatter(content)
    fm = parsed.get("frontmatter")
    if not fm:
        return None, [f"frontmatter: {plan_path} has no parseable frontmatter"]
    kind = _record_kind_for_path(resolved)
    status = _frontmatter_shipped_status(fm, kind)
    return status, [f"frontmatter: {plan_path} ({kind}) reads {status}"]


def _read_emission_snapshot(plan_path: str) -> tuple[Optional[dict], list[str]]:
    """Leg 3: advisory cross-check against `state/cockpit-emission.json`'s cached
    `plans[]`/`handoffs[]` arrays, keyed on `provenance.path`.

    Never raises — an absent/unreadable/malformed emission file is not an error (the
    file is regenerated periodically, not guaranteed present in every checkout); it
    degrades to `(None, [])`.
    """
    state_root = _resolve_state_root()
    if state_root is None:
        return None, []
    emission_path = state_root / "cockpit-emission.json"
    if not emission_path.is_file():
        return None, []
    try:
        payload = json.loads(emission_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, []
    for bucket_name in ("plans", "handoffs"):
        for record in payload.get(bucket_name, []) or []:
            provenance = record.get("provenance") or {}
            if provenance.get("path") == plan_path:
                snapshot = {
                    "status": record.get("status"),
                    "shipped_sha": record.get("shipped_sha"),
                    "deliverable_status": record.get("deliverable_status"),
                    "emitted_at": record.get("emitted_at"),
                }
                return snapshot, [
                    f"cockpit-emission.json ({bucket_name}, emitted {record.get('emitted_at')}): "
                    f"status={snapshot['status']!r} shipped_sha={snapshot['shipped_sha']!r}"
                ]
    return None, []


def verify_shipped(
    ref_or_sha: str,
    plan_path: Optional[str] = None,
    *,
    repo_root: Optional[Path] = None,
) -> ShipVerdict:
    """Combine git ancestry + live frontmatter + cockpit-emission cross-check into one
    shipped-state verdict for `ref_or_sha` (optionally scoped to `plan_path`'s claim).

    See module docstring + spec backlink for the full three-leg design and the
    verdict-precedence table (indeterminate / shipped / not_shipped / disagreement).
    `plan_path`, when given, is resolved relative to `repo_root` if not already
    absolute, and is also used verbatim as the `provenance.path` lookup key for leg 3
    (that file's keys are repo-root-relative).
    """
    root = repo_root if repo_root is not None else Path.cwd()
    evidence: list[str] = []

    resolved_sha = resolve_ref(root, ref_or_sha)
    if resolved_sha is None:
        evidence.append(f"git: '{ref_or_sha}' does not resolve to a commit")
        return ShipVerdict(
            ref_or_sha=ref_or_sha,
            resolved_sha=None,
            git_on_main=None,
            frontmatter_status=None,
            emission_snapshot=None,
            verdict="indeterminate",
            evidence=evidence,
        )

    if not check_origin_main_reachable(root):
        fetch_origin_main(root)
    git_on_main = sha_on_origin_main(root, resolved_sha)
    if git_on_main is None:
        evidence.append(
            f"git: origin/main unreachable or {resolved_sha[:8]} not resolvable against it"
        )
    else:
        evidence.append(f"git: {resolved_sha[:8]} is {'' if git_on_main else 'NOT '}on origin/main")

    frontmatter_status: Optional[str] = None
    emission_snapshot: Optional[dict] = None
    if plan_path is not None:
        frontmatter_status, fm_evidence = _read_frontmatter_status(plan_path, root)
        evidence.extend(fm_evidence)
        if frontmatter_status is None:
            evidence.append(f"indeterminate: {plan_path} could not be read/parsed")
            return ShipVerdict(
                ref_or_sha=ref_or_sha,
                resolved_sha=resolved_sha,
                git_on_main=git_on_main,
                frontmatter_status=None,
                emission_snapshot=None,
                verdict="indeterminate",
                evidence=evidence,
            )

        emission_snapshot, snap_evidence = _read_emission_snapshot(plan_path)
        evidence.extend(snap_evidence)
        if emission_snapshot is not None:
            snap_shipped = (
                emission_snapshot.get("shipped_sha") is not None
                or emission_snapshot.get("status") == "implemented"
                or emission_snapshot.get("status") == "shipped"
            )
            live_shipped = frontmatter_status == "shipped"
            if snap_shipped != live_shipped:
                evidence.append(
                    f"cockpit-emission.json (emitted {emission_snapshot.get('emitted_at')}) "
                    "disagrees with live frontmatter"
                )

    verdict = _compute_verdict(git_on_main, frontmatter_status)
    return ShipVerdict(
        ref_or_sha=ref_or_sha,
        resolved_sha=resolved_sha,
        git_on_main=git_on_main,
        frontmatter_status=frontmatter_status,
        emission_snapshot=emission_snapshot,
        verdict=verdict,
        evidence=evidence,
    )


def _compute_verdict(git_on_main: Optional[bool], frontmatter_status: Optional[str]) -> Verdict:
    """Pure verdict-precedence table — spec § Specification "Verdict logic".

    `emission_snapshot` deliberately takes no part here (Anti-scope: it is advisory-only
    and can be stale by design).
    """
    if git_on_main is None:
        return "indeterminate"
    if frontmatter_status is None:
        # No plan_path given (or already handled as indeterminate above) — git alone decides.
        return "shipped" if git_on_main else "not_shipped"
    if git_on_main and frontmatter_status == "shipped":
        return "shipped"
    if not git_on_main and frontmatter_status != "shipped":
        return "not_shipped"
    return "disagreement"


def main(argv: list[str]) -> int:
    """Optional CLI entry: ``verify-shipped <ref_or_sha> [--plan <path>]`` -> JSON verdict.

    Mirrors `coordinator_core.ops.emit.envelope.main`'s CLI-trampoline pattern (parse
    args, print, return an exit code) for the same launcher-shim generation path
    (`coordinator/bin/gen-launcher-shim.py`), should a picking-up caller need this
    directly invocable outside a Python import.
    """
    ref_or_sha: Optional[str] = None
    plan_path: Optional[str] = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--plan":
            i += 1
            if i >= len(argv):
                print("verify-shipped: --plan requires a value", file=sys.stderr)
                return 1
            plan_path = argv[i]
        elif ref_or_sha is None:
            ref_or_sha = arg
        else:
            print(f"verify-shipped: unexpected argument: {arg}", file=sys.stderr)
            return 1
        i += 1

    if ref_or_sha is None:
        print("verify-shipped: usage: verify-shipped <ref_or_sha> [--plan <path>]", file=sys.stderr)
        return 1

    result = verify_shipped(ref_or_sha, plan_path)
    print(json.dumps(
        {
            "ref_or_sha": result.ref_or_sha,
            "resolved_sha": result.resolved_sha,
            "git_on_main": result.git_on_main,
            "frontmatter_status": result.frontmatter_status,
            "emission_snapshot": result.emission_snapshot,
            "verdict": result.verdict,
            "evidence": result.evidence,
        },
        indent=2,
    ))
    return 0 if result.verdict in ("shipped",) else (1 if result.verdict == "not_shipped" else 2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
