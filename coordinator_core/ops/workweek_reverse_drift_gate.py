"""
coordinator_core.ops.workweek_reverse_drift_gate — /workweek-complete Step 4g
blocking merge gate: runs every registered `reverse_drift_cmd` and folds the
3-way result into one exit code.

Purpose: `list_reverse_drift_cmds._run` (the discovery half) already emits
`<plugin>|<source_path>|<reverse_drift_cmd>` rows plus a business exit code
that distinguishes "genuinely N/A" (0) from "structurally misconfigured" (3)
from "reader error" (anything else). This module is the EXECUTION half: it
consumes that 3-way rc, and — only on the clean rc==0 path — cd's into each
row's source_path and runs its reverse_drift_cmd as a blocking merge gate,
folding per-command pass/fail into one aggregate exit code. Previously
inlined bash in `/workweek-complete` Step 4g (DoE-claude
coordinator/commands/workweek-complete.md); ported here so the ceremony step
becomes a single CLI invocation.

Exit codes (mirrors the bash oracle's `[[ ... ]] || exit 1` gate exactly,
folded through the `COORDINATOR_OVERRIDE_REVERSE_DRIFT=1` escape hatch):
  0 — gate passed (no copy_install plugins, all reverse_drift_cmd rows
      succeeded, OR override is set and would otherwise have failed).
  1 — gate failed: reader reported misconfiguration (rc==3), reader reported
      an unexpected error (rc not in {0, 3}), or >=1 reverse_drift_cmd
      exited non-zero — UNLESS COORDINATOR_OVERRIDE_REVERSE_DRIFT=1, in which
      case the failure is reported but does not block (matches the bash
      oracle's per-branch `|| exit 1` override check).

Each registered reverse_drift_cmd is parsed with `shlex.split` and executed
directly (no shell) from its plugin's source_path — replaces the former
`bash -euo pipefail -c <cmd>` shell-out. A value containing a shell
metacharacter that implies real shell semantics (`|`, `&&`, `||`, `;`, `>`,
`<`, backtick, `$(`) is refused rather than silently mis-executed as a flat
argv (see _shell_metachar_check) — same fail-loud-on-ambiguous-value posture
`coordinator-resolve-validation-cmd.py` established for the analogous
COORDINATOR_FAST_TEST_CMD/full_test_cmd config values, and the same guard
list `docs/2026-07-29-debash-residual-sites-spec.md` Group B specifies for
the two other user-configured-command call sites this repo ports in the same
pass. Shell-safety of the value itself remains the operator's responsibility
otherwise (advisory double-quote warning lives in list_reverse_drift_cmds,
not here); a plain argv command (no metacharacters) runs with identical
fail-fast semantics to the retired `bash -e` wrapper — Python's subprocess
model already treats a non-zero exit as failure without needing `-e` to say
so, and there is no pipeline of shell built-ins left to reorder once the
value has been refused as non-argv-shaped.

Port source: coordinator/commands/workweek-complete.md § Step 4g (DoE-claude),
the per-plugin execution loop with 3-way rc branching (bash lines ~1730-1765).
Discovery half (list-reverse-drift-cmds) was already ported; this module
covers ONLY the previously-still-bash execution loop.
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
  - Does NOT re-implement registry discovery or per-repo scoping — both are
    delegated verbatim to `list_reverse_drift_cmds._run`. This module owns
    only the execution loop and the 3-way rc fold.
  - Does NOT swallow a reverse_drift_cmd's own stdout/stderr — subprocess
    inherits the caller's streams so operator-visible remediation output
    from the reverse_drift_cmd itself still surfaces directly.
  - Does NOT spawn a shell for a reverse_drift_cmd value — `shlex.split` plus
    a direct `subprocess.run` on the resulting argv. A value that needs real
    shell semantics (pipe, `&&`/`||` chaining, `;` sequencing, redirection,
    command substitution) is refused with a remediation message and counted
    as a gate failure rather than being silently run as a mis-split argv
    (docs/2026-07-29-debash-residual-sites-spec.md Group D).
  - Does NOT default --scope-repo to "check every plugin on the machine" —
    unlike `list_reverse_drift_cmds` (whose bare invocation is
    all-rows-legacy), this CLI's `main()` always resolves a concrete repo
    root (via `git rev-parse --show-toplevel`, falling back to cwd) when
    --scope-repo is omitted, matching the ceremony step's `REVDRIFT_SCOPE_REPO`
    computation — bare invocation from a non-meta repo must never accidentally
    widen to "check every copy_install plugin on the machine."
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from coordinator_core.win_portability import no_console_creationflags, no_console_passthrough_kwargs
import sys
from typing import List, Optional, Tuple

from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.ops.list_reverse_drift_cmds import _run

_PROG = "workweek-complete-reverse-drift-gate"

# Same guard list as Group B's user-configured-command call sites
# (docs/2026-07-29-debash-residual-sites-spec.md) — a reverse_drift_cmd
# containing one of these implies real shell semantics a flat argv cannot
# reproduce (piping, chaining, sequencing, redirection, command
# substitution). Deliberately does NOT flag bare `$` (plain variable
# reference) — same exclusion list_reverse_drift_cmds._run's own advisory
# quote-check already makes for this config value.
_SHELL_METACHAR_RE = re.compile(r"&&|\|\||[|;<>`]|\$\(")


def _shell_metachar_check(cmd: str) -> Optional[str]:
    """Returns the matched substring if `cmd` contains a shell metacharacter
    this direct-exec port cannot safely honor; None when `cmd` looks like a
    plain argument vector safe for shlex.split + subprocess.run(shell=False).
    """
    match = _SHELL_METACHAR_RE.search(cmd)
    return match.group(0) if match else None


_USAGE = """\
Usage: workweek-complete-reverse-drift-gate [--scope-repo <repo-root>]
Blocking /workweek-complete Step 4g merge gate: runs every registered
copy_install plugin's reverse_drift_cmd and folds pass/fail into one exit code.
  --scope-repo <repo-root>  Repo root to scope discovery to (default: resolved
                            via `git rev-parse --show-toplevel`, falling back
                            to cwd — matches the ceremony step's own default).
Exit: 0 = gate passed (incl. genuinely N/A, or override set on a would-be
          failure); 1 = gate failed (misconfigured reader, reader error, or
          >=1 reverse_drift_cmd failed) and no override is set.
Environment: COORDINATOR_OVERRIDE_REVERSE_DRIFT=1 downgrades any failure to a
             reported-but-non-blocking warning (matches the bash oracle's
             per-branch override check).
"""


def _default_scope_repo() -> str:
    """Resolve the releasing repo root: `git rev-parse --show-toplevel`,
    falling back to cwd on any failure (no git, not a repo, etc.) — mirrors
    the ceremony step's own `REVDRIFT_SCOPE_REPO="$(git rev-parse
    --show-toplevel 2>/dev/null || pwd)"` computation verbatim."""
    top = show_toplevel()
    if top:
        return top
    return os.getcwd()


