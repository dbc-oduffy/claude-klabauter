"""Per-finding run record: join review-wave Findings Ledger rows to the run's usage scan.

``build_findings_record`` reads only files the run already wrote (reviewer sidecars named by
``inline_review.wave_sidecar_paths``, the ``agent_usage.scan_run`` body) and spawns nothing.
``write_findings_record`` lands the body as ``<record_dir>/findings.json``. Both are report-only:
the build never raises, and callers treat a write failure as non-fatal.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops import review_findings_ledger as _ledger
from coordinator_core.ops.dispatch_emit import agent_sidecar as _sidecar
from coordinator_core.session.claimed_write import replace_text

FINDINGS_FILE = "findings.json"

TOKENS_BASIS = "even-split-of-reviewer-total"

#: A reason that marks a rejected finding as not a defect at all, rather than a declined fix.
_FALSE_ALARM_RE = re.compile(
    r"false[\s_-]?alarm|false[\s_-]?positive|not\s+an?\s+(?:defect|bug|issue|problem)|"
    r"non[\s-]?issue|works?\s+as\s+(?:designed|intended)|by\s+design|intended\s+behaviou?r",
    re.IGNORECASE,
)

_APPLIER_KEYS = ("applied_by", "applier", "applying_agent")


def _disposition(status: str, reason: object) -> str:
    """Map a ledger status onto applied / rejected / false-alarm / suspended / unresolved."""
    if status == "applied":
        return "applied"
    if status in ("em-rejected", "em_rejected", "rejected"):
        return "false-alarm" if isinstance(reason, str) and _FALSE_ALARM_RE.search(reason) else "rejected"
    if status == "suspended":
        return "suspended"
    return "unresolved"


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _agent_index(usage_scan: Optional[dict]) -> list:
    agents = usage_scan.get("agents") if isinstance(usage_scan, dict) else None
    return [a for a in agents if isinstance(a, dict)] if isinstance(agents, list) else []


def _match_agent(sidecar_rel: str, agents: list) -> Optional[dict]:
    """The usage agent that wrote ``sidecar_rel``: its id is the sidecar stem's last dot segment
    (``<agent_type>.<agent_id>.md``), else its label's slug names a path segment (the
    ``sidecar_dir("ask", run, label)`` directory)."""
    stem = sidecar_rel.rsplit("/", 1)[-1]
    if stem.endswith(".md"):
        stem = stem[:-3]
    agent_id = stem.rsplit(".", 1)[-1]
    segments = set(sidecar_rel.split("/"))
    for a in agents:
        if agent_id and a.get("agent_id") == agent_id:
            return a
    for a in agents:
        label = a.get("label")
        if isinstance(label, str) and label and (_sidecar.slug(label) in segments or _sidecar.slug(label) == stem):
            return a
    return None


def _agent_total(agent: dict) -> Optional[int]:
    total = agent.get("total_tokens")
    return total if isinstance(total, int) and not isinstance(total, bool) else None


def _applying_agent(row: dict, disposition: str, agent: Optional[dict]) -> tuple:
    """``(applying_agent, reason)``: the applier the row names, else the reviewer's label when it
    stamped the row applied, ``"em"`` for a rejected row, else null with a reason."""
    for key in _APPLIER_KEYS:
        named = row.get(key)
        if isinstance(named, str) and named:
            return named, None
    if disposition in ("rejected", "false-alarm"):
        return "em", None
    if disposition == "applied":
        if row.get("verified") is True and agent is not None and isinstance(agent.get("label"), str):
            return agent["label"], None
        return None, "applier not recorded"
    return None, f"row is {disposition}"


def build_findings_record(worktree_root: Path, inline_review: object, usage_scan: Optional[dict]) -> dict:
    """``{findings, by_role, skipped}`` for every Findings Ledger row across the review wave.

    ``tokens`` is the matched reviewer agent's ``total_tokens`` split over its rows (``tokens_basis``
    says so; the remainder goes to the first rows so the split sums exactly) and ``agent_tokens``
    the whole figure; with no matched agent or no scan both are null and ``tokens_reason`` says
    why. An unreadable or ledger-less sidecar adds a ``skipped`` entry. Never raises."""
    findings: list = []
    skipped: list = []
    listed = inline_review.get("wave_sidecar_paths") if isinstance(inline_review, dict) else None
    agents = _agent_index(usage_scan)
    for raw in listed if isinstance(listed, list) else []:
        if not (isinstance(raw, str) and raw):
            continue
        rel = _norm(raw)
        try:
            path = Path(worktree_root) / rel
            view = _ledger.read_ledger(path)
            if view is None:
                skipped.append(f"{rel}: unreadable or no Findings Ledger")
                continue
            _stamped, rows = view
            role = _ledger._extract_frontmatter_agent_type(
                _ledger._normalize_crlf(path.read_text(encoding="utf-8"))
            ) or None
            agent = _match_agent(rel, agents)
            total = _agent_total(agent) if agent is not None else None
            if agent is None:
                reason = "agent not matched" if usage_scan is not None else "no usage scan"
            else:
                reason = None if total is not None else "agent carries no total_tokens"
            n = len(rows)
            for i, row in enumerate(rows):
                status = _ledger._row_status(row)
                disposition = _disposition(status, row.get("reason"))
                applier, applier_reason = _applying_agent(row, disposition, agent)
                file_rel = row.get("file")
                out: dict[str, Any] = {
                    "finding_id": row.get("id"),
                    "sidecar": rel,
                    "file": _norm(file_rel) if isinstance(file_rel, str) else None,
                    "ledger_status": status,
                    "disposition": disposition,
                    "applying_agent": applier,
                    "reviewer_role": role,
                    "tokens": None if total is None else total // n + (1 if i < total % n else 0),
                    "agent_tokens": total,
                }
                if applier_reason:
                    out["applying_agent_reason"] = applier_reason
                if total is not None:
                    out["tokens_basis"] = TOKENS_BASIS
                else:
                    out["tokens_reason"] = reason
                findings.append(out)
        except Exception as exc:  # noqa: BLE001 -- the record is a report, never a gate
            skipped.append(f"{rel}: {exc!r}")
    by_role: dict[str, dict] = {}
    for f in findings:
        bucket = by_role.setdefault(f["reviewer_role"] or "unknown", {"findings": 0, "tokens": 0})
        bucket["findings"] += 1
        bucket["tokens"] += f["tokens"] or 0
    return {"findings": findings, "by_role": by_role, "skipped": skipped}


def write_findings_record(record_dir: Path, body: dict) -> Path:
    """Write ``<record_dir>/findings.json`` and return its path."""
    target = Path(record_dir) / FINDINGS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    replace_text(target, json.dumps(body, indent=2, sort_keys=True) + "\n")
    return target
