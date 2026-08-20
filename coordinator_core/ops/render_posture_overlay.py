"""
coordinator_core.ops.render_posture_overlay — idempotent managed-section merge
of a chosen Posture overlay block into a target managed-section file (a
consumer repo's `.claude/em-context.md`, not a global CLAUDE.md).

Purpose: reads `<coordinator-root>/templates/postures/<anchor>.md` (authored
by a sibling chunk — this module assumes the file exists at that path) and
merges its content into `<target>` as a MARKER-DELIMITED managed section:

    <!-- coordinator:posture:start -->
    ...anchor template content (its own `## Posture` heading lives INSIDE
    the markers as content — the markers are the anchor, not the heading)...
    <!-- coordinator:posture:end -->

Behavior:
    - No managed block present  -> INSERT (append to end of file). The rest
      of the file is never rewritten (install.md's never-clobber invariant).
    - Managed block present     -> SWAP (replace between markers) so a
      re-run / posture change hot-swaps in place; never appends a duplicate.
    - anchor == "default" injects default.md like any other anchor — there
      is no special "default removes the block" branch; all three anchors
      are treated uniformly.
    - check_only computes the intended action, prints it, and leaves the
      target file byte-unchanged (no write of any kind).

Size guard: THIS MODULE ENFORCES NO BYTE BUDGET FOR THIS TARGET. It
originally self-gated post-merge byte size against the same HARD threshold
`check-claude-md-size.py` enforces, scraped out of that DoE-resident file's
source at runtime — but `coordinator_core.claude_md_budget.is_governed_claude_md`
is a basename/governed-path concept (the global `~/.claude/CLAUDE.md`, or a
dev-repo-sentinel-anchored `coordinator/CLAUDE.md`), and this module's real
target is a per-repo `<consumer-repo>/.claude/em-context.md` — a surface
that predicate does not, and should not, recognize. Gating an unrelated
per-repo file against the global CLAUDE.md budget would be asserting a
constraint that does not apply to it. What exists on the consuming side is
read-time VISIBILITY, not a write-time refusal: `assert-em-role.py`'s
`_REPO_SNIPPET_SOFT_CAP_BYTES` (32,768 bytes) banners an oversized
`em-context.md` at read time, but then delivers the full content into the
session anyway — it warns and proceeds, it does not block. So nothing stops
an oversized `em-context.md` from being written here, by design — do not
reintroduce a byte-budget die() here for this target.

Collision detection: a target file that already carries a MARKERLESS
`## Posture` or `## Working Style` heading (the shape the pre-installer "How
to tune posture" doctrine told users to hand-author) OUTSIDE the managed
marker span is a collision with an unmanaged, hand-authored section —
checked on BOTH the insert path (no managed block yet) and the swap path
(managed block already present alongside a separate stray heading). This
module FAILS LOUD on that collision rather than silently appending a second
`## Posture` heading or silently swap-merging past it.

Port of: render-posture-overlay.sh (DoE a1a568d2, 2026-07-22)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Exit codes (parity-critical — the DoE trampoline forwards this verbatim):
    0 — success (insert/swap performed, or --check-only report printed)
    1 — usage error / validation failure / collision
        (mirrors every `die()`/bad-usage exit in the bash oracle, which
        funnels ALL such failures to a single exit 1 — not split further,
        to stay byte-parity with the oracle's exit-code contract)
    2 — RESERVED for the DoE trampoline's own claude-klabauter-link/import failure
        (engine-root resolution / ImportError) — never returned by this
        module's own main(); documented here so the two layers' exit-code
        tables stay disjoint (rule: transport-failure code must not collide
        with a business code — see docs/plans/2026-07-16-bash-clean-slate-
        residual-migration.md porter-brief addendum § 3b).

Negative-spec (do NOT "fix" while porting):
    - The bash oracle guards on BASH_VERSINFO[0] < 4 before anything else
      (bash-4+ required for none of ITS OWN syntax here, actually — this
      particular oracle's guard is defensive/copy-pasted convention rather
      than gating a bash-4-only construct it uses) — that guard is
      bash-specific and has no Python analogue; this port has no equivalent
      check (Python has no bash-version floor).
    - The oracle's atomic rewrite is `mktemp` (default mode 0600) + `mv`
      over the target, which does NOT preserve the target CLAUDE.md's prior
      permission bits (rename() replaces the destination inode's metadata
      with the source temp file's). This module mirrors that exactly via
      `tempfile.mkstemp` (also mode 0600) + `os.replace` — CLAUDE.md is a
      non-executable doc, so the addendum's "permission preservation" rule
      (§5, scoped to executable files: git hooks / launchers) does not
      apply here, and silently adding a chmod-preserve step would be an
      unrequested behavior change, not a faithful port.
    - Marker-rewrite technique (DR-148 lineage): the oracle explicitly bans
      `sed -i` (BSD/GNU incompatible) in favor of a full-file rewrite to a
      temp file + atomic replace. This port achieves the same *effect*
      (never edits the target file in place) via pure-Python string
      manipulation instead of re-shelling to `awk`/`grep`/`sed`/`mktemp`/
      `mv`/`cat`/`wc` — a strict portability improvement (no dependency on
      any POSIX text-tool being on PATH, matters most on Windows) that
      preserves the oracle's line-based, exact-string-equality marker
      matching semantics byte-for-byte.
"""


