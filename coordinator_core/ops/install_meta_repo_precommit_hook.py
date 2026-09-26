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
which checks ship in the hook. **COUNT THAT LIST; do not trust a number in
this sentence.** It deliberately no longer states how many gates there are:
the previous wording said "the current four gates" and went stale when
`detect-staged-rollback` was deleted 2026-08-25 (DR-359), after which the
registry held three. That stale four was then copied into an acceptance
criterion and into a cross-repo memo, producing two wrong assertions that a
prior-art check, a coverage check and an Opus review all passed over. Prose
about a structure does not update when the structure does — see
`state/lessons/2026-08-25-a-comment-describing-state-is-not-the-st-*.yaml`.

Retirement history, which does not go stale:
`coordinator-precommit-exec-bit-check` was retired 2026-07-29 — see
"Orphaned regions" below for the already-installed-hook handling that
retirement needed. `detect-staged-rollback` was added 2026-07-29 (see
"Exit-code clamping" below for why it forced this file's exit-code
discipline to change first) and DELETED 2026-08-25.

Adding a gate is a registry entry, not a new code path: fresh-install,
upgrade-append, and idempotency all derive from it generically.

If a pre-commit hook already exists with content other than this gate chain
(custom hooks, Git LFS prefix, etc.), the installer appends whatever gates
are still missing after the existing block rather than clobbering it.

Port of: install-meta-repo-precommit-hook.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: cross-repo/inbox/2026-06-08-exec-bit-drift-runtime-tripwire-tests.md
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

2026-07-28 REWRITE — fail-loud, self-relative, registry-driven. Replaces two
generations of defect found in the same session:

  1. The ORIGINAL bash-ported version baked a literal
     `$HOME/.claude/plugins/coordinator-claude/coordinator/bin/...` path into
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

2026-08-25 — the same defect a THIRD way: currency was version-only.
`_bin_dir()` resolves self-relative at INSTALL time and that result is baked
into the emitted hook, so the hook is a photograph of wherever the engine
lived that day. `_gate_is_stale()` compared only the `# gate-version: N`
stamp, so moving the engine clone — which bumps no version — was invisible
forever: the gates failed CANNOT-RUN on every commit, and the remediation the
BLOCKED banner itself prints (re-run the installer) reported "already
installed and current — no-op" and changed nothing.

Observed on the Windows box: `~/.claude`'s hook was generated against the
engine clone's original location; that clone later moved to another drive.
Every commit to Claude Central blocked, and a reinstall repaired exactly one
gate — `detect-staged-rollback`, which happened to carry a stale version
stamp — while silently leaving the other two pointing at the dead path.

Fixed by giving `_gate_is_stale()` a PATH axis alongside the version axis
(both derived from `_gate_script_line()`, one source of truth shared with
emission). Deliberately fixed at install time: making the hook resolve the
engine at COMMIT time via the settings-home forwarders was considered and
rejected on cost — that puts an extra Python interpreter start on each of
four gates on every commit, against a floor of ~54 ms best / ~294 ms typical
per start on this hardware. A hook is a hot path; an installer is not.

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

GENERATES = []

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
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY

_PROG = "install-meta-repo-precommit-hook"


@dataclass(frozen=True)
class _Gate:
    marker: str
    filename: str
    kind: str
    label: str
    override_env: str  # env var that bypasses a CANNOT-RUN block (missing script/interpreter)
    # that scenario — every REGISTRY entry still states its version explicitly (see registries
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
    return _show_toplevel(cwd=target)


def _atomic_write(path: str, content: str) -> None:
    tmp_path = f"{path}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    os.replace(tmp_path, path)
    os.chmod(path, 0o755)
    declare_write(path)


def _strip_trailing_exit0(text: str) -> str:
    rstripped = text.rstrip("\n")
    lines = rstripped.split("\n") if rstripped else []
    if lines and lines[-1].strip() == "exit 0":
        lines = lines[:-1]
    result = "\n".join(lines)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def _gate_version_line(gate: _Gate) -> str:
    return f"# gate-version: {gate.version}"


def _gate_script_line(gate: _Gate, bin_dir: Path) -> str:
    return f'_gate_script="{"/".join([bin_dir.as_posix(), gate.filename])}"'


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


