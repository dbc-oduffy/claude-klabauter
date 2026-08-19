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
from coordinator_core.launchable import resolve_launchable
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)
from coordinator_core.claude_klabauter_root import coordinator_claude_klabauter_root_with_class
from coordinator_core.ops.discover_working_repos import main as _discover_working_repos_main
from coordinator_core.win_portability import is_executable, no_console_creationflags


# ---------------------------------------------------------------------------
# Exit-code contract (PORTER-BRIEF-ADDENDUM § 3/3b).
#   0 -- success (incl. dry-run preview, interactive-abort, non-interactive
#        print-and-exit -- all no-op-safe terminal states, matching the
#        oracle's own posture for those branches).
#   1 -- a real business failure (unknown arg, brew/toolchain install failed,
#        a post-toolchain step failed). Matches the oracle's uniform use of
#        exit 1 for every failure branch -- first-run.sh predates the
#        dedicated-code convention and never distinguished failure classes;
#        faithfully reproduced, not "improved" mid-port.
#   3 -- DEDICATED transport-failure code for the TRAMPOLINE layer only (the
#        claude-klabauter link/import failed before this module's own main() could
#        run at all) -- never returned by this module itself, only by the
#        DoE polyglot trampoline that imports it. Documented here so the two
#        files' contracts are readable together.
# ---------------------------------------------------------------------------
EXIT_OK = 0
EXIT_FAIL = 1

_SHORT_TIMEOUT = 20
_INSTALL_TIMEOUT = 900  # brew installs of large formulae (node) can be slow.


def _run(cmd: List[str], timeout: int = _SHORT_TIMEOUT, **kwargs) -> subprocess.CompletedProcess:
    """Every subprocess call in this module funnels through here: bounded
    timeout, stdin closed (never blocks waiting on a child's stdin read),
    console-flash suppressed on Windows (DR-054)."""
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
            # Accepted for CLI-shape compatibility with the retired oracle's
            # re-exec marker; this port never re-execs itself (see module
            # docstring), so the flag is a no-op here -- present so a caller
            # (or muscle-memory operator) invoking with the old flag does not
            # hit the unknown-argument branch.
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


# ---------------------------------------------------------------------------
# unit3 (detection half) -- toolchain probes. Mirrors the oracle's 3.2-safe
# detection block (L266-312): own minimal probes, no shared prereq_probe.
# ---------------------------------------------------------------------------


class _Env:
    def __init__(self) -> None:
        self.bash_ok = False
        self.python_ok = False
        self.node_ok = False
        self.uv_ok = False
        self.git_lfs_ok = False
        self.brew_ok = False


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
        # Absent/broken bash is a routine, expected prereq-probe outcome —
        # env.bash_ok=False surfaces via build_plan's install-step list below,
        # so no separate diagnostic here.
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
            env.python_ok = False  # routine prereq-probe outcome; surfaces via build_plan

    env.node_ok = shutil.which("node") is not None

    env.uv_ok = shutil.which("uv") is not None

    try:
        proc = _run(["git", "lfs", "version"], capture_output=True, text=True)
        env.git_lfs_ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
        env.git_lfs_ok = False  # routine prereq-probe outcome; surfaces via build_plan

    env.brew_ok = shutil.which("brew") is not None

    return env


# ---------------------------------------------------------------------------
# unit3 (plan-building half) -- mirrors _fr_next_step / _fr_build_plan
# (L318-356). Plain list, no bash-4 arrays needed here either.
# ---------------------------------------------------------------------------


def build_plan(env: _Env, no_git_lfs: bool) -> List[str]:
    steps: List[str] = []
    if not env.brew_ok:
        steps.append("install Homebrew (absent on this machine)")
    if not env.bash_ok:
        steps.append("brew install bash  (>=4.3 required; stock macOS is 3.2)")
    if not env.python_ok:
        steps.append("brew install python@3.12  (Python 3.11+ required)")
    if not env.node_ok:
        steps.append("brew install node")
    if not env.uv_ok:
        steps.append("brew install uv")
    if not no_git_lfs and not env.git_lfs_ok:
        steps.append("brew install git-lfs  then  git lfs install  (global, idempotent)")
    elif no_git_lfs:
        steps.append("git-lfs SKIPPED (--no-git-lfs passed; LFS-backed clones will be pointer-only)")
    # Review-parity note: the oracle emits a bash>=4.3 re-exec step here only
    # when bash_ok is false; this port never re-execs (see module docstring)
    # so that step line is intentionally NOT reproduced -- it would describe
    # a mechanism this port doesn't use. Everything downstream is unchanged.
    steps.append("seed machine-local registry  (post-toolchain, C1b, Step 3)")
    steps.append("run install-substrate -> platform-localize  (post-toolchain, C1b, Step 4)")
    steps.append("tell you to /reload-plugins")
    return steps


