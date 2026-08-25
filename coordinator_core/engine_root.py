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

Resolution chain (rung 1 renamed by C14; the rest unchanged from the bash oracle):
  1. COORDINATOR_ENGINE_ROOT env var — if already set, return it unchanged. The
     retired CLAUDE_KLABAUTER_ROOT is read at this rung only to report itself as retired
     (see `coordinator_engine_root_env`); it never supplies a value.
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
from coordinator_core.win_portability import same_path

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
    "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c"
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
    "  Reference: plugins/coordinator/docs/wiki/machine-local-registry.md §4c"
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
    # Rung 1: engine root already set in environment (§4b idempotency gate).
    # Never memoized: this is a direct env read, already as cheap as a memo
    # lookup, and honoring a caller's env override on every call is the
    # entire point of the idempotency gate.
    #
    # READ THROUGH THE C10 ACCESSOR, NEVER THE RAW NAME. A literal
    # "CLAUDE_KLABAUTER_ROOT" here is rewritten by the publish transform, which splits
    # env-var names as readily as module names — so the mirror's copy of this
    # rung looks for CLAUDE_KLABAUTER_ROOT and can never see the CLAUDE_KLABAUTER_ROOT a
    # live-tree caller actually exported. That made Rung 1 inert across the
    # tree boundary, which is precisely the DR-326 case it exists to serve,
    # and the warm server exporting the mirror's root fleet-wide makes the
    # crossing the common path on this box rather than the edge one.
    # COORDINATOR_ENGINE_ROOT is transform-stable, so the accessor crosses
    # intact where the raw name cannot. Surfaced by claude-klabauter-ff,
    # 2026-08-20, reproduced under a synthetic HOME.
    existing = coordinator_engine_root_env("engine_root.coordinator_engine_root") or ""
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

#: Rung 1's honest answer. An environment hit resolves a PATH for free, but it
#: carries no evidence about WHICH tree that path is: the engine-root variable
#: is inherited from whichever process exported it, and on a co-located box the
#: warm server exports the published mirror's own root into the environment it
#: serves from. Classifying that as `live-working-tree` — as this rung did
#: until 2026-08-21 — is an unchecked assertion, and it silently defeated
#: `state_root.py`'s published-mirror guard for every writer routed through it
#: (state/audits/2026-08-21-transform-resolved-writer-inventory.md).
#:
#: The fix is NOT to run the gate here: that would cost a shim load plus a
#: registry read on a rung whose whole purpose is to be free, on a hot path
#: every engine import pays. Instead the rung reports that it does not know,
#: and the only two consumers that branch on the class — both in
#: `state_root.py`, both on the state-WRITE path, neither hot — pay for
#: `classify_env_resolved_root()` at the moment the answer actually matters.
#: Every other caller in the tree discards the class (`_cls`,
#: `_resolution_class`) and is unaffected by this value.
_RESOLUTION_UNVERIFIED_ENV_LITERAL = "unverified-env"

#: The mirror-side answer `classify_env_resolved_root()` returns. Hardcoded
#: rather than imported from the shim for the same reason as the literal
#: above it: value is contract, and if the shim's constant changes this must
#: change with it. Kept identical to `state_root.py`'s own copy of this
#: string, which is the only consumer that compares against it.
_RESOLUTION_RESOLVED_ENGINE_LITERAL = "resolved-engine"

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


#: The registry key naming the LIVE engine source tree. Deliberately NOT
#: repo-named: the publish transform rewrites repo identifiers, so a key like
#: `repos.claude_klabauter` becomes `repos.claude_klabauter` in the published
#: engine and resolves that engine to ITSELF -- the mechanism that lost two
#: working files into the mirror (state/bug-backlog/2026-08-20-central-scope-
#: queue-entries-land-in-the-6a0c80dedc44.yaml). None of `engine`, `source` or
#: `root` is a repo token, so this spelling survives publish byte-identical.
#: The mirror shipping its own `coordinator_core/engine_root.py` under that
#: exact name is the standing proof.
_ENGINE_SOURCE_ROOT_KEY = "engine.source_root"


