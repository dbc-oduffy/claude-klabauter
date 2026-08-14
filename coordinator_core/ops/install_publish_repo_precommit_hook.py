"""
coordinator_core.ops.install_publish_repo_precommit_hook — OSS publish-repo
pre-commit exec-bit drift gate + illegal-path gate installer.

Purpose: writes (or upgrades) `.git/hooks/pre-commit` in the OSS
coordinator-claude publish repo with a POSIX-sh shim that runs two gates on
every commit: (1) the exec-bit drift gate (node --test against
coordinator/tests/plugin-ecosystem/exec-bit.test.js), and (2) the
NTFS-illegal-path gate (coordinator/bin/check-no-illegal-paths.sh).
Idempotent and conditional: only installs when the running git repo's
canonical root matches the caller-supplied EXPECTED_REPO_ROOT. Repos that are
NOT the expected publish target are a clean no-op skip.

Foreign-hook handling: if `.git/hooks/pre-commit` already exists with content
other than the coordinator exec-bit shim, print a one-line offer-shape
message and continue — never blocks the caller (e.g. install.sh).

Upgrade-path handling: if the exec-bit gate is already installed but the
illegal-path gate is absent (hook written before D4), the paths check is
appended to the existing hook without regenerating it.

Port of: install-publish-repo-precommit-hook.sh (DoE b5a4192c, 2026-07-20)
Spec backlinks:
    docs/plans/2026-06-11-exec-bit-install-surface-completion.md § Chunk 5
    docs/plans/2026-06-30-cross-platform-file-naming-helper.md § Wave D4
    docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md (residual)

Negative-spec:
    - Does NOT delete or overwrite a foreign (non-coordinator) pre-commit
      hook — offers a manual-install command instead, mirroring the bash
      oracle's offer-shape (never mistake this for a bug: this is the
      documented "do not block install.sh" behavior).
    - The generated shim's identity guard baked-in path comparison uses the
      SAME `_canon`-style empty-string-on-failure semantics as the bash
      oracle: a failed `cd` during canonicalization yields an empty string,
      which can only equal another empty string (both sides empty is the
      sole false-positive-match case, exactly mirroring the shell version —
      not "fixed" here).
    - Missing-SCRIPT cases (the illegal-path helper or the exec-bit test file
      genuinely ABSENT on disk) remain self-healing NON-BLOCKING skips —
      deliberately unchanged by the 2026-07-28 D2 fix below. This is a real
      judgement call documented here rather than silently assumed: a publish
      repo may legitimately not carry `coordinator/bin/check-no-illegal-paths.sh`
      (it's an internal helper, not something every OSS publish target is
      expected to vendor), so its absence alone is not evidence of a broken
      install the way an unrunnable-but-present helper is (see D3 below).
      A present-but-non-regular-file helper (e.g. a directory landed at that
      path) is a different, genuinely anomalous case and fails LOUD (D3).
      Review: code-reviewer F4 (2026-07-28) — a genuinely-absent illegal-path
      helper is now OBSERVABLE, not fully silent: it prints a low-noise
      informational stderr line ("skipped -- no helper vendored ... by
      design, not a regression") so `git commit -v` output distinguishes
      "this publish repo never carries the helper, working as intended"
      from an install regression, without changing the non-blocking outcome.

2026-07-28 D2 fix — missing-INTERPRETER cases now fail LOUD, not silently.
Prior to this fix, both the top-of-script `command -v bash || exit 0` guard
and the `if ! command -v node; then exit 0; fi` guard silently exited the
WHOLE script (both gates) whenever bash or node was unresolvable on PATH —
the same "gate present but inert" shape the meta-repo installer's own
2026-07-28 rewrite (see install_meta_repo_precommit_hook.py's module
docstring) already fixed for its own gates. Mirrored here: each gate now
independently detects its own missing interpreter, prints a named BLOCKED
banner + remediation, and exits 1 UNLESS the caller sets the matching
COORDINATOR_OVERRIDE_PRECOMMIT_{{BASH,NODE}}_MISSING=1 escape hatch (an
operator-authorized bypass of "the gate could not run at all", never of a
real finding). This also fixes an ordering defect: the bash guard used to
run before the identity/repo-match check, so a bash-less box would report
BLOCKED even on unrelated commits outside the OSS publish repo; it is now
scoped to only the bash-dependent gate (illegal-path), after the identity
guard has already passed.
Behavior note: as a side effect of moving the interpreter checks to their
own gates (rather than one whole-script early exit), a missing
`exec-bit.test.js` file no longer also skips gate 2 (illegal-path) — each
gate's self-healing skip is now independent of the other's, matching the
"one gate's absence must not silently swallow another gate" principle this
fix is applying throughout.

2026-07-28 D3 fix — present-but-unrunnable illegal-path helper now fails
LOUD, not silently. The gate previously tested `[ -x "$_paths_check" ]`
before invoking it, so a helper that existed but lacked the exec bit made
the gate vanish with zero output, indistinguishable from the gate running
and finding nothing clean. The exec bit was never actually required: the
gate invokes its interpreter explicitly (`bash "$_paths_check"`), and
passing a script to an interpreter by name never needs the exec bit —
`-x` was gating on the wrong property. Fixed by gating on `-f` (existence
as a regular file) instead, which both closes the silent-skip (a present,
non-executable script now runs normally) and correctly still catches the
one remaining anomalous case: something existing at that path that is NOT
a runnable regular file (e.g. a directory). That case fails LOUD with a
named BLOCKED banner, exiting 1 UNLESS the caller sets
COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS=1. A genuinely ABSENT helper
(nothing at that path at all) is unaffected by this fix and remains a
silent self-healing skip — see the negative-spec block above for why that
split is deliberate, not an oversight.
"""

