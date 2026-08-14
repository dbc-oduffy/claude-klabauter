"""
coordinator_core.claude_klabauter_root — ported from coordinator/lib/coordinator-claude-klabauter-root.sh
(DoE clean-slate migration, sourced-lib variant — DoE .sh is left untouched; its ~60
`source coordinator-claude-klabauter-root.sh` callers switch to `import coordinator_core.claude_klabauter_root`
in a later gated wave, per port-template variant "SOURCED LIB").

Purpose: resolves the claude-klabauter sibling-repo root, analogous to how CLAUDE_HOME->~/.claude
works for the coordinator meta-repo. Mirror-image of `coordinator_core.ops.gen_doe_root_pointer`
(which resolves DOE_ROOT from inside a DoE-clone-relative context) — this module resolves
CLAUDE_KLABAUTER_ROOT for callers already running inside the claude-klabauter engine.

Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § C1 / AC1
Windows portability rung: docs/plans/2026-07-14-claude-klabauter-windows-portability.md § C1

Resolution chain (unchanged from the bash oracle), in order:
  1. CLAUDE_KLABAUTER_ROOT env var — if already set, return it unchanged.
  1.5. <settings-home>/machine-local/.claude-klabauter-root pointer file — a cheap direct-file-read,
       checked ahead of the expensive machine-local subprocess ladder so per-invoke
       resolution spawns zero subprocesses on Windows. Falls through to rung 2 if
       absent/empty.
  2. `machine-local get repos.claude_klabauter` (CLI, PATH-resolved) — delegates to the
     §4c four-rung discovery ladder (explicit env override -> OS-keyed search-root
     marker autodiscovery -> path-exceptions -> registry.local.toml fallback). Does NOT
     reimplement those rungs here — shells out exactly like the bash oracle's Rung 2, so
     the registry-merge logic has exactly one implementation.
  3. Hard error (RuntimeError) with actionable remediation, mirroring the bash oracle's
     stderr message verbatim (module-level constant so callers can print it themselves).

Public API:
    def coordinator_claude_klabauter_root() -> str   — mirrors the shell function of the same name.
        Returns the resolved absolute path. Raises RuntimeError (message = the bash
        oracle's stderr remediation text) on failure. Unlike the shell function this
        does NOT export CLAUDE_KLABAUTER_ROOT into os.environ on success as a side effect free
        of caller intent — callers that want the §4b idempotency-gate behavior set
        os.environ["CLAUDE_KLABAUTER_ROOT"] themselves after a successful call.

Negative-spec:
    - Does NOT reimplement the machine-local registry.toml/registry.local.toml parser —
      shells out to the `machine-local` CLI (PATH-resolved), exactly like the bash
      oracle's Rung 2 and gen_doe_root_pointer.py's Tier 2.
    - Does NOT export CLAUDE_KLABAUTER_ROOT to os.environ as a side effect (the bash oracle does,
      per its own §4b idempotency-gate docstring) — a pure resolver is safer to import
      from a long-lived process (e.g. a future op) where implicit env mutation on
      import-time-adjacent calls would be a surprising side effect. Callers that need
      the shell's idempotency-gate behavior opt in explicitly.
      Review: code-reviewer — this note previously recorded a deliberate
      ASYMMETRY against `coordinator_core.ops.coordinator_doe_root`, which did
      export `REPO_DOE_CLAUDE` to os.environ on every successful resolution to
      mirror ITS bash oracle's `export`. That asymmetry was retired on
      2026-07-21: the export leaked interpreter-global state across tests and
      into every subprocess child's inherited env, and `coordinator_doe_root` is
      now pure too (its re-resolution guard moved to an explicit module-scope
      memo with a reset seam). Both resolvers now make the SAME choice, and this
      module's was the one that turned out right — see that module's docstring
      § DECISION REVERSAL.
    - Does NOT spawn a subprocess for Rung 1 or Rung 1.5 — only Rung 2 shells out.
    - The bash oracle's `set -uo pipefail` / BASH_VERSINFO guard notes have no meaning
      here — this is a pure-Python module. Omitted intentionally.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import types
from pathlib import Path
from typing import Optional, Tuple

from coordinator_core._settings_home import machine_local_dir

#: Wall-clock bound on the Rung 2 `machine-local` subprocess. This resolver is
#: reached from PreToolUse hook paths, so an unbounded wait blocks an interactive
#: tool call outright; a timeout degrades to Rung 3's actionable error instead,
#: which is the same disposition Rung 2 already takes on an exec failure. Sized to
#: be far longer than a registry read and far shorter than a hook budget.
_RUNG2_TIMEOUT_SECS = 2.0

_REMEDIATION = (
    "coordinator_claude_klabauter_root: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set.\n"
    "  The machine-local registry has no 'repos.claude_klabauter' entry on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c"
)


def coordinator_claude_klabauter_root() -> str:
    """Resolve the claude-klabauter sibling-repo root via the documented chain.

    Returns the resolved absolute path (as read — no realpath/normalization beyond
    the source's own whitespace-strip, mirroring the bash oracle's behavior).
    Raises RuntimeError with the bash oracle's remediation text on failure.
    """
    # Rung 1: CLAUDE_KLABAUTER_ROOT already set in environment (§4b idempotency gate).
    existing = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if existing:
        return existing

    # Rung 1.5: cheap direct-file-read pointer, checked ahead of the expensive
    # machine-local subprocess ladder. Absence/emptiness is a normal fallback
    # state, not an error — falls through to Rung 2.
    pointer_path = machine_local_dir() / ".claude-klabauter-root"
    try:
        with open(pointer_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass  # missing/unreadable pointer file — normal, falls through to Rung 2

    # Rung 2: machine-local registry (§4c four-rung discovery ladder runs inside
    # the `machine-local` CLI itself — not reimplemented here).
    ml_bin = shutil.which("machine-local")
    if ml_bin is not None:
        try:
            from coordinator_core.win_portability import no_console_creationflags

            result = subprocess.run(
                [ml_bin, "get", "repos.claude_klabauter"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_RUNG2_TIMEOUT_SECS,
                **no_console_creationflags(),
            )
        except OSError:
            # `which` found a `machine-local` on PATH but exec failed (e.g. removed
            # mid-race, not actually executable) — falls through to Rung 3's hard
            # error like any other unresolved case; not a distinct failure mode.
            result = None
        except subprocess.TimeoutExpired:
            # Same disposition as the exec failure above: an unresolved Rung 2, not a
            # distinct failure mode. The bound exists because this resolver is reached
            # from PreToolUse hook paths on the interactive critical path — an
            # unbounded wait there hangs the tool call rather than degrading to Rung
            # 3's actionable error, and a slow answer is worth less than a prompt one.
            result = None

        if result is not None and result.returncode == 0:
            resolved = result.stdout.strip()
            if resolved:
                return resolved

    # Rung 3: hard error with actionable remediation.
    raise RuntimeError(_REMEDIATION)


# --- DR-132 two-tier gate wrapper -----------------------------------------
#
# `coordinator_claude_klabauter_root_with_class()` below wraps C3's shim
# (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`) rather than
# reimplementing its published-engine-vs-live-working-tree gate — the
# single-implementation property (C3<->C4b<->claude_klabauter_root all defer to one
# ladder) is the entire point of the DR-132 restructure. The shim is loaded
# BY PATH (`importlib.util.spec_from_file_location`), not imported as a
# package, because the one-way no-import contract runs only in this
# direction: the shim must stay import-independent of `coordinator_core`
# (it is also installed standalone into a bare `<settings-home>/bin/`, see
# its own module docstring), but `coordinator_core` loading the shim by path
# creates no such dependency back onto it.
#
# Spec backlink: pln-two-tier-engine-root-resolutio-024269 § C4 (wrapper half)
#
# C5 (engine/edit skew advisory) hook: deliberately NOT reimplemented here.
# The advisory only ever fires when the resolution class comes back
# ``resolved-engine`` (see the shim's own `resolve_claude_klabauter_root_with_class`
# docstring), and every code path in `coordinator_claude_klabauter_root_with_class()`
# below that can produce that class routes through the SAME loaded shim
# instance's `resolve_claude_klabauter_root_with_class()` — so the shim's own
# once-per-process advisory (module-scope flag, `_reset_skew_advisory()`)
# already covers this wrapper's callers without a second implementation.
# `_reset_skew_advisory()` below is a thin test-seam passthrough, not a
# parallel check. Do not add a duplicate stderr write here — it would
# double the advisory for every in-process caller that goes through this
# wrapper. Spec backlink: pln-two-tier-engine-root-resolutio-024269 § C5

#: `coordinator_core/claude_klabauter_root.py`'s parent-of-parent is the claude-klabauter repo
#: root — no chicken-and-egg with resolving the very root this module exists
#: to resolve, since the shim's path is fixed relative to this file, not to
#: any already-resolved CLAUDE_KLABAUTER_ROOT value.
_SHIM_PATH = Path(__file__).resolve().parent.parent / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"

#: Review: code-reviewer — the free-rung early returns below (Rung 1, Rung
#: 1.5) hardcode this literal rather than importing the shim's
#: `RESOLUTION_LIVE_WORKING_TREE` constant, deliberately: loading the shim
#: just to read a string constant would defeat the hot-path short-circuit
#: those rungs exist to preserve (no shim load, no gate walk). Kept as a
#: named module-level constant instead of an inline literal so the
#: duplication is self-documenting; per the shim's own `RESOLUTION_*`
#: comment, these strings are "part of the contract, not just their names" —
#: if the shim's constant value ever changes, this one must change with it.
_RESOLUTION_LIVE_WORKING_TREE_LITERAL = "live-working-tree"

#: Review: code-reviewer — sanctioned path-load consumer surface. This
#: wrapper's Cheap-short-circuit step (see `coordinator_claude_klabauter_root_with_class`
#: step 2 below) reaches into the shim's underscore-prefixed helpers
#: (`_ml_dir`, `_registry_value`, `_resolve_claude_klabauter_root`) rather than going
#: exclusively through the shim's public `resolve_claude_klabauter_root_with_class()`.
#: This is a DECLARED exception, not general license: `coordinator_core`
#: (this module only) is a named path-load consumer of those three helpers
#: plus `resolve_claude_klabauter_root_with_class` and the `RESOLUTION_*` constants —
#: see the matching declaration in the shim's own module docstring
#: (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`). Changing
#: `resolve_claude_klabauter_root_with_class()`'s step-1 precondition (the
#: published-engine-registered-and-usable check) obliges updating this
#: wrapper's short-circuit in the SAME change — the underscore prefix still
#: means "not for general callers" for everyone else.
#:
#: The mechanical backstop for that obligation is
#: `coordinator_core/tests/test_claude_klabauter_root_two_tier.py`'s cross-entrypoint
#: agreement test: it drives a fixture with `repos.claude_klabauter` ABSENT,
#: so this wrapper takes the short-circuit while the shim runs its own full
#: ladder, and both are asserted to still agree. A future step-order change
#: that breaks that agreement fails there, not only in prose.

_shim_module: Optional[types.ModuleType] = None


def _load_shim() -> types.ModuleType:
    """Load C3's shim module by path (never `import`), memoized module-scope.

    Raises `RuntimeError` if the shim file is missing — that is a broken
    checkout, not a normal fallback state, so it does not degrade quietly
    the way the free rungs below do.
    """
    global _shim_module
    if _shim_module is not None:
        return _shim_module
    if not _SHIM_PATH.is_file():
        raise RuntimeError(
            f"coordinator_claude_klabauter_root_with_class: shim not found at '{_SHIM_PATH}' — "
            "broken or partial claude-klabauter checkout."
        )
    spec = importlib.util.spec_from_file_location("_claude_klabauter_root_gate_shim", _SHIM_PATH)
    if spec is None or spec.loader is None:
        # Review: code-reviewer — a bare `assert` here is stripped under
        # `python -O`/PYTHONOPTIMIZE, degrading this fail-loud check to an
        # unguarded AttributeError two lines below; an explicit raise
        # survives an optimized run.
        raise RuntimeError(
            f"coordinator_claude_klabauter_root_with_class: could not build an import spec "
            f"for shim at '{_SHIM_PATH}' — broken or partial claude-klabauter checkout."
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _shim_module = module
    return module


def _reset_shim_cache() -> None:
    """Test-only helper: clear the module-scope shim-load memo."""
    global _shim_module
    _shim_module = None


def _reset_skew_advisory() -> None:
    """Test-only helper: clear the loaded shim's once-per-process
    engine/edit skew advisory flag (C5). A no-op if the shim has not been
    loaded yet in this process — mirrors ``_reset_gate_memo``'s "nothing to
    clear" tolerance below."""
    if _shim_module is not None and hasattr(_shim_module, "_reset_skew_advisory"):
        _shim_module._reset_skew_advisory()


