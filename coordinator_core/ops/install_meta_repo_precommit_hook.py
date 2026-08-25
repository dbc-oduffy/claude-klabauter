"""
coordinator_core.ops.install_meta_repo_precommit_hook — meta-repo git-hook
gate installer (pre-commit, and — as of 2026-07-28 — post-merge/post-checkout).

Purpose: installs (or upgrades, or appends onto an existing custom hook) the
meta-repo's `.git/hooks/pre-commit` gate chain. Idempotent, conditional: only
installs when the resolved repo root is the meta-repo (`$HOME/.claude`
itself, identified by canonicalized path compare). Consumer repos are
skipped cleanly.

The target is the meta-repo, RESOLVED — never inferred from cwd. A bare
invocation resolves it via `_default_target()`; an argument names it
explicitly and still passes the identity guard. See `_default_target()` for
the 2026-07-30 defect that distinction closes (cwd-inferred default made a
bare invocation from a working repo a silent no-op that reported success).

**2026-07-28 addition — `main_post_sync()` / `_POST_SYNC_GATE_REGISTRY`:**
every gate above fires either on the SENDING side (`pre-commit`, this
module's original scope) or the AUTHORING side
(`coordinator_core.install.gen_settings_hooks`'s own kill-switch check).
Neither fires on the RECEIVING side of a `git merge`/`git pull` —
`coordinator-precommit-foreign-platform-check`'s own docstring names this
gap explicitly ("A `post-merge` leg would be needed to close that specific
vector; not built here"). `main_post_sync()` installs `.git/hooks/post-merge`
and `.git/hooks/post-checkout` with their own registry
(`_POST_SYNC_GATE_REGISTRY`), reusing every mechanism (`_Gate`,
`_gate_block`, `_hook_body`, the fresh-install/append/idempotency shape via
the shared `_install_or_append_hook`) the pre-commit chain already
established — adding a post-sync gate is a registry entry, exactly like
adding a pre-commit gate is.

**2026-07-29 — `main_install_all()`.** `main_post_sync()` shipped fully
built and fully tested with no install-time call site — the only launcher
imported `main`, never `main_post_sync`, so the receiving-side gates it
installs were live nowhere until this fix. `main_install_all()` is now the
launcher's own entrypoint: it drives both `main()` and `main_post_sync()`
behind one call, so there is exactly one place to invoke and it cannot be
half-run. See `main_install_all()`'s own docstring for the reasoning.

Gate registry (`_GATE_REGISTRY` below) is the single source of truth for
which checks ship in the hook — see that registry for the current four
gates (`detect-staged-rollback`, added 2026-07-29 — see "Exit-code clamping"
below for why it forced this file's exit-code discipline to change first;
`coordinator-precommit-exec-bit-check`, present at that time, was retired
2026-07-29 — see "Orphaned regions" below for the already-installed-hook
handling that retirement needed). Adding a gate is a registry entry, not a
new code path: fresh-install, upgrade-append, and idempotency all derive
from it generically.

If a pre-commit hook already exists with content other than this gate chain
(custom hooks, Git LFS prefix, etc.), the installer appends whatever gates
are still missing after the existing block rather than clobbering it.

Port of: install-meta-repo-precommit-hook.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: cross-repo/inbox/2026-06-08-exec-bit-drift-runtime-tripwire-tests.md
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

2026-07-28 REWRITE — fail-loud, self-relative, registry-driven. Replaces two
generations of defect found in the same session:

  1. The ORIGINAL bash-ported version baked a literal
     `$HOME/.claude/plugins/coordinator/bin/...` path into
     every emitted hook. Commit b644d5a9 migrated the whole executable
     surface into this repo (`claude-klabauter/coordinator/bin/`); that literal
     directory has not existed since. Every gate was guarded by a plain
     `[ -f ]` / `[ -x ]` test, so a missing script made the gate a silent
     no-op and the hook still exited 0 — proven empirically: a `settings.json`
     full of foreign-platform paths staged clean through this hook. Fixed
     here by resolving the bin dir SELF-RELATIVE to this module
     (`_bin_dir()`): this ops package and `coordinator/bin/` are siblings in
     the same repo, so the running engine's own location identifies its own
     gate scripts — no env var, no `$HOME` literal, no cross-repo lookup.

  2. A second, uncommitted in-flight edit (found already sitting in this
     working tree when this rewrite started — a live collision, not a stale
     leftover; see the executor run-report sidecar for this dispatch) fixed
     the self-relative resolution but replaced the missing-helper failure
     mode with a WARN-and-continue `_cc_gate()` helper (prints to stderr,
     `return 0`). That is the same fail-open shape as defect #1 wearing
     different clothes: a missing/broken gate still lets the commit through,
     just with a log line nobody is guaranteed to read at commit time. It
     also still pointed the illegal-path gate at `check-no-illegal-paths.sh`,
     which does not exist (the file was renamed `.py` in the same de-bash
     migration), and invoked it (and the exec-bit gate) via `bash "$script"`
     even though both are `#!/usr/bin/env python3` — bash executing a Python
     file does not error, it silently misparses the docstring as inert
     statements and exits 0, which is defect #1's failure mode a THIRD way
     (empirically confirmed: `bash coordinator-precommit-exec-bit-check`
     prints a `local: can only be used in a function` warning to stderr and
     still exits 0 without running any of the script's actual logic). This
     rewrite replaces that draft outright: every gate now fails LOUD (exit 1,
     named BLOCKED banner, remediation) on either a missing script or a
     missing interpreter — there is no gate whose absence is silent or
     advisory, and every registered script is invoked with the interpreter
     its own shebang declares (`python3`, never `bash`).

  Escape hatch (PM ruling, same session): a hard `exit 1` on every commit to
  the meta-repo, with no way out short of re-running the installer, risks
  wedging the machine on the same day this class of failure was already
  costly. Each gate's CANNOT-RUN block (missing script OR missing
  interpreter) honors `gate.override_env`, spelled identically to the
  sibling gate scripts' own internal content-check overrides
  (`COORDINATOR_OVERRIDE_PRECOMMIT_EXEC_BIT` / `..._PLATFORM_PATHS` /
  `..._SETTINGS_TRACKING`; `..._ILLEGAL_PATHS` is new, following the same
  pattern, since that script had none). This is orthogonal to a script's own
  override once it actually runs — the wrapper-level override only ever
  bypasses "the gate could not run at all," never a genuine finding.

  Exit-code clamping (2026-07-29, adopted from
  `install_claude_klabauter_precommit_hook.py`'s `_gate_block`): a gate's own exit code
  used to propagate straight out of this hook via a bare `|| exit $?`. That
  was safe only because every gate registered here happened to exit 0 or 1 —
  a property of the four gate scripts, not of this installer. A pre-commit
  hook that exits 2 is read by the Claude Code harness as a blocking DENY
  that kills Bash/Write/Edit together, INCLUDING the tools needed to repair
  the hook — this bricked the primary macOS box four times on 2026-07-28,
  once the fifth `_GATE_REGISTRY` entry (`detect-staged-rollback.py`, whose
  own documented contract — `coordinator_core.ops.detect_staged_rollback` —
  includes an exit-2 transport failure) entered the picture. Every gate
  block here now captures `$?` into `$_gate_rc` and re-derives the
  branch from it: exactly 0 continues, anything else (1, 2, or any future
  surprise value) is a real finding (the gate DID run) that BLOCKS and exits
  1 — never the raw code. Applies to both the python and bash exec shapes.
  This is a distinct branch from the pre-existing missing-script/missing-
  interpreter CANNOT-RUN branches above (`_cannot_run_branch`) and
  deliberately does NOT honor `gate.override_env` (`_finding_branch`,
  unlike `_cannot_run_branch`) — see the "Escape hatch" paragraph above: the
  wrapper-level override only ever bypasses "the gate could not run at
  all", never a genuine finding, and clamping a real finding's exit code
  must not quietly turn it into something the wrapper override can bypass.
  Bumped every existing gate's `version` (1→2, `_GATE_REGISTRY` and
  `_POST_SYNC_GATE_REGISTRY` alike — both drive `_gate_block`) alongside
  this fix: currency is decided by the version stamp, not a byte diff (see
  "Versioned regions" below), so a box with an already-installed v1 hook
  needs the version bump to actually receive the clamped body on its next
  `install-meta-repo-precommit-hook` run rather than being reported
  "already installed and current" forever.

Negative-spec (still-intentional behavior, not a residual bug):
    - Never emits a hook body that can exit anything other than 0 or 1 — see
      "Exit-code clamping" above and this module's own tests
      (`test_gate_script_exit_2_is_clamped_to_1`,
      `test_hook_body_never_contains_a_bare_exit_dollar_question`).
    - `_canon()` returns empty string on a non-directory / missing target —
      a non-existent target is a guaranteed skip, never a false identity
      match against `$HOME/.claude`.
    - No check-only / dry-run mode — the op always mutates when the identity
      guard passes.
    - `_find_gate_region()` never guesses a region boundary it can't confirm:
      it raises rather than crossing into another gate's header line, and
      raises rather than defaulting to end-of-file when no blank-line
      terminator is found — see that function's own docstring. A hand-edited
      hook that lost its separator blank line fails the install loudly
      instead of silently deleting whatever the mis-bounded region swallowed.
    - The bash-less carve-out (`command -v bash >/dev/null 2>&1 || exit 0`)
      is preserved as a documented, INTENTIONAL skip for any `kind="bash"`
      gate group — GitHub Desktop's MinGit ships sh+python but no bash. As of
      this rewrite the registry holds zero bash-kind gates (all five current
      scripts are pure Python), so that guard is not emitted into the
      current hook body; the machinery stays in place for the day a
      bash-only gate is reintroduced, rather than being deleted and
      re-invented later. This is a DELIBERATE divergence from the dispatch
      brief's literal wording (which assumed bash-kind gates still exist in
      the registry today) — flagged here, and in the run-report sidecar,
      rather than silently assumed.
"""