from __future__ import annotations

import os
import subprocess
import sys

from coordinator_core.session.declared_writes import declare_write
# Cross-package import of the SSOT doc-pointer display string (same
# precedent write_guards already uses for operator_override_note itself) --
# emitted hook-body remediation text points readers at the doc that
# enumerates these keys, never names a key inline (B6/B8, see
# docs/wiki/guard-messaging.md § Register). Repo-qualified ("claude-klabauter
# <path>"), so it stays fleet-addressable when this hook fires inside the
# OSS publish repo's own tree, not just claude-klabauter's.
from coordinator_core.bash_guards._helpers import OVERRIDE_KEYS_DOC_DISPLAY

GENERATES = []  # writes only the OSS publish repo's own .git/hooks/pre-commit, never tracked

_PROG = "install-publish-repo-precommit-hook"

_GATE_MARKER = "coordinator-oss-exec-bit-gate"
_PATHS_MARKER = "check-no-illegal-paths"

_APPEND_TEMPLATE = """
# === OSS publish-repo illegal-path gate (appended by install-publish-repo-precommit-hook.sh) ===
# check-no-illegal-paths: catches commits with NTFS-illegal filenames.
# Self-healing: a genuinely ABSENT helper exits 0 silently (a publish repo may
# legitimately not carry it). A helper that EXISTS but is not a runnable
# regular file (e.g. a directory landed at that path) fails LOUD instead of
# silently vanishing (D3, 2026-07-28) -- gated on `-f` (existence), never
# `-x`: the gate is invoked via an explicit interpreter (`bash "$_paths_check"`),
# which never requires the exec bit, so `-x` was both unnecessary and the
# source of the silent skip this fix closes. Missing bash interpreter fails
# LOUD (D2, 2026-07-28) instead of silently no-opping the gate.
_paths_check="$_cur/coordinator/bin/check-no-illegal-paths.sh"
if ! command -v bash >/dev/null 2>&1; then
  if [ "${COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING:-}" = "1" ]; then
    echo "pre-commit: illegal-path gate SKIPPED -- no bash interpreter found on PATH (override set)." >&2
  else
    echo "pre-commit: BLOCKED -- illegal-path gate cannot run: no bash interpreter found on PATH." >&2
    echo "pre-commit: remediation: install bash. See __OVERRIDE_DOC_POINTER__ for override options." >&2
    exit 1
  fi
else
  if [ -f "$_paths_check" ]; then
    bash "$_paths_check" || exit $?
  elif [ -e "$_paths_check" ]; then
    if [ "${COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS:-}" = "1" ]; then
      echo "pre-commit: illegal-path gate SKIPPED -- helper exists but is not a runnable file: $_paths_check (override set)." >&2
    else
      echo "pre-commit: BLOCKED -- illegal-path gate cannot run: helper exists but is not a runnable file: $_paths_check." >&2
      echo "pre-commit: remediation: replace $_paths_check with a regular file. See __OVERRIDE_DOC_POINTER__ for override options." >&2
      exit 1
    fi
  else
    echo "pre-commit: illegal-path gate skipped -- no helper vendored at $_paths_check (this publish repo does not carry it; by design, not a regression)." >&2
  fi
fi
"""