def engine_source_root() -> Optional[str]:
    """The live engine SOURCE tree, resolved through a transform-proof key.

    Answers a different question from `coordinator_engine_root()`. That one
    asks "which engine is THIS process running?" — and for a process running
    out of the published mirror, the mirror is the correct answer. This asks
    "where does engine-owned working substrate belong?", whose answer is the
    live source tree no matter which copy of the engine is asking.

    Returns None when the key is unregistered, which is the normal state on a
    consumer install: there is one engine repo, it is a real checkout, and the
    existing repo-named ladder already resolves it correctly. Callers fall
    back to that ladder rather than treating None as an error.

    NOT `coordinator_engine_source_root_env()`, in this same module. That is the
    LOCATOR axis's read accessor and names the same concept in English, which
    makes the pair easy to confuse — but it reads an ENVIRONMENT variable, and a
    process-inherited value is the precise hazard this whole slate exists to
    close: the warm server exports its own root into the environment it serves
    from, which is how a mirror came to be labelled a live working tree. Write
    routing must resolve off disk, where no other process's inheritance can
    reach it. Same words, opposite trust model; do not collapse them.

    NEGATIVE SPEC — do not put this on `coordinator_engine_root()`'s ladder.
    Import resolution, `sys.path` setup and the warm-serving hot path all want
    the engine that is actually executing; substituting the source tree there
    would make a published engine import a different tree than the one it
    shipped from. This key is for WRITE routing only.
    """
    try:
        shim = _load_shim()
        value = shim._registry_value(shim._ml_dir(), _ENGINE_SOURCE_ROOT_KEY)
    except Exception:
        return None
    value = (value or "").strip()
    if not value or is_published_engine_mirror(value):
        # A key pointed at the mirror is the very confusion this exists to
        # end; refuse to launder it into a "correct" answer.
        return None
    return value


def is_published_engine_mirror(root: str) -> bool:
    """True when ``root`` IS the registered published engine mirror.

    The one predicate for "am I about to treat a build artifact as a working
    tree". Resolves the mirror via `published_engine_mirror_path()` (registered
    AND on-disk usable AND stamped) and compares with
    `win_portability.same_path`, this plane's single path-equality primitive —
    a junction hop to the same directory must compare equal, which a
    normcase/realpath comparison can miss on Windows.

    Fail-open: no registered mirror, or an unreadable registry, means there is
    no mirror to confuse this root with, so the answer is False. Callers use
    this to REFUSE, and a refusal invented out of an unrelated registry hiccup
    would be worse than the read it is guarding.
    """
    mirror = published_engine_mirror_path()
    if not mirror:
        return False
    return same_path(root, mirror)


