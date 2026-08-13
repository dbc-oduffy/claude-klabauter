r"""
coordinator_core.ops.prune_resolved_queue_entries — strip resolved-state bloat
from legacy prose queue files (improvement-queue.md / bug-backlog.md).

Purpose: doctrine alone cannot prevent drift — each EM has a non-deterministic
way of marking closure in markdown, so this pruner is the structural backstop.
Runs on-demand from `/update-docs` (Phase 11i, S5).

CASE SENSITIVITY: closure keywords are matched UPPERCASE-ONLY by convention.
Mixed-case "Fixed" or lowercase "fixed" in narrative survives — only the
all-caps EM-closure convention triggers a strip. This is intentional:
lowercase usage is overwhelmingly narrative ("the bug was fixed by..."),
uppercase is the closure-annotation pattern.

OUT OF SCOPE: inline closure annotations on main-line list entries like
  "- 2026-MM-DD | source | file | description — APPLIED to X.md"
are deliberately not stripped — too high false-positive risk against
legitimate "landed in /X" / "applied to Y" descriptions of active queue
items. Doctrine expects EMs to delete the line on resolution; the pruner
only catches structural closure-annotation patterns (heading / strikethrough
/ table-row shapes).

Rules (identical numbering to the bash/awk oracle, preserved for cross-referencing
history/lessons that cite "Rule N"):
  Rule 1 — Entry-shape (queue files only): delete any entry block whose
           `resolution:` sub-line starts with "resolved" (or any
           non-pending/non-in_progress value), or which carries a
           `**Closeout:**` sub-line. Applies to improvement-queue.md. NOT
           bug-backlog.md.
           NOTE: coordinator-improvement-queue.md was a legacy prose queue
           removed in 2026-06; the central queue is now structured YAML under
           state/improvement-queue/ and closes via git mv — line-deletion
           pruning no longer applies to it (and it is no longer in the
           basename allowlist below, matching the oracle's current — not
           historical — allowlist).
  Rule 2 — Section-body: delete any section matching
           `^## (Processed|Resolved|History|Closed|Done|Archive|Closeout)` and
           its entire body up to the next `##` heading or EOF. Applies to all
           allowlisted files. Regex breadth catches per-run-suffixed variants
           like "## Resolved this run (bug-blitz 2026-05-06-22h42)".
  Rule 3 — Ceremony-line strip (queue files only): drop trivial
           schema-ceremony sub-lines that never change in practice —
           "  recurring: 0" and "  resolution: pending" / "  resolution:
           in_progress". Per DR-056 (amended 2026-05-17): main-line-only
           schema; non-zero recurring counters fold into the main line as
           " [recurring: N]" when needed.
  Rule 4 — Idempotent: running twice produces no further changes.
  Rule 5 — H3 status-closure block (all allowlisted files): delete any H3
           heading "^### " carrying a bracketed status "[FIXED 2026-...]" /
           "[FIXED]" / "[FIXED — ...]" (keyword immediately followed by
           closing-bracket, date digit, or em-dash/hyphen), OR ending with
           bare-keyword-at-EOL (whitespace before keyword, EOL after). Body
           suppressed until the next `##` or `###` heading. Catches the
           example-game-repo-bug-backlog pattern: "### [FIXED 2026-05-16] BS-... <30
           lines of forensic>". Markdown links with closure-keyword display
           text (e.g. "[CLOSED issue](url)") survive because the
           bracket-content pattern requires date/dash/close, not a bare word.
  Rule 6 — Strikethrough-closure line strip (all allowlisted files): drop any
           single line containing "~~" AND an UPPERCASE closure keyword
           (FIXED|RESOLVED|CLOSED|DONE|COMPLETED). Catches the example-sim-repo
           table-row pattern: "| ~~BS-...~~ | ... ~~P2~~ CLOSED | ... —
           **FIXED (sha):** ... |". Non-closure strikethrough (e.g. "~~old
           approach~~ — we now ...") survives because it lacks the keyword.
  Rule 7 — Table-row resolution strip (all allowlisted files): drop any
           "| BS-..." table row where COLUMN 2 (the first content cell after
           the id) starts with a closure keyword. Catches "| BS-2026-03-19-1
           | FIXED (run 2) — ... |". Rows with the keyword in column 3+, or
           only in narrative text, survive.
  Rule 8 — Buffer-orphan guard (queue files only): when a Rule 6/7 closure
           line appears mid-entry-collection (between a buffered main-line
           and its sublines), treat the entire entry as closure-annotated —
           drop the main line, drop the closure line, and suppress subsequent
           orphaned sublines until the next non-subline. Prevents
           synthetic-subline corruption.

Usage: prune-resolved-queue-entries.sh <queue-file>

Exit codes (parity-critical):
  0 — file pruned and atomically replaced (or no changes needed)
  1 — usage error, disallowed basename, or file-not-found (fails loud with a
      stderr message — never skips silently)

On parse error or unexpected structure, fails loud with a stderr message —
does NOT skip silently. Only operates on the allowlisted queue file
basenames below (path-allowlist guard, matched by basename so both absolute
and relative paths work).

Port of: prune-resolved-queue-entries.sh (coordinator-claude b5a4192c, 2026-07-20)
Spec backlink: docs/plans/2026-05-07-prune-resolved-state-bloat.md § S5
               (lives in the coordinator-claude consumer-project docs/plans/, not
               here — this module is the claude-klabauter-owned engine half)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (faithful oracle-bug repro — do not silently "fix" these):
    - The atomic replace uses a tempfile created in the SAME directory as the
      target (mirrors the oracle's `mktemp "${INPUT}.tmp.XXXXXX"` + `mv`), so
      the replacement is atomic on the same filesystem. Like the oracle, the
      replacement file's fresh-tempfile permission bits (not the original
      file's bits) win after the swap — harmless here because the targets are
      plain non-executable markdown; git's tree object records mode 100644
      for a non-executable blob regardless of the local filesystem bit, so
      this has no observable effect for this script's file class (unlike a
      git-hook/launcher rewrite, where permission preservation would matter —
      see the migration brief's Rule 5, which does not apply to this port).
    - `[[:space:]]` in the awk oracle is POSIX-space (space, tab, newline,
      vertical-tab, form-feed, carriage-return); Python's ``\s`` (raw-string
      pattern) is treated as an equivalent superset for per-line matching
      (newline never appears mid-line since lines are processed with their
      line terminator stripped before matching).
    - A final input line lacking a trailing newline gets ONE appended on
      output — this reproduces awk's `print` always emitting its ORS
      ("\n") after every record, including the last, regardless of whether
      the source record was itself newline-terminated. Not "fixed" here;
      faithfully carried forward from the oracle.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile

from coordinator_core.session.declared_writes import declare_write

_PROG = "prune-resolved-queue-entries.sh"

# Path-allowlist guard — only operate on these two basenames.
_ALLOWED_BASENAMES = frozenset({"improvement-queue.md", "bug-backlog.md"})

# Basenames for which Rule 1 (entry-shape strip) applies.
_RULE1_BASENAMES = frozenset({"improvement-queue.md"})

_CLOSURE_KEYWORDS = r"FIXED|RESOLVED|CLOSED|DONE|COMPLETED"

_RE_H2_CLOSURE = re.compile(
    r"^## (Processed|Resolved|History|Closed|Done|Archive|Closeout)"
)
_RE_H3_PREFIX = re.compile(r"^### ")
_RE_H3_BRACKET = re.compile(
    r"\[(" + _CLOSURE_KEYWORDS + r")(\]|\s+([0-9]|—|-))"
)
_RE_H3_BARE_EOL = re.compile(r"\s(" + _CLOSURE_KEYWORDS + r")\s*$")
_RE_STRIKE_KEYWORD = re.compile(
    r"(^|[^A-Za-z0-9_])(" + _CLOSURE_KEYWORDS + r")([^A-Za-z0-9_]|$)"
)
_RE_TABLE_ROW = re.compile(
    r"^\| BS-[^ |]+\s*\| (" + _CLOSURE_KEYWORDS + r")([^A-Za-z0-9_]|$)"
)
_RE_MAIN_LINE = re.compile(r"^- \d{4}-\d{2}-\d{2} \|")
_RE_RECURRING_ZERO = re.compile(r"^  recurring: 0\s*$")
_RE_RESOLUTION_PENDING = re.compile(r"^  resolution: pending\s*$")
_RE_RESOLUTION_IN_PROGRESS = re.compile(r"^  resolution: in_progress\s*$")


def _is_h2_closure(content: str) -> bool:
    return bool(_RE_H2_CLOSURE.match(content))


def _is_h3_closure(content: str) -> bool:
    if not _RE_H3_PREFIX.match(content):
        return False
    if _RE_H3_BRACKET.search(content):
        return True
    if _RE_H3_BARE_EOL.search(content):
        return True
    return False


def _is_strikethrough_closure(content: str) -> bool:
    if "~~" not in content:
        return False
    return bool(_RE_STRIKE_KEYWORD.search(content))


def _is_table_row_closure(content: str) -> bool:
    return bool(_RE_TABLE_ROW.match(content))


def prune_lines(lines: list[str], apply_rule1: bool) -> list[str]:
    """Apply Rules 1-8 to `lines` (each WITH its trailing line terminator, or
    without one on a final unterminated line) and return the pruned list.

    Faithful state-machine port of the awk oracle's single pass — the
    ordering below (Rule-8 orphan-suppression check, then section-suppression
    fall-through, then heading-closure detectors, then per-line closure-marker
    drops, then Rule-1 entry buffering) is load-bearing: reordering any of
    these blocks changes which of two competing rules "wins" on an
    ambiguous line, exactly as it would in the awk program.
    """
    out: list[str] = []
    buf: list[str] = []
    buf_resolved = False
    in_resolved_section = False
    in_h3_closure = False
    suppress_orphan_sublines = False

    def flush_buffer() -> None:
        nonlocal buf, buf_resolved
        if not buf_resolved:
            out.extend(buf)
        buf = []
        buf_resolved = False

    def discard_buffer_to_orphan() -> None:
        nonlocal buf, buf_resolved, suppress_orphan_sublines
        if buf:
            buf = []
            buf_resolved = False
            suppress_orphan_sublines = True

    for line in lines:
        # Strip exactly one trailing line terminator for pattern matching;
        # re-attach whatever was stripped when buffering/emitting the line
        # itself (never the bare `content`).
        if line.endswith("\n"):
            content = line[:-1]
        else:
            content = line

        # --- Rule 8: orphan-subline suppression (runs first) ---
        if suppress_orphan_sublines:
            if content.startswith("  "):
                continue
            suppress_orphan_sublines = False
            # fall through

        # --- Section-suppression handlers run first so we exit on boundaries ---
        if in_h3_closure:
            if content.startswith("## ") or content.startswith("### "):
                in_h3_closure = False
                # fall through to normal processing below
            else:
                continue

        if in_resolved_section:
            if content.startswith("## "):
                in_resolved_section = False
                # fall through
            else:
                continue

        # --- Heading-closure detectors (set suppression flags, consume the line) ---
        if _is_h2_closure(content):
            flush_buffer()
            in_resolved_section = True
            continue

        if _is_h3_closure(content):
            flush_buffer()
            in_h3_closure = True
            continue

        # --- Per-line closure-marker drops (Rules 6, 7) ---
        if _is_strikethrough_closure(content) or _is_table_row_closure(content):
            if buf:
                discard_buffer_to_orphan()
            else:
                flush_buffer()
            continue

        # --- Rule 1: entry-shape strip (queue files only) ---
        if apply_rule1:
            is_main = bool(_RE_MAIN_LINE.match(content))
            is_subline = content.startswith("  ")

            if not buf:
                if is_main:
                    buf.append(line)
                else:
                    out.append(line)
                continue

            # buf non-empty — currently gathering an entry
            if is_subline and not is_main:
                # Rule 3: ceremony-line strip.
                if _RE_RECURRING_ZERO.match(content):
                    continue
                if _RE_RESOLUTION_PENDING.match(content):
                    continue
                if _RE_RESOLUTION_IN_PROGRESS.match(content):
                    continue
                buf.append(line)
                if content.startswith("  resolution: "):
                    buf_resolved = True
                elif content.startswith("  **Closeout:**"):
                    buf_resolved = True
                continue

            # Non-sub-line — flush current buffer, then handle this line
            flush_buffer()
            if is_main:
                buf.append(line)
            else:
                out.append(line)
            continue
        else:
            out.append(line)

    if buf:
        flush_buffer()

    return out


def _atomic_replace(path: str, new_lines: list[str]) -> None:
    """Write `new_lines` to `path` atomically via a same-directory tempfile.

    Mirrors the oracle's `mktemp "${INPUT}.tmp.XXXXXX"` + `mv "$TMP" "$INPUT"`
    — a same-directory tempfile guarantees the replace is a same-filesystem
    rename (atomic), which a cross-filesystem tempdir (e.g. Windows TEMP on a
    different drive) would not guarantee. Deliberately NOT
    `tempfile.gettempdir()` — see the migration brief's Windows-portability
    rule; using the target's own directory sidesteps that hazard entirely
    rather than needing the TMPDIR-vs-TEMP ladder.
    """
    target_dir = os.path.dirname(os.path.abspath(path))
    basename = os.path.basename(path)
    fd, tmp_path = tempfile.mkstemp(prefix=f"{basename}.tmp.", dir=target_dir)
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            for line in new_lines:
                # awk's `print` always appends its ORS ("\n") to every
                # emitted record, INCLUDING a final input line that itself
                # lacked a trailing newline — replicate that oracle behavior
                # (a faithful port, not a "fix": the oracle always newline-
                # terminates its last line).
                fh.write(line if line.endswith("\n") else line + "\n")
        os.replace(tmp_path, path)
        declare_write(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            print(f"skip: _atomic_replace: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(f"Usage: {_PROG} <queue-file>", file=sys.stderr)
        return 1

    input_path = argv[0]
    basename = os.path.basename(input_path)

    if basename not in _ALLOWED_BASENAMES:
        print(
            f"ERROR: {_PROG} only operates on improvement-queue.md or "
            f"bug-backlog.md. Refusing to prune: {input_path}",
            file=sys.stderr,
        )
        return 1

    if not os.path.isfile(input_path):
        print(f"ERROR: file not found: {input_path}", file=sys.stderr)
        return 1

    apply_rule1 = basename in _RULE1_BASENAMES

    with open(input_path, "r", newline="", encoding="utf-8") as fh:
        lines = fh.readlines()

    pruned = prune_lines(lines, apply_rule1)
    _atomic_replace(input_path, pruned)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