#: Module-scope memo for the (expensive) two-tier gate answer, keyed on
#: `(registry mtime pair, session root)`. Mirrors
#: `coordinator_core.ops.coordinator_doe_root`'s DECISION REVERSAL shape
#: (module docstring § DECISION REVERSAL) — an explicit memo with a reset
#: seam, not an `os.environ` export, so the cache dies with the test/process
#: boundary rather than leaking into subprocess children or across pytest
#: cases. A single-entry cache (not an unbounded dict) is deliberate: the
#: registry/session-root pair changes at most once per long-lived-process
#: lifetime in practice, and an unbounded key space would never evict.
_GATE_MEMO_KEY: Optional[Tuple[float, float, float, Optional[str]]] = None
_GATE_MEMO_VALUE: Optional[Tuple[Optional[str], str]] = None

#: Review: code-reviewer — two known, accepted limitations of this memo,
#: deliberately left unaddressed rather than fixed:
#:   (1) mtime granularity is filesystem-dependent (FAT32/exFAT: ~2s, some
#:       legacy/network filesystems coarser); two writes to a tracked file
#:       within the same tick produce an identical memo key, silently
#:       masking the second write for the rest of the process. Realistic
#:       install targets (NTFS/ext4/APFS) are sub-second and
#:       `machine-local set` is interactive/human-paced, so this is accepted
#:       as a narrow, low-likelihood window rather than closed.
#:   (2) no lock guards the read-check-write below; under CPython's GIL the
#:       tuple itself cannot corrupt, but two threads racing the full gate
#:       simultaneously can both miss the memo and both pay the full walk
#:       cost once. Benign — accepted, not fixed — but worth knowing if this
#:       module is ever called from a threaded server context (the
#:       module docstring's "~30 long-lived-process call sites" language
#:       does not itself rule that out).