def _print_plan(steps: List[str]) -> None:
    print("about to:")
    for i, step in enumerate(steps, start=1):
        print(f"  [{i}] {step}")


# ---------------------------------------------------------------------------
# unit2 -- _fr_run_post_toolchain (182 LOC in the oracle). Steps 1-6.
# ---------------------------------------------------------------------------


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
    """Deferred-import wrapper over `resolution_journal.record_resolution`
    — see `clone_sibling_repo._record_resolution`'s docstring for why a
    module-level import of `resolution_journal` is not used here (this
    module is transitively reachable from `coordinator_core.ops`'s eager
    op-registration walk via its own downstream import graph)."""
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
    """Step 3. Prompts unless --confirm/--yes or non-interactive or stdin is
    not a TTY -- mirrors the oracle's prompt gate exactly.

    Discovery is an in-process call into
    ``coordinator_core.ops.discover_working_repos`` -- this repo's own
    native three-tier discovery port -- not a ``bash``-spawned subprocess.
    The DoE-side ``discover-working-repos.sh`` this used to shell out to is
    itself only a polyglot trampoline back onto that same claude-klabauter module (see
    that file's own header), so the subprocess hop was pure indirection with
    no logic on the other end to preserve; calling the module directly
    drops one more bash-spawn with no behavior change to the discovered-repo
    output. ``machine-local`` remains a real, separate CLI binary (not a
    claude-klabauter module) and is still invoked as a subprocess below -- but it now
    lives in claude-klabauter's own ``coordinator/bin/`` (the b644d5a9 executable-
    surface relocation moved it out of the DoE-claude ``CLAUDE_PLUGIN_ROOT``
    entirely), so it is resolved off ``coordinator_claude_klabauter_root()``, never
    ``plugin_root``.

    Missing-CLI tolerance is deliberate, not a gap: this step seeds the
    registry with OTHER discovered sibling repos (a convenience so a fresh
    machine doesn't have to hand-register every working repo), not the
    load-bearing ``repos.claude_klabauter`` entry itself -- that registration
    is scripts/setup.py's own job (see its module docstring, responsibility
    3). A fresh install's later steps (ensure-venv, platform-localize) do
    not read anything this step writes, so warn-and-continue is the correct
    posture here, same as the retired oracle's own prompt-and-skip gate.
    """
    print("[post-toolchain] Seeding machine-local registry...")

    try:
        claude_klabauter_root_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
        claude_klabauter_root = Path(claude_klabauter_root_str)
    except RuntimeError as exc:
        print(f"[post-toolchain] WARNING: cannot resolve CLAUDE_KLABAUTER_ROOT to locate machine-local: {exc}", file=sys.stderr)
        print("  Register repos manually later: machine-local set repos.<name> <path>")
        # Discovery precondition (CLAUDE_KLABAUTER_ROOT) unresolvable — resolved to
        # nothing this run, not "we never got there".
        _record_resolution(_REPOS_REGISTRY_CLAUSE_INDEX, ())
        return

    # `is_executable` passes the bare extension-less name on Windows because a
    # PATHEXT sibling (`machine-local.cmd`) is delivered alongside it — but the
    # bare file is not what CreateProcess can launch (WinError 193), so the argv
    # must name the sibling. `resolve_launchable` is that mapping.
    machine_local_bin = claude_klabauter_root / "coordinator" / "bin" / "machine-local"
    machine_local_argv = resolve_launchable(str(machine_local_bin))

    if not is_executable(machine_local_bin):
        print(f"[post-toolchain] WARNING: machine-local CLI not found/executable at {machine_local_bin}", file=sys.stderr)
        print("  Register repos manually later: machine-local set repos.<name> <path>")
        # The machine-local CLI (a required tool, not merely a discovered
        # input) is absent — resolved to nothing this run.
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
        # Operator declined the consent prompt — resolved to nothing this
        # run (a declined consent gate, per the design note's own example).
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
            set_proc = _run([*machine_local_argv, "set", registry_key, repo_path], timeout=30)
            if set_proc.returncode != 0:
                print(f"[post-toolchain] WARNING: failed to register {registry_key} — skipping.", file=sys.stderr)
                continue
        except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError):
            print(f"[post-toolchain] WARNING: failed to register {registry_key} — skipping.", file=sys.stderr)
            continue
        # Only a genuinely-succeeded `machine-local set` call is journaled
        # — a failed/timed-out registration performed no write to record.
        registered.append(WriteSurfaceEntry(kind="machine-local-key", key=registry_key))

    if not found_any:
        print("[post-toolchain] No repos discovered. Register later: machine-local set repos.<name> <path>")

    # Discovery ran (found_any True or False, and each attempted
    # registration succeeded or failed) — `registered` is the concrete,
    # fully-resolved set this run actually wrote, possibly empty (no repos
    # discovered, or every registration attempt failed).
    _record_resolution(_REPOS_REGISTRY_CLAUSE_INDEX, tuple(registered))