def classify_env_resolved_root(root: str) -> str:
    """Resolve the class Rung 1 deliberately does not compute.

    `coordinator_engine_root_with_class()`'s Rung 1 returns
    `_RESOLUTION_UNVERIFIED_ENV_LITERAL` because an environment hit proves a
    path and nothing else. This pays the cost that rung refuses to: it asks
    whether that path IS the published engine mirror, via
    `published_engine_mirror_path()` — the same "registered and on-disk
    usable" check the full gate uses, so this does not re-derive the
    `repos.claude_klabauter` read (see this module's docstring,
    "single-implementation property").

    Call this ONLY where the distinction changes behaviour — in practice the
    state-WRITE path in `state_root.py`. It costs a shim load; putting it
    back on the resolution hot path is the exact regression Rung 1 exists to
    avoid.

    NEGATIVE SPEC — this is a mirror check, not a working-tree proof. A path
    that is not the published mirror classifies as a live working tree, which
    is the answer Rung 1 asserted unconditionally before; the change is that
    the mirror case is now excluded rather than assumed away. No registered
    mirror means no mirror to confuse this root with, so a single-tree box
    keeps the live-tree answer by construction rather than by luck. Fail-open
    on an unreadable registry, inheriting `published_engine_mirror_path()`'s
    contract: the guard's job is to catch the mirror, not to make every
    registry hiccup unwritable.
    """
    if is_published_engine_mirror(root):
        return _RESOLUTION_RESOLVED_ENGINE_LITERAL
    return _RESOLUTION_LIVE_WORKING_TREE_LITERAL


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
         the gate ahead of it. Resolves the SAME path
         `coordinator_engine_root()` already returns today, and classifies
         it `_RESOLUTION_UNVERIFIED_ENV_LITERAL` — an env hit proves a path
         and nothing about which tree it is. Callers that need the
         distinction call `classify_env_resolved_root()`; see that constant's
         comment for why the check is not run here.
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
    # Through the C10 accessor, not the raw name — same reason as
    # `coordinator_engine_root`'s Rung 1 above: the publish transform rewrites
    # a literal "CLAUDE_KLABAUTER_ROOT" and the mirror's copy of this rung then cannot
    # see what a live-tree caller exported. This is the site `cc_invoke`'s
    # `_delegate_to_gate` reaches when it loads a MIRROR candidate's gate, so
    # a raw read here is what sent that path falling through to the
    # machine-local registry — the dependency Rung 1 exists to remove.
    existing = coordinator_engine_root_env(
        "engine_root.coordinator_engine_root_with_class"
    ) or ""
    if existing:
        return existing, _RESOLUTION_UNVERIFIED_ENV_LITERAL

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
    """Read accessor for the engine-root env var. The dual-read window is CLOSED.

    Answers from `COORDINATOR_ENGINE_ROOT` only. `CLAUDE_KLABAUTER_ROOT` is still READ
    but never RETURNED: a set-but-retired old name produces the retired
    advisory and a census row, then `None`. Returns `None` if neither is set —
    this accessor does not invent a value the caller didn't have before.

    `site` tags the reading call site for the advisories below — pass a short
    stable identifier (e.g. the calling module's `__name__`), not a
    per-invocation value.

    PRECEDENCE IS LOAD-BEARING WHILE BOTH ARE SET: the new name wins, and the
    disagreement advisory fires. A stale `CLAUDE_KLABAUTER_ROOT` inherited from an
    ancestor process must never override a fresh `COORDINATOR_ENGINE_ROOT` set
    by the immediate parent.

    NEGATIVE SPEC — WHAT THIS SEAM DOES NOT COVER. Closing the window here did
    NOT retire the old name across the engine, and reading this docstring as if
    it did is the error a review caught on 2026-08-20. Eight `ops/` modules
    still read `CLAUDE_KLABAUTER_ROOT` directly through a module-local `_CLAUDE_KLABAUTER_ROOT_ENV`
    and never reach this function, and three sites still export it to children.
    Until those are routed or carved out, "the old name no longer answers"
    is true of THIS SEAM and false of the engine.

    See module-level "C10: dual-read env accessor" block for the negative
    spec on why the fallback was a time-boxed window (closed by C14), not a
    permanent shim.
    """
    new_val = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")
    old_val = os.environ.get(_ENGINE_ROOT_OLD_VAR, "")

    if new_val and old_val and new_val != old_val:
        _maybe_emit_engine_root_conflict(new_val, old_val)

    if new_val:
        return new_val
    if old_val:
        # C14 CLOSED THE WINDOW: the old name no longer ANSWERS. It is still
        # READ, for one reason — to say so. Returning None silently here would
        # turn an operator's stale pin into a resolution failure several rungs
        # downstream, reported against whatever surface happened to need the
        # root; naming it at the point of the stale read is the difference
        # between a named cause and a session spent bisecting.
        _maybe_emit_engine_root_retired(site, old_val)
    return None