_FRESH_HOOK_TEMPLATE = """#!/bin/sh
# coordinator-oss-exec-bit-gate — OSS publish-repo pre-commit gates.
# 1. exec-bit drift gate (coordinator-oss-exec-bit-gate)
# 2. illegal-path gate (check-no-illegal-paths) — D4
# Identity-scoped to this repo (canonical path baked in at install time).
# Exits 0 silently when run outside the expected repo.
# Missing SCRIPT/test-file is self-healing (silent skip); missing
# INTERPRETER (node for gate 1, bash for gate 2) fails LOUD with a named
# banner and a per-gate COORDINATOR_OVERRIDE_PRECOMMIT_*_MISSING bypass
# (D2, 2026-07-28) instead of silently exiting the whole script.
#
# Spec backlinks:
#   docs/plans/2026-06-11-exec-bit-install-surface-completion.md § Chunk 5
#   docs/plans/2026-06-30-cross-platform-file-naming-helper.md § Wave D4
OSS_REPO_ROOT="{expected_repo_root}"
_canon() {{
  # Review: reviewer — return empty string on cd failure so a failed _canon never
  # matches a non-empty expected path (safe skip rather than masked edge case).
  [ -n "$1" ] || {{ echo ""; return; }}
  (cd "$1" 2>/dev/null && pwd -P) || {{ echo ""; }}
}}
_cur="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "$_cur" ] || exit 0
if [ "$(_canon "$_cur")" != "$(_canon "$OSS_REPO_ROOT")" ]; then
  exit 0
fi

# Gate 1: exec-bit drift (skipped entirely, loudly or per override, if node
# is unresolvable — never silently disables gate 2 below as well).
if [ "${{COORDINATOR_OVERRIDE_PRECOMMIT_EXEC_BIT:-}}" = "1" ]; then
  echo "pre-commit: exec-bit check skipped (override set)." >&2
elif ! command -v node >/dev/null 2>&1; then
  if [ "${{COORDINATOR_OVERRIDE_PRECOMMIT_NODE_MISSING:-}}" = "1" ]; then
    echo "pre-commit: exec-bit gate SKIPPED -- no node interpreter found on PATH (override set)." >&2
  else
    echo "pre-commit: BLOCKED -- exec-bit gate cannot run: no node interpreter found on PATH." >&2
    echo "pre-commit: remediation: install node. See __OVERRIDE_DOC_POINTER__ for override options." >&2
    exit 1
  fi
else
  EXEC_BIT_TEST="$_cur/coordinator/tests/plugin-ecosystem/exec-bit.test.js"
  if [ -f "$EXEC_BIT_TEST" ]; then
    if ! node --test "$EXEC_BIT_TEST" >&2 2>&1; then # verify-no-console-flash: allow — pre-commit hook, runs on explicit git commit only
      # Review: reviewer — >&2 first redirects stdout to stderr, then 2>&1 merges
      # remaining stderr to the same fd; the prior order 2>&1 >&2 was wrong: it
      # duplicated stderr to current stdout (tty) before redirecting stdout to stderr.
      echo "" >&2
      echo "pre-commit: exec-bit drift detected (above). Fix and re-commit." >&2
      echo "See __OVERRIDE_DOC_POINTER__ for override options." >&2
      exit 1
    fi
  fi
fi

# Gate 2: NTFS-illegal path check — catches commits with illegal filenames.
# check-no-illegal-paths. Self-healing: a genuinely ABSENT helper exits 0
# silently; a helper that EXISTS but is not a runnable regular file fails
# LOUD (D3, 2026-07-28). Gated on `-f`, never `-x` -- see _APPEND_TEMPLATE's
# comment above for why the exec bit is unnecessary here.
_paths_check="$_cur/coordinator/bin/check-no-illegal-paths.sh"
if ! command -v bash >/dev/null 2>&1; then
  if [ "${{COORDINATOR_OVERRIDE_PRECOMMIT_BASH_MISSING:-}}" = "1" ]; then
    echo "pre-commit: illegal-path gate SKIPPED -- no bash interpreter found on PATH (override set)." >&2
  else
    echo "pre-commit: BLOCKED -- illegal-path gate cannot run: no bash interpreter found on PATH." >&2
    echo "pre-commit: remediation: install bash. See __OVERRIDE_DOC_POINTER__ for override options." >&2
    exit 1
  fi
else
  if [ -f "$_paths_check" ]; then
    bash "$_paths_check" || exit $?
  elif [ -e "$_paths_check" ]; then
    if [ "${{COORDINATOR_OVERRIDE_PRECOMMIT_ILLEGAL_PATHS:-}}" = "1" ]; then
      echo "pre-commit: illegal-path gate SKIPPED -- helper exists but is not a runnable file: $_paths_check (override set)." >&2
    else
      echo "pre-commit: BLOCKED -- illegal-path gate cannot run: helper exists but is not a runnable file: $_paths_check." >&2
      echo "pre-commit: remediation: replace $_paths_check with a regular file. See __OVERRIDE_DOC_POINTER__ for override options." >&2
      exit 1
    fi
  else
    echo "pre-commit: illegal-path gate skipped -- no helper vendored at $_paths_check (this publish repo does not carry it; by design, not a regression)." >&2
  fi
fi
"""