from __future__ import annotations

GENERATES = []  # merges into a caller-supplied target file (a consumer repo's .claude/em-context.md), never a fixed claude-klabauter artifact

import os
import re
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.session.declared_writes import declare_write

MARKER_START = "<!-- coordinator:posture:start -->"
MARKER_END = "<!-- coordinator:posture:end -->"

_VALID_ANCHORS = ("precision", "default", "substrate-free")

_HEADING_RE = re.compile(r"^## (Posture|Working Style)\s*$")

USAGE = """Usage: render-posture-overlay.sh <anchor> <target> [--check-only]

  <anchor>            One of: precision | default | substrate-free
  <target>            Path to the managed-section target to merge the
                       posture block into (e.g. <repo>/.claude/em-context.md).
                       Created, including parent directories, if absent.
  --check-only        Compute and print the intended action; write nothing.

Reads: <coordinator-root>/templates/postures/<anchor>.md

Exit codes:
  0  success (insert/swap performed, or --check-only report printed)
  1  usage error / validation failure / collision
"""


class PostureOverlayError(Exception):
    """Raised for every die()-equivalent failure path — caught once in main(), mapped to exit 1."""


def _die(msg: str) -> None:
    raise PostureOverlayError(msg)


def _strip_managed_span(lines: List[str]) -> List[str]:
    """Mirror the awk collision-scan pass: drop marker lines and everything between them.

    Line-based, exact-string equality against the whole (stripped) line —
    matches the bash oracle's `$0 == start` / `$0 == end` awk comparisons.
    """
    out: List[str] = []
    in_block = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == MARKER_START:
            in_block = True
            continue
        if stripped == MARKER_END:
            in_block = False
            continue
        if in_block:
            continue
        out.append(line)
    return out


def _find_collision(lines: List[str]) -> Optional[str]:
    for idx, line in enumerate(lines, start=1):
        if _HEADING_RE.match(line.rstrip("\n")):
            return f"{idx}:{line.rstrip(chr(10))}"
    return None


def _swap(original_lines: List[str], managed_block: str) -> str:
    """Replace the (first) managed span with managed_block, dropping the rest — mirrors the awk swap pass."""
    out: List[str] = []
    in_block = False
    printed_block = False
    for line in original_lines:
        stripped = line.rstrip("\n")
        if stripped == MARKER_START:
            if not printed_block:
                out.append(managed_block)
                printed_block = True
            in_block = True
            continue
        if stripped == MARKER_END:
            in_block = False
            continue
        if in_block:
            continue
        out.append(line.rstrip("\n"))
    return "\n".join(out)


