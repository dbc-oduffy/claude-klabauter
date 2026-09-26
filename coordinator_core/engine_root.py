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
  1.5. <settings-home>/machine-local/.claude-klabauter-live-root pointer file — a cheap direct-file-read,
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
      This note previously recorded a deliberate
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

_RUNG2_TIMEOUT_SECS = 2.0

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


#: memoizing at process scope. Naive SINGLE-SLOT memoization would be the
#: same missing-key COLLISION class C7 fixes for the two git-config caches:
#: instead, mirroring `_GATE_MEMO`'s shape below — a dict, not a Tuple pair
_ROOT_MEMO: dict = {}


def _reset_root_memo() -> None:
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
    # READ THROUGH THE C10 ACCESSOR, NEVER THE RAW NAME. A literal
    # "CLAUDE_KLABAUTER_ROOT" here is rewritten by the publish transform, which splits
    # rung looks for CLAUDE_KLABAUTER_ROOT and can never see the CLAUDE_KLABAUTER_ROOT a
    # COORDINATOR_ENGINE_ROOT is transform-stable, so the accessor crosses
    existing = coordinator_engine_root_env("engine_root.coordinator_engine_root") or ""
    if existing:
        return existing

    ml_dir = machine_local_dir()
    memo_key = _registry_mtime_pair(ml_dir)
    cached = _ROOT_MEMO.get(memo_key)
    if cached is not None:
        return cached

    pointer_path = ml_dir / ".claude-klabauter-live-root"
    try:
        with open(pointer_path, "r", encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            _ROOT_MEMO[memo_key] = val
            return val
    except OSError:
        pass

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
            result = None
        except subprocess.TimeoutExpired:
            raise RuntimeError(_TIMEOUT_REMEDIATION) from None

        if result is not None and result.returncode == 0:
            resolved = result.stdout.strip()
            if resolved:
                _ROOT_MEMO[memo_key] = resolved
                return resolved

    raise RuntimeError(_REMEDIATION)


#: any already-resolved CLAUDE_KLABAUTER_ROOT value.
_SHIM_PATH = Path(__file__).resolve().parent.parent / "coordinator" / "lib" / "resolve-claude-klabauter" / "_resolve_claude_klabauter.py"

#: `RESOLUTION_LIVE_WORKING_TREE` constant, deliberately: loading the shim
#: duplication is self-documenting; per the shim's own `RESOLUTION_*`
_RESOLUTION_LIVE_WORKING_TREE_LITERAL = "live-working-tree"

_RESOLUTION_UNVERIFIED_ENV_LITERAL = "unverified-env"

_RESOLUTION_RESOLVED_ENGINE_LITERAL = "resolved-engine"

#: This is a DECLARED exception, not general license: `coordinator_core`
#: plus `resolve_claude_klabauter_root_with_class` and the `RESOLUTION_*` constants —
#: is REGISTERED AT ALL (a bare `_registry_value` read, never

_shim_module: Optional[types.ModuleType] = None


def _load_shim() -> types.ModuleType:
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
        # `python -O`/PYTHONOPTIMIZE, degrading this fail-loud check to an
        raise RuntimeError(
            f"coordinator_engine_root_with_class: could not build an import spec "
            f"for shim at '{_SHIM_PATH}' — broken or partial claude-klabauter checkout."
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _shim_module = module
    return module


def _reset_shim_cache() -> None:
    global _shim_module
    _shim_module = None


def _reset_skew_advisory() -> None:
    if _shim_module is not None and hasattr(_shim_module, "_reset_skew_advisory"):
        _shim_module._reset_skew_advisory()


#: `coordinator_core.ops.coordinator_doe_root`'s DECISION REVERSAL shape
#: (module docstring § DECISION REVERSAL) — an explicit memo with a reset
#: review finding 8): a warm server serves dispatches from DIFFERENT
#: slot is the same missing-key COLLISION class C7 fixes for the two
_GATE_MEMO: "dict[Tuple[float, float, float, Optional[str]], Tuple[str, str]]" = {}


def _reset_gate_memo() -> None:
    _GATE_MEMO.clear()


def _registry_mtime_pair(ml_dir: Path) -> Tuple[float, float, float]:

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return -1.0

    return (
        _mtime(ml_dir / "registry.toml"),
        _mtime(ml_dir / "registry.local.toml"),
        _mtime(ml_dir / ".claude-klabauter-live-root"),
    )


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
        return None
    return value


def is_published_engine_mirror(root: str) -> bool:
    mirror = published_engine_mirror_path()
    if not mirror:
        return False
    # FUNCTION-LOCAL, matching this module's lazy `no_console_creationflags`
    from coordinator_core.win_portability import same_path

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
         itself reads the `.claude-klabauter-live-root` pointer as ITS OWN rung 2) rather
         than paying for the full `_is_claude_klabauter_source_tree` session-root
         walk the gate would otherwise do first (2026-08-18, C4: this
         replaced the retired per-repo `_is_engine_working_repo` gate with
         a structural session-root-vs-live-root comparison; the short-circuit
         here is unaffected either way — it still skips the walk entirely).
         THIS branch is where Rung 1.5's `.claude-klabauter-live-root` pointer fast path
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
         reads `.claude-klabauter-live-root` as its own rung inside the gate, so a working
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
    # a literal "CLAUDE_KLABAUTER_ROOT" and the mirror's copy of this rung then cannot
    existing = coordinator_engine_root_env(
        "engine_root.coordinator_engine_root_with_class"
    ) or ""
    if existing:
        return existing, _RESOLUTION_UNVERIFIED_ENV_LITERAL

    shim = _load_shim()
    ml_dir = shim._ml_dir()

    published_key = shim._registry_value(ml_dir, "repos.claude_klabauter")
    if not published_key:
        # directly — the latter does not honor `MACHINE_LOCAL_REGISTRY_DIR`,
        pointer_path = ml_dir / ".claude-klabauter-live-root"
        try:
            with open(pointer_path, "r", encoding="utf-8") as f:
                val = f.read().strip()
            if val:
                return val, _RESOLUTION_LIVE_WORKING_TREE_LITERAL
        except OSError:
            pass

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


# NEGATIVE SPEC — the dual-read fallback is a TIME-BOXED WINDOW, NOT A SHIM:
#   - The old name (`CLAUDE_KLABAUTER_ROOT`) is never republished as new API — it is
#   - `_ENGINE_ROOT_FALLBACK_EMITTED`/`_ENGINE_ROOT_CONFLICT_EMITTED` exist so
_ENGINE_ROOT_NEW_VAR = "COORDINATOR_ENGINE_ROOT"
_ENGINE_ROOT_OLD_VAR = "CLAUDE_KLABAUTER_ROOT"

_ENGINE_ROOT_FALLBACK_EMITTED: "set[str]" = set()

_ENGINE_ROOT_CONFLICT_EMITTED = False


def _reset_engine_root_env_advisories() -> None:
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


# THE DEFECT THIS ADDRESSES. One variable has been answering two questions:
#   DISPATCH  "which engine executes?"        -> COORDINATOR_ENGINE_ROOT
#   LOCATOR   "where is the source checkout?" -> COORDINATOR_ENGINE_SOURCE_ROOT
# dies on the DISPATCH axis.
# CLAUDE_KLABAUTER_ROOT SURVIVES on the locator axis. It does not. DR-326's 2026-08-20
# spelling (COORDINATOR_ENGINE_SOURCE_ROOT) rather than the legacy one. The
# NEGATIVE SPEC -- THE INVARIANT THAT MAKES THIS LANDABLE:
# **THE EXISTING VARIABLE NEVER CHANGES MEANING. THE LOCATOR EXPORT IS PURELY
# ADDITIVE.** C10's window is a RENAME window: old and new names carry the SAME
# value, so a fallback read is always correct. C18 is a SEMANTIC SPLIT: afterwards
# start getting a different answer -- four parties x two MEANINGS, which is not
# VARIABLES: a consumer that ignores the new one behaves exactly as it does today.
# NAME RATIONALE, held to the same discipline as C10's:
#   COORDINATOR_ENGINE_SOURCE_ROOT
#       `engine_root.py` and `COORDINATOR_ENGINE_ROOT` correct.
#     - Shares the `COORDINATOR_ENGINE_` stem with the dispatch variable, so the
#   Rejected: `COORDINATOR_SOURCE_ROOT` (ambiguous with the CONSUMING project's
#   `COORDINATOR_CHECKOUT_ROOT` (same ambiguity, and "checkout" names a git
#   operation rather than the thing); `CLAUDE_KLABAUTER_ROOT` retained as the locator name
_ENGINE_SOURCE_ROOT_VAR = "COORDINATOR_ENGINE_SOURCE_ROOT"

_LOCATOR_MISREAD_EMITTED: "set[str]" = set()


def _reset_locator_axis_advisories() -> None:
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
    try:
        shim = _load_shim()
        return shim._resolve_published_engine(shim._ml_dir())
    except Exception:
        return None
