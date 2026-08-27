"""
coordinator_core.ops.handoff_author_lint — JSON-RPC "handoff.author_lint"
operation.

Purpose: catch, AT AUTHOR TIME, the hand-typed values in a handoff or spinoff
body that would otherwise fail — or silently degrade — somewhere much later in
the artifact's life, on someone else's session.

This is the "move enforcement earlier" half of the north star
(`CLAUDE.md` § North star): every finding this op reports is a defect a gate
already catches, but catches at the far end, where the victim is the picking-up
EM rather than the author. Three of the four have a SILENT failure mode, which
is why the author never learns about them:

  ``AC_NO_CHECKBOXES``
      An `## Acceptance criteria` section whose items are prose bullets rather
      than `- [ ]` checkboxes. The consumed-handoff completeness gate counts
      boxes; zero boxes reports `indeterminate` — a quiet unverified, not a
      loud wrong. The author sees nothing.

  ``LEDGER_ROW_UNPARSEABLE``
      A `## Session Ledger` row the canonical grammar rejects. `aggregate`
      simply does not emit a record for it, so a chain of malformed rows
      renders as ZERO effort while every file still looks well-formed. The
      2026-08-19 production instance (`0.3d`/`0.05d` written against a `\\d+`
      COUNT field) ran two sessions before anyone noticed.

  ``SUMMARY_PLACEHOLDER``
      The scaffolder's literal placeholder `summary:` left unreplaced. It is
      present and under the cap, so no validator objects; the record is
      committed to `state/` with placeholder text as its summary forever.

  ``SUMMARY_OVER_CAP``
      The one finding here whose downstream gate is LOUD rather than silent —
      and it is reported anyway, because loud-at-the-far-end still means the
      author is not the one who hears it. Since 2026-08-13
      (`handoff_transition._claim` → `handoff_normalize.normalize_present_summary`)
      an over-cap summary is truncated ahead of the claim gate rather than
      stranding the baton, so this finding is advisory: it tells the author
      their summary will be TRUNCATED, not that the baton is broken.

Read-only by construction. This op reports; it never rewrites the file. An op
that silently repaired a body would re-create the defect it exists to surface —
the author would keep typing the wrong shape and keep not learning.

Params:
    handoff_path (str, REQUIRED) — repo-relative path to the handoff/spinoff.

Returns:
    exit_code (int)   — 0 when clean, 1 when findings present, 2 on a read
                        failure (indeterminate — never conflated with clean).
    path      (str)   — the path as given.
    clean     (bool)  — True iff `findings` is empty.
    findings  (list)  — [{code, where, error, hint}]; `hint` NAMES THE FIX, per
                        `docs/wiki/guard-messaging.md` § Register: one fact,
                        once, plus a terse alternative.

Self-registration: importing this module fires
@register_op("handoff.author_lint") as a side-effect. Added to
coordinator_core/ops/__init__.py to trigger registration at start_server() time.

Reuse, not re-derivation — every grammar this op checks is owned elsewhere and
imported:
    * checkbox parsing / AC-section detection — `handoff_discharge_criteria`
      (`_parse_checkboxes`, `_ACC_CRITERIA_HEADING_RE`), the module that
      already resolves boxes inside that section by identity and position.
    * Session Ledger row grammar — `session_ledger.aggregate_chain_loe`
      (`unparseable_ledger_rows`), the parser that defines what actually gets
      summed. A second grammar here would be the exact drift the ledger
      block's own comment warns against.
    * summary cap and placeholder set — `handoff_normalize`
      (`_SUMMARY_MAX_CHARS`, `_PLACEHOLDER_SUMMARIES`).

Spec backlink:
    state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md
    § Known instances 2-4, AC-4 and AC-5.
    docs/reference/handoff-authoring-surface-classification.md — the
    classification this op discharges the "caught at author time" column of.

Negative-spec (hard-won):
    - Does NOT mutate the file, git-commit, or normalize anything. Read-only.
    - Does NOT judge an EMPTY `## Session Ledger` block — a freshly scaffolded
      handoff correctly has one; the row is appended at `/handoff` or
      `/workstream-complete`, not at birth.
    - Does NOT judge an absent `## Acceptance criteria` section as a finding on
      its own: not every handoff kind owns one, and inventing a criteria
      requirement here would be a new gate, not an earlier one. Only a section
      that EXISTS and carries zero boxes is reported.
    - Does NOT check body prose quality, section presence, or anything an EM
      alone knows. Chores go; authorship stays
      (`state/handoffs/2026-08-21-handoffs-and-spinoffs-minimal-for-hand-rolling.md`
      § What must NOT be automated).
    - Does NOT relax any downstream gate. Every gate still runs, unchanged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import handoff_archive_dest, main_worktree_root
from coordinator_core.ops.handoff_discharge_criteria import (
    _ACC_CRITERIA_HEADING_RE,
    _parse_checkboxes,
)
from coordinator_core.ops.handoff_normalize import (
    _PLACEHOLDER_SUMMARIES,
    _SUMMARY_MAX_CHARS,
)
from coordinator_core.session_ledger.aggregate_chain_loe import unparseable_ledger_rows


def _finding(code: str, where: str, error: str, hint: str) -> dict:
    return {"code": code, "where": where, "error": error, "hint": hint}


def _summary_findings(fm_text: str) -> list[dict]:
    """`summary:` findings, measured on the DECODED value — the same value
    `schema_validate._cf_summary_length_cap` measures and
    `handoff_normalize.normalize_present_summary` truncates. Measuring raw
    on-disk text instead would diverge on quoted escapes and comment tails
    (see that function's own review note)."""
    try:
        fm = yaml.safe_load(fm_text) or {}
    except Exception:  # noqa: BLE001 — an unparseable frontmatter is the schema
        # gate's finding to report, not this op's; nothing about `summary:` can
        # be said about bytes that do not decode.
        return []
    if not isinstance(fm, dict):
        return []
    summary = fm.get("summary")
    if summary is None:
        return []
    value = str(summary)

    findings: list[dict] = []
    if value in _PLACEHOLDER_SUMMARIES:
        findings.append(
            _finding(
                "SUMMARY_PLACEHOLDER",
                "summary:",
                "summary: is still the scaffolder's placeholder text",
                "Replace it with a one-line summary of the work, or re-scaffold "
                "passing the title through so the normalizer derives it.",
            )
        )
    elif len(value) > _SUMMARY_MAX_CHARS:
        findings.append(
            _finding(
                "SUMMARY_OVER_CAP",
                "summary:",
                f"summary: is {len(value)} chars, over the "
                f"{_SUMMARY_MAX_CHARS}-char cap; it will be truncated at claim",
                f"Shorten it to {_SUMMARY_MAX_CHARS} chars or fewer to choose "
                "the wording yourself instead of taking the truncation.",
            )
        )
    return findings


def _acceptance_criteria_findings(body: str) -> list[dict]:
    """One finding: an `## Acceptance criteria` section that EXISTS and carries
    zero checkboxes. That is precisely the shape the completeness gate reports
    as `indeterminate` — present enough to look answered, empty enough to
    verify nothing."""
    has_section = any(
        _ACC_CRITERIA_HEADING_RE.match(line) for line in body.splitlines()
    )
    if not has_section:
        return []
    if _parse_checkboxes(body):
        return []
    return [
        _finding(
            "AC_NO_CHECKBOXES",
            "## Acceptance criteria",
            "the section carries zero `- [ ]`/`- [x]` checkboxes",
            "Write each criterion as a `- [ ]` checkbox. The completeness gate "
            "counts boxes; a prose list reports `indeterminate`, which reads as "
            "unverified rather than unmet.",
        )
    ]


def _ledger_findings(body: str) -> list[dict]:
    return [
        _finding(
            "LEDGER_ROW_UNPARSEABLE",
            f"## Session Ledger (line {row['line_no']})",
            f"row does not parse: {row['text']!r}",
            "Use `YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary>`. "
            "`Nd`/`No` are integer COUNTS of dispatches, not durations — a row "
            "the grammar rejects is dropped silently and the chain sums to zero.",
        )
        for row in unparseable_ledger_rows(body)
    ]


def _resolve_read_path(handoff_path: str, repo_root: Path) -> "tuple[Optional[Path], Optional[str]]":
    """Resolve a repo-relative handoff path to a readable file, following the
    live -> archive fallback every other handoff-body op uses (a handoff picked
    up and archived between authoring and lint is still lintable)."""
    try:
        worktree = main_worktree_root(repo_root)
    except ValueError as exc:
        # `main_worktree_root` refuses to guess when handed something that is
        # neither a git common dir nor a worktree root. Surfaced as exit 2
        # (indeterminate) rather than propagated: a lint that RAISES gives its
        # caller a stack trace where the envelope already has a place to say
        # "could not determine".
        return None, str(exc)
    live = contained_path(worktree / handoff_path, [worktree])
    if live is None:
        return None, f"path escapes the worktree: {handoff_path}"
    if live.is_file():
        return live, None
    archived = handoff_archive_dest(worktree, live)
    if archived.is_file():
        return archived, None
    return None, f"handoff not found on disk: {handoff_path}"


@register_op("handoff.author_lint")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.author_lint" handler. See the module docstring for the
    param, the envelope, and what each finding code means."""
    if repo_root is None:
        return {
            "exit_code": 2,
            "clean": False,
            "path": params.get("handoff_path") or "",
            "findings": [],
            "error": (
                "repo_root is required — handoff.author_lint resolves paths "
                "under the main worktree and refuses to guess one"
            ),
        }

    handoff_path = params.get("handoff_path") or ""
    if not handoff_path:
        return {
            "exit_code": 2,
            "clean": False,
            "path": "",
            "findings": [],
            "error": "missing required param: handoff_path",
        }

    path, err = _resolve_read_path(handoff_path, repo_root)
    if err or path is None:
        return {
            "exit_code": 2,
            "clean": False,
            "path": handoff_path,
            "findings": [],
            "error": err,
        }

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "exit_code": 2,
            "clean": False,
            "path": handoff_path,
            "findings": [],
            "error": f"unreadable: {exc}",
        }

    # A file with no parseable frontmatter still gets its BODY linted: the
    # checkbox and ledger grammars are body-only, and refusing to lint them
    # because the frontmatter is malformed would hand the author a second
    # silent pass. The frontmatter's own malformation is the schema gate's
    # finding to report, not this op's.
    split = split_frontmatter(text)
    fm_text = split.fm_text if split is not None else ""
    body = split.body_with_leading_newline if split is not None else text
    findings = (
        _summary_findings(fm_text)
        + _acceptance_criteria_findings(body)
        + _ledger_findings(body)
    )
    return {
        "exit_code": 1 if findings else 0,
        "clean": not findings,
        "path": handoff_path,
        "findings": findings,
    }
