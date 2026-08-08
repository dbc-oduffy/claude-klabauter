"""
coordinator_core.ops.generate_exec_summary — Port of: generate-exec-summary.sh
(example-doctrine-repo b5a4192c, 2026-07-20).

Purpose: populate the two MANAGED sections (identity, progress) of a per-repo
docs/exec-summary.md from disk artifacts (README.md/CLAUDE.md,
state/week-changelog/, git log) and preserve the two HAND sections (special,
goals) verbatim across regenerations. Shaped after regenerate-orientation-cache:
git-root resolve, disk derivation, --check flag, no-clobber create.

Spec backlink: docs/plans/2026-07-03-exec-summary-per-repo-brief.md § C2
Spec backlink: docs/wiki/exec-summary-artifact.md § Generator contract

CLI usage (mirrors the bash oracle byte-for-byte):
    generate-exec-summary.sh [--check]

Options:
    --check   Print generated content to stdout without writing to disk.

Exit-code contract (main()), UNCHANGED from the bash oracle — no claude-klabauter-engine
transport call exists inside this module (no cc_invoke / IPC round-trip):
    0  — success (file written, or --check printed).
    1  — fail-loud: not inside a git repository; a HAND fence pair is absent or
         malformed on an existing target (generator refuses to overwrite an
         unparseable HAND region); or the claude-klabauter/state-root resolver failed
         (coordinator_claude_klabauter_root() raised).
    2  — CLI usage error (unknown argument).

Behavior:
    New file (docs/exec-summary.md absent) — creates with MANAGED sections filled
      from disk and HAND sections seeded with placeholder text.
    Existing file — re-derives MANAGED sections from disk; preserves HAND sections
      verbatim. Exits non-zero (fail-loud) if any HAND fence is absent or malformed.

Negative-spec:
    - Does NOT overwrite HAND sections on existing files.
    - Does NOT continue silently if HAND fences are malformed on an existing file.
    - Does NOT add a cockpit-contract entity or bump CONTRACT_VERSION (anti-scope).
    - Does NOT write to disk when --check is passed.
    - MANAGED-section markdown link targets (e.g. `](archive/foo.md)`, or a
      stray `](../archive/foo.md)` harvested from a nested source) are ALL
      treated as repo-root-relative and rewritten to resolve correctly from
      the output file's actual location, docs/exec-summary.md, one directory
      below repo root — e.g. `](../archive/foo.md)`. External URLs, mailto:,
      anchors, and absolute paths are left untouched. HAND sections are left
      untouched entirely (author-owned, not rewritten).
    - Faithful oracle-bug repro: the bash oracle's Rule-5 state-root resolution uses
      `if coordinator_is_meta_repo "$_csr_git_root"; then ... else ... fi` — bash's
      `if` treats ANY non-zero exit (both "false"=1 and "error"=2) as the else
      branch. This means a genuine resolution ERROR inside coordinator_is_meta_repo
      (e.g. HOME unresolvable) is silently swallowed and misclassified as "not the
      meta-repo" rather than propagating as a hard failure. This port reproduces
      that quirk exactly (`meta_repo_identity.MetaRepoResolutionError` caught and
      treated as `is_meta = False`) rather than "fixing" it mid-port — see
      `_resolve_state_root` below.
    - Does NOT reimplement coordinator/lib/coordinator-state-root.py's Rule 1-4
      (--central, --subject, --artifact, --print-map) branches — this script's own
      call site never passes those flags (bare `coordinator_state_root` == Rule 5
      only), so only Rule 5 is ported.
"""

from __future__ import annotations

import glob
import os
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from coordinator_core import claude_klabauter_root as _claude_klabauter_root_mod
from coordinator_core import meta_repo_identity as _meta_repo_identity
from coordinator_core.ops._relative_link import relative_markdown_target
from coordinator_core.session.declared_writes import declare_write

_SUBPROCESS_TIMEOUT_SECS = 10

_HAND_SPECIAL_PLACEHOLDER = (
    "_What sets this project apart from similar efforts. What problems does it solve that nothing else\n"
    "does? Distil the differentiator into 2–4 sentences. The generator preserves this verbatim on\n"
    "every refresh — edit once, it survives regen._"
)