def _reset_gate_memo() -> None:
    """Test-only helper: clear the two-tier gate answer memo.

    Mirrors `coordinator_core.ops.coordinator_doe_root._reset_doe_root_cache`.
    Exists because the memo is interpreter-lifetime state: under pytest the
    first test to resolve would otherwise pin the answer for every later
    test in the same process.
    """
    global _GATE_MEMO_KEY, _GATE_MEMO_VALUE
    _GATE_MEMO_KEY = None
    _GATE_MEMO_VALUE = None


def _registry_mtime_pair(ml_dir: Path) -> Tuple[float, float, float]:
    """`(registry.toml mtime, registry.local.toml mtime, .claude-klabauter-root mtime)`,
    `-1.0` for a missing file — cheap staleness key for the gate memo below.
    Never raises.

    Review: code-reviewer — the `.claude-klabauter-root` sentinel mtime is included
    because the full-gate branch's `_resolve_claude_klabauter_root` falls back to
    reading that sentinel when the registry key is absent; omitting it meant
    a mid-process edit to the sentinel could not invalidate the memo. A
    missing sentinel is a stable `-1.0`, not an error — `stat()` is the only
    I/O this key performs."""

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    return (
        _mtime(ml_dir / "registry.toml"),
        _mtime(ml_dir / "registry.local.toml"),
        _mtime(ml_dir / ".claude-klabauter-root"),
    )


