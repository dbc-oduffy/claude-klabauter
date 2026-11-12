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

Content-root resolution: the templates/ tree (coordinator/skills/new-project/templates/) still
lives in the content clone, so a `read_content_root()` resolution remains needed for that lookup.
render-template-tree.py itself, however, is now co-located in THIS repo's coordinator/bin
(migrated in the coordinator/bin executable-surface migration) and is resolved there first,
relative to this repo's own root — no content-root hop needed to find it. Only if the
co-located sibling is somehow absent does `_find_render_tree` fall back to the content-root
lookup.

Exit-code contract (matches the bash oracle's own documented table):
    0  Scaffold created successfully.
    1  Pre-flight failure (occupied dir, bad args, missing DoE-side siblings), a
       pnpm smoke-step failure, or a claude-klabauter-link resolution failure (machine-local
       registry lookup). A dedicated transport-failure code is NOT used here — this
       module has no registered op / engine-root-import seam of its own (it does not
       shell back into claude-klabauter), and the ONE claude-klabauter-adjacent lookup it performs
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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core._settings_home import resolve_machine_local_cli
from coordinator_core._content_root_primitive import content_root_for
from coordinator_core.temp_layout import managed_tempdir
from coordinator_core.content_root import read_content_root
from coordinator_core.launchable import resolve_launchable
from coordinator_core.machine_resolver import registry_get as _registry_get
from coordinator_core.ops.repo_setup_contract import PROJECT_TYPES
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

# writes only into a brand-new project dir under COORDINATOR_PROJECTS_ROOT or
# $HOME/Code_Projects, always a fresh separate repo outside claude-klabauter's own tree
GENERATES = []

_PROG = "new-project-scaffold.sh"

_USAGE = (
    "Usage: new-project-scaffold.sh --name <name> [--parent <dir>] "
    "[--template next-app|empty|python-package] [--project-type game-dev|web-dev|data-science|general] "
    "[--subtypes a,b] [--like <sibling-path-or-registry-slug>] [--no-smoke]"
)

_SUBTYPE_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")

# --like carries only what the sibling DECLARES: project_type/project_subtypes from its
# coordinator.local.md frontmatter, plus the `## ` sections of its CLAUDE.md whose heading
# contains "convention". Invariant: reads at most those two files; never the rest of the
# CLAUDE.md (project-specific prose) and never pyproject.toml.
_CONVENTION_HEADING_RE = re.compile(r"^##\s+.*convention", re.IGNORECASE)

_GIT_TIMEOUT = 30
_MACHINE_LOCAL_TIMEOUT = 15
_RENDER_TREE_TIMEOUT = 120
_PNPM_INSTALL_TIMEOUT = 900
_PNPM_TYPECHECK_TIMEOUT = 300
_PNPM_TEST_TIMEOUT = 300


def _resolve_machine_local() -> Optional[str]:
    return resolve_machine_local_cli()


def _co_located_render_tree() -> Optional[str]:
    this_repo_root = Path(__file__).resolve().parents[2]
    candidate = this_repo_root / "coordinator" / "bin" / "render-template-tree.py"
    if candidate.is_file():
        return str(candidate)
    return None


def _find_render_tree(root: str) -> Optional[str]:
    co_located = _co_located_render_tree()
    if co_located is not None:
        return co_located
    content_root = content_root_for(root)
    if content_root is None:
        return None
    candidate = str(content_root / "bin" / "render-template-tree.py")
    if os.path.isfile(candidate):
        return candidate
    return None


_SCRATCH_DIR_NAMES = frozenset({"scratch", "scratchpad"})


def _is_scratch_target(target_abs: str) -> bool:
    """True when ``target_abs`` is under the system temp dir or has a ``scratch``/``scratchpad`` path component.

    Such a target is throwaway: a live ``repos.*`` key for it feeds the registry-derived leak pattern.
    The read side is ``check_registry_codename_leak._is_throwaway_path``; keep the two predicates in step.
    """
    parts = {p.lower() for p in Path(target_abs).parts}
    if parts & _SCRATCH_DIR_NAMES:
        return True
    try:
        tmp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
        resolved = os.path.normcase(os.path.realpath(target_abs))
    except OSError:
        return False
    return (resolved + os.sep).startswith(tmp.rstrip(os.sep) + os.sep)


def _register_repo(project_name: str, target: str) -> int:
    slug = project_name.lower().replace("-", "_")
    target_abs = os.path.abspath(target)

    if _is_scratch_target(target_abs):
        print(
            f"{_PROG}: not registering repos.{slug}: {target_abs} is under a temp or scratch location",
            file=sys.stderr,
        )
        return 0

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

    stored = (_registry_get(f"repos.{slug}") or "").replace(os.sep, "/")
    target_posix = target_abs.replace(os.sep, "/")
    if stored != target_posix:
        print(
            f"{_PROG}: repos.{slug} registration verify mismatch: expected {target_posix!r}, got {stored!r}",
            file=sys.stderr,
        )
        return 1

    print(f"Registered repos.{slug} -> {target_abs}")
    return 0


def _resolve_sibling(like: str) -> Optional[Path]:
    """An existing directory path wins; otherwise `like` is a registry slug (`repos.<slug>`)."""
    candidate = Path(like).expanduser()
    if candidate.is_dir():
        return candidate
    slug = like.lower().replace("-", "_")
    stored = _registry_get(f"repos.{slug}") or ""
    if stored and Path(stored).is_dir():
        return Path(stored)
    return None


def _read_sibling(like: str) -> Tuple[Optional[dict], int]:
    """Read-only: returns {project_type, subtypes, conventions} or (None, 1) with a one-line stderr."""
    sibling = _resolve_sibling(like)
    if sibling is None:
        print(f"ERROR: --like sibling does not resolve to a directory or registry slug: {like}", file=sys.stderr)
        return None, 1
    local_md = sibling / "coordinator.local.md"
    try:
        local_text = local_md.read_text(encoding="utf-8")
    except OSError:
        print(f"ERROR: --like sibling has no readable coordinator.local.md: {sibling}", file=sys.stderr)
        return None, 1

    project_type = ""
    subtypes: List[str] = []
    lines = local_text.splitlines()
    if lines and lines[0].strip() == "---":
        in_block_subtypes = False
        for line in lines[1:]:
            if line.strip() == "---":
                break
            m = re.match(r"project_type:\s*(\S+)", line)
            if m:
                project_type = m.group(1).strip("'\"")
                in_block_subtypes = False
                continue
            m = re.match(r"project_subtypes:\s*(.*)$", line)
            if m:
                rest = m.group(1).strip()
                in_block_subtypes = not rest
                if rest.startswith("["):
                    subtypes = [s.strip().strip("'\"") for s in rest.strip("[]").split(",") if s.strip()]
                continue
            if in_block_subtypes:
                item = re.match(r"\s*-\s*(\S+)", line)
                if item:
                    subtypes.append(item.group(1).strip("'\""))
                    continue
                in_block_subtypes = False
    if project_type not in PROJECT_TYPES:
        print(f"ERROR: --like sibling declares no valid project_type in {local_md}", file=sys.stderr)
        return None, 1
    subtypes = [s for s in subtypes if _SUBTYPE_RE.fullmatch(s)]

    conventions: List[str] = []
    try:
        claude_lines = (sibling / "CLAUDE.md").read_text(encoding="utf-8").splitlines()
    except OSError:
        claude_lines = []
    capturing = False
    in_fence = False
    for line in claude_lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            if line.startswith("## "):
                capturing = bool(_CONVENTION_HEADING_RE.match(line))
            elif line.startswith("# "):
                capturing = False
        if capturing:
            conventions.append(line)
    return {
        "project_type": project_type,
        "subtypes": subtypes,
        "conventions": "\n".join(conventions).strip("\n"),
    }, 0


def _parse_args(argv: List[str]) -> Tuple[Optional[dict], int]:
    project_name = ""
    like = ""
    parent_dir = ""
    template = "next-app"
    no_smoke = False
    project_type = ""
    subtypes: List[str] = []

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
        elif arg == "--project-type":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --project-type requires a value", file=sys.stderr)
                return None, 1
            project_type = value
            i += 2
        elif arg == "--subtypes":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --subtypes requires a value", file=sys.stderr)
                return None, 1
            subtypes = [s.strip() for s in value.split(",")]
            i += 2
        elif arg == "--like":
            value = argv[i + 1] if i + 1 < n else ""
            if not value:
                print("ERROR: --like requires a value", file=sys.stderr)
                return None, 1
            like = value
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

    if template not in ("next-app", "empty", "python-package"):
        print(
            f"ERROR: --template must be 'next-app', 'empty' or 'python-package'; got: {template}",
            file=sys.stderr,
        )
        return None, 1

    if project_type and project_type not in PROJECT_TYPES:
        print(
            f"ERROR: --project-type must be one of {', '.join(PROJECT_TYPES)}; got: {project_type}",
            file=sys.stderr,
        )
        return None, 1

    for sub in subtypes:
        if not _SUBTYPE_RE.fullmatch(sub):
            print(
                f"ERROR: --subtypes takes comma-separated names of [a-z0-9][a-z0-9_-]*; got: {sub!r}",
                file=sys.stderr,
            )
            return None, 1

    return {
        "project_name": project_name,
        "parent_dir": parent_dir,
        "template": template,
        "no_smoke": no_smoke,
        "project_type": project_type,
        "subtypes": subtypes,
        "like": like,
    }, 0


def _python_package_files(project_name: str) -> dict:
    """Seed tree for the python-package template: repo-relative posix path -> content.

    The import name is the project name lowercased with every non-alphanumeric run folded
    to `_` (a leading digit gains a `_` prefix) so it is always an importable identifier.
    """
    pkg = re.sub(r"[^a-z0-9]+", "_", project_name.lower()).strip("_") or "pkg"
    if pkg[0].isdigit():
        pkg = f"_{pkg}"
    pyproject = (
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        f"name = {json.dumps(project_name)}\n"
        'version = "0.1.0"\n'
        'requires-python = ">=3.10"\n'
        "dependencies = []\n"
        "\n"
        "[tool.setuptools.packages.find]\n"
        f'include = ["{pkg}*"]\n'
        "\n"
        "[tool.pytest.ini_options]\n"
        'testpaths = ["tests"]\n'
        'markers = ["slow: excluded from the fast tier (scripts/run_fast_tests.py)"]\n'
    )
    runner = (
        '"""Fast-tier test runner: pytest over tests/ minus tests marked `slow`."""\n'
        "import subprocess\n"
        "import sys\n"
        "\n"
        "\n"
        "def main() -> int:\n"
        '    cmd = [sys.executable, "-m", "pytest", "-m", "not slow", "-q", *sys.argv[1:]]\n'
        "    return subprocess.run(cmd).returncode\n"
        "\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    sys.exit(main())\n"
    )
    smoke_test = (
        f"import {pkg}\n"
        "\n"
        "\n"
        "def test_package_imports():\n"
        f'    assert {pkg}.__name__ == "{pkg}"\n'
    )
    return {
        "pyproject.toml": pyproject,
        f"{pkg}/__init__.py": f"# {pkg}\n",
        "tests/test_smoke.py": smoke_test,
        "scripts/run_fast_tests.py": runner,
    }


def _is_consumer() -> bool:
    try:
        from coordinator_core.machine_profile import machine_profile
    except ImportError:  # pragma: no cover
        return False
    return machine_profile() == "consumer"


def _resolve_parent_dir(explicit_parent: str) -> str:
    """Resolution order, first hit wins: the explicit flag, then the
    COORDINATOR_PROJECTS_ROOT env var, then a literal concat of the HOME env var
    with "/Code_Projects"."""
    if explicit_parent:
        return explicit_parent
    env_root = os.environ.get("COORDINATOR_PROJECTS_ROOT", "")
    if env_root:
        return env_root
    if _is_consumer():
        return os.path.dirname(os.getcwd())
    home = os.environ.get("HOME", "")
    return f"{home}/Code_Projects"


def _git_init_main(target: str) -> int:
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

    sibling_info: Optional[dict] = None
    if parsed["like"]:
        sibling_info, like_rc = _read_sibling(parsed["like"])
        if sibling_info is None:
            return like_rc

    parent_dir = _resolve_parent_dir(parsed["parent_dir"])
    target = os.path.join(parent_dir, project_name)

    if os.path.isdir(target):
        try:
            non_empty = bool(os.listdir(target))
        except OSError:
            non_empty = False
        if non_empty:
            print(f"ERROR: target directory already exists and is non-empty: {target}", file=sys.stderr)
            print("Remove or rename the existing directory and retry.", file=sys.stderr)
            return 1

    try:
        os.makedirs(target, exist_ok=True)
    except OSError as exc:
        print(f"{_PROG}: mkdir -p failed for {target}: {exc}", file=sys.stderr)
        return 1

    git_rc = _git_init_main(target)
    if git_rc != 0:
        return git_rc

    written: set = set()

    if template == "next-app":
        root = read_content_root()
        if not root:
            print(f"{_PROG}: could not resolve the content root", file=sys.stderr)
            return 1

        content_root = content_root_for(root)
        if content_root is None:
            print(
                f"ERROR: no coordinator content root under the resolved content root: {root}",
                file=sys.stderr,
            )
            return 1

        template_src = str(content_root / "skills" / "new-project" / "templates" / "next-app")
        if not os.path.isdir(template_src):
            print(f"ERROR: next-app template not found at: {template_src}", file=sys.stderr)
            return 1

        render_tree = _find_render_tree(root)
        if render_tree is None:
            print("ERROR: render-template-tree.sh not found or not executable", file=sys.stderr)
            return 1

        staging = tempfile.mkdtemp(dir=managed_tempdir())
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
            staging_path = Path(staging)
            target_path = Path(target)
            for rendered_file in staging_path.rglob("*"):
                if rendered_file.is_file():
                    declare_write(target_path / rendered_file.relative_to(staging_path))
                    written.add(rendered_file.relative_to(staging_path).as_posix())
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    like_type = sibling_info["project_type"] if sibling_info else ""
    like_subtypes = sibling_info["subtypes"] if sibling_info else []
    coordinator_project_type = parsed["project_type"] or like_type or (
        "web-dev" if template == "next-app" else "general"
    )
    seed_subtypes = parsed["subtypes"] or like_subtypes
    seed_frontmatter = f"project_type: {coordinator_project_type}\n"
    if seed_subtypes:
        seed_frontmatter += f"project_subtypes: [{', '.join(seed_subtypes)}]\n"
    coordinator_local_path = os.path.join(target, "coordinator.local.md")
    readme_path = os.path.join(target, "README.md")
    try:
        with open(coordinator_local_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"---\n{seed_frontmatter}---\n")
        with open(readme_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(f"# {project_name}\n")
    except OSError as exc:
        print(f"{_PROG}: writing seed files failed: {exc}", file=sys.stderr)
        return 1
    # DR-276: declared after both writes land — the contract is a report of
    # what was ACTUALLY written, not of an intended surface.
    declare_write(coordinator_local_path)
    declare_write(readme_path)
    written.update(("coordinator.local.md", "README.md"))

    if template == "python-package":
        for rel, content in _python_package_files(project_name).items():
            path = Path(target) / rel
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(content)
            except OSError as exc:
                print(f"{_PROG}: writing {rel} failed: {exc}", file=sys.stderr)
                return 1
            declare_write(path)
            written.add(rel)

    if sibling_info and sibling_info["conventions"]:
        claude_md_path = os.path.join(target, "CLAUDE.md")
        try:
            with open(claude_md_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(f"# {project_name}\n\n{sibling_info['conventions']}\n")
        except OSError as exc:
            print(f"{_PROG}: writing CLAUDE.md failed: {exc}", file=sys.stderr)
            return 1
        declare_write(claude_md_path)
        written.add("CLAUDE.md")

    print(f"new_project.scaffold.file_manifest: {json.dumps(sorted(written))}")

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

    if _is_consumer():
        print(f"Not registering repos.{project_name.lower().replace('-', '_')}: consumer profile keeps no fleet registry entry.")
    else:
        reg_rc = _register_repo(project_name, target)
        if reg_rc != 0:
            return reg_rc

    print(f"Scaffold complete: {target}")
    return 0
