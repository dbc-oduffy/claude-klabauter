"""
coordinator_core.ops.new_project_scaffold — deterministic greenfield-scaffold
helper for the coordinator:new-project skill.

Purpose: create a new project directory, initialise a git repo pinned to branch `main`,
optionally render a template tree (delegating to render-template-tree.py, co-located in
this repo's coordinator/bin/ as of the coordinator/bin executable-surface migration --
itself a thin CLI trampoline over coordinator_core.ops.render_template_tree), then write
minimal seed files that downstream repo-setup can consume without interactive prompts.
Optionally runs a pnpm install/typecheck/test smoke pass for the next-app template.

This module DOES NOT invoke scaffold-canonical-structure.sh, coordinator-ensure-post-
commit-hook, coordinator-currency, or coordinator-configure-git. Coordinator onboarding
is the coordinator:new-project SKILL's responsibility (via repo-setup delegation), not
this helper's. This boundary is intentional — keep scaffold atomically composable.
Unchanged from the bash oracle.

Port of: new-project-scaffold.sh (DoE 290997c7, 2026-07-22)
Spec backlink: docs/plans/2026-06-22-new-project-bootstrap-skill.md § C3

DoE-root resolution: the templates/ tree (coordinator/skills/new-project/templates/) still
lives in the DoE clone, so a DoE-root resolution remains needed for that lookup.
render-template-tree.py itself, however, is now co-located in THIS repo's coordinator/bin
(migrated in the coordinator/bin executable-surface migration) and is resolved there first,
relative to this repo's own root — no DoE-root hop needed to find it. Only if the
co-located sibling is somehow absent does `_find_render_tree` fall back to the legacy
DoE-root lookup. That DoE-root resolution order (env override, then `machine-local`
registry) mirrors coordinator_core.ops.render_template_tree's `_resolve_doe_root` —
duplicated rather than imported, per that module's own "kept small and local" convention
(no shared private-helper import across op modules).

Exit-code contract (matches the bash oracle's own documented table):
    0  Scaffold created successfully.
    1  Pre-flight failure (occupied dir, bad args, missing DoE-side siblings), a
       pnpm smoke-step failure, or a makima-link resolution failure (machine-local
       registry lookup). A dedicated transport-failure code is NOT used here — this
       module has no registered op / engine-root-import seam of its own (it does not
       shell back into makima), and the ONE makima-adjacent lookup it performs
       (machine-local, to locate the DoE clone) shares exit 1 with pre-flight failure,
       consistent with the established precedent in
       coordinator_core.ops.render_template_tree (same collision, same rationale:
       there is no OTHER distinct business failure mode on that path to collide with —
       tracked as a Wave-B trampoline-consistency sweep item, not fixed piecemeal here).

Negative-spec (faithful oracle-bug repro — do NOT silently "fix"):
    - The bash oracle ran under `set -euo pipefail`. Steps NOT wrapped in an explicit
      `if`/`||` check (the git-init fallback pair, the render-template-tree.sh
      invocation, `cp -a`, `mkdir -p`, the seed-file heredocs) therefore propagate
      THEIR OWN child exit code as the script's exit code on failure, rather than a
      hardcoded 1. This module mirrors that: git-init-fallback / render-tree / copy /
      mkdir / seed-write failures propagate the underlying error's exit code (or 1
      where the underlying tool's conventional failure code is 1 and Python raised an
      exception rather than yielding a returncode, e.g. mkdir/cp equivalents). Only the
      pnpm smoke steps are hardcoded to exit 1 on failure — because the bash oracle
      wrapped THOSE (and only those) in an explicit `if ! (...); then ... exit 1; fi`.
    - Does NOT re-derive the DR-148 bash-version guard — meaningless in pure Python.
    - Does NOT reimplement render-template-tree.sh's tree-walk/token-substitution logic
      — delegates via subprocess, exactly as the bash oracle called its sibling script.
    - Parent-dir default resolution reproduces the oracle's literal string
      concatenation (`"${HOME}/Code_Projects"`) rather than `os.path.expanduser("~")` —
      if HOME is unset, the oracle produces the (odd but faithful) path
      "/Code_Projects", not a resolved home directory.

Timeout/stdin/CREATE_NO_WINDOW triad (per the safe-tier verification wave's addendum —
hard rule, not oracle fidelity): every subprocess.run call in this module carries
timeout=<secs>, stdin=subprocess.DEVNULL, and **_CREATIONFLAGS. The bash
oracle had none of these (unbounded `git`/pnpm/render-tree calls, inherited stdin, and
on Windows a console-window popup per child process) — a hung child pnpm/git process
would otherwise block this module indefinitely, and each child would flash a console
window on a clean Windows install. This is a deliberate hang-prevention/Windows-
portability fix, not a silently-dropped oracle behavior: the observable success/failure
contract on a non-hanging run is unchanged.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.launchable import resolve_launchable
from coordinator_core.machine_resolver import registry_get as _registry_get
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import is_executable, no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

# writes only into a brand-new project dir under COORDINATOR_PROJECTS_ROOT or
# $HOME/Code_Projects, always a fresh separate repo outside makima's own tree
GENERATES = []

_PROG = "new-project-scaffold.sh"  # literal program-name prefix, matches the DoE filename

_USAGE = (
    "Usage: new-project-scaffold.sh --name <name> [--parent <dir>] "
    "[--template next-app|empty] [--no-smoke]"
)

# Timeout budgets (seconds) — addendum rule 2 (unbounded-hang class).
_GIT_TIMEOUT = 30
_MACHINE_LOCAL_TIMEOUT = 15
_RENDER_TREE_TIMEOUT = 120
_PNPM_INSTALL_TIMEOUT = 900
_PNPM_TYPECHECK_TIMEOUT = 300
_PNPM_TEST_TIMEOUT = 300

# Windows-portability triad, addendum rule A4 — suppresses the console-window
# popup every child subprocess would otherwise flash on Windows. Matches the
# sibling ops in this wave (migrate_cross_repo_layout.py, orphan_branch_sweep.py).


def _resolve_machine_local() -> Optional[str]:
    """Locate the `machine-local` CLI on PATH. Returns None if absent."""
    return shutil.which("machine-local")


def _resolve_doe_root() -> Tuple[Optional[str], int]:
    """Resolve the DoE clone root. Returns (root_or_None, exit_code_on_failure).

    Tier 1: DOE_ROOT env var (permanent legacy alias — wins first when both
        DOE_ROOT and REPO_DOE_CLAUDE are set, per coordinator_registry.doe_root()).
    Tier 2: REPO_DOE_CLAUDE env var (operator override).
    Tier 3: `machine-local get repos.doe_claude` (registry).
    Mirrors coordinator_core.ops.render_template_tree's `_resolve_doe_root`
    and coordinator_registry.doe_root()'s precedence.

    Review: code-reviewer (2026-07-22, Finding 4) — DOE_ROOT was previously
    missing from this hand-rolled resolver, silently dropping the legacy-alias
    rung the shared coordinator_registry.doe_root() honors.

    Tier 3 itself tries `registry_get` first, zero-spawn, and falls back to
    the `machine-local get` CLI only on a miss -- `registry_get` alone
    doesn't reach the CLI's autodiscovery/`path-exceptions.toml` rungs, and
    this repo's own `.coordinator-dev-repo` marker proves autodiscovery is
    live for `repos.doe_claude` on a real machine, not a hypothetical
    (2026-08-16 review finding).
    """
    doe_root_override = os.environ.get("DOE_ROOT", "")
    if doe_root_override:
        return doe_root_override, 0

    env_override = os.environ.get("REPO_DOE_CLAUDE", "")
    if env_override:
        return env_override, 0

    # Zero-spawn: `registry_get` reads the same registry.local.toml over
    # registry.toml chain `machine-local get` would, in-process (see
    # `coordinator_core.machine_resolver.registry_get`).
    value = _registry_get("repos.doe_claude") or ""
    if not value:
        ml_bin = _resolve_machine_local()
        if ml_bin is not None:
            try:
                proc = subprocess.run(
                    [ml_bin, "get", "repos.doe_claude"],
                    capture_output=True,
                    text=True,
                    timeout=_MACHINE_LOCAL_TIMEOUT,
                    stdin=subprocess.DEVNULL,
                    **_CREATIONFLAGS,
                )
                if proc.returncode == 0:
                    value = proc.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
    if not value:
        print(f"{_PROG}: could not resolve repos.doe_claude via the registry", file=sys.stderr)
        return None, 1
    return value, 0


def _co_located_render_tree() -> Optional[str]:
    """Locate render-template-tree.py co-located in THIS repo's coordinator/bin.

    render-template-tree.py migrated into this repo in the coordinator/bin
    executable-surface migration (DoE-claude commit b644d5a9) -- it is now
    makima's OWN sibling executable, not DoE-resident content, so it is
    resolved relative to this repo unconditionally, ahead of any DoE-root
    lookup (env override or registry alike). The DoE-root fallback below is
    kept as a compatibility safety net for a checkout where this co-located
    sibling is somehow absent.
    """
    # is_executable() answers "does this .py file's own mode bit make it
    # directly launchable" -- true on POSIX (resolve_launchable execs it via
    # its shebang) but always False on Windows for a .py path, since Windows
    # never launches a .py file directly (resolve_launchable resolves a
    # python interpreter for it there regardless of any exec bit). So the
    # Windows leg only needs existence.
    this_repo_root = Path(__file__).resolve().parents[2]
    candidate = this_repo_root / "coordinator" / "bin" / "render-template-tree.py"
    if candidate.is_file() and (os.name == "nt" or is_executable(candidate)):
        return str(candidate)
    return None


def _find_render_tree(doe_root: str) -> Optional[str]:
    """Locate render-template-tree.py: co-located first, then the DoE root."""
    co_located = _co_located_render_tree()
    if co_located is not None:
        return co_located
    candidate = os.path.join(doe_root.rstrip("/"), "coordinator", "bin", "render-template-tree.py")
    if os.path.isfile(candidate) and (os.name == "nt" or is_executable(candidate)):
        return candidate
    return None


def _register_repo(project_name: str, target: str) -> int:
    """Self-register the freshly scaffolded repo into machine-local's
    `repos.<slug>` registry.

    slug = project_name.lower().replace('-', '_'). Calls `machine-local set
    repos.<slug> <target-abs>` then verifies the round-trip via `machine-local
    get repos.<slug>`. This is what lets the DoE new-project skill drop its
    inline Phase 4.5 machine-local fence entirely -- the scaffold now
    self-registers instead of the caller doing it as a separate ceremony step.
    """
    slug = project_name.lower().replace("-", "_")
    target_abs = os.path.abspath(target)

    ml_bin = _resolve_machine_local()
    if ml_bin is None:
        print(f"{_PROG}: machine-local not found -- cannot register repos.{slug}", file=sys.stderr)
        return 1

    try:
        proc = subprocess.run(
            [ml_bin, "set", f"repos.{slug}", target_abs],
            capture_output=True,
            text=True,
            timeout=_MACHINE_LOCAL_TIMEOUT,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{_PROG}: machine-local set repos.{slug} failed: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(
            f"{_PROG}: machine-local set repos.{slug} exited {proc.returncode}: {proc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    # Round-trip verification read: in-process (zero-spawn -- see
    # `coordinator_core.machine_resolver.registry_get`) rather than a
    # `machine-local get` shell-out. Reads the same registry.local.toml the
    # `set` call above just wrote, synchronously. `machine-local get` itself
    # POSIX-normalizes its repos.* branch's return value (see
    # `coordinator_core.ops.gen_doe_root_pointer` module docstring); the raw
    # `registry_get` read has no such key-specific behaviour, so the same
    # normalization is applied here explicitly to preserve the CLI's
    # documented "repos.* round-trips as POSIX regardless of platform"
    # contract byte-for-byte.
    stored = (_registry_get(f"repos.{slug}") or "").replace(os.sep, "/")
    # The registry round-trips repos.* values through machine-local as
    # POSIX-separated strings regardless of platform (registry contract,
    # not a scaffold decision) -- os.path.abspath() on Windows returns
    # native backslashes, so compare both sides POSIX-normalized rather
    # than raw, or every registration on Windows fails verification.
    target_posix = target_abs.replace(os.sep, "/")
    if stored != target_posix:
        print(
            f"{_PROG}: repos.{slug} registration verify mismatch: expected {target_posix!r}, got {stored!r}",
            file=sys.stderr,
        )
        return 1

    print(f"Registered repos.{slug} -> {target_abs}")
    return 0


def _parse_args(argv: List[str]) -> Tuple[Optional[dict], int]:
    """Parse CLI args. Returns (parsed_dict_or_None, exit_code). exit_code is 0 iff parsed is not None."""
    project_name = ""
    parent_dir = ""
    template = "next-app"
    no_smoke = False

    i = 0
    n = len(argv)
    while i < n:
        arg = argv[i]
        if arg == "--name":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --name requires a value", file=sys.stderr)
                return None, 1
            project_name = value
            i += 2
        elif arg == "--parent":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --parent requires a value", file=sys.stderr)
                return None, 1
            parent_dir = value
            i += 2
        elif arg == "--template":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --template requires a value", file=sys.stderr)
                return None, 1
            template = value
            i += 2
        elif arg == "--no-smoke":
            no_smoke = True
            i += 1
        else:
            print(f"ERROR: unknown argument: {arg}", file=sys.stderr)
            print(_USAGE, file=sys.stderr)
            return None, 1

    if not project_name:
        print("ERROR: --name is required", file=sys.stderr)
        print(_USAGE, file=sys.stderr)
        return None, 1

    if template not in ("next-app", "empty"):
        print(f"ERROR: --template must be 'next-app' or 'empty'; got: {template}", file=sys.stderr)
        return None, 1

    return {
        "project_name": project_name,
        "parent_dir": parent_dir,
        "template": template,
        "no_smoke": no_smoke,
    }, 0


def _resolve_parent_dir(explicit_parent: str) -> str:
    """Resolution order, first hit wins: the explicit flag, then the
    COORDINATOR_PROJECTS_ROOT env var, then a literal concat of the HOME env var
    with "/Code_Projects"."""
    if explicit_parent:
        return explicit_parent
    env_root = os.environ.get("COORDINATOR_PROJECTS_ROOT", "")
    if env_root:
        return env_root
    home = os.environ.get("HOME", "")
    return f"{home}/Code_Projects"


def _git_init_main(target: str) -> int:
    """Try `git init -b main`; fall back to `git init` + `symbolic-ref`.

    Faithful to the bash oracle's set -e propagation on the fallback leg — see the
    module-level negative-spec.
    """
    try:
        proc = subprocess.run(
            ["git", "init", "-b", "main", target, "--quiet"],
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        primary_ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        primary_ok = False

    if primary_ok:
        return 0

    try:
        proc = subprocess.run(
            ["git", "init", target, "--quiet"],
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{_PROG}: git init failed: {exc}", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        return proc.returncode

    try:
        proc = subprocess.run(
            ["git", "-C", target, "symbolic-ref", "HEAD", "refs/heads/main"],
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{_PROG}: git symbolic-ref failed: {exc}", file=sys.stderr)
        return 1
    return proc.returncode


def _run_smoke_step(cmd: List[str], cwd: str, timeout: int, label: str) -> int:
    """Run one pnpm smoke step. Hardcoded exit 1 on any failure — matches the bash
    oracle's explicit `if ! (...); then echo ERROR...; exit 1; fi` wrapping (the ONE
    place the oracle overrides the underlying child's exit code)."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, timeout=timeout, stdin=subprocess.DEVNULL, **_CREATIONFLAGS
        )
    except (OSError, subprocess.TimeoutExpired):
        print(f"ERROR: {label} failed -- scaffold is incomplete", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(f"ERROR: {label} failed -- scaffold is incomplete", file=sys.stderr)
        return 1
    return 0


def main(argv: List[str]) -> int:
    parsed, rc = _parse_args(argv)
    if parsed is None:
        return rc

    project_name = parsed["project_name"]
    template = parsed["template"]
    no_smoke = parsed["no_smoke"]

    parent_dir = _resolve_parent_dir(parsed["parent_dir"])
    target = os.path.join(parent_dir, project_name)

    # -----------------------------------------------------------------
    # Pre-flight: fail loud if target is non-empty
    # -----------------------------------------------------------------
    if os.path.isdir(target):
        try:
            non_empty = bool(os.listdir(target))
        except OSError:
            non_empty = False
        if non_empty:
            print(f"ERROR: target directory already exists and is non-empty: {target}", file=sys.stderr)
            print("Remove or rename the existing directory and retry.", file=sys.stderr)
            return 1

    # -----------------------------------------------------------------
    # Create parent and target directories
    # -----------------------------------------------------------------
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print(f"{_PROG}: mkdir -p failed for {target}: {exc}", file=sys.stderr)
        return 1

    # -----------------------------------------------------------------
    # Git init — pin to branch main
    # -----------------------------------------------------------------
    git_rc = _git_init_main(target)
    if git_rc != 0:
        return git_rc

    # -----------------------------------------------------------------
    # Template render (next-app only; empty template has no source tree)
    # -----------------------------------------------------------------
    if template == "next-app":
        doe_root, doe_rc = _resolve_doe_root()
        if doe_root is None:
            return doe_rc

        template_src = os.path.join(doe_root.rstrip("/"), "coordinator", "skills", "new-project", "templates", "next-app")
        if not os.path.isdir(template_src):
            print(f"ERROR: next-app template not found at: {template_src}", file=sys.stderr)
            return 1

        render_tree = _find_render_tree(doe_root)
        if render_tree is None:
            print("ERROR: render-template-tree.sh not found or not executable", file=sys.stderr)
            return 1

        staging = tempfile.mkdtemp()
        try:
            try:
                proc = subprocess.run(
                    [*resolve_launchable(render_tree), template_src, staging, f"PROJECT_NAME={project_name}"],
                    timeout=_RENDER_TREE_TIMEOUT,
                    stdin=subprocess.DEVNULL,
                    **_CREATIONFLAGS,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                print(f"{_PROG}: render-template-tree.sh invocation failed: {exc}", file=sys.stderr)
                return 1
            if proc.returncode != 0:
                return proc.returncode

            try:
                shutil.copytree(staging, target, dirs_exist_ok=True, symlinks=True, copy_function=shutil.copy2)
            except (OSError, shutil.Error) as exc:
                print(f"{_PROG}: copying rendered tree into {target} failed: {exc}", file=sys.stderr)
                return 1
            # DR-276: declare every file the copytree actually landed under
            # `target`, walked post-copy (not the staging tree) so the
            # declared paths are the real destination this write claims —
            # a scaffolder writing many files declares each one, not the
            # target directory once.
            staging_path = Path(staging)
            target_path = Path(target)
            for rendered_file in staging_path.rglob("*"):
                if rendered_file.is_file():
                    declare_write(target_path / rendered_file.relative_to(staging_path))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    # -----------------------------------------------------------------
    # Seed files (always written, for all templates)
    # -----------------------------------------------------------------
    coordinator_project_type = "web-dev" if template == "next-app" else "general"
    coordinator_local_path = os.path.join(target, "coordinator.local.md")
    readme_path = os.path.join(target, "README.md")
    try:
        with open(coordinator_local_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"---\nproject_type: {coordinator_project_type}\n---\n")
        with open(readme_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# {project_name}\n")
    except OSError as exc:
        print(f"{_PROG}: writing seed files failed: {exc}", file=sys.stderr)
        return 1
    # DR-276: declared after both writes land — the contract is a report of
    # what was ACTUALLY written, not of an intended surface.
    declare_write(coordinator_local_path)
    declare_write(readme_path)

    # -----------------------------------------------------------------
    # Smoke (unless --no-smoke) — for next-app only
    # -----------------------------------------------------------------
    if not no_smoke and template == "next-app":
        print(f"Running smoke checks in {target}...")
        pnpm = shutil.which("pnpm") or "pnpm"

        rc = _run_smoke_step([pnpm, "install"], target, _PNPM_INSTALL_TIMEOUT, "pnpm install")
        if rc != 0:
            return rc
        rc = _run_smoke_step([pnpm, "typecheck"], target, _PNPM_TYPECHECK_TIMEOUT, "pnpm typecheck")
        if rc != 0:
            return rc
        rc = _run_smoke_step([pnpm, "test"], target, _PNPM_TEST_TIMEOUT, "pnpm test")
        if rc != 0:
            return rc

        print("Smoke checks passed.")

    # -----------------------------------------------------------------
    # Self-register repos.<slug> in machine-local (kills the DoE-side
    # inline Phase 4.5 fence entirely).
    # -----------------------------------------------------------------
    reg_rc = _register_repo(project_name, target)
    if reg_rc != 0:
        return reg_rc

    print(f"Scaffold complete: {target}")
    return 0
