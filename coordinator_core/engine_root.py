"""
coordinator_core.engine_root — ported from coordinator/lib/coordinator-claude-klabauter-root.sh
(DoE clean-slate migration, sourced-lib variant — DoE .sh is left untouched; its ~60
`source coordinator-claude-klabauter-root.sh` callers switch to `import coordinator_core.engine_root`
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
    def coordinator_engine_root() -> str   — mirrors the shell function of the same name.
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
import sys
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

# Cross-reference: coordinator/bin/lib/cc_invoke.py defines this same literal
# (plan pln-the-ceremony-tail-stops-lying-b58fb3 AC3b). The two rungs sit on
# opposite sides of a declared one-way no-import boundary and cannot share a
# symbol; the constant is duplicated deliberately and each side asserts the literal.
_REGISTRY_READ_TIMEOUT_TOKEN = "machine-local registry read timed out"

_REMEDIATION = (
    "coordinator_engine_root: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set.\n"
    "  The machine-local registry has no 'repos.claude_klabauter' entry on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c"
)

#: Rung-3 remediation text for the timeout arm — distinguishable from `_REMEDIATION`
#: (the absent-key text) so a caller can tell "no entry" apart from "the read never
#: finished." Names the reader timeout, not `machine-local set`: setting a registry
#: key does nothing for a read that didn't get far enough to see it. Carries
#: `_REGISTRY_READ_TIMEOUT_TOKEN` (AC3b).
_TIMEOUT_REMEDIATION = (
    "coordinator_engine_root: cannot resolve CLAUDE_KLABAUTER_ROOT — "
    f"{_REGISTRY_READ_TIMEOUT_TOKEN}.\n"
    "  The `machine-local get repos.claude_klabauter` subprocess did not return within "
    f"{_RUNG2_TIMEOUT_SECS}s.\n"
    "  This is a hung/slow read, not a missing registry entry — re-run once the machine's "
    "load has settled.\n"
    "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c"
)


#: Process-scope memo for `coordinator_engine_root()`'s Rung 1.5/Rung 2 answer
#: (finding 8, staff-eng review; state/lessons/2026-07-06-tri-plane-read-ops-
#: must-process-memoize.yaml). Per-call resolution spawns a `machine-local`
#: subprocess on every dispatch, which blows the sub-10ms SLA of a warm,
#: repeatedly-dispatched process — the lesson requires resolving once and
#: memoizing at process scope. Naive SINGLE-SLOT memoization would be the
#: same missing-key COLLISION class C7 fixes for the two git-config caches:
#: a warm server can serve dispatches whose registry state changes mid-
#: process (`machine-local set` landing between two calls), and a bare
#: last-write-wins slot would silently serve a stale root to every caller
#: after that write. Keyed on `_registry_mtime_pair`'s cheap staleness tuple
#: instead, mirroring `_GATE_MEMO`'s shape below — a dict, not a Tuple pair
#: of module globals, so a registry mtime change invalidates only its own
#: key rather than colliding with whatever the last caller happened to see.
_ROOT_MEMO: dict = {}


def _reset_root_memo() -> None:
    """Test-only helper: clear the process-scope root-resolution memo."""
    _ROOT_MEMO.clear()


def coordinator_engine_root() -> str:
    """Resolve the claude-klabauter sibling-repo root via the documented chain.

    Returns the resolved absolute path (as read — no realpath/normalization beyond
    the source's own whitespace-strip, mirroring the bash oracle's behavior).
    Raises RuntimeError with the bash oracle's remediation text on failure.

    Rung 1.5/Rung 2's answer is memoized process-scope, keyed on
    `_registry_mtime_pair` (see `_ROOT_MEMO`) — resolved once per distinct
    registry state per process, not once globally and not once per call.
    """
    # Rung 1: CLAUDE_KLABAUTER_ROOT already set in environment (§4b idempotency gate).
    # Never memoized: this is a direct env read, already as cheap as a memo
    # lookup, and honoring a caller's env override on every call is the
    # entire point of the idempotency gate.
    existing = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if existing:
        return existing

    ml_dir = machine_local_dir()
    memo_key = _registry_mtime_pair(ml_dir)
    cached = _ROOT_MEMO.get(memo_key)
    if cached is not None:
        return cached

    # Rung 1.5: cheap direct-file-read pointer, checked ahead of the expensive
    # machine-local subprocess ladder. Absence/emptiness is a normal fallback
    # state, not an error — falls through to Rung 2.
    pointer_path = ml_dir / ".claude-klabauter-root"
    try:
        with open(pointer_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            _ROOT_MEMO[memo_key] = val
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
            # Distinct disposition from the exec failure above (AC4): the read never
            # finished, so `machine-local set` remediation is actively wrong advice —
            # there's no evidence the key is absent, only that the subprocess didn't
            # answer in time. The bound exists because this resolver is reached from
            # PreToolUse hook paths on the interactive critical path — an unbounded
            # wait there hangs the tool call rather than degrading to Rung 3's
            # actionable error, and a slow answer is worth less than a prompt one.
            raise RuntimeError(_TIMEOUT_REMEDIATION) from None

        if result is not None and result.returncode == 0:
            resolved = result.stdout.strip()
            if resolved:
                _ROOT_MEMO[memo_key] = resolved
                return resolved

    # Rung 3: hard error with actionable remediation. Deliberately NOT
    # memoized — a transient absent-key/timeout state must not pin every
    # later call in the process to the same hard failure once the registry
    # is fixed up (e.g. `machine-local set` landing after this call).
    raise RuntimeError(_REMEDIATION)


# --- DR-132 two-tier gate wrapper -----------------------------------------
#
# `coordinator_engine_root_with_class()` below wraps C3's shim
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
# docstring), and every code path in `coordinator_engine_root_with_class()`
# below that can produce that class routes through the SAME loaded shim
# instance's `resolve_claude_klabauter_root_with_class()` — so the shim's own
# once-per-process advisory (module-scope flag, `_reset_skew_advisory()`)
# already covers this wrapper's callers without a second implementation.
# `_reset_skew_advisory()` below is a thin test-seam passthrough, not a
# parallel check. Do not add a duplicate stderr write here — it would
# double the advisory for every in-process caller that goes through this
# wrapper. Spec backlink: pln-two-tier-engine-root-resolutio-024269 § C5

#: `coordinator_core/engine_root.py`'s parent-of-parent is the claude-klabauter repo
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
#: wrapper's Cheap-short-circuit step (see `coordinator_engine_root_with_class`
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
#: `coordinator_core/tests/test_engine_root_two_tier.py`'s cross-entrypoint
#: agreement test: it drives a fixture with `repos.claude_klabauter` ABSENT,
#: so this wrapper takes the short-circuit while the shim runs its own full
#: ladder, and both are asserted to still agree. A future step-order change
#: that breaks that agreement fails there, not only in prose.
#:
#: C5 (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md) changed
#: the step-1 precondition again: `_resolve_published_engine` now ALSO
#: requires a valid engine build stamp ("an engine root is a stamped build.
#: No stamp, no engine.") before treating `repos.claude_klabauter` as
#: usable. This short-circuit's own precondition — `repos.claude_klabauter`
#: is REGISTERED AT ALL (a bare `_registry_value` read, never
#: `_resolve_published_engine`) — is unaffected: it decides only whether the
#: gate's published-engine branches can fire, not whether they succeed, so
#: a registered-but-unstamped root still takes the full-gate path at step 3
#: below and correctly fails to resolve there rather than short-circuiting
#: past the stamp check. Recorded here per the obligation above rather than
#: left implicit.

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
            f"coordinator_engine_root_with_class: shim not found at '{_SHIM_PATH}' — "
            "broken or partial claude-klabauter checkout."
        )
    spec = importlib.util.spec_from_file_location("_claude_klabauter_root_gate_shim", _SHIM_PATH)
    if spec is None or spec.loader is None:
        # Review: code-reviewer — a bare `assert` here is stripped under
        # `python -O`/PYTHONOPTIMIZE, degrading this fail-loud check to an
        # unguarded AttributeError two lines below; an explicit raise
        # survives an optimized run.
        raise RuntimeError(
            f"coordinator_engine_root_with_class: could not build an import spec "
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
#: cases.
#:
#: PER-KEY DICT, not a single-entry `(key, value)` pair (C10, staff-eng
#: review finding 8): a warm server serves dispatches from DIFFERENT
#: session roots interleaved in one process, and a bare last-write-wins
#: slot is the same missing-key COLLISION class C7 fixes for the two
#: git-config caches — session A's memo entry was evicted outright by
#: session B's call, so A's next call re-walked the full gate even though
#: its own registry state had not changed. The key space is bounded in
#: practice (distinct registry states × distinct session roots actually
#: served by one long-lived process), so an unbounded dict does not
#: meaningfully grow unbounded the way e.g. a per-request cache would.
_GATE_MEMO: "dict[Tuple[float, float, float, Optional[str]], Tuple[str, str]]" = {}

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
#:       simultaneously for the SAME key can both miss the memo and both
#:       pay the full walk cost once. Benign — accepted, not fixed — but
#:       worth knowing if this module is ever called from a threaded server
#:       context (the module docstring's "~30 long-lived-process call
#:       sites" language does not itself rule that out).


def _reset_gate_memo() -> None:
    """Test-only helper: clear the two-tier gate answer memo.

    Mirrors `coordinator_core.ops.coordinator_doe_root._reset_doe_root_cache`.
    Exists because the memo is interpreter-lifetime state: under pytest the
    first test to resolve would otherwise pin the answer for every later
    test in the same process.
    """
    _GATE_MEMO.clear()


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


def coordinator_engine_root_with_class() -> Tuple[str, str]:
    """Resolve the claude-klabauter root AND the DR-132 resolution class alongside it.

    Wraps C3's `resolve_claude_klabauter_root_with_class()` shim — does NOT
    reimplement the published-engine-vs-live-working-tree gate. Returns
    `(root, resolution_class)` where the class is one of the shim's
    `RESOLUTION_RESOLVED_ENGINE` / `RESOLUTION_LIVE_WORKING_TREE` /
    `RESOLUTION_UNRESOLVED` string constants.

    HOT-PATH SHAPE (do not "simplify" away — see plan § C4 wrapper half):
      1. Rung 1 (`CLAUDE_KLABAUTER_ROOT` env var) — `coordinator_engine_root()`'s
         existing free rung, re-checked here so this function never runs
         the gate ahead of it. Classifies as `RESOLUTION_LIVE_WORKING_TREE`:
         it is the SAME resolution `coordinator_engine_root()` already
         performs today, just with a class label attached; it predates and
         is unaffected by the two-tier gate.
      2. Cheap short-circuit: if `repos.claude_klabauter` (the published
         engine mirror key) is not registered at all, the gate's step 1/3
         (published-engine branches) can never fire — skip straight to the
         shim's own live-tree resolution (`_resolve_claude_klabauter_root`, which
         itself reads the `.claude-klabauter-root` pointer as ITS OWN rung 2) rather
         than paying for the full `_is_claude_klabauter_source_tree` session-root
         walk the gate would otherwise do first (2026-08-18, C4: this
         replaced the retired per-repo `_is_engine_working_repo` gate with
         a structural session-root-vs-live-root comparison; the short-circuit
         here is unaffected either way — it still skips the walk entirely).
         THIS branch is where Rung 1.5's `.claude-klabauter-root` pointer fast path
         now lives — checked here, ahead of the full gate walk, so the
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
    `coordinator_engine_root()`'s `RuntimeError`/`_REMEDIATION` shape; the
    two error types are distinct because they come from distinct call
    chains (see `coordinator_engine_root()`'s own remediation vs. the
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

    cached = _GATE_MEMO.get(memo_key)
    if cached is not None:
        return cached

    result = shim.resolve_claude_klabauter_root_with_class()
    _GATE_MEMO[memo_key] = result
    return result


# --- C10: dual-read env accessor for the engine-root variable rename -------
#
# Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md
# § "Design decision — the variable's new name" / § C10.
#
# WHY THIS EXISTS: the module rename (C1-C9) is atomic within one process; the
# ENV VAR rename cannot be, because the variable crosses a process boundary
# between parents and children running from trees at potentially different
# published versions. `coordinator_engine_root_env()` and
# `coordinator_engine_root_env_exports()` are the ONE seam every reader/writer
# routes through, so the eventual one-line rename (C14) does not require a
# second 256-site sweep.
#
# NEGATIVE SPEC — the dual-read fallback is a TIME-BOXED WINDOW, NOT A SHIM:
#   - The old name (`CLAUDE_KLABAUTER_ROOT`) is never republished as new API — it is
#     only ever READ here, never the spelling anything is told to write.
#   - The window closes in C14, once a publish round converges the live tree
#     and the published mirror on the new name. A reader that treats this
#     fallback as permanent, or adds a third name to the ladder, is doing the
#     one thing this window is explicitly not for.
#   - `_ENGINE_ROOT_FALLBACK_EMITTED`/`_ENGINE_ROOT_CONFLICT_EMITTED` exist so
#     C14's exit condition is evidence ("no reading site has hit the fallback
#     in N days"), not an unverifiable claim.
_ENGINE_ROOT_NEW_VAR = "COORDINATOR_ENGINE_ROOT"
_ENGINE_ROOT_OLD_VAR = "CLAUDE_KLABAUTER_ROOT"

#: Per-reading-site once-per-process emission memo (fallback-used case) —
#: keyed on the caller-supplied `site` tag so two distinct reading sites each
#: get their own single emission, mirroring `_skew_advisory_emitted`'s
#: once-per-process shape in the shim above but per-key rather than global.
_ENGINE_ROOT_FALLBACK_EMITTED: "set[str]" = set()

#: Once-per-process emission memo (both-set-and-disagree case) — global, not
#: per-site: the disagreement is a property of the environment snapshot, not
#: of which site happened to read it first.
_ENGINE_ROOT_CONFLICT_EMITTED = False


def _reset_engine_root_env_advisories() -> None:
    """Test-only helper: clear both once-per-process emission memos."""
    global _ENGINE_ROOT_CONFLICT_EMITTED
    _ENGINE_ROOT_FALLBACK_EMITTED.clear()
    _ENGINE_ROOT_CONFLICT_EMITTED = False


def coordinator_engine_root_env(site: str) -> Optional[str]:
    """Read accessor for the engine-root env var during the dual-read window.

    Prefers `COORDINATOR_ENGINE_ROOT`; falls back to `CLAUDE_KLABAUTER_ROOT` only if the
    new name is unset. Returns `None` if neither is set — deliberately
    unchanged from today's behaviour (this accessor does not invent a value
    the caller didn't have before).

    `site` tags the reading call site for the fallback-used advisory below —
    pass a short stable identifier (e.g. the calling module's `__name__`),
    not a per-invocation value.

    PRECEDENCE IS LOAD-BEARING: the new name wins whenever both are set — a
    stale `CLAUDE_KLABAUTER_ROOT` inherited from an ancestor process must never
    override a fresh `COORDINATOR_ENGINE_ROOT` set by the immediate parent.

    See module-level "C10: dual-read env accessor" block for the negative
    spec on why this fallback is a time-boxed window (closed by C14), not a
    permanent shim.
    """
    new_val = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")
    old_val = os.environ.get(_ENGINE_ROOT_OLD_VAR, "")

    if new_val and old_val and new_val != old_val:
        _maybe_emit_engine_root_conflict(new_val, old_val)

    if new_val:
        return new_val
    if old_val:
        _maybe_emit_engine_root_fallback(site)
        return old_val
    return None


def _maybe_emit_engine_root_fallback(site: str) -> None:
    """Emit the fallback-used advisory (stderr, once per `site` per
    process) — fires whenever `CLAUDE_KLABAUTER_ROOT` is what answered."""
    if site in _ENGINE_ROOT_FALLBACK_EMITTED:
        return
    _ENGINE_ROOT_FALLBACK_EMITTED.add(site)
    print(
        f"coordinator_engine_root_env[{site}]: read via CLAUDE_KLABAUTER_ROOT fallback "
        "(dual-read window, closes C14).",
        file=sys.stderr,
    )


def _maybe_emit_engine_root_conflict(new_val: str, old_val: str) -> None:
    """Emit the both-set-and-disagree advisory (stderr, once per process) —
    fires whenever `COORDINATOR_ENGINE_ROOT` and `CLAUDE_KLABAUTER_ROOT` are both set
    to different values, naming both and which won."""
    global _ENGINE_ROOT_CONFLICT_EMITTED
    if _ENGINE_ROOT_CONFLICT_EMITTED:
        return
    _ENGINE_ROOT_CONFLICT_EMITTED = True
    print(
        f"coordinator_engine_root_env: COORDINATOR_ENGINE_ROOT={new_val!r} "
        f"CLAUDE_KLABAUTER_ROOT={old_val!r} disagree — COORDINATOR_ENGINE_ROOT wins.",
        file=sys.stderr,
    )


def coordinator_engine_root_env_exports(value: str) -> dict:
    """Write helper: the dict of env vars to export for `value`.

    During the dual-read window this sets BOTH `COORDINATOR_ENGINE_ROOT` and
    `CLAUDE_KLABAUTER_ROOT` to the same value, so a child running from a pre-rename
    mirror (reading only the old name) and a child running from a
    post-rename tree (reading the new name) both resolve correctly. C14
    drops the old key once a publish round converges both trees on the new
    name — see the module-level "C10" block above.
    """
    return {_ENGINE_ROOT_NEW_VAR: value, _ENGINE_ROOT_OLD_VAR: value}


# --- C18: the two axes get two variables ----------------------------------
#
# Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C18.
# Canonical axis definition: docs/decisions/DR-326.
#
# THE DEFECT THIS ADDRESSES. One variable has been answering two questions:
#
#   DISPATCH  "which engine executes?"        -> COORDINATOR_ENGINE_ROOT
#   LOCATOR   "where is the source checkout?" -> COORDINATOR_ENGINE_SOURCE_ROOT
#
# They coincide only on a box whose engine IS the live tree. On a stamped-mirror
# box -- the direction this workstream is moving -- they differ, and a locator
# consumer reading the dispatch answer is handed a build output where it wanted
# a working tree. DR-326's 2026-08-19 refinement is explicit that the old name
# dies on the DISPATCH axis.
#
# ON THE OLD NAME'S FATE, corrected 2026-08-20: that refinement also said
# CLAUDE_KLABAUTER_ROOT SURVIVES on the locator axis. It does not. DR-326's 2026-08-20
# amendment supersedes that clause on PM ruling -- the name is eliminated
# outright and no axis inherits it, so the locator axis gets its own token-free
# spelling (COORDINATOR_ENGINE_SOURCE_ROOT) rather than the legacy one. The
# dispatch/locator SPLIT the refinement draws is untouched and is what this
# block implements; only which spelling survives changed.
#
# NEGATIVE SPEC -- THE INVARIANT THAT MAKES THIS LANDABLE:
# **THE EXISTING VARIABLE NEVER CHANGES MEANING. THE LOCATOR EXPORT IS PURELY
# ADDITIVE.** C10's window is a RENAME window: old and new names carry the SAME
# value, so a fallback read is always correct. C18 is a SEMANTIC SPLIT: afterwards
# there are two facts, and a fallback read is correct for only one of them. If
# this changed what the existing variable means, an unrouted locator consumer in
# the published mirror, the deployed settings home, or DoE-claude would silently
# start getting a different answer -- four parties x two MEANINGS, which is not
# landable in one plan or in ten. Additive-only makes it four parties x two
# VARIABLES: a consumer that ignores the new one behaves exactly as it does today.
#
# DO NOT collapse the two axes "because they are usually the same path". That
# they are usually equal is what makes the divergence dangerous, not what makes
# it safe.
#
# NAME RATIONALE, held to the same discipline as C10's:
#   COORDINATOR_ENGINE_SOURCE_ROOT
#     - No repo token, so the publish depersonalization transform is a no-op on
#       it and both trees ship one spelling -- the property that made
#       `engine_root.py` and `COORDINATOR_ENGINE_ROOT` correct.
#     - Shares the `COORDINATOR_ENGINE_` stem with the dispatch variable, so the
#       pair reads as two facts about one thing rather than two unrelated knobs.
#     - `SOURCE` is the discriminator that carries the axis: a source checkout
#       versus a built engine.
#   Rejected: `COORDINATOR_SOURCE_ROOT` (ambiguous with the CONSUMING project's
#   source, which is what most callers mean by "source root");
#   `COORDINATOR_CHECKOUT_ROOT` (same ambiguity, and "checkout" names a git
#   operation rather than the thing); `CLAUDE_KLABAUTER_ROOT` retained as the locator name
#   (carries the repo token the PM ruling removes, and the transform rewrites it).
_ENGINE_SOURCE_ROOT_VAR = "COORDINATOR_ENGINE_SOURCE_ROOT"

#: Once-per-process, per-site memo for the locator-axis misread advisory.
#: C18's exit evidence is "no consumer read the dispatch variable on the locator
#: axis in N days" -- the same observability shape as C10's AC24, one axis over,
#: and it needs its OWN window because C10's closes on a rename converging while
#: this one closes on consumers being routed.
_LOCATOR_MISREAD_EMITTED: "set[str]" = set()


def _reset_locator_axis_advisories() -> None:
    """Test-only helper: clear the locator-misread memo."""
    _LOCATOR_MISREAD_EMITTED.clear()


def coordinator_engine_source_root_env(site: str) -> Optional[str]:
    """Read accessor for the LOCATOR axis -- where the source checkout is.

    Returns `COORDINATOR_ENGINE_SOURCE_ROOT` when set. Falls back to the
    dispatch variable ONLY so an unrouted caller keeps working during the
    transition, and emits once per `site` when it does -- because that fallback
    is exactly the misread C18 exists to retire, and C18's exit condition is
    evidence that it stopped happening rather than an assertion that it did.

    Returns None when neither is set: this accessor does not invent a checkout.
    """
    own = os.environ.get(_ENGINE_SOURCE_ROOT_VAR, "")
    if own:
        return own
    shared = os.environ.get(_ENGINE_ROOT_NEW_VAR, "") or os.environ.get(_ENGINE_ROOT_OLD_VAR, "")
    if not shared:
        return None
    _maybe_emit_locator_misread(site)
    return shared


def _maybe_emit_locator_misread(site: str) -> None:
    """Emit the locator-axis misread advisory (stderr, once per `site`)."""
    if site in _LOCATOR_MISREAD_EMITTED:
        return
    _LOCATOR_MISREAD_EMITTED.add(site)
    print(
        f"coordinator_engine_source_root_env[{site}]: no "
        f"{_ENGINE_SOURCE_ROOT_VAR}; answered from the DISPATCH variable, which "
        "names the executing engine and may be a published mirror rather than a "
        "source checkout.",
        file=sys.stderr,
    )


def coordinator_engine_source_root_exports(source_root: Optional[str]) -> dict:
    """Write helper: the locator-axis export, or `{}` when unresolvable.

    ADDITIVE BY CONSTRUCTION -- this returns only the locator key and never the
    dispatch keys, so a caller merging it into an env cannot alter what the
    dispatch variable means. Returning `{}` rather than raising is deliberate: a
    box with no registered source checkout must keep spawning children exactly as
    it does today.
    """
    if not source_root:
        return {}
    return {_ENGINE_SOURCE_ROOT_VAR: source_root}


def published_engine_mirror_path() -> Optional[str]:
    """Return the registered `repos.claude_klabauter` published-engine-mirror
    path, or ``None`` if it is not registered/usable — the SAME
    "registered and on-disk usable" check `coordinator_engine_root_with_class`
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
