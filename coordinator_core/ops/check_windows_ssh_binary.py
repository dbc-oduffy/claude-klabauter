"""
coordinator_core.ops.check_windows_ssh_binary — warn when git would push over
SSH via an MSYS ssh.exe instead of Win32-OpenSSH.

Why this exists: on Windows, a git hook that shells out to `git push` over
SSH inherits whatever `ssh` binary git resolves. If git resolves an MSYS
ssh.exe (GitHub Desktop's bundled MinGit, VS Team Explorer's Git, cwrsync,
or Git for Windows' own bundled usr/bin/ssh.exe) instead of Win32-OpenSSH,
the push fails auth: MSYS emulates AF_UNIX over loopback TCP and structurally
cannot reach the Win32 named pipe (openssh-ssh-agent) that the SSH agent
listens on. The remedy is 1Password's documented pin:

    git config --global core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe"

This needs a probe, not just documentation, because the coordinator
post-commit auto-push hook exits 0 unconditionally (it must never block a
commit) — so on an unpinned box, work/* pushes fail silently forever and the
ONLY signal is .git/push-failures.log, which nobody reads.

Full background, reproduced two-armed control, and the mechanism writeup:
coordinator/docs/wiki/bash-on-windows-gotchas.md §15 (example-doctrine-repo).

Contract for scripts in bin/install-health/ (see install_health_run's own
docstring): self-gate on OS, exit 0 silently when the gate fails, be
idempotent, never exit non-zero — this probe's own fail-safe direction is
warn-and-continue, not block-install (see main()'s always-0 return).

Known limitation: when GIT_SSH_COMMAND or core.sshCommand is a full command
line (e.g. `"C:/.../ssh.exe" -F /dev/null`), this probe splits on whitespace
to find the binary token, same as bash `read`'s default IFS splitting — a
quoted path containing a space will not resolve cleanly. That's an
acceptable probe-fidelity gap, not a correctness bug: it can only
under-detect (falls through to the "unresolvable" WARN branch), never
mis-report a PASS. Reproduced verbatim from the bash oracle.

Direct-import adaptation: no subprocess hop back into a Python CLI is
needed here (unlike seed_skill_overrides.py's example-doctrine-repo-resident-helper shape) —
this module IS the whole probe, ported 1:1 into a single in-process
function chain. The only subprocess calls are to `git` itself, which has no
Python-native substitute.

Port of: check-windows-ssh-binary.sh (example-doctrine-repo 290997c7, 2026-07-22)
Spec backlink: docs/decisions/ DR-079 (tool 8 assignment to claude-klabauter); source
memo cross-repo/inbox/2026-07-21-claude-central-em-dr079-doe-dispositions-and-install-health-defect.md

Negative-spec:
    - OS gate uses sys.platform.startswith("win") (matches the
      ensure_python3_exe_shim.py sibling convention), not a literal OSTYPE
      string match — the bash oracle gated on OSTYPE (msys*/cygwin*/win32*)
      because it runs AS a bash script under one of those shells; this
      module runs as a native Python process, so the Windows-native
      sys.platform value is the correct analogue. WSL reports the Linux
      platform value and is correctly excluded either way (WSL's git
      resolves a Linux ssh, not a Windows one — non-applicable, same as
      macOS/Linux).
    - Never exits non-zero, in ANY branch, including subprocess/git failure
      paths — this is a warn-only probe; an install-health failure here must
      not fail the install. Reproduced verbatim from the bash oracle's
      unconditional `exit 0` on every path.
    - Does not attempt to repair or pin the ssh binary — reports the hazard
      and exits 0, directing the operator to the documented
      `core.sshCommand` pin. Reproduced verbatim, not "fixed" in this port.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from typing import Optional, Sequence, Tuple

_LOG_PREFIX = "[check-windows-ssh-binary]"
_PIN_HINT = '  git config --global core.sshCommand "C:/Windows/System32/OpenSSH/ssh.exe"'

# fnmatch patterns are matched against the resolved ssh path AFTER
# backslash-to-forward-slash normalization and lower-casing, so every
# pattern here is written lowercase/forward-slash to match 1:1 against the
# bash oracle's `case` patterns (which operated on the same normalized
# value).
_WIN32_OPENSSH_PATTERNS = (
    "*:/windows/system32/openssh/ssh.exe",
    "/?/windows/system32/openssh/ssh.exe",
)
_MSYS_PATTERNS = (
    "*usr/bin/ssh",
    "*usr/bin/ssh.exe",
    "*githubdesktop*",
    "*team explorer*",
    "*teamexplorer*",
    "*cwrsync*",
    "*mingit*",
)


def _is_windows() -> bool:
    """OS gate. WSL reports the Linux platform value — correctly excluded,
    mirroring the sibling ensure_python3_exe_shim.py convention (see module
    docstring "Negative-spec" for why this differs from the bash oracle's
    OSTYPE gate)."""
    return sys.platform.startswith("win")


def _run_git(args: Sequence[str]) -> Optional[str]:
    """Run a git subcommand, returning stripped stdout on success or None on
    any failure (missing git, non-zero exit, no repo, no such config key) —
    collapses every failure mode to the same "not available" signal, matching
    the bash oracle's `2>/dev/null || true` fallback-to-empty shape."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )  # popup-safe-env-suppressed
    except OSError as exc:
        # Review: code-reviewer (Finding 1) — replaced a stringified fragment
        # of the try-block's own source with a plain human sentence.
        print(f"skip: git subcommand failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _has_ssh_remote() -> bool:
    """True iff `git remote get-url origin` resolves to an SSH-shaped URL
    (git@... or ssh://...). Case-sensitive prefix match, matching the bash
    oracle's `case` statement (bash case is case-sensitive by default, no
    nocasematch)."""
    origin_url = _run_git(["remote", "get-url", "origin"]) or ""
    return origin_url.startswith("git@") or origin_url.startswith("ssh://")


def _first_token(value: str) -> str:
    """Whitespace-split and take the first token, matching bash `read -r
    _resolved _ <<<"$value"` — IFS-default splitting, discarding everything
    after the first token (see module docstring "Known limitation")."""
    parts = value.split()
    return parts[0] if parts else ""


def _resolve_ssh_binary() -> Tuple[str, str]:
    """Resolve which ssh binary git would actually use, in git's own
    precedence order: GIT_SSH_COMMAND env var -> core.sshCommand config ->
    GIT_SSH env var -> whatever `ssh` resolves to on PATH.

    Returns (resolved_path, source_label). resolved_path is "" if nothing
    resolves via any of the four sources.
    """
    git_ssh_command = os.environ.get("GIT_SSH_COMMAND", "")
    if git_ssh_command:
        return _first_token(git_ssh_command), "GIT_SSH_COMMAND"

    core_ssh_command = _run_git(["config", "--get", "core.sshCommand"]) or ""
    if core_ssh_command:
        return _first_token(core_ssh_command), "core.sshCommand"

    git_ssh = os.environ.get("GIT_SSH", "")
    if git_ssh:
        return git_ssh, "GIT_SSH"

    return shutil.which("ssh") or "", "PATH"


def _warn(*lines: str) -> None:
    for line in lines:
        print(f"{_LOG_PREFIX} {line}", file=sys.stderr)


def _classify_and_warn(resolved: str, source: str) -> None:
    """Normalize the resolved path and emit the matching warning (or none,
    on PASS). Every branch is a no-op with respect to the process exit code
    — callers always return 0 (see module docstring negative-spec)."""
    resolved_norm = resolved.replace("\\", "/")
    resolved_lc = resolved_norm.lower()

    if resolved_lc == "":
        _warn(
            "no ssh binary resolvable (checked GIT_SSH_COMMAND, core.sshCommand, "
            "GIT_SSH, PATH) — git push over SSH will fail with no clear error. "
            "Pin the binary explicitly:",
            _PIN_HINT,
        )
        return

    if any(fnmatch.fnmatchcase(resolved_lc, pat) for pat in _WIN32_OPENSSH_PATTERNS):
        # PASS: resolved to Win32-OpenSSH. Silent, no warning.
        return

    if any(fnmatch.fnmatchcase(resolved_lc, pat) for pat in _MSYS_PATTERNS):
        _warn(
            f"git will use '{resolved}' (resolved via {source}) to push over SSH — "
            "this is an MSYS ssh.exe.",
            "MSYS ssh.exe emulates AF_UNIX over loopback TCP and cannot reach the Win32 named pipe",
            r"(\\.\pipe\openssh-ssh-agent) the SSH agent listens on, so publickey auth WILL fail.",
            "Fix (pin git to Win32-OpenSSH):",
            _PIN_HINT,
            "See coordinator/docs/wiki/bash-on-windows-gotchas.md §15 for the reproduced control.",
        )
        return

    if not os.path.exists(resolved):
        _warn(
            f"resolved ssh binary '{resolved}' (via {source}) does not exist on disk — "
            "git push over SSH is likely to fail.",
            "Pin git to Win32-OpenSSH explicitly:",
            _PIN_HINT,
        )
        return

    _warn(
        f"resolved ssh binary '{resolved}' (via {source}) is neither confirmed Win32-OpenSSH nor a",
        "recognized MSYS shape — no core.sshCommand pin is set, so which ssh git actually invokes is",
        "ambiguous. If it turns out to be an MSYS ssh.exe, publickey auth to the Windows SSH agent",
        "will fail (see coordinator/docs/wiki/bash-on-windows-gotchas.md §15). Pin it explicitly:",
        _PIN_HINT,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point. argv is accepted for CLI-shape parity but unused — the
    bash oracle took no positional args either.

    Always returns 0 — this is a warn-only probe (see module docstring
    negative-spec). The body past the OS gate is wrapped in a broad
    try/except so this guarantee is structurally enforced at main()'s own
    boundary, not merely inherited from _run_git's narrower `except OSError`
    — `subprocess.run(..., text=True)` can raise `UnicodeDecodeError` (a
    ValueError subclass, not an OSError) decoding non-UTF-8 bytes under a
    non-UTF-8 Windows codepage, which would otherwise escape uncaught and be
    counted as a failed REQUIRED install step by install_health_run.py's
    generic `except Exception`. Review: code-reviewer (Finding 1) —
    main() must guarantee its own never-exits-nonzero contract rather than
    depend on the orchestrator's catch to absorb the divergence."""
    del argv
    if not _is_windows():
        return 0
    try:
        if not _has_ssh_remote():
            return 0
        resolved, source = _resolve_ssh_binary()
        _classify_and_warn(resolved, source)
    except Exception as exc:  # pragma: no cover - defensive, see docstring
        _warn(f"internal error (ignored, warn-only probe): {exc!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
