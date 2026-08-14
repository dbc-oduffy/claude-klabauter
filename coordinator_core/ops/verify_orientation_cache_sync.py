"""
coordinator_core.ops.verify_orientation_cache_sync — schema verifier for
state/orientation_cache.md.

Purpose: the checking half of the orientation-cache authoring-discipline pair
(the writer half is `coordinator_core.orientation.regenerate_cache`).
Enforces the fixed schema documented in
`coordinator/pipelines/workday-start-internals.md § 5.5`: a line ceiling
(`LINE_CEILING`, imported from the writer — see that module's docstring for
the current value and the measurement behind it), a three-key frontmatter
block, an allowlisted heading set, and per-section shape regexes for
Active workstreams / Pinboard / Auto-push health / Trust caveats / Wiki /
Architecture atlas / Fast test / Audits & censuses, plus a
filesystem-detector-regression guard (a `*.uproject` anywhere in the repo
requires a first Trust-caveats line naming "Unreal Engine project detected").

C6 (2026-07-30): ``## Project``, ``## Counters``, and ``## Priorities`` are no
longer schema-legal headings — the writer retired all three (a re-quote of
CLAUDE.md's own first line, and two handoff/spinoff-derived census/answer
sections a skill already recomputes at the moment it matters) in favour of
four ROUTING sections this module now checks instead: Wiki / Architecture
atlas / Fast test / Audits & censuses. Their shape checks are deliberately
loose (a bounded bullet-shaped line) — the writer's own negative-spec (no
counts) is what these sections must honour, not a machine-checkable regex on
the section's prose content.

Port of: verify-orientation-cache-sync.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee
               docs/plans/2026-05-18-orientation-cache-authoring-discipline.md

Division of labor with the DoE trampoline (template-variant #1, direct
import — see docs/wiki/DOE-PORT template): the trampoline resolves
REPO_ROOT / STATE_ROOT / CACHE_FILE (a 5-rule resolver,
`coordinator-state-root.py`, deliberately NOT re-derived here — reused via
subprocess exactly as `coordinator_core.ops` reuses
`coordinator-claude-klabauter-root.sh` for CLAUDE_KLABAUTER_ROOT — re-deriving a 5-rule cross-repo
resolver in a second language is a duplicate-logic drift risk, not a
simplification) and handles `--list` / missing-cache-file (both no-transport,
no-schema-check outcomes). This module owns the schema-check logic only:
`verify()` takes an already-resolved cache path + repo root and returns
(violations, line_count); `main()` is a thin CLI wrapper for direct testing
and for the trampoline's direct-import call.

Exit-code contract (main()) — parity-critical, matches the bash oracle's own
two codes exactly, business-only (the trampoline layers on a THIRD,
transport-failure code — see its own comment block, not duplicated here):
    0 — no violations found
    1 — one or more schema violations found (violations printed to stdout)
    2 — usage error (missing cache_file / repo_root argv)

Negative-spec:
    - Does NOT resolve STATE_ROOT / CACHE_FILE itself — that stays a bash
      responsibility in the trampoline (see division-of-labor note above).
    - Does NOT handle `--list` or the "no cache file — nothing to verify"
      exit-0 path — both are trampoline-side, pre-import concerns.
    - Frontmatter value extraction uses a first-colon split, not awk's
      `-F': *'` global-colon-field split — the two diverge only when a value
      contains an embedded colon, and only `generated_by` is content-checked
      (against `^[a-z0-9-]+$`, which excludes colons outright), so both
      splitting strategies agree on every PASS/FAIL outcome; `generated_at`/
      `git_head_at_generation` are presence-only checks. Not a behavior gap.
    - Workstream line length uses Python `len()` (Unicode code points) where
      the bash oracle uses `${#line}` (byte or code-point count depending on
      locale) — these agree under any UTF-8 locale, which is this codebase's
      standard assumption; not re-deriving locale-dependent byte counting.
    - `*.uproject` detection order (which match is reported when multiple
      exist) is NOT guaranteed identical to `find`'s traversal order — only
      presence/absence is a schema-checked fact; the reported path is
      diagnostic text only, never asserted on.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.orientation.regenerate_cache import (
    LINE_CEILING,
    WORKSTREAM_BODY_CAP,
    WORKSTREAM_MAX,
)
from coordinator_core.win_portability import no_console_creationflags

_CREATIONFLAGS = no_console_creationflags()

_SUBPROCESS_TIMEOUT_SECS = 10

# Must stay in sync with what `orientation.regenerate_cache`'s `_render_cache` actually
# emits — a heading the emitter produces but this tuple omits makes the verifier flag the
# engine's own output as out-of-schema, which is a verifier bug, not a cache defect.
# `Housekeeping` was such an omission: emitted since 2026-07-23 (C17b), added here
# 2026-07-28 after a diagnosis found every compliant cache failing on it.
#
# C6 (2026-07-30): ``Project``/``Counters``/``Priorities`` retired; ``Wiki``/
# ``Architecture atlas``/``Fast test``/``Audits & censuses`` added — see module
# docstring.
ALLOWED_HEADINGS = (
    "Trust caveats",
    "Active workstreams",
    "Rechecks due ≤7 days",
    "Branch",
    "Recent commits",
    "Auto-push health",
    "Wiki",
    "Architecture atlas",
    "Fast test",
    "Audits & censuses",
    "Housekeeping",
    "Pinboard",
)

_WORKSTREAM_RE = re.compile(r"^[0-9]+\. [A-Za-z]")
_PINBOARD_RE = re.compile(r"^- [0-9]{4}-[0-9]{2}-[0-9]{2} [a-z0-9-]+: .{1,120}$")
_AUTOPUSH_RE = re.compile(r"^- ⚠ [0-9]+ unpushed commit")
_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
# Purpose-map pointer shape (C6): every bullet in Wiki/Architecture
# atlas/Fast test/Audits & censuses is "- `<path>` — <prose>" or
# "- <label>: `<cmd>`" — a bounded bullet line, never a bare number. This is
# intentionally loose: the writer's own negative-spec (no counts) is enforced
# by code review of `regenerate_cache.py`'s emit_* probes, not by a regex here
# trying to prove a NEGATIVE (the absence of a digit run would also reject a
# legitimate path segment or command flag that happens to contain one).
_POINTER_RE = re.compile(r"^- .+$")

# WORKSTREAM_BODY_CAP, LINE_CEILING, and WORKSTREAM_MAX are imported above
# from the writer (coordinator_core.orientation.regenerate_cache), not
# redeclared here — that module is the single source of truth for every
# bound that actually constrains what the writer produces (see LINE_CEILING's
# own docstring for the 2026-07-28 reconciliation this replaces: this module
# used to independently declare a stale `_LINE_CEILING = 35`, checked
# post-hoc only, that silently disagreed with the writer's own byte budget).
#
# _PINBOARD_MAX / _AUTOPUSH_MAX / _TRUST_CAVEATS_MAX stay declared HERE, not
# moved: none of them corresponds to a number the writer itself enforces or
# could drift against. The writer's Pinboard/Auto-push-health sections are
# structurally single-line by construction (`_first_line`, a scalar return
# type) rather than parameterized by a count constant, and Trust caveats
# never emits more than one line today — these three are pure shape/count
# assertions this verifier makes about the artifact, with no writer-side
# counterpart to reconcile against.
_PINBOARD_MAX = 1
_AUTOPUSH_MAX = 1
_TRUST_CAVEATS_MAX = 5
_FIND_MAXDEPTH = 6


def is_allowed_heading(heading: str) -> bool:
    return heading in ALLOWED_HEADINGS


def _section_lines(lines: List[str], heading: str, *, bullets_only: bool) -> List[str]:
    """Mirror the bash awk state machine:
    /^## <heading>$/{in_s=1; next} /^## /{in_s=0} in_s && <cond>{print}
    """
    target = "## " + heading
    out: List[str] = []
    in_section = False
    for line in lines:
        if line == target:
            in_section = True
            continue
        if line.startswith("## "):
            in_section = False
            continue
        if in_section:
            if bullets_only:
                if line.startswith("- "):
                    out.append(line)
            else:
                if line.strip() != "":
                    out.append(line)
    return out


def _check_frontmatter(lines: List[str]) -> List[str]:
    violations: List[str] = []
    fm_end: Optional[int] = None
    seen = 0
    for idx, line in enumerate(lines, start=1):
        if line == "---":
            seen += 1
            if seen == 2:
                fm_end = idx
                break
    if fm_end is None or fm_end <= 1:
        violations.append("frontmatter not found (expected --- delimited block at top of file)")
        return violations

    fm_body = lines[1 : fm_end - 1]

    def _extract(key: str) -> str:
        prefix = key + ":"
        for line in fm_body:
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return ""

    generated_by = _extract("generated_by")
    generated_at = _extract("generated_at")
    git_head_at = _extract("git_head_at_generation")

    if not generated_by:
        violations.append("frontmatter missing generated_by")
    elif not _SLUG_RE.match(generated_by):
        violations.append(
            f"frontmatter generated_by='{generated_by}' is not a single lowercase slug "
            "(no parentheticals, no 'patched by' prose allowed)"
        )
    if not generated_at:
        violations.append("frontmatter missing generated_at")
    if not git_head_at:
        violations.append("frontmatter missing git_head_at_generation")
    return violations


def _check_headings(lines: List[str]) -> List[str]:
    violations = []
    for line in lines:
        if line.startswith("## "):
            heading = line[3:]
            if not is_allowed_heading(heading):
                violations.append(f"out-of-schema heading: '## {heading}'")
    return violations


def _check_pointer_section(lines: List[str], heading: str) -> List[str]:
    """Shared shape check for the four C6 purpose-map sections (Wiki,
    Architecture atlas, Fast test, Audits & censuses): every bullet must be a
    bounded ``- ...`` pointer line. See `_POINTER_RE`'s own comment for why
    this stays a loose shape check rather than trying to machine-verify the
    writer's own no-counts negative-spec here."""
    violations = []
    for line in _section_lines(lines, heading, bullets_only=False):
        if not _POINTER_RE.match(line):
            violations.append(f"'## {heading}' line fails bullet shape '- ...': '{line}'")
    return violations


