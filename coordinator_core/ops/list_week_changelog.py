"""
coordinator_core.ops.list_week_changelog — week-changelog inventory lister,
a substrate-blindness guard.

Purpose: `/workweek-complete` Step 1a calls this to force an explicit ledger
read before any "no ledger" claim is allowed downstream. For each daily file
under `state/week-changelog/` (excluding `HEADER.md`), prints the basename,
total line count, and count of lines starting with a commit SHA (`^[a-f0-9]{7,}`
— nonzero means substantive content present). Then prints the HEADER's
`Week starting` / `Last /workweek-start` marker lines (or an absence note).

Idempotent, read-only, exit 0 on empty (the absence IS the signal) and exit 0
on ANY resolution failure (fail-safe advisory — mirrors the bash oracle's
`set -uo pipefail` + `trap 'exit 0' ERR`).

Port of: list-week-changelog.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-bash-polyglot-clean-slate-full-5c71ee

Negative-spec (do NOT "fix" mid-port — faithfully reproduced oracle quirks):
    - Does NOT resolve state root via the coordinator-state-root.sh bash seam
      directly — self-contains the DR-047/stop-the-rot Rule-5 default-branch
      resolution (bare `coordinator_state_root()`, no --central/--subject/
      --artifact): meta-repo cwd (git root == CLAUDE_HOME) routes to
      CLAUDE_KLABAUTER_ROOT/state; any other (sibling-repo) cwd uses <git-root>/state
      directly. Mirrors the sibling pattern in
      ops/check_weekly_staleness.py::_resolve_state_root (same shape,
      independently duplicated per that module's own documented rationale —
      no shared helper exists yet).
    - The optional `repo_root` positional argument (`ROOT` in the bash
      oracle) is used ONLY for the "is this a git repo" guard and its own
      error message — it does NOT feed the state-root resolution, which
      always uses the actual process cwd's git root. This is a pre-existing
      oracle quirk (`coordinator_state_root` never reads the bash script's
      `$ROOT` variable either), faithfully reproduced, not fixed.
    - The bash oracle's `commits=$(grep -cE ... || echo 0)` has a bug: when
      grep finds ZERO matching lines, `grep -c` still prints "0" to stdout
      (exit code 1), and because of the `||`, `echo 0` ALSO runs and its
      output is captured into the same command substitution — so `commits`
      becomes the two-line string "0\n0" instead of "0". This means every
      zero-commit-line file prints an extra bare "0" line in the output.
      This module reproduces that exact doubled-zero artifact byte-for-byte
      (see `_grep_commit_count_bugcompat`) rather than fixing it to a clean
      single "0".
    - Does NOT write any state; read-only.
"""

from __future__ import annotations

import os
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags, same_path
import sys
from pathlib import Path
from typing import List, Optional

from coordinator_core._settings_home import settings_home

_CLAUDE_KLABAUTER_ROOT_ENV = "CLAUDE_KLABAUTER_ROOT"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"
_STATE_ROOT_OVERRIDE_ENV = "LWC_TEST_STATE_ROOT"