_HAND_GOALS_PLACEHOLDER = (
    "_The 2–4 most important near-term objectives. What does success look like in the next 4–8 weeks?\n"
    "Reference concrete milestones or workstreams where useful. The generator preserves this verbatim\n"
    "on every refresh — edit once, it survives regen._"
)


# ---------------------------------------------------------------------------
# State-root resolution (Rule 5 of coordinator-state-root.py only — see module
# docstring negative-spec for why the other four rules are out of scope here).
# ---------------------------------------------------------------------------

def _resolve_state_root(repo_root: str) -> str:
    """Resolve the coordinator state root for `repo_root`, mirroring Rule 5 of
    coordinator/lib/coordinator-state-root.py (the only rule this script's bare
    `coordinator_state_root` call site exercises — no --central/--subject/--artifact).

    Returns `<claude_klabauter_root>/state` when repo_root IS the coordinator meta-repo,
    else `<repo_root>/state`.

    Raises RuntimeError (coordinator_claude_klabauter_root's own remediation text) when
    repo_root IS the meta-repo but CLAUDE_KLABAUTER_ROOT cannot be resolved — matches the
    bash oracle's fail-loud `|| return 1` on that branch.
    """
    try:
        is_meta = _meta_repo_identity.is_meta_repo(repo_root)
    except _meta_repo_identity.MetaRepoResolutionError:
        # Faithful oracle-bug repro — see module docstring negative-spec.
        is_meta = False

    if is_meta:
        claude_klabauter_root = _claude_klabauter_root_mod.coordinator_claude_klabauter_root()
        return os.path.join(claude_klabauter_root, "state")
    return os.path.join(repo_root, "state")


# ---------------------------------------------------------------------------
# HAND block validation and extraction
# (only called on existing files; new files receive placeholder text)
# ---------------------------------------------------------------------------