def run_gate(scope_repo: Optional[str], *, override: Optional[bool] = None) -> Tuple[List[str], int]:
    """Runs the full discover-then-execute gate. Returns (messages, exit_code).

    `override` defaults to reading COORDINATOR_OVERRIDE_REVERSE_DRIFT from the
    environment when not explicitly passed (test seam).

    Deliberate isolation boundary — do not convert the reader spawn to an
    in-process import. Mechanism: crash containment + cwd isolation — the
    gate runs against a foreign source_path (a registered plugin mirror),
    whose reverse_drift_cmd is project-declared and must not crash or leak
    cwd into this process. See
    state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md.
    """
    if override is None:
        override = os.environ.get("COORDINATOR_OVERRIDE_REVERSE_DRIFT", "0") == "1"

    messages: List[str] = []
    stdout_lines, stderr_lines, rc = _run(scope_repo)
    messages.extend(stderr_lines)

    if rc == 3:
        messages.append(
            "Reverse-drift gate MISCONFIGURED — copy_install plugins exist but none "
            "have a reverse_drift_cmd. Register with: machine-local set "
            "plugin.mirrors.<name>.reverse_drift_cmd '<invocation>'. See "
            "docs/wiki/machine-local-registry.md § reverse_drift_cmd."
        )
        return messages, (0 if override else 1)

    if rc != 0:
        messages.append(
            f"Reverse-drift gate: reader exited with an unexpected code (rc={rc}) — "
            "e.g. a registry-read or invocation error. Check the reader output above "
            "before merging."
        )
        return messages, (0 if override else 1)

    # rc == 0: run each registered command from its source_path. Empty rows =
    # no copy_install plugins on this machine = genuinely N/A (clean pass).
    any_failed = False
    for row in stdout_lines:
        parts = row.split("|", 2)
        if len(parts) != 3 or not parts[0]:
            continue
        plugin, source_path, cmd = parts

        metachar = _shell_metachar_check(cmd)
        if metachar:
            any_failed = True
            messages.append(
                f"Reverse-drift gate: {plugin}'s reverse_drift_cmd contains a "
                f"shell metacharacter ({metachar!r}) this gate cannot safely "
                "execute without a shell — express it as a plain argument "
                "vector (no pipes, &&/||chaining, ; sequencing, redirection, "
                "backticks, or $(...) command substitution), or wrap it in a "
                f"script and register that script's path instead. Command: "
                f"{cmd!r} in {source_path!r}."
            )
            continue

        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            any_failed = True
            messages.append(
                f"Reverse-drift gate: {plugin}'s reverse_drift_cmd could not "
                f"be parsed as a plain argument vector ({exc}) — {cmd!r} in "
                f"{source_path!r}."
            )
            continue

        if not argv:
            # Empty after parsing (e.g. whitespace-only) — matches the retired
            # bash oracle's no-op-empty-script behavior: nothing to run, no
            # failure.
            continue

        result = subprocess.run(
            argv,
            cwd=source_path,
            check=False,
            **no_console_passthrough_kwargs(),
        )
        if result.returncode != 0:
            any_failed = True
            messages.append(
                f"Reverse-drift gate: {plugin}'s reverse_drift_cmd failed "
                f"(rc={result.returncode}) in {source_path!r}."
            )

    if any_failed:
        messages.append(
            "Reverse-drift gate FAILED. Remediation: run "
            "`example_game_repo_recover --step reverse-drift` to back-propagate "
            "live→source, or override with COORDINATOR_OVERRIDE_REVERSE_DRIFT=1."
        )
        return messages, (0 if override else 1)

    return messages, 0


def main(argv: List[str]) -> int:
    scope_repo: Optional[str] = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--help", "-h"):
            print(_USAGE)
            return 0
        if arg == "--scope-repo":
            if i + 1 >= len(argv) or not argv[i + 1]:
                print(f"{_PROG}: --scope-repo requires a path argument", file=sys.stderr)
                return 2
            scope_repo = argv[i + 1]
            i += 2
            continue
        print(f"{_PROG}: unknown argument '{arg}'", file=sys.stderr)
        return 2

    if scope_repo is None:
        scope_repo = _default_scope_repo()

    messages, exit_code = run_gate(scope_repo)
    for line in messages:
        print(line, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
