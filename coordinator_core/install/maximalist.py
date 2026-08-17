"""
coordinator_core.install.maximalist — cold maximalist coordinator install
phase-sequence orchestrator.

Naked-Python port of ``coordinator/scripts/install-maximalist.sh``
[DoE-claude repo] — the F11 "hand-run ~15 scripts in order" collapse into
ONE re-runnable command. The DoE-side polyglot trampoline (same filename,
``.sh`` KEPT per the template-variant #1 convention § "avoid N caller edits
with zero functional benefit", since ``README.md``/``INSTALL.md``/the
packageability manifest's ``programmatic_entry_point`` hardcode
``coordinator/scripts/install-maximalist.sh`` verbatim) self-resolves
``CLAUDE_PLUGIN_ROOT``/``REPO_DOE_CLAUDE`` from its own on-disk location with
no plugin-registration dependency of its own, exports them into
``os.environ`` (mirroring the bash oracle's own ``export
CLAUDE_PLUGIN_ROOT``/``export REPO_DOE_CLAUDE``) — but THIS module (the
orchestration body it imports) is claude-klabauter-resident post-port (DR-047), so
the real precondition to reach ``main()`` here is: claude-klabauter cloned AND
``repos.claude_klabauter`` registered via machine-local, OR ``CLAUDE_KLABAUTER_ROOT``
exported manually. Pre-port (all-bash), this script was genuinely
self-contained and runnable as the very first command on a bare machine;
post-port that property no longer holds — the trampoline must resolve
CLAUDE_KLABAUTER_ROOT before it can even import ``main`` below. Once import succeeds,
the trampoline does a plain **in-process import** of ``main`` here —
template-variant #1 (like ``coordinator-auto-push``), NOT the IPC/
``cc_invoke`` op path. This orchestrator itself further shells out to a mix
of DoE-side sub-scripts (some already-ported polyglot trampolines, some
still bash-only) via ``subprocess.run`` per phase, matching the bash
oracle's own subprocess-per-phase shape — that per-phase subprocess fan-out
is internal to ``run()``/``main()``, distinct from how the trampoline
reaches this module.

.. Review: code-reviewer — F1, "callable before any plugin is registered"
   claim was false post-port (this module requires CLAUDE_KLABAUTER_ROOT resolution
   to even be imported); corrected framing to state the real precondition.

FAMILY-I fresh-install surface: this is the maximalist installer
ORCHESTRATOR. ``coordinator_core.install.substrate`` and
``coordinator_core._settings_home`` (called BY Phase 3 below) are
CALLED BY this module, NOT ports of the orchestration logic itself
(related-distinct, not duplicated coverage).

Documented divergence from the bash oracle (structural, not a scope-drop):
  - The bash-4-version guard (BASH_VERSINFO[0]<4) at the top of the oracle
    existed because the SCRIPT ITSELF used bash-4-only syntax. This module
    is plain Python — the concern is structurally inapplicable, not silently
    dropped. The DoE-side trampoline (a sh/python polyglot, like every other
    ported trampoline in this migration) still parses cleanly on bash 3.2
    per DR-148 (its re-exec line is a bare string under sh).
  - Phase 3 Step 1 (install-substrate) is called via a **direct in-process
    import** (``coordinator_core.install.substrate.main``) rather than the
    oracle's ``env PYTHONPATH=<claude_klabauter_root> python3 -m
    coordinator_core.install.substrate`` subprocess dance — this module
    already lives inside ``coordinator_core`` in the same process, so the
    oracle's ``machine-local get repos.claude_klabauter`` resolution (whose
    entire purpose was locating a *separate* claude-klabauter checkout to construct
    the subprocess's PYTHONPATH) is moot here and is not reproduced.
  - 2026-07-21 (retire-all-bash C13): the remaining ten ``["bash", ...]``
    per-phase subprocess spawns (detect-existing-claude-home,
    install-health-run, gen-doe-root-pointer, gen-claude-doe-shim,
    gen-claude-doe-launcher, register-coordinator-mirror,
    check-install-singularity, capture-fan-out-threshold,
    platform-localize, coordinator-setup-state record setup_concluded) are
    now **direct in-process calls**, same idiom as Step 3.5c's
    gen-settings-hooks (DR-059). Each DoE-side ``.sh``/``.py`` this module
    used to spawn was ALREADY only a thin polyglot trampoline back into a
    ``coordinator_core.ops``/``coordinator_core.install``/
    ``coordinator_core.hooks`` module living in THIS package (grepped at
    port time — none of the ten carries real business logic of its own);
    the prior paragraph's "belongs to THEIR repo" rationale never actually
    applied to these ten, only to genuinely-DoE-owned, still-bash siblings
    (e.g. the ``bin/install-health/*.sh`` drop-ins ``install-health-run``
    itself still fans out to) that remain subprocess-delegated because they
    carry logic this repo has no business duplicating.
    register-coordinator-mirror's own DoE-local "coordinator live path"
    resolution used to shell out to ``resolve-coordinator-clone.sh
    --for-content`` (script-relative bash spawn, with a ``claude-home
    plugins`` fallback). DR-079 (2026-07-21) repoints Tier 1 to the native
    ``coordinator_core.resolve_coordinator_clone.resolve_content_root()``
    peer — a verified drop-in (``--for-content`` is a retained legacy alias
    of ``--content-root``, identical resolution ladder) — retiring that
    bash spawn entirely; ``_resolve_coordinator_live_path`` now calls the
    native function in-process for Tier 1 and keeps the ``claude-home
    plugins`` Tier 2 fallback (still a genuine external CLI, no native peer)
    unchanged.
  - Step 9 (platform-localize) "installed script not found" branch:
    the bash oracle's asymmetric WARN+FAILED-without-halting case existed
    because ``platform-localize.sh`` was a per-machine TEMPLATE artifact
    rendered by install-substrate that could plausibly be absent (a
    rendering bug, a partial install). Called in-process via
    ``coordinator_core.hooks.platform_localize.main``, there is no longer a
    per-machine file whose absence is even possible to observe — the logic
    ships inside this same package. The "not found" branch is retired as
    inapplicable rather than reproduced as dead code; the phase is now a
    plain ``run_required`` like its siblings (see
    ``test_maximalist.py`` for the updated coverage).

Negative-spec (faithful oracle-bug repro, not a fix):
  - ``run_advisory`` sets a module-level FAILED flag but never halts the
    chain — exactly like the bash oracle's non-halting Step 6/Step 7
    contract.
  - ``--check-only`` is read-only about MUTATIONS only, not about hard
    preconditions — a genuinely-missing wrapper source or python interpreter
    still FATALs even under ``--check-only``, matching the oracle exactly.
  - Step 9 (platform-localize) no longer has a "not found" case to
    reproduce (see the divergence note above) — retired, not normalized in
    place, since the file whose absence the oracle's asymmetry was about
    can no longer be absent from an in-process call.

Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
Spec backlink: coordinator/commands/install.md (Phase 1-7); tasks/2026-07-08-install-dogfood-friction.md § F11
Prior bash implementation: coordinator/scripts/install-maximalist.sh (622 lines,
    retired as the live body but the filename/CLI contract preserved verbatim
    by the DoE-side polyglot trampoline — see git log for the prior body).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from coordinator_core._settings_home import native_path_form
from coordinator_core.win_portability import no_console_creationflags

# A2: every subprocess.run gets a bounded timeout + stdin=DEVNULL. Cold,
# one-shot install phases can be slow (pip installs in ensure-coordinator-venv)
# but must never hang the orchestrator forever on a wedged child.
_SUBPROCESS_TIMEOUT = 600

_USAGE = """\
install-maximalist.sh -- single idempotent orchestrator for the cold maximalist
coordinator install phase sequence.

Usage:
  install-maximalist.sh [--check-only] [--non-interactive] [--help|-h]

Flags:
  --check-only       Read-only report pass -- no mutations. Every phase runs its
                      read-only checks / dry-run mode and reports would-do state.
  --non-interactive   Suppresses any prompt sub-scripts might otherwise offer.
                      This orchestrator itself never prompts; phases that
                      inherently require a human decision (operator identity,
                      project type, persona customization, optional
                      integrations) are always skipped here regardless of this
                      flag, with a pointer to run /coordinator:install for
                      the guided version of that step.
  --help, -h          Print this usage and exit 0.

What this does:
  Runs the mechanical (non-judgment-call) subset of the cold maximalist
  install phase sequence, end-to-end, in the exact order documented in
  coordinator/commands/install.md:
    1.  install-substrate.sh           (Phase 3 Step 1 -- machine-local substrate)
    2.  install-health-run.sh          (Phase 3 Step 1b -- drop-in health scripts)
    3.  seed repos.doe_claude registry (best-effort, self-resolved clone path)
    4.  gen-doe-root-pointer.sh        (Step 3.5a.1 -- ~/.claude/.doe-root pointer)
    5.  gen-claude-doe-shim.sh         (Step 3.5a.2 -- claude() shell shim)
    6.  claude-doe wrapper install     (Step 3.5b -- ~/.local/bin/claude-doe)
    6.5 gen-claude-doe-launcher.sh     (Step 3.5b.2 -- Windows-only launcher; no-op elsewhere)
    7.  gen-settings-hooks.sh          (Step 3.5c -- settings.json hook block)
    8.  register-coordinator-mirror.sh (Step 5 -- plugin.mirrors registration)
    9.  ensure-coordinator-venv         (Step 6 -- coordinator_whoami venv; native)
    9.5 compileall                     (Step 6b -- precompile coordinator_core bytecode; native)
    10. scaffold-canonical-structure    (Step 7 -- canonical doc structure; native)
    11. check-install-singularity.sh   (Step 7.5 -- canonical-locus integrity gate)
    12. capture-fan-out-threshold.sh   (Step 8 -- fan-out large-wave threshold)
    13. platform-localize.sh           (Step 9 -- settings.local.json / marketplaces)
    14. coordinator-setup-state.sh record setup_concluded (Phase 7 Step 0 -- receipt)

What this deliberately skips (run /coordinator:install for these -- the
guided/interactive superset):
    - Phase 1 env-normalization offers (bash/PowerShell/Windows Terminal installs)
    - Phase 2 operator identity capture + working-repos discovery + CLAUDE.local.md
    - Phase 4 ~/.claude git-tracking offer
    - Phase 5 coordinator.local.md (project-local -- that is /coordinator:repo-setup)
    - Phase 6 persona customization / 1Password GitHub auth
    - Phase 7 guided orientation walkthrough ("walk me through the coordinator")

