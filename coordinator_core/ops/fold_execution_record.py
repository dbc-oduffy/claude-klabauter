"""
coordinator_core.ops.fold_execution_record — ported from
coordinator/bin/coordinator-fold-execution-record (DOE-PORT bin-entrypoint variant,
bash-kill campaign).

Purpose: given a plan file, reads all state/subagent-share/*/<plan-slug>.<chunk-id>.md
run-report sidecars (the universal subsume of the retired flight-recorder — see
docs/plans/2026-07-13-subagent-run-report-subsume.md C6) and emits two markdown blocks
to stdout:
  Part A — "## Execution Observations": one entry per sidecar, keyed by chunk-id
            resolved to the AC/segment it targets (from the plan's ## Chunks section).
  Part B — "## Completion Entry Prose": a suggested past-tense TITLE + <=8-sentence
            BODY synthesised from the observations + an optional --desc one-liner.

The prose synthesis (Part B) is a judgment step. This module gathers and structures
the raw material mechanically and emits it in a clearly-labelled block. If sidecars
are present but no chunk-to-AC mapping is resolvable, the block is emitted with the
raw chunk-id so a downstream Sonnet compose step can fill in the AC annotation.

Mechanical parts: frontmatter parse, chunk->AC map extraction, sidecar body assembly.
Judgment parts: prose title + body composition (emitted as structured material for
downstream).

Usage:
    fold_execution_record.main(["--plan", "<path>", "--desc", "<one-liner>"])

Exit codes:
  0 — normal exit (output emitted, or no sidecars found -> skip signal on stdout)
  1 — argument / validation error

SKIP sentinel — callers MUST check stdout for a line matching:
  <!-- coordinator-fold-execution-record: SKIP
before appending stdout to a plan file. Scripted pipelines that blindly redirect
stdout (>> plan) will silently write the SKIP comment into the plan body. Hard-error
SKIPs (plan-not-found, invalid-slug, repo-root-fail) also write a diagnostic to
stderr. Soft SKIPs (no subagent-share dir, no sidecars, trivial observations) write
only to stdout; exit is always 0 for all SKIPs.

NEVER commits or stages files. NEVER edits the plan or any sidecar.

Sidecar addressing (DR-047 contract-derived, NOT a example-doctrine-repo-side path reconstruction):
each chunk sidecar lives at state/subagent-share/<session-id>/<provision_key>.md per
Claude-klabauter's landed CONTRACT.md (coordinator_core/subagent_sandbox/CONTRACT.md
§ Provision-and-emit contract). provision_key is the SAME pre-flattened
"<plan-slug>.<chunk-id>" token fan-out-dispatch.py passes at provision time (join on
".", per DEC-6) — this module reconstructs that exact token per known chunk-id and
globs for it under every session directory (the session-id leaf is not known at fold
time, so the session segment is a wildcard). Constructing a DIFFERENT key here would
silently mislocate every sidecar and fail-open SKIP.

plan_slug derivation contract (CRITICAL SEMANTIC COUPLING): this module and
coordinator/bin/fan-out-dispatch.py derive plan_slug via the IDENTICAL idiom — strip
a leading "YYYY-MM-DD-" date prefix from the plan's basename via regex
`^[0-9]{4}-[0-9]{2}-[0-9]{2}-`, then strip a trailing ".md" suffix. This equivalence
is asserted by example-doctrine-repo's coordinator/tests/run-report-provision-key-flattening.bats
(parity test), which greps both scripts for their respective idiom fragments — same
input MUST yield a byte-identical slug on both sides, or provisioning (write side)
and folding (read side) silently mislocate each other's sidecars.

Negative-spec (faithfully reproduced bash-oracle behavior — do NOT "fix" mid-port):
    - Trailing-whitespace-only "---" lines in a sidecar do NOT close frontmatter
      (the oracle's `/^---$/` requires an exact line match); this port preserves
      that via exact-equality comparison against a de-line-ended line, not a
      whitespace-tolerant match.
    - The frontmatter `chunk:` value's surrounding quote-stripping is a single
      independent strip on each side (matching the oracle's `gsub(/^["']|["']$/,
      "")`), not a paired-quote parse — an unmatched leading quote with no trailing
      quote (or vice versa) is still stripped on the side it appears.
    - Blank-trim only removes leading/trailing blank lines from an observations
      body; interior blank lines are preserved verbatim.
    - This module never resolves symlinks when falling back to the
      grandparent-of-plan-dir repo-root heuristic (mirrors the oracle's logical
      `cd && pwd`, not a physical/resolved path).

Spec backlink: (inline — no plan doc yet on the example-doctrine-repo side; this module inherits that
posture from its bash oracle, which names itself as the spec surface)
Prior bash implementation: coordinator/bin/coordinator-fold-execution-record.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

_PROG = "coordinator-fold-execution-record"

_DATE_PREFIX_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}-")
_SLUG_VALID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Review: code-reviewer — re.ASCII constrains \s to the same [\t\n\x0b\f\r ]
# class as bash's POSIX [[:space:]], matching the oracle rather than Python's
# default Unicode-whitespace superset (e.g. NEL, LINE/PARAGRAPH SEPARATOR).
_CHUNKS_SECTION_ENTER_RE = re.compile(r"^##\s+Chunks", re.ASCII)
_HEADING2_RE = re.compile(r"^##\s", re.ASCII)
_CHUNK_HEADING_RE = re.compile(r"^###\s+(C[0-9]+[a-z-]*)\s*[—-]\s*(.+)$", re.ASCII)
_TRIVIAL_OBS_RE = re.compile(r"^(n/a|none|—|-|na)$")


def _derive_plan_slug(plan_path: str) -> str:
    """Strip leading YYYY-MM-DD- and trailing .md — the shared idiom with
    fan-out-dispatch.py's `_provision_sidecars` (see module docstring)."""
    plan_basename = os.path.basename(plan_path)
    plan_slug = _DATE_PREFIX_RE.sub("", plan_basename)
    if plan_slug.endswith(".md"):
        plan_slug = plan_slug[:-3]
    return plan_slug