def _canon(path: str) -> str:
    """Canonicalize a path the same way the bash oracle's `canon()` does.

    C19 — retired the `["/bin/sh", "-c", 'cd "$1" && pwd -P', ...]` bridge:
    `pwd -P` after a successful `cd` resolves every symlink component and
    prints the physical absolute path, which is exactly `os.path.realpath`'s
    contract. This is NOT the :64 carve-out (that one generates a POSIX-sh
    git-hook BODY that git itself execs; this is claude-klabauter's own runtime
    resolving a path, no git-hook-exec structural reason applies).

    Empty input, or a `cd`-equivalent failure (path is not a directory),
    both yield "" — never raises.

    Fixed 2026-07-21: previously shelled out to a literal ``/bin/sh`` to run
    ``cd "$1" && pwd -P``, mirroring the bash oracle byte-for-byte. That path
    does not exist on native Windows (even Git for Windows' shell lives at
    ``...\\Git\\bin\\sh.exe``, never the POSIX absolute path), so the
    subprocess raised ``FileNotFoundError`` (an ``OSError`` subclass) on
    every call and this always returned "" — meaning `repo_root` and
    `expected_repo_root` compared "" == "" (the documented empty-matches-
    empty edge case) on EVERY invocation, silently defeating the identity
    guard: any repo, expected or not, was treated as a match. Now uses pure
    `os.path.realpath` resolution — no subprocess, no platform-specific
    shell dependency — which also fixes a second latent defect: comparing
    this against a `repo_root` value straight from `git rev-parse
    --show-toplevel` (always forward-slash, even on Windows) would fail to
    match the SAME directory expressed with native backslashes, since both
    sides are independently resolved through this same function.
    """
    if not path:
        return ""
    if not os.path.isdir(path):
        return ""
    try:
        return os.path.realpath(path)
    except OSError:
        print(f"skip: _canon: return os.path.realpath(path) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""


def _git_repo_root(cwd: str) -> str:
    """Return `git rev-parse --show-toplevel` output for cwd, or "" on failure."""
    try:
        from coordinator_core.win_portability import no_console_creationflags

        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_repo_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def main(argv: list[str]) -> int:
    cwd = os.getcwd()

    # ------------------------------------------------------------------
    # Argument check
    # ------------------------------------------------------------------
    if not argv or not argv[0]:
        print(
            f"{_PROG}: missing EXPECTED_REPO_ROOT argument — skipping.",
            file=sys.stderr,
        )
        return 0
    expected_repo_root = argv[0]
    canonical_expected = _canon(expected_repo_root)

    # ------------------------------------------------------------------
    # Git-repo guard
    # ------------------------------------------------------------------
    repo_root = _git_repo_root(cwd)
    if not repo_root:
        print(f"{_PROG}: not in a git repo — skipping.", file=sys.stderr)
        return 0

    # ------------------------------------------------------------------
    # Identity guard: only install inside the expected OSS publish repo.
    # ------------------------------------------------------------------
    if _canon(repo_root) != canonical_expected:
        print(
            f"{_PROG}: not the expected OSS repo ({repo_root}) — skipping.",
            file=sys.stderr,
        )
        return 0

    # `repo_root` is git's own output (`rev-parse --show-toplevel`), which is
    # ALWAYS forward-slash even on native Windows — os.path.join would then
    # append native-separator segments onto a forward-slash prefix, producing
    # a mixed-separator string (neither valid POSIX nor valid native display
    # form). This is a real filesystem path (opened/chmod'd below, not a wire
    # value), so normpath() to one consistent native form for both fs use and
    # display, rather than routing through wire_paths.rel_id (POSIX-only).
    hook_path = os.path.normpath(os.path.join(repo_root, ".git", "hooks", "pre-commit"))

    # ------------------------------------------------------------------
    # Idempotency: paths check present -> both gates wired, fully installed.
    # ------------------------------------------------------------------
    existing = ""
    if os.path.isfile(hook_path):
        try:
            with open(hook_path, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        except OSError:
            existing = ""

    if os.path.isfile(hook_path) and _PATHS_MARKER in existing:
        print(
            f"{_PROG}: gates already installed at {hook_path} — no-op.",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # Upgrade path: exec-bit gate present but illegal-path gate absent.
    # ------------------------------------------------------------------
    if os.path.isfile(hook_path) and _GATE_MARKER in existing:
        with open(hook_path, "a", encoding="utf-8") as fh:
            fh.write(_APPEND_TEMPLATE.replace("__OVERRIDE_DOC_POINTER__", OVERRIDE_KEYS_DOC_DISPLAY))
        # DR-276: declared AFTER the write lands, at the FINAL destination.
        declare_write(hook_path)
        print(
            f"{_PROG}: appended illegal-path gate to existing {hook_path}.",
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # Foreign-hook handling: existing hook, not the coordinator shim.
    # ------------------------------------------------------------------
    if os.path.isfile(hook_path):
        print(
            f"{_PROG}: an existing pre-commit hook is in place; the coordinator "
            "guards were NOT installed — install manually by running: bash "
            f'coordinator/bin/install-publish-repo-precommit-hook.sh "$(pwd)"',
            file=sys.stderr,
        )
        return 0

    # ------------------------------------------------------------------
    # Fresh-install path: write the canonical OSS shim.
    # ------------------------------------------------------------------
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    hook_body = _FRESH_HOOK_TEMPLATE.format(expected_repo_root=canonical_expected).replace(
        "__OVERRIDE_DOC_POINTER__", OVERRIDE_KEYS_DOC_DISPLAY
    )
    with open(hook_path, "w", encoding="utf-8") as fh:
        fh.write(hook_body)
    os.chmod(hook_path, 0o755)
    # DR-276: declared AFTER the write lands, at the FINAL destination.
    declare_write(hook_path)
    print(f"{_PROG}: installed {hook_path}.", file=sys.stderr)
    return 0
