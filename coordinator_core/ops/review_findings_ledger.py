"""
coordinator_core.ops.review_findings_ledger — the reviewer-applies-own-findings
ledger op (retires `append_integrator_dispositions` / `fan_out_integrator`;
DoE-claude docs/plans/2026-09-26-retire-review-integrator.md, row M2).

Purpose: the review-integrator agent is retired. A reviewer now applies every
finding it logs directly to the reviewed artifact, then records one row per
finding in its own sidecar under a `## Findings Ledger` heading (a fenced
```json array). This module is the sole engine surface for that record:

  - `verify --sidecar <p>`: confirm the ledger's row count equals the
    reviewer's declared findings count, confirm each row's evidence
    (`applied` rows: `after` present in `file` now, `before` present at a
    recorded baseline and absent now; `em-rejected` rows: a non-empty
    `reason`; `suspended` rows: only accepted under a premise verdict),
    and stamp `findings_ledger:` frontmatter plus `verified: true` per row on
    success.
  - `reject --sidecar <p> --finding <id> --reason <text>`: EM-only (denied to
    subagents by `bash_guards.block_subagent_findings_reject`, M3). Restores
    `before` over `after` in the target file, marks the row `em-rejected`.
  - `targets --add <path>...`: EM-only. Registers the review target set a
    confined reviewer may write to
    (`<git_root>/.git/coordinator-sessions/<session_id>/review-targets.txt`),
    read by the re-scoped `write_guards.block_confined_agent_write` (M1).

Zero git spawns. All comparisons are in-process and CRLF-normalized.

Negative-spec:
  - Does NOT adjudicate a reviewer-vs-reviewer contest. A later reviewer's
    edit that removes an earlier row's recorded `after` is not detected by
    `verify` re-deriving evidence for that row a second time — the EM reads
    each ledger directly; no adjudication lane is added here.
  - Does NOT touch `state/review-trail/*.json` — a separate ledger.
  - Does NOT check that a finding landed in the RIGHT place, only that the
    claimed text transition actually happened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.session.declared_writes import declare_write

# Generator-provenance declaration (generator_provenance.py). `verify`/`reject`
# mutate whichever sidecar and reviewed-artifact file the caller names; the
# set is data-dependent, never a fixed artifact.
MUTATES = [".coordinator-local/subagent-share/**/*.md", "**/*"]

_LEDGER_HEADING = "## Findings Ledger"
_FINDINGS_HEADING = "## Findings"
_EXIT_INTERVIEW_HEADING = "## Exit interview"

_STATUS_APPLIED = "applied"
_STATUS_EM_REJECTED = "em-rejected"
_STATUS_SUSPENDED = "suspended"
_VALID_STATUSES = (_STATUS_APPLIED, _STATUS_EM_REJECTED, _STATUS_SUSPENDED)

#: Refusals kept from `append_integrator_dispositions` (§ Contract, M2): this
#: op's `verify`/`reject` may only ever target a real, still-open reviewer
#: findings sidecar — never an arbitrary file, never the caller's own
#: run-report. Membership mirrors that module's own re-derivation exactly
#: (DoE-claude `subagent-sandbox-policy.yaml` `report_type_map:` rows routed
#: to the `review-findings`/`staff-eng-review` templates); ported rather than
#: re-derived, since the underlying policy file has not moved.
_REVIEWER_AGENT_TYPES = frozenset(
    {
        "coordinator:code-reviewer",
        "coordinator:code-reviewer-weekly",
        "coordinator:plan-coverage-checker",
        "coordinator:parallel-review-synthesizer",
        "coordinator:security-audit-worker",
        "coordinator:dep-cve-auditor",
        "coordinator:test-evidence-parser",
        "coordinator:staff-eng",
        "coordinator:staff-data-sci",
        "coordinator:senior-front-end",
        "coordinator:staff-ux",
        "coordinator:vp-product",
        "coordinator:eng-director",
        "coordinator:overengineering-reviewer",
        "coordinator:apm",
        "coordinator:premise-checker",
        "coordinator:falsifier-integrity-reviewer",
        "coordinator:subtractive-adjudicator",
        "coordinator:blitz-em",
    }
)

#: `coordinator-doc-new --type review-findings`'s self-persist fallback
#: stamps this literal into `agent_type:` — a doc-TYPE token, not an agent
#: identity. Ported from `append_integrator_dispositions._REVIEWER_DOC_TYPE_TOKENS`.
_REVIEWER_DOC_TYPE_TOKENS = frozenset({"review-findings"})

_AGENT_TYPE_RE = re.compile(r"^agent_type:[ \t]*(.*)$")

_FENCE_DELIM_RE = re.compile(r"(?m)^```[A-Za-z0-9_-]*[ \t]*\r?$")


class LedgerError(ValueError):
    """Raised for every fail-loud validation failure in this module."""


def _normalize_crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _find_heading(text: str, heading: str) -> Optional[int]:
    """Index of `heading` where it occurs as a REAL ATX heading line, or None.

    Line-anchored, exact — a prose mention of the heading (common in this
    module's own docs) must never be read as the real boundary."""
    match = re.search(rf"(?m)^{re.escape(heading)}[ \t]*$", text)
    return match.start() if match is not None else None


def _find_findings_heading(text: str) -> Optional[Tuple[int, int]]:
    """`(start, end_of_heading_line)` of the reviewer's `## Findings` heading,
    tolerating a qualifier after the word (`## Findings (3 blocking)`,
    `## Findings table (plan order)`) — ported from
    `append_integrator_dispositions._find_findings_heading` (same rationale:
    the premise-check pass's own heading wording)."""
    match = re.search(rf"(?m)^{re.escape(_FINDINGS_HEADING)}\b[^\n]*$", text)
    return (match.start(), match.end()) if match is not None else None


def _extract_findings_section(text: str) -> Optional[str]:
    """Body of the `## Findings` section, ending at whichever of
    `## Exit interview`, `## Findings Ledger`, or end-of-document comes
    first. Returns None if the heading is absent."""
    span = _find_findings_heading(text)
    if span is None:
        return None
    rest = text[span[1]:]
    end = len(rest)
    for boundary in (_EXIT_INTERVIEW_HEADING, _LEDGER_HEADING):
        idx = _find_heading(rest, boundary)
        if idx is not None and idx < end:
            end = idx
    return rest[:end]


def _iter_fenced_blocks(text: str):
    """Yield the body of every CONSECUTIVE pair of fence-delimiter lines, in
    document order — ported from `append_integrator_dispositions._iter_fenced_blocks`."""
    delimiters = list(_FENCE_DELIM_RE.finditer(text))
    for opening, closing in zip(delimiters, delimiters[1:]):
        body_start = opening.end()
        if body_start < len(text) and text[body_start] == "\n":
            body_start += 1
        body = text[body_start:closing.start()]
        if body.endswith("\n"):
            body = body[:-1]
        if body.endswith("\r"):
            body = body[:-1]
        yield body


def _iter_json_findings_payloads(text: str):
    for body in _iter_fenced_blocks(text):
        try:
            parsed = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
            yield parsed["findings"]
        elif isinstance(parsed, list):
            # A bare top-level array under an envelope-less block (some
            # personas emit the array directly, with no wrapping object).
            yield parsed


def _declared_findings_count(text: str) -> Optional[int]:
    """The reviewer's own declared findings count: the union of every
    fenced JSON findings array, or (absent one) the number of
    `### Finding N` sub-headings inside the `## Findings` section, or
    (absent both) `None` — meaning nothing to check the ledger against, and
    `verify` trusts the ledger's own row count in that case."""
    # Scoped to the reviewer's OWN `## Findings` section only — the ledger's
    # own fenced ```json array (a list of ROWS, not findings) sits later in
    # the same document and must never be mistaken for a findings payload.
    section = _extract_findings_section(text)
    total = 0
    saw_json = False
    if section is not None:
        for findings in _iter_json_findings_payloads(section):
            saw_json = True
            total += len(findings)
    if saw_json:
        return total

    if section is not None:
        subheadings = re.findall(r"(?m)^###\s+Finding\b", section)
        if subheadings:
            return len(subheadings)
    return None


def _frontmatter_bounds(text: str) -> Optional[Tuple[int, int]]:
    """`(body_start, body_end)` of the frontmatter block, `body_end` at the
    closing `---` line's own offset (an insert there lands at column zero)."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None
    start = len(lines[0])
    offset = start
    for line in lines[1:]:
        if line.rstrip() == "---":
            return (start, offset)
        offset += len(line)
    return None


def _extract_frontmatter_key(head: str, key: str) -> Optional[str]:
    match = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*)$", head)
    return match.group(1).strip() if match else None


def _extract_baseline_sha256_block(head: str) -> Dict[str, str]:
    """Parse the `baseline_sha256:` frontmatter mapping (one `  <file>: <hex>`
    line per touched file, stamped at dispatch time — before this op ever
    runs). Returns {} when the key is absent or empty."""
    match = re.search(r"(?m)^baseline_sha256:[ \t]*$", head)
    if match is None:
        return {}
    out: Dict[str, str] = {}
    pos = match.end() + 1 if match.end() < len(head) else len(head)
    line_re = re.compile(r'^[ \t]+["\']?([^"\':]+)["\']?:[ \t]*["\']?([0-9a-fA-F]{64})["\']?[ \t]*$')
    while pos < len(head):
        next_end = head.find("\n", pos)
        segment = head[pos:] if next_end == -1 else head[pos:next_end]
        m2 = line_re.match(segment)
        if m2 is None:
            break
        out[_normalize_path(m2.group(1).strip())] = m2.group(2).lower()
        pos = len(head) if next_end == -1 else next_end + 1
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(_normalize_crlf(text).encode("utf-8")).hexdigest()


def _ledger_span(text: str) -> Optional[Tuple[int, int, int, int]]:
    """`(heading_start, fence_open_end, fence_body_start, fence_body_end)` of
    the first fenced block following `## Findings Ledger`, or None."""
    heading_idx = _find_heading(text, _LEDGER_HEADING)
    if heading_idx is None:
        return None
    rest = text[heading_idx:]
    delimiters = list(_FENCE_DELIM_RE.finditer(rest))
    for opening, closing in zip(delimiters, delimiters[1:]):
        body_start = opening.end()
        if body_start < len(rest) and rest[body_start] == "\n":
            body_start += 1
        return (
            heading_idx,
            heading_idx + opening.start(),
            heading_idx + body_start,
            heading_idx + closing.start(),
        )
    return None


def _parse_ledger_rows(text: str) -> Optional[List[Dict[str, Any]]]:
    span = _ledger_span(text)
    if span is None:
        return None
    _h, _fo, body_start, body_end = span
    body = text[body_start:body_end]
    if body.endswith("\n"):
        body = body[:-1]
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LedgerError(f"`{_LEDGER_HEADING}` fenced block is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise LedgerError(f"`{_LEDGER_HEADING}` fenced block must be a JSON array of rows")
    for row in parsed:
        if not isinstance(row, dict):
            raise LedgerError(f"`{_LEDGER_HEADING}` row is not an object: {row!r}")
    return parsed


def _rewrite_ledger_rows(text: str, rows: List[Dict[str, Any]]) -> str:
    span = _ledger_span(text)
    if span is None:
        raise LedgerError(f"cannot rewrite ledger — no `{_LEDGER_HEADING}` block found")
    _h, _fo, body_start, body_end = span
    rendered = json.dumps(rows, indent=2)
    return text[:body_start] + rendered + "\n" + text[body_end:]


def _stamp_frontmatter_key(text: str, key: str, value: str) -> str:
    """Replace an existing top-level `key:` line, or insert one at column
    zero just before the closing `---`, leaving every other line untouched."""
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        raise LedgerError("sidecar carries no `---` frontmatter block to stamp")
    start, end = bounds
    head, rest = text[:end], text[end:]
    key_re = re.compile(rf"(?m)^{re.escape(key)}:.*$")
    if key_re.search(head[start:]):
        head = head[:start] + key_re.sub(lambda _m: f"{key}: {value}", head[start:], count=1)
    else:
        head = head + f"{key}: {value}\n"
    return head + rest


def _extract_frontmatter_agent_type(text: str) -> str:
    bounds = _frontmatter_bounds(text)
    if bounds is None:
        return ""
    head = text[: bounds[1]]
    for line in head.splitlines():
        match = _AGENT_TYPE_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _check_sidecar_scope(sidecar_path: Path, repo_root: Path, text: str) -> None:
    """Refusals kept from `append_integrator_dispositions` (§ Contract): the
    target must be a real, still-open reviewer findings sidecar under a
    `subagent-share` path segment, carrying a reviewer-family `agent_type`."""
    try:
        rel = sidecar_path.resolve().relative_to(repo_root.resolve())
        rel_normalized = _normalize_path(str(rel))
    except ValueError:
        rel_normalized = _normalize_path(str(sidecar_path))

    if not any(segment == "subagent-share" for segment in rel_normalized.split("/")):
        raise LedgerError(
            "target must live under <machinery_root>/subagent-share/<session_id>/ "
            f"(the reviewer's own provisioned sidecar) — got: {sidecar_path}"
        )

    agent_type = _extract_frontmatter_agent_type(text)
    if agent_type not in _REVIEWER_AGENT_TYPES and agent_type not in _REVIEWER_DOC_TYPE_TOKENS:
        accepted = sorted(_REVIEWER_AGENT_TYPES) + sorted(_REVIEWER_DOC_TYPE_TOKENS)
        raise LedgerError(
            f"target's frontmatter agent_type is {agent_type!r}, which is not one "
            f"of the values this tool acts on ({', '.join(accepted)}) — it only "
            "ever acts on a sidecar whose deliverable IS a finding set, never a "
            "run-report or your OWN sidecar."
        )


def _row_status(row: Dict[str, Any]) -> str:
    return row.get("status") or _STATUS_APPLIED


class VerifyOutcome:
    def __init__(self, ok: bool, failures: Optional[List[str]] = None, stamp: Optional[Dict[str, Any]] = None):
        self.ok = ok
        self.failures = failures or []
        self.stamp = stamp or {}


def _verify_applied_row(
    row: Dict[str, Any], repo_root: Path, baseline_sha256: Dict[str, str]
) -> Optional[str]:
    """Returns an error string on failure, else None."""
    finding_id = row.get("id", "<unknown>")
    file_rel = row.get("file")
    if not file_rel:
        return f"{finding_id}: row carries no `file`"
    file_rel_norm = _normalize_path(str(file_rel))
    target = repo_root / file_rel_norm
    if not target.is_file():
        return f"{finding_id}: file not found on disk: {file_rel_norm}"
    current = _normalize_crlf(target.read_text(encoding="utf-8", errors="replace"))
    before = _normalize_crlf(str(row.get("before") or ""))
    after = _normalize_crlf(str(row.get("after") or ""))

    if not before and not after:
        return f"{finding_id}: both `before` and `after` are empty"

    baseline_hash = baseline_sha256.get(file_rel_norm)

    if not after:
        # Deletion: `before` must be non-empty, absent now, present at baseline.
        if before in current:
            return f"{finding_id}: `before` text is still present in {file_rel_norm} — deletion not applied"
        if baseline_hash is not None:
            # Weaker check for deletion (position of the removed span is not
            # recorded): the file must have actually changed since baseline.
            if _sha256(current) == baseline_hash:
                return (
                    f"{finding_id}: {file_rel_norm} hashes identical to its recorded baseline — "
                    "no edit landed"
                )
        return None

    if not before:
        # Insertion: `after` must be present now and must NOT have existed at
        # baseline (checked by absence-from-baseline-hash reconstruction: the
        # file with this occurrence of `after` removed must hash to baseline).
        if after not in current:
            return f"{finding_id}: `after` text not found in {file_rel_norm} — insertion not applied"
        if baseline_hash is not None:
            reconstructed = current.replace(after, "", 1)
            if _sha256(reconstructed) != baseline_hash and _sha256(current) == baseline_hash:
                return (
                    f"{finding_id}: {file_rel_norm} hashes identical to its recorded baseline — "
                    "no edit landed"
                )
        return None

    # Replacement.
    if after not in current:
        return f"{finding_id}: `after` text not found in {file_rel_norm} — replacement not applied"
    if before in current:
        return f"{finding_id}: `before` text is still present in {file_rel_norm} — replacement not applied"
    if baseline_hash is not None:
        reconstructed = current.replace(after, before, 1)
        if _sha256(reconstructed) != baseline_hash:
            return (
                f"{finding_id}: {file_rel_norm} does not reconcile against its recorded baseline "
                "once this row's edit is reversed — the claimed `before`/`after` pair does not "
                "match what actually happened"
            )
    return None


def verify(sidecar_path: Path, *, repo_root: Path) -> VerifyOutcome:
    if not sidecar_path.is_file():
        raise LedgerError(f"sidecar not found: {sidecar_path}")
    text = sidecar_path.read_text(encoding="utf-8", errors="replace")
    _check_sidecar_scope(sidecar_path, repo_root, text)

    rows = _parse_ledger_rows(text)
    if rows is None:
        raise LedgerError(f"no `{_LEDGER_HEADING}` block found in {sidecar_path}")

    declared_count = _declared_findings_count(text)
    if declared_count is not None and len(rows) != declared_count:
        return VerifyOutcome(
            False,
            [
                f"ledger has {len(rows)} row(s) but the reviewer declared "
                f"{declared_count} finding(s) — every finding must have exactly one row"
            ],
        )

    bounds = _frontmatter_bounds(text)
    head = text[: bounds[1]] if bounds else ""
    baseline_sha256 = _extract_baseline_sha256_block(head)

    failures: List[str] = []
    seen_ids = set()
    applied = em_rejected = suspended = 0
    new_rows: List[Dict[str, Any]] = []
    for row in rows:
        finding_id = row.get("id")
        if not finding_id:
            failures.append("a ledger row carries no `id`")
            new_rows.append(row)
            continue
        if finding_id in seen_ids:
            failures.append(f"{finding_id}: duplicate row id")
        seen_ids.add(finding_id)

        status = _row_status(row)
        if status not in _VALID_STATUSES:
            failures.append(f"{finding_id}: unrecognized status {status!r}")
            new_rows.append(row)
            continue

        if row.get("superseded_by"):
            # A later reviewer's edit superseded this row's own evidence —
            # satisfied by naming the finding it defers to, never
            # re-deriving evidence for a text span that no longer exists.
            new_rows.append(row)
            applied += 1
            continue

        if status == _STATUS_APPLIED:
            error = _verify_applied_row(row, repo_root, baseline_sha256)
            if error:
                failures.append(error)
                new_rows.append(row)
                continue
            applied += 1
        elif status == _STATUS_EM_REJECTED:
            if not row.get("reason"):
                failures.append(f"{finding_id}: em-rejected row carries no `reason`")
                new_rows.append(row)
                continue
            em_rejected += 1
        elif status == _STATUS_SUSPENDED:
            suspended += 1

        stamped_row = dict(row)
        stamped_row["verified"] = True
        new_rows.append(stamped_row)

    if failures:
        return VerifyOutcome(False, failures)

    verified_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_text = _rewrite_ledger_rows(text, new_rows)
    stamp_value = (
        "{rows: %d, applied: %d, em_rejected: %d, suspended: %d, verified_at: %s}"
        % (len(new_rows), applied, em_rejected, suspended, verified_at)
    )
    new_text = _stamp_frontmatter_key(new_text, "findings_ledger", stamp_value)
    sidecar_path.write_text(new_text, encoding="utf-8", newline="\n")
    declare_write(sidecar_path)
    return VerifyOutcome(
        True,
        stamp={
            "rows": len(new_rows),
            "applied": applied,
            "em_rejected": em_rejected,
            "suspended": suspended,
            "verified_at": verified_at,
        },
    )


def reject(sidecar_path: Path, finding_id: str, reason: str, *, repo_root: Path) -> str:
    """Restore `before` over `after` in the finding's file, mark the row
    `em-rejected` with `reason`. Returns the target file's repo-relative path.

    Refuses (raises `LedgerError`) unless the row's own `after` text occurs
    exactly once in the current file at reject time — a later reviewer's
    edit may have removed it, and a naive restore would silently clobber
    unrelated text or no-op."""
    if not reason or not reason.strip():
        raise LedgerError("--reason must be a non-empty one-line explanation")
    if not sidecar_path.is_file():
        raise LedgerError(f"sidecar not found: {sidecar_path}")
    text = sidecar_path.read_text(encoding="utf-8", errors="replace")
    _check_sidecar_scope(sidecar_path, repo_root, text)
    rows = _parse_ledger_rows(text)
    if rows is None:
        raise LedgerError(f"no `{_LEDGER_HEADING}` block found in {sidecar_path}")

    target_row = None
    for row in rows:
        if row.get("id") == finding_id:
            target_row = row
            break
    if target_row is None:
        raise LedgerError(f"no ledger row with id {finding_id!r} in {sidecar_path}")

    file_rel = _normalize_path(str(target_row.get("file") or ""))
    if not file_rel:
        raise LedgerError(f"{finding_id}: row carries no `file`")
    after = _normalize_crlf(str(target_row.get("after") or ""))
    before = _normalize_crlf(str(target_row.get("before") or ""))
    target_path = repo_root / file_rel
    if not target_path.is_file():
        raise LedgerError(f"{finding_id}: file not found on disk: {file_rel}")

    file_text = _normalize_crlf(target_path.read_text(encoding="utf-8", errors="replace"))
    if after:
        occurrences = file_text.count(after)
        if occurrences == 0:
            raise LedgerError(
                f"{finding_id}: `after` text not found in {file_rel} — nothing to revert "
                "(a later edit may have already changed this span)"
            )
        if occurrences > 1:
            raise LedgerError(
                f"{finding_id}: `after` text occurs {occurrences} times in {file_rel} — "
                "refusing an ambiguous revert; narrow the row's recorded text"
            )
        new_file_text = file_text.replace(after, before, 1)
    else:
        # Insertion being rejected with nothing to remove is a no-op revert.
        new_file_text = file_text

    target_path.write_text(new_file_text, encoding="utf-8", newline="\n")
    declare_write(target_path)

    target_row["status"] = _STATUS_EM_REJECTED
    target_row["reason"] = reason.strip()
    target_row.pop("verified", None)
    new_text = _rewrite_ledger_rows(text, rows)
    sidecar_path.write_text(new_text, encoding="utf-8", newline="\n")
    declare_write(sidecar_path)
    return file_rel


def _session_targets_path(repo_root: Path, session_id: str) -> Path:
    return repo_root / ".git" / "coordinator-sessions" / session_id / "review-targets.txt"


def targets_add(repo_root: Path, session_id: str, paths: List[str]) -> List[str]:
    """Append `paths` (repo-relative, forward-slash) to the session's
    review-targets file, de-duplicated. Returns the full, de-duplicated set
    after the append. Refuses an absolute path or a drive letter."""
    normalized: List[str] = []
    for raw in paths:
        # Checked textually, not via `Path(raw)` — on POSIX, `pathlib.Path`
        # never recognizes a Windows drive letter (`.drive` is always ""),
        # so this must catch a `C:foo.py`-shaped path regardless of the host
        # platform this op happens to run on.
        if raw.startswith("/") or raw.startswith("\\") or re.match(r"^[A-Za-z]:", raw):
            raise LedgerError(
                f"targets --add refuses an absolute or drive-lettered path: {raw!r} — "
                "pass a repo-relative forward-slash path"
            )
        normalized.append(_normalize_path(raw))

    target_file = _session_targets_path(repo_root, session_id)
    existing: List[str] = []
    if target_file.is_file():
        existing = [
            line.strip()
            for line in target_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    merged = list(existing)
    for entry in normalized:
        if entry not in merged:
            merged.append(entry)

    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("\n".join(merged) + ("\n" if merged else ""), encoding="utf-8", newline="\n")
    declare_write(target_file)
    return merged


def _resolve_git_root(cwd: Optional[str] = None) -> Path:
    root = show_toplevel(cwd)
    if root is None:
        raise LedgerError(f"cannot resolve git repo root from {cwd or '.'}")
    return Path(root)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review-findings-ledger",
        description=(
            "The reviewer-applies-own-findings ledger: verify a sidecar's "
            "`## Findings Ledger` block, EM-only reject a finding, or "
            "EM-only register the session's confined-reviewer write targets."
        ),
    )
    parser.add_argument("--root", default=None, help="Repo root (default: git rev-parse from cwd).")
    sub = parser.add_subparsers(dest="command", required=True)

    verify_p = sub.add_parser("verify", help="Verify a reviewer sidecar's findings ledger.")
    verify_p.add_argument("--sidecar", required=True)

    reject_p = sub.add_parser("reject", help="EM-only: revert one finding and record why.")
    reject_p.add_argument("--sidecar", required=True)
    reject_p.add_argument("--finding", required=True)
    reject_p.add_argument("--reason", required=True)

    targets_p = sub.add_parser("targets", help="EM-only: register review targets for this session.")
    targets_p.add_argument("--add", action="append", default=[], dest="add")
    targets_p.add_argument(
        "--session-id",
        required=True,
        help="The invoking EM session's own id (keys the same sandbox roots the write guard reads).",
    )

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        git_root = Path(args.root) if args.root else _resolve_git_root()
    except LedgerError as exc:
        print(f"review-findings-ledger: {exc}", file=sys.stderr)
        return 2

    if args.command == "verify":
        try:
            outcome = verify(Path(args.sidecar), repo_root=git_root)
        except LedgerError as exc:
            print(f"review-findings-ledger: {exc}", file=sys.stderr)
            return 2
        if not outcome.ok:
            print("review-findings-ledger: verify FAILED:", file=sys.stderr)
            for failure in outcome.failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
        print(f"review-findings-ledger: OK — {outcome.stamp}")
        return 0

    if args.command == "reject":
        try:
            touched = reject(Path(args.sidecar), args.finding, args.reason, repo_root=git_root)
        except LedgerError as exc:
            print(f"review-findings-ledger: {exc}", file=sys.stderr)
            return 1
        print(f"review-findings-ledger: OK — reverted {args.finding} in {touched}")
        return 0

    if args.command == "targets":
        if not args.add:
            print("review-findings-ledger: targets --add requires at least one path", file=sys.stderr)
            return 2
        try:
            merged = targets_add(git_root, args.session_id, args.add)
        except LedgerError as exc:
            print(f"review-findings-ledger: {exc}", file=sys.stderr)
            return 1
        print(f"review-findings-ledger: OK — {len(merged)} target(s) registered for session {args.session_id}")
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