Idempotent and safe to re-run at any time -- every phase delegates to an
already-idempotent sub-script; nothing here clobbers live registry/config files.
"""


class _UsageError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _parse_args(argv: Sequence[str]) -> Optional[Dict[str, bool]]:
    """Returns {'check_only':.., 'non_interactive':..} or None (help printed, exit 0)."""
    check_only = False
    non_interactive = False
    for arg in argv:
        if arg == "--check-only":
            check_only = True
        elif arg == "--non-interactive":
            non_interactive = True
        elif arg in ("--help", "-h"):
            print(_USAGE)
            return None
        else:
            raise _UsageError(arg)
    return {"check_only": check_only, "non_interactive": non_interactive}


def _run(cmd: Sequence[str], env: Optional[Dict[str, str]] = None) -> int:
    """subprocess.run wrapper: bounded timeout, stdin guard, no console flash (A2/A4)."""
    try:
        result = subprocess.run(
            list(cmd),
            env=env,
            stdin=subprocess.DEVNULL,
            timeout=_SUBPROCESS_TIMEOUT,
            **no_console_creationflags(),
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        print(
            f"ERROR: command timed out after {_SUBPROCESS_TIMEOUT}s: {' '.join(cmd)}",
            file=sys.stderr,
        )
        return 124
    except FileNotFoundError as exc:
        print(f"ERROR: command not found: {cmd[0]} ({exc})", file=sys.stderr)
        return 127


def _registry_get_for_check(key: str) -> Optional[str]:
    """Best-effort registry read for check-only freshness reporting on a
    best-effort seed block. Uses the direct-tomllib
    ``coordinator_core.machine_resolver.registry_get`` reader -- NOT the
    ``machine-local`` CLI -- deliberately: the real (non-check-only) seed
    blocks this mirrors are pinned by
    ``test_seed_claude_klabauter_check_only_does_not_invoke_machine_local`` to
    never shell out to ``machine-local`` during ``--check-only`` (side-effect
    and reset-order risk on a code path with no other reason to spawn a
    subprocess). The tomllib reader is process-local and read-only, so it
    reports accurately without touching that contract."""
    from coordinator_core.machine_resolver import registry_get

    return registry_get(key)


from coordinator_core.install._shared import RequireHomeError, require_home
from coordinator_core.install._shared import env_overlay as _env_overlay
from coordinator_core.install.write_surface import (
    ShapedClause,
    StaticClause,
    WriteSurfaceDeclaration,
    WriteSurfaceEntry,
)


def _collect_writer_declarations(
    repo_root: Path,
) -> "tuple[Dict[str, WriteSurfaceDeclaration], List[str]]":
    """Discover every writer's `WriteSurfaceDeclaration`, keyed by
    `writer_id`, by delegating to `write_surface_manifest`'s public
    `discover_declarations` seam (shared AST-scan discovery) rather than
    re-deriving a second writer roster — C4's brief is explicit that
    hand-listing writers here would recreate the exact staleness class the
    manifest's own discovery mechanism was built to eliminate (a prior
    hand-maintained roster went stale by 13 entries within hours).

    A module that fails to import, or whose `WRITE_SURFACE` attribute is
    not a `WriteSurfaceDeclaration`, is LOUD here (a WARN to stderr naming
    the module and exception) and returned as a synthetic id in the second
    tuple element, for the caller to fold into `unreported_writer_ids`.

    Review: code-reviewer (P2) -- this previously silently `continue`d on
    such a failure with zero logging, and the failed module landed in
    neither `derivations` nor `unreported` -- indistinguishable from a
    writer that was never part of the install target set at all. That is
    precisely the "did not report" vs. "nothing to remove" collapse the
    design note's negative spec forbids (see
    docs/research/2026-08-06-install-receipt-persistence-design.md).
    The prior docstring here claimed `write_surface_manifest`'s own
    emission op surfaces this loudly -- true of THAT op, but that is a
    separate, not-guaranteed-to-run command that never fires as part of
    `_build_and_persist_receipt`; this fix makes the failure loud on THIS
    path instead of relying on an unrelated call site.

    The first module to claim a given `writer_id` wins (matches the
    manifest's own first-claim precedent) since this function only needs
    ONE declaration per writer to drive receipt derivation.
    """
    from coordinator_core.ops.write_surface_manifest import discover_declarations

    declarations, failures = discover_declarations(repo_root)
    failed_ids: List[str] = []
    for source_hint, reason in failures:
        print(
            f"WARN: writer discovery failed for {source_hint} -- this "
            f"module's receipt coverage will be recorded as unreported "
            f"rather than silently dropped: {reason}",
            file=sys.stderr,
        )
        failed_ids.append(f"<discovery-failed:{source_hint}>")
    return declarations, failed_ids


def _build_and_persist_receipt(repo_root: Path) -> None:
    """C4's run-end recording leg: read the resolution journal, derive
    receipt entries for every writer whose declaration this run can fully
    resolve, and persist the result.

    Coverage rule (the load-bearing part -- see the design note's negative
    spec): a writer whose declaration carries ONLY `StaticClause` entries
    (including a declared-empty `clauses=()`) derives directly, with no
    journal row needed -- there is nothing runtime-resolved to pin down, so
    such a writer is never marked unreported for lack of a journal entry.
    A writer carrying at least one `ShapedClause` is marked unreported
    UNLESS the journal carries a resolution for every one of that writer's
    shaped-clause indices; a partial journal (some but not all shaped
    clauses resolved) is treated the same as no journal at all -- this
    function never asks `build_receipt`/`derive_receipt_entries` to derive
    from a partially-resolved writer, since that would either raise
    (`UnresolvedShapedClauseError`) or (if it silently supplied `None` for
    the missing indices) misreport a partial writer as fully covered.

    Never raises -- this is a recording leg, not a required install phase
    (see this function's sole call site in `_run_body`).
    """
    from coordinator_core.install.receipt import build_receipt, persist_receipt
    from coordinator_core.install.resolution_journal import read_journal

    journal = read_journal()
    declarations, discovery_failed_ids = _collect_writer_declarations(repo_root)

    derivations = []
    unreported: List[str] = list(discovery_failed_ids)
    for writer_id, decl in declarations.items():
        shaped_indices = [i for i, c in enumerate(decl.clauses) if isinstance(c, ShapedClause)]
        if not shaped_indices:
            derivations.append((decl, None))
            continue
        resolutions = journal.get(writer_id)
        if resolutions is None or any(i not in resolutions for i in shaped_indices):
            unreported.append(writer_id)
            continue
        derivations.append((decl, resolutions))

    receipt = build_receipt(derivations, unreported_writer_ids=unreported)
    persist_receipt(receipt)


@contextlib.contextmanager
def _environ_patched(env: Dict[str, str]):
    """Temporarily overlay ``env`` onto ``os.environ`` for an in-process phase.

    The orchestrator builds a per-install ``env`` dict (CLAUDE_PLUGIN_ROOT,
    REPO_DOE_CLAUDE, CHECK_ONLY, PATH, ...) that subprocess phases receive via
    ``env=``. In-process phases read ``os.environ`` directly, so a phase ported
    off a subprocess must see the same variables or its resolution order
    silently changes. Restores the prior environment on exit, including
    removing keys that did not exist before.

    Thin alias over ``coordinator_core.install._shared.env_overlay`` (2026-07-21):
    ``first_run`` needed the identical scoping primitive, so the implementation
    moved to the shared module rather than being copied. The name is kept for the
    existing call sites.
    """
    with _env_overlay(env):
        yield


def _compileall_interpreters() -> List[str]:
    """Resolve the interpreter(s) that actually execute the shipped bins.

    ``.pyc`` caches are per-interpreter-version and live in ``__pycache__``
    beside the source -- precompiling under only the venv interpreter would
    miss the interpreter that matters most: the coordinator bins resolve a
    bare ``python3``/``python`` off PATH on Unix and never touch the venv at
    all. Returns base-python first (if resolvable), then the venv python when
    it exists and differs from base-python -- deduped, in precompile order.
    """
    from coordinator_core._settings_home import settings_home
    from coordinator_core.install.ensure_venv import _resolve_base_python, venv_python_path

    interpreters: List[str] = []
    base_py = _resolve_base_python()
    if base_py:
        interpreters.append(base_py)
    venv_py = venv_python_path(settings_home() / ".coordinator-venv")
    if venv_py.exists() and str(venv_py) not in interpreters:
        interpreters.append(str(venv_py))
    return interpreters


def _run_compileall(interp: str, pkg_root: Path) -> subprocess.CompletedProcess:
    """One ``compileall -q`` invocation under a single interpreter.

    Deliberate isolation boundary, not a candidate for an in-process
    import — ``interp`` is the resolved install/target interpreter, which
    is by construction a different interpreter than the one running this
    module; ``compileall`` must run under the target interpreter to
    byte-compile for that interpreter's own bytecode magic number. See
    ``state/audits/2026-08-06-self-spawn-isolation-boundary-classification.md``
    for the recorded verdict."""
    return subprocess.run(
        [interp, "-m", "compileall", "-q", str(pkg_root)],
        capture_output=True,
        text=True,
        timeout=120,
        **no_console_creationflags(),
    )


def _claude_home_cli_argv(*args: str) -> List[str]:
    """Resolve an executable argv for the ``claude-home`` helper.

    A bare ``"claude-home"`` fails on Windows with WinError 2 (CreateProcess
    does not consult PATHEXT). Probe known install locations for the
    delivered ``.cmd`` first, then PATH (``shutil.which`` DOES honour
    PATHEXT), then fall back to the bare name. The POSIX rungs below were
    originally ported verbatim from the register-coordinator-mirror.sh
    trampoline's own helper, but have since diverged (see the POSIX
    paragraph below); the Windows branch's shape was never drawn from that
    helper and is unaffected by the divergence.

    Settings-home first (DR-210 Amendment 2026-07-24: "resolves nothing
    through ~/.claude/bin") — this probe previously tried the retired compat
    mirror's ``.cmd`` BEFORE settings-home's, an inverted precedence on the
    platform that matters most (Windows is the primary machine). Swapped so
    settings-home wins whenever both candidates exist; the mirror candidate
    is retained, tried last.

    POSIX branch mirrors the same settings-home-first shape: a bare
    ``"claude-home"`` PATH lookup is order-dependent on whatever the
    invoking process's PATH happens to contain, which can resolve to the
    retired mirror ahead of settings-home (see
    state/audits/2026-07-25-claude-bin-mirror-read-rungs.md § 2, this
    function's row). Probe settings-home by explicit path first, then PATH,
    and the retired mirror only after both; the bareword is the final rung.
    This ladder differs from ``bin/claude-klabauter-doctor-probe.py::_resolve_machine_local``
    (the in-family model), which is PATH-first, then settings-home, then the
    mirror — the opposite order for the first two rungs. Both orders satisfy
    "mirror last," which is the invariant DR-210 cares about, but the two
    functions are not drop-in equivalent and should not be assumed so by a
    future consolidation.

    Negative spec, BOTH branches: the mirror does not go ahead of
    ``shutil.which``. Probing it earlier would let a retired directory
    outrank the operator's own PATH, which is the precedence the audit
    exists to remove, not to relocate.

    The Windows branch carried that inversion until 2026-08-15 and also
    ignored ``COORDINATOR_SETTINGS_HOME`` outright, deriving its
    settings-home candidate from the home directory alone — so a relocated
    settings home found no candidate and fell through to the retired
    mirror. Both are fixed; the two branches now run the same ladder.

    Negative spec: this helper is NOT the interactive launch chain, and an
    earlier pass deferred the Windows fix on the mistaken belief that it
    was. Its only caller runs ``subprocess.run(..., capture_output=True)``
    for a ``claude-home plugins`` query. The console-input-mode defect that
    makes launch-chain depth load-bearing belongs to ``claude-doe`` /
    ``claude.exe`` (see ``93089e568``), a different artifact reached
    through a different function. Do not import that caution here.
    """
    if os.name == "nt":
        home = (
            os.environ.get("CLAUDE_HOME")
            or os.environ.get("HOME")
            or os.environ.get("USERPROFILE")
            or os.path.expanduser("~")
        )
        settings_home_cand = os.path.join(
            os.environ.get("COORDINATOR_SETTINGS_HOME")
            or os.path.join(home, ".coordinator-claude-settings"),
            "bin",
            "claude-home.cmd",
        )
        if os.path.isfile(settings_home_cand):
            return [settings_home_cand, *args]
        found = shutil.which("claude-home")
        if found:
            return [found, *args]
        mirror_cand = os.path.join(home, ".claude", "bin", "claude-home.cmd")
        if os.path.isfile(mirror_cand):
            return [mirror_cand, *args]
        return ["claude-home", *args]

    home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or os.path.expanduser("~")
    settings_home_cand = os.path.join(
        os.environ.get("COORDINATOR_SETTINGS_HOME")
        or os.path.join(home, ".coordinator-claude-settings"),
        "bin",
        "claude-home",
    )
    if os.path.isfile(settings_home_cand):
        return [settings_home_cand, *args]
    found = shutil.which("claude-home")
    if found:
        return [found, *args]
    mirror_cand = os.path.join(home, ".claude", "bin", "claude-home")
    if os.path.isfile(mirror_cand):
        return [mirror_cand, *args]
    return ["claude-home", *args]


def _resolve_coordinator_live_path() -> str:
    """DoE-local "coordinator live path" fact -- the one piece of genuinely
    DoE-owned resolution logic register-coordinator-mirror.sh's trampoline
    used to perform before handing off to the (already claude-klabauter-native)
    ``coordinator_core.ops.register_coordinator_mirror`` engine module via
    ``--live-path``. Reproduced here (not duplicated engine-side) now that
    this phase calls that engine module in-process instead of spawning the
    trampoline:

    Tier 1: ``coordinator_core.resolve_coordinator_clone.resolve_content_root()``
    -- the native peer of ``resolve-coordinator-clone.sh --content-root``
    (``--for-content`` is a retained legacy alias, identical resolution
    ladder). DR-079 (2026-07-21) repoints this tier from a script-relative
    bash spawn to a direct in-process call now that the native port exists;
    the DoE bash oracle remains the source of truth this port mirrors, but
    is no longer subprocess-invoked here.
    Tier 2 (defensive fallback, native resolver raises):
    ``claude-home plugins`` + flat-layout join.

    Returns "" on unresolvable failure (both tiers exhausted); the caller
    treats that as a fatal phase result, matching the trampoline's own
    ``sys.exit(1)`` on the identical condition.
    """
    coordinator_live = ""
    try:
        from coordinator_core.resolve_coordinator_clone import (
            ResolveCoordinatorCloneError,
            resolve_content_root,
        )

        coordinator_live = resolve_content_root()
    except ResolveCoordinatorCloneError:
        coordinator_live = ""

    if not coordinator_live:
        try:
            plugins_result = subprocess.run(
                _claude_home_cli_argv("plugins"),
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=_SUBPROCESS_TIMEOUT,
                **no_console_creationflags(),
                check=True,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            print(
                f"register-coordinator-mirror: failed to resolve coordinator live path: {exc}",
                file=sys.stderr,
            )
            return ""
        coordinator_live = os.path.join(
            plugins_result.stdout.strip(), "coordinator-claude", "coordinator"
        )

    return coordinator_live


def _is_windows_host() -> bool:
    if os.environ.get("OS") == "Windows_NT":
        return True
    try:
        result = subprocess.run(
            ["uname", "-s"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
            **no_console_creationflags(),
        )
        uname = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        # uname absent is routine on Windows (no POSIX uname on PATH); the
        # OS env var check above already covers the common Windows case, so
        # an empty fallback here (-> not a recognized POSIX-ish uname) is
        # expected, not a diagnostic-worthy failure.
        uname = ""
    return uname.startswith(("MINGW", "MSYS", "CYGWIN", "Windows"))


class _Orchestrator:
    """Owns PHASE_NUM/FAILED mutable state -- mirrors the bash oracle's globals."""

    def __init__(self) -> None:
        self.phase_num = 0
        self.failed = False
        # F9: every skip_note() call also appends here so the final summary
        # can list every phase that did nothing, even if its inline notice
        # scrolled off-screen or was interleaved with a noisy stdout/stderr
        # child process elsewhere in the run.
        self.skipped: List[str] = []

    def phase_header(self, desc: str) -> None:
        self.phase_num += 1
        print()
        print(f"=== [{self.phase_num}] {desc} ===")

    def run_required(
        self, desc: str, cmd: Sequence[str], env: Optional[Dict[str, str]] = None
    ) -> None:
        """Fail loud: exits the process on non-zero, matching the bash oracle's
        `run_required` (which calls `exit "$rc"` from inside the helper)."""
        self.phase_header(desc)
        rc = _run(cmd, env=env)
        if rc == 0:
            return
        print(file=sys.stderr)
        print(f"FATAL: phase '{desc}' failed (exit {rc}): {' '.join(cmd)}", file=sys.stderr)
        print("  install-maximalist.sh stops here -- fix the reported error and re-run.", file=sys.stderr)
        print("  Re-running is safe: every phase is idempotent.", file=sys.stderr)
        sys.exit(rc)

    def run_advisory(
        self, desc: str, cmd: Sequence[str], env: Optional[Dict[str, str]] = None
    ) -> None:
        """Log failure but continue -- matches install.md's non-halting contract
        for Step 6/Step 7."""
        self.phase_header(desc)
        rc = _run(cmd, env=env)
        if rc != 0:
            print(
                f"WARN: phase '{desc}' failed (exit {rc}) -- continuing (advisory, not fatal): {' '.join(cmd)}",
                file=sys.stderr,
            )
            self.failed = True

    def run_required_py(
        self,
        desc: str,
        fn: Callable[[List[str]], int],
        argv: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """In-process analogue of ``run_required`` for phases now called as a
        plain Python function instead of a ``["bash", ...]`` subprocess --
        fail-loud on non-zero, identical FATAL messaging and exit behavior.

        Review: code-reviewer (Lane B install F1) -- the subprocess model this
        replaces gave the orchestrator an implicit guarantee for free: a
        spawned script's own crash only ever surfaced as a returncode, never
        a raised exception. Wrap the in-process call so an unexpected
        exception from `fn` converts to the same FATAL messaging path instead
        of propagating a raw traceback out of the whole install chain."""
        self.phase_header(desc)
        try:
            if env is not None:
                with _environ_patched(env):
                    rc = fn(argv)
            else:
                rc = fn(argv)
        except Exception as exc:  # noqa: BLE001 -- restores subprocess-boundary crash-never-propagates guarantee
            print(file=sys.stderr)
            print(f"FATAL: phase '{desc}' raised an unexpected exception: {exc}", file=sys.stderr)
            print("  install-maximalist.sh stops here -- fix the reported error and re-run.", file=sys.stderr)
            print("  Re-running is safe: every phase is idempotent.", file=sys.stderr)
            sys.exit(1)
        if rc == 0:
            return
        print(file=sys.stderr)
        print(f"FATAL: phase '{desc}' failed (exit {rc})", file=sys.stderr)
        print("  install-maximalist.sh stops here -- fix the reported error and re-run.", file=sys.stderr)
        print("  Re-running is safe: every phase is idempotent.", file=sys.stderr)
        sys.exit(rc)

    def run_advisory_py(
        self,
        desc: str,
        fn: Callable[[List[str]], int],
        argv: List[str],
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        """In-process analogue of ``run_advisory``: log failure but continue.

        Review: code-reviewer (Lane B install F1) -- see `run_required_py`'s
        docstring; the same unguarded-exception exposure applies here, except
        advisory phases must never abort the chain, so an unexpected
        exception is logged and treated as an advisory failure, not a
        `sys.exit`."""
        self.phase_header(desc)
        try:
            if env is not None:
                with _environ_patched(env):
                    rc = fn(argv)
            else:
                rc = fn(argv)
        except Exception as exc:  # noqa: BLE001 -- advisory phase must never abort orchestration
            print(
                f"WARN: phase '{desc}' raised an unexpected exception: {exc} -- continuing (advisory, not fatal)",
                file=sys.stderr,
            )
            self.failed = True
            return
        if rc != 0:
            print(
                f"WARN: phase '{desc}' failed (exit {rc}) -- continuing (advisory, not fatal)",
                file=sys.stderr,
            )
            self.failed = True

    def skip_note(self, msg: str) -> None:
        print()
        print(f"--- SKIPPED: {msg} ---")
        self.skipped.append(msg)


def _defender_offer(check_only: bool, non_interactive: bool, orch: _Orchestrator) -> None:
    """Phase 3 Step 1c -- Windows Defender process-exclusion offer.

    Windows-only, admin-gated, operator-consented, default-DECLINED. Faithful
    port of the bash oracle's PowerShell-driven admin check + consent prompt.
    """
    orch.phase_header("Windows Defender process-exclusion offer (Phase 3 Step 1c -- declinable)")

    if not _is_windows_host():
        orch.skip_note("Defender process-exclusion offer -- non-Windows host")
        return

    ps = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not ps:
        orch.skip_note("Defender process-exclusion offer -- no powershell.exe/pwsh on PATH")
        return

    def _powershell(command: str) -> str:
        try:
            result = subprocess.run(
                [ps, "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                **no_console_creationflags(),
            )
            return result.stdout.replace("\r", "").strip()
        except (OSError, subprocess.TimeoutExpired):
            # Falls through to the is_admin != "True" skip-note below, which
            # is already user-visible -- no separate diagnostic needed.
            return ""

    is_admin = _powershell(
        "[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())"
        ".IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"
    )
    if is_admin != "True":
        orch.skip_note(
            "Defender process-exclusion offer -- not running elevated "
            "(re-run from an elevated/Run-as-Administrator shell to see this offer)"
        )
        return

    print("[setup] Defender real-time scanning re-scans every spawned coordinator")
    print("[setup]   interpreter process; excluding them cuts spawn latency (measured")
    print("[setup]   bash p90 285ms -> 19.5ms) but a COMPROMISED interpreter would then")
    print("[setup]   run unscanned. Never applied without explicit consent.")

    targets: List[str] = []
    for tool in ("bash", "git", "sh"):
        tool_path = shutil.which(tool)
        if not tool_path:
            continue
        cygpath = shutil.which("cygpath")
        if cygpath:
            try:
                result = subprocess.run(
                    [cygpath, "-w", tool_path],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=10,
                    **no_console_creationflags(),
                )
                if result.stdout.strip():
                    tool_path = result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass  # best-effort Windows-path translation; falls back to the original tool_path
        targets.append(tool_path)

    if not targets:
        print("[setup]   No coordinator toolchain binaries resolved on PATH -- nothing to exclude.")
        return

    print("[setup]   Would exclude:")
    for t in targets:
        print(f"[setup]     - {t}")

    if check_only:
        orch.skip_note("Defender exclusion apply -- check-only (would prompt for consent above)")
        return
    if non_interactive or not sys.stdin.isatty():
        print("[setup]   Non-interactive/unattended context -- default is DECLINED, no exclusions applied.")
        print("[setup]   Re-run interactively from an elevated shell to opt in.")
        return

    try:
        consent = input(f"[setup] Add Defender process exclusions for these {len(targets)} binaries? [y/N] ")
    except EOFError:
        consent = ""
    if not consent.strip().lower().startswith("y"):
        print("[setup]   Declined -- no exclusions applied.")
        return

    for t in targets:
        rc = _run(
            [ps, "-NoProfile", "-WindowStyle", "Hidden", "-Command", "Add-MpPreference -ExclusionProcess $env:EXCL_PATH"],
            env={**os.environ, "EXCL_PATH": t},
        )
        if rc == 0:
            print(f"[setup]   Excluded: {t}")
        else:
            print(f"WARN: failed to add Defender exclusion for {t} (non-fatal)", file=sys.stderr)
            orch.failed = True


def _install_claude_doe_wrapper(
    coord_root: str,
    claude_home_dir: str,
    check_only: bool,
    orch: _Orchestrator,
    claude_klabauter_root: str,
    settings_bin: str,
) -> None:
    """Step 3.5b -- make the claude-doe wrapper reachable at ``~/.local/bin/claude-doe``.
    Pure-Python inline (the bash oracle does this inline too, not via a sub-script).

    ``wrapper_src`` is deliberately NOT derived from ``coord_root`` (the
    resolved DoE-clone ``coordinator/`` dir) -- the executable ``bin/``
    surface migrated wholesale to claude-klabauter in commit ``b644d5a9``
    (2026-07-22), so ``claude-doe`` now lives at
    ``<claude_klabauter_root>/coordinator/bin/claude-doe.py``, not under the DoE clone.
    ``coord_root`` still correctly houses ``templates/`` (DoE doctrine
    content, untouched by that migration) for the sibling shim/launcher
    generators, so this split is DR-047's contract/engine boundary, not an
    inconsistency. ``claude_klabauter_root`` is an explicit param (not a
    ``Path(__file__)``-derived constant) so tests can redirect it to an
    isolated fixture tree instead of this module's real on-disk location.

    POSIX (C2): ``~/.local/bin/claude-doe`` is a SYMLINK onto
    ``<settings_bin>/claude-doe`` -- the shim ``_install_bin_resolvers``
    (``substrate.py``, invoked earlier in this same install pass at Phase 3
    Step 1) generates by re-execing this same ``wrapper_src`` -- rather than
    a second ``shutil.copy2`` of the source binary. Two delivery mechanisms
    installing two independent copies of the same CLI under two names can
    drift (one four-days-stale in production the day this was found: a
    Jul-21 bash build at ``~/.local/bin/claude-doe`` alongside a Jul-24
    Python port resolved by the settings-home shim); a symlink makes that
    staleness structurally impossible -- ``~/.local/bin/claude-doe`` always
    resolves to whatever the shim loop last wrote, because it IS that
    artifact under a second name. ``settings_bin`` is passed in by the
    caller (computed once in ``_run_body``, the same value used for the F2
    PATH prepend) rather than re-resolved here, so this function has exactly
    one source of truth for the settings-home ``bin/`` directory.

    Windows has no equivalent-cost native symlink story -- ``os.symlink``
    there requires either elevated (Administrator) privileges or Developer
    Mode enabled by default, neither of which this installer can assume or
    silently request -- so it keeps a ``shutil.copy2`` install of the
    wrapper bytes -- ``settings_bin`` is accepted but unused on that
    branch. The copy itself, however, is staged to a same-directory temp
    path and published with ``os.replace`` rather than written in place:
    an in-place ``shutil.copy2`` onto ``wrapper_dst`` truncates-then-writes
    the destination, and a concurrent shell holding that path open (e.g.
    the very shim this install pass is re-publishing) can observe or
    execute a half-written file mid-copy. ``os.replace`` on Windows is
    ``MoveFileExW`` with ``MOVEFILE_REPLACE_EXISTING``, atomic for a
    same-volume rename the way POSIX ``rename(2)`` is; deriving the temp
    path from ``local_bin`` (the destination's own directory) rather than
    the system temp dir keeps it on the same volume so ``os.replace``
    stays a true rename instead of degrading to a non-atomic copy+delete
    across volumes. A sharing violation on the replace itself (destination
    open with a deny-write share mode) is surfaced as a loud install
    failure, matching this function's existing FATAL-and-exit pattern --
    silently leaving a half-published wrapper is not an acceptable
    degradation.

    Negative-spec: do NOT resolve the symlink target via PATH lookup or via
    ``coordinator_core._settings_home.settings_home()`` -- both would
    re-derive a value the caller already computed and risk drifting from the
    F2 PATH-prepend's own resolution the moment ``COORDINATOR_SETTINGS_HOME``
    is overridden.
    """
    orch.phase_header("claude-doe wrapper install (Step 3.5b -- ~/.local/bin/claude-doe)")
    wrapper_src = os.path.join(claude_klabauter_root, "coordinator", "bin", "claude-doe.py")
    wrapper_dst = os.path.join(claude_home_dir, ".local", "bin", "claude-doe")
    local_bin = os.path.dirname(wrapper_dst)

    if not os.path.isfile(wrapper_src):
        print(f"FATAL: expected wrapper source not found: {wrapper_src}", file=sys.stderr)
        sys.exit(1)

    if os.name == "nt":
        if check_only:
            if os.path.isfile(wrapper_dst):
                print(f"claude_doe_wrapper: ready ({wrapper_dst})")
            else:
                print(f"claude_doe_wrapper: would install ({wrapper_dst})")
            return

        os.makedirs(local_bin, exist_ok=True)
        # Stage the copy at a same-directory temp path, then publish with
        # `os.replace` rather than `shutil.copy2` writing `wrapper_dst` in
        # place -- an in-place copy truncates-then-writes the destination,
        # and a concurrent shell holding that path open can observe or
        # execute a half-written file mid-copy (the same defect class the
        # POSIX branch above already guards against via its own
        # tmp-then-`os.replace` publish). The temp path is derived from
        # `local_bin` (the destination's own directory), not the system
        # temp dir, so it stays on the same volume and `os.replace` is a
        # true atomic rename rather than a non-atomic cross-volume
        # copy+delete.
        tmp_dst = os.path.join(local_bin, f"claude-doe.tmp-{os.getpid()}")
        try:
            # A5: preserve/force exec bits explicitly -- shutil.copy2 preserves mode
            # from source (mirrors `cp -p`), but the trailing chmod belt-and-braces
            # guards the case where the source itself lacks +x for some reason,
            # matching the oracle's unconditional `chmod +x` after `cp -p`.
            shutil.copy2(wrapper_src, tmp_dst)
            st = os.stat(tmp_dst)
            os.chmod(tmp_dst, st.st_mode | 0o111)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_dst)
            print(f"FATAL: failed to install claude-doe wrapper to {wrapper_dst}: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            os.replace(tmp_dst, wrapper_dst)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_dst)
            print(f"FATAL: failed to install claude-doe wrapper to {wrapper_dst}: {exc}", file=sys.stderr)
            sys.exit(1)

        print(f"claude_doe_wrapper: installed ({wrapper_dst})")
        path_env = os.environ.get("PATH", "")
        if f":{local_bin}:" not in f":{path_env}:":
            print(f"  NOTE: {local_bin} is not yet on PATH -- add to your login rc for new terminals:")
            print('    export PATH="$HOME/.local/bin:$PATH"   # e.g. append to ~/.zprofile')
        return

    link_target = os.path.join(settings_bin, "claude-doe")

    if check_only:
        if os.path.islink(wrapper_dst) and os.readlink(wrapper_dst) == link_target:
            print(f"claude_doe_wrapper: ready (symlink {wrapper_dst} -> {link_target})")
        else:
            print(f"claude_doe_wrapper: would symlink {wrapper_dst} -> {link_target}")
        return

    os.makedirs(local_bin, exist_ok=True)
    already_correct = os.path.islink(wrapper_dst) and os.readlink(wrapper_dst) == link_target
    if not already_correct:
        # Review: code-reviewer (Finding 1) -- build the new link at a temp
        # sibling path and `os.replace()` it onto wrapper_dst, rather than
        # `unlink` then `symlink` in two separate syscalls. The unlink-then-
        # symlink shape has a window where, if the symlink call itself raises
        # (read-only mount, permissions, disk full), wrapper_dst is left
        # completely ABSENT -- strictly worse than the stale-but-present
        # state this function exists to repair, and worse than a no-op
        # failure. `os.replace()` is POSIX rename(2), which atomically
        # replaces an existing symlink/regular file with the new symlink, so
        # there is never a window with no claude-doe at all; the prior
        # covers-all-four-non-terminal-states comment about `os.path.lexists`
        # no longer applies -- `os.replace` overwrites absent, stale-file,
        # broken-symlink, and wrong-target destinations uniformly, with no
        # pre-check needed.
        tmp_dst = f"{wrapper_dst}.tmp-{os.getpid()}"
        try:
            os.symlink(link_target, tmp_dst)
        except OSError as exc:
            print(f"FATAL: failed to symlink claude-doe wrapper to {wrapper_dst}: {exc}", file=sys.stderr)
            sys.exit(1)
        try:
            os.replace(tmp_dst, wrapper_dst)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_dst)
            print(f"FATAL: failed to symlink claude-doe wrapper to {wrapper_dst}: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"claude_doe_wrapper: linked ({wrapper_dst} -> {link_target})")
    else:
        print(f"claude_doe_wrapper: already linked ({wrapper_dst} -> {link_target})")

    path_env = os.environ.get("PATH", "")
    if f":{local_bin}:" not in f":{path_env}:":
        print(f"  NOTE: {local_bin} is not yet on PATH -- add to your login rc for new terminals:")
        print('    export PATH="$HOME/.local/bin:$PATH"   # e.g. append to ~/.zprofile')


def run(
    check_only: bool,
    non_interactive: bool,
    coord_root: str,
    doe_clone: str,
    claude_klabauter_root: str,
    claude_home_dir: Optional[str] = None,
) -> int:
    """Core orchestration entry -- callable directly (e.g. from tests) without
    going through argv parsing. ALWAYS returns an int exit code, even though
    internally `run_required` phases (and the claude-doe wrapper's missing-
    source guard) signal failure via `sys.exit`, matching the bash oracle's
    own `exit "$rc"` inside the `run_required` helper -- that SystemExit is
    caught at the bottom of this function and converted to a return value so
    callers (including tests) never need to catch SystemExit themselves.

    Env scoping (2026-07-21): the whole orchestration runs under an empty
    ``_env_overlay``, so every ``os.environ`` write any phase makes is unwound
    when ``run()`` returns. The load-bearing one is ``_run_body``'s F2 PATH
    prepend, which is NOT deletable -- the in-process phases that follow it
    resolve the just-installed bins off ``os.environ["PATH"]``. As a bash script
    that export died with the process; as an imported module it persisted for the
    interpreter's life and was inherited by every later subprocess child, which
    under pytest meant one install test repointed PATH for the rest of the
    session. Scoping at this seam rather than around the single assignment keeps
    the phases' view of PATH intact (they still see it) while bounding the write
    to the run, and covers any phase that writes env without this fix noticing.

    ``claude_klabauter_root`` is REQUIRED (not defaulted here) -- the phases it feeds
    (``claude-doe`` wrapper install, ``gen-claude-klabauter-root-pointer.py``) resolve
    real subprocess/file-copy targets, so silently defaulting to
    ``Path(__file__).resolve().parents[2]`` inside this function would make
    every direct-call test (this module's own coverage in
    ``test_maximalist.py``) transparently fall through to the REAL machine's
    claude-klabauter checkout and machine-local registry the moment a test omitted the
    kwarg -- a live-state-mutation hazard in test runs, not just a stale
    default. ``main()`` (the CLI entry point) is the one caller that computes
    the real-install default and passes it in explicitly; tests pass their
    own isolated fixture path instead.
    """
    with _env_overlay({}):
        try:
            return _run_body(check_only, non_interactive, coord_root, doe_clone, claude_home_dir, claude_klabauter_root)
        except SystemExit as exc:
            return int(exc.code) if isinstance(exc.code, int) else 1


def _run_body(
    check_only: bool,
    non_interactive: bool,
    coord_root: str,
    doe_clone: str,
    claude_home_dir: Optional[str],
    claude_klabauter_root: str,
) -> int:
    # Root of the home-resolution seam: three downstream consumers derive real
    # filesystem targets from this one value (the settings-home/PATH prepend,
    # coordinator-identity.yaml, and the ~/.local/bin/claude-doe wrapper
    # destination), so a "" here silently anchors all three at the filesystem
    # root rather than failing. `main()` — the real-install entry point — never
    # passes claude_home_dir, so the env ladder below IS the production path.
    # It delegates to the ONE canonical resolver (CLAUDE_HOME -> HOME ->
    # USERPROFILE, empty treated as unset, non-absolute rejected) rather than
    # re-deriving a fourth variant; the missing USERPROFILE rung is why a
    # native HOME-less Windows shell resolved "" here.
    if not claude_home_dir:
        try:
            claude_home_dir = require_home("install-maximalist")
        except RequireHomeError as exc:
            print(f"FATAL: {exc}", file=sys.stderr)
            return 1

    # Root write-side seam for the DoE clone path form. The DoE-side trampoline
    # resolves this under Git-Bash on Windows, where `pwd` yields the MSYS mount
    # form (`/x/DoE-claude`); handed on verbatim it is re-read by native-Windows
    # node / py.exe consumers as drive-relative `X:\x\DoE-claude` (doubled
    # drive), which is how `repos.doe_claude` and the `.doe-root` pointer came to
    # hold an unresolvable path. Normalizing once HERE covers every downstream
    # derivation in one place -- the REPO_DOE_CLAUDE env overlay handed to child
    # phases, the `.coordinator-dev-repo` sentinel probe, and the
    # `machine-local set repos.doe_claude` seed that gen_doe_root_pointer later
    # reads. No-op off Windows and on already-native paths.
    doe_clone = native_path_form(doe_clone)

    env = dict(os.environ)
    env["CLAUDE_PLUGIN_ROOT"] = coord_root
    env["REPO_DOE_CLAUDE"] = doe_clone
    env["CHECK_ONLY"] = "1" if check_only else ""
    if check_only or non_interactive:
        env["COORDINATOR_NON_INTERACTIVE"] = "1"

    sentinel = os.path.join(doe_clone, ".coordinator-dev-repo")
    if os.path.isfile(sentinel):
        env.setdefault("COORDINATOR_PLUGIN_ROOT_TRUSTED", "1")

    # -- Resolution journal: clear at run start, propagate the run-scoped
    # path so every child phase (subprocess AND in-process) inherits the
    # SAME journal `record_resolution` calls append to.
    # C4 of docs/research/2026-08-06-install-receipt-persistence-design.md.
    # `env` (used by every `_run`/`run_required_py`/`run_advisory_py` call
    # below) is where the subprocess phases pick this up; `os.environ` is
    # additionally set so `_environ_patched(env)` overlays it for in-process
    # phases exactly like every other env key this orchestrator threads
    # through (matches the F2 PATH-prepend precedent above). Recording-leg
    # setup only -- must never abort the install (negative spec: a
    # bookkeeping failure is a worse defect than the missing receipt this
    # chunk exists to fix).
    try:
        from coordinator_core.install.resolution_journal import (
            RESOLUTION_JOURNAL_ENV_VAR,
            _journal_path,
            clear_journal,
        )

        clear_journal()
        journal_path = str(_journal_path())
        env[RESOLUTION_JOURNAL_ENV_VAR] = journal_path
    except Exception as exc:  # noqa: BLE001 -- recording leg must never break the install
        print(
            f"WARN: resolution-journal setup failed -- continuing without install-receipt "
            f"recording this run: {exc}",
            file=sys.stderr,
        )

    orch = _Orchestrator()

    print("install-maximalist.sh -- cold maximalist install orchestrator")
    print(f"  Coordinator root : {coord_root}")
    print(f"  DoE clone root   : {doe_clone}")
    print(f"  Mode             : {'--check-only (read-only)' if check_only else 'live (mutating)'}")
    print(f"  Interactive      : {'no (--non-interactive)' if non_interactive else 'yes'}")

    # -- Phase A -- structural fork classification (informational; read-only) --
    # Retired the ["bash", detect-existing-claude-home.sh] spawn (C13): that
    # DoE-side script was only a thin polyglot trampoline back into THIS
    # repo's coordinator_core.ops.detect_existing_claude_home -- called
    # in-process now, matching Step 3.5c's precedent (DR-059).
    orch.phase_header("Detect existing Claude home state (informational)")
    from coordinator_core.ops.detect_existing_claude_home import (  # local import: avoid import cost on --help
        main as _detect_existing_claude_home_main,
    )

    try:
        with _environ_patched(env):
            rc = _detect_existing_claude_home_main([])
    except Exception as exc:  # noqa: BLE001 -- informational-only phase must never abort the install chain
        print(f"WARN: detect-existing-claude-home raised an unexpected exception: {exc} -- continuing", file=sys.stderr)
    else:
        if rc != 0:
            print("WARN: detect-existing-claude-home reported non-zero -- continuing", file=sys.stderr)

    # -- Phase B -- operator identity (Phase 2 in install.md) -- human-in-the-loop --
    identity_file = os.path.join(claude_home_dir, ".claude", "coordinator-identity.yaml")
    orch.phase_header("Operator identity (Phase 2 -- read-only check)")
    if os.path.isfile(identity_file):
        print(f"operator_identity: ready ({identity_file})")
    else:
        print("operator_identity: missing")
        orch.skip_note(
            "Operator identity capture, working-repos discovery, and CLAUDE.local.md "
            "render (Phase 2) -- these need an interactive AskUserQuestion prompt. "
            "Run /coordinator:install (or 'walk me through the coordinator') to complete this step."
        )

    # -- Phase 3 Step 1 -- install-substrate (in-process; see module docstring) --
    orch.phase_header("install-substrate (Phase 3 Step 1 -- machine-local substrate)")
    from coordinator_core.install import substrate as _substrate  # local import: avoid import cost on --help

    try:
        substrate_rc = _substrate.run(setup_only=False, check_only=check_only)
    except _substrate.SubstrateFatalError as exc:
        print(str(exc), file=sys.stderr)
        substrate_rc = 1
    if substrate_rc != 0:
        print(file=sys.stderr)
        print(
            f"FATAL: phase 'install-substrate (Phase 3 Step 1 -- machine-local substrate)' "
            f"failed (exit {substrate_rc})",
            file=sys.stderr,
        )
        print("  install-maximalist.sh stops here -- fix the reported error and re-run.", file=sys.stderr)
        print("  Re-running is safe: every phase is idempotent.", file=sys.stderr)
        return substrate_rc

    # -- F2 -- prepend the just-installed bin dirs to THIS process's PATH --
    settings_bin = os.path.join(
        env.get("COORDINATOR_SETTINGS_HOME") or os.path.join(claude_home_dir, ".coordinator-claude-settings"),
        "bin",
    )
    claude_bin = os.path.join(claude_home_dir, ".claude", "bin")
    path_parts = env.get("PATH", "").split(os.pathsep)
    for b in (claude_bin, settings_bin):
        if b not in path_parts:
            path_parts.insert(0, b)
    env["PATH"] = os.pathsep.join(path_parts)
    # Deliberate process-env write: the in-process phases below (install-health-run,
    # doctor, ...) resolve the just-installed bins off os.environ["PATH"], so this
    # cannot be dropped. It is bounded by the empty `_env_overlay` wrapping the whole
    # orchestration in `run()` above -- see that docstring's env-scoping note.
    os.environ["PATH"] = env["PATH"]

    # -- Phase 3 Step 1b -- install-health-run --
    # Retired the ["bash", install-health-run.sh] spawn (C13): that DoE-side
    # script was only a thin polyglot trampoline back into THIS repo's
    # coordinator_core.ops.install_health_run -- called in-process now. The
    # orchestrator's own OWN sub-scripts (bin/install-health/*.sh drop-ins)
    # remain bash and are still subprocess-delegated BY that module -- out of
    # C13's scope (a genuinely DoE/plugin-owned drop-in surface, not a
    # trampoline back into this package).
    from coordinator_core.ops.install_health_run import (  # local import: avoid import cost on --help
        main as _install_health_run_main,
    )

    orch.run_required_py(
        "install-health-run (Phase 3 Step 1b -- install-health drop-ins)",
        _install_health_run_main,
        [],
        env=env,
    )

    # -- Phase 3 Step 1c -- Windows Defender process-exclusion offer --
    _defender_offer(check_only, non_interactive, orch)

    # -- Phase 3 Step 3 (partial) -- best-effort seed repos.doe_claude --
    if check_only:
        current = _registry_get_for_check("repos.doe_claude")
        if current == doe_clone:
            orch.skip_note(f"repos.doe_claude registry key up to date ({doe_clone})")
        else:
            orch.skip_note(f"Seed repos.doe_claude registry key -- check-only (would seed: {doe_clone})")
    else:
        orch.phase_header("Seed repos.doe_claude registry key (best-effort)")
        # Resolve to a concrete argv rather than passing the bare name through to
        # subprocess: shutil.which honours PATHEXT (so it happily finds
        # machine-local.cmd on Windows) but CreateProcess does NOT, so the bare
        # name then fails with WinError 2. Prefer the canonical resolver, which
        # additionally sidesteps shebang-exec via [sys.executable, _machine_local.py].
        from coordinator_core.install._shared import resolve_machine_local_cli

        ml_argv = resolve_machine_local_cli(coord_root)
        if ml_argv is None:
            found = shutil.which("machine-local")
            ml_argv = [found] if found else None
        if ml_argv:
            rc = _run([*ml_argv, "set", "repos.doe_claude", doe_clone], env=env)
            if rc == 0:
                print(f"repos.doe_claude: seeded ({doe_clone})")
            else:
                print(
                    "WARN: machine-local set repos.doe_claude failed -- REPO_DOE_CLAUDE env override still in effect for this run",
                    file=sys.stderr,
                )
        else:
            print(
                "NOTE: machine-local not yet on PATH in this shell -- REPO_DOE_CLAUDE env override covers this run; "
                "open a new shell and re-run to persist the registry key.",
                file=sys.stderr,
            )

    # -- Phase 3 Step 3 (partial) -- best-effort seed repos.claude_klabauter --
    # Breaks the claude-klabauter<->coordinator install-order circularity (cross-repo ask
    # 2026-07-22, claude-central-em "Ask 1"): claude-klabauter-first installs advisory-skip
    # this key in scripts/setup.py::register_claude_klabauter_root() because machine-local
    # isn't yet on PATH at that point in ITS OWN chain, and a coordinator-first
    # install never chains back to seed it either -- so the key has only ever
    # existed on this machine because it was hand-set once (2026-07-03). Seeding
    # it here, from the claude-klabauter clone that is running THIS install, closes the
    # loop from the claude-klabauter side without waiting on a coordinator-side fix.
    #
    # This block MUST run before Step 3.5a.1b (gen-claude-klabauter-root-pointer.py) below:
    # that advisory step resolves the claude-klabauter root via REPO_CLAUDE_KLABAUTER or
    # `machine-local get repos.claude_klabauter`, which is exactly the read this
    # write makes possible for the first time on a fresh machine.
    #
    # Path derivation is deliberately NOT coordinator_core.claude_klabauter_root.
    # coordinator_claude_klabauter_root() -- that resolver reads the very registry key
    # this block seeds, so calling it here would just re-enact the circularity.
    # This code runs FROM the claude-klabauter clone (coordinator_core/install/maximalist.py),
    # so the clone root is derived from this file's own location instead.
    claude_klabauter_clone = Path(__file__).resolve().parents[2]
    if check_only:
        current = _registry_get_for_check("repos.claude_klabauter")
        if current == str(claude_klabauter_clone):
            orch.skip_note(f"repos.claude_klabauter registry key up to date ({claude_klabauter_clone})")
        else:
            orch.skip_note(f"Seed repos.claude_klabauter registry key -- check-only (would seed: {claude_klabauter_clone})")
    elif not (claude_klabauter_clone / "coordinator_core").is_dir():
        # Mirrors gen-claude-klabauter-root-pointer.py's own sanity guard: a bare
        # directory-existence check on the wrong marker cannot distinguish
        # "the claude-klabauter clone" from "some directory" (an unrelated 18-entry
        # bin/ sits at this repo's root alongside coordinator/bin's real 563
        # -- see the 2026-07-22 cross-repo ask this block implements). The
        # coordinator_core package dir is a distinctive marker instead.
        print(
            f"WARN: derived claude-klabauter clone root {claude_klabauter_clone} has no coordinator_core/ subdir -- "
            "skipping repos.claude_klabauter seed (best-effort, not fatal)",
            file=sys.stderr,
        )
    else:
        orch.phase_header("Seed repos.claude_klabauter registry key (best-effort)")
        from coordinator_core.install._shared import resolve_machine_local_cli

        ml_argv = resolve_machine_local_cli(coord_root)
        if ml_argv is None:
            found = shutil.which("machine-local")
            ml_argv = [found] if found else None
        if ml_argv:
            rc = _run([*ml_argv, "set", "repos.claude_klabauter", str(claude_klabauter_clone)], env=env)
            if rc == 0:
                print(f"repos.claude_klabauter: seeded ({claude_klabauter_clone})")
            else:
                print(
                    "WARN: machine-local set repos.claude_klabauter failed -- gen-claude-klabauter-root-pointer.py "
                    "(Step 3.5a.1b below) may not resolve this run",
                    file=sys.stderr,
                )
        else:
            print(
                "NOTE: machine-local not yet on PATH in this shell -- repos.claude_klabauter not seeded; "
                "open a new shell and re-run to persist the registry key.",
                file=sys.stderr,
            )

    # -- Step 3.5a.1 -- gen-doe-root-pointer --
    # Retired the ["bash", gen-doe-root-pointer.sh] spawn (C13): that DoE-side
    # script was only a thin polyglot trampoline back into THIS repo's
    # coordinator_core.ops.gen_doe_root_pointer -- called in-process now.
    from coordinator_core.ops.gen_doe_root_pointer import (  # local import: avoid import cost on --help
        main as _gen_doe_root_pointer_main,
    )

    pointer_args = ["--check-only"] if check_only else []
    orch.run_required_py(
        "gen-doe-root-pointer (Step 3.5a.1 -- ~/.claude/.doe-root pointer)",
        _gen_doe_root_pointer_main,
        pointer_args,
        env=env,
    )

    # -- Step 3.5a.1b -- gen-claude-klabauter-root-pointer.py (advisory) --
    # Same migrated-`bin/` bug class as `_install_claude_doe_wrapper`'s
    # `claude-doe` below: this script lives at
    # `<claude_klabauter_root>/coordinator/bin/gen-claude-klabauter-root-pointer.py` post
    # b644d5a9, not under the DoE clone's `coord_root/bin/`.
    py_bin = shutil.which("python3") or shutil.which("python")
    if py_bin:
        claude_klabauter_pointer_args = ["--check-only"] if check_only else []
        orch.run_advisory(
            "gen-claude-klabauter-root-pointer.py (Step 3.5a.1b -- <settings-home>/machine-local/.claude-klabauter-root pointer)",
            [
                py_bin,
                os.path.join(claude_klabauter_root, "coordinator", "bin", "gen-claude-klabauter-root-pointer.py"),
                *claude_klabauter_pointer_args,
            ],
            env=env,
        )
    else:
        print(
            "WARN: no python3/python interpreter found on PATH -- skipping gen-claude-klabauter-root-pointer.py (Step 3.5a.1b)",
            file=sys.stderr,
        )

    # -- Step 3.5a.2 -- gen-claude-doe-shim --
    # Retired the ["bash", gen-claude-doe-shim.sh] spawn (C13): that DoE-side
    # script was only a thin polyglot trampoline back into THIS repo's
    # coordinator_core.ops.gen_claude_doe_shim -- called in-process now.
    from coordinator_core.ops.gen_claude_doe_shim import (  # local import: avoid import cost on --help
        _default_shell_family as _gen_claude_doe_shim_default_family,
        main as _gen_claude_doe_shim_main,
    )

    # `gen_claude_doe_shim.main()` has no co-located DoE-side script path of
    # its own to derive the oracle's `${_script_dir}/../templates/shell/...`
    # default from -- its own docstring says the DoE trampoline resolves
    # that default and always passes `--template` explicitly. `coord_root`
    # (this repo's resolved DoE-clone `coordinator/` dir) is exactly that
    # default location: `<coord_root>/templates/shell/claude-doe-shim.sh.tmpl`
    # -- `templates/` is DoE doctrine content, unaffected by the b644d5a9
    # `bin/` migration, so `coord_root` (not `claude_klabauter_root`) is correct here.
    # D7 cold-install dogfood fix (2026-07-24): this call site previously
    # omitted `--template` entirely, so every `--check-only` (and live) run
    # hard-failed this required phase with "no default resolvable". `
    # --check-only` is listed first so it stays a literal prefix of the
    # logged argv line (test_c13_check_only_forwarded_to_each_native_phase
    # substring-matches "gen-claude-doe-shim --check-only").
    # The template must follow the SHELL FAMILY, not be hardcoded. The generator
    # copies template bytes verbatim but names its destination from the family
    # (`_shim_filename`), and that family defaults to "powershell" on native
    # Windows. A hardcoded `.sh.tmpl` here therefore wrote 62 lines of bash into
    # `claude-doe-shim.ps1`, whose dot-source defines no `claude()` at all — a
    # plugin-less session on every launch, with the profile's sentinel block
    # present and correct so nothing downstream reported a problem.
    # `--shell` is passed explicitly rather than left to the default so the
    # template and the family cannot drift apart again from this call site.
    _shim_family = _gen_claude_doe_shim_default_family()
    _shim_tmpl_name = (
        "claude-doe-shim.ps1.tmpl" if _shim_family == "powershell" else "claude-doe-shim.sh.tmpl"
    )
    _shim_tmpl = os.path.join(coord_root, "templates", "shell", _shim_tmpl_name)
    shim_args = (["--check-only"] if check_only else []) + [
        "--template",
        _shim_tmpl,
        "--shell",
        _shim_family,
    ]
    orch.run_required_py(
        "gen-claude-doe-shim (Step 3.5a.2 -- claude() shell shim)",
        _gen_claude_doe_shim_main,
        shim_args,
        env=env,
    )

    # -- Step 3.5b -- claude-doe wrapper install --
    _install_claude_doe_wrapper(coord_root, claude_home_dir, check_only, orch, claude_klabauter_root, settings_bin)

    # -- Step 3.5b.2 -- gen-claude-doe-launcher --
    # Retired the ["bash", gen-claude-doe-launcher.sh] spawn (C13): that
    # DoE-side script was only a thin polyglot trampoline back into THIS
    # repo's coordinator_core.ops.gen_claude_doe_launcher -- called
    # in-process now.
    from coordinator_core.ops.gen_claude_doe_launcher import (  # local import: avoid import cost on --help
        main as _gen_claude_doe_launcher_main,
    )

    # Same class of bug as the shim call site above: `gen_claude_doe_launcher`
    # has no co-located script path to derive its `--template-dir` default
    # from, and expects the DoE trampoline to pass it explicitly (default
    # location: `<coord_root>/templates/bin` -- also DoE doctrine content,
    # unaffected by the `bin/` migration). `--check-only` first for the same
    # logged-argv-substring reason as the shim call site.
    _launcher_tmpl_dir = os.path.join(coord_root, "templates", "bin")
    launcher_args = (["--check-only"] if check_only else []) + ["--template-dir", _launcher_tmpl_dir]
    orch.run_required_py(
        "gen-claude-doe-launcher (Step 3.5b.2 -- Windows claude-doe.cmd/.ps1 launcher)",
        _gen_claude_doe_launcher_main,
        launcher_args,
        env=env,
    )

    # -- Step 3.5c -- gen-settings-hooks (no --check-only support upstream) --
    #
    # DR-059 bash kill: this step used to `bash <coord_root>/bin/gen-settings-hooks.sh`.
    # That .sh is itself only an sh/python polyglot trampoline whose entire body
    # re-execs python and imports THIS repo's
    # coordinator_core.install.gen_settings_hooks -- i.e. Claude-klabauter was spawning a
    # cold bash.exe to PATH-probe python to import back into claude-klabauter. Called
    # in-process now, matching the Phase 3 Step 1 / Step 6 / Step 7 in-package
    # idiom (local import + direct call + explicit fail-loud), which also keeps
    # the operator kill-switch marker (~/.claude/.coordinator-hooks-disabled)
    # honoured -- generate() resolves it as a sibling of the resolved --out path,
    # exactly as it did under the trampoline.
    if check_only:
        orch.skip_note(
            "gen-settings-hooks (Step 3.5c) -- check-only (generator has no dry-run mode; "
            "install.md skips this call under --check-only too)"
        )
    else:
        _hooks_desc = "gen-settings-hooks (Step 3.5c -- settings.json hook block)"
        orch.phase_header(_hooks_desc)
        from coordinator_core.install.gen_settings_hooks import (  # local import: avoid import cost on --help
            GenSettingsHooksError,
            generate,
            kill_switch_marker_path,
        )

        try:
            # The generator resolves the coordinator root and the default --out
            # from the environment; run it under the same env the subprocess
            # form received (REPO_DOE_CLAUDE / PATH / CLAUDE_PLUGIN_ROOT) so
            # resolution order is unchanged by going in-process.
            with _environ_patched(env):
                hooks_status = generate()
        except GenSettingsHooksError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            print(file=sys.stderr)
            print(f"FATAL: phase '{_hooks_desc}' failed (exit 1)", file=sys.stderr)
            print("  install-maximalist.sh stops here -- fix the reported error and re-run.", file=sys.stderr)
            print("  Re-running is safe: every phase is idempotent.", file=sys.stderr)
            return 1

        # F9 fix: the prior code discarded `generate()`'s return status
        # entirely and printed the SessionStart NOTE unconditionally -- so a
        # marker-disabled (or clone-absent) skip produced install output
        # indistinguishable from a real hook-write success. Surface the
        # status explicitly and, for the two skip cases, route through
        # `orch.skip_note` (visible in the phase stream and in the final
        # summary the same way every other skip in this orchestrator is).
        # Exit code is deliberately UNCHANGED (still rc=0 for both skip
        # cases) -- this is a visibility fix, not a behavior change: a
        # marker-disabled no-op has always been a documented, intentional
        # operator choice (see the kill-switch's own "0 success (including
        # operator-kill-switch no-op)" contract, and
        # test_step35c_honours_operator_kill_switch_through_new_call_path's
        # load-bearing rc==0 assertion) and a clone-absent skip is the
        # oracle's own soft-skip contract -- neither is a run failure.
        print(f"gen-settings-hooks: {hooks_status}")
        if hooks_status == "skipped (disabled by operator marker)":
            with _environ_patched(env):
                marker = kill_switch_marker_path()
            orch.skip_note(
                f"{_hooks_desc} -- DISABLED by operator marker ({marker}). "
                "Delete that file to re-enable coordinator hook generation, then re-run this installer."
            )
        elif hooks_status == "skipped (clone absent)":
            orch.skip_note(
                f"{_hooks_desc} -- DoE clone not resolved yet; complete Step 3.5a "
                "(gen-doe-root-pointer / repos.doe_claude seed) first, then re-run."
            )
        else:
            print("  NOTE: SessionStart hooks take effect at next Claude Code boot (settings.json")
            print("  hot-reloads hook definitions, but SessionStart fires only at session start).")

    # -- Step 5 -- register-coordinator-mirror --
    # Retired the ["bash", register-coordinator-mirror.sh] spawn (C13): that
    # DoE-side script was a thin polyglot trampoline over THIS repo's
    # coordinator_core.ops.register_coordinator_mirror -- but it ALSO
    # resolved a genuinely DoE-owned fact (the "coordinator live path") before
    # handing off via --live-path. That resolution (_resolve_coordinator_live_path,
    # verbatim port of the trampoline's own helper) now runs here; the engine
    # module itself is called in-process.
    from coordinator_core.ops.register_coordinator_mirror import (  # local import: avoid import cost on --help
        main as _register_coordinator_mirror_main,
    )

    def _register_coordinator_mirror_phase(_argv: List[str]) -> int:
        coordinator_live = _resolve_coordinator_live_path()
        if not coordinator_live:
            return 1
        mirror_argv = (["--check-only"] if check_only else []) + ["--live-path", coordinator_live]
        return _register_coordinator_mirror_main(mirror_argv)

    orch.run_required_py(
        "register-coordinator-mirror (Step 5 -- plugin.mirrors registration)",
        _register_coordinator_mirror_phase,
        [],
        env=env,
    )

    # -- Step 6 -- ensure-coordinator-venv (native, advisory) --
    _venv_desc = "ensure-coordinator-venv (Step 6 -- coordinator_whoami venv)"
    orch.phase_header(_venv_desc)
    from coordinator_core._settings_home import settings_home  # local import: avoid import cost on --help
    from coordinator_core.install.ensure_venv import EnsureVenvError, ensure_coordinator_venv

    try:
        venv_status = ensure_coordinator_venv(
            Path(coord_root), settings_home(), claude_home=claude_home_dir, check_only=check_only,
        )
        print(f"ensure-coordinator-venv: {venv_status}")
    except EnsureVenvError as exc:
        print(
            f"WARN: phase '{_venv_desc}' failed -- continuing (advisory, not fatal): {exc}",
            file=sys.stderr,
        )
        orch.failed = True

    # -- Step 6b -- compileall (native, advisory) --
    # Recovers first-bin-invocation latency: without this, bytecode compilation
    # cost is paid cold on the FIRST call to any bin after install, not at
    # install time.
    _compileall_desc = "compileall (Step 6b -- precompile coordinator_core bytecode)"
    orch.phase_header(_compileall_desc)
    if check_only:
        print("compileall: skipped -- --check-only writes no .pyc")
    else:
        _pkg_root = Path(__file__).resolve().parent.parent
        _compileall_interps = _compileall_interpreters()
        if not _compileall_interps:
            print(
                f"WARN: phase '{_compileall_desc}' found no interpreter to precompile "
                "under -- continuing (advisory, not fatal)",
                file=sys.stderr,
            )
            orch.failed = True
        for _interp in _compileall_interps:
            try:
                _compileall_proc = _run_compileall(_interp, _pkg_root)
            except (subprocess.TimeoutExpired, OSError) as exc:
                print(
                    f"WARN: phase '{_compileall_desc}' failed under {_interp} -- "
                    f"continuing (advisory, not fatal): {exc}",
                    file=sys.stderr,
                )
                orch.failed = True
                continue
            if _compileall_proc.returncode != 0:
                print(
                    f"WARN: phase '{_compileall_desc}' failed under {_interp} "
                    f"(exit {_compileall_proc.returncode}) -- continuing "
                    "(advisory, not fatal)",
                    file=sys.stderr,
                )
                if _compileall_proc.stderr:
                    print(_compileall_proc.stderr.strip(), file=sys.stderr)
                orch.failed = True
            else:
                print(f"compileall: precompiled under {_interp}")

    # -- Step 7 -- scaffold-canonical-structure (native, advisory) --
    _scaffold_desc = "scaffold-canonical-structure (Step 7 -- canonical document structure)"
    orch.phase_header(_scaffold_desc)
    from coordinator_core.install.scaffold_structure import (  # local import: avoid import cost on --help
        scaffold_canonical_structure,
    )

    try:
        _scaffold_root = os.path.join(claude_home_dir, ".claude")
        _scaffold_result = scaffold_canonical_structure(
            _scaffold_root, Path(coord_root), dry_run=check_only,
        )
        print(
            f"scaffold-canonical-structure: {_scaffold_result.created_dirs} dir(s), "
            f"{_scaffold_result.created_readmes} README(s), {_scaffold_result.created_gitkeeps} "
            f".gitkeep(s), {_scaffold_result.created_files} file(s) "
            f"{'would be ' if check_only else ''}created; {_scaffold_result.skipped} skipped; "
            f"{len(_scaffold_result.dropped_entries)} declared-eager entries dropped "
            "(manifest/parser disagreement); "
            f"{len(_scaffold_result.satisfied_elsewhere)} declared-eager entries satisfied "
            "elsewhere (produced_by)"
        )
        # Review: code-reviewer -- Step 7 previously hand-rolled a summary
        # that never read dropped_entries/satisfied_elsewhere, defeating the
        # docstring's claim that this live path surfaces a genuine orphan
        # (manifest/parser disagreement); now folded into the summary line.
    except Exception as exc:
        # Review: code-reviewer -- widened from `except ScaffoldError` to catch
        # unwrapped OSError/PermissionError from scaffold_structure's raw fs
        # writes (mkdir/touch/write_text/copyfile), matching probe_p12's
        # `except Exception` for the identical call so this advisory phase
        # can't crash the installer process (AC D3).
        print(
            f"WARN: phase '{_scaffold_desc}' failed -- continuing (advisory, not fatal): {exc}",
            file=sys.stderr,
        )
        orch.failed = True

    # -- Step 7.5 -- check-install-singularity (always runs, incl. --check-only) --
    # Retired the ["bash", check-install-singularity.sh] spawn (C13): that
    # DoE-side script was only a thin polyglot trampoline back into THIS
    # repo's coordinator_core.install.check_install_singularity -- called
    # in-process now.
    from coordinator_core.install.check_install_singularity import (  # local import: avoid import cost on --help
        main as _check_install_singularity_main,
    )

    orch.run_required_py(
        "check-install-singularity (Step 7.5 -- canonical-locus integrity gate)",
        _check_install_singularity_main,
        [],
        env=env,
    )

    # -- Step 8 -- capture-fan-out-threshold --
    # Retired the ["bash", capture-fan-out-threshold.sh] spawn (C13): that
    # DoE-side script was only a thin polyglot trampoline back into THIS
    # repo's coordinator_core.ops.capture_fan_out_threshold -- called
    # in-process now.
    from coordinator_core.ops.capture_fan_out_threshold import (  # local import: avoid import cost on --help
        main as _capture_fan_out_threshold_main,
    )

    threshold_args = ["--check-only"] if check_only else []
    orch.run_required_py(
        "capture-fan-out-threshold (Step 8 -- fan-out large-wave threshold)",
        _capture_fan_out_threshold_main,
        threshold_args,
        env=env,
    )

    # -- Step 9 -- platform-localize --
    # Retired the ["bash", <installed>/platform-localize.sh] spawn (C13): that
    # installed-template script was only a thin polyglot trampoline back into
    # THIS repo's coordinator_core.hooks.platform_localize -- called
    # in-process now. There is no longer a per-machine file whose absence is
    # observable (see module docstring divergence note), so the "not found"
    # WARN+FAILED-without-halting asymmetry is retired, not reproduced: this
    # is a plain run_required phase like its siblings.
    if check_only:
        orch.skip_note("platform-localize (Step 9) -- check-only mode")
    else:
        from coordinator_core.hooks.platform_localize import (  # local import: avoid import cost on --help
            main as _platform_localize_main,
        )

        orch.run_required_py(
            "platform-localize (Step 9 -- settings.local.json / marketplaces)",
            _platform_localize_main,
            [],
            env=env,
        )

    # -- Phase 7 Step 0 -- record setup_concluded receipt (idempotent) --
    # Retired the ["bash", coordinator-setup-state.sh] spawn (C13): that
    # DoE-side script was only a thin polyglot trampoline back into THIS
    # repo's coordinator_core.ops.coordinator_setup_state -- called
    # in-process now.
    if check_only:
        orch.skip_note(
            "coordinator-setup-state record setup_concluded (Phase 7 Step 0) -- check-only (would record)"
        )
    else:
        from coordinator_core.ops.coordinator_setup_state import (  # local import: avoid import cost on --help
            main as _coordinator_setup_state_main,
        )

        orch.run_required_py(
            "coordinator-setup-state record setup_concluded (Phase 7 Step 0 -- receipt)",
            _coordinator_setup_state_main,
            ["record", "setup_concluded"],
            env=env,
        )

    # -- Install receipt: build + persist from the resolution journal now
    # that every writing phase above has run (ordering: AFTER the phases
    # that write, so the receipt reflects the completed run, not a
    # snapshot mid-install). Recording leg only -- never fails the install;
    # a journal-read, receipt-build, or persist failure is logged loudly
    # and swallowed here, matching the run-start setup's same contract.
    try:
        _build_and_persist_receipt(Path(claude_klabauter_root))
    except Exception as exc:  # noqa: BLE001 -- recording leg must never break the install
        print(f"WARN: install-receipt build/persist failed (non-fatal): {exc}", file=sys.stderr)

    # -- Phases explicitly out of scope for this mechanical orchestrator --
    orch.skip_note("Phase 4 (~/.claude git-tracking offer) -- operator's call; run /coordinator:install")
    orch.skip_note("Phase 5 (coordinator.local.md project_type) -- project-local; run /coordinator:repo-setup")
    orch.skip_note("Phase 6 (persona customization / 1Password GitHub auth) -- opt-in; run /coordinator:install")
    orch.skip_note(
        "Phase 7 guided orientation -- REQUIRED next step, not merely optional: restart Claude Code, "
        "then say 'walk me through the coordinator' to co-write CLAUDE.md and complete orientation."
    )

    print()
    print("=== install-maximalist.sh complete ===")
    if orch.skipped:
        # F9: a phase that did nothing is indistinguishable from a phase that
        # succeeded unless something says so, in one place, at the end of the
        # run -- not only inline where a long/noisy run can bury it. Every
        # `--check-only` informational skip (Phase 4/5/6/7 explicitly out of
        # scope, "would seed" previews, etc.) also lands here; that is
        # intentional -- an operator scanning only the tail should be able to
        # see every phase this run did NOT act on and why, not just the ones
        # that indicate a real defect.
        print()
        print(f"SKIPPED PHASES ({len(orch.skipped)}):")
        for msg in orch.skipped:
            print(f"  - {msg}")
    if check_only:
        print("Ran in --check-only mode -- no mutations were made.")
    elif orch.failed:
        print("Completed with one or more ADVISORY failures above (non-fatal) -- review the WARN lines.")
    else:
        print("All mechanical wiring phases completed successfully.")
    print()
    print("NEXT STEP (required, not optional): restart Claude Code, then say")
    print('  "walk me through the coordinator"')
    print("to complete operator identity capture and guided orientation -- see")
    print("coordinator/commands/install.md Phase 2 and Phase 7.")

    return 1 if orch.failed else 0


def main(argv: List[str]) -> int:
    try:
        parsed = _parse_args(argv)
    except _UsageError as exc:
        print(f"ERROR: unrecognized argument: {exc.message}", file=sys.stderr)
        print("  Run with --help for usage.", file=sys.stderr)
        return 2
    if parsed is None:
        return 0

    coord_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    doe_clone = os.environ.get("REPO_DOE_CLAUDE")
    if not coord_root or not doe_clone:
        print(
            "install-maximalist: CLAUDE_PLUGIN_ROOT and REPO_DOE_CLAUDE must both be set "
            "by the caller (the DoE-side trampoline self-resolves and exports both before "
            "invoking this module).",
            file=sys.stderr,
        )
        return 1

    # `coordinator_core` only ever runs from inside the real claude-klabauter clone --
    # this module lives at `<claude_klabauter_root>/coordinator_core/install/maximalist.py`,
    # two levels below the clone root. See `run()`'s docstring for why this
    # default is computed HERE (the real-install entry point) rather than
    # inside `run()` itself.
    claude_klabauter_root = str(Path(__file__).resolve().parents[2])

    try:
        return run(
            check_only=parsed["check_only"],
            non_interactive=parsed["non_interactive"],
            coord_root=coord_root,
            doe_clone=doe_clone,
            claude_klabauter_root=claude_klabauter_root,
        )
    except SystemExit as exc:
        # run_required phases sys.exit() directly (matches the bash oracle's
        # `exit "$rc"` inside the helper) -- propagate that exact code.
        return int(exc.code) if isinstance(exc.code, int) else 1


WRITE_SURFACE = WriteSurfaceDeclaration(
    writer_id="maximalist",
    source_module="coordinator_core.install.maximalist",
    clauses=(
        # Clause 1 — `_install_claude_doe_wrapper` (Step 3.5b): the one
        # place this ORCHESTRATOR writes directly rather than delegating to
        # a declaring writer module. POSIX: `os.symlink` + atomic
        # `os.replace` onto `~/.local/bin/claude-doe`, pointed at
        # `<settings-home>/bin/claude-doe`. Windows: `shutil.copy2` of the
        # wrapper binary from `<claude-klabauter-root>/coordinator/bin/claude-doe.py`
        # to a same-directory temp path, `os.chmod` on that temp path, then
        # atomic `os.replace` onto the same destination (no native symlink
        # story assumed — see the function's own docstring). One entry
        # covers both branches; they share a destination and purpose,
        # differing only in copy vs. symlink for the staged artifact.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    path="<install-base>/.local/bin/claude-doe",
                    reason=(
                        "claude-doe wrapper install (Step 3.5b): POSIX "
                        "symlinks this path onto "
                        "<settings-home>/bin/claude-doe (atomic "
                        "os.replace of a temp symlink); Windows instead "
                        "shutil.copy2's the wrapper binary from "
                        "<claude-klabauter-root>/coordinator/bin/claude-doe.py to a "
                        "same-directory temp path, chmods +x, then "
                        "publishes with an atomic os.replace onto this "
                        "path. Written in-line by this orchestrator, not "
                        "through a declaring delegate."
                    ),
                ),
            ),
        ),
        # Clause 2 — `_defender_offer` (Windows-only, admin-gated,
        # operator-consented, default-DECLINED): adds process-exclusion
        # entries to Windows Defender's real-time scanning config via
        # `powershell.exe -Command Add-MpPreference -ExclusionProcess`.
        # No kind in the frozen eight-kind vocabulary honestly names a
        # third-party OS security-product preference write (Defender
        # persists this in an opaque, GUID-keyed registry hive this repo
        # has no visibility into) — `file-path` is the least-dishonest
        # choice, matching the stated-reason escape hatch precedent
        # (`substrate._fnm_step`, `first_run`'s `_brew_install`); `path`
        # is deliberately left unset and the free-text `reason` carries
        # the actual truth.
        StaticClause(
            entries=(
                WriteSurfaceEntry(
                    kind="file-path",
                    reason=(
                        "Windows Defender process-exclusion offer "
                        "(_defender_offer, Phase 3 Step 1c): consent-"
                        "gated, admin-elevation-gated, default-DECLINED "
                        "(no exclusion applied unless the operator "
                        "explicitly opts in at an interactive, elevated "
                        "prompt). When accepted, runs "
                        "`Add-MpPreference -ExclusionProcess <path>` per "
                        "resolved toolchain binary (bash/git/sh) via "
                        "powershell.exe; the resulting registry write "
                        "location is Defender-internal and not enumerable "
                        "from this repo's own source."
                    ),
                ),
            ),
        ),
    ),
)
"""This module is a genuine ORCHESTRATOR for every OTHER phase — Phase 3
Step 1 (install-substrate), install-health-run, gen-doe-root-pointer,
gen-claude-doe-shim, gen-claude-doe-launcher, gen-settings-hooks,
register-coordinator-mirror, ensure-coordinator-venv, scaffold-canonical-
structure, check-install-singularity, capture-fan-out-threshold,
platform-localize, and coordinator-setup-state are each called in-process
and each already declares (or is separately allowlisted for) its own
WRITE_SURFACE — re-declaring their clauses here would double-count every
entry in the emitted manifest, the exact caller-vs-delegate duplication
this debt item exists to remove.

`_install_claude_doe_wrapper` (Step 3.5b, clause 1 above) is the one
exception: it writes `~/.local/bin/claude-doe` directly, in-line in this
module, rather than through a declaring delegate — so it is this writer's
own surface to declare, not double-counted with anyone else's.

Not declared here (deliberately, not an oversight):
  - The best-effort `repos.doe_claude` / `repos.claude_klabauter`
    machine-local seed blocks (`_run([*ml_argv, "set", ...])`) shell out to
    the external `machine-local` CLI, a SEPARATE top-level package
    (`coordinator_core.machine_resolver`/its CLI entry point) outside
    `coordinator_core/install/` — this test's own negative spec states a
    cross-package delegate call is structurally invisible to its one-level
    AST walk, and by the same token is not this writer's surface to claim
    (machine-local's own CLI package is where that declaration belongs, if
    it wants one).
"""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