def coordinator_claude_klabauter_root_with_class() -> Tuple[str, str]:
    """Resolve the claude-klabauter root AND the DR-132 resolution class alongside it.

    Wraps C3's `resolve_claude_klabauter_root_with_class()` shim — does NOT
    reimplement the published-engine-vs-live-working-tree gate. Returns
    `(root, resolution_class)` where the class is one of the shim's
    `RESOLUTION_RESOLVED_ENGINE` / `RESOLUTION_LIVE_WORKING_TREE` /
    `RESOLUTION_UNRESOLVED` string constants.

    HOT-PATH SHAPE (do not "simplify" away — see plan § C4 wrapper half):
      1. Rung 1 (`CLAUDE_KLABAUTER_ROOT` env var) — `coordinator_claude_klabauter_root()`'s
         existing free rung, re-checked here so this function never runs
         the gate ahead of it. Classifies as `RESOLUTION_LIVE_WORKING_TREE`:
         it is the SAME resolution `coordinator_claude_klabauter_root()` already
         performs today, just with a class label attached; it predates and
         is unaffected by the two-tier gate.
      2. Cheap short-circuit: if `repos.claude_klabauter` (the published
         engine mirror key) is not registered at all, the gate's step 1/3
         (published-engine branches) can never fire — skip straight to the
         shim's own live-tree resolution (`_resolve_claude_klabauter_root`, which
         itself reads the `.claude-klabauter-root` pointer as ITS OWN rung 2) rather
         than paying for the full `_is_engine_working_repo` session-root
         walk the gate would otherwise do first. THIS branch is where Rung
         1.5's `.claude-klabauter-root` pointer fast path now lives — checked here,
         ahead of the full gate walk (`_is_engine_working_repo`), so the
         single-tree box (no klabauter registered) keeps today's
         byte-identical zero-subprocess fast path (AC4). Note this still
         pays one `_load_shim()`/`exec_module` cost (the unconditional
         `_load_shim()` call at the top of this function, before this
         branch) — it is the gate walk, not the shim load, that is skipped
         here. On a dual-boot box (klabauter
         IS registered) the pointer is deliberately NOT consulted here —
         step 3's full gate decides instead, per plan
         `2026-08-12-arm-the-klabauter-dual-boot-the-wrapper.md` § Problem:
         the pointer previously pre-empted the gate on every installed
         machine, since the installer always writes it. This loses nothing
         on the dual-boot path: the shim's `_resolve_claude_klabauter_root` already
         reads `.claude-klabauter-root` as its own rung inside the gate, so a working
         repo still resolves via the pointer from inside step 3.
      3. Otherwise, run the full gate (`resolve_claude_klabauter_root_with_class()`),
         memoized module-scope on `(registry mtime pair, session root)` so
         a long-lived process re-invoking this on every call does not
         re-walk the registry/session-root chain each time. See
         `_reset_gate_memo()` for the test-seam contract.

    Raises whatever the shim raises on a hard miss
    (`ClaudeKlabauterResolutionError`) — this function does not translate that into
    `coordinator_claude_klabauter_root()`'s `RuntimeError`/`_REMEDIATION` shape; the
    two error types are distinct because they come from distinct call
    chains (see `coordinator_claude_klabauter_root()`'s own remediation vs. the
    shim's registry-plus-published-engine remediation text).
    """
    existing = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if existing:
        return existing, _RESOLUTION_LIVE_WORKING_TREE_LITERAL

    shim = _load_shim()
    ml_dir = shim._ml_dir()

    published_key = shim._registry_value(ml_dir, "repos.claude_klabauter")
    if not published_key:
        # Rung 1.5 (`.claude-klabauter-root` pointer) fast path — see this function's
        # own docstring, step 2. Only reachable here, on the
        # klabauter-absent single-tree box, so a dual-boot box never lets
        # the pointer pre-empt step 3's full gate.
        # Reuses the already-computed `ml_dir` (override-aware via
        # `shim._ml_dir()`) rather than re-resolving `machine_local_dir()`
        # directly — the latter does not honor `MACHINE_LOCAL_REGISTRY_DIR`,
        # so the two would disagree on the pointer file's location whenever
        # that override is set. Review: code-reviewer.
        pointer_path = ml_dir / ".claude-klabauter-root"
        try:
            with open(pointer_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val, _RESOLUTION_LIVE_WORKING_TREE_LITERAL
        except OSError:
            pass  # missing/unreadable pointer file — normal, falls through

        root = shim._resolve_claude_klabauter_root(ml_dir)
        return root, shim.RESOLUTION_LIVE_WORKING_TREE

    session_root = shim._session_repo_root()
    memo_key = (
        *_registry_mtime_pair(ml_dir),
        str(session_root) if session_root is not None else None,
    )

    global _GATE_MEMO_KEY, _GATE_MEMO_VALUE
    if _GATE_MEMO_KEY == memo_key and _GATE_MEMO_VALUE is not None:
        return _GATE_MEMO_VALUE

    result = shim.resolve_claude_klabauter_root_with_class()
    _GATE_MEMO_KEY = memo_key
    _GATE_MEMO_VALUE = result
    return result


def published_engine_mirror_path() -> Optional[str]:
    """Return the registered `repos.claude_klabauter` published-engine-mirror
    path, or ``None`` if it is not registered/usable — the SAME
    "registered and on-disk usable" check `coordinator_claude_klabauter_root_with_class`
    already performs via the shim's `_resolve_published_engine`, exposed here
    standalone so a caller (``coordinator_core.state_root``'s Rule 5 sibling
    branch) can ask "is THIS path the published mirror clone?" without
    running the full live-tree-vs-published gate, which answers a different
    question (which root the CURRENT process should treat as ITS OWN claude-klabauter
    root) that is irrelevant to a caller identifying a specific candidate
    directory. Deliberately reuses the shim's `_resolve_published_engine`
    rather than re-deriving the `repos.claude_klabauter` registry read here —
    see this module's docstring, "single-implementation property". Never
    raises: fail-open, mirroring the shim helper's own contract."""
    try:
        shim = _load_shim()
        return shim._resolve_published_engine(shim._ml_dir())
    except Exception:
        return None
