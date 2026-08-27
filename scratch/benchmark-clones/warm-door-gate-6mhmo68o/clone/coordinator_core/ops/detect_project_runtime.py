"""
coordinator_core.ops.detect_project_runtime — advisory runtime/operational
marker scan for repo-setup Phase 1 context.

Purpose: surface what runtime/operational invariants the filesystem of
`cwd` reveals (lockfile -> package manager, compose file -> Docker, CI dir
-> CI vendor, Infisical refs -> no .env). Output is stdout-only ADVISORY
context for the PM answering repo-setup Phase 2 question 2 — never
authoritative.

Output: a one-line summary header + indented bullets per detected stack +
fixed Notes footer. Exit 0 even when no markers fire.

Port of: detect-project-runtime.sh (DoE b5a4192c, 2026-07-20, 125 lines)
Spec backlink: archive/specs/2026-05-06-detect-project-runtime.md (DoE-claude)
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Negative-spec (preserved verbatim from the bash oracle — do NOT "fix"
while porting):
    - Does NOT write files, set env vars, mutate the working tree, call
      external services, or emit structured artifacts (no YAML, no JSON,
      no CONTEXT.md, no sidecar). NO skill, agent, or hook reads this
      output programmatically. Adding a consumer requires a separate plan.
    - `.env` tracked-file detection runs `git ls-files` only when `cwd` is
      inside a git worktree; outside a git repo `env_tracked` defaults to
      1 ("unknown / treat as present") exactly like the bash oracle's
      `env_tracked=1` initializer — this means a bare `infisical.json`
      with NO `.env.example` present, scanned OUTSIDE a git repo, detects
      NOTHING (neither Secrets bullet fires), which looks surprising but
      is byte-for-byte the oracle's existing behavior, not a bug
      introduced by this port.
    - Multiple Node lockfiles render as a single comma-joined bullet
      (`Node (multiple lockfiles: bun,pnpm,yarn,npm)`), in the fixed
      precedence order bun > pnpm > yarn > npm — mirrors the bash oracle's
      append order exactly, not sorted.
    - Pre-existing oracle quirk, faithfully reproduced (NOT fixed on port):
      the bash regex `^\\.env$|^\\.env\\.` used to detect "a tracked .env
      file" also matches the literal filename `.env.example` (it starts
      with `.env.`) — so git-committing ONLY `.env.example` (no actual
      `.env` secret file) already flips `env_tracked` to True and
      suppresses the Infisical bullet, even though no real secret file is
      tracked. See test_detect_project_runtime.py::
      test_secrets_env_example_git_tracked_counts_as_a_tracked_env_file.
"""

from __future__ import annotations

import glob
import os
import subprocess
from coordinator_core.win_portability import leaf_spawn_creationflags
import sys
from typing import List


def _has_glob(cwd: str, pattern: str) -> bool:
    return len(glob.glob(os.path.join(cwd, pattern))) > 0


def _detect_node(cwd: str, lines: List[str]) -> None:
    if not os.path.isfile(os.path.join(cwd, "package.json")):
        return
    managers: List[str] = []
    if os.path.isfile(os.path.join(cwd, "bun.lockb")):
        managers.append("bun")
    if os.path.isfile(os.path.join(cwd, "pnpm-lock.yaml")):
        managers.append("pnpm")
    if os.path.isfile(os.path.join(cwd, "yarn.lock")):
        managers.append("yarn")
    if os.path.isfile(os.path.join(cwd, "package-lock.json")):
        managers.append("npm")
    if len(managers) == 0:
        lines.append("Node (no lockfile detected)")
    elif len(managers) == 1:
        lines.append(f"Node ({managers[0]})")
    else:
        lines.append(f"Node (multiple lockfiles: {','.join(managers)})")


def _detect_python(cwd: str, lines: List[str]) -> None:
    if os.path.isfile(os.path.join(cwd, "pyproject.toml")):
        lines.append("Python (pyproject.toml — poetry/pdm/uv/pip)")
    elif _has_glob(cwd, "requirements*.txt"):
        lines.append("Python (requirements.txt — pip)")


def _detect_simple_markers(cwd: str, lines: List[str]) -> None:
    if os.path.isfile(os.path.join(cwd, "Cargo.toml")):
        lines.append("Rust (cargo)")
    if os.path.isfile(os.path.join(cwd, "go.mod")):
        lines.append("Go (modules)")
    if os.path.isfile(os.path.join(cwd, "Gemfile")):
        lines.append("Ruby (bundler)")
    if os.path.isfile(os.path.join(cwd, "Dockerfile")):
        lines.append("Docker (image build)")
    if any(
        os.path.isfile(os.path.join(cwd, name))
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
    ):
        lines.append("Docker Compose (multi-service)")
    if os.path.isdir(os.path.join(cwd, ".github", "workflows")) and _has_glob(
        cwd, os.path.join(".github", "workflows", "*.y*ml")
    ):
        lines.append("GitHub Actions CI")
    if os.path.isfile(os.path.join(cwd, ".gitlab-ci.yml")):
        lines.append("GitLab CI")
    if os.path.isfile(os.path.join(cwd, "Makefile")):
        lines.append("Make (build entry)")


def _is_inside_git_worktree(cwd: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            timeout=5,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _is_inside_git_worktree: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return proc.returncode == 0


def _tracked_env_files_present(cwd: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"skip: _tracked_env_files_present: proc = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    for path in proc.stdout.splitlines():
        if path == ".env" or path.startswith(".env."):
            return True
    return False


def _detect_secrets(cwd: str, lines: List[str]) -> None:
    infisical_present = os.path.isfile(os.path.join(cwd, "infisical.json")) or os.path.isfile(
        os.path.join(cwd, ".infisical.json")
    )
    env_example_present = os.path.isfile(os.path.join(cwd, ".env.example"))

    # Default to "unknown / treat as present" for non-git contexts — matches
    # the bash oracle's `env_tracked=1` initializer exactly (see negative-spec).
    env_tracked = True
    if _is_inside_git_worktree(cwd):
        env_tracked = _tracked_env_files_present(cwd)

    if infisical_present and not env_tracked:
        lines.append("Secrets: Infisical (no .env files tracked)")
    if env_example_present and (not infisical_present or env_tracked):
        lines.append("Secrets: .env (template only — .env.example present)")
    if infisical_present and env_example_present and not env_tracked:
        # Both signals present — list both per plan ("list both if both conditions met").
        lines.append("Secrets: .env.example also present (legacy template)")


def scan(cwd: str) -> List[str]:
    """Run all marker detectors against `cwd` and return the ordered hit list."""
    lines: List[str] = []
    _detect_node(cwd, lines)
    _detect_python(cwd, lines)
    _detect_simple_markers(cwd, lines)
    _detect_secrets(cwd, lines)
    return lines


def render(cwd: str, lines: List[str]) -> str:
    """Render the detected marker lines into the oracle's fixed stdout shape."""
    if not lines:
        return f"Detected: no known stack markers (script saw {cwd})."

    cwd_base = os.path.basename(cwd.rstrip(os.sep)) or cwd
    out = [f"Detected runtime profile for {cwd_base}:"]
    for line in lines:
        out.append(f"  - {line}")
    out.append("")
    out.append("Notes:")
    out.append("  - This is advisory context for repo-setup. Confirm with the PM before relying on it.")
    return "\n".join(out)


def main(argv: List[str]) -> int:
    """CLI entry: scan the current working directory, print, always exit 0."""
    cwd = os.getcwd()
    lines = scan(cwd)
    print(render(cwd, lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