def _gate_is_stale(text: str, gate: _Gate, bin_dir: Optional[Path] = None) -> bool:
    """True iff `gate`'s region can be LOCATED in `text` and is out of date on
    either axis: its body does not carry today's version stamp, OR (when
    `bin_dir` is supplied) the path it shells out to is not the path this
    installer would emit now. False if the region cannot be located at all
    (an unrecognized/legacy shape — not a claim this module can safely act
    on, see the `stale_gates` comprehension's docstring at its one call site)
    or if the region is current on both axes. Callers are expected to already
    have established marker presence (`gate.marker in text`) before calling
    this — it does not re-check it.

    THE PATH AXIS (2026-08-25). Version-only currency made a relocated engine
    permanently invisible. `_bin_dir()` resolves self-relative at INSTALL time
    and the result is baked into the emitted hook, so the hook is a photograph
    of wherever the engine lived that day — correct when taken, wrong the
    moment the clone moves. Because a moved engine bumps no `version`, every
    gate stayed "current" forever: the gates failed CANNOT-RUN on every
    commit, and re-running the installer — the exact remediation the BLOCKED
    banner prints — reported "already installed and current — no-op" and
    changed nothing. See the module docstring's 2026-08-25 note for the
    observed case that produced this.

    Fixing this at install time is deliberate, and the alternative was
    rejected on cost: making the hook resolve the engine at COMMIT time (via
    the settings-home forwarders) would put an extra Python interpreter start
    on every gate of every commit — four of them, against a floor of ~54 ms
    best / ~294 ms typical per start on this hardware. A hook is a hot path;
    an installer is not. The path belongs baked, and the installer's job is to
    notice when its own photograph has gone out of date.

    `bin_dir` is optional so the version-only contract stays available to
    callers that have no bin dir in hand (and to the existing tests that
    assert it); the install path always passes it."""
    region = _find_gate_region(text, gate.marker)
    if region is None:
        return False
    start, end, _indent = region
    body = text[start:end]
    if _gate_version_line(gate) not in body:
        return True
    if bin_dir is not None and _gate_script_line(gate, bin_dir) not in body:
        return True
    return False


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


# hit the missing-script CANNOT-RUN branch (loud BLOCKED, `exit 1`) from then

def _find_all_gate_markers(text: str) -> List[str]:
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
            # CANNOT-RUN BLOCKED finding on the gate's next commit instead
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
    return _py_probe_sh.python_probe_lines("_py")


# Named env var for the bash-kind group-level CANNOT-RUN escape hatch, spelled
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
    script_line = _gate_script_line(gate, bin_dir)
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
        # CANNOT-RUN cases above (missing script/interpreter — the gate
        # (as detect-staged-rollback's COORDINATOR_OVERRIDE_PRECOMMIT_MASS_
        # DELETION does internally, before this wrapper ever sees a nonzero
        # code). This is exit-code CLAMPING only: any nonzero `$_gate_rc`
        return [
            f'    echo "pre-commit: BLOCKED -- gate [{gate.label}] ({gate.marker}) reported a problem '
            '(exit code $_gate_rc) -- see output above." >&2',
            "    exit 1",
        ]

    lines = [
        f"# --- Gate: {gate.label} ({gate.marker}) ---",
        _gate_version_line(gate),
        script_line,
        'if [ ! -f "$_gate_script" ]; then',
    ]
    # later commit RETIRING that gate. In the second case a vague pointer
    lines += _cannot_run_branch(
        "missing script $_gate_script",
        "run coordinator/bin/install-meta-repo-precommit-hook.py to restore it, "
        "or coordinator/bin/remove-claude-klabauter-precommit-hook.py if this gate was retired",
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


# post-merge / post-checkout — the RECEIVING-side leg of the 2026-07-28
# or the AUTHORING side (gen_settings_hooks' own kill-switch check); the
# incident's actual TRANSMISSION was a `git merge`/`git pull` on the
# adding a pre-commit gate is a registry entry in `_GATE_REGISTRY`.

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


def _marker_is_installed(text: str, marker: str) -> bool:
    if _find_gate_region(text, marker) is not None:
        return True
    marker_re = re.compile(r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])")
    for line in text.splitlines():
        code_part = line.split("#", 1)[0]
        if marker_re.search(code_part):
            return True
    return False


