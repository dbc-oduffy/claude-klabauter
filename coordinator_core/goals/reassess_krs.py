"""
coordinator_core.goals.reassess_krs — weekly KR re-assessment for per-repo goal artifacts.

Purpose: byte-parity port of coordinator/bin/reassess-goal-krs.sh (386 LoC bash) — for
each state/goals/*.yaml whole-document goal artifact (schema: goal, C1 2026-07-13 shape,
no '---' frontmatter fence), parse the key_results[] list block and correlate each KR's
extracted keywords against existing weekly signal (completion log, handoff records, the
week-changelog HEADER.md). Proposes a per-KR status and a perceptible_movement flag; KRs
with no movement AND weekly_perceptible:true are flagged
"*** maybe-not-a-goal — no perceptible movement this week".

READS existing signal only — does NOT build new instrumentation:
  - <bin_dir>/query-completions.sh --since <since> --format json   (bash sibling, T3a/T5)
  - ``coordinator_core.ops.ceremony.records_query.query_records("handoff", repo_root,
    since=since)`` — in-process native records seam (2026-07-21 repoint; formerly a
    ``query-records.js --type handoff --since <since> --format json`` node spawn). The
    JSON blob assembled from the native result (``json.dumps(records, indent=2)``)
    reproduces ``query-records.js``'s ``--format json`` shape exactly — a bare array of
    ``{path, frontmatter}`` records, no envelope (query-records.js:1601-1604
    ``formatRecords()`` / ``JSON.stringify(records, null, 2)``). ``repo_root`` is passed
    as ``worktree_root`` because the retired spawn inherited this process's cwd with no
    ``--root``, so the node sibling's own root detection (query-records.js:586-593
    ``detectRoot()`` — ``git rev-parse --show-toplevel`` from cwd, falling back to
    ``process.cwd()``) resolved to the same directory ``repo_root`` already names for
    the week-changelog read below.
  - <goals_dir's repo>/state/week-changelog/HEADER.md (best-effort)

query-completions.sh remains a subprocess shim to a still-non-Python sibling — an
INTERIM measure per the T4a-g3 recipe § 7 cross-tranche sequencing note (it becomes
native transitively once its own sibling module is repointed). The query-records.js
signal source above no longer shells out — see the negative-spec note below.

OUTPUT: a per-goal proposed-status report (string, mirrors the bash script's stdout)
plus (non-dry-run only) a rewritten proposed-re-assessment comment block appended to
each active goal artifact with movement KRs. Does NOT overwrite the live `status` field.

KR-suggestion source (DR-130, example-doctrine-repo:coordinator/schemas/kr-suggestion.schema.json):
a fourth, fully optional signal — any producer resident in this repo may drop a
``state/kr-suggestions/<date>-<slug>.yaml`` record ahead of a weekly re-assessment run.
Unlike the three sources ``_gather_signal`` folds into ``all_signal_text`` for keyword
matching, these are structured per-goal/per-KR records, so they are read separately
(``_gather_kr_suggestions``) and resolved inside ``reassess()``'s own goal loop against
each goal's parsed `id:` and `key_results[].id` — never through the flat-text channel,
which would destroy the structure. Presented ALONGSIDE the computed proposal for the
same KR, never in place of it; see the negative-spec entries below for the invariants
this must never violate.

Self-registration: importing this module fires ``@register_op("goals.reassess_krs", ...)``
as a side-effect (see bottom of file). goals_dir/bin_dir are explicit caller-supplied
absolute paths (mirrors cartography.*/percolate.* "none"-scope ops — no repo_root-derived
state access; the trampoline resolves both paths itself, exactly as the original bash
script derived SCRIPT_DIR/REPO_ROOT from its own BASH_SOURCE location).

Spec backlink: docs/plans/2026-07-06-goal-setting-okr-legibility-system.md § C6
Port of: coordinator/bin/reassess-goal-krs.sh (example-doctrine-repo)
Recipe: scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 1 (example-doctrine-repo)

Negative-spec (hard-won, preserve exactly):
  - Does NOT overwrite the goal artifact's live `status:` field — writes only a
    "# --- KR Re-assessment (proposed ...) ---" comment block.
  - The proposed-block anchor uses `since=` (the query window), NOT today's date —
    an explicit F5 bash-version fix avoiding diff churn on identical re-runs. The
    Python port preserves this anchor choice.
  - Whole-document YAML has no closing '---' fence (C1) — the proposed block is
    ALWAYS appended at end-of-file, never inserted mid-document.
  - `--dry-run` is report-only: zero file writes, identical stdout report.
  - Missing query-completions.sh, or a failure raised by the native records-query
    call (unknown type / unparseable ``since`` / directory-scan failure), degrades to
    a warning, never a fatal error (unlike the bash version's hard node-availability
    gate, which this port intentionally drops per the recipe's cross-tranche note).
    ``query_records()`` itself is in-process, so "not found on disk" no longer
    applies to the handoff signal source — only the exception classes it can raise
    (``ValueError``, ``SystemExit``) or an empty ``repo_root`` are handled.
  - An absent/empty `state/kr-suggestions/` is a clean skip, no warning — this
    source is fully optional (unlike the three `_gather_signal` sources, whose
    absence degrades movement-detection quality and IS warned about). A single
    malformed suggestion file warns by name and never blocks its siblings or
    aborts the run.
  - The reader never writes to a suggestion record (no `status:` mutation) and
    never auto-applies one — applying is a human writing `proposed_status` into
    the goal's live `key_results[].status`, no code path here performs that
    write. It never reads another repo's `state/kr-suggestions/` — resolution
    is local to `repo_root`'s own `state/`, mirroring the handoff/changelog
    sources above.
  - A suggestion whose anchors resolve cleanly but whose target goal is
    non-active is NEVER silently dropped either — it gets its own
    "NOT PRESENTED" line in the resolution-issues report block, distinct from
    an UNRESOLVED (anchor doesn't exist) line, so a human can tell the two
    apart. The goal artifact itself is untouched in this case (non-active
    goals never get the write-back pass).
  - A suggestion never suppresses or overwrites the computed proposal for the
    same KR — it is appended alongside `process_kr_entry`'s own proposed_line,
    always additive.
  - A `goal_id` claimed by more than one `state/goals/*.yaml` file's `id:`
    field is never resolved against either — resolution is genuinely
    undecidable, so every suggestion targeting it gets its own "AMBIGUOUS"
    line in the resolution-issues report block, distinct from both UNRESOLVED
    (anchor doesn't exist) and NOT PRESENTED (resolved, non-active goal), and
    is never written into any goal artifact. Nothing here silently picks the
    last-read file, which would otherwise leak the suggestion into whichever
    goal happens to share the duplicated id.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from coordinator_core.ipc import register_op
from coordinator_core import launchable
from coordinator_core.launchable import resolve_launchable
from coordinator_core.ops.ceremony.records_query import query_records
from coordinator_core.win_portability import is_executable

# ---------------------------------------------------------------------------
# Stopword list — verbatim port of the bash grep -v -E stopword alternation.
# ---------------------------------------------------------------------------
_STOPWORDS = frozenset(
    """a an the and or of to in for is are be by with on at from as it its this
    that we our per week monthly daily each every by via into has have had can
    will should must not no if when then do does did was were been""".split()
)

# Matches the "# --- KR Re-assessment (proposed" ... "# --- end proposed re-assessment ---"
# block boundaries the bash version strips before re-appending a fresh one.
_BLOCK_BEGIN_RE = re.compile(r"^# --- KR Re-assessment \(proposed")
_BLOCK_END_RE = re.compile(r"^# --- end proposed re-assessment ---$")

# New-KR-list-item line: "  - " (any leading whitespace, dash, required whitespace).
_KR_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+")
# Indented "field: value" line inside an active KR entry.
_KR_FIELD_RE = re.compile(r"^[ \t]+([a-z_]+):[ \t]*(.*)$")
# Inline "id: value" / "text: value" on the dash line itself, after stripping
# through the first "- " occurrence (mirrors bash's `${line#*- }`).
_INLINE_ID_RE = re.compile(r"^id:[ \t]*(.*)$")
_INLINE_TEXT_RE = re.compile(r"^text:[ \t]*(.*)$")


# ---------------------------------------------------------------------------
# Pure parsing helpers — each a direct transliteration of one bash function.
# ---------------------------------------------------------------------------


def parse_frontmatter_field(text: str, field: str) -> str:
    """Whole-document-YAML first-match scalar extractor.

    Port of parse_frontmatter_field() — scans every line for the first one
    starting with ``<field>:`` and returns everything after the colon with
    leading whitespace stripped (matching the awk ``sub(/^field:[[:space:]]*/, "")``
    behavior — trailing content, including any embedded quotes, is returned
    as-is; callers strip quotes themselves, matching the bash callers'
    ``tr -d '"'`` post-processing).
    """
    pattern = re.compile(r"^" + re.escape(field) + r":[ \t]*(.*)$")
    for line in text.splitlines():
        m = pattern.match(line)
        if m:
            return m.group(1)
    return ""


def extract_key_results(text: str) -> str:
    """Extract the key_results[] YAML list block.

    Port of extract_key_results() — starts collecting after a line that is
    exactly ``key_results:`` (that header line itself is NOT included in the
    output), stops at the next top-level key (a line starting with a letter
    or underscore with NO leading whitespace), and returns the collected
    lines newline-joined (no trailing newline — matches awk `print`'s
    per-line behavior when the caller joins captured stdout).
    """
    lines = text.splitlines()
    out: List[str] = []
    in_kr = False
    for line in lines:
        if not in_kr:
            if line == "key_results:" or line.startswith("key_results:"):
                # awk's /^key_results:/ matches a PREFIX, not an exact-line —
                # preserve that: any line starting with "key_results:" opens
                # the block (the header line is skipped, never emitted).
                in_kr = True
            continue
        # in_kr is True from here.
        if re.match(r"^[a-zA-Z_]", line) and not re.match(r"^[ \t]", line):
            break
        out.append(line)
    return "\n".join(out)


def match_signal(keyword: str, all_signal_text: str) -> bool:
    """Case-insensitive substring search over the combined weekly signal text.

    Port of match_signal() (``grep -qi``). Keywords are always plain
    ``[a-z0-9]+`` tokens by construction (see _extract_keywords), so a plain
    lowercase substring check is exactly equivalent to the bash grep -qi call
    — no regex metacharacters can appear in `keyword`.
    """
    return keyword.lower() in all_signal_text.lower()


def _extract_keywords(text: str) -> List[str]:
    """Port of the keyword-extraction pipeline inside process_kr_entry():
    lowercase -> squeeze non-alnum runs to single spaces (``tr -cs 'a-z0-9' ' '``)
    -> split on that single-space delimiter (``tr ' ' '\\n'``) -> drop stopwords
    (``grep -v -E '^(...)$'``) -> take first 5 surviving tokens (``head -5``).

    PRESERVED BASH QUIRK (parity-critical, do not "fix"): when `text` starts
    (or, after squeezing, would start) with a non-alnum character — e.g. a
    literal leading `"` from an inline `text: "..."` YAML value — the squeeze
    step leaves a SINGLE leading space, and splitting on that space yields a
    genuine leading EMPTY token. `grep -v -E '^(stopword|...)$'` does NOT match
    an empty line (none of the stopword alternatives is the empty string), so
    that empty token SURVIVES the stopword filter and consumes one of the 5
    `head -5` slots — silently pushing what would have been the 5th real
    keyword out of the window. The empty token itself never causes a
    match_signal() call (the caller's `len(kw) < 3` guard skips it, exactly
    mirroring `for kw in ${keywords}` word-splitting skipping empty args) —
    but the WASTED SLOT is real and changes which keywords get tested. This
    module reproduces it exactly (do not add an `if t:` truthy filter here —
    that would silently fix the bug and break golden-diff parity with the
    bash version). Flagged as a follow-up bugfix candidate for a later
    redesign pass, not fixed in this byte-parity port.
    """
    lowered = text.lower()
    squeezed = re.sub(r"[^a-z0-9]+", " ", lowered)
    tokens = squeezed.split(" ")
    filtered = [t for t in tokens if t not in _STOPWORDS]
    return filtered[:5]


def process_kr_entry(
    kr_id: str,
    kr_text: str,
    current_status: str,
    weekly_perceptible: str,
    all_signal_text: str,
) -> Optional[Dict[str, Any]]:
    """Assess a single parsed KR entry against weekly signal.

    Port of process_kr_entry(). Returns None when both id and text are empty
    (the bash version's early ``return`` — no report line, no proposed line).
    Otherwise returns a dict with keys: movement, proposed_status, flag,
    kr_label, report_line, proposed_line, flagged (bool — True when the
    "maybe-not-a-goal" flag fired, mirroring the bash FLAGS_FOUND side-effect).
    """
    if not kr_id and not kr_text:
        return None

    movement = "no"
    if kr_text:
        for kw in _extract_keywords(kr_text):
            if len(kw) < 3:
                continue
            if match_signal(kw, all_signal_text):
                movement = "yes"
                break

    proposed_status = current_status
    if current_status == "not-started" and movement == "yes":
        proposed_status = "in-progress"
    elif current_status == "in-progress" and movement == "yes":
        proposed_status = "in-progress"

    flag = ""
    flagged = False
    if movement == "no" and weekly_perceptible.lower() == "true":
        flag = " *** maybe-not-a-goal — no perceptible movement this week"
        flagged = True

    kr_label = kr_id if kr_id else kr_text[:40]

    report_line = (
        f"  KR [{kr_label}]: current={current_status} | movement={movement} "
        f"| proposed={proposed_status}{flag}"
    )
    proposed_line = (
        f"    # KR {kr_label}: proposed_status: {proposed_status} "
        f"| perceptible_movement: {movement}{flag}"
    )

    return {
        "movement": movement,
        "proposed_status": proposed_status,
        "flag": flag,
        "flagged": flagged,
        "kr_label": kr_label,
        "report_line": report_line,
        "proposed_line": proposed_line,
    }


def parse_kr_block(kr_block: str) -> List[Tuple[str, str, str, str]]:
    """Parse the key_results[] block into (id, text, status, weekly_perceptible)
    tuples, one per "- " list item.

    Port of the bash main-loop's inline KR-entry state machine: a new item
    starts at a "  - " line (inline id:/text: field permitted on that same
    line, mirroring bash's ``${line#*- }`` — shortest-prefix strip through the
    FIRST "- " substring in the line); subsequent indented "field: value"
    lines set/override id/text/status/weekly_perceptible for the current
    entry (last-write-wins, same as the bash case statement); a new "- " line
    or EOF flushes the previous entry.
    """
    entries: List[Tuple[str, str, str, str]] = []
    kr_id = ""
    kr_text = ""
    kr_status = ""
    kr_weekly_perceptible = ""
    in_entry = False

    def flush() -> None:
        if in_entry:
            entries.append((kr_id, kr_text, kr_status, kr_weekly_perceptible))

    for line in kr_block.splitlines():
        if _KR_ITEM_RE.match(line):
            flush()
            in_entry = True
            kr_id, kr_text = "", ""
            kr_status, kr_weekly_perceptible = "not-started", "false"

            idx = line.find("- ")
            local_rest = line[idx + 2 :] if idx != -1 else line
            m_id = _INLINE_ID_RE.match(local_rest)
            m_text = _INLINE_TEXT_RE.match(local_rest)
            if m_id:
                kr_id = m_id.group(1)
            elif m_text:
                kr_text = m_text.group(1)
            continue

        if in_entry:
            m = _KR_FIELD_RE.match(line)
            if m:
                field_name, field_val = m.group(1), m.group(2)
                if field_name == "id":
                    kr_id = field_val
                elif field_name == "text":
                    kr_text = field_val
                elif field_name == "status":
                    kr_status = field_val
                elif field_name == "weekly_perceptible":
                    kr_weekly_perceptible = field_val

    flush()
    return entries


def strip_prior_proposed_block(text: str) -> str:
    """Remove any prior "# --- KR Re-assessment (proposed ..." ... "# --- end
    proposed re-assessment ---" block (inclusive of both boundary lines).

    Port of the awk skip-block filter in the bash write-back path.
    """
    out: List[str] = []
    skip = False
    for line in text.splitlines():
        if _BLOCK_BEGIN_RE.match(line):
            skip = True
            continue
        if skip and _BLOCK_END_RE.match(line):
            skip = False
            continue
        if not skip:
            out.append(line)
    result = "\n".join(out)
    if out:
        result += "\n"
    return result


def build_proposed_block(since: str, proposed_lines: List[str]) -> str:
    """Build the proposed-re-assessment comment block (anchored on `since`,
    not today's date — F5 fix, avoids diff churn on identical re-runs)."""
    lines = [f"# --- KR Re-assessment (proposed since={since}) ---"]
    lines.extend(proposed_lines)
    lines.append("# --- end proposed re-assessment ---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Signal gathering — query-completions.py remains a subprocess shim to a
# still-non-Python sibling (interim, see module docstring cross-tranche
# note); the handoff-records source is now an in-process native call (see
# module docstring negative-spec, 2026-07-21 repoint).
# ---------------------------------------------------------------------------


def _forward_child_stderr(source: str, stderr: str, warnings: List[str]) -> None:
    """Surface a signal-source child's stderr into the caller-visible warning list.

    This shim runs under ``capture_output=True``, so anything the child writes to
    stderr is held in the pipe and dropped unless it is deliberately forwarded.
    Since 2026-07-22 query-completions.py reports partial reads that way, naming
    entries it skipped for unparseable frontmatter rather than dropping them
    silently. A skipped entry is a real record on disk that no consumer can see, and
    it degrades this module's movement detection into a false negative, so the line
    belongs in ``warnings`` (the op's caller-facing diagnostic channel) rather than in
    a discarded pipe.

    Negative-spec: do NOT filter these lines back down to restore the pre-2026-07-22
    quiet — the child stays silent for files that legitimately carry no frontmatter,
    so anything arriving here is a true positive. The handoff-records signal source
    no longer runs as a child process (native in-process call), so it no longer
    routes through this helper.
    """
    text = (stderr or "").strip()
    if text:
        warnings.append(f"{source} reported on stderr: {text}")


def _gather_signal(
    bin_dir: Optional[Path], repo_root: Optional[Path], since: str
) -> Tuple[str, List[str]]:
    """Gather weekly signal text (completions + handoffs + week-changelog
    header) and any best-effort warnings, exactly mirroring the bash
    version's degrade-to-warning-not-fatal behavior for each of the three
    sources.
    """
    warnings: List[str] = []
    completion_signal = ""
    handoff_signal = ""
    changelog_text = ""

    bin_dir_label = str(bin_dir) if bin_dir else "<unresolved bin dir>"

    qc_path = (bin_dir / "query-completions.py") if bin_dir else None
    if qc_path and is_executable(qc_path):
        try:
            from coordinator_core.win_portability import no_console_creationflags

            if launchable._is_windows():
                qc_argv = resolve_launchable(str(qc_path))
            else:
                qc_argv = [sys.executable, str(qc_path)]

            proc = subprocess.run(
                [*qc_argv, "--since", since, "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
                **no_console_creationflags(),
            )
            if proc.returncode == 0:
                completion_signal = proc.stdout
            else:
                warnings.append(
                    f"signal degraded — query-completions.py exited {proc.returncode}; "
                    "movement detection may produce false negatives"
                )
            _forward_child_stderr("query-completions.py", proc.stderr, warnings)
        except (OSError, subprocess.TimeoutExpired):
            pass  # best-effort, matches bash's `|| true`
    else:
        warnings.append(
            "signal incomplete — query-completions.sh not found at "
            f"{bin_dir_label}; movement detection may produce false negatives"
        )

    if repo_root:
        try:
            handoff_records = query_records("handoff", Path(repo_root), since=since)
            handoff_signal = json.dumps(handoff_records, indent=2)
        except (ValueError, SystemExit) as exc:
            # Same degrade-to-warning posture the retired query-records.js spawn had
            # on a non-zero exit (e.g. an unparseable --since value) — fail-open,
            # never fatal. See _gather_signal's docstring negative-spec.
            warnings.append(
                f"signal degraded — records_query.query_records(handoff) raised {exc!r}; "
                "movement detection may produce false negatives"
            )
    else:
        warnings.append(
            "signal incomplete — no repo_root supplied for the handoff records query; "
            "movement detection may produce false negatives"
        )

    if repo_root:
        changelog_header = Path(repo_root) / "state" / "week-changelog" / "HEADER.md"
        if changelog_header.is_file():
            try:
                changelog_text = changelog_header.read_text(encoding="utf-8")
            except OSError:
                pass  # best-effort, matches the sibling signal reads' `|| true`

    all_signal_text = f"{completion_signal}\n{handoff_signal}\n{changelog_text}"
    return all_signal_text, warnings


def _gather_kr_suggestions(repo_root: Optional[Path]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Best-effort read of `state/kr-suggestions/*.yaml` (DR-130 contract).

    Sibling to `_gather_signal` in posture, not in shape: this source returns
    structured records (parsed per `kr-suggestion.schema.json`), not flat text
    for keyword matching — resolution against a goal's `id:`/`key_results[].id`
    happens in `reassess()`'s per-goal loop, not here.

    Returns (records, warnings). No `repo_root`, or an absent/empty
    `state/kr-suggestions/` directory, returns ([], []) with no warning — this
    source is fully optional, unlike the three `_gather_signal` sources, and
    degrades exactly as a missing `state/goals/` does today. A per-file parse
    failure (bad YAML, or a document that isn't a mapping) warns naming that
    file and is skipped; sibling files are unaffected. No JSON-Schema
    validation is performed here (out of scope per the DR-130 reader plan) —
    each record's fields are read with `.get()` defaults downstream.
    """
    if not repo_root:
        return [], []
    suggestions_dir = Path(repo_root) / "state" / "kr-suggestions"
    if not suggestions_dir.is_dir():
        return [], []

    records: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for path in sorted(p for p in suggestions_dir.iterdir() if p.is_file() and p.suffix == ".yaml"):
        try:
            text = path.read_text(encoding="utf-8")
            parsed = yaml.safe_load(text)
        except (OSError, yaml.YAMLError) as exc:
            warnings.append(f"kr-suggestion {path.name} could not be parsed: {exc!r} — skipped")
            continue
        if not isinstance(parsed, dict):
            warnings.append(f"kr-suggestion {path.name} did not parse to a mapping — skipped")
            continue
        parsed["__reader_source_path__"] = str(path)
        records.append(parsed)
    return records, warnings


def _render_kr_suggestion(kr_id: str, suggestion: Dict[str, Any], live_status: str) -> Tuple[str, str]:
    """Render one resolved KR-suggestion into a report line and a
    proposed-block comment line — appended ALONGSIDE, never instead of, the
    computed proposal for the same KR (`process_kr_entry`'s own report/
    proposed lines).

    Staleness: a non-null `expected_current_status` that diverges from the
    target KR's live status is called out explicitly; `null` means the
    producer didn't check and carries no staleness claim either way.
    """
    proposed_status = suggestion.get("proposed_status", "")
    rationale = suggestion.get("rationale", "")
    provenance = suggestion.get("provenance") or {}
    producing_system = provenance.get("producing_system", "")
    source_ref = provenance.get("source_ref", "")
    span = provenance.get("span")
    recorded_at = provenance.get("recorded_at", "")

    expected = suggestion.get("expected_current_status")
    stale_note = ""
    if expected is not None and expected != live_status:
        stale_note = (
            f" *** STALE — suggestion derived when status was {expected!r}, "
            f"live status is now {live_status!r}"
        )

    provenance_str = f"{producing_system}/{source_ref}"
    if span:
        provenance_str += f"@{span}"
    provenance_str += f" recorded_at={recorded_at}"

    report_line = (
        f"  KR-SUGGESTION [{kr_id}]: proposed={proposed_status} | rationale={rationale!r} "
        f"| provenance: {provenance_str}{stale_note}"
    )
    proposed_line = (
        f"    # KR-SUGGESTION {kr_id}: proposed_status: {proposed_status} "
        f"| rationale: {rationale} | provenance: {provenance_str}{stale_note}"
    )
    return report_line, proposed_line


def _write_goal_file_atomic(goal_file: Path, new_text: str) -> None:
    """Write *new_text* to *goal_file* atomically (mkstemp + os.replace).

    Review: code-reviewer P0 — the prior plain ``goal_file.write_text(...)``
    truncate-write left a state/goals/*.yaml artifact vulnerable to
    truncation/corruption if the process is killed mid-write (e.g. by
    cc_invoke's outer client-side subprocess timeout — see the CLI
    trampoline's CC_INVOKE_TIMEOUT_SECS default bump for the other half of
    this fix). Mirrors the same mkstemp-in-same-dir + os.replace pattern
    already used by orientation/regenerate_cache.py::write_cache().
    """
    fd, tmp = tempfile.mkstemp(dir=str(goal_file.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_text)
        os.replace(tmp, goal_file)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass  # best-effort tmp cleanup on an already-failing path; original exception re-raises below
        raise


# ---------------------------------------------------------------------------
# Top-level orchestration — pure over its inputs except for the file I/O at
# the very edges (goal-file reads/writes, signal-gathering subprocess calls).
# ---------------------------------------------------------------------------


def reassess(
    goals_dir: Path,
    since: str = "7d",
    dry_run: bool = False,
    bin_dir: Optional[Path] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run the full weekly KR re-assessment over goals_dir/*.yaml.

    Returns a dict:
        exit_code   (int)  — always 0 on the success path (mirrors bash: fatal
                             conditions like missing goals_dir degrade to an
                             INFO skip, never a non-zero exit).
        report      (str)  — the full stdout report text (newline-joined).
        flags_found (bool) — True if any KR was flagged "maybe-not-a-goal".
        written     (list[str]) — paths of goal artifacts rewritten (empty
                             when dry_run or no proposed_lines).
        warnings    (list[str]) — best-effort signal-source warnings.
    """
    goals_dir = Path(goals_dir)
    report_lines: List[str] = []
    written: List[str] = []

    if not goals_dir.is_dir():
        report_lines.append(
            f"INFO: No goals directory found at {goals_dir} — skipping KR re-assessment."
        )
        return {
            "exit_code": 0,
            "report": "\n".join(report_lines),
            "flags_found": False,
            "written": written,
            "warnings": [],
        }

    goal_files = sorted(p for p in goals_dir.iterdir() if p.is_file() and p.suffix == ".yaml")

    if not goal_files:
        report_lines.append(
            f"INFO: No goal files found in {goals_dir} — skipping KR re-assessment."
        )
        return {
            "exit_code": 0,
            "report": "\n".join(report_lines),
            "flags_found": False,
            "written": written,
            "warnings": [],
        }

    all_signal_text, warnings = _gather_signal(bin_dir, repo_root, since)
    kr_suggestions, kr_suggestion_warnings = _gather_kr_suggestions(repo_root)
    warnings = warnings + kr_suggestion_warnings

    # Pre-resolve KR-suggestion anchors (DR-130) against every goal artifact's
    # `id:` field and `key_results[].id` set — independent of active/inactive
    # status, since resolution asks only "does this anchor exist", not
    # "will this week's report act on it." goal_id resolves by the artifact's
    # own `id:` field, NEVER by filename (AC3); kr_id resolves by
    # key_results[].id, NEVER by array index (AC4). Goal status is captured
    # alongside so a suggestion that resolves cleanly onto a NON-ACTIVE goal
    # can still be reported (see below) rather than silently vanishing when
    # the per-goal loop skips that goal's active-only processing — invariant
    # 4 of the DR-130 contract ("an unresolvable anchor is reported, never
    # silently dropped") is read here to cover "resolved but unreachable this
    # run" too, not just "anchor doesn't exist" — the human-visible failure
    # (a suggestion nobody sees) is the same in both cases.
    goal_kr_ids_by_goal_id: Dict[str, set] = {}
    goal_status_by_goal_id: Dict[str, str] = {}
    goal_id_counts: Dict[str, int] = {}
    goal_text_by_file: Dict[Path, str] = {}
    for goal_file in goal_files:
        gid_text = goal_file.read_text(encoding="utf-8")
        goal_text_by_file[goal_file] = gid_text
        gid = parse_frontmatter_field(gid_text, "id").replace('"', "")
        if not gid:
            continue
        goal_id_counts[gid] = goal_id_counts.get(gid, 0) + 1
        goal_kr_ids_by_goal_id[gid] = {
            kr_id for kr_id, *_ in parse_kr_block(extract_key_results(gid_text)) if kr_id
        }
        goal_status_by_goal_id[gid] = parse_frontmatter_field(gid_text, "status").replace('"', "")

    # A goal_id claimed by more than one goal file is undecidable to resolve
    # against — see the module docstring's AMBIGUOUS negative-spec entry.
    duplicate_goal_ids = {gid for gid, count in goal_id_counts.items() if count > 1}

    suggestions_by_goal: Dict[str, List[Dict[str, Any]]] = {}
    resolution_report_lines: List[str] = []
    for sug in kr_suggestions:
        sug_goal_id = sug.get("goal_id")
        sug_kr_id = sug.get("kr_id")
        source = sug.get("__reader_source_path__", "<unknown kr-suggestion>")
        if sug_goal_id in duplicate_goal_ids:
            resolution_report_lines.append(
                f"  KR-SUGGESTION AMBIGUOUS [{source}]: goal_id={sug_goal_id!r} is claimed "
                f"by {goal_id_counts[sug_goal_id]} state/goals/*.yaml files — resolution is "
                "undecidable, not presented or written to any goal artifact"
            )
            continue
        if sug_goal_id not in goal_kr_ids_by_goal_id:
            resolution_report_lines.append(
                f"  KR-SUGGESTION UNRESOLVED [{source}]: goal_id={sug_goal_id!r} "
                "matches no state/goals/*.yaml id: field"
            )
            continue
        if sug_kr_id not in goal_kr_ids_by_goal_id[sug_goal_id]:
            resolution_report_lines.append(
                f"  KR-SUGGESTION UNRESOLVED [{source}]: goal_id={sug_goal_id!r} resolved, "
                f"but kr_id={sug_kr_id!r} matches no key_results[].id on that goal"
            )
            continue
        goal_status = goal_status_by_goal_id.get(sug_goal_id, "")
        if goal_status not in ("active", ""):
            resolution_report_lines.append(
                f"  KR-SUGGESTION NOT PRESENTED [{source}]: goal_id={sug_goal_id!r} "
                f"kr_id={sug_kr_id!r} resolved, but that goal's status={goal_status!r} "
                "(non-active) — not presented this run"
            )
            continue
        suggestions_by_goal.setdefault(sug_goal_id, []).append(sug)

    report_lines.append("=== Weekly KR Re-assessment ===")
    report_lines.append(f"Signal window: --since {since}")
    report_lines.append(f"Goals dir:     {goals_dir}")
    report_lines.append("")

    flags_found = False

    for goal_file in goal_files:
        goal_name = goal_file.name
        text = goal_text_by_file[goal_file]
        goal_title = parse_frontmatter_field(text, "title").replace('"', "")
        goal_status = parse_frontmatter_field(text, "status").replace('"', "")
        goal_id = parse_frontmatter_field(text, "id").replace('"', "")

        if goal_status not in ("active", ""):
            report_lines.append(f"-- {goal_name}: status={goal_status} — skipping (non-active)")
            continue

        report_lines.append(f"Goal: {goal_title or goal_name}")
        report_lines.append(f"  File: {goal_file}")

        kr_block = extract_key_results(text)
        if not kr_block:
            report_lines.append("  (no key_results[] found)")
            report_lines.append("")
            continue

        suggestions_by_kr: Dict[str, List[Dict[str, Any]]] = {}
        for sug in suggestions_by_goal.get(goal_id, []):
            suggestions_by_kr.setdefault(sug.get("kr_id"), []).append(sug)

        proposed_lines: List[str] = []
        for kr_id, kr_text, kr_status, kr_weekly_perceptible in parse_kr_block(kr_block):
            result = process_kr_entry(kr_id, kr_text, kr_status, kr_weekly_perceptible, all_signal_text)
            if result is None:
                continue
            report_lines.append(result["report_line"])
            proposed_lines.append(result["proposed_line"])
            if result["flagged"]:
                flags_found = True

            for sug in suggestions_by_kr.get(kr_id, []):
                sug_report_line, sug_proposed_line = _render_kr_suggestion(kr_id, sug, kr_status)
                report_lines.append(sug_report_line)
                proposed_lines.append(sug_proposed_line)

        if not dry_run and proposed_lines:
            proposed_block = build_proposed_block(since, proposed_lines)
            stripped = strip_prior_proposed_block(text)
            new_text = stripped + proposed_block
            _write_goal_file_atomic(goal_file, new_text)
            written.append(str(goal_file))

        report_lines.append("")

    if resolution_report_lines:
        report_lines.append("=== KR-suggestion resolution issues ===")
        report_lines.extend(resolution_report_lines)
        report_lines.append("")

    report_lines.append("=== Re-assessment summary ===")
    if flags_found:
        report_lines.append(
            "ACTION NEEDED: One or more KRs had no perceptible movement despite weekly_perceptible:true."
        )
        report_lines.append(
            "Review flagged KRs (marked '*** maybe-not-a-goal') and confirm or reclassify."
        )
    else:
        report_lines.append(
            "All active KRs with weekly_perceptible:true show movement (or none required assessment)."
        )

    if dry_run:
        report_lines.append("")
        report_lines.append("(dry-run mode — no files were modified)")

    report_lines.append("")
    report_lines.append("Proposed statuses written to goal artifacts for EM/PM confirmation.")
    report_lines.append("These are PROPOSALS only — the live 'status' field is unchanged.")

    return {
        "exit_code": 0,
        "report": "\n".join(report_lines),
        "flags_found": flags_found,
        "written": written,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Op registration — "none"-scoped (no repo_root-derived state access; the
# caller/trampoline supplies goals_dir/bin_dir as explicit absolute paths,
# same class as cartography.*/percolate.* per ipc.py::_OP_KEY_SCOPE).
# ---------------------------------------------------------------------------


@register_op("goals.reassess_krs")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "goals.reassess_krs" handler.

    Params:
        goals_dir (str)  — absolute path to the state/goals directory. Required.
        since     (str)  — signal-query window, e.g. "7d". Default "7d".
        dry_run   (bool) — report-only, zero writes. Default False.
        bin_dir   (str)  — absolute path to the coordinator/bin dir housing
                            query-completions.sh/query-records.js (the
                            trampoline's own directory — mirrors the bash
                            script's BASH_SOURCE-derived SCRIPT_DIR). Optional;
                            absent degrades both signal sources to warnings.
        signal_repo_root (str) — absolute path to the repo whose
                            state/week-changelog/HEADER.md should be read for
                            changelog signal. Optional.

    Returns the dict shape documented on reassess(), plus on error:
        exit_code 1, error (str).
    """
    goals_dir_raw = params.get("goals_dir") or ""
    if not goals_dir_raw:
        return {"exit_code": 1, "error": "missing required param: goals_dir"}

    since = params.get("since") or "7d"
    dry_run = bool(params.get("dry_run", False))
    bin_dir_raw = params.get("bin_dir") or ""
    signal_repo_root_raw = params.get("signal_repo_root") or ""

    bin_dir = Path(bin_dir_raw) if bin_dir_raw else None
    signal_repo_root = Path(signal_repo_root_raw) if signal_repo_root_raw else None

    return reassess(
        goals_dir=Path(goals_dir_raw),
        since=since,
        dry_run=dry_run,
        bin_dir=bin_dir,
        repo_root=signal_repo_root,
    )
