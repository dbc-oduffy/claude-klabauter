"""
coordinator_core.ops.generate_exec_summary — Port of: generate-exec-summary.sh
(DoE b5a4192c, 2026-07-20).

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
         (coordinator_claude_klabauter_root_with_class() raised, including the
         published-engine-mirror refusal reused from
         coordinator_core.state_root._claude_klabauter_state).
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
    - Does NOT refuse to regenerate a MANAGED section whose existing content
      looks hand-edited (a MANAGED fence is always fully refreshed, same as
      any other run) — it only warns to stderr first, so the overwrite is
      visible rather than a silent loss of narrative nobody had a chance to
      salvage. See `_validate_managed_shape`.
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
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
import sys
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from coordinator_core import meta_repo_identity as _meta_repo_identity
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops._relative_link import relative_markdown_target
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.state_root import StateRootError
from coordinator_core.state_root import _claude_klabauter_state as _guarded_claude_klabauter_state

# names itself, mirroring the sibling CLI trampoline's own GENERATES entry
GENERATES = [
    {
        "artifact": "docs/exec-summary.md",
        "stamp_key": "generated",
        "sources": ["coordinator_core/ops/generate_exec_summary.py"],
    },
]

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


def _resolve_state_root(repo_root: str) -> str:
    """Resolve the coordinator state root for `repo_root`, mirroring Rule 5 of
    coordinator/lib/coordinator-state-root.py (the only rule this script's bare
    `coordinator_state_root` call site exercises — no --central/--subject/--artifact).

    Returns `<claude_klabauter_root>/state` when repo_root IS the coordinator meta-repo,
    else `<repo_root>/state`.

    Raises RuntimeError (coordinator_claude_klabauter_root's own remediation text) when
    repo_root IS the meta-repo but CLAUDE_KLABAUTER_ROOT cannot be resolved — matches the
    bash oracle's fail-loud `|| return 1` on that branch. Also raises
    RuntimeError (as `coordinator_core.state_root.StateRootError`, itself a
    RuntimeError subclass) when the meta-repo branch resolves to a PUBLISHED
    engine mirror rather than a live working tree — this reuses
    `coordinator_core.state_root._claude_klabauter_state`'s own published-mirror guard
    (class-AWARE `coordinator_claude_klabauter_root_with_class`, not the class-less
    `coordinator_claude_klabauter_root` this function used to call directly) rather than
    reimplementing the check; see commit 5dedf53b9, which added that guard to
    `state_root.py` and this call site was found to have been missed by.
    """
    try:
        is_meta = _meta_repo_identity.is_meta_repo(repo_root)
    except _meta_repo_identity.MetaRepoResolutionError:
        is_meta = False

    if is_meta:
        return _guarded_claude_klabauter_state()
    return os.path.join(repo_root, "state")


def _validate_hand_fences(path: str) -> Tuple[bool, List[str]]:
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


_MANAGED_INLINE_CITATION_RE = re.compile(r"\*\([^)\n]*\)\*")


def _validate_managed_shape(path: str) -> List[str]:
    warnings: List[str] = []
    for name in ("identity", "progress"):
        content = _extract_managed(path, name)
        if _MANAGED_INLINE_CITATION_RE.search(content):
            warnings.append(
                f'WARNING: {path}: MANAGED: {name} content does not match '
                "generator-producible shape (looks hand-edited) — overwriting with regenerated content\n"
            )
    return warnings


def _extract_managed(path: str, name: str) -> str:
    begin = f"<!-- BEGIN MANAGED: {name} -->"
    end = f"<!-- END MANAGED: {name} -->"
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


def _extract_hand(path: str, name: str) -> str:
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


def _first_h1(lines: Sequence[str]) -> str:
    for line in lines:
        if line.startswith("# "):
            return line[2:]
    return ""


def _strip_leading_noise(lines: Sequence[str]) -> List[str]:
    out = list(lines)
    idx = 0
    in_comment = False
    while idx < len(out):
        stripped = out[idx].strip()
        if in_comment:
            if "-->" in stripped:
                in_comment = False
            idx += 1
            continue
        if stripped == "":
            idx += 1
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            idx += 1
            continue
        if stripped.startswith("![") or stripped.startswith("[!["):
            idx += 1
            continue
        break
    return out[idx:]


def _first_nonblank_after_h1(lines: Sequence[str]) -> str:
    after: Optional[List[str]] = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            after = list(lines[i + 1 :])
            break
    if after is None:
        return ""
    for line in _strip_leading_noise(after):
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
    after: Optional[List[str]] = None
    for i, line in enumerate(lines):
        if line.startswith("# "):
            after = list(lines[i + 1 :])
            break
    if after is None:
        return ""
    out: List[str] = []
    for line in _strip_leading_noise(after):
        if line.strip() == "":
            if out:
                break
            continue
        out.append(line)
    return "\n".join(out)


def _derive_identity(repo_root: str) -> str:
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


def _extract_section(text: str, header_line: str) -> str:
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


def _extract_last_section_by_prefix(text: str, header_prefix: str) -> str:
    out: List[str] = []
    current: List[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            if in_section:
                out = current
            current = []
            in_section = line.startswith(header_prefix)
            continue
        if in_section and line.strip() != "":
            current.append(line)
    if in_section:
        out = current
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
    lines = text.split("\n")
    last = len(lines)
    while last > 0 and lines[last - 1].strip() == "":
        last -= 1
    return "\n".join(lines[:last])


_MAX_PROGRESS_INPUT_AGE_DAYS = 14

_ISO_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _date_from_path(path: str) -> Optional[datetime]:
    for component in (os.path.basename(path), os.path.basename(os.path.dirname(path))):
        match = _ISO_DATE_RE.search(component)
        if not match:
            continue
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _is_stale_input(path: str, now: Optional[datetime] = None) -> bool:
    authored = _date_from_path(path)
    if authored is None:
        return False
    reference = now or datetime.now(timezone.utc)
    return (reference - authored).days > _MAX_PROGRESS_INPUT_AGE_DAYS


def _newest_pending_release(repo_dir: str) -> str:
    """The freshest archived pending-release accumulator, across BOTH archive
    directories that doctrinally hold one.

    They are the same accumulator at two points in one lifecycle, not a drift:
    week-rollover archives the live `state/week-changelog/` fragments into
    `archive/week-changelogs/<week-start>/`, and `/merging-to-main` Step 10
    `git mv`s the accumulator into `archive/release-notes/` once a release tag
    is cut. Reading only the former meant **cutting a release was the act that
    moved a repo's freshest Highlights out of the only directory this reader
    looked in** — so the repos shipping most regularly were exactly the ones
    whose Progress section froze, and the tile made the repos that ship least
    look the most current. Ruling: widen the reader, move no writer
    (doe-claude-em, 2026-09-04, cross-repo/inbox
    `exec-summary-progress-rung2-archive-contract-ruling`).

    Selection is by AUTHORED DATE, never list position. `sorted(...)[-1]` over
    a merged list is wrong twice: it orders lexically across two different
    directory prefixes, and only `release-notes/` names carry the date in the
    filename — `week-changelogs/` entries may carry it only on the parent
    directory. `_date_from_path` already reads both shapes.

    On an exact date tie, `archive/release-notes/` wins: it is the post-merge
    copy, so it is the one whose contents were actually released.
    """
    roots = (
        os.path.join(repo_dir, "archive", "release-notes"),
        os.path.join(repo_dir, "archive", "week-changelogs"),
    )
    best = ""
    best_key: Optional[Tuple[datetime, int]] = None
    for rank, root in enumerate(roots):
        tie_break = 1 - rank
        for path in glob.glob(os.path.join(root, "**", "*pending-release*.md"), recursive=True):
            if not os.path.isfile(path):
                continue
            authored = _date_from_path(path)
            key = (authored or datetime.min.replace(tzinfo=timezone.utc), tie_break)
            if best_key is None or key > best_key:
                best_key = key
                best = path
    return best


def _derive_progress(state_root: str, repo_root: str) -> str:
    wc_dir = os.path.join(state_root, "week-changelog")
    output = ""
    highlights = ""

    if os.path.isdir(wc_dir):
        for wc_file in sorted(glob.glob(os.path.join(wc_dir, "*.md")), reverse=True):
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
                if _is_stale_input(wc_file):
                    break
                highlights = candidate
                break

        if not highlights:
            repo_dir = os.path.dirname(state_root)
            latest_pr = _newest_pending_release(repo_dir)
            if latest_pr and _is_stale_input(latest_pr):
                latest_pr = ""
            if latest_pr and os.path.isfile(latest_pr):
                try:
                    with open(latest_pr, encoding="utf-8", errors="replace") as fh:
                        pr_text = fh.read()
                except OSError:
                    pr_text = ""
                highlights = _extract_section(pr_text, "## Highlights")
                if not highlights:
                    highlights = _extract_last_section_by_prefix(pr_text, "## Week of ")

    if highlights:
        output += f"**Recent highlights:**\n\n{highlights}\n\n"

    if not os.path.isdir(wc_dir) or not highlights:
        log_out = _run_git_log(repo_root)
        if log_out:
            output += f"**Recent commits:**\n\n```\n{log_out}\n```\n\n"

    if not output:
        output = "_No progress data available. Run `regenerate-orientation-cache` and `/workday-complete` to populate._"

    return _trim_trailing_blank(output)


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
        "generator: coordinator/bin/generate-exec-summary.py",
        "---",
        "",
        f"# {project_title} — Executive Summary",
        "",
        '> One-screen "why this project matters" brief. The two MANAGED sections are refreshed by',
        "> `coordinator/bin/generate-exec-summary.py` from disk artifacts; the two HAND sections are yours to",
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
    return "\n".join(lines)


def _resolve_repo_root() -> Optional[str]:
    root = show_toplevel()
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
        for warning in _validate_managed_shape(target):
            sys.stderr.write(warning)
        hand_special = _extract_hand(target, "special")
        hand_goals = _extract_hand(target, "goals")
    else:
        hand_special = _HAND_SPECIAL_PLACEHOLDER
        hand_goals = _HAND_GOALS_PLACEHOLDER

    output = _emit_file(project_title, repo_name, identity, hand_special, hand_goals, progress, iso_now)

    if check_only:
        sys.stdout.write(output + "\n")
        # this is the SAME normalization the MANAGED-section HAND-fence
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
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(output + "\n")

    # report of what was ACTUALLY written, not of an intended surface.
    declare_write(target)

    if is_new:
        sys.stderr.write(f"generate-exec-summary: created {target}\n")
    else:
        sys.stderr.write(f"generate-exec-summary: updated {target}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