# ---------------------------------------------------------------------------
# Stamped-engine provisioning (docs/plans/2026-08-19-an-engine-root-is-a-
# stamped-build.md chunk C1) -- PREREQUISITE FOR that plan's C4 (fail-closed
# on an unstamped engine root). Without this, a fresh box would have no
# mirror and no way to get one once C4 lands: `_resolve_published_engine`
# would return None forever and the ladder would have nothing left to fall
# back to.
#
# SHAPE CHOSEN: (b) from C1's plan body -- a stamped LOCAL BUILD OUTPUT
# DIRECTORY, never the live working tree (`write_engine_stamp`'s own
# docstring forbids that: "never a development convenience", because it
# pins the generation while the code moves underneath it). (b) is admissible
# ONLY if it runs the SAME `_resolve_claude_klabauter_root` -> `_resolve_claude_klabauter_root`
# identifier transform a human publish round runs (see C11) -- this
# provisioning step satisfies that by invoking the REAL `coordinator/bin/
# publish.py` machinery (never reimplemented -- Hard constraint 6), targeting
# the `publish-mirror:claude_klabauter` row set, exactly as a human publish
# round does. Because the destination is a freshly `git init`'d local
# directory rather than a network clone of the published repo, (b) needs NO
# NETWORK AT ALL: a fresh clone with no connectivity still reaches a
# stamped, registered engine via this path -- this is (a) minus the network
# clone, per the plan body's own framing ("(b) is (a) minus the network
# clone -- a legitimate lighter-weight shape").
#
# Advisory, never fail-closed: this step WARNS and continues on any failure
# (git unavailable, dirty claude-klabauter tree failing publish's own dirty-tree gate,
# publish.py exiting non-zero) -- C1 ships and is verified BEFORE C4 removes
# the unstamped fallback (Hard constraint 3), so a failure here must not
# brick the rest of first-run/setup.py.
_PUBLISH_TIMEOUT = 900  # a real percolate round over ~40 rows is not fast.
_ENGINE_BUILD_SUBDIR = ("engine-build", "claude-klabauter")
_KLABAUTER_MIRROR_REGISTRY_KEY = "repos.claude_klabauter"
_KLABAUTER_MIRROR_PATH_REGISTRY_KEY = "publish.mirrors.claude_klabauter.path"
# One row from `setup/publish-targets.portable` whose dest sigil is
# `publish-mirror:claude_klabauter` and who has siblings sharing that sigil
# -- naming ANY one such row causes `publish.py`'s own `main()` to auto-
# expand the request to the row's WHOLE mirror (see that file's
# "Mirror-name/row-name collision resolution" block) -- so this single name
# publishes every row of the mirror, not just this one.
_KLABAUTER_MIRROR_ROW_NAME = "claude-klabauter-bin"


def _resolve_machine_local_argv(claude_klabauter_root: Path) -> Optional[List[str]]:
    """Same resolution `_seed_machine_local_registry` uses: `machine-local`
    now lives in claude-klabauter's own `coordinator/bin/`, never `plugin_root` (see
    that function's docstring)."""
    machine_local_bin = claude_klabauter_root / "coordinator" / "bin" / "machine-local"
    if not is_executable(machine_local_bin):
        return None
    return resolve_launchable(str(machine_local_bin))