def _resolve_git_root(plan_path: str) -> str:
    """Mirror the bash oracle's two-rung repo-root resolver: `git rev-parse
    --show-toplevel` from the plan's directory, then a logical (non-symlink-
    resolving) grandparent-of-plan-dir fallback."""
    plan_dir = os.path.dirname(plan_path) or "."
    try:
        from coordinator_core.win_portability import no_console_creationflags

        proc = subprocess.run(
            ["git", "-C", plan_dir, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
        if proc.returncode == 0:
            candidate = proc.stdout.strip()
            if candidate:
                return candidate
    except OSError:
        pass

    fallback = os.path.normpath(os.path.join(plan_dir, "..", ".."))
    fallback = os.path.abspath(fallback)
    if os.path.isdir(fallback):
        return fallback
    return ""


def _parse_chunk_ac_map(plan_text: str) -> Dict[str, str]:
    """Parse the plan's ## Chunks section for `### C<N>[suffix] — <label>`
    subsection headings, returning {lowercased chunk-id: trimmed AC label}."""
    chunk_ac_map: Dict[str, str] = {}
    in_chunks_section = False
    # Review: code-reviewer — str.splitlines() treats a wider set of Unicode
    # characters as line boundaries (e.g. NEL, LINE/PARAGRAPH SEPARATOR) than
    # bash's `while IFS= read -r line` (splits on \n only). split("\n") mirrors
    # the oracle's newline-only semantics.
    for line in plan_text.split("\n"):
        if _CHUNKS_SECTION_ENTER_RE.match(line):
            in_chunks_section = True
            continue
        if _HEADING2_RE.match(line):
            in_chunks_section = False
            continue
        if not in_chunks_section:
            continue
        m = _CHUNK_HEADING_RE.match(line)
        if m:
            chunk_key = m.group(1).lower()
            ac_label = m.group(2).rstrip()
            chunk_ac_map[chunk_key] = ac_label
    return chunk_ac_map


def _extract_frontmatter_chunk(lines: List[str]) -> str:
    """Extract the `chunk:` frontmatter value between the first and second exact
    "---" lines; first match wins, unmatched runs return "" (fallback to
    filename-derived chunk-id lives in the caller)."""
    n = 0
    for line in lines:
        if line == "---":
            n += 1
            if n == 2:
                break
            continue
        if n == 1 and line.startswith("chunk:"):
            val = line[len("chunk:"):]
            val = val.lstrip()
            if val[:1] in ("'", '"'):
                val = val[1:]
            if val[-1:] in ("'", '"'):
                val = val[:-1]
            return val
    return ""


def _extract_observations_body(lines: List[str]) -> List[str]:
    """Extract lines under a `## Observations` heading (prefix-matched, so
    `## Observations & Notes` variants qualify) until the next `## ` heading."""
    in_obs = False
    out: List[str] = []
    for line in lines:
        if line.startswith("## Observations"):
            in_obs = True
            continue
        if in_obs and line.startswith("## "):
            in_obs = False
            continue
        if in_obs:
            out.append(line)
    return out


def _trim_blank_lines(lines: List[str]) -> List[str]:
    """Drop leading blank lines (before the first non-whitespace line) and
    trailing blank lines; interior blank lines are preserved verbatim."""
    trimmed: List[str] = []
    found = False
    for line in lines:
        if line.strip():
            found = True
        if found:
            trimmed.append(line)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def _is_trivial_observation(obs_body: str) -> bool:
    """True if `obs_body` is empty/whitespace-only or one of the oracle's
    recognized trivial tokens (n/a, none, em-dash, hyphen)."""
    # Review: code-reviewer — sibling of the :92-94 \s-regex finding: mirrors
    # the oracle's `${obs_body//[[:space:]]/}` (ASCII-only ${space} class), not
    # Python's default Unicode-whitespace \s.
    trimmed = re.sub(r"\s+", "", obs_body, flags=re.ASCII)
    if not trimmed:
        return True
    return bool(_TRIVIAL_OBS_RE.match(trimmed.lower()))


def _title_case_slug(plan_slug: str) -> str:
    """Mirror the bash oracle's awk title-case: replace '-' with ' ', split on
    whitespace (collapsing/trimming runs), uppercase each word's first char and
    leave the remainder of the word unchanged."""
    words = plan_slug.replace("-", " ").split()
    return " ".join(w[:1].upper() + w[1:] for w in words)


def _collect_sidecar_files(subagent_share_dir: str, plan_slug: str) -> List[str]:
    """Mirror `find <dir> -mindepth 2 -maxdepth 2 -name "<slug>.*.md" | sort`:
    exactly one level of session directories under subagent-share/, matched
    files sorted by full path for deterministic output."""
    pattern = f"{plan_slug}.*.md"
    matches: List[str] = []
    try:
        session_entries = list(os.scandir(subagent_share_dir))
    except OSError:
        return matches
    for session_entry in session_entries:
        if not session_entry.is_dir():
            continue
        try:
            file_entries = list(os.scandir(session_entry.path))
        except OSError:
            continue
        for file_entry in file_entries:
            # Review: code-reviewer — fnmatch.fnmatch() case-normalizes via
            # os.path.normcase() (no-op on POSIX, but case-insensitive on Windows),
            # diverging from the oracle's always-case-sensitive `find -name`.
            # fnmatchcase() forces byte-consistent matching on every platform.
            if file_entry.is_file() and fnmatch.fnmatchcase(file_entry.name, pattern):
                matches.append(file_entry.path)
    matches.sort()
    return matches


def _parse_args(argv: List[str]) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Return (plan_path, desc_flag, error_exit_code). error_exit_code is None on
    success; when set, the caller has already written the diagnostic to stderr."""
    plan_path: Optional[str] = None
    desc_flag: Optional[str] = None
    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--plan":
            if i + 1 >= n or argv[i + 1] == "":
                sys.stderr.write(f"{_PROG}: --plan requires an argument\n")
                return None, None, 1
            plan_path = argv[i + 1]
            i += 2
            continue
        if arg == "--desc":
            if i + 1 >= n or argv[i + 1] == "":
                sys.stderr.write(f"{_PROG}: --desc requires an argument\n")
                return None, None, 1
            desc_flag = argv[i + 1]
            i += 2
            continue
        if arg == "--":
            break
        if arg.startswith("-"):
            sys.stderr.write(f"{_PROG}: unknown flag: {arg}\n")
            return None, None, 1
        sys.stderr.write(f"{_PROG}: unexpected positional argument: {arg}\n")
        return None, None, 1

    if not plan_path:
        sys.stderr.write(f"Usage: {_PROG} --plan <path> [--desc <one-liner>]\n")
        return None, None, 1

    return plan_path, desc_flag, None


def main(argv: List[str]) -> int:
    """CLI entrypoint — see module docstring for the full contract (flags,
    exit codes, SKIP sentinel)."""
    plan_path, desc_flag, err = _parse_args(argv)
    if err is not None:
        return err
    assert plan_path is not None

    if not os.path.isfile(plan_path) or not os.access(plan_path, os.R_OK):
        sys.stderr.write(f"{_PROG}: plan not found or unreadable: {plan_path}\n")
        print("<!-- coordinator-fold-execution-record: SKIP plan-not-found -->")
        return 0

    plan_slug = _derive_plan_slug(plan_path)
    if not plan_slug:
        sys.stderr.write(f"{_PROG}: could not derive plan-slug from: {plan_path}\n")
        print("<!-- coordinator-fold-execution-record: SKIP slug-derivation-failed -->")
        return 0

    if not _SLUG_VALID_RE.match(plan_slug):
        sys.stderr.write(
            f"{_PROG}: invalid plan-slug derived from filename: {plan_slug}\n"
        )
        print("<!-- coordinator-fold-execution-record: SKIP invalid-slug -->")
        return 0

    git_root = _resolve_git_root(plan_path)
    if not git_root or not os.path.isdir(git_root):
        sys.stderr.write(
            f"{_PROG}: cannot resolve repo root from plan path: {plan_path}\n"
        )
        print("<!-- coordinator-fold-execution-record: SKIP repo-root-unresolvable -->")
        return 0

    subagent_share_dir = os.path.join(git_root, "state", "subagent-share")
    if not os.path.isdir(subagent_share_dir):
        print(
            "<!-- coordinator-fold-execution-record: SKIP no-subagent-share-dir "
            "state/subagent-share/ -->"
        )
        return 0

    sidecar_files = _collect_sidecar_files(subagent_share_dir, plan_slug)
    if not sidecar_files:
        print(
            "<!-- coordinator-fold-execution-record: SKIP no-sidecars-in "
            f"state/subagent-share/*/{plan_slug}.*.md -->"
        )
        return 0

    with open(plan_path, "r", encoding="utf-8", errors="replace") as fh:
        plan_text = fh.read()
    chunk_ac_map = _parse_chunk_ac_map(plan_text)

    chunk_ids: List[str] = []
    obs_bodies: List[str] = []

    for sidecar in sidecar_files:
        if not os.path.isfile(sidecar) or not os.access(sidecar, os.R_OK):
            continue
        try:
            with open(sidecar, "r", encoding="utf-8", errors="replace") as fh:
                # Review: code-reviewer — split("\n") only, mirroring the
                # oracle's newline-only line-splitting (see _parse_chunk_ac_map).
                lines = fh.read().split("\n")
        except OSError:
            continue

        sidecar_chunk = _extract_frontmatter_chunk(lines)
        if not sidecar_chunk:
            base = os.path.basename(sidecar)
            if base.endswith(".md"):
                base = base[:-3]
            prefix = plan_slug + "."
            if base.startswith(prefix):
                base = base[len(prefix):]
            sidecar_chunk = base

        obs_lines = _trim_blank_lines(_extract_observations_body(lines))
        obs_body = "\n".join(obs_lines)

        if _is_trivial_observation(obs_body):
            continue

        chunk_ids.append(sidecar_chunk)
        obs_bodies.append(obs_body)

    if not chunk_ids:
        print("<!-- coordinator-fold-execution-record: SKIP all-observations-trivial -->")
        return 0

    parts: List[str] = []

    parts.append("## Execution Observations\n\n")
    parts.append(
        "*Generated by coordinator-fold-execution-record from "
        f"state/subagent-share/*/{plan_slug}.*.md*\n\n"
    )

    for cid, obs in zip(chunk_ids, obs_bodies):
        cid_lc = cid.lower()
        ac_label = chunk_ac_map.get(cid_lc, "")
        if ac_label:
            heading = f"{cid.upper()} — {ac_label}"
        else:
            heading = f"{cid.upper()} (chunk-to-AC mapping not resolved — annotate manually)"
        parts.append(f"### {heading}\n\n")
        parts.append(f"{obs}\n\n")

    parts.append("## Completion Entry Prose\n\n")
    parts.append(
        "<!-- JUDGMENT NOTE: TITLE and BODY below are structured raw material.\n"
        "     Shell assembly is mechanical. A downstream Sonnet compose step\n"
        "     should rewrite these into polished past-tense prose if needed. -->\n\n"
    )

    slug_title_cased = _title_case_slug(plan_slug)
    if desc_flag:
        candidate_title = desc_flag
    else:
        candidate_title = f"[JUDGMENT STEP — suggest title from: {slug_title_cased}]"

    parts.append(f"**TITLE (past-tense one-liner):** {candidate_title}\n\n")
    parts.append("**BODY (≤8 sentences — compose from observations below):**\n\n")

    for cid, obs in zip(chunk_ids, obs_bodies):
        cid_lc = cid.lower()
        ac_label = chunk_ac_map.get(cid_lc, cid.upper())
        parts.append(f"[{cid.upper()} — {ac_label}]\n")
        parts.append(f"{obs}\n\n")

    if desc_flag:
        parts.append(f"[Caller-supplied description]\n{desc_flag}\n\n")

    parts.append("<!-- END coordinator-fold-execution-record output -->\n")

    sys.stdout.write("".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
