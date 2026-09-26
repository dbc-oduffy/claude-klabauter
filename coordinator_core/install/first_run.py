"""
coordinator_core.install.first_run -- Port of: the DoE-owned fresh-machine
bootstrap entrypoint `coordinator/scripts/first-run.sh` (DoE c3322493,
2026-07-22) (BIG_PORT Wave C, id: first-run).

Purpose: lands a freshly git-cloned coordinator-claude checkout on a new
machine. Detects/installs the toolchain (Homebrew, bash>=4.3, python@3.12,
node, uv, git-lfs) via `brew`, seeds the machine-local repo registry, then
runs the post-toolchain orchestration chain: install-substrate (in-process
import, template-variant #1) -> platform-localize (in-process import,
native as of the 2026-07-21 pure-Python-shop cutover -- see
run_post_toolchain's Step 4c) -> git lfs install. Step 4b
(ensure-coordinator-venv) is RETIRED (docs/plans/2026-08-18-retire-
coordinator-venv.md chunk C4): `coordinator_core.install.ensure_venv
.ensure_coordinator_venv` is reachable only via the explicit
`--allow-venv-fallback` opt-in elsewhere in the install chain
(`scripts/setup.py`, `coordinator_core.install.substrate`'s Step C10a-3),
never unconditionally from this module.

Unit decomposition (matches the DOE-PORT brief):
  unit1 -- arg parsing / config-load preamble                     -> parse_args()
  unit2 -- _fr_run_post_toolchain (the single largest function;
           toolchain-detection/post-install run)                  -> run_post_toolchain()
  unit3 -- _fr_next_step/_fr_build_plan/remaining orchestration/
           emit logic                                             -> build_plan(), main()

Architectural simplification (NOT a scope-drop -- read before touching this
file). The bash oracle re-exec'd ITSELF under a freshly-brew-installed
bash>=4.3 (`exec "$_fr_new_bash" "$SELF" --post-toolchain ...`) purely so
that ITS OWN post-toolchain function body -- bash-4 syntax the oracle's own
header calls out as confined to that path -- could be *executed*, not merely
*parsed*, under bash 3.2 (the well-known "define the function, never call it
on the 3.2 path" trick). This port is Python from the first line: there is
no bash-3.2-parse constraint on the interpreter running this module, so the
self-re-exec dance has no Python analogue and is dropped entirely. Observable
behavior is preserved 1:1 -- same probes, same plan text/step ordering, same
downstream scripts invoked in the same order, same exit-code contract.
`ensure-coordinator-venv` was a native in-process call (Port B,
`coordinator_core.install.ensure_venv`) prior to 2026-08-18; that Step 4b
call site is now retired outright (docs/plans/2026-08-18-retire-
coordinator-venv.md chunk C4 -- see the purpose paragraph above). The
oracle's Step 4c NEWLY-installed-bash requirement for `platform-
localize.sh` no longer applies at all -- see the retired-bug note below.

RETIRED oracle bug (2026-07-21 pure-Python-shop cutover -- this bug is FIXED,
not faithfully reproduced, unlike the rest of this port's parity contract).
Prior to this cutover, Step 4c resolved `platform-localize.sh` at
`$PLUGIN_ROOT/bin/platform-localize.sh` -- i.e. inside the COORDINATOR SOURCE
TREE, not the install destination -- and spawned `bash <that path>`.
`coordinator_core.install.substrate` installs `platform-localize.sh` to
`<settings-home>/bin/` (a DIFFERENT directory, never `$PLUGIN_ROOT/bin/` --
see `docs/wiki/coordinator-installer-shape.md` "durable-substrate-to-
settings-home"), so the old not-found guard fired on every machine lacking a
separately-placed copy at that source-tree path. Doubly broken: even when a
copy WAS found there, `platform-localize.sh` had itself already been ported
(DoE side) to a `#!/usr/bin/env python3` trampoline over THIS repo's own
`coordinator_core.hooks.platform_localize` -- so `bash <path>` fed Python
source to bash as a shell script, which never worked. Step 4c below now
calls `coordinator_core.hooks.platform_localize.main()` directly in-process,
which sidesteps both the wrong-path bug and the stale bash spawn.

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292
Spec backlink: pln-claude-klabauter-pure-python-shop-retire-0f8aee § C12
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import List, Optional

from coordinator_core.install._shared import env_overlay
from coordinator_core.install.timeouts import PLATFORM_PACKAGE_INSTALL_SECS
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.machine_resolver import registry_set
from coordinator_core.engine_root import coordinator_engine_root_with_class
from coordinator_core.ops.discover_working_repos import main as _discover_working_repos_main
from coordinator_core.win_portability import is_executable, no_console_creationflags


# Exit-code contract (PORTER-BRIEF-ADDENDUM § 3/3b).
#   3 -- DEDICATED transport-failure code for the TRAMPOLINE layer only (the
EXIT_OK = 0
EXIT_FAIL = 1

_SHORT_TIMEOUT = 20

_INSTALL_TIMEOUT = PLATFORM_PACKAGE_INSTALL_SECS


def _run(cmd: List[str], timeout: int = _SHORT_TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
        **kwargs,
    )


class _Args:
    dry_run: bool = False
    confirm: bool = False
    no_git_lfs: bool = False
    post_toolchain: bool = False
    non_interactive: bool = False


def parse_args(argv: List[str]) -> _Args:
    """unit1 -- mirrors the oracle's `while [ $# -gt 0 ]; case "$1" in ...`
    arg loop (L42-66) plus the COORDINATOR_NON_INTERACTIVE env-var mapping
    (L38-40). Unknown args print the oracle's own usage line to stderr and
    the caller exits 1 (business failure, not transport)."""
    args = _Args()
    if os.environ.get("COORDINATOR_NON_INTERACTIVE"):
        args.non_interactive = True

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--plan", "--dry-run"):
            args.dry_run = True
        elif tok in ("--confirm", "--yes"):
            args.confirm = True
        elif tok == "--no-git-lfs":
            args.no_git_lfs = True
        elif tok == "--post-toolchain":
            args.post_toolchain = True
        elif tok == "--non-interactive":
            args.non_interactive = True
        else:
            raise _UsageError(tok)
        i += 1
    return args


class _UsageError(Exception):
    def __init__(self, unknown_arg: str):
        super().__init__(unknown_arg)
        self.unknown_arg = unknown_arg


# detection block (L266-312): own minimal probes, no shared prereq_probe.


# returned EXIT_FAIL and killed the entire first-run flow, reported only as a

_LINUX_PKG_MANAGERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apt-get", ("apt-get", "install", "-y")),
    ("dnf", ("dnf", "install", "-y")),
    ("yum", ("yum", "install", "-y")),
    ("zypper", ("zypper", "--non-interactive", "install")),
    ("pacman", ("pacman", "-S", "--noconfirm")),
    ("apk", ("apk", "add", "--no-cache")),
)

_LINUX_PKG_NAMES: dict[str, dict[str, str]] = {
    "python@3.12": {
        "apt-get": "python3",
        "dnf": "python3",
        "yum": "python3",
        "zypper": "python3",
        "pacman": "python",
        "apk": "python3",
    },
    "node": {
        "apt-get": "nodejs",
        "dnf": "nodejs",
        "yum": "nodejs",
        "zypper": "nodejs",
        "pacman": "nodejs",
        "apk": "nodejs",
    },
    "git-lfs": {"apk": "git-lfs"},
}


def _host_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "nt" or sys.platform == "win32":
        return "windows"
    return "unknown"


def _running_as_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    try:
        return geteuid() == 0
    except OSError:
        return False


def _detect_linux_pkg_manager() -> Optional[str]:
    for name, _argv in _LINUX_PKG_MANAGERS:
        if shutil.which(name):
            return name
    return None


def _pkg_install_argv(manager: str, formula: str) -> List[str]:
    prefix = next(argv for name, argv in _LINUX_PKG_MANAGERS if name == manager)
    package = _LINUX_PKG_NAMES.get(formula, {}).get(manager, formula)
    argv = list(prefix) + [package]
    if not _running_as_root():
        argv.insert(0, "sudo")
    return argv


def _install_uv() -> int:
    print("[first-run] installing uv...")
    py = shutil.which("python3") or sys.executable
    try:
        proc = _run([py, "-m", "pip", "install", "--user", "uv"], timeout=_INSTALL_TIMEOUT)
        if proc.returncode == 0 and shutil.which("uv"):
            print("[first-run] uv installed (pip).")
            return EXIT_OK
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        pass
    print(
        "[first-run] ERROR: could not install uv automatically.\n"
        "  Install it and re-run: https://docs.astral.sh/uv/getting-started/installation/",
        file=sys.stderr,
    )
    return EXIT_FAIL


def _linux_pkg_install(formula: str, label: Optional[str] = None) -> int:
    label = label or formula
    manager = _detect_linux_pkg_manager()
    if manager is None:
        names = ", ".join(name for name, _ in _LINUX_PKG_MANAGERS)
        print(
            f"[first-run] ERROR: no supported package manager on PATH (looked for: {names}).\n"
            f"  Install {label} with this distribution's package manager and re-run.",
            file=sys.stderr,
        )
        return EXIT_FAIL
    argv = _pkg_install_argv(manager, formula)
    print(f"[first-run] {' '.join(argv)}...")
    try:
        proc = _run(argv, timeout=_INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        print(f"[first-run] ERROR: {' '.join(argv)} failed to run: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if proc.returncode != 0:
        print(f"[first-run] ERROR: {' '.join(argv)} exited non-zero.", file=sys.stderr)
        return EXIT_FAIL
    print(f"[first-run] {label} installed.")
    return EXIT_OK


def _pkg_install(formula: str, label: Optional[str] = None) -> int:
    platform = _host_platform()
    if platform == "darwin":
        return _brew_install(formula, label)
    if formula == "uv":
        return _install_uv()
    if platform == "linux":
        return _linux_pkg_install(formula, label)
    print(
        f"[first-run] ERROR: automatic install of {label or formula} is not supported on "
        f"this platform ({sys.platform}).\n"
        f"  Install it manually and re-run.",
        file=sys.stderr,
    )
    return EXIT_FAIL


class _Env:
    def __init__(self) -> None:
        self.bash_ok = False
        self.python_ok = False
        self.node_ok = False
        self.uv_ok = False
        self.git_lfs_ok = False
        self.brew_ok = False
        self.pkg_manager: Optional[str] = None


def _bash_version_ok(bash_path: str) -> bool:
    """Native reimplementation (2026-07-21 pure-Python-shop cutover): parses
    `bash --version`'s own banner line (e.g. "GNU bash, version 5.2.15(1)-
    release ...") instead of spawning `bash -c '<embedded script>'` to read
    back BASH_VERSINFO — still invokes the bash binary (unavoidable: this IS
    a probe of bash's own version) but no longer hands it a script to
    interpret."""
    try:
        proc = _run([bash_path, "--version"], capture_output=True, text=True)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    m = re.search(r"version (\d+)\.(\d+)", proc.stdout or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return major > 4 or (major == 4 and minor >= 3)


def detect_environment() -> _Env:
    env = _Env()

    bash_path = shutil.which("bash")
    if bash_path:
        env.bash_ok = _bash_version_ok(bash_path)

    py3 = shutil.which("python3")
    if py3:
        try:
            proc = _run([py3, "-c", "import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)"])
            env.python_ok = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            env.python_ok = False

    env.node_ok = shutil.which("node") is not None

    env.uv_ok = shutil.which("uv") is not None

    try:
        proc = _run(["git", "lfs", "version"], capture_output=True, text=True)
        env.git_lfs_ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        env.git_lfs_ok = False

    env.brew_ok = shutil.which("brew") is not None
    if _host_platform() == "linux":
        env.pkg_manager = _detect_linux_pkg_manager()

    return env


# (L318-356). Plain list, no bash-4 arrays needed here either.


def _plan_install_line(env: _Env, formula: str, suffix: str = "") -> str:
    if _host_platform() == "darwin":
        return f"brew install {formula}{suffix}"
    if formula == "uv":
        return f"install uv (pip --user; no distro packages it){suffix}"
    if env.pkg_manager:
        return f"{' '.join(_pkg_install_argv(env.pkg_manager, formula))}{suffix}"
    return f"install {formula} — NO SUPPORTED PACKAGE MANAGER ON PATH{suffix}"


def build_plan(env: _Env, no_git_lfs: bool) -> List[str]:
    steps: List[str] = []
    if _host_platform() == "darwin" and not env.brew_ok:
        steps.append("install Homebrew (absent on this machine)")
    if not env.bash_ok:
        _bash_note = (
            "  (>=4.3 required; stock macOS is 3.2)"
            if _host_platform() == "darwin"
            else "  (>=4.3 required)"
        )
        steps.append(_plan_install_line(env, "bash", _bash_note))
    if not env.python_ok:
        steps.append(_plan_install_line(env, "python@3.12", "  (Python 3.11+ required)"))
    if not env.node_ok:
        steps.append(_plan_install_line(env, "node"))
    if not env.uv_ok:
        steps.append(_plan_install_line(env, "uv"))
    if not no_git_lfs and not env.git_lfs_ok:
        steps.append(_plan_install_line(env, "git-lfs", "  then  git lfs install  (global, idempotent)"))
    elif no_git_lfs:
        steps.append("git-lfs SKIPPED (--no-git-lfs passed; LFS-backed clones will be pointer-only)")
    steps.append("seed machine-local registry  (post-toolchain, C1b, Step 3)")
    steps.append("run install-substrate -> platform-localize  (post-toolchain, C1b, Step 4)")
    steps.append("tell you to /reload-plugins")
    return steps


def _print_plan(steps: List[str]) -> None:
    print("about to:")
    for i, step in enumerate(steps, start=1):
        print(f"  [{i}] {step}")


_ML_REPOS_KEY_PREFIX = "repos."
"""The machine-local registry key namespace `_seed_machine_local_registry`
writes discovered sibling repos under (`repos.<derived-key>`). A module-
level constant so the writer's `_run([machine_local_bin, "set", ...])` call
site and `WRITE_SURFACE`'s shaped-clause template read one spelling."""

_REPOS_REGISTRY_CLAUSE_INDEX = 0
"""Index of `WRITE_SURFACE`'s sole SHAPED clause (the `repos.<derived-key>`
machine-local registry seed) — the only clause `_seed_machine_local_registry`
journals against; the other two clauses are `StaticClause`s and need no
resolution."""


def _record_resolution(clause_index: int, entries) -> None:
    from coordinator_core.install import resolution_journal

    resolution_journal.record_resolution("first-run", clause_index, entries)

_GIT_LFS_GLOBAL_CONFIG_REASON = (
    "`git lfs install` (global, idempotent — Step 5) writes the LFS smudge/"
    "clean/process filter into the operator's global git config; the exact "
    "key set (`filter.lfs.*`) is determined by the installed git-lfs "
    "version, not enumerable from this repo's own source, so this clause "
    "states the mechanism rather than an assumed key list."
)

_BREW_INSTALL_REASON = (
    "`brew install <formula>` (bash/python@3.12/node/uv/git-lfs, via "
    "`_brew_install`) invokes Homebrew, a third-party installer whose "
    "on-disk footprint (Cellar paths, symlinks, formula-specific post-"
    "install steps) is unbounded and not ours to enumerate — none of the "
    "eight declared kinds honestly names it, so this is a stated-reason "
    "entry naming the mechanism rather than a fabricated path/key. Same "
    "shape as `substrate._fnm_step`'s brew/curl fnm install."
)


def _derive_repo_key(repo_base: str) -> str:
    """Lowercase, non-alnum-collapse-to-underscore, strip leading/trailing
    underscore -- byte-identical to the oracle's `tr` pipeline (L150-154)
    and to cross-repo-memo's `_receiver_repo_key` resolver."""
    lowered = repo_base.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "_", lowered)
    return collapsed.strip("_")