from __future__ import annotations

GENERATES = []  # writes only $HOME/.claude's .git/hooks/pre-commit (and post-merge/post-checkout), never tracked

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.install.write_surface import (
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.session.declared_writes import declare_write
from coordinator_core import meta_repo_identity as _meta_repo_identity
from coordinator_core import py_probe_sh as _py_probe_sh
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
# Cross-package import of the SSOT doc-pointer display string (same
# precedent write_guards already uses for operator_override_note itself) --
# emitted hook-body remediation text points readers at the doc that
# enumerates these keys, never names a key inline (B6/B8, see
# docs/wiki/guard-messaging.md § Register).
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY

_PROG = "install-meta-repo-precommit-hook"


@dataclass(frozen=True)
class _Gate:
    marker: str        # presence key: substring searched for in the hook body to find this gate's region
    filename: str      # bin/ filename (relative to the resolved bin dir)
    kind: str          # "python" or "bash" — selects which interpreter resolution/guard applies
    label: str         # human label for the BLOCKED banner
    override_env: str  # env var that bypasses a CANNOT-RUN block (missing script/interpreter)
    # currency key: bumped whenever this gate's emitted body changes; see "Versioned regions"
    # below — marker presence alone no longer means "installed and current", only "installed at
    # some version". Defaulted (not left required) so a test/caller building an ad-hoc _Gate
    # inline for a one-off scenario isn't forced to pick a version number that means nothing to
    # that scenario — every REGISTRY entry still states its version explicitly (see registries
    # below), the default only covers call sites that don't care.
    version: int = 1


_GATE_REGISTRY: List[_Gate] = [
    _Gate(
        marker="check-no-illegal-paths",
        filename="check-no-illegal-paths.py",
        kind="python",
        label="illegal-path",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS",
        version=2,
    ),
    _Gate(
        marker="coordinator-precommit-foreign-platform-check",
        filename="coordinator-precommit-foreign-platform-check",
        kind="python",
        label="foreign-platform-path",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_PLATFORM_PATHS",
        version=2,
    ),
    _Gate(
        marker="coordinator-precommit-settings-tracking-check",
        filename="coordinator-precommit-settings-tracking-check",
        kind="python",
        label="settings-tracking",
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_SETTINGS_TRACKING",
        version=2,
    ),
    _Gate(
        marker="detect-staged-rollback",
        filename="detect-staged-rollback.py",
        kind="python",
        label="staged-rollback",
        # 2026-08-21, PM ruling: the gate script's exact-blob rollback check
        # (check 1) was KILLED -- see coordinator_core.ops.detect_staged_
        # rollback's module docstring for the full ruling. The only check
        # left in that script is the mass-deletion tripwire, so this entry's
        # CANNOT-RUN override key repoints to that check's own key; the old
        # STAGED_ROLLBACK key is dead (no code anywhere reads it any more)
        # and must not be reintroduced by analogy for a future check.
        override_env="COORDINATOR_OVERRIDE_PRECOMMIT_MASS_DELETION",
        version=2,
    ),
]


def _bin_dir() -> Path:
    """The directory holding the pre-commit gate scripts, resolved SELF-
    RELATIVE to this module — this ops package (`coordinator_core/ops/`) and
    `coordinator/bin/` are siblings under the same repo root, so the engine
    that is actually running locates its own helpers. No `$HOME` literal, no
    env var, no cross-repo registry read: see the module docstring's
    2026-07-28 REWRITE note for why a resolved-root literal was the defect,
    not the fix.
    """
    return Path(__file__).resolve().parents[2] / "coordinator" / "bin"


def _canon(path: str) -> str:
    """Canonicalize a path the same way the bash oracle's canon() did.

    Bash: `(cd "$1" 2>/dev/null && pwd -P) || echo ""` — requires $1 to be an
    existing, cd-able directory; resolves symlinks. Returns "" on any failure
    so a failed canon never matches a non-empty expected path.
    """
    if not path:
        return ""
    try:
        if not os.path.isdir(path):
            return ""
        return os.path.realpath(path)
    except OSError:
        print(f"skip: _canon: if not os.path.isdir(path): failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def _git_toplevel(target: str) -> Optional[str]:
    """Resolve the repo root for `target` via `git -C <target> rev-parse --show-toplevel`.

    Returns None on any git failure (not a git repo, git missing, etc.) —
    parity with the bash oracle's `|| { ...skip...; exit 0; }` branch.
    """
    return _show_toplevel(cwd=target)


def _atomic_write(path: str, content: str) -> None:
    """Write `content` to `path` atomically: write to a `.tmp.<pid>` sibling, then rename.

    A concurrent git-commit never reads a torn hook. Every caller in this
    module chmods 0o755 right after — a git hook must be executable to fire
    at all, so this always forces the one mode a hook actually needs rather
    than trying to preserve whatever bits an earlier file happened to carry.
    """
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp_path, path)
    os.chmod(path, 0o755)
    declare_write(path)


def _strip_trailing_exit0(text: str) -> str:
    """Strip a bare trailing `exit 0` line so gates appended after it are not
    dead code.

    A `sh` script that already returned never reaches anything appended past
    it. The pre-rewrite installer blindly concatenated new gate blocks onto
    the end of the file, which silently made every appended gate unreachable
    whenever the base hook (ours from an earlier run, or a human's custom
    hook) ended in an explicit `exit 0` — the exact same "guard present but
    inert" shape as the missing-script defect, just at hook-assembly time
    instead of runtime. Every hook this installer writes now re-adds exactly
    one trailing `exit 0` at the true end, so this strip-then-reappend keeps
    that invariant regardless of how many times gates get appended.
    """
    rstripped = text.rstrip("\n")
    lines = rstripped.split("\n") if rstripped else []
    if lines and lines[-1].strip() == "exit 0":
        lines = lines[:-1]
    result = "\n".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


# ---------------------------------------------------------------------------
# Versioned regions — upgrade awareness.
#
# Before this: `marker not in existing_text` was the ENTIRE installed-ness
# test (see `_install_or_append_hook`'s pre-2026-07-28-later history). A gate
# whose marker survived but whose BODY had since changed — an older
# implementation, a since-fixed bug, a changed invocation — was reported
# "already installed" and left exactly as it was. There was no upgrade path
# at all short of deleting the hook by hand.
#
# Fix: each gate's emitted block carries a `# gate-version: N` stamp (written
# by `_gate_block`, immediately below its stable `# --- Gate: ... ---` header
# comment). "Installed" now splits into two independent questions:
#   - present?  `gate.marker in existing_text` (unchanged from before).
#   - current?  the LOCATED region for that marker contains today's
#     `_gate_version_line(gate)` (new).
# marker-absent -> append (as before). marker-present + current -> no-op.
# marker-present + stale -> replace THAT gate's region in place, leaving
# every other gate's region and any human content untouched.
# ---------------------------------------------------------------------------

def _gate_version_line(gate: _Gate) -> str:
    return f"# gate-version: {gate.version}"


def _find_gate_region(text: str, marker: str) -> Optional[Tuple[int, int, str]]:
    """Locate the on-disk region for the gate whose header comment names
    `marker` — `(start_char, end_char, indent)`, or None if no such header
    is present at all.

    A region runs from the `# --- Gate: <label> (<marker>) ---` header line
    through and including the first BLANK line that follows it. Every gate
    block this module ever emits — whether at top level (`_hook_body`,
    `_install_or_append_hook`'s append path) or nested inside the
    bash-presence group (`_bash_group_lines`, indented) — is immediately
    followed by exactly one blank separator line before the next block (or
    the enclosing group's `fi`, or the trailing `exit 0`), so this boundary
    rule holds regardless of nesting. `indent` is the header line's own
    leading whitespace, captured so a REPLACEMENT can be re-indented to
    match a nested (bash-group) original rather than flattening it to
    column 0.

    That "next blank line" invariant is only guaranteed for text this module
    itself emitted. This module's own docstring names hand-authored and
    custom hooks as first-class inputs, and a human edit CAN remove the
    separator blank line between one gate's block and the next content
    (accidental one-line deletion, a merge-conflict resolution, hand-splicing
    a footer directly after a gate) — if that happens, blindly walking to the
    first blank line found ANYWHERE past the header would silently swallow a
    sibling gate's block, or all trailing human content, into this gate's
    region; a caller that then replaces that over-extended region deletes
    whatever it swallowed. So the search is bounded: it raises rather than
    crossing into another gate's own header line, and raises rather than
    falling back to end-of-file when no blank-line terminator is found at
    all. Refusing to locate an ambiguous region is recoverable (fix the hook
    file, re-run); silently deleting a sibling gate's block or a human's
    content is not.
    """
    header_re = re.compile(r"^([ \t]*)# --- Gate: .*\(" + re.escape(marker) + r"\) ---\s*$")
    any_header_re = re.compile(r"^[ \t]*# --- Gate: .*\([^)]*\) ---\s*$")
    lines = text.splitlines(keepends=True)
    start_idx = None
    indent = ""
    for i, line in enumerate(lines):
        m = header_re.match(line.rstrip("\n"))
        if m:
            start_idx = i
            indent = m.group(1)
            break
    if start_idx is None:
        return None
    end_idx = None
    for j in range(start_idx + 1, len(lines)):
        stripped = lines[j].rstrip("\n")
        if stripped.strip() == "":
            end_idx = j + 1
            break
        if any_header_re.match(stripped):
            raise RuntimeError(
                f"{_PROG}: internal error: gate {marker!r}'s region has no blank-line "
                f"separator before the next gate header (line {j + 1}) — refusing to "
                "guess a boundary that could swallow that sibling gate's block. The hook "
                "file may have been hand-edited (e.g. a separator blank line was removed "
                "between two gate blocks) — restore the blank line, or reset the hook and "
                "re-run the installer."
            )
    if end_idx is None:
        raise RuntimeError(
            f"{_PROG}: internal error: gate {marker!r}'s region has no blank-line "
            "terminator anywhere before end-of-file — refusing to treat end-of-file as "
            "the boundary, which could silently delete trailing human-authored content. "
            "The hook file may have been hand-edited (e.g. a trailing separator blank "
            "line was removed) — restore the blank line, or reset the hook and re-run "
            "the installer."
        )
    start_char = sum(len(l) for l in lines[:start_idx])
    end_char = sum(len(l) for l in lines[:end_idx])
    return start_char, end_char, indent


def _gate_is_stale(text: str, gate: _Gate) -> bool:
    """True iff `gate`'s region can be LOCATED in `text` and that region's
    body does not carry today's version stamp. False if the region cannot
    be located at all (an unrecognized/legacy shape — not a claim this
    module can safely act on, see the `stale_gates` comprehension's
    docstring at its one call site) or if the region is current. Callers
    are expected to already have established marker presence
    (`gate.marker in text`) before calling this — it does not re-check it."""
    region = _find_gate_region(text, gate.marker)
    if region is None:
        return False
    start, end, _indent = region
    return _gate_version_line(gate) not in text[start:end]


def _replace_stale_gate_regions(text: str, stale_gates: List[_Gate], bin_dir: Path) -> str:
    """Surgically replace each `stale_gates` entry's on-disk region with a
    freshly generated (current-version) block, re-indented to match the
    original region's own indentation. Every byte outside the located
    regions — other gates' blocks, any human-authored content in the same
    hook file — is left untouched. Regions are resolved and then applied in
    DESCENDING start-offset order so replacing one never shifts the
    already-computed offsets of another.

    Fails loud (raises) rather than silently skipping if a gate this
    function was told is stale cannot actually be located — "stale" is only
    ever computed from a located region (`_gate_is_current`), so a miss here
    means the text changed out from under this call, not a normal case to
    swallow.
    """
    regions: List[Tuple[_Gate, Tuple[int, int, str]]] = []
    for gate in stale_gates:
        region = _find_gate_region(text, gate.marker)
        if region is None:
            raise RuntimeError(
                f"{_PROG}: internal error: gate {gate.marker!r} was classified stale but its "
                "region could not be re-located for replacement."
            )
        regions.append((gate, region))
    regions.sort(key=lambda item: item[1][0], reverse=True)

    for gate, (start, end, indent) in regions:
        block_lines = _gate_block(gate, bin_dir)
        indented = "\n".join((indent + line if line else line) for line in block_lines)
        replacement = indented + "\n\n"
        text = text[:start] + replacement + text[end:]
    return text


# ---------------------------------------------------------------------------
# Orphaned regions — a gate RETIRED out of the registry entirely (not merely
# bumped to a new version) leaves its region behind in any hook already
# installed on a machine before the retirement landed. Versioned-region
# upgrade-awareness (above) only ever asks "is this registry entry's region
# current", so a marker that is no longer in the registry at all is invisible
# to `missing_gates`/`stale_gates` and was left untouched forever -- exactly
# the gap named when `coordinator-precommit-exec-bit-check` was retired
# (2026-07-29): the gate script file is deleted, but an already-installed
# hook's region still shells out to it, so every commit on that machine would
# hit the missing-script CANNOT-RUN branch (loud BLOCKED, `exit 1`) from then
# on -- a strictly worse outcome than the neutralized no-op hook it replaced.
# `_remove_orphaned_gate_regions` closes that gap generically: any region
# whose header names a marker absent from the CURRENT registry is spliced
# out, so retiring a gate converges an already-installed hook back to a
# clean state on its next install run, the same way adding or upgrading one
# does.
# ---------------------------------------------------------------------------

def _find_all_gate_markers(text: str) -> List[str]:
    """Return every gate marker named by a `# --- Gate: ... (<marker>) ---`
    header currently present in `text`, in file order (duplicates possible
    only in a hand-edited/corrupted hook; callers dedupe as needed)."""
    header_re = re.compile(r"^[ \t]*# --- Gate: .*\(([^)]*)\) ---\s*$")
    markers: List[str] = []
    for line in text.splitlines():
        m = header_re.match(line.rstrip("\n"))
        if m:
            markers.append(m.group(1))
    return markers


def _remove_orphaned_gate_regions(text: str, current_markers: "set[str]") -> Tuple[str, List[str]]:
    """Strip every gate region in `text` whose marker is NOT in
    `current_markers` (a gate retired out of the registry, not merely
    bumped) — returns `(new_text, removed_markers)`.

    Uses the same `_find_gate_region` boundary rule as stale-gate
    replacement (fails loud on an ambiguous/unbounded region rather than
    guessing) — an orphan whose region cannot be safely located is left in
    place rather than silently dropped; it will still surface as a
    CANNOT-RUN BLOCKED finding on the next commit, which is loud and
    recoverable, unlike a mis-bounded splice.
    """
    orphaned = [m for m in _find_all_gate_markers(text) if m not in current_markers]
    if not orphaned:
        return text, []

    regions: List[Tuple[str, Tuple[int, int, str]]] = []
    removed: List[str] = []
    for marker in orphaned:
        try:
            region = _find_gate_region(text, marker)
        except RuntimeError as exc:
            # Ambiguous boundary (no blank-line terminator, or it crosses
            # into a sibling gate's header) -- the same "refuse to guess"
            # posture _find_gate_region documents for stale-gate
            # replacement. Left in place: it will surface as a loud
            # CANNOT-RUN BLOCKED finding on the gate's next commit instead
            # (missing script), which is recoverable; silently corrupting
            # an unbounded splice is not.
            print(f"{_PROG}: skipping orphan removal for {marker!r}: {exc}", file=sys.stderr)
            continue
        if region is None:
            continue
        regions.append((marker, region))
        removed.append(marker)
    regions.sort(key=lambda item: item[1][0], reverse=True)

    for _marker, (start, end, _indent) in regions:
        text = text[:start] + text[end:]
    return text, removed


def _py_resolve_line() -> str:
    """POSIX `sh` interpreter probe, resolved in-order (python3, python, py)
    and skipping any hit resolved under `WindowsApps` — a candidate whose
    path contains that segment (case-insensitively, mirroring
    `pyresolve._is_store_python`'s `"windowsapps" in path.lower()` check) is
    the Microsoft Store's non-functional App Execution Alias stub, not a
    real interpreter; `.cmd` launchers generated by `gen-launcher-shim.py`
    already filter it (`findstr /I /C:"\\WindowsApps\\"`, walking every
    `where python.exe` hit rather than only the first) — this mirrors that
    filter for the emitted `sh` probe, which previously had none and would
    invoke the stub instead of falling through to a real interpreter.

    Deliberately walks `$PATH` by hand (POSIX `sh` has no `command -v -a`;
    that flag is a bash/GNU extension) rather than taking `command -v`'s
    single first hit — the realistic Windows topology this guards is a
    WindowsApps alias for the SAME candidate name (e.g. `python`) earlier on
    PATH than a real python.org install of that same name later on PATH, so
    a design that only falls through to the NEXT NAME on a stub hit (as
    `pyresolve.py`'s Step-2 `_which()`-per-name loop does, for the
    in-process case where PATH re-probing per directory isn't needed) would
    never reach the real interpreter. Probes both the bare candidate and a
    `.exe`-suffixed form per directory since MSYS/git-bash resolves the
    extension via `command -v` but a manual `-x` file test does not.
    No-op on POSIX, where the `WindowsApps` path component never appears.

    Delegates to `coordinator_core.py_probe_sh` rather than hand-rolling the
    probe — see that module's docstring for the "why one shared
    implementation" reasoning, and `install_claude_klabauter_precommit_hook.
    _py_resolve_line`, which shares the same helper. Only the probe is
    shared; the surrounding gate-block machinery stays independently
    duplicated (this module's own docstring, "Kept as an independent
    module").

    The shared form is not a byte-for-byte copy of the inline version it
    replaces, and the difference is deliberate: the skip test is a pure
    `case` glob (`*/[Ww][Ii][Nn]...[Ss]/*`) instead of
    `case "$(printf '%s' "$_py_exe" | tr '[:upper:]' '[:lower:]')"`. Both
    match `WindowsApps` case-insensitively, but the glob costs no process
    spawn, where the `tr` form forked a subshell PLUS `tr` for every
    candidate in every `$PATH` directory. That is emitted into a
    pre-commit hook, so it ran on every commit — precisely the Windows
    process-spawn tax this repo treats as break-class rather than as a
    micro-optimisation."""
    return _py_probe_sh.python_probe_lines("_py")


# Named env var for the bash-kind group-level CANNOT-RUN escape hatch, spelled
# the same way the per-gate override_env fields are (D2, 2026-07-28) — see
# _bash_group_lines' docstring for why this replaced the old silent
# `command -v bash || exit 0` shape.
_BASH_MISSING_OVERRIDE_ENV = "COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING"


def _bash_group_lines(bash_gates: List["_Gate"], bin_dir: Path) -> List[str]:
    """Emit the bash-kind gate group, gated on bash's presence.

    D2 fix (2026-07-28): the prior shape was a bare
    `command -v bash >/dev/null 2>&1 || exit 0` emitted before this group —
    GitHub Desktop's MinGit lacks bash, and that guard silently exited the
    WHOLE hook (not just the bash-kind gates) with zero output whenever bash
    was unresolvable, the same "gate present but inert" failure mode the
    2026-07-28 rewrite (see module docstring) already fixed for missing
    scripts/python. This mirrors that fix for the bash-kind group: bash's
    absence now emits a named BLOCKED banner + remediation and exits 1,
    unless the operator sets COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING=1
    (mirrors the per-gate override_env escape-hatch pattern).

    No bash-kind gates are registered in `_GATE_REGISTRY` as of this writing
    (see module docstring's negative-spec) — this function is exercised only
    if `bash_gates` is non-empty, keeping the machinery live for the day one
    is reintroduced without leaving a silent-skip regression in place until
    then.
    """
    override_test = f'[ "${_BASH_MISSING_OVERRIDE_ENV}" = "1" ]'
    lines = [
        "# POSIX-sh + bash guard: GitHub Desktop's MinGit lacks bash; the gate(s)",
        "# below need it. Missing bash now fails LOUD (named banner, override",
        "# escape hatch) rather than silently exiting the whole hook (D2).",
        "if ! command -v bash >/dev/null 2>&1; then",
        f"  if {override_test}; then",
        '    echo "pre-commit: bash-kind gate(s) SKIPPED -- no bash interpreter found on PATH (override set)." >&2',
        "  else",
        '    echo "pre-commit: BLOCKED -- bash-kind gate(s) cannot run: no bash interpreter found on PATH." >&2',
        f'    echo "pre-commit: remediation: install bash. See {OVERRIDE_KEYS_DOC_DISPLAY} for override options." >&2',
        "    exit 1",
        "  fi",
        "else",
    ]
    for gate in bash_gates:
        for line in _gate_block(gate, bin_dir):
            lines.append("  " + line if line else line)
        lines.append("")
    lines.append("fi")
    return lines


def _gate_block(gate: _Gate, bin_dir: Path) -> List[str]:
    """Emit the runtime lines for one gate: fail LOUD (exit 1, named banner)
    on either a missing interpreter or a missing script, else run. Both
    CANNOT-RUN cases honor `gate.override_env` as an escape hatch, spelled
    the same way the sibling gate scripts' own internal content-check
    overrides already are (`COORDINATOR_OVERRIDE_PRECOMMIT_` plus the gate's
    own name, set to `1`) — a
    missing helper on a wedged machine must be bypassable by an operator who
    knows what they're doing, without reintroducing a silent default skip:
    the override is opt-in per invocation, printed to stderr every time it
    fires, and named in the BLOCKED banner's own remediation text so an
    operator hitting the block sees the exact spelling to use.

    This is orthogonal to (and does not touch) the gate SCRIPT's own
    override for a real policy finding once it runs — this override only
    ever bypasses "the gate could not run at all", never a genuine
    settings-tracking/foreign-platform/exec-bit violation the script itself
    detected. Named per-gate banners mean a BLOCKED commit tells the
    operator exactly which gate failed and how to fix (or bypass) it — the
    discriminator this rewrite exists to restore versus the WARN-and-
    continue draft it replaces (see module docstring).

    The second emitted line is a `# gate-version: N` stamp (see
    `_gate_version_line()`) — this is the versioned-region marker the
    installer's currency check (`_gate_is_current()`) reads back out of a
    located gate region. The header comment line ABOVE it is deliberately
    left byte-identical to its pre-versioning form (no version suffix) —
    it is the block-boundary anchor `_find_gate_region()` searches for, and
    changing its text would itself be an unrelated-looking diff every time
    a gate's version bumps.
    """
    script_path = "/".join([bin_dir.as_posix(), gate.filename])
    override_test = f'[ "${gate.override_env}" = "1" ]'

    def _cannot_run_branch(reason: str, remediation: str) -> List[str]:
        return [
            f"  if {override_test}; then",
            f'    echo "pre-commit: gate [{gate.label}] ({gate.marker}) SKIPPED -- {reason} (override set)." >&2',
            "  else",
            f'    echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) cannot run: {reason}." >&2',
            f'    echo "pre-commit: remediation: {remediation}. See {OVERRIDE_KEYS_DOC_DISPLAY} for override options." >&2',
            "    exit 1",
            "  fi",
        ]

    def _finding_branch() -> List[str]:
        # Deliberately does NOT honor gate.override_env: unlike the
        # CANNOT-RUN cases above (missing script/interpreter — the gate
        # never got to run at all), this branch fires only once the gate
        # script HAS run and returned nonzero — a real finding (or the
        # script's own exit-2 transport failure). The wrapper-level override
        # is PM-ruled to bypass only "could not run", never a genuine
        # finding (see this function's own docstring); a gate that wants its
        # findings to be overridable exposes its OWN content-check override
        # (as detect-staged-rollback's COORDINATOR_OVERRIDE_PRECOMMIT_MASS_
        # DELETION does internally, before this wrapper ever sees a nonzero
        # code). This is exit-code CLAMPING only: any nonzero `$_gate_rc`
        # (1, 2, or a future surprise value) surfaces as exactly 1, never the
        # raw code — see module docstring's "Exit-code clamping".
        return [
            f'    echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) reported a problem '
            '(exit code $_gate_rc) -- see output above." >&2',
            "    exit 1",
        ]

    lines = [
        f"# --- Gate: {gate.label} ({gate.marker}) ---",
        _gate_version_line(gate),
        f'_gate_script="{script_path}"',
        'if [ ! -f "$_gate_script" ]; then',
    ]
    lines += _cannot_run_branch(
        "missing script $_gate_script", "re-run the coordinator installer to restore it"
    )

    if gate.kind == "python":
        lines.append('elif [ -z "$_py" ]; then')
        lines += _cannot_run_branch(
            "no python interpreter found (python3/python/py) on PATH", "install Python, then retry"
        )
        lines += [
            "else",
            '  "$_py" "$_gate_script"',
            "  _gate_rc=$?",
            '  if [ "$_gate_rc" -ne 0 ]; then',
        ]
        lines += _finding_branch()
        lines += ["  fi", "fi"]
    elif gate.kind == "bash":
        # Bash presence is guaranteed by the group-level carve-out
        # (`command -v bash || exit 0`) already run before any bash-kind
        # gate reaches here, so there is no separate missing-interpreter
        # branch to duplicate it.
        lines += [
            "else",
            '  bash "$_gate_script"',
            "  _gate_rc=$?",
            '  if [ "$_gate_rc" -ne 0 ]; then',
        ]
        lines += _finding_branch()
        lines += ["  fi", "fi"]
    else:
        raise ValueError(f"unknown gate kind in registry: {gate.kind!r}")

    return lines


def _hook_body(bin_dir: Path, gates: List[_Gate]) -> str:
    """Assemble a full hook body for exactly `gates` (in registry order).

    Python-kind gates are bash-free by construction and run first,
    unconditionally — the same "survive on a bash-less box" property the
    2026-07-28 platform/settings gates were built for. Bash-kind gates (none
    currently registered — see module docstring) are grouped behind the
    documented `command -v bash || exit 0` carve-out so their absence stays
    visibly distinct from a missing-script BLOCKED failure.
    """
    python_gates = [g for g in gates if g.kind == "python"]
    bash_gates = [g for g in gates if g.kind == "bash"]

    lines = [
        "#!/bin/sh",
        "# Meta-repo pre-commit gates — fire before drift can land.",
        "# Registry-driven (coordinator_core.ops.install_meta_repo_precommit_hook);",
        "# every gate below fails LOUD (exit 1, named banner) on a missing script",
        "# or missing interpreter — never a silent skip. See that module's",
        "# docstring for the incident class this replaces.",
        "",
    ]
    if python_gates:
        lines.append(_py_resolve_line())
        lines.append("")
        for gate in python_gates:
            lines.extend(_gate_block(gate, bin_dir))
            lines.append("")
    if bash_gates:
        lines.extend(_bash_group_lines(bash_gates, bin_dir))
        lines.append("")
    lines.append("exit 0")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# post-merge / post-checkout — the RECEIVING-side leg of the 2026-07-28
# incident cluster. Every gate above fires on the SENDING side (pre-commit)
# or the AUTHORING side (gen_settings_hooks' own kill-switch check); the
# incident's actual TRANSMISSION was a `git merge`/`git pull` on the
# receiving machine, which had no guard at all until this registry.
# `coordinator-precommit-foreign-platform-check`'s own docstring names this
# gap explicitly. Shares `_Gate`/`_gate_block`/`_hook_body` with the
# pre-commit registry above (same shape, different hook filenames + gate
# list) — adding a post-sync gate is a registry entry here, exactly like
# adding a pre-commit gate is a registry entry in `_GATE_REGISTRY`.
# ---------------------------------------------------------------------------

_POST_SYNC_GATE_REGISTRY: List[_Gate] = [
    _Gate(
        marker="coordinator-postsync-marker-resync-check",
        filename="coordinator-postsync-marker-resync-check",
        kind="python",
        label="marker-resync",
        override_env="COORDINATOR_OVERRIDE_POSTSYNC_MARKER_RESYNC",
        version=2,
    ),
]

_POST_SYNC_HOOK_FILENAMES = ("post-merge", "post-checkout")


def _install_or_append_hook(repo_root: str, hook_filename: str, gates: List[_Gate]) -> int:
    """Shared install/append logic for a SINGLE named git hook
    (`.git/hooks/<hook_filename>`) against `gates` (in registry order).
    Both `main()` (pre-commit, `_GATE_REGISTRY`) and
    `main_post_sync()` (post-merge/post-checkout, `_POST_SYNC_GATE_REGISTRY`)
    call this — the fresh-install, append, and idempotency behavior is
    identical regardless of which hook filename or registry is in play, so
    it lives in exactly one place rather than being re-derived per hook
    type."""
    hook_path = os.path.join(repo_root, ".git", "hooks", hook_filename)
    bin_dir = _bin_dir()

    hook_exists = os.path.isfile(hook_path)
    if hook_exists:
        try:
            with open(hook_path, "r", encoding="utf-8") as fh:
                existing_text = fh.read()
        except OSError as exc:
            print(f"skip: _install_or_append_hook: reading {hook_path} failed: {exc}", file=sys.stderr)
            existing_text = ""
    else:
        existing_text = ""

    # Orphan removal — a gate retired OUT of the registry entirely (not
    # merely bumped) leaves a dead region behind in any hook installed
    # before the retirement landed; that region still shells out to a now-
    # deleted script, which would BLOCK every future commit rather than stay
    # silent. See `_remove_orphaned_gate_regions`'s own docstring. Runs
    # before missing/stale classification so both are computed against the
    # already-cleaned text.
    orphaned_gates: List[str] = []
    if hook_exists and existing_text:
        current_markers = {g.marker for g in gates}
        existing_text, orphaned_gates = _remove_orphaned_gate_regions(existing_text, current_markers)

    missing_gates = [g for g in gates if g.marker not in existing_text]
    # Present-but-stale: marker survived AND its region is locatable (a
    # `# --- Gate: ... ---` header this module itself would have emitted),
    # but that region's body no longer carries today's version stamp — see
    # "Versioned regions" above _gate_version_line. A gate whose marker is
    # present via raw substring match but whose region can't be located at
    # all (e.g. a legacy/hand-authored hook that predates the header-comment
    # shape, or references the marker only inside a script PATH) is left
    # alone rather than guessed-at — "stale" is only ever a claim this
    # module can back with a located, replaceable region.
    stale_gates = [g for g in gates if g.marker in existing_text and _gate_is_stale(existing_text, g)]

    if hook_exists and not missing_gates and not stale_gates and not orphaned_gates:
        print(f"{_PROG}: gate already installed and current at {hook_path} — no-op.", file=sys.stderr)
        return 0

    if not hook_exists:
        content = _hook_body(bin_dir, gates)
        _atomic_write(hook_path, content)
        print(f"{_PROG}: installed {hook_path}.", file=sys.stderr)
        return 0

    # Existing hook, and at least one gate is missing and/or stale. First
    # replace every stale gate's region IN PLACE (surgical — see
    # _replace_stale_gate_regions), then run the append path for whatever's
    # still genuinely absent, exactly as before. Strip any trailing bare
    # `exit 0` so appended blocks are reachable, then re-add exactly one at
    # the end.
    working_text = existing_text
    if stale_gates:
        working_text = _replace_stale_gate_regions(working_text, stale_gates, bin_dir)

    base = _strip_trailing_exit0(working_text)
    missing_python = [g for g in missing_gates if g.kind == "python"]
    missing_bash = [g for g in missing_gates if g.kind == "bash"]

    addition_lines: List[str] = [""]
    if missing_python:
        if "_py=" not in working_text:
            addition_lines.append(_py_resolve_line())
            addition_lines.append("")
        for gate in missing_python:
            addition_lines.extend(_gate_block(gate, bin_dir))
            addition_lines.append("")

    if missing_bash:
        # Each append gets its own self-contained bash-presence guard (see
        # _bash_group_lines) rather than relying on a prior guard already in
        # the file — the guard is now a full if/else block, not a standalone
        # short-circuit line, so there is no single substring to detect and
        # reuse across append events.
        addition_lines.extend(_bash_group_lines(missing_bash, bin_dir))
        addition_lines.append("")

    new_content = base + "\n".join(addition_lines) + "\nexit 0\n"
    _atomic_write(hook_path, new_content)

    reports = []
    if orphaned_gates:
        reports.append(f"removed retired gate(s) [{', '.join(orphaned_gates)}]")
    if stale_gates:
        reports.append(f"replaced stale gate(s) [{', '.join(g.marker for g in stale_gates)}]")
    if missing_gates:
        reports.append(f"appended gate(s) [{', '.join(g.marker for g in missing_gates)}]")
    print(f"{_PROG}: {'; '.join(reports)} at {hook_path}.", file=sys.stderr)
    return 0


def _default_target() -> Optional[str]:
    """The meta-repo root, resolved from the environment — the target when the
    caller passed no argument.

    Why this is not `"."` (2026-07-30): the previous default inferred the
    target from cwd, so a bare invocation from any working repo resolved that
    repo, failed the identity guard below, and returned 0. A session runs from
    a working repo, so the ONE surface whose whole job is to be installed was
    the one surface a bare invocation never installed — and the skip was
    indistinguishable from success to `/coordinator:install` and `/repo-setup`
    (DoE `state/bug-backlog/2026-07-29-meta-repo-gate-installer-is-cwd-gated-so-3763de751e55.yaml`;
    it is why `~/.claude/.git/hooks` held only `pre-commit` on 2026-07-29 even
    though the `main_install_all` call site had already landed). Resolving the
    subject rather than inferring it from where the caller happens to stand is
    the fix; the identity guard survives for an EXPLICIT target, where a
    mismatch is a caller error worth naming.

    Home resolution delegates to `meta_repo_identity` — the canonical
    meta-repo-identity primitive — rather than re-deriving it from `HOME`
    alone, which is unset on Windows often enough to matter and ignores
    `CLAUDE_HOME` test/CI sandboxes.
    """
    try:
        return str(_meta_repo_identity._resolve_meta_repo_root())
    except _meta_repo_identity.MetaRepoResolutionError as exc:
        print(f"{_PROG}: meta-repo root unresolvable ({exc}) — NO gate installed.", file=sys.stderr)
        return None


def _resolve_meta_repo_target(target: Optional[str]) -> Optional[str]:
    """Shared identity guard: resolve `target` to the meta-repo root, or None
    if it is not a git repo / not the meta-repo. `target=None` means "the
    caller named no target" and resolves the meta-repo from the environment
    (`_default_target`), never from cwd. Both `main()` and `main_post_sync()`
    apply this identically before touching any hook file."""
    if target is None:
        target = _default_target()
        if target is None:
            return None

    toplevel = _git_toplevel(target)
    if toplevel is None:
        print(f"{_PROG}: {target} not in a git repo — NO gate installed.", file=sys.stderr)
        return None
    repo_root = toplevel
    if not repo_root:
        print(f"{_PROG}: empty repo root — NO gate installed.", file=sys.stderr)
        return None

    meta_repo = _default_target()
    if meta_repo is None:
        return None

    if _canon(repo_root) != _canon(meta_repo):
        print(
            f"{_PROG}: {repo_root} is not the meta-repo ({meta_repo}) — NO gate "
            f"installed. Invoke with no argument to target the meta-repo.",
            file=sys.stderr,
        )
        return None
    return repo_root


def main(argv: List[str]) -> int:
    target = argv[0] if argv else None
    repo_root = _resolve_meta_repo_target(target)
    if repo_root is None:
        return 0
    return _install_or_append_hook(repo_root, _SENDING_HOOK_FILENAME, _GATE_REGISTRY)


def main_post_sync(argv: List[str]) -> int:
    """Installs (or upgrades) the `post-merge` AND `post-checkout` gate
    chains — the receiving-side leg (see module-level comment above
    `_POST_SYNC_GATE_REGISTRY`). Both hook filenames get the SAME gate
    registry, installed independently (a `post-merge`-only or
    `post-checkout`-only partial state from an earlier run still converges
    to both being current on the next call, same idempotency property as
    `main()`)."""
    target = argv[0] if argv else None
    repo_root = _resolve_meta_repo_target(target)
    if repo_root is None:
        return 0
    rc = 0
    for hook_filename in _POST_SYNC_HOOK_FILENAMES:
        rc = _install_or_append_hook(repo_root, hook_filename, _POST_SYNC_GATE_REGISTRY) or rc
    return rc


def main_install_all(argv: List[str]) -> int:
    """Single install-time entrypoint: installs the SENDING-side gate
    (`pre-commit`, `main()`/`_GATE_REGISTRY`) AND the RECEIVING-side gates
    (`post-merge`/`post-checkout`, `main_post_sync()`/
    `_POST_SYNC_GATE_REGISTRY`) in one call.

    Why this exists (2026-07-29): `main_post_sync()` shipped fully built and
    fully tested (`test_install_post_sync_hooks.py`) with no install-time
    call site at all — the only launcher
    (`coordinator/bin/install-meta-repo-precommit-hook.py`) imported `main`,
    never `main_post_sync`, so the receiving-side gates this function installs
    were live nowhere. That is the exact "gate present but not wired" failure
    class this module's own docstring already names twice for OTHER reasons
    (hardcoded bin-dir literal, WARN-and-continue draft) — a THIRD instance,
    this time at the call-site layer rather than inside a single gate. Rather
    than adding a second call the launcher must ALSO remember to make (the
    shape that produced this exact bug), the launcher's own `main()` import
    was repointed at this combined function — see
    `coordinator/bin/install-meta-repo-precommit-hook.py`'s own docstring —
    so there is exactly one call site and it cannot be half-run.

    Both legs share the identical `_resolve_meta_repo_target` identity guard;
    a non-meta-repo / non-git target skips both cleanly, matching the
    individual functions' own skip semantics. Returns 0 only if BOTH legs
    returned 0 — a nonzero from either leg propagates (mirrors
    `main_post_sync`'s own `rc = ... or rc` fold across its two hook
    filenames, extended one level up across the two legs)."""
    rc_pre = main(argv)
    rc_post = main_post_sync(argv)
    return rc_pre or rc_post


# Review: coordinator:code-reviewer — relocated out of the import block
# (previously split two contiguous import statements) to sit beside
# WRITE_SURFACE, which reads it.
_SENDING_HOOK_FILENAME = "pre-commit"
"""The sending-side hook filename `main()` installs `_GATE_REGISTRY` into.
A named constant so `main()`'s call site and `WRITE_SURFACE` share one
spelling — two independent literals is the copy-drift shape this writer's
own declaration exists to remove, and the receiving side already avoids it
via `_POST_SYNC_HOOK_FILENAMES`."""


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="install-meta-repo-precommit-hook",
    source_module="coordinator_core.ops.install_meta_repo_precommit_hook",
    clauses=(
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(
                    kind="hook-gate-region",
                    path=_SENDING_HOOK_FILENAME,
                    begin_marker=f"# --- Gate: {gate.label} ({gate.marker}) ---",
                    end_marker=None,
                )
                for gate in _GATE_REGISTRY
            )
            + tuple(
                WriteSurfaceEntry(
                    kind="hook-gate-region",
                    path=hook_filename,
                    begin_marker=f"# --- Gate: {gate.label} ({gate.marker}) ---",
                    end_marker=None,
                )
                for hook_filename in _POST_SYNC_HOOK_FILENAMES
                for gate in _POST_SYNC_GATE_REGISTRY
            ),
        ),
        StaticClause(
            entries=tuple(
                WriteSurfaceEntry(kind="file-path", path=hook_filename)
                for hook_filename in _POST_SYNC_HOOK_FILENAMES
            ),
        ),
    ),
)
"""This writer's declared write surface — derived FROM `_GATE_REGISTRY`,
`_POST_SYNC_GATE_REGISTRY`, and `_POST_SYNC_HOOK_FILENAMES` (never a
restated literal list of gate names or hook filenames), so a future edit
adding/removing a gate or a post-sync hook filename alone cannot make this
declaration stale without its test going red. See spec backlink:
docs/plans/2026-08-06-writer-declared-write-surface-manifest.md, chunk C2.

`begin_marker` is derived exactly as `_gate_block()` emits it (the
`# --- Gate: {label} ({marker}) ---` header line `_find_gate_region()`
searches for) — never a hand-guessed spelling. `end_marker` is `None`, not
`ABSENT_ON_LEGACY_INSTALLS`: `_find_gate_region()` documents that a gate
region's true terminator is a STRUCTURAL boundary (the first blank line
following the header, or the next gate's header/EOF), not a literal marker
string — there is no end-marker string to have been legacy-absent, so the
"not applicable" state applies uniformly across every install, not only
pre-versioning ones.

The SENDING-side hook filename comes from `_SENDING_HOOK_FILENAME`, the
same constant `main()`'s call site uses — it was two independent literals
until C2b's review, which is the copy-drift shape this declaration exists
to remove; the RECEIVING-side hook filenames instead come from
`_POST_SYNC_HOOK_FILENAMES` itself, both as the `hook-gate-region` clause's
per-hook-file fan-out and as the `file-path` clause below."""