_COMMIT_LINE_RE = re.compile(r"^[a-f0-9]{7,}")
_WEEK_MARKER_RE = re.compile(r"^\*\*(Week starting|Last /workweek-start):")


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Return the path to _machine_local.py, settings-home first, honouring
    MACHINE_LOCAL_IMPL for tests.

    Settings-home-first per DR-210 Amendment 2026-07-24 ("coordinator resolves
    nothing through ``~/.claude/bin``"); the retired compat mirror stays as a
    last-resort rung only. Negative-spec: does NOT stop consulting the mirror —
    a machine whose settings-home copy is absent must still resolve.
    """
    override = os.environ.get("MACHINE_LOCAL_IMPL")
    if override:
        return override
    settings_home_impl = os.path.join(str(settings_home()), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``machine-local get <key>`` and return the value, or None on failure."""
    impl = _machine_local_impl()
    if not os.path.exists(impl):
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root: CLAUDE_KLABAUTER_ROOT env, else machine-local, else None."""
    override = os.environ.get(_CLAUDE_KLABAUTER_ROOT_ENV, "").strip()
    if override:
        return override
    return _machine_local_get("repos.claude_klabauter")


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Promoted from realpath-only to
    samefile-then-fallback semantics: broader (junction-aware) equality is
    correct here since this call site only checks "is git_root the meta-repo
    home", where a junction-aliased home must compare equal."""
    return same_path(a, b)


def _git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Resolve the current working directory's git repo root, or None if not in one."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_root: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _resolve_state_root() -> Optional[str]:
    """Resolve the coordinator state root — mirrors `coordinator_state_root`
    (Rule 5: bare call, no flags).

    Test seam: LWC_TEST_STATE_ROOT, if set, is returned verbatim (bypasses
    git/meta-repo resolution entirely) — used by the co-located pytest.
    """
    override = os.environ.get(_STATE_ROOT_OVERRIDE_ENV)
    if override:
        return override

    git_root = _git_root()
    if not git_root:
        return None

    if _same_path(git_root, _claude_home()):
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            return None
        return os.path.join(claude_klabauter_root, "state")

    return os.path.join(git_root, "state")


def _grep_commit_count_bugcompat(path: Path) -> str:
    """Reproduce the bash oracle's `grep -cE '^[a-f0-9]{7,}' "$f" || echo 0` bug.

    When zero lines match, grep -c still exits 1 (no match) but has already
    printed "0" to stdout; the bash `||` then also runs `echo 0`, and BOTH
    lines land in the command substitution — so the captured value is the
    two-line string "0\n0", not "0". When >=1 line matches, grep -c exits 0
    printing just the count, and `echo 0` never runs. Returns the exact
    string that would have been captured by the bash `$(...)`.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        print(f"skip: _grep_commit_count_bugcompat: text = path.read_text(errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
        return "0\n0"
    count = sum(1 for line in text.splitlines() if _COMMIT_LINE_RE.match(line))
    if count == 0:
        return "0\n0"
    return str(count)


def _wc_l(path: Path) -> str:
    """Mirror `wc -l < "$f" | tr -d ' '` — count of newline-terminated lines."""
    try:
        with path.open("rb") as fh:
            data = fh.read()
    except OSError:
        print(f"skip: _wc_l: with path.open(\"rb\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    return str(data.count(b"\n"))


def main(argv: List[str]) -> int:
    try:
        return _main(argv)
    except Exception:
        # Mirrors the bash oracle's `set -uo pipefail` + `trap 'exit 0' ERR`:
        # any unhandled failure is advisory-only, never a hard failure.
        return 0


def _main(argv: List[str]) -> int:
    root = argv[0] if argv else (_git_root() or os.getcwd())

    is_repo = _git_root(cwd=root) is not None
    if not is_repo:
        print(
            f"list-week-changelog.sh: {root} is not a git repo "
            f"(cwd={os.getcwd()}) — cannot resolve state/week-changelog/",
            file=sys.stderr,
        )
        return 0

    state_root = _resolve_state_root()
    if state_root is None:
        # No parity oracle for this branch in the bash original (its state-root
        # resolver fail-loud-returns 1, which the ERR trap then converts to a
        # silent exit 0) — mirror that: print nothing further, exit 0.
        return 0

    changelog_dir = Path(state_root) / "week-changelog"

    if not changelog_dir.is_dir():
        print(f"(no state/week-changelog/ directory at {root})")
        return 0

    any_found = False
    for entry in sorted(changelog_dir.glob("*.md")):
        base = entry.name
        if base == "HEADER.md":
            continue
        any_found = True
        lines = _wc_l(entry)
        commits = _grep_commit_count_bugcompat(entry)
        print(f"{base}  lines={lines}  commit-lines={commits}")

    if not any_found:
        print("(no daily files)")

    print("---")

    header = changelog_dir / "HEADER.md"
    if header.is_file():
        try:
            text = header.read_text(errors="replace")
        except OSError:
            text = ""
        matched = False
        for line in text.splitlines():
            if _WEEK_MARKER_RE.match(line):
                print(line)
                matched = True
        if not matched:
            print("(HEADER has no Week-starting / Last-workweek-start markers)")
    else:
        print("(no HEADER.md)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