def _check_workstreams(lines: List[str]) -> List[str]:
    violations = []
    ws_lines = _section_lines(lines, "Active workstreams", bullets_only=False)
    for line in ws_lines:
        if not _WORKSTREAM_RE.match(line):
            violations.append(f"workstream line fails name-only regex: '{line}'")
        if len(line) > WORKSTREAM_BODY_CAP:
            # Review: code-reviewer F4 — message now cites the enforced
            # constant instead of a hardcoded "80-char" that had drifted from
            # WORKSTREAM_BODY_CAP=84.
            violations.append(f"workstream line exceeds {WORKSTREAM_BODY_CAP}-char body cap: '{line}'")
    if len(ws_lines) > WORKSTREAM_MAX:
        violations.append(
            f"Active workstreams section has {len(ws_lines)} entries (max {WORKSTREAM_MAX})"
        )
    return violations


def _check_pinboard(lines: List[str]) -> List[str]:
    violations = []
    pb_lines = _section_lines(lines, "Pinboard", bullets_only=True)
    for line in pb_lines:
        if not _PINBOARD_RE.match(line):
            violations.append(
                f"pinboard line fails shape '- YYYY-MM-DD writer-slug: <note>': '{line}'"
            )
    if len(pb_lines) > _PINBOARD_MAX:
        violations.append(
            f"Pinboard section has {len(pb_lines)} lines (max {_PINBOARD_MAX} — one-slot only)"
        )
    return violations