def _refuse_if_not_engine_root() -> Optional[str]:
    """Refuse to install when THIS module's tree is not the resolved engine root.

    `_bin_dir()` is self-relative and correct by construction — it names the
    `coordinator/bin/` of whichever engine copy is running. That path is then
    baked ABSOLUTE into the emitted hook and frozen there. So the emitter has
    never had a path bug; the hole is that nothing checks WHO is allowed to
    run it. Install from a working tree and the hook tracks that tree forever:
    a branch switch that removes a gate script leaves the hook unable to run
    it, and its only offered remedy is a `COORDINATOR_OVERRIDE_PRECOMMIT_*`
    env var. That is a resolution bug teaching an operator to switch off a
    safety gate. Observed 2026-08-26 (doe-claude-6b): three of four gates
    resolved, one did not, purely because a sibling checkout had moved.

    WHY A REFUSAL AND NOT A RESOLVER CALL IN THE HOOK. Emitting a resolver
    call would move the resolution to fire time and cost an interpreter start
    on the commit hot path, every commit, forever — against a 500ms
    brightline. A refusal is one check paid once at install. It also fails in
    the safe direction: loud at install, versus silent until a gate goes
    missing months later.

    THE DISCRIMINATOR IS THE RESOLUTION CLASS, NOT A PATH COMPARISON. A first
    cut of this guard compared `coordinator_engine_root()` against this
    module's own root and was very nearly a no-op: called from a live working
    tree, that resolver ANSWERS with that tree
    (`('/…/claude-klabauter', 'live-working-tree')` measured here), so the two
    paths agree precisely in the case worth refusing. What distinguishes the
    hazard is the class the resolver already reports beside the path —
    `live-working-tree` means the gate paths about to be frozen into the hook
    belong to a tree whose branches move.

    A PUBLISHED MIRROR MUST EXIST TO REFUSE TOWARD. On a single-tree box
    `live-working-tree` is the only resolution there is, and refusing would
    break the ordinary install to guard a case that needs two trees. So this
    refuses only when `published_engine_mirror_path()` names a registered,
    on-disk mirror that could have been installed from instead — which also
    makes the refusal message able to name the exact command to re-run.
    """
    from coordinator_core.engine_root import (  # noqa: PLC0415 - lazy, install-only path
        coordinator_engine_root_with_class,
        published_engine_mirror_path,
    )

    self_root = Path(__file__).resolve().parents[2]
    try:
        _engine_root, resolution_class = coordinator_engine_root_with_class()
    except (RuntimeError, OSError):
        return None
    if resolution_class != "live-working-tree":
        return None

    mirror = published_engine_mirror_path()
    if not mirror:
        return None
    try:
        mirror_resolved = Path(mirror).resolve()
    except OSError:
        return None
    if mirror_resolved == self_root:
        return None

    return (
        f"install-meta-repo-precommit-hook: refusing — running from a live working "
        f"tree ({self_root}), with a published engine mirror registered at "
        f"{mirror_resolved}.\n"
        f"    Gate paths are absolute and frozen into the hook at install time, so a "
        f"hook installed from here tracks this tree's branches: a checkout that moves "
        f"a gate script away leaves the hook unable to run it, and the only remedy it "
        f"offers is disabling the gate.\n"
        f"    Re-run from the mirror: "
        f"python3 {mirror_resolved}/coordinator/bin/install-meta-repo-precommit-hook.py"
    )


def _install_or_append_hook(repo_root: str, hook_filename: str, gates: List[_Gate]) -> int:
    """Shared install/append logic for a SINGLE named git hook
    (`.git/hooks/<hook_filename>`) against `gates` (in registry order).
    Both `main()` (pre-commit, `_GATE_REGISTRY`) and
    `main_post_sync()` (post-merge/post-checkout, `_POST_SYNC_GATE_REGISTRY`)
    call this — the fresh-install, append, and idempotency behavior is
    identical regardless of which hook filename or registry is in play, so
    it lives in exactly one place rather than being re-derived per hook
    type."""
    refusal = _refuse_if_not_engine_root()
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

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

    orphaned_gates: List[str] = []
    if hook_exists and existing_text:
        current_markers = {g.marker for g in gates}
        existing_text, orphaned_gates = _remove_orphaned_gate_regions(existing_text, current_markers)

    missing_gates = [g for g in gates if not _marker_is_installed(existing_text, g.marker)]
    stale_gates = [
        g
        for g in gates
        if _marker_is_installed(existing_text, g.marker)
        and _gate_is_stale(existing_text, g, bin_dir)
    ]

    if hook_exists and not missing_gates and not stale_gates and not orphaned_gates:
        print(f"{_PROG}: gate already installed and current at {hook_path} — no-op.", file=sys.stderr)
        return 0

    if not hook_exists:
        content = _hook_body(bin_dir, gates)
        _atomic_write(hook_path, content)
        print(f"{_PROG}: installed {hook_path}.", file=sys.stderr)
        return 0

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