def _validate_hand_fences(path: str) -> Tuple[bool, List[str]]:
    """Validate that both HAND fence pairs (special, goals) are present and paired.

    Returns (ok, error_lines). error_lines is empty when ok is True.
    Negative-spec: does NOT write any output or modify any file.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []

    ok = True
    errors: List[str] = []
    for name in ("special", "goals"):
        begin = f"<!-- BEGIN HAND: {name} -->"
        end = f"<!-- END HAND: {name} -->"
        found_begin = any(line == begin for line in lines)
        found_end = any(line == end for line in lines)
        if not (found_begin and found_end):
            errors.append(
                f'ERROR: {path}: HAND fence pair "{name}" is absent or malformed '
                "— aborting; generator will not overwrite unparseable HAND region\n"
            )
            ok = False
    return ok, errors


def _extract_hand(path: str, name: str) -> str:
    """Extract content between a named HAND fence pair, verbatim (excluding fence lines)."""
    begin = f"<!-- BEGIN HAND: {name} -->"
    end = f"<!-- END HAND: {name} -->"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        lines = []

    in_block = False
    out: List[str] = []
    for line in lines:
        if line == begin:
            in_block = True
            continue
        if line == end:
            in_block = False
            continue
        if in_block:
            out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Identity derivation (MANAGED: identity)
# Precedence: README H1 + lead paragraph -> CLAUDE.md first line after H1 -> basename
# ---------------------------------------------------------------------------

def _first_h1(lines: Sequence[str]) -> str:
    """First line matching `/^# /`, with the leading '# ' stripped. Empty if none."""
    for line in lines:
        if line.startswith("# "):
            return line[2:]
    return ""


def _first_nonblank_after_h1(lines: Sequence[str]) -> str:
    """First non-blank line strictly after the first `/^# /` line. Empty if none."""
    found_h1 = False
    for line in lines:
        if not found_h1:
            if line.startswith("# "):
                found_h1 = True
            continue
        if line.strip() != "":
            return line
    return ""


def _read_lines(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        print(f"skip: _read_lines: with open(path, encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return []


def _derive_project_title(repo_root: str) -> str:
    """Short project title (first line of identity, cap 200 chars)."""
    result = ""
    readme = os.path.join(repo_root, "README.md")
    if os.path.isfile(readme):
        result = _first_h1(_read_lines(readme))

    claude_md = os.path.join(repo_root, "CLAUDE.md")
    if not result and os.path.isfile(claude_md):
        result = _first_nonblank_after_h1(_read_lines(claude_md))

    if not result:
        result = os.path.basename(repo_root.rstrip(os.sep))

    result = result.replace("\n", "")
    return result[:200]


def _extract_lead_paragraph(lines: Sequence[str]) -> str:
    """First non-blank paragraph after the H1 (up to the next blank line)."""
    found_h1 = False
    in_para = False
    out: List[str] = []
    for line in lines:
        is_h1 = line.startswith("# ")
        is_blank = line.strip() == ""
        if is_h1:
            found_h1 = True
            continue
        if found_h1 and not in_para and is_blank:
            continue
        if found_h1 and not in_para and not is_blank:
            in_para = True
        if in_para and is_blank:
            break
        if in_para:
            out.append(line)
    return "\n".join(out)


def _derive_identity(repo_root: str) -> str:
    """Full identity block: H1 title + lead paragraph (blank-line separated)."""
    result = ""
    readme = os.path.join(repo_root, "README.md")
    if os.path.isfile(readme):
        readme_lines = _read_lines(readme)
        title = _first_h1(readme_lines)
        if title:
            lead = _extract_lead_paragraph(readme_lines)
            result = title
            if lead:
                result = f"{result}\n\n{lead}"

    claude_md = os.path.join(repo_root, "CLAUDE.md")
    if not result and os.path.isfile(claude_md):
        result = _first_nonblank_after_h1(_read_lines(claude_md))

    if not result:
        result = f"Project at {os.path.basename(repo_root.rstrip(os.sep))}"

    return result


# ---------------------------------------------------------------------------
# Progress derivation (MANAGED: progress)
# Sources: week-changelog Highlights + git-log fallback
#
# C6 (2026-07-30): this used to also read orientation_cache.md's ``## Counters``
# section ("Activity counters") -- that section is retired (the writer now
# emits a purpose map, never a census: see
# coordinator_core.orientation.regenerate_cache's module docstring). The read
# already degraded gracefully to "" on any cache lacking the heading, so this
# is non-fatal, but leaving the dead branch in place would leave it silently
# and permanently degraded rather than repointed -- it is removed outright
# instead, since there is no replacement numeric-activity source to repoint
# it at (the whole point of the rewrite is that a count is not a fact worth
# caching). Highlights and the git-log fallback below are unaffected.
# ---------------------------------------------------------------------------

def _extract_section(text: str, header_line: str) -> str:
    """Extract non-blank lines of a `## <Header>` section, up to (not including)
    the next `## ` header line. Blank lines inside the section are skipped, not
    treated as a terminator (only a new `## ` heading terminates it)."""
    in_section = False
    out: List[str] = []
    for line in text.splitlines():
        if not in_section:
            if line == header_line:
                in_section = True
            continue
        if line.startswith("## "):
            break
        if line.strip() != "":
            out.append(line)
    return "\n".join(out)


def _run_git_log(repo_root: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, "log", "--oneline", "-8"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _run_git_log: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _trim_trailing_blank(text: str) -> str:
    """Trim trailing all-whitespace lines so the fence closes cleanly."""
    lines = text.split("\n")
    last = len(lines)
    while last > 0 and lines[last - 1].strip() == "":
        last -= 1
    return "\n".join(lines[:last])


def _derive_progress(state_root: str, repo_root: str) -> str:
    wc_dir = os.path.join(state_root, "week-changelog")
    output = ""
    highlights = ""

    # --- Highlights from week-changelog (if directory present) ---
    if os.path.isdir(wc_dir):
        for wc_file in sorted(glob.glob(os.path.join(wc_dir, "*.md"))):
            if not os.path.isfile(wc_file):
                continue
            try:
                with open(wc_file, encoding="utf-8", errors="replace") as fh:
                    wc_text = fh.read()
            except OSError:
                print(f"skip: _derive_progress: with open(wc_file, encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
                continue
            candidate = _extract_section(wc_text, "## Highlights")
            if candidate:
                highlights = candidate
                break

        # Fallback: most recent archived pending-release.md.
        if not highlights:
            repo_dir = os.path.dirname(state_root)
            pattern = os.path.join(repo_dir, "archive", "week-changelogs", "**", "*pending-release*.md")
            candidates = sorted(glob.glob(pattern, recursive=True))
            latest_pr = candidates[-1] if candidates else ""
            if latest_pr and os.path.isfile(latest_pr):
                try:
                    with open(latest_pr, encoding="utf-8", errors="replace") as fh:
                        pr_text = fh.read()
                except OSError:
                    pr_text = ""
                highlights = _extract_section(pr_text, "## Highlights")

    # --- Assemble output ---
    if highlights:
        output += f"**Recent highlights:**\n\n{highlights}\n\n"

    # git-log fallback: week-changelog absent OR highlights empty.
    if not os.path.isdir(wc_dir) or not highlights:
        log_out = _run_git_log(repo_root)
        if log_out:
            output += f"**Recent commits:**\n\n```\n{log_out}\n```\n\n"

    if not output:
        output = "_No progress data available. Run `regenerate-orientation-cache` and `/workday-complete` to populate._"

    return _trim_trailing_blank(output)


# ---------------------------------------------------------------------------
# Link rewriting for MANAGED sections
# The output file lives at docs/exec-summary.md — one directory below repo
# root — so any repo-root-relative markdown link target needs relativizing
# to resolve correctly from that location.
# ---------------------------------------------------------------------------

#: Repo-root-relative path of the generated file — the base every MANAGED
#: link target below is relativized against via the shared
#: ``coordinator_core.ops._relative_link`` helper.
_EXEC_SUMMARY_OUT_PATH = "docs/exec-summary.md"


def _rewrite_managed_links(text: str) -> str:
    """Rewrite inline markdown link targets `](TARGET)` to resolve correctly
    from `docs/exec-summary.md`. Leaves external URLs, mailto:, anchors, and
    absolute paths untouched. Every other target is treated as repo-root-
    relative BY CONTRACT (the module docstring's negative-spec) and routed
    through ``relative_markdown_target``, which normalizes away any stray
    leading `./`/`../` before relativizing — MANAGED-section content is
    sometimes harvested verbatim from a nested source (e.g. an archived
    week-changelog's `## Highlights` section), and a target that already
    carries a relative prefix computed for THAT source's own location must
    not be layered with a second relativization on top of it (that was the
    one-`../`-too-many defect: a prior version's skip-list left such targets
    untouched instead of renormalizing them). A bare target and a stray-
    `../`-prefixed target that resolve to the same file now produce the
    identical, correct output.
    Reference-style links and autolinks are out of scope (inline `](...)` only).
    Negative-spec: does NOT touch HAND-section content (never called on it)."""
    out_lines: List[str] = []
    for line in text.splitlines():
        result = ""
        rest = line
        while True:
            idx = rest.find("](")
            if idx == -1:
                result += rest
                break
            result += rest[: idx + 2]
            rest = rest[idx + 2 :]
            close_idx = rest.find(")")
            if close_idx == -1:
                result += rest
                rest = ""
                break
            target = rest[:close_idx]
            skip = (
                target.startswith("http://")
                or target.startswith("https://")
                or target.startswith("mailto:")
                or target.startswith("#")
                or target.startswith("/")
            )
            if not skip and target != "":
                target = relative_markdown_target(target, _EXEC_SUMMARY_OUT_PATH)
            result += target + ")"
            rest = rest[close_idx + 1 :]
        out_lines.append(result)
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# File emission
# ---------------------------------------------------------------------------

def _emit_file(
    project_title: str,
    repo_name: str,
    identity: str,
    hand_special: str,
    hand_goals: str,
    progress: str,
    iso_now: str,
) -> str:
    lines = [
        "---",
        "kind: exec-summary",
        f"repo: {repo_name}",
        f"project: {project_title}",
        f"generated: {iso_now}",
        "generator: bin/generate-exec-summary.sh",
        "---",
        "",
        f"# {project_title} — Executive Summary",
        "",
        '> One-screen "why this project matters" brief. The two MANAGED sections are refreshed by',
        "> `bin/generate-exec-summary.sh` from disk artifacts; the two HAND sections are yours to",
        "> author once and are preserved verbatim on every regeneration.",
        ">",
        "> Spec backlink: docs/wiki/exec-summary-artifact.md",
        "",
        "## What this project is",
        "",
        "<!-- BEGIN MANAGED: identity -->",
        identity,
        "<!-- END MANAGED: identity -->",
        "",
        "## What makes it special",
        "",
        "<!-- BEGIN HAND: special -->",
        hand_special,
        "<!-- END HAND: special -->",
        "",
        "## Near-term goals",
        "",
        "<!-- BEGIN HAND: goals -->",
        hand_goals,
        "<!-- END HAND: goals -->",
        "",
        "## Progress",
        "",
        "<!-- BEGIN MANAGED: progress -->",
        progress,
        "<!-- END MANAGED: progress -->",
    ]
    # Mirrors the bash oracle's `$(...)` command-substitution trailing-newline strip:
    # the caller re-adds exactly one trailing newline at write/print time.
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _resolve_repo_root() -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            check=False,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _resolve_repo_root: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if proc.returncode != 0:
        return None
    root = proc.stdout.strip()
    return root or None


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    check_only = False
    for arg in args:
        if arg == "--check":
            check_only = True
        else:
            sys.stderr.write(f"ERROR: unknown argument: {arg}\n")
            return 2

    repo_root = _resolve_repo_root()
    if repo_root is None:
        sys.stderr.write("ERROR: not inside a git repository\n")
        return 1

    target = os.path.join(repo_root, "docs", "exec-summary.md")

    try:
        state_root = _resolve_state_root(repo_root)
    except RuntimeError as exc:
        sys.stderr.write(str(exc))
        if not str(exc).endswith("\n"):
            sys.stderr.write("\n")
        return 1

    iso_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    repo_name = os.path.basename(repo_root.rstrip(os.sep))
    project_title = _derive_project_title(repo_root)
    identity = _derive_identity(repo_root)
    progress = _derive_progress(state_root, repo_root)
    identity = _rewrite_managed_links(identity)
    progress = _rewrite_managed_links(progress)

    if os.path.isfile(target):
        ok, errors = _validate_hand_fences(target)
        if not ok:
            for err in errors:
                sys.stderr.write(err)
            return 1
        hand_special = _extract_hand(target, "special")
        hand_goals = _extract_hand(target, "goals")
    else:
        hand_special = _HAND_SPECIAL_PLACEHOLDER
        hand_goals = _HAND_GOALS_PLACEHOLDER

    output = _emit_file(project_title, repo_name, identity, hand_special, hand_goals, progress, iso_now)

    if check_only:
        sys.stdout.write(output + "\n")
        # The rendered `generated: <iso_now>` line changes on every run by
        # construction, so a raw byte-for-byte compare against the existing
        # target would report "stale" unconditionally even when nothing else
        # changed. Strip that one line from both sides before comparing --
        # this is the SAME normalization the MANAGED-section HAND-fence
        # extraction already treats as immaterial to freshness.
        def _drop_generated_line(text: str) -> str:
            return "\n".join(
                line for line in text.splitlines() if not line.startswith("generated: ")
            )

        if not os.path.isfile(target):
            sys.stderr.write(
                f"generate-exec-summary: check failed: {target} is absent (would create)\n"
            )
            return 1
        with open(target, "r", encoding="utf-8") as fh:
            existing = fh.read()
        if _drop_generated_line(existing.rstrip("\n")) != _drop_generated_line(output):
            sys.stderr.write(
                f"generate-exec-summary: check failed: {target} is stale (content differs)\n"
            )
            return 1
        sys.stderr.write(f"generate-exec-summary: check: {target} up to date (no-op)\n")
        return 0

    is_new = not os.path.isfile(target)
    if is_new:
        os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(output + "\n")

    # DR-276: declared AFTER the write lands, never before — the contract is a
    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(target)

    if is_new:
        sys.stderr.write(f"generate-exec-summary: created {target}\n")
    else:
        sys.stderr.write(f"generate-exec-summary: updated {target}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