def _check_auto_push_health(lines: List[str]) -> List[str]:
    violations = []
    aph_lines = _section_lines(lines, "Auto-push health", bullets_only=True)
    for line in aph_lines:
        if not _AUTOPUSH_RE.match(line):
            violations.append(
                "Auto-push health line fails shape "
                f"'- ⚠ <N> unpushed commit(s) ...': '{line}'"
            )
    if len(aph_lines) > _AUTOPUSH_MAX:
        violations.append(
            f"Auto-push health section has {len(aph_lines)} lines (max {_AUTOPUSH_MAX})"
        )
    return violations


def _check_trust_caveats(lines: List[str]) -> Tuple[List[str], List[str], str]:
    violations = []
    tc_lines = _section_lines(lines, "Trust caveats", bullets_only=True)
    tc_first = tc_lines[0] if tc_lines else ""
    if len(tc_lines) > _TRUST_CAVEATS_MAX:
        violations.append(
            f"Trust caveats section has {len(tc_lines)} lines (max {_TRUST_CAVEATS_MAX})"
        )
    return violations, tc_lines, tc_first


def _git_ls_files_uproject(repo_root: str) -> Optional[str]:
    """Caller MUST already have confirmed repo_root is inside a git work tree
    (see `_has_git_worktree`) — not re-checked here to avoid a duplicate
    subprocess spawn per invocation."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "ls-files", "*.uproject"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _git_ls_files_uproject: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.strip().splitlines()
    if not first:
        return None
    # Forward-slash on purpose: this path is a WIRE VALUE — it is interpolated
    # into the violation message this op emits, and the bash oracle emitted
    # `git ls-files` output, which is always forward-slash. os.path.join would
    # hand back a backslash form on Windows and break byte-parity with the
    # oracle. Same normalization as _find_uproject's return.
    return Path(repo_root, first[0]).as_posix()


def _find_uproject(repo_root: str, *, exclude_git_and_node_modules: bool) -> Optional[str]:
    root_depth = repo_root.rstrip(os.sep).count(os.sep)
    matches: List[str] = []
    for dirpath, dirnames, filenames in os.walk(repo_root):
        depth = dirpath.rstrip(os.sep).count(os.sep) - root_depth
        if depth >= _FIND_MAXDEPTH:
            dirnames[:] = []
        if exclude_git_and_node_modules:
            dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git")]
        for fname in filenames:
            if fname.endswith(".uproject"):
                matches.append(os.path.join(dirpath, fname))
        if matches:
            break
    # as_posix for the same wire-value reason as _git_ls_files_uproject above:
    # the winning match is interpolated into the emitted violation text.
    return Path(matches[0]).as_posix() if matches else None


def _detect_uproject(repo_root: str) -> Optional[str]:
    """Mirror the bash three-branch detector exactly (git-ls-files, then
    excluded-find fallback when git is present-but-empty, or unfiltered-find
    when git is entirely unavailable/not-a-worktree).
    """
    has_git = _has_git_worktree(repo_root)
    if has_git:
        found = _git_ls_files_uproject(repo_root)
        if found:
            return found
        return _find_uproject(repo_root, exclude_git_and_node_modules=True)
    return _find_uproject(repo_root, exclude_git_and_node_modules=False)


def _has_git_worktree(repo_root: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _has_git_worktree: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return result.returncode == 0


def _check_uproject_guard(
    repo_root: str, tc_lines: List[str], tc_first: str
) -> List[str]:
    violations = []
    uproject_path = _detect_uproject(repo_root)
    if uproject_path:
        if not tc_lines:
            violations.append(
                "detector-output missing: *.uproject present in repo "
                f"({uproject_path}) but ## Trust caveats section absent — "
                "regenerate via bin/regenerate-orientation-cache.py"
            )
        elif "Unreal Engine project detected" not in tc_first:
            violations.append(
                "detector-output corrupted: *.uproject present but first Trust "
                f"caveats line does not contain 'Unreal Engine project detected': '{tc_first}'"
            )
    return violations


def verify(cache_path: str, repo_root: str) -> Tuple[List[str], int]:
    """Run every schema check against ``cache_path``.

    Returns (violations, line_count). line_count mirrors `wc -l` exactly
    (counts newline bytes, not "number of lines of content").
    """
    with open(cache_path, "rb") as fh:
        raw = fh.read()
    line_count = raw.count(b"\n")
    text = raw.decode("utf-8", errors="replace")
    # Strip CRLF line endings before splitting so schema regexes (many are
    # anchored with `$` or an exact tail match) see clean content on both
    # Windows-authored (CRLF) and POSIX-authored (LF) cache files. line_count
    # above is computed from the raw byte count on purpose and stays
    # `wc -l`-parity even though CRLF halves the byte-level newline shape.
    lines = text.replace("\r\n", "\n").split("\n")

    violations: List[str] = []

    if line_count > LINE_CEILING:
        violations.append(f"file length {line_count} exceeds {LINE_CEILING}-line ceiling")

    violations.extend(_check_frontmatter(lines))
    violations.extend(_check_headings(lines))
    violations.extend(_check_workstreams(lines))
    for heading in ("Wiki", "Architecture atlas", "Fast test", "Audits & censuses"):
        violations.extend(_check_pointer_section(lines, heading))
    violations.extend(_check_pinboard(lines))
    violations.extend(_check_auto_push_health(lines))
    tc_violations, tc_lines, tc_first = _check_trust_caveats(lines)
    violations.extend(tc_violations)
    violations.extend(_check_uproject_guard(repo_root, tc_lines, tc_first))

    return violations, line_count


def main(argv: List[str]) -> int:
    """CLI entry: ``verify_orientation_cache_sync.py <cache_file> <repo_root>``.

    Assumes the caller (the DoE trampoline) has already confirmed
    ``cache_file`` exists and resolved ``repo_root`` — see module docstring's
    division-of-labor note.
    """
    if len(argv) < 2:
        print(
            "verify-orientation-cache-sync: usage: <cache_file> <repo_root>",
            file=sys.stderr,
        )
        return 2

    cache_path, repo_root = argv[0], argv[1]
    violations, line_count = verify(cache_path, repo_root)

    if violations:
        print(f"verify-orientation-cache-sync: FAIL ({cache_path})", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "Fix: regenerate the cache via bin/regenerate-orientation-cache "
            "(do NOT patch in place).",
            file=sys.stderr,
        )
        return 1

    print(f"verify-orientation-cache-sync: OK ({cache_path}, {line_count} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