def _maybe_emit_engine_root_retired(site: str, root_value: str = "") -> None:
    """Emit the old-name-is-retired advisory (stderr, once per `site` per
    process) and append the same observation to the durable census.

    C14 CHANGED WHAT THIS MEANS, and the change is the point. Before C14 this
    fired when `CLAUDE_KLABAUTER_ROOT` ANSWERED, and the census existed to evidence that
    nothing was reading it any more so the window could close. C14 closed the
    window on the other three precondition items instead — the mirror ships the
    new name, the deployed settings-home copies were re-provisioned and
    validated live, and the sibling consumers acknowledged — so the old name
    now answers NOTHING. This advisory therefore fires on a read that no longer
    resolves: an operator or an ancestor process still exporting a name the
    engine has retired.

    That inverts the census from *evidence a window may close* into *a
    regression detector for a stale pin*, which is the residual risk the
    close-without-a-soak deliberately accepted. It is the more useful of the
    two: a non-zero count here is now actionable at the point of the stale
    read, naming both the site and the value, rather than surfacing several
    rungs downstream as an unresolvable-root failure against whatever surface
    happened to need it first.

    The census import is LAZY and the call is WRAPPED: this runs on the
    `scoped-git-commit` hot path, and an observability write that can raise
    here turns every ceremony on this box into an outage. A process with no
    stale pin never imports the census at all.
    """
    if site in _ENGINE_ROOT_FALLBACK_EMITTED:
        return
    _ENGINE_ROOT_FALLBACK_EMITTED.add(site)
    print(
        f"coordinator_engine_root_env[{site}]: {_ENGINE_ROOT_OLD_VAR} is set but "
        f"is NO LONGER HONOURED — the dual-read window closed (C14). "
        f"Export {_ENGINE_ROOT_NEW_VAR} instead.",
        file=sys.stderr,
    )
    try:
        from coordinator_core.engine_root_census import record_fallback_read

        record_fallback_read(site, root_value=root_value)
    except Exception:
        pass


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

    C14 CLOSED THE DUAL-WRITE WINDOW: this now exports the NEW NAME ONLY.

    Until C14 it set both names, so a child running from a pre-rename mirror
    (reading only the old name) and one from a post-rename tree both resolved.
    That is no longer needed and is no longer harmless: continuing to export
    the old name is what KEEPS a stale reader working, and therefore what kept
    the precondition open — the 26 fallback reads measured on 2026-08-20 all
    traced to the old name being exported or pinned, never to a consumer that
    could not have used the new one.

    Closed on the other three precondition items rather than on a soak: the
    published mirror ships the new name (its own fallback is the transformed
    `CLAUDE_KLABAUTER_ROOT`, never `CLAUDE_KLABAUTER_ROOT`), the deployed settings-home
    copies were re-provisioned and validated by live execution under the new
    name alone, and the sibling consumers acknowledged — DoE's PM ruled the old
    name goes rather than being tolerated to the end of the window.

    The accessor still READS the old name, solely to name it as retired; see
    `_maybe_emit_engine_root_retired`. That is the residual-risk net this
    close deliberately trades the soak for.
    """
    return {_ENGINE_ROOT_NEW_VAR: value}


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

    The RETIRED name is not a rung here and never was a legitimate one. The C18
    block below records DR-326's 2026-08-20 amendment as "the name is eliminated
    outright and no axis inherits it"; a locator-axis fallback to it contradicted
    that ruling in the same file that states it. It was invisible to every
    precedence-ORDER check because it sat AFTER the new name, and it answered
    without routing through `_maybe_emit_engine_root_retired`, so the census sink
    built to observe exactly this could not see it.

    Returns None when neither is set: this accessor does not invent a checkout.
    """
    own = os.environ.get(_ENGINE_SOURCE_ROOT_VAR, "")
    if own:
        return own
    shared = os.environ.get(_ENGINE_ROOT_NEW_VAR, "")
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