def run(anchor: str, target: str, check_only: bool, coordinator_root: str) -> Tuple[int, str]:
    """Compute (and, unless check_only, perform) the insert/swap merge.

    Returns (exit_code, message) — message goes to stderr on both success
    (informational, matching the oracle's own stderr-only success report)
    and failure (the die() text).
    """
    if anchor not in _VALID_ANCHORS:
        return 1, (
            f"render-posture-overlay.sh: ERROR: Unknown anchor '{anchor}' — "
            f"must be one of: {', '.join(_VALID_ANCHORS)}"
        )

    coord_root = Path(coordinator_root)
    template_path = coord_root / "templates" / "postures" / f"{anchor}.md"
    target_path = Path(target)

    try:
        if not template_path.is_file():
            _die(f"Template not found: {template_path}")

        template_content = template_path.read_text(encoding="utf-8")
        # Strip a single trailing newline the way `$(cat ...)` command
        # substitution does (bash strips ALL trailing newlines) — match that
        # exactly, not just one, for byte parity with the oracle.
        template_content = template_content.rstrip("\n")

        for marker, label in ((MARKER_START, "start"), (MARKER_END, "end")):
            if any(line.rstrip("\n") == marker for line in template_content.splitlines()):
                _die(
                    f"Template {template_path} contains a line byte-identical to the {label} "
                    f"marker ({marker}) — this would corrupt marker-boundary detection on merge. "
                    "Rephrase the template to avoid an exact-line match against the marker string."
                )

        # A consumer repo has no `.claude/em-context.md` before its first
        # install — absence is the normal first-run case, not an error.
        # Treat a missing target as virtually empty for merge computation;
        # its parent directories are created (and the file itself written)
        # only on the actual write path below, never under --check-only.
        if target_path.is_file():
            original_text = target_path.read_text(encoding="utf-8")
        else:
            original_text = ""
        original_lines = original_text.splitlines(keepends=True)

        has_start_marker = any(line.rstrip("\n") == MARKER_START for line in original_lines)

        scan_lines = _strip_managed_span(original_lines) if has_start_marker else original_lines
        collision = _find_collision(scan_lines)
        if collision is not None:
            _die(
                "Collision: target already has a markerless '## Posture' or '## Working Style' "
                f"heading outside the managed marker span ({collision}). This is a hand-authored "
                "section from before managed-section merging existed (or added after an earlier "
                "install). Resolve by hand: either delete/rename that section, or wrap it manually "
                f"in {MARKER_START} / {MARKER_END} markers, then re-run."
            )

        if has_start_marker:
            if not any(line.rstrip("\n") == MARKER_END for line in original_lines):
                _die(
                    f"Target has a start marker but no matching end marker ({MARKER_END}) — "
                    "malformed managed block. Resolve by hand."
                )
            action = "swap"
        else:
            action = "insert"

        managed_block = f"{MARKER_START}\n{template_content}\n{MARKER_END}"

        if action == "swap":
            new_content = _swap(original_lines, managed_block)
        else:
            if original_text == "":
                new_content = managed_block
            else:
                new_content = original_text.rstrip("\n") + "\n\n" + managed_block

        new_bytes = (new_content + "\n").encode("utf-8")
        new_size = len(new_bytes)

        if check_only:
            return 0, (
                f"render-posture-overlay.sh: --check-only: would {action} managed posture block "
                f"(anchor={anchor}) into {target_path} (post-merge size {new_size} bytes)"
            )

        target_path.parent.mkdir(parents=True, exist_ok=True)

        fd, tmp_name = tempfile.mkstemp(
            prefix=target_path.name + ".tmp.", dir=str(target_path.parent)
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(new_bytes)
            os.replace(tmp_name, str(target_path))
            # DR-276: declared AFTER the atomic replace lands, never before —
            # the contract is a report of what was ACTUALLY written, not of
            # an intended surface.
            declare_write(target_path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                print(f"skip: run: os.unlink(tmp_name) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            _die(f"write/atomic-replace failed for {target_path}: {exc}")

        return 0, (
            f"render-posture-overlay.sh: {action} managed posture block (anchor={anchor}) "
            f"into {target_path} ({new_size} bytes)"
        )
    except PostureOverlayError as exc:
        print(f"skip: run: if not template_path.is_file(): failed: {exc}", file=sys.stderr)
        return 1, f"render-posture-overlay.sh: ERROR: {exc}"


def main(argv: List[str], coordinator_root: Optional[str] = None) -> int:
    """CLI entrypoint mirroring the bash oracle's arg-parse/usage/exit contract.

    `coordinator_root` is a required keyword for real invocation (the DoE
    trampoline resolves it from its own on-disk location, since the anchor
    templates are DoE-resident, not claude-klabauter-resident) but defaults to None so
    a missing caller-supplied value surfaces as a normal usage-style error
    rather than an AttributeError/TypeError.
    """
    args = list(argv)

    # Mirrors the oracle exactly: `-h`/`--help` is only recognized as a flag
    # AFTER <anchor> <target> have been consumed (i.e. `$# -lt 2`
    # is checked first) — `script -h` alone (1 arg) hits the usage+exit-1
    # path below, not usage+exit-0. Only `script <a> <t> -h` exits 0.
    if len(args) < 2:
        print(USAGE, end="")
        return 1

    anchor = args[0]
    target = args[1]
    rest = args[2:]

    check_only = False
    for a in rest:
        if a == "--check-only":
            check_only = True
        elif a in ("-h", "--help"):
            print(USAGE, end="")
            return 0
        else:
            print(
                f"render-posture-overlay.sh: ERROR: Unknown argument: {a} "
                "(run with --help for usage)",
                file=sys.stderr,
            )
            return 1

    if coordinator_root is None:
        print(
            "render-posture-overlay.sh: ERROR: coordinator_root not supplied "
            "(internal — trampoline must pass its own resolved coordinator root)",
            file=sys.stderr,
        )
        return 1

    exit_code, message = run(anchor, target, check_only, coordinator_root)
    print(message, file=sys.stderr)
    return exit_code


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="render-posture-overlay",
    source_module="coordinator_core.ops.render_posture_overlay",
    clauses=(
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="rc-block",
                    path="${_EM_CONTEXT_REPO_ROOT}/.claude/em-context.md",
                    begin_marker=MARKER_START,
                    end_marker=MARKER_END,
                ),
            ),
        ),
    ),
)
"""One call site today (DoE's `install.md:500`); a second (`repo-setup`) is
named as a future caller in `install.md:540` but is not wired yet, so this
declares the one real caller only, not a speculative second.

`kind="rc-block"`, not `hook-gate-region`: the latter is the BEGIN-only git
-hook gate comment region (`install_meta_repo_precommit_hook`'s
`end_marker=None` shape); this writer's marker pair is a genuine
begin/end-delimited managed section in a markdown file, matching `rc-block`.

`begin_marker`/`end_marker` are `MARKER_START`/`MARKER_END` themselves — the
module's own constants, read by `_swap()` at write time — never retyped, so
a future edit to either constant changes this declaration for free.

`path` keeps the `${_EM_CONTEXT_REPO_ROOT}` placeholder unresolved: `target`
is supplied by the caller at runtime (DoE's trampoline resolves it from its
own env var), and this declaration describes the surface's SHAPE, not one
machine's resolved absolute path."""