def _seed_machine_local_registry(confirm: bool, non_interactive: bool) -> None:
    print("[post-toolchain] Seeding machine-local registry...")

    try:
        claude_klabauter_root_str, _resolution_class = coordinator_engine_root_with_class()
        claude_klabauter_root = Path(claude_klabauter_root_str)
    except RuntimeError as exc:
        print(f"[post-toolchain] WARNING: cannot resolve CLAUDE_KLABAUTER_ROOT to locate machine-local: {exc}", file=sys.stderr)
        print("  Register repos manually later: machine-local set repos.<name> <path>")
        _record_resolution(_REPOS_REGISTRY_CLAUSE_INDEX, ())
        return

    do_seed = True
    if not confirm and not non_interactive and sys.stdin.isatty():
        try:
            reply = input("Seed machine-local registry with discovered repos? [Y/n] ").strip().lower()
        except EOFError:
            reply = ""
        if reply not in ("", "y", "yes"):
            do_seed = False
            print("[post-toolchain] Registry seeding skipped. Register later: machine-local set repos.<name> <path>")

    if not do_seed:
        _record_resolution(_REPOS_REGISTRY_CLAUSE_INDEX, ())
        return

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _discover_working_repos_main([])
    except Exception as exc:  # noqa: BLE001 — never-block contract (matches discover_working_repos.main()'s own posture)
        print(f"[post-toolchain] WARNING: working-repo discovery failed: {exc}", file=sys.stderr)
    discovered = buf.getvalue().splitlines()

    found_any = False
    registered: List[WriteSurfaceEntry] = []
    for repo_path in discovered:
        repo_path = repo_path.strip()
        if not repo_path:
            continue
        found_any = True
        repo_base = os.path.basename(repo_path)
        repo_key = _derive_repo_key(repo_base)
        registry_key = f"{_ML_REPOS_KEY_PREFIX}{repo_key}"
        print(f"[post-toolchain] Registering {registry_key} = {repo_path}")
        try:
            registry_set(registry_key, repo_path)
        except (ValueError, OSError) as exc:
            print(f"[post-toolchain] WARNING: failed to register {registry_key}: {exc} — skipping.", file=sys.stderr)
            continue
        registered.append(WriteSurfaceEntry(kind="machine-local-key", key=registry_key))

    if not found_any:
        print("[post-toolchain] No repos discovered. Register later: machine-local set repos.<name> <path>")

    _record_resolution(_REPOS_REGISTRY_CLAUSE_INDEX, tuple(registered))


