"""
coordinator_core.ops.setup_fnm_pin — repo-setup-time fnm pin resolution.

Purpose: if the target repo has a .node-version or .nvmrc pin file, install the
pinned Node version via fnm and emit activation guidance for bare shells. This
is PIN RESOLUTION ONLY — it does not install the fnm binary; that is owned by
coordinator:install (a separate chunk). If fnm is absent, fails loud with
actionable remediation.

Usage:
  setup-fnm-pin [<repo-root>]

  <repo-root>  — optional; directory containing the repo (default: cwd).

Env seams (injectable for tests — set these to mock fnm presence/absence):
  COORDINATOR_FNM_CMD      — override the fnm command (default: fnm).
                             Set to a mock script path in tests to intercept calls.
  COORDINATOR_FNM_ABSENT   — set to "1" to simulate fnm not found (for tests).

Exit codes:
  0 — success (pin resolved, or no pin file found — both are clean exits)
  1 — fnm absent when a pin file is present, OR repo root does not exist, OR
      pin file is empty (fail loud with remediation)

Contract:
  - No pin file  → pure no-op, exits 0, zero output.
  - Pin file + fnm present  → runs `fnm install <version>`, emits env guidance.
  - Pin file + fnm absent   → exits 1 with remediation message on stderr.
  - Idempotent: `fnm install` itself is idempotent; safe to call multiple times.

Port of: setup-fnm-pin.sh (coordinator-claude 6fb5fb37, 2026-07-22)
Spec backlink: docs/plans/2026-06-23-setup-time-substrate-completeness.md § C1d (AC6)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec:
    - `.node-version` takes precedence over `.nvmrc` when both are present
      (checked first, mirroring the bash oracle's `if/elif` ladder) — this is
      NOT "most recently modified" or any other heuristic, just file-name
      priority order.
    - Version string is read with ALL whitespace stripped (not just
      leading/trailing) — mirrors the bash oracle's `tr -d '[:space:]'`, so an
      embedded internal space/newline in a pin file collapses rather than
      erroring.
    - `COORDINATOR_FNM_ABSENT=1` short-circuits presence detection before the
      real `fnm` binary is even probed — this mirrors the bash oracle's
      early-return in `_fnm_is_present`, which is why an absolute/relative
      `COORDINATOR_FNM_CMD` path is still not exec'd in that mode.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional
from coordinator_core.win_portability import is_executable, no_console_creationflags, no_console_passthrough_kwargs

_PROG = "setup-fnm-pin"


def _resolve_pin_file(repo_root: Path) -> Optional[Path]:
    """.node-version checked first, then .nvmrc — first hit wins."""
    node_version = repo_root / ".node-version"
    if node_version.is_file():
        return node_version
    nvmrc = repo_root / ".nvmrc"
    if nvmrc.is_file():
        return nvmrc
    return None


def _fnm_is_present(fnm_cmd: str) -> bool:
    if os.environ.get("COORDINATOR_FNM_ABSENT", "") == "1":
        return False
    if fnm_cmd.startswith("/") or fnm_cmd.startswith("./"):
        return os.path.isfile(fnm_cmd) and is_executable(fnm_cmd)
    return shutil.which(fnm_cmd) is not None


def main(argv: List[str]) -> int:
    """CLI entry: resolve pin, probe fnm, install, emit activation guidance."""
    repo_root = Path(argv[0]) if argv else Path(os.getcwd())

    if not repo_root.is_dir():
        print(f"{_PROG}: repo root does not exist: {repo_root}", file=sys.stderr)
        return 1

    pin_file = _resolve_pin_file(repo_root)
    if pin_file is None:
        return 0

    pin_version = pin_file.read_text(encoding="utf-8", errors="replace")
    pin_version = "".join(pin_version.split())

    if not pin_version:
        print(f"{_PROG}: pin file is empty: {pin_file}", file=sys.stderr)
        return 1

    fnm_cmd = os.environ.get("COORDINATOR_FNM_CMD", "fnm")

    if not _fnm_is_present(fnm_cmd):
        print(
            f"{_PROG}: fnm not installed — run coordinator:install to install "
            "the Node toolchain manager, then re-run repo-setup",
            file=sys.stderr,
        )
        return 1

    print(f"{_PROG}: installing Node {pin_version} via fnm (pin: {pin_file})")

    subprocess.run([fnm_cmd, "install", pin_version], check=True, **no_console_passthrough_kwargs())

    print()
    print(f"Node {pin_version} installed. To activate in your current shell:")
    print('  eval "$(fnm env)"')
    print()
    print("Or prepend the fnm-managed Node to PATH directly (add to your shell profile):")
    print(
        f'  export PATH="$HOME/.local/share/fnm/node-versions/{pin_version}/installation/bin:$PATH"'
    )
    print()
    print("For bash/zsh login shells, add to ~/.bashrc or ~/.zshrc:")
    print('  eval "$(fnm env --use-on-cd)"')

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
