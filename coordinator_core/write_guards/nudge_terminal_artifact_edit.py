"""coordinator_core.write_guards.nudge_terminal_artifact_edit — advisory guard.

Discharges: the defect surfaced live 2026-07-28 by commit 257448d7 (reverted
by c7b2484a) — a forward-binding instruction (an Anti-scope clause meant to
constrain FUTURE work) was written into a plan whose frontmatter ``status:``
was already terminal (``implemented``). The pickup-guidance prose that
permitted the write was corrected at 8a0bcafd, but prose alone does not
discharge the rule: nobody re-reads a delivered plan's own body before
building the thing it constrains, which is exactly why the instruction went
unread until it was too late. This guard adds tool-boundary friction the
moment such a write is attempted, per the discharge test in this repo's own
CLAUDE.md (§ North star): "for every rule, what artifact discharges it? 'The
operator remembers' is not an answer."

This is a NEW guard, not a port of a example-doctrine-repo reference ``.sh`` hook — there is no
``nudge-terminal-artifact-edit.sh`` in the retired bash-hook corpus.

CLASS is "advisory", deliberately — never a hard-deny. Recording
correspondence/history against a delivered plan (a status update, a note
pointing at the successor artifact, a post-mortem addendum) is legitimate
and common; a hard block would punish that legitimate case identically to
the forward-binding-instruction case this guard actually targets. Per
design-as-offers (global CLAUDE.md § Implementation Standards): the message
leads with the better alternative, not the violation, and the write always
proceeds — this guard only adds ``additionalContext``, never
``permissionDecision``.

PRIORITY = 150, in the ADVISORY PHASE's own PRIORITY namespace — see
docs/wiki/write-guard-priority-bands.md, the SSOT for this convention (this
docstring previously mis-stated it three ways, corrected there: it called
110+ an advisory-only band when ten hard-denies live at 119-132 in the
hard-deny phase's own, independent namespace, and it omitted 76, 105, and
111 from the advisory occupancy list it named). `engine.py::evaluate` runs
a hard-deny phase to completion before the advisory phase starts at all —
the two phases never compete for the same slot, so a hard-deny at, say,
120 and an advisory at 120 are not a collision. Within the advisory phase
alone, the occupied slots below 150 at time of writing are 76, 100, 105,
110, 111, 130, 140, 140 (a since-fixed collision — see the wiki page) —
110 (``nudge_windows_subprocess_popup``), 130 (``nudge_baton_body_bar``),
140 (``nudge_tasks_state_folder_split``) among them. 150 continues that
same advisory band one slot past the highest advisory PRIORITY in use at
the time this guard was authored; there is no ordering dependency between
this guard and any advisory below it (each fires on a disjoint condition —
subprocess popups, queue writes, baton-body edits, tasks/-placement — so
relative order among them is a no-op in practice), so "next available slot
in the band" is the whole of the justification. See the wiki page's
same-surface collision rule before assuming any future "next available
slot" pick is automatically safe — a slot must sort ahead of every
existing advisory that could plausibly co-match the same write, not merely
be numerically free.

DETECTION: two conditions, both required.
  1. The target path matches ``docs/plans/**``, ``docs/problems/**``, or
     ``docs/research/**`` (verified against this repo's actual tree before
     hardcoding — all three exist and are the session-authored working-doc
     dirs named in this repo's own CLAUDE.md § Architecture) with a ``.md``
     leaf.
  2. The file ALREADY EXISTS on disk (a brand-new file is never a terminal
     artifact — there is nothing to be terminal yet) and it is terminal by a
     COMPOSITE signal, EITHER leg sufficient:
       a. frontmatter ``status:`` is one of the terminal set: ``implemented``,
          ``shipped``, ``superseded`` (this repo's plans corpus also uses
          ``complete``/``approved``/``ratified`` for other lifecycle points,
          but those are not the terminal-and-delivered set this leg targets
          — narrowing to exactly the named three avoids silently widening
          this leg's own remit beyond its original spec); OR
       b. the plan body has at least one ``| ACn | ... |`` row (this repo's
          AC-table convention) and EVERY such row carries a bolded
          ``**met**`` marker (case-insensitive) — i.e. every acceptance
          criterion is discharged regardless of what the status field says.

     Leg (b) was added 2026-08-04 after a real incident:
     ``docs/plans/2026-08-03-scope-guard-peer-claim-release.md`` was fully
     delivered — all 14 ACs ``**met**`` — but read ``status: approved``,
     because close-out-and-stamp could not join one chunk's evidence (a
     deliverable-id fork, fixed separately the same day in ``97c210fb9377``).
     So a delivered plan sat at a pre-terminal status indefinitely, and this
     guard's original status-only DETECTION stayed silent while an EM
     annotated it with a forward-binding instruction — exactly the incident
     this guard exists to catch. The lesson: a status field is a lagging,
     failure-prone liveness signal, and any guard leaning on it ALONE
     inherits every close-out defect downstream. This module's own DETECTION
     used to argue explicitly for narrowing to the three named status values
     and stopping there ("this list is not widened... [to] every
     terminal-flavored word"); that argument covered only the status field
     itself and is superseded by leg (b), which does not widen the status
     set at all but adds an independent, content-derived terminal signal
     next to it.

Frontmatter is read via the shared parser
(``coordinator_core.frontmatter.primitives.split_frontmatter`` +
``read_fm_field_unquoted`` + ``frontmatter_body_text``) — the same
primitives module every other frontmatter-reading caller in this package
uses (``archive_stamp``, ``coverage``, ``distill/_common``,
``distill/delete_guard``, ...). This guard does NOT parse frontmatter by
hand a second time.

CONTENT DISCRIMINATION (2026-08-04, PM ruling): the guard now inspects the
edited text before firing, distinguishing two legitimate cases:
  - CORRESPONDENCE — a dated supersession note, "refuted by X", a pointer to
    a successor artifact, historical annotation — is legitimate and common
    on a delivered plan (see CLASS rationale above) and must NOT nag.
  - INSTRUCTION — forward-binding content: must/should/do not/going
    forward/before building/the next session, a brand-new AC row (new scope
    by construction), prescriptive next steps — is what the offer targets.

Classification is a single POSITIVE instruction-tell regex
(``_INSTRUCTION_RE``), no suppressor branch — mirrors
``nudge_unrouted_sizing``'s documented text-classification design choice: a
message with no instruction tell is already excluded because the gate never
opens for it (a stronger discharge than a correctly-ordered suppressor would
be), and a message that happens to also read as correspondence but STILL
carries a "must"/"next session" clause is exactly the mixed case that must
still fire.
  - Edit: the tell is checked against ``new_string`` alone (the FULL
    precision case — the payload's own delta, never pre-existing body
    text).
  - Write / MultiEdit: no precise delta is available at this transport's
    payload shape (``new_string``/``edits[]`` are not reliably present for
    these tools), so the check falls back to scanning the plan's whole
    post-edit BODY text (frontmatter stripped). This is a NAMED, coarser
    check — see "Honest limitation" below.

Honest limitation (named, not papered over): the Write/MultiEdit fallback
classifies the whole body, not the delta, so (a) it can fire on an edit that
added nothing instruction-shaped, if the file already carried "must"/
"should" language elsewhere (false positive — cost is one line of advisory
text, acceptable per design-as-offers); and (b) more importantly, an
instruction added via Write/MultiEdit phrased WITHOUT any of the listed tell
words (e.g. imperative mood with no modal — "Rework the retry loop before
v2" has no "must"/"should"/"next session" token) slips past the regex
entirely — this is the single largest false-negative surface. The Edit path
(new_string-scoped) shares that same word-list limitation but not the
whole-body-vs-delta one.

Live-alternative candidate scan: when the guard fires, the offer names REAL
on-disk candidates (an open handoff, a sizing object sharing topic tokens
with the plan) rather than generic categories, per this repo's own
discharge test in CLAUDE.md ("the operator remembers" is not an answer). The
scan is bounded and string-prefiltered before any I/O:
``os.scandir(state/handoffs/)`` and ``os.scandir(state/sizings/)``, each
capped at ``_SCAN_CAP`` entries, non-recursive; a STRING-ONLY filename-token
overlap prefilter runs before the one disk read per matching candidate (to
confirm ``status: open`` for a handoff, or a NON-terminal status —
``_SIZING_TERMINAL_STATUSES``, added 2026-08-10 alongside the sizing-object
``declined`` value — for a sizing). This keeps the write
hot path's per-invocation cost to: one bounded regex on the path, one disk
read of the (already-small) target plan, two compiled-regex passes over
text already in memory, and at most ``_SCAN_CAP`` filename comparisons per
scanned directory plus a handful of confirming reads — no directory walk
outside these two capped scans, no git command, no subprocess.

Negative-spec:
  - Does NOT block/deny anything — CLASS is "advisory"; the envelope carries
    only ``additionalContext``, never ``permissionDecision``. See CLASS
    rationale above.
  - Does NOT resolve a git root or spawn any subprocess to locate the repo
    root or read the target file — the target-path GATE is a pure string
    match on the tool payload's own path, and the on-disk read resolves the
    candidate against ``payload['cwd']`` (falling back to ``Path.cwd()``)
    with ``pathlib`` alone, never a shell-out or ``os.path`` string-split.
    Windows and POSIX separators are both normalized before the path-shape
    match; the disk read itself uses ``pathlib.Path`` throughout, which
    accepts forward slashes on both platforms. The live-alternative scan is
    likewise ``os.scandir``/``pathlib`` only, never ``git``.
  - Does NOT fire when the file does not yet exist on disk, has no
    parseable frontmatter, or is not terminal by EITHER composite leg — in
    each case there is no terminal state to detect, and the guard returns
    ``None`` (a brand-new plan being drafted is never "already delivered").
  - Does NOT fire on a non-terminal ``status:`` with an undischarged or
    absent AC table (``draft``, ``executing``, ``ready_to_fire``, ``open``,
    ...) — see DETECTION above.
  - Does NOT fire on content that reads as correspondence rather than an
    instruction, even on a terminal plan — see CONTENT DISCRIMINATION above;
    this is the fix for the pre-2026-08-04 behaviour, which nagged on ANY
    edit to a terminal artifact regardless of content.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op),
    matching every other write_guards module's fail-open contract.

Spec backlink: commit 257448d7 (the incident), c7b2484a (the revert),
  8a0bcafd (the prose-level pickup-guidance correction this guard mechanizes);
  docs/plans/2026-08-03-scope-guard-peer-claim-release.md (the 2026-08-04
  status-vs-AC-table incident) and commit 97c210fb9377 (the deliverable-id
  fork fix that let close-out-and-stamp see the AC table was fully
  discharged).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.frontmatter.primitives import (
    frontmatter_body_text,
    read_fm_field_unquoted,
    split_frontmatter,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 150  # advisory-phase band; next slot after 110/130/140 (see module docstring
# and docs/wiki/write-guard-priority-bands.md -- 120 is a hard-deny-phase slot, a
# different namespace, not part of this band)

#: The three terminal-and-delivered status values this guard's status leg
#: targets (see module docstring DETECTION leg (a) on why this set is not
#: widened further).
_TERMINAL_STATUSES = frozenset({"implemented", "shipped", "superseded"})

#: `| ACn | ... | ... |` row, as used across docs/plans/*.md. The "open"
#: marker elsewhere in this corpus is `☐` (BALLOT BOX); the "discharged"
#: marker on the real fixture that motivated leg (b) is a bolded `**met**`
#: cell.
_AC_ROW_RE = re.compile(r"^\|\s*AC\d+\s*\|.*\|\s*$", re.MULTILINE)
_AC_MET_RE = re.compile(r"\*\*met\*\*", re.IGNORECASE)

#: Plan/spec-class working-doc dirs, verified present in this repo's own
#: tree before hardcoding (docs/plans, docs/problems, docs/research — all
#: three named in this repo's CLAUDE.md § Architecture). Anchored so a
#: coincidental substring (e.g. ``vendor/docs/plans/x.md`` under a nested
#: repo) still matches — the guard is advisory, not a security boundary, so
#: a loose anchor erring toward "offer more often" is the safer direction
#: here (unlike the hard-deny guards' stricter containment checks).
_PLAN_SPEC_PATH_RE = re.compile(r"(^|/)docs/(plans|problems|research)/[^/]+\.md$")

# ---------------------------------------------------------------------------
# Content-shape classification -- single positive gate, no suppressor branch.
# See module docstring CONTENT DISCRIMINATION (mirrors
# nudge_unrouted_sizing's documented text-classification design).
# ---------------------------------------------------------------------------

_INSTRUCTION_RE = re.compile(
    r"\bmust\b"
    r"|\bshould\b"
    r"|\bdo not\b"
    r"|\bdon't\b"
    r"|\bgoing forward\b"
    r"|\bbefore building\b"
    r"|\bthe next session\b"
    r"|\bnext session\b"
    r"|\bnext step(s)?\b"
    r"|\brequires?\b"
    r"|\bneeds? to\b"
    r"|\|\s*AC\d+\s*\|",  # a brand-new AC row is new scope, forward-binding by construction
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Live-alternative candidate scan -- bounded, no git, string-prefilter before I/O.
# ---------------------------------------------------------------------------

_SCAN_CAP = 300

_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "into", "over",
    "are", "was", "were", "has", "have", "not", "but", "its", "of", "to",
    "in", "on", "a", "an", "is", "it", "one", "own",
})

_HANDOFF_STATUS_RE = re.compile(r'^status:\s*"?([A-Za-z_]+)"?\s*$', re.MULTILINE)

#: A sizing-object is whole-document YAML (no `---` frontmatter fence), so its
#: own `status:` line sits at the document's own top level rather than inside
#: a fenced block. NOT an alias of `_HANDOFF_STATUS_RE` (was, until
#: 2026-08-10) -- this repo's house style for `state/sizings/*.yaml` puts a
#: trailing `# draft | sized | routed | superseded | ...` enum-listing
#: comment directly on the `status:` line (verified: every sampled sizing
#: file carries one; zero sampled `state/handoffs/*.md` files do, so the two
#: corpora are NOT unified here even though they were before -- confirmed by
#: re-checking both directories rather than assumed). The shared regex's `$`
#: anchored right after the (optionally quoted) value, so it silently failed
#: to match ANY commented status line -- `m` came back `None`, the exclusion
#: guard (`if m and ...`) was skipped, and a terminal (declined/superseded/
#: shipped) sizing was offered as a live alternative anyway. Fail-open,
#: confirmed reproduced against this repo's actual files:
#: `status: routed  # draft | sized | routed | superseded` and
#: `status: declined  # draft | sized | routed | superseded | shipped | declined`
#: both failed to match the old pattern. This pattern tolerates an optional
#: trailing `#`-comment (and trailing whitespace before it) while still
#: requiring the value itself to be a bare word -- a quoted value containing
#: a literal `#`, or a fully commented-out `# status: x` line (no leading
#: `status:` at column 0), still does not match.
_SIZING_STATUS_RE = re.compile(
    r'^status:\s*"?([A-Za-z_]+)"?\s*(?:#.*)?$', re.MULTILINE
)

#: Terminal sizing statuses (`coordinator_core.frontmatter.schemas.
#: sizing-object.schema.json`'s own `status.enum` terminal members, own local
#: copy per this package's per-module convention -- see e.g.
#: `deliverable_cascade._SIZING_TERMINAL_STATUS`) -- a sizing-object in one of
#: these states is DONE, not a live alternative an EM should be redirected
#: into. Added 2026-08-10 alongside `declined`
#: (docs/plans/2026-08-10-a-terminal-status-for-a-declined-sizing.md § C2):
#: before this, `_sizing_candidates` offered ANY topically-overlapping sizing
#: file regardless of status -- a declined (or already-superseded/shipped)
#: sizing could be handed back as if it were still live, exactly the
#: "phantom live item" failure class this plan closes for a status QUERY;
#: this guard's own live-alternative offer is a second surface of the same
#: failure, so it is fixed here too rather than left to keep offering a dead
#: end.
_SIZING_TERMINAL_STATUSES = frozenset({"superseded", "shipped", "declined"})


def _slug_tokens(stem: str) -> set:
    return {
        t for t in re.split(r"[^a-z0-9]+", stem.lower())
        if len(t) > 2 and t not in _STOPWORDS
    }


def _handoff_candidates(repo_root: Path, plan_tokens: set, limit: int = 1) -> list:
    """Return up to `limit` `state/handoffs/*.md` paths sharing topic tokens with the
    plan AND carrying `status: open` -- confirmed via a bounded, prefiltered disk read
    (see module docstring "Live-alternative candidate scan").
    """
    handoffs_dir = repo_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return []
    scored = []
    try:
        entries = sorted(os.scandir(handoffs_dir), key=lambda e: e.name)
    except OSError:
        return []
    for i, entry in enumerate(entries):
        if i >= _SCAN_CAP:
            break
        if not entry.name.endswith(".md"):
            continue
        overlap = plan_tokens & _slug_tokens(Path(entry.name).stem)
        if not overlap:
            continue  # string-only prefilter -- no I/O for a non-matching filename
        try:
            with open(entry.path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        m = _HANDOFF_STATUS_RE.search(head)
        if not m or m.group(1).strip().lower() != "open":
            continue
        scored.append((len(overlap), f"state/handoffs/{entry.name}"))
    scored.sort(key=lambda t: -t[0])
    return [path for _, path in scored[:limit]]


def _sizing_candidates(repo_root: Path, plan_tokens: set, limit: int = 1) -> list:
    """Return up to `limit` `state/sizings/*.yaml` paths sharing topic tokens with the
    plan AND NOT in a terminal status (`_SIZING_TERMINAL_STATUSES`) -- filename-token
    prefilter first (no I/O for a non-matching filename), then a bounded confirming
    disk read per matching candidate, mirroring `_handoff_candidates`'s own
    prefilter-then-confirm shape (that function confirms `status: open`; this one
    confirms the sizing is NOT terminal).

    A sizing whose `status:` line is absent or unreadable (`OSError` on open, or no
    `status:` match at all) is treated as a candidate (fail-open, matching this
    guard's advisory posture elsewhere) rather than excluded on an ambiguous read --
    the confirming read here is a precision improvement over the pre-2026-08-10
    filename-only match, not a new gate that can itself go silent. This is a
    DELIBERATE fail-open, not the same shape as the `_SIZING_STATUS_RE` trailing-
    comment gap fixed the same day: that one was an accidental parse failure on a
    common, well-formed line (the regex silently not matching text it should have
    matched); this one is "the file could not be read/parsed at all" (locked,
    permission-denied, or genuinely absent status field), where offering it as a
    candidate rather than blocking the write is the correct advisory-posture call --
    see CLASS rationale in the module docstring.
    """
    sizings_dir = repo_root / "state" / "sizings"
    if not sizings_dir.is_dir():
        return []
    scored = []
    try:
        entries = sorted(os.scandir(sizings_dir), key=lambda e: e.name)
    except OSError:
        return []
    for i, entry in enumerate(entries):
        if i >= _SCAN_CAP:
            break
        if not (entry.name.endswith(".yaml") or entry.name.endswith(".yml")):
            continue
        overlap = plan_tokens & _slug_tokens(Path(entry.name).stem)
        if not overlap:
            continue  # string-only prefilter -- no I/O for a non-matching filename
        try:
            with open(entry.path, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            scored.append((len(overlap), f"state/sizings/{entry.name}"))
            continue
        m = _SIZING_STATUS_RE.search(head)
        if m and m.group(1).strip().lower() in _SIZING_TERMINAL_STATUSES:
            continue  # a terminal sizing is not a live alternative to offer
        scored.append((len(overlap), f"state/sizings/{entry.name}"))
    scored.sort(key=lambda t: -t[0])
    return [path for _, path in scored[:limit]]


def _find_candidates(repo_root: Path, plan_rel_path: str) -> list:
    """Return concrete, on-disk live-alternative lines for the offer message.

    Falls back to naming the categories themselves (still concrete paths, per the
    discharge test in this repo's CLAUDE.md -- "the operator remembers" is not an
    answer) when no topically-overlapping candidate is found.
    """
    plan_tokens = _slug_tokens(Path(plan_rel_path).stem)
    lines = []
    for path in _handoff_candidates(repo_root, plan_tokens):
        lines.append(f"  - {path} (open handoff, same topic)")
    for path in _sizing_candidates(repo_root, plan_tokens):
        lines.append(f"  - {path} (sizing object, same topic)")
    if not lines:
        lines = [
            "  - state/handoffs/<topic>.md (no matching open handoff found -- /handoff to start one)",
            "  - state/sizings/<topic>.yaml (no matching sizing object found -- coordinator:sizing to start one)",
            "  - docs/decisions/ (a standalone decision record)",
        ]
    return lines


# ---------------------------------------------------------------------------
# Terminal-status resolution -- composite signal (status leg OR AC-table leg).
# ---------------------------------------------------------------------------


def _terminal_status_desc(text: str) -> Optional[str]:
    """Return a short status-description string iff `text` (a full target file's
    content) reads as terminal by the composite signal, else None.

    Composite: `status:` in `_TERMINAL_STATUSES`, OR an AC table whose every row
    carries `**met**`. Either alone is sufficient -- see module docstring
    DETECTION leg (b) for the incident this fixes.
    """
    split = split_frontmatter(text)
    status = None
    if split is not None:
        status = read_fm_field_unquoted(split.fm_text, "status")

    if status and status.strip().lower() in _TERMINAL_STATUSES:
        return status.strip()

    body = frontmatter_body_text(text) if split is not None else text
    rows = _AC_ROW_RE.findall(body)
    if rows and all(_AC_MET_RE.search(row) for row in rows):
        return f"{status or 'status: (non-terminal)'}, but every AC row is **met**"

    return None


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize(path: str) -> str:
    normalized = path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _resolve_disk_path(file_path_norm: str, cwd: Optional[str]) -> Path:
    candidate = Path(file_path_norm)
    if candidate.is_absolute():
        return candidate
    base = Path(cwd) if cwd else Path.cwd()
    return base / candidate


def _repo_root(cwd: Optional[str]) -> Path:
    """Best-effort repo root -- `cwd` if it (or an ancestor, bounded) holds `.git`,
    else `cwd` itself. No subprocess, no `git` invocation.
    """
    base = Path(cwd) if cwd else Path.cwd()
    probe = base
    for _ in range(8):
        if (probe / ".git").exists():
            return probe
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return base


def _edit_text_for_classification(tool_name: str, payload: Dict[str, Any], disk_text: str) -> str:
    """Return the text to run the instruction-tell regex against.

    Edit -> `new_string` alone (the precise delta, see module docstring
    CONTENT DISCRIMINATION). Write / MultiEdit -> the whole post-edit body
    (the coarser fallback -- see "Honest limitation" in the module
    docstring).
    """
    if tool_name == "Edit":
        tool_input = payload.get("tool_input") or {}
        if isinstance(tool_input, dict):
            new_string = tool_input.get("new_string")
            if new_string:
                return new_string
    return disk_text


_REASON_TEMPLATE = """OFFER: {file_path_norm} reads as delivered ({status_desc}) -- this edit reads as an INSTRUCTION (forward-binding), not correspondence. Nobody is coming back to read a "must"/"next session" clause in a delivered plan (happened: commit 257448d7; also docs/plans/2026-08-03-scope-guard-peer-claim-release.md).

Use instead:
{alternatives}

If this write is CORRESPONDENCE (a dated note, "refuted by X", a status/history addendum) rather than an instruction, ignore this -- nothing is blocked and the write already happened."""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None
        file_path_norm = _normalize(file_path)

        if not _PLAN_SPEC_PATH_RE.search(file_path_norm):
            return None

        disk_path = _resolve_disk_path(file_path_norm, payload.get("cwd"))
        if not disk_path.is_file():
            # A brand-new file is never a terminal artifact -- nothing to
            # be terminal yet.
            return None

        try:
            text = disk_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

        status_desc = _terminal_status_desc(text)
        if status_desc is None:
            return None

        classify_text = _edit_text_for_classification(tool_name, payload, text)
        if not _INSTRUCTION_RE.search(classify_text):
            return None

        repo_root = _repo_root(payload.get("cwd"))
        alternatives = "\n".join(_find_candidates(repo_root, file_path_norm))

        reason = _REASON_TEMPLATE.format(
            file_path_norm=file_path_norm,
            status_desc=status_desc,
            alternatives=alternatives,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard offers only on a
        # positive terminal-status match, never on an error.
        return None