def provision_stamped_engine(claude_klabauter_root: Path, timeout: int = _PUBLISH_TIMEOUT) -> bool:
    """Ensure a registered, STAMPED engine root exists. Returns True iff one
    exists (already did, or was provisioned this call) — False is always
    advisory (a printed WARNING with a runnable remediation), never raised.

    Idempotent: a already-stamped destination is a no-op past the registry
    write. Safe to call from both `first_run.py`'s own post-toolchain
    sequence (the coordinator-claude bootstrap, which may run before
    claude-klabauter is even cloned -- see the claude-klabauter-root resolution guard
    below) and `scripts/setup.py`'s `register_claude_klabauter_root` (claude-klabauter's
    own AUTHORITATIVE registration surface, which is where this reliably has
    a resolved `claude_klabauter_root` to work with).
    """
    from coordinator_core.machine_resolver import registry_get
    from coordinator_core._settings_home import settings_home
    from coordinator_core.warm import skew

    machine_local_argv = _resolve_machine_local_argv(claude_klabauter_root)
    if machine_local_argv is None:
        print(
            "[first-run] WARNING: machine-local CLI not found — cannot provision or "
            "register a stamped engine root.",
            file=sys.stderr,
        )
        print(
            "  Remediation: once machine-local is installed, run: "
            "python coordinator/bin/publish.py claude-klabauter-bin",
            file=sys.stderr,
        )
        return False

    registered = registry_get(_KLABAUTER_MIRROR_REGISTRY_KEY)
    dest = Path(registered) if registered else None
    if dest is None or not dest.is_dir():
        dest = settings_home().joinpath(*_ENGINE_BUILD_SUBDIR)

    stamp_path = dest / "coordinator_core" / skew.ENGINE_STAMP_FILENAME
    if stamp_path.is_file():
        # Already stamped — just make sure the registry agrees (idempotent).
        _run([*machine_local_argv, "set", _KLABAUTER_MIRROR_REGISTRY_KEY, str(dest)], timeout=30)
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

    set_proc = _run(
        [*machine_local_argv, "set", _KLABAUTER_MIRROR_PATH_REGISTRY_KEY, str(dest)], timeout=30,
        capture_output=True, text=True,
    )
    if set_proc.returncode != 0:
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
    except (OSError, subprocess.TimeoutExpired, subprocess.SubprocessError) as exc:
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

    reg_proc = _run([*machine_local_argv, "set", _KLABAUTER_MIRROR_REGISTRY_KEY, str(dest)], timeout=30)
    if reg_proc.returncode != 0:
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
    """Steps 1-6 proper. Split out of ``run_post_toolchain`` so the env overlay
    wraps the whole sequence without re-indenting it; not a separate seam."""
    print(f"[post-toolchain] PLUGIN_ROOT={plugin_root}")

    # Step 2: optional preflight via the coordinator-claude install-chain
    # walker (non-fatal on the walker's own failure). The retired oracle's
    # `plugin_root/scripts/setup.sh` was itself a python3-shebanged
    # trampoline over this SAME engine's coordinator_core.ops.setup_chain_walker
    # -- an in-process call replaces the stale `bash <path>` spawn, which fed
    # the trampoline's Python source to bash as a script (never worked;
    # reimplemented native, not merely de-bashed). The trampoline's SOURCE
    # FILE has since moved: the b644d5a9 executable-surface relocation moved
    # coordinator/scripts/ (and coordinator/lib/, coordinator/bin/) out of the
    # DoE-claude CLAUDE_PLUGIN_ROOT entirely and into claude-klabauter's OWN checkout
    # (this repo's `coordinator/` tree) -- so the walker's repo_root/lib_dir
    # env vars (mirroring the trampoline's own `main()`: repo_root =
    # <coordinator-tree-root>, lib_dir = <coordinator-tree-root>/scripts/lib,
    # SCRIPT_DIR-relative not repo_root-relative -- see that file's header)
    # must resolve off coordinator_claude_klabauter_root(), never plugin_root. Unlike
    # site 3 below, an unresolvable CLAUDE_KLABAUTER_ROOT here is fail-loud: this is
    # install-path code and a broken/absent claude-klabauter checkout at this point
    # means the rest of Steps 4a-4c (which import coordinator_core modules
    # that live in THIS SAME checkout) cannot possibly succeed either --
    # silently skipping the preflight and stumbling into those steps would
    # produce a much more confusing failure downstream.
    try:
        claude_klabauter_root_for_preflight_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
        claude_klabauter_root_for_preflight = Path(claude_klabauter_root_for_preflight_str)
    except RuntimeError as exc:
        print(f"[post-toolchain] ERROR: cannot resolve CLAUDE_KLABAUTER_ROOT for setup preflight: {exc}", file=sys.stderr)
        return EXIT_FAIL

    coordinator_tree_root = claude_klabauter_root_for_preflight / "coordinator"
    if coordinator_tree_root.is_dir():
        print("[post-toolchain] Running setup preflight (toolchain status, non-fatal)...")
        # Scoped to the walker call only (2026-07-21): these two vars are the
        # retired trampoline's own argv-equivalent — they were a CHILD process's
        # env when this was a `bash <path>` spawn and have no business outliving
        # the in-process call that replaced it.
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
            f"(resolved CLAUDE_KLABAUTER_ROOT={claude_klabauter_root_for_preflight}).",
            file=sys.stderr,
        )

    # Step 3: seed machine-local registry.
    _seed_machine_local_registry(args.confirm, args.non_interactive)

    # Step 3b: provision a stamped engine root (docs/plans/2026-08-19-an-
    # engine-root-is-a-stamped-build.md C1), best-effort. On a genuinely
    # fresh box this usually no-ops here (claude-klabauter is not yet cloned,
    # so CLAUDE_KLABAUTER_ROOT is unresolvable) -- `scripts/setup.py`'s own
    # `register_claude_klabauter_root` is the AUTHORITATIVE call site for that case
    # and calls the same function once claude-klabauter's own installer runs.
    # This call site exists for the re-run/already-registered case.
    try:
        claude_klabauter_root_for_engine_str, _resolution_class = coordinator_claude_klabauter_root_with_class()
        provision_stamped_engine(Path(claude_klabauter_root_for_engine_str))
    except RuntimeError:
        print(
            "[post-toolchain] Skipping stamped-engine provisioning: CLAUDE_KLABAUTER_ROOT not yet "
            "resolvable (claude-klabauter not cloned yet). scripts/setup.py provisions it "
            "once claude-klabauter is installed.",
        )

    # Step 4a: install-substrate — in-process import (template-variant #1,
    # direct-import; this caller is now Python, so the subprocess `python3
    # -m coordinator_core.install.substrate` the oracle used is upgraded to
    # a plain call, same as coordinator-auto-push / handoff-gate-aging).
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

    # Step 4b: ensure-coordinator-venv -- RETIRED (docs/plans/2026-08-18-
    # retire-coordinator-venv.md chunk C4, AC5). `ensure_coordinator_venv`
    # is now reachable ONLY via the explicit `--allow-venv-fallback` opt-in
    # (`scripts/setup.py`'s `_fallback_to_venv` / `provision_deps`, and
    # `coordinator_core.install.substrate`'s flag-gated Step C10a-3); this
    # unconditional first-run call site is retired outright, not
    # flag-gated, because first-run.py's own CLI carries no such flag.
    # Machine-interpreter `coordinator_whoami` provisioning no longer
    # depends on this call — it is handled independently by
    # `scripts/setup.py`'s post-registration advisory step (chunk C10).

    # Step 4c: platform-localize -- native in-process call (2026-07-21
    # pure-Python-shop cutover). `plugin_root/bin/platform-localize.sh` is
    # itself a python3-shebanged trampoline over this SAME engine's
    # coordinator_core.hooks.platform_localize (DoE-owned file kept
    # `.sh`-suffixed for caller-path stability -- see that trampoline's own
    # header); the retired `bash <path>` spawn fed the trampoline's Python
    # source to bash as a script, which never worked -- reimplemented
    # native, not merely de-bashed. This also RETIRES the module docstring's
    # previously-documented "platform-localize.sh not found at
    # $PLUGIN_ROOT/bin/" negative-spec bug: this port no longer looks for
    # that file on disk at all, so the resolved-source-tree-vs-install-
    # destination path mismatch it described can no longer fire.
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

    # Step 5: git-lfs (unless --no-git-lfs). `git lfs install` is idempotent.
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

    # Step 6: closing instruction.
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


