"""
coordinator_core.session_ledger.aggregate_chain_loe

Purpose: chain-walk aggregator — traverse a handoff predecessor chain, parse
every ``## Session Ledger`` block encountered, and emit summed LoE metrics.
Given the terminal handoff of a multi-session chain (the one consumed by the
chain-terminal ``/workstream-complete``), walks the ``predecessor:`` chain
backward to root, collects every Session Ledger block from every handoff
visited, deduplicates by ``session_id``, and emits summed
(agent_dispatches, opus_dispatches, em_tokens) + unioned commits + recomputed
t-shirt. Consumed by ``/workstream-complete`` Step 2.6 on the chain-terminal
path.

Port of: aggregate-chain-loe.sh (example-doctrine-repo b644d5a9, 2026-07-22, 709 LoC bash)
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 4
Spec backlink: docs/plans/2026-06-29-handoff-lineage-dag-fan-in-fan-out.md § C2

Chain-walk delegation: this port calls ``coordinator_core.dag.walk_forward``
in-process (edge_kinds={'predecessor', 'additional_predecessors'}) instead of
shelling out to ``bin/lib/walk-handoff-dag.js`` — dag.py is the already-landed
Python port of that primitive (T4d). ``forked_from`` is deliberately excluded
from the edge-kind set: it is lineage/render-only and excluded from LoE
aggregation per DR-014 effort-isolation, exactly as the bash oracle excludes it.

Two callers into this module:
  - The CLI trampoline (``coordinator/bin/aggregate-chain-loe.py``,
    example-doctrine-repo) calls this module's own ``main()`` directly, in-process —
    this is a cold ceremony-only caller, so there is no daemon-RPC overhead
    to justify (mirrors ``regenerate-orientation-cache``'s trampoline shape,
    NOT ``cc_invoke()``/``route()``). A former extensionless sibling
    (``coordinator/bin/aggregate-chain-loe``, no ``.py`` suffix) called
    ``aggregate()`` directly instead of going through ``main()`` — a
    duplicate-CLI leftover from the strangler port, deduped at source in
    favor of this ``.py`` trampoline; see git log for its removal.
  - ``@register_op("session_ledger.aggregate_chain_loe")`` below exists for
    future in-process/daemon-RPC callers; central-registry wiring
    (ops/__init__.py, ops/_registry_map.py, ipc.py::_OP_KEY_SCOPE,
    authz/classification.py) is deferred to the EM per the build-wave's
    shared-tree concurrency-safety convention (see this chunk's central-reg
    fragment).

Negative-spec (mirrors the bash oracle, preserve exactly):
  - Does NOT write anything — read-only chain walk + report. No side effects.
  - Does NOT walk ``forked_from`` edges (lineage/render-only, DR-014
    effort-isolation — see above).
  - Does NOT overwrite a caller's frontmatter — output is a standalone report
    string (yaml-frontmatter or json), spliced in by the caller if desired.
  - ``resolve_handoff_path``'s tier-4 archive search is NOT the same code path
    as ``dag.resolve_target``'s internal predecessor-edge resolution — this
    mirrors an existing asymmetry in the bash oracle (the CLI's own
    ``--terminal-handoff`` entry-point resolution searches
    ``<git-root>/archive/handoffs/**``; the DAG walk's internal edge resolution
    infers its own repo_root from the terminal handoff's own directory, via
    ``dag.walk_forward``'s ``handoff_dir=None`` default — NOT recomputed here).
    Do not "fix" this by threading an explicit ``handoff_dir`` into
    ``walk_forward``; that would diverge from the bash oracle's actual
    (already node-invoked-with-no---handoff-dir-flag) behaviour.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from coordinator_core.dag import walk_forward
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ipc import register_op
from coordinator_core.wire_paths import rel_id
from coordinator_core.loe_thresholds import DEFAULT_THRESHOLDS, compute_tshirt, load_thresholds
from coordinator_core.state_root import StateRootError, coordinator_state_root

_EDGE_KINDS = {"predecessor", "additional_predecessors"}

_KNOWN_FIELDS = (
    "session_id",
    "agent_dispatches",
    "opus_dispatches",
    "em_tokens",
    "commits",
    "created",
)

_SESSION_LEDGER_HEADING_RE = re.compile(r"^## Session Ledger")
_ANY_HEADING_RE = re.compile(r"^## ")
_SEPARATOR_ROW_RE = re.compile(r"^-+$")
_NUMERIC_RE = re.compile(r"^[0-9]+$")
_TOKEN_RE = re.compile(r"^[0-9,_]+$")

# One-line-append grammar (the ``coordinator-doc-new`` scaffold's ACTUAL
# format, and what every live handoff uses in practice — see
# ``parse_session_ledgers`` docstring for the format-divergence history):
#   YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <one-line summary>
# e.g. "2026-07-25 | 67193d | L | 21d / 2o | Actioned 3 closed_reason memos..."
# ``tshirt`` is captured only to correctly delimit the row (not stored in
# the emitted record — aggregate() recomputes tshirt itself from summed
# agent_dispatches/opus_dispatches/em_tokens via loe_thresholds.compute_tshirt,
# it never reads a per-session tshirt value out of a ledger row).
_ONELINE_RE = re.compile(
    r"^(?P<created>\d{4}-\d{2}-\d{2})\s*\|\s*"
    r"(?P<session_id>[0-9a-fA-F]{4,12})\s*\|\s*"
    r"(?P<tshirt>\S+)\s*\|\s*"
    r"(?P<agent_dispatches>\d+)d\s*/\s*(?P<opus_dispatches>\d+)o\s*\|\s*"
    r"(?P<summary>.+)$"
)


def format_oneline_row(
    created: str,
    session_id: str,
    tshirt: str,
    agent_dispatches: int,
    opus_dispatches: int,
    summary: str,
) -> str:
    """Render one ``## Session Ledger`` row in the one-line-append grammar
    ``_ONELINE_RE`` parses back — the single authoritative place that
    ASSEMBLES this row, mirroring ``_ONELINE_RE``/``_parse_oneline_row`` as
    the single authoritative place that PARSES it. Callers that need to
    write a ready-to-paste Session Ledger line (e.g.
    ``handoff-loe-summary.py``) MUST call this rather than hand-formatting
    a third copy of the field order — the ``coordinator-doc-new`` scaffold
    comment and ``docgen/templates/handoff.json`` document this shape in
    prose only; this function is the one place that actually builds it.

    *session_id* is truncated to its trailing 6 characters (``sid6``),
    matching the ``sid[-6:]`` convention ``coordinator_core.ops.
    coordinator_complete_entry`` already uses for its own sid6-suffixed
    filenames — NOT the ``tail -c 7 | head -c 6`` variant referenced in
    ``wsc_resolve.py`` (that variant drops the session id's final
    character; this keeps it).

    *created* is truncated to its leading ``YYYY-MM-DD`` — accepts either a
    bare date or a full ISO timestamp.

    Negative-spec: does NOT validate that the resulting sid6 is valid hex.
    A session id whose trailing 6 characters aren't hex digits (e.g. the
    literal ``"unknown"`` session-id-resolution fallback) produces a row
    ``_ONELINE_RE`` will NOT parse back — that degradation is inherent to
    the grammar (hex-only sid6) and intentionally left to the caller to
    detect via a round-trip parse, not silently patched over here.
    """
    date = (created or "")[:10]
    sid6 = (session_id or "")[-6:]
    return f"{date} | {sid6} | {tshirt} | {agent_dispatches}d / {opus_dispatches}o | {summary}"


# ---------------------------------------------------------------------------
# git-root / state-root resolution
# ---------------------------------------------------------------------------


def resolve_repo_root(cwd: Optional[Path] = None) -> Path:
    """``git rev-parse --show-toplevel`` from *cwd* — mirrors bash GIT_ROOT (:112-121).

    Raises ``ValueError`` when *cwd* is not inside a git repo (mirrors the bash
    oracle's hard ``exit 1`` — "not inside a git repo").

    Windows Git Bash cygpath normalisation (bash :116-121) is a deliberate
    parity-PLUS drop: ``pathlib.Path``/``git``'s own output already gives a
    consistent form under Python subprocess invocation — no cygpath shim
    needed here.
    """
    cwd = cwd or Path.cwd()
    out = show_toplevel(str(cwd))
    if not out:
        raise ValueError("not inside a git repo")
    return Path(out)


def resolve_state_root(coordinator_root: Path, cwd: Path) -> Path:
    """Resolve the state root via the native ``coordinator_state_root`` seam.

    Calls ``coordinator_core.state_root.coordinator_state_root()`` (Rule 4/5
    default) in-process — the already-landed native peer of
    coordinator-state-root.sh (example-doctrine-repo 6fb5fb37, 2026-07-22; aggregate-chain-loe.sh's
    own ``source coordinator-state-root.sh; STATE_ROOT="$(coordinator_state_root)"``).
    *coordinator_root* is unused by the native seam (it derives the
    example-doctrine-repo/claude-klabauter roots via its own resolvers) and is retained only for call-site
    compatibility with existing callers of this function.

    Review: code-reviewer — *cwd* is threaded explicitly to
    ``coordinator_state_root(git_root=...)`` rather than left to that seam's
    own ambient-``os.getcwd()`` ``git rev-parse``, so this function is
    provably scoped to the *cwd* argument (not the process's ambient cwd) on
    the primary path too, not just the fallback. ``resolve_repo_root(cwd)``
    already resolves the git root FROM *cwd* (``git -C cwd rev-parse
    --show-toplevel``), so passing that resolved root through as
    ``git_root=`` is safe even when *cwd* is a subdirectory — unlike passing
    *cwd* itself raw, which ``_resolve_git_root``'s "treat git_root as
    already-a-toplevel" contract would misinterpret. Rule-5 classification
    (``is_meta_repo``/``_state_of``) only consumes the resolved root string,
    so pre-resolving it here changes nothing about that dispatch.

    Falls back to ``<git-root>/state`` (pre-seam, Rule-5-sibling-repo shape)
    only when the native seam raises ``StateRootError`` — never silently
    returns a wrong-repo path.
    """
    del coordinator_root  # unused: native seam self-resolves example-doctrine-repo/claude-klabauter roots
    try:
        git_root = resolve_repo_root(cwd)
        out = coordinator_state_root(git_root=str(git_root))
    except (ValueError, StateRootError):
        # ValueError: cwd is not inside a git repo (resolve_repo_root); do
        # NOT fall back to the seam's own ambient-cwd resolution here — that
        # would silently resolve against a *different* repo than the one
        # `cwd` names, exactly the hazard this function's contract forbids.
        out = ""
    if out:
        return Path(out)
    return resolve_repo_root(cwd) / "state"


# ---------------------------------------------------------------------------
# resolve_handoff_path — 4-tier resolution of --terminal-handoff (bash :135-192)
# ---------------------------------------------------------------------------


def resolve_handoff_path(
    raw: str,
    git_root: Path,
    handoffs_dir: Path,
    archive_dir: Path,
) -> Optional[str]:
    """Resolve *raw* (the ``--terminal-handoff`` argument) to an absolute path.

    Tiers, in order:
      1. As-is — absolute path or relative to the process cwd.
      2. Relative to *git_root*.
      3. Basename under *handoffs_dir* (``state/handoffs/<basename>``).
      4. Recursive search under *archive_dir* — first by exact basename match,
         then (if none found) by path-suffix match against either the full
         *raw* string or its basename, over every ``*.md`` file.

    Returns ``None`` if unresolvable in all four tiers (mirrors the bash
    oracle's empty-string return, which the caller treats as "not found").
    """
    raw = raw.strip()
    if not raw:
        return None

    # 1. As-is (absolute path or relative to process cwd — os.path.isfile
    #    resolves relative paths against os.getcwd(), same as bash's `[[ -f ]]`).
    if os.path.isfile(raw):
        return os.path.abspath(raw)

    # 2. Relative to git root
    from_root = git_root / raw
    if from_root.is_file():
        return str(from_root)

    # 3. Basename under state/handoffs/
    basename = os.path.basename(raw)
    in_handoffs = handoffs_dir / basename
    if in_handoffs.is_file():
        return str(in_handoffs)

    # 4. Recursive search under archive/handoffs/**/
    # Review: code-reviewer — return the FIRST match in os.walk traversal
    # order (no sort). The bash oracle takes `find ... | head -1`, whose
    # result order is filesystem-traversal order (arbitrary/OS-dependent,
    # NOT lexicographic); sorting here silently re-resolved duplicate
    # basenames across archive subdirs to a different physical file than
    # the oracle would pick.
    if archive_dir.is_dir():
        for root, _dirs, files in os.walk(archive_dir):
            for f in files:
                if f == basename:
                    return os.path.join(root, f)

        for root, _dirs, files in os.walk(archive_dir):
            for f in files:
                if not f.endswith(".md"):
                    continue
                full = os.path.join(root, f)
                if full.endswith(raw) or full.endswith(basename):
                    return full

    return None


# ---------------------------------------------------------------------------
# extract_frontmatter_field — bounded YAML-frontmatter scalar extractor
# (bash :200-225)
# ---------------------------------------------------------------------------


def extract_frontmatter_field(text: str, field: str) -> str:
    """Extract a single scalar frontmatter field between the first ``---`` pair.

    Handles ``field: value``, strips surrounding double-quotes. Returns the
    empty string if the field, or the frontmatter block itself, is absent.
    Comment lines (first non-whitespace char ``#``) and blank lines inside the
    block are skipped (mirrors the awk ``in_fm && /^[[:space:]]*[^#]/`` guard).
    """
    pattern = re.compile(r"^[ \t]*" + re.escape(field) + r"[ \t]*:[ \t]*")
    in_fm = False
    for line in text.splitlines():
        if line.startswith("---"):
            if in_fm:
                break
            in_fm = True
            continue
        if not in_fm:
            continue
        if not re.match(r"^[ \t]*[^#]", line):
            continue
        m = pattern.match(line)
        if m:
            val = line[m.end():].strip()
            if len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                val = val[1:-1]
            return val
    return ""


# ---------------------------------------------------------------------------
# parse_session_ledgers — the awk-table-parser rewrite (bash :234-321)
# ---------------------------------------------------------------------------


def _parse_oneline_row(line: str) -> Optional[Dict[str, str]]:
    """Row-parser for the one-line-append grammar (``_ONELINE_RE``).

    Returns a complete record dict, or ``None`` if *line* doesn't match — the
    caller emits the record directly (unlike the Field/Value grammar, one
    oneliner line IS one whole session's record, there is no cross-line
    accumulation). ``em_tokens``/``commits`` are absent from this grammar
    entirely — represented as ``"null"``/``""`` respectively, the SAME
    "genuinely absent" sentinels the Field/Value grammar's ``_blank()``
    defaults already use for an unfilled row, so ``aggregate()``'s
    already-existing absent-vs-zero handling (``tok_raw != "null"`` guard,
    the empty-``commits_raw`` no-op) covers both grammars identically with
    no new corruption surface.
    """
    m = _ONELINE_RE.match(line.strip())
    if not m:
        return None
    return {
        "session_id": m.group("session_id"),
        "agent_dispatches": m.group("agent_dispatches"),
        "opus_dispatches": m.group("opus_dispatches"),
        "em_tokens": "null",
        "commits": "",
        "created": m.group("created"),
    }


def parse_session_ledgers(text: str) -> List[Dict[str, str]]:
    """Parse ALL ``## Session Ledger`` blocks from a handoff file body.

    Returns one record dict per block (multiple blocks per file are supported
    — append-only handoff history, NOT "parse the first table"). Each record
    has keys ``session_id, agent_dispatches, opus_dispatches, em_tokens,
    commits, created``. A record is only emitted (flushed) if ``session_id``
    is non-empty. A block ends at the next ``##`` heading (of any kind) or
    EOF.

    Two grammars are recognized inside a ``## Session Ledger`` block, and
    MAY be mixed within the same block (the real 2026-07-24 production case —
    a chain carrying both an archived Field/Value handoff and live
    one-liner-only handoffs):

      1. **Field/Value table** (``| field | value |`` rows, gated by
         ``line.startswith("|")``) — accumulates across multiple rows into
         one record, flushed at the next heading/EOF. ``agent_dispatches``/
         ``opus_dispatches`` default to ``"0"``, ``em_tokens`` defaults to
         ``"null"`` when absent (mirrors the awk ``flush_record`` defaults).
         This is the grammar 3 archived handoffs (2026-07-22..24) use; the
         ``coordinator-doc-new`` scaffolder no longer emits it.
      2. **One-line-append** (``YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> |
         <summary>``, dispatched to ``_parse_oneline_row``) — what the
         scaffolder actually emits and what every live (non-archived)
         handoff uses. Each matching line is a complete, independent record
         (no cross-line accumulation) emitted directly into ``records``.
         ``em_tokens``/``commits`` are not carried by this grammar; see
         ``_parse_oneline_row`` for how their absence is represented.

    Section-boundary detection (the ``## Session Ledger`` / next-``##``
    scan) exists exactly once below; the two grammars differ only in their
    per-line row-parser, not in block detection.
    """

    def _blank() -> Dict[str, str]:
        return {k: "" for k in _KNOWN_FIELDS}

    records: List[Dict[str, str]] = []
    in_ledger = False
    current = _blank()

    def _flush() -> None:
        if current["session_id"]:
            records.append(
                {
                    "session_id": current["session_id"],
                    "agent_dispatches": current["agent_dispatches"] or "0",
                    "opus_dispatches": current["opus_dispatches"] or "0",
                    "em_tokens": current["em_tokens"] or "null",
                    "commits": current["commits"],
                    "created": current["created"],
                }
            )

    for line in text.splitlines():
        if _SESSION_LEDGER_HEADING_RE.match(line):
            _flush()
            in_ledger = True
            current = _blank()
            continue

        if in_ledger and _ANY_HEADING_RE.match(line) and not _SESSION_LEDGER_HEADING_RE.match(line):
            _flush()
            in_ledger = False
            current = _blank()
            continue

        if not in_ledger:
            continue

        oneline_rec = _parse_oneline_row(line)
        if oneline_rec is not None:
            records.append(oneline_rec)
            continue

        if line.startswith("|"):
            inner = re.sub(r"^\|[ \t]*", "", line)
            inner = re.sub(r"[ \t]*\|[ \t]*$", "", inner)
            cells = re.split(r"[ \t]*\|[ \t]*", inner)
            if len(cells) < 2:
                continue
            field = cells[0].strip()
            value = cells[1].strip()
            if field == "Field" or _SEPARATOR_ROW_RE.match(field):
                continue
            if field in _KNOWN_FIELDS:
                current[field] = value

    if in_ledger:
        _flush()

    return records


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def _to_int(value: str) -> int:
    """Sanitize a numeric field: non-all-digits => 0 (mirrors bash :502-503)."""
    return int(value) if _NUMERIC_RE.match(value or "") else 0


def _parse_date_prefix(value: str) -> Optional[datetime]:
    """Parse the first 10 chars of *value* as YYYY-MM-DD; None if unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def aggregate(
    terminal_handoff: str,
    repo_root: Union[str, Path],
    handoffs_dir: Union[str, Path],
    archive_dir: Union[str, Path],
    thresholds: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Core aggregation — the full chain-walk + Session-Ledger-sum pipeline.

    Args:
        terminal_handoff: raw ``--terminal-handoff`` argument (as given by the
            caller — resolved via ``resolve_handoff_path``).
        repo_root: the calling repo's git root (for CHAIN_STARTING_HANDOFF's
            relative-path display and ``resolve_handoff_path`` tier 2).
        handoffs_dir: the resolved state/handoffs directory (STATE_ROOT/handoffs
            — may differ from repo_root/state/handoffs when the
            coordinator_state_root seam redirects to a central claude-klabauter state
            root; see ``resolve_state_root``).
        archive_dir: the resolved archive/handoffs directory — ALWAYS
            ``<repo_root>/archive/handoffs`` (mirrors the bash oracle's own
            GIT_ROOT-relative ARCHIVE_DIR, deliberately not STATE_ROOT-relative
            — see module docstring negative-spec).
        thresholds: t-shirt threshold rows (``loe_thresholds.load_thresholds``
            shape). Defaults to ``loe_thresholds.DEFAULT_THRESHOLDS`` when None.

    Returns a dict with keys: ``exit_code`` (0 success, 1 error), and on
    success: ``chain_total``, ``agent_dispatches``, ``opus_dispatches``,
    ``em_tokens`` (int or None), ``tshirt``, ``commits`` (list[str]),
    ``chain_sessions_with_ledger`` (str), ``chain_span_days`` (int or None),
    ``chain_starting_handoff`` (str or None), ``chain_walk_terminated_early``
    (str, ``''`` when clean). On error: ``error`` (str).
    """
    repo_root = Path(repo_root)
    handoffs_dir = Path(handoffs_dir)
    archive_dir = Path(archive_dir)

    terminal_abs = resolve_handoff_path(terminal_handoff, repo_root, handoffs_dir, archive_dir)
    if terminal_abs is None:
        return {"exit_code": 1, "error": f"terminal handoff not found: {terminal_handoff}"}

    # handoff_dir intentionally omitted (None) — dag.walk_forward infers it from
    # dirname(terminal_abs), exactly mirroring the bash oracle's `node
    # walk-handoff-dag.js --start <path>` invocation (no --handoff-dir flag).
    # See module docstring negative-spec.
    # Review: code-reviewer — _EDGE_KINDS is already a set literal;
    # walk_forward only reassigns edge_kinds when None (never mutates a
    # passed-in set), so the set(...) copy here was a no-op.
    walk = walk_forward(terminal_abs, edge_kinds=_EDGE_KINDS)
    chain_order: List[str] = walk["orderedPaths"]
    terminated_early: str = walk["terminatedEarly"]

    if not chain_order:
        return {"exit_code": 1, "error": "walk-handoff-dag returned empty output"}

    chain_total = len(chain_order)

    total_ad = 0
    total_od = 0
    total_tok: Optional[int] = None
    commits: List[str] = []
    seen_sids: set = set()
    handoffs_with_ledger = 0

    for hpath in chain_order:
        try:
            text = Path(hpath).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            # dag.walk_forward already resolved this path as part of the chain,
            # so a read failure here is a genuine anomaly (not "no ledger
            # present") — surface it rather than silently undercounting LoE.
            print(
                f"aggregate_chain_loe: failed to read {hpath}, excluding from "
                f"aggregation: {exc}",
                file=sys.stderr,
            )
            continue

        records = parse_session_ledgers(text)
        if records:
            handoffs_with_ledger += 1

        for rec in records:
            sid = rec["session_id"]
            if sid:
                if sid in seen_sids:
                    continue
                seen_sids.add(sid)

            total_ad += _to_int(rec["agent_dispatches"])
            total_od += _to_int(rec["opus_dispatches"])

            tok_raw = rec["em_tokens"]
            if tok_raw and tok_raw != "null" and _TOKEN_RE.match(tok_raw):
                clean = int(tok_raw.replace(",", "").replace("_", ""))
                total_tok = clean if total_tok is None else total_tok + clean

            commits_raw = rec["commits"]
            if commits_raw:
                for c in commits_raw.split(","):
                    c = c.strip()
                    if c and c not in commits:
                        commits.append(c)

    chain_sessions_with_ledger = f"{handoffs_with_ledger} of {chain_total}"

    # chain_span_days: first (root, chain_order[-1]) vs last (terminal, chain_order[0])
    # 'created' frontmatter field date diff, degrading to None on any parse failure.
    chain_span_days: Optional[int] = None
    first_handoff = chain_order[-1]
    last_handoff = chain_order[0]
    try:
        first_text = Path(first_handoff).read_text(encoding="utf-8", errors="replace")
        last_text = Path(last_handoff).read_text(encoding="utf-8", errors="replace")
        first_created = extract_frontmatter_field(first_text, "created")
        last_created = extract_frontmatter_field(last_text, "created")
        first_dt = _parse_date_prefix(first_created)
        last_dt = _parse_date_prefix(last_created)
        if first_dt is not None and last_dt is not None:
            # Review: code-reviewer — match the bash oracle's local-tz
            # epoch-second diff (date -d/-j -> epoch, // 86400) rather than
            # a naive calendar-day subtraction. time.mktime() interprets the
            # naive midnight datetime as local time (DST-aware, same as the
            # oracle's `date` invocation), so a chain spanning a DST
            # transition day truncates identically on both sides.
            diff_secs = int(time.mktime(last_dt.timetuple()) - time.mktime(first_dt.timetuple()))
            if diff_secs >= 0:
                chain_span_days = diff_secs // 86400
    except OSError:
        pass  # degrade to None on any parse failure, per the comment above

    # chain_starting_handoff: root path, relative to repo_root if possible
    root_abs = Path(chain_order[-1])
    try:
        chain_starting_handoff = rel_id(root_abs, repo_root)
    except ValueError:
        chain_starting_handoff = str(root_abs)

    tshirt = compute_tshirt(total_ad, total_od, total_tok, thresholds)

    return {
        "exit_code": 0,
        "chain_total": chain_total,
        "agent_dispatches": total_ad,
        "opus_dispatches": total_od,
        "em_tokens": total_tok,
        "tshirt": tshirt,
        "commits": commits,
        "chain_sessions_with_ledger": chain_sessions_with_ledger,
        "chain_span_days": chain_span_days,
        "chain_starting_handoff": chain_starting_handoff,
        "chain_walk_terminated_early": terminated_early,
    }


# ---------------------------------------------------------------------------
# Output formatters — byte-parity with bash :624-709
# ---------------------------------------------------------------------------


def format_yaml_frontmatter(result: Dict[str, Any]) -> str:
    """Render the aggregate() result as the yaml-frontmatter output (bash :625-658)."""
    em_tokens_out = str(result["em_tokens"]) if result["em_tokens"] is not None else "null"

    lines = [
        "chain_loe:",
        f"  sessions: {result['chain_total']}",
        f"  agent_dispatches: {result['agent_dispatches']}",
        f"  opus_dispatches: {result['opus_dispatches']}",
        f"  em_tokens: {em_tokens_out}",
        f'  tshirt: "{result["tshirt"]}"',
    ]

    commits = result["commits"]
    if commits:
        lines.append("commits:")
        for c in commits:
            lines.append(f'  - "{c}"')

    lines.append(f'chain_sessions_with_ledger: "{result["chain_sessions_with_ledger"]}"')

    if result["chain_span_days"] is not None:
        lines.append(f"chain_span_days: {result['chain_span_days']}")

    if result["chain_starting_handoff"]:
        lines.append(f'chain_starting_handoff: "{result["chain_starting_handoff"]}"')

    if result["chain_walk_terminated_early"]:
        lines.append(f'chain_walk_terminated_early: "{result["chain_walk_terminated_early"]}"')

    return "\n".join(lines) + "\n"


def format_json(result: Dict[str, Any]) -> str:
    """Render the aggregate() result as the json output (bash :660-703)."""
    import json

    tok_json: Any = result["em_tokens"] if result["em_tokens"] is not None else None

    obj: Dict[str, Any] = {
        "chain_loe": {
            "sessions": result["chain_total"],
            "agent_dispatches": result["agent_dispatches"],
            "opus_dispatches": result["opus_dispatches"],
            "em_tokens": tok_json,
            "tshirt": result["tshirt"],
        }
    }

    if result["commits"]:
        obj["commits"] = result["commits"]

    obj["chain_sessions_with_ledger"] = result["chain_sessions_with_ledger"]
    obj["chain_span_days"] = result["chain_span_days"]

    if result["chain_starting_handoff"]:
        obj["chain_starting_handoff"] = result["chain_starting_handoff"]

    if result["chain_walk_terminated_early"]:
        obj["chain_walk_terminated_early"] = result["chain_walk_terminated_early"]

    return json.dumps(obj) + "\n"


def resolve_thresholds(thresholds_path: Optional[Union[str, Path]]) -> List[Dict[str, Any]]:
    """Load thresholds from *thresholds_path*, falling back to DEFAULT_THRESHOLDS.

    Mirrors the recipe's parity-plus improvement over the bash oracle's inline
    ``TSHIRT_TABLE`` copy — loads the real ``config/loe-thresholds.yaml`` when a
    path is supplied and readable, eliminating the sync-drift risk the bash
    inline copy carried. Any load failure (missing file, malformed YAML,
    missing key) degrades silently to ``DEFAULT_THRESHOLDS`` — never fatal, this
    is a cold ceremony-only report generator, not a correctness-critical write.
    """
    if not thresholds_path:
        return DEFAULT_THRESHOLDS
    try:
        return load_thresholds(thresholds_path)
    except (OSError, KeyError, ValueError):
        return DEFAULT_THRESHOLDS
    except Exception:  # noqa: BLE001 — yaml.YAMLError and friends
        return DEFAULT_THRESHOLDS


# ---------------------------------------------------------------------------
# CLI entry point — consumed in-process by the example-doctrine-repo-side CLI trampoline
# (coordinator/bin/aggregate-chain-loe.py). Byte-parity with the retired
# bash oracle's own arg-parsing / help text / exit-code convention
# (example-doctrine-repo b644d5a9, 2026-07-22).
# ---------------------------------------------------------------------------

_HELP_TEXT = """Usage: aggregate-chain-loe.sh --terminal-handoff <path> [OPTIONS]

Options:
  --terminal-handoff <path>        Path to the handoff being consumed by the
                                   chain-terminal session (the immediate predecessor).
                                   Absolute or relative to cwd. Required.
  --format <yaml-frontmatter|json> Output format (default: yaml-frontmatter)
  -h, --help                       Show this help

Output yaml-frontmatter example:
  chain_loe:
    sessions: 6
    agent_dispatches: 87
    opus_dispatches: 12
    em_tokens: 1847000
    tshirt: "XL"
  commits:
    - "abc1234"
  chain_sessions_with_ledger: "6 of 6"
  chain_span_days: 14
  chain_starting_handoff: "state/handoffs/2026-05-05_141200_chain-root.md"

Output json example:
  {"chain_loe": {"sessions": 6, "agent_dispatches": 87, ...}, "commits": ["abc1234"], ...}
(Review: code-reviewer F5 — updated from stale loe:/chain_sessions: shape to match actual chain_loe: output)

Termination signals (recorded as chain_walk_terminated_early):
  missing-link   — one or more edge targets could not be resolved; walk continues on other edges
  lineage-cycle  — a genuine back-edge (authoring error) was detected; benign diamonds are NOT flagged

Exit codes:
  0 — success (possibly with partial aggregate if walk terminated early)
  1 — fatal argument or environment error
"""


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point — mirrors the bash oracle's arg-parse/resolve/aggregate/emit pipeline.

    Negative-spec (preserve exactly, do not "fix"):
      - ``--format`` is validated at OUTPUT time, after the chain walk has
        already run (mirrors the bash oracle's trailing ``case "$FORMAT" in``
        dispatch at :624-709 — an invalid ``--format`` still pays for a full
        chain walk before erroring). Harmless (read-only, no side effects) but
        deliberately reproduced rather than fast-failed.
      - Unknown/missing-value flags error immediately with the bash oracle's
        exact message text and exit 1 (bash :98-106).
    """
    if argv is None:
        argv = sys.argv[1:]
    args = list(argv)

    terminal_handoff = ""
    fmt = "yaml-frontmatter"
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--terminal-handoff":
            if i + 1 >= len(args):
                print("Error: --terminal-handoff is required", file=sys.stderr)
                return 1
            terminal_handoff = args[i + 1]
            i += 2
            continue
        if a == "--format":
            if i + 1 >= len(args):
                print("Error: --format is required", file=sys.stderr)
                return 1
            fmt = args[i + 1]
            i += 2
            continue
        if a in ("-h", "--help"):
            sys.stdout.write(_HELP_TEXT)
            return 0
        print(f"Error: unknown argument: {a}", file=sys.stderr)
        return 1

    if not terminal_handoff:
        print("Error: --terminal-handoff is required", file=sys.stderr)
        return 1

    try:
        git_root = resolve_repo_root()
    except ValueError:
        print("Error: not inside a git repo", file=sys.stderr)
        return 1

    coordinator_root_env = os.environ.get("COORDINATOR_CONTENT_ROOT")
    coordinator_root = Path(coordinator_root_env) if coordinator_root_env else git_root / "coordinator"

    state_root = resolve_state_root(coordinator_root, Path.cwd())
    handoffs_dir = state_root / "handoffs"
    archive_dir = git_root / "archive" / "handoffs"

    thresholds_path = coordinator_root / "config" / "loe-thresholds.yaml"
    thresholds = resolve_thresholds(thresholds_path if thresholds_path.exists() else None)

    result = aggregate(
        terminal_handoff=terminal_handoff,
        repo_root=git_root,
        handoffs_dir=handoffs_dir,
        archive_dir=archive_dir,
        thresholds=thresholds,
    )
    if result["exit_code"] != 0:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1

    if fmt == "yaml-frontmatter":
        sys.stdout.write(format_yaml_frontmatter(result))
    elif fmt == "json":
        sys.stdout.write(format_json(result))
    else:
        print(f"Error: unknown format '{fmt}'. Use: yaml-frontmatter | json", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# JSON-RPC handler (future daemon-RPC callers; NOT the CLI trampoline's path —
# see module docstring). Central-registry wiring (ops/__init__.py,
# ops/_registry_map.py, ipc.py::_OP_KEY_SCOPE, authz/classification.py) is
# deferred to the EM per the build-wave's shared-tree concurrency-safety rule.
# ---------------------------------------------------------------------------


@register_op("session_ledger.aggregate_chain_loe")
async def _session_ledger_aggregate_chain_loe(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'session_ledger.aggregate_chain_loe' handler.

    COMPUTE_ONLY (read-only chain walk + report; writes nothing).

    Required params:
        terminal_handoff (str) — the --terminal-handoff argument.

    Optional params:
        format          (str)  — "yaml-frontmatter" (default) or "json".
        handoffs_dir    (str)  — absolute path override for STATE_ROOT/handoffs
                                  (defaults to <worktree_root>/state/handoffs when
                                  absent — the daemon-RPC path does not have a
                                  live coordinator_state_root shell seam to
                                  consult, unlike the CLI trampoline).
        thresholds_path (str)  — absolute path to a loe-thresholds.yaml to load;
                                  defaults to DEFAULT_THRESHOLDS when absent.

    repo_root is the per-request resolved repo root (injected by
    ipc.dispatch_message from the _origin_worktree envelope field, common_dir
    scope — see this chunk's central-reg fragment for the _OP_KEY_SCOPE entry).
    From this, worktree_root = repo_root.parent (main_worktree_root convention)
    is used as both the git-root and the archive/handoffs base; handoffs_dir
    defaults to worktree_root/state/handoffs (the daemon-RPC path has no
    per-repo coordinator_state_root shell-seam to consult — callers needing
    central-state redirection should pass handoffs_dir explicitly).
    """
    if repo_root is None:
        return {"exit_code": 1, "error": "session_ledger.aggregate_chain_loe requires a per-repo dispatch key (_origin_worktree); repo_root is None."}

    from coordinator_core.ops.fleet._common import main_worktree_root

    worktree_root = main_worktree_root(Path(repo_root))

    terminal_handoff = params.get("terminal_handoff") or ""
    if not terminal_handoff:
        return {"exit_code": 1, "error": "missing required param: terminal_handoff"}

    fmt = params.get("format") or "yaml-frontmatter"
    if fmt not in ("yaml-frontmatter", "json"):
        return {"exit_code": 1, "error": f"unknown format '{fmt}'. Use: yaml-frontmatter | json"}

    handoffs_dir_raw = params.get("handoffs_dir") or ""
    handoffs_dir = Path(handoffs_dir_raw) if handoffs_dir_raw else worktree_root / "state" / "handoffs"
    archive_dir = worktree_root / "archive" / "handoffs"

    thresholds = resolve_thresholds(params.get("thresholds_path"))

    result = aggregate(
        terminal_handoff=terminal_handoff,
        repo_root=worktree_root,
        handoffs_dir=handoffs_dir,
        archive_dir=archive_dir,
        thresholds=thresholds,
    )
    if result["exit_code"] != 0:
        return result

    output = format_yaml_frontmatter(result) if fmt == "yaml-frontmatter" else format_json(result)
    result["output"] = output
    return result