# stamped-build.md chunk C1) -- PREREQUISITE FOR that plan's C4 (fail-closed
# DIRECTORY, never the live working tree (`write_engine_stamp`'s own
# MEASUREMENT that retires the excuse (2026-08-21, normal tier, warm):
_PUBLISH_ROUND_ADVISORY_BUDGET_SECS = 30
_ENGINE_BUILD_SUBDIR = ("engine-build", "claude-klabauter")
_KLABAUTER_MIRROR_REGISTRY_KEY = "repos.claude_klabauter"
_KLABAUTER_MIRROR_PATH_REGISTRY_KEY = "publish.mirrors.claude_klabauter.path"
_KLABAUTER_MIRROR_ROW_NAME = "claude-klabauter-bin"


def _register_engine_key(key: str, value: str) -> bool:
    try:
        registry_set(key, value)
    except (ValueError, OSError) as exc:
        print(f"[first-run] WARNING: failed to register {key}: {exc}", file=sys.stderr)
        return False
    return True


def provision_stamped_engine(
    claude_klabauter_root: Path, timeout: int = _PUBLISH_ROUND_ADVISORY_BUDGET_SECS
) -> bool:
    """Ensure a registered, STAMPED engine root exists. Returns True iff one
    exists (already did, or was provisioned this call) — False is always
    advisory (a printed WARNING with a runnable remediation), never raised.

    Idempotent: a already-stamped destination is a no-op past the registry
    write. Safe to call from both `first_run.py`'s own post-toolchain
    sequence (the coordinator-claude bootstrap, which may run before
    claude-klabauter is even cloned -- see the engine-root resolution guard
    below) and `scripts/setup.py`'s `register_claude_klabauter_root` (claude-klabauter's
    own AUTHORITATIVE registration surface, which is where this reliably has
    a resolved `claude_klabauter_root` to work with).

    The publish round is bounded by `_PUBLISH_ROUND_ADVISORY_BUDGET_SECS`, a
    budget on how long an ADVISORY install step may block — deliberately not
    an estimate of how long `publish.py` takes, and deliberately outside the
    `install` timeout family. Expiry is a normal outcome here, not an error:
    it prints the same runnable remediation the non-zero-exit path prints and
    returns False, exactly as every other failure in this function does. See
    that constant for the measurement and the reasoning.
    """
    from coordinator_core.machine_resolver import registry_get
    from coordinator_core._settings_home import settings_home
    from coordinator_core.warm import skew

    registered = registry_get(_KLABAUTER_MIRROR_REGISTRY_KEY)
    dest = Path(registered) if registered else None
    if dest is None or not dest.is_dir():
        dest = settings_home().joinpath(*_ENGINE_BUILD_SUBDIR)

    stamp_path = dest / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    if stamp_path.is_file():
        _register_engine_key(_KLABAUTER_MIRROR_REGISTRY_KEY, str(dest))
        return True

    dest.mkdir(parents=True, exist_ok=True)
    if not (dest / ".git").is_dir():
        try:
            init_proc = _run(["git", "init", str(dest)], timeout=30, capture_output=True, text=True)
            if init_proc.returncode != 0:
                print(f"[first-run] WARNING: `git init {dest}` failed — cannot provision a stamped engine.", file=sys.stderr)
                return False
            commit_proc = _run(
                ["git", "-C", str(dest), "commit", "--allow-empty", "-m", "engine-build: init"],
                timeout=30,
                capture_output=True,
                text=True,
            )
            if commit_proc.returncode != 0:
                print(f"[first-run] WARNING: initial commit in {dest} failed — cannot provision a stamped engine.", file=sys.stderr)
                return False
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
            print(f"[first-run] WARNING: git init of engine-build directory failed: {exc}", file=sys.stderr)
            return False

    if not _register_engine_key(_KLABAUTER_MIRROR_PATH_REGISTRY_KEY, str(dest)):
        print(f"[first-run] WARNING: failed to register {_KLABAUTER_MIRROR_PATH_REGISTRY_KEY} — skipping engine provisioning.", file=sys.stderr)
        return False

    publish_script = claude_klabauter_root / "coordinator" / "bin" / "publish.py"
    if not publish_script.is_file():
        print(f"[first-run] WARNING: {publish_script} not found — cannot run a publish round.", file=sys.stderr)
        return False

    print(f"[first-run] Provisioning a stamped engine build at {dest} (running a publish round)...")
    try:
        publish_proc = _run(
            [sys.executable, str(publish_script), _KLABAUTER_MIRROR_ROW_NAME],
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        print(
            f"[first-run] WARNING: the publish round did not finish inside its {timeout}s "
            "advisory budget — this engine is not stamped. The round is over budget "
            "(publish.py, measured at 80.8s of process time for a --dry-run preview alone).",
            file=sys.stderr,
        )
        print(
            "  Remediation: run the round yourself, unbounded: "
            f"python coordinator/bin/publish.py {_KLABAUTER_MIRROR_ROW_NAME}",
            file=sys.stderr,
        )
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[first-run] WARNING: publish round failed to run: {exc}", file=sys.stderr)
        return False

    if publish_proc.returncode != 0:
        print(
            f"[first-run] WARNING: publish round into {dest} exited {publish_proc.returncode} — "
            "engine not stamped this run.",
            file=sys.stderr,
        )
        print(f"{publish_proc.stdout}\n{publish_proc.stderr}", file=sys.stderr)
        print(
            "  Remediation: once the reported issue is resolved, re-run: "
            f"python coordinator/bin/publish.py {_KLABAUTER_MIRROR_ROW_NAME}",
            file=sys.stderr,
        )
        return False

    if not stamp_path.is_file():
        print(
            f"[first-run] WARNING: publish round exited 0 but no stamp found at {stamp_path} — "
            "engine not registered.",
            file=sys.stderr,
        )
        return False

    if not _register_engine_key(_KLABAUTER_MIRROR_REGISTRY_KEY, str(dest)):
        print(f"[first-run] WARNING: failed to register {_KLABAUTER_MIRROR_REGISTRY_KEY} after a successful publish round.", file=sys.stderr)
        return False

    print(f"[first-run] Stamped engine registered: {_KLABAUTER_MIRROR_REGISTRY_KEY} = {dest}")
    return True


def run_post_toolchain(plugin_root: Path, args: _Args) -> int:
    """unit2 -- Steps 1-6. Returns EXIT_OK/EXIT_FAIL, fail-loud on any
    step's non-zero exit (mirrors the oracle's `|| { ...; exit 1; }` guards
    throughout `_fr_run_post_toolchain`).

    Env scoping (2026-07-21): ``CLAUDE_PLUGIN_ROOT`` is set for the DURATION of the
    run rather than written process-wide. The in-process phases below (substrate,
    ensure-venv, platform-localize) read it off ``os.environ``, so it cannot simply
    be dropped -- but as a bash script this was an ``export`` in a process about to
    exit, whereas the imported-module equivalent persisted for the interpreter's
    life and was inherited by every later subprocess child. See
    ``_shared.env_overlay`` for the general note.
    """
    with env_overlay({"CLAUDE_PLUGIN_ROOT": str(plugin_root)}):
        return _run_post_toolchain_steps(plugin_root, args)


def _run_post_toolchain_steps(plugin_root: Path, args: _Args) -> int:
    print(f"[post-toolchain] PLUGIN_ROOT={plugin_root}")

    # DoE-claude CLAUDE_PLUGIN_ROOT entirely and into claude-klabauter's OWN checkout
    # SCRIPT_DIR-relative not repo_root-relative -- see that file's header)
    try:
        claude_klabauter_root_for_preflight_str, _resolution_class = coordinator_engine_root_with_class()
        claude_klabauter_root_for_preflight = Path(claude_klabauter_root_for_preflight_str)
    except RuntimeError as exc:
        print(f"[post-toolchain] ERROR: cannot resolve the engine root for setup preflight: {exc}", file=sys.stderr)
        return EXIT_FAIL

    coordinator_tree_root = claude_klabauter_root_for_preflight / "coordinator"
    if coordinator_tree_root.is_dir():
        print("[post-toolchain] Running setup preflight (toolchain status, non-fatal)...")
        try:
            walker_env = {
                "COORDINATOR_SETUP_REPO_ROOT": str(coordinator_tree_root),
                "COORDINATOR_SETUP_LIB_DIR": str(coordinator_tree_root / "scripts" / "lib"),
            }
            with env_overlay(walker_env):
                from coordinator_core.ops.setup_chain_walker import main as _setup_chain_walker_main

                _setup_chain_walker_main(["--preflight"])
        except Exception as exc:  # noqa: BLE001 — non-fatal, mirrors the oracle's `|| true`.
            print(f"[post-toolchain] setup preflight raised (non-fatal, continuing): {exc}")
    else:
        print(
            f"[post-toolchain] setup preflight skipped: {coordinator_tree_root} not found "
            f"(resolved engine root={claude_klabauter_root_for_preflight}).",
            file=sys.stderr,
        )

    _seed_machine_local_registry(args.confirm, args.non_interactive)

    # `register_claude_klabauter_root` is the AUTHORITATIVE call site for that case
    try:
        claude_klabauter_root_for_engine_str, _resolution_class = coordinator_engine_root_with_class()
        provision_stamped_engine(Path(claude_klabauter_root_for_engine_str))
    except RuntimeError:
        print(
            "[post-toolchain] Skipping stamped-engine provisioning: the engine root not yet "
            "resolvable (claude-klabauter not cloned yet). scripts/setup.py provisions it "
            "once claude-klabauter is installed.",
        )

    print("[post-toolchain] Step 4a: install-substrate...")
    try:
        from coordinator_core.install.substrate import main as _substrate_main
    except ImportError as exc:
        print(f"[post-toolchain] ERROR: coordinator_core.install.substrate not importable: {exc}", file=sys.stderr)
        return EXIT_FAIL
    substrate_rc = _substrate_main([])
    if substrate_rc != 0:
        print("[post-toolchain] ERROR: install-substrate exited non-zero. Aborting.", file=sys.stderr)
        return EXIT_FAIL
    print("[post-toolchain] install-substrate: done.")


    # $PLUGIN_ROOT/bin/" negative-spec bug: this port no longer looks for
    print("[post-toolchain] Step 4c: platform-localize...")
    try:
        from coordinator_core.hooks.platform_localize import main as _platform_localize_main
    except ImportError as exc:
        print(f"[post-toolchain] ERROR: coordinator_core.hooks.platform_localize not importable: {exc}", file=sys.stderr)
        return EXIT_FAIL
    localize_rc = _platform_localize_main([])
    if localize_rc != 0:
        print("[post-toolchain] ERROR: platform-localize exited non-zero. Aborting.", file=sys.stderr)
        return EXIT_FAIL
    print("[post-toolchain] platform-localize: done.")

    if args.no_git_lfs:
        print("[post-toolchain] Skipping git lfs install (--no-git-lfs). LFS-backed clones will be pointer-only.")
    else:
        print("[post-toolchain] Running: git lfs install (global, idempotent)...")
        try:
            proc = _run(["git", "lfs", "install"], timeout=30)
            if proc.returncode != 0:
                print("[post-toolchain] WARNING: git lfs install failed (is git-lfs installed?). Continuing.", file=sys.stderr)
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            print("[post-toolchain] WARNING: git lfs install failed (is git-lfs installed?). Continuing.", file=sys.stderr)
        print("[post-toolchain] git lfs install: done.")

    print()
    print("================================================================")
    print("  first-run complete.")
    print()
    print("  Next step: in Claude Code, run:")
    print("    /reload-plugins")
    print()
    print("  (Do NOT restart Claude Code — /reload-plugins is sufficient.")
    print("   Note: a SessionStart hook cannot register into the session")
    print("   that is already running, so one /reload-plugins lag is")
    print("   inherent — this is expected.)")
    print("================================================================")
    return EXIT_OK


# Homebrew install + brew-offers (oracle L421-493). Live system mutation --


def _install_homebrew() -> int:
    print("[first-run] Installing Homebrew...")
    installer_url = "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh"
    try:
        proc = _run(
            ["/bin/bash", "-c", f'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL {installer_url})"'],
            timeout=_INSTALL_TIMEOUT,
        )
        if proc.returncode != 0:
            print("[first-run] ERROR: Homebrew installer exited non-zero.", file=sys.stderr)
            return EXIT_FAIL
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        print(f"[first-run] ERROR: Homebrew install failed to run: {exc}", file=sys.stderr)
        return EXIT_FAIL

    for candidate in (
        "/opt/homebrew/bin/brew",
        "/usr/local/bin/brew",
        "/home/linuxbrew/.linuxbrew/bin/brew",
    ):
        if is_executable(candidate):
            os.environ["PATH"] = os.path.dirname(candidate) + os.pathsep + os.environ.get("PATH", "")
            break

    if not shutil.which("brew"):
        print("[first-run] ERROR: Homebrew install succeeded but brew not found on PATH.", file=sys.stderr)
        print("  Add Homebrew to your PATH and re-run.", file=sys.stderr)
        return EXIT_FAIL
    print("[first-run] Homebrew installed.")
    return EXIT_OK


def _brew_install(formula: str, label: Optional[str] = None) -> int:
    label = label or formula
    print(f"[first-run] brew install {formula}...")
    try:
        proc = _run(["brew", "install", formula], timeout=_INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
        print(f"[first-run] ERROR: brew install {formula} failed to run: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if proc.returncode != 0:
        print(f"[first-run] ERROR: brew install {formula} exited non-zero.", file=sys.stderr)
        return EXIT_FAIL
    print(f"[first-run] {label} installed.")
    return EXIT_OK


# unit3 -- top-level orchestration (main). Mirrors oracle L358-526.


def main(argv: Optional[List[str]] = None) -> int:
    with env_overlay({}):
        return _main_body(argv)


def _main_body(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    try:
        args = parse_args(argv)
    except _UsageError as exc:
        print(f"first-run: unknown argument: {exc.unknown_arg}", file=sys.stderr)
        print(
            "Usage: first-run [--plan|--dry-run] [--confirm|--yes] [--no-git-lfs] [--non-interactive]",
            file=sys.stderr,
        )
        return EXIT_FAIL

    # unit1: PLUGIN_ROOT = parent of the resolved coordinator source tree.
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root_env:
        plugin_root = Path(plugin_root_env)
    else:
        # locate `bin/`, `lib/`, `scripts/` MUST pass CLAUDE_PLUGIN_ROOT
        plugin_root = Path.cwd()

    env = detect_environment()
    steps = build_plan(env, args.no_git_lfs)

    if args.dry_run:
        print("\nfirst-run.sh — dry run (no changes will be made)\n")
        _print_plan(steps)
        print("\nExiting (--dry-run / --plan). Re-run without the flag to proceed.")
        return EXIT_OK

    print("\nfirst-run.sh — new-machine first-run setup\n")
    _print_plan(steps)
    print()

    should_proceed = False
    if args.confirm:
        print("Proceeding (--confirm / --yes).\n")
        should_proceed = True
    elif sys.stdin.isatty():
        try:
            reply = input("Proceed? [Y/n] ").strip().lower()
        except EOFError:
            reply = "n"
        if reply in ("", "y", "yes"):
            should_proceed = True
        else:
            print("Aborted.")
            return EXIT_OK
    elif args.non_interactive:
        print("Non-interactive mode (COORDINATOR_NON_INTERACTIVE set) without --confirm.")
        print("Action plan printed above. Re-run with --confirm (or --yes) to execute.")
        return EXIT_OK
    else:
        print("first-run: non-interactive shell detected and --confirm not passed.", file=sys.stderr)
        print("Re-run with --confirm (or --yes) to proceed without a prompt.", file=sys.stderr)
        return EXIT_FAIL

    if not should_proceed:
        return EXIT_OK

    if _host_platform() == "darwin" and not env.brew_ok:
        rc = _install_homebrew()
        if rc != EXIT_OK:
            return rc
        env.brew_ok = shutil.which("brew") is not None

    if not env.bash_ok:
        rc = _pkg_install("bash", "bash")
        if rc != EXIT_OK:
            return rc

    if not env.python_ok:
        rc = _pkg_install("python@3.12", "python@3.12")
        if rc != EXIT_OK:
            return rc

    if not env.node_ok:
        rc = _pkg_install("node", "node")
        if rc != EXIT_OK:
            return rc

    if not env.uv_ok:
        rc = _pkg_install("uv", "uv")
        if rc != EXIT_OK:
            return rc

    if args.no_git_lfs:
        print("[first-run] Skipping git-lfs (--no-git-lfs). LFS-backed clones will be pointer-only.")
    elif not env.git_lfs_ok:
        rc = _pkg_install("git-lfs", "git-lfs")
        if rc != EXIT_OK:
            return rc
        print("[first-run] git-lfs package installed. `git lfs install` runs in post-toolchain Step 5.")

    return run_post_toolchain(plugin_root, args)


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="first-run",
    source_module="coordinator_core.install.first_run",
    clauses=(
        ShapedClause(
            discovered_by="_seed_machine_local_registry (discover_working_repos)",
            entry_template=WriteSurfaceEntry(
                kind="machine-local-key",
                key=f"{_ML_REPOS_KEY_PREFIX}<derived-key>",
            ),
        ),
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    reason=_GIT_LFS_GLOBAL_CONFIG_REASON,
                ),
            ),
        ),
        # `_BREW_INSTALL_REASON`) — a stated-reason entry naming the
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    reason=_BREW_INSTALL_REASON,
                ),
            ),
        ),
    ),
)


if __name__ == "__main__":
    sys.exit(main())