# ---------------------------------------------------------------------------
# Homebrew install + brew-offers (oracle L421-493). Live system mutation --
# each step idempotent (brew install is a no-op when already sufficient).
# ---------------------------------------------------------------------------


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

    # Deliberate process-env write: the `_brew_install` steps that follow in
    # `_main_body` resolve `brew` off os.environ["PATH"], so this cannot be dropped.
    # Bounded by the empty `env_overlay` wrapping `main()` -- see its docstring.
    for candidate in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew"):
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


# ---------------------------------------------------------------------------
# unit3 -- top-level orchestration (main). Mirrors oracle L358-526.
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry.

    Env scoping (2026-07-21): the whole run executes under an empty
    ``env_overlay``, so every ``os.environ`` write it performs is unwound on
    return. The load-bearing one is ``_install_homebrew``'s PATH prepend, which is
    NOT deletable -- the ``_brew_install`` calls that follow it (and the
    ``shutil.which("brew")`` re-probe) resolve ``brew`` off ``os.environ["PATH"]``.
    As a bash script that export died with the process; as an imported module it
    persisted for the interpreter's life and leaked into every later subprocess
    child. Scoping here rather than around the assignment keeps the downstream
    steps' view of PATH intact while bounding the write to this invocation.
    """
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
    # The trampoline resolves and passes this via env (see DoE-side file);
    # fall back to this module's own package location for direct-import
    # callers/tests that don't go through the trampoline.
    plugin_root_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root_env:
        plugin_root = Path(plugin_root_env)
    else:
        # coordinator_core/install/first_run.py has no reliable relative path
        # to a DoE coordinator/ tree in the general case (they are separate
        # repos) — callers that need the toolchain-mutation flow to actually
        # locate `bin/`, `lib/`, `scripts/` MUST pass CLAUDE_PLUGIN_ROOT
        # (the trampoline always does — see its own header).
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

    if not env.brew_ok:
        rc = _install_homebrew()
        if rc != EXIT_OK:
            return rc
        env.brew_ok = shutil.which("brew") is not None

    if not env.bash_ok:
        rc = _brew_install("bash", "bash")
        if rc != EXIT_OK:
            return rc
        # Review: code-reviewer -- Finding 4 (2026-07-17 BIG_PORT Wave C sidecar):
        # env.bash_ok is intentionally NOT re-derived here. Nothing downstream reads
        # it again -- Step 4c is a native in-process call (platform-localize) that
        # doesn't invoke bash at all (2026-07-21 pure-Python-shop cutover retired
        # the last bash-dependent Step 4c path); Step 4b (ensure-coordinator-venv)
        # is retired outright (docs/plans/2026-08-18-retire-coordinator-venv.md
        # chunk C4) and never ran under bash either. A re-check here would just
        # be dead state.

    if not env.python_ok:
        rc = _brew_install("python@3.12", "python@3.12")
        if rc != EXIT_OK:
            return rc

    if not env.node_ok:
        rc = _brew_install("node", "node")
        if rc != EXIT_OK:
            return rc

    if not env.uv_ok:
        rc = _brew_install("uv", "uv")
        if rc != EXIT_OK:
            return rc

    if args.no_git_lfs:
        print("[first-run] Skipping git-lfs (--no-git-lfs). LFS-backed clones will be pointer-only.")
    elif not env.git_lfs_ok:
        rc = _brew_install("git-lfs", "git-lfs")
        if rc != EXIT_OK:
            return rc
        print("[first-run] git-lfs brew formula installed. `git lfs install` runs in post-toolchain Step 5.")

    return run_post_toolchain(plugin_root, args)


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="first-run",
    source_module="coordinator_core.install.first_run",
    clauses=(
        # Clause 1 — `_seed_machine_local_registry`'s Step 3: registers
        # every sibling repo `discover_working_repos` finds under
        # `repos.<derived-key>` via `machine-local set`. SHAPED: the key
        # set depends on what's discovered on this machine at run time, not
        # enumerable in source. Same `repos.<derived-key>` shape as
        # write_surface.py's own SHAPED-form worked example.
        ShapedClause(
            discovered_by="_seed_machine_local_registry (discover_working_repos)",
            entry_template=WriteSurfaceEntry(
                kind="machine-local-key",
                key=f"{_ML_REPOS_KEY_PREFIX}<derived-key>",
            ),
        ),
        # Clause 2 — Step 5: `git lfs install` (global, idempotent),
        # skipped when `--no-git-lfs`. A real global git-config mutation,
        # but the exact `filter.lfs.*` key set is owned by the installed
        # git-lfs binary, not knowable from this repo's own source — stated
        # reason, not a fabricated key list.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="git-config-key",
                    reason=_GIT_LFS_GLOBAL_CONFIG_REASON,
                ),
            ),
        ),
        # Clause 3 — `_brew_install` (bash/python@3.12/node/uv/git-lfs
        # formulae, invoked from `_main_body` when `detect_environment`
        # finds a tool absent). No kind in the eight-kind vocabulary
        # honestly names an unbounded third-party-installer footprint (see
        # `_BREW_INSTALL_REASON`) — a stated-reason entry naming the
        # mechanism, deliberately not a fabricated `file-path`, so this
        # surface is visible to a future drift/uninstall pass rather than
        # silently missing.
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
