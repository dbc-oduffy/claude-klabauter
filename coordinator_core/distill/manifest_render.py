"""
coordinator_core.distill.manifest_render — human-legible markdown rendering
for the C9 disposal-manifest shape (2026-07-23 architecture review § 1b).

Purpose: the disposal manifest is the only artifact in the distill-ceremony
tier that precedes an irreversible act (a real file delete), and until this
module existed it had the thinnest human surface of anything in the system —
JSON on disk plus an op result carrying three integers and a boolean. The
per-file guard receipts AC3 demands are structured for a machine; this module
renders them for a human, so a PM at stamp time reads a short markdown table
rather than skimming (or not reading) a JSON blob.

Sibling, not member, of ``manifest_schema.py``: that module's own docstring
scopes it to "STRUCTURE and VALIDATION only... performs no disk I/O and no
business logic" — rendering is presentation, a third concern, so it gets its
own module rather than growing manifest_schema.py past its own negative spec.

Negative-spec: this module performs no I/O, no guard evaluation, and makes no
eligibility/authorization judgment — it only formats an already-assembled (or
already-loaded) disposal-manifest dict into markdown. Pure function, no
side effects.

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C9/C12/C13
Governing review: state/review-trail/findings/2026-07-23-the Staff Engineer-arch-ceremony-redesign-post-execution.md § 1b
"""

from __future__ import annotations

from typing import Any

__all__ = ["render_disposal_manifest"]


def _escape_cell(value: Any) -> str:
    """Escape one markdown-table cell value: pipes would otherwise split the
    cell, and a literal newline (a multi-line guard-evidence string, e.g. a
    multi-line commitment-closure detail) would otherwise break the row."""
    text = str(value)
    text = text.replace("|", "\\|")
    text = text.replace("\r\n", "\n").replace("\n", "<br>")
    return text


def _blocking_guard_and_evidence(row: dict[str, Any]) -> tuple[str, str]:
    """Return (guard names, evidence) for a RETAINED row.

    Prefers the structured per-guard receipts (``guards_run`` entries with
    verdict "block") for both columns. Falls back to the row's free-text
    ``retention_reason`` when no receipt carries a "block" verdict — this
    covers the synthetic blockers (``artifact-class-unresolved``,
    ``memory-pointer-exclusion``, the absent-path "candidate path absent on
    disk at assemble time" reason) that are never represented as a
    ``guards_run`` receipt, so a retained row is never rendered with an
    unexplained blank guard/evidence pair.
    """
    blocking = [r for r in row.get("guards_run", []) if r.get("verdict") == "block"]
    if blocking:
        guard_names = ", ".join(r["guard"] for r in blocking)
        evidence = "<br>".join(f"{r['guard']}: {r['evidence']}" for r in blocking)
        return guard_names, evidence

    reason = row.get("retention_reason") or ""
    prefix = "blocked by: "
    guard_names = reason[len(prefix):] if reason.startswith(prefix) else "—"
    return guard_names, reason or "—"


def render_disposal_manifest(manifest: dict[str, Any]) -> str:
    """Render a disposal-manifest dict as a markdown document: a counts line,
    a mass-throttle banner when the flag is set, and a per-row table (path,
    artifact_class, verdict, blocking guard(s) + evidence for retained rows).

    Pure — no I/O, no mutation of ``manifest``. Safe to call on a manifest
    before OR after the disposal_authorized_* stamp is applied (STAMP_FIELDS
    are not consulted here).
    """
    stats = manifest["scan_stats"]
    lines = [
        f"# Disposal manifest — run {manifest['run_id']}",
        "",
        f"**Scanned:** {stats['total_scanned']}  "
        f"**Eligible:** {stats['eligible_count']}  "
        f"**Retained:** {stats['retained_count']}",
        "",
    ]

    if manifest.get("mass_throttle"):
        lines.append(
            "> ⚠️ **MASS THROTTLE ENGAGED** — this run's eligible set crossed "
            "a named safety threshold (absolute count or ratio). "
            "`distill.apply_disposal` refuses to act on this manifest unless "
            "the PM stamp's note acknowledges it (`mass-throttle-ack`)."
        )
        lines.append("")

    rows = manifest.get("rows", [])
    if not rows:
        lines.append("_No candidates scanned._")
        return "\n".join(lines) + "\n"

    lines.append("| Path | Class | Verdict | Blocking guard(s) | Evidence |")
    lines.append("|---|---|---|---|---|")
    for row in rows:
        path = _escape_cell(row["path"])
        artifact_class = _escape_cell(row["artifact_class"])
        if row["eligible"]:
            verdict = "ELIGIBLE"
            guard_names, evidence = "—", "—"
        else:
            verdict = "RETAINED"
            raw_guards, raw_evidence = _blocking_guard_and_evidence(row)
            guard_names, evidence = _escape_cell(raw_guards), _escape_cell(raw_evidence)
        lines.append(f"| {path} | {artifact_class} | {verdict} | {guard_names} | {evidence} |")

    return "\n".join(lines) + "\n"
