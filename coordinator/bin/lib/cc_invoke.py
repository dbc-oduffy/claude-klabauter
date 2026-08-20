"""cc_invoke — Python-side transport for coordinator_core.invoke.

Port of: coordinator-core-invoke.sh (DoE c6d97219, 2026-07-22) — the bash transport's
fail-closed timeout/nonzero-exit/empty-stdout ladder and DEC-1..3 op-timeout budget
logic are mirrored here deliberately; several comments below note specific behavioral
parity points the port preserved.

Purpose: spawns `sys.executable -m coordinator_core.invoke <op> <params_json> --repo <repo_root>`
with a timeout cap; applies a fail-closed timeout/nonzero-exit/empty-stdout ladder,
then parses the
{jsonrpc,id,result} envelope that is coordinator_core.invoke's default (non---bare)
response shape (see DR-215 ref below). On the route() path, CLAUDE_KLABAUTER_ROOT is resolved ONCE via the native
_resolve_claude_klabauter_root ladder (env var → pointer file → coordinator_core.engine_root,
no bash subprocess anywhere) — forwarded to cc_invoke() via _claude_klabauter_root for both the
find_spec gate and the subprocess env (single resolution source).

Public API:
    resolve_colocated_claude_klabauter_root(script_file) -> str
        Self-location-first CLAUDE_KLABAUTER_ROOT resolution for a CLI that lives INSIDE the
        engine checkout (coordinator/bin/*.py). Tries Path(script_file)'s
        parents[2] first (probed against coordinator_core/ + pyproject.toml markers);
        falls back to _resolve_claude_klabauter_root()'s machine-local registry ladder only
        when that probe misses (the published/vendored-outside-the-checkout case).

    cc_invoke(op, params, repo_root) -> dict
        Returns the bare result dict on success (jsonrpc/id/result wrapper stripped).
        Raises RuntimeError on ANY transport failure (timeout / ImportError / empty stdout /
        bad envelope / op-error envelope) — except a structural contract-pin failure
        (engine rc=2), which raises the distinct StructuralPinError subclass instead.
        NEVER returns legacy after a native attempt. Uses the non-bare envelope-parse
        call convention (params via --params-file, ARG_MAX-immune — see cc_invoke's
        own docstring's Params transport note; NOT positional argv).

    cc_invoke_bare(op, params, repo_root) -> dict
        The shared Python promotion of the retired bash cc_invoke (see module Port of
        note): the --bare
        fail-closed ladder + the DEC-1..3 per-op timeout-budget logic, moved OUT of the
        shell transport so downstream facades can `from cc_invoke import cc_invoke_bare`
        instead of each inlining a local mirror of the shell --bare ladder (the campaign
        anti-goal). Spawns coordinator_core.invoke with --bare (engine emits the bare
        result object directly) and --params-file (ARG_MAX-immune on Windows/msys), with a
        per-op timeout ceiling resolved once-per-process from the engine's op-budget dump.
        Shares the timeout/nonzero-exit/empty-stdout fail-closed rungs with cc_invoke() via
        _raise_on_process_failure — one ladder, two call conventions. Returns the bare
        result dict; raises RuntimeError on any transport failure, or StructuralPinError
        (a RuntimeError subclass) specifically on a structural contract-pin failure
        (engine rc=2).

    route(op, params, repo_root, legacy_fn)
        State-1 seam-absent (find_spec("coordinator_core.invoke") returns None with CLAUDE_KLABAUTER_ROOT
        on sys.path) → call and return legacy_fn(). No native spawn attempted.
        State-2 seam-present → cc_invoke(op, params, repo_root); propagate result or exception.
        Transport failure on the native path → raise (HARD error, never fall to legacy_fn).

    route_mutation(op, params, repo_root, legacy_fn)
        Mutation-aware sibling of route(): calls route(), then raises RouteMutationError
        if the returned dict carries a non-zero 'exit_code', a non-empty 'failed' list, or
        a non-empty string 'error' with exit_code absent/0 — the engine repo's op-level refusals
        live INSIDE the result payload with no top-level 'error' key at the ENVELOPE level,
        so bare route() would return them unraised. Python sibling of the shell transport's
        strangle_route_mutation (Port of: strangler-facade.sh, DoE c6d97219, 2026-07-22).

Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C1
DR-215 ref: coordinator_core/invoke/__main__.py's default (non---bare) response IS the
            {jsonrpc,id,result} envelope this module's cc_invoke() parses (--bare is
            opt-in server-side) — the envelope-parse convention is verified against the
            real engine's default response shape, not just this module's own fake test
            harness. The retired bash cc_invoke (Port of note, top of module) was the
            byte-oracle for the --bare/--params-file ladder mirrored by cc_invoke_bare(),
            not for this non-bare envelope-parse function.

Negative-spec (retired transport patterns — DO NOT reintroduce):
    - The coordinator_core client-module seam is retired (DR-215); this module does NOT
      import or use it.
    - Unix domain sockets are retired (DR-215); this module does NOT open one.
    - IPC authentication tokens are retired (DR-215); this module does NOT read one.
    - This is a TWO-STATE router (seam-present / seam-absent); there is no daemon-aware
      third state IN THIS ROUTER, and none should be added here. DR-315 (2026-08-15)
      authorizes a demand-driven warm engine process on the seam-present side of this
      same two-state split — that is a property of what coordinator_core.invoke's own
      process does once seam-present dispatch reaches it (a client-side pipe-first,
      spawn-on-FileNotFoundError decision inside the engine's own entry paths), not a
      third state this router discriminates on. route()'s State-1/State-2 shape is
      unchanged by DR-315 and stays two states.
    - find_spec is an INTENTIONAL improvement over the retired shell facade's full-import
      probe; do NOT replace it with an execute-import to "match" the old bash behavior.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Tuple, cast

GENERATES = []  # writes only tempfile.mkstemp() params files (cc_invoke + cc_invoke_bare), always unlinked; no tracked artifact


class StructuralPinError(RuntimeError):
    """Marks a non-self-healing structural contract-pin failure (engine rc=2 /
    JSON-RPC -32001), distinct from a generic transport failure.

    Raised by _raise_on_process_failure when the invoke process exits 2 — the
    engine's own discriminator between "structural pin broken, will not
    self-heal on retry" (rc=2, JSON-RPC STRUCTURAL_PIN_ERROR = -32001 per
    coordinator_core/ipc.py) and any other op error (rc=1, the plain
    RuntimeError fallthrough). Subclasses RuntimeError, so an existing
    `except RuntimeError` caller still catches it unchanged; callers that need
    to react differently to a structural pin catch StructuralPinError first.
    """


# ---------------------------------------------------------------------------
# Lazy op registration — armed ONCE here, at the shared trampoline seam, so
# every in-process trampoline (all 135 route through _resolve_claude_klabauter_root
# before `from coordinator_core.ops.<name> import main`) has it set before
# coordinator_core.ops is ever imported, killing the ~108ms eager op-module
# load on the cold-trampoline path. Module-top imports above this line are
# stdlib-only (importlib.util, json, os, subprocess, sys, tempfile, typing) —
# no coordinator_core import sits above this line; every coordinator_core
# import in this module is function-local.
# Spec backlink: DoE-claude:pln-decouple-coordinator-s-own-bin-42d50a § C8
#
# WHY A `sys` ATTRIBUTE AND NOT `os.environ` (2026-07-28). This used to be
# `os.environ.setdefault(_LAZY_OPS_ENV_KEY, "1")` — a PROCESS-environment
# mutation performed as an import side effect, which every subprocess a caller
# spawned afterwards inherited by default (subprocess.run/Popen with no
# explicit `env=` copies the live os.environ). That was correct for the
# caller's OWN in-process op import — the whole point of arming it — and wrong
# for every OTHER child the caller spawned: 59 test modules in this tree assert
# the op registry at import time, so a spawned pytest run saw a skipped
# eager-import and failed collection on a green tree (commit 5943ec01 patched
# one such site by hand; `child_env()` below generalised the strip). The
# variable was only ever needed as an in-process signal, so it now travels on
# `sys`, which no child inherits by any mechanism. Nothing but this process can
# observe it, which is exactly the scope the flag always wanted.
# Scoping study: docs/research/2026-07-28-lazy-ops-import-side-effect-scope.md § 6 (c).
#
# The conditional preserves the old `setdefault` semantics at this seam: an
# operator who exported COORDINATOR_CORE_LAZY_OPS keeps ownership of the
# decision in both directions. `coordinator_core/ops/__init__.py` reads the
# environment variable first for the same reason.
# ---------------------------------------------------------------------------
_LAZY_OPS_ENV_KEY = "COORDINATOR_CORE_LAZY_OPS"
_LAZY_OPS_SYS_ATTR = "_coordinator_core_lazy_ops"
_LAZY_OPS_INJECTED_BY_THIS_MODULE = _LAZY_OPS_ENV_KEY not in os.environ
if _LAZY_OPS_INJECTED_BY_THIS_MODULE:
    setattr(sys, _LAZY_OPS_SYS_ATTR, True)


def _no_console_kw(claude_klabauter_root: str) -> dict:
    """Splat-ready Windows console-suppression kwarg for a `coordinator_core.invoke`
    child spawn. ``claude_klabauter_root`` is already resolved by every call site here (the
    engine spawn itself), so this is a plain function-local coordinator_core import
    (no seam violation — see the module-top note above) rather than a fresh
    resolution. Falls back to the same suppression kwargs computed inline (zero
    imports beyond ``subprocess``) on any import failure, rather than silently
    dropping console suppression — a resolution failure must never turn a quiet
    spawn into a visible console window (Review: code-reviewer P1 — this was the
    most fanned-out `_no_console_kw`-shaped helper in coordinator/bin/ still
    fail-opening to bare ``{}``, matched here to the pattern ccbdbecc2 applied to
    sweep-boot.py/standup.py/render-project-tracker/refresh-plugin-live-install.py).

    The fallback reproduces the primitive's POSIX contract exactly -- ``{}`` off
    Windows, not ``{"creationflags": 0}``. Both splat harmlessly into
    ``subprocess.run``, but a caller comparing against ``no_console_creationflags()``
    or testing the mapping's truthiness would see the substitute disagree with the
    thing it substitutes for."""
    try:
        if claude_klabauter_root not in sys.path:
            sys.path.insert(0, claude_klabauter_root)
        from coordinator_core.win_portability import no_console_creationflags

        return no_console_creationflags()
    except Exception:  # noqa: BLE001 -- fail-open, matches this module's transport posture
        if os.name != "nt":
            return {}
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _no_console_passthrough_kw(claude_klabauter_root: str) -> dict:
    """`_no_console_kw` for a child whose OUTPUT MUST REACH THE OPERATOR.

    Same resolution and fail-open posture as `_no_console_kw` above, plus the
    std fds. Console suppression alone is not enough: with no
    ``stdout=``/``stderr=`` passed, CPython omits ``STARTF_USESTDHANDLES``, so
    the child binds its standard handles to the fresh window-less console
    ``CREATE_NO_WINDOW`` allocates instead of inheriting this process's -- and
    everything it prints is lost. Passing the fds explicitly restores the
    inheritance. Canonical implementation, kept in sync by hand because this
    module fails open without coordinator_core:
    ``coordinator_core.win_portability.no_console_passthrough_kwargs``.

    Real fds, not ``sys.stdout``/``sys.stderr``: the child inherits OS handles,
    and the fd is what a redirection actually moved. A stream with no fd
    (``pythonw``, a captured object) contributes nothing and degrades to plain
    inheritance rather than raising.
    """
    kwargs = dict(_no_console_kw(claude_klabauter_root))
    for key, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
        try:
            fd = stream.fileno()
        except (AttributeError, ValueError, OSError):
            continue
        if fd >= 0:
            kwargs[key] = fd
    return kwargs


def child_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env dict safe to pass as `env=` to a spawned child that is
    NOT itself a coordinator_core.invoke dispatch.

    BELT-AND-BRACES since 2026-07-28, no longer load-bearing. This module no
    longer writes COORDINATOR_CORE_LAZY_OPS into `os.environ` (see the channel
    note above), so a child can only inherit the variable when an operator
    exported it — and an operator's explicit choice is precisely what this
    function has always refused to strip. It is kept rather than deleted
    because the environment variable remains a supported operator override, so
    a copy-of-os.environ still has a defined, documented contract here.

    Historically: a plain `os.environ` copy carried the flag into the child
    whenever THIS module was the one that set it, silently making the child's
    own `import coordinator_core.ops` skip eager registration. This returns a
    copy with the key removed in exactly that case; an operator who set the
    variable themselves always keeps their own value, in this process and
    every child of it.

    `overrides`, if given, is applied on top (last-write-wins) after the
    strip — for a caller that wants to add its own env vars to the same
    spawn without a second dict-merge step.

    Callers that DO want the flag to reach the child (nested
    coordinator_core.invoke dispatch) should not use this — see
    `_build_subprocess_env`, which builds that env explicitly instead.

    AC11 (pln-the-machine-local-registry-rea-50be37 § C5): also propagates
    COORDINATOR_SETTINGS_HOME via `_settings_home_env` (never overwriting an
    already-set child value), same rationale as `_build_subprocess_env` —
    this spawns children too (e.g. `_machine_local_get`'s registry-read
    subprocess), and they should hit rung 0 instead of re-resolving via CLI.
    """
    env = _settings_home_env(dict(os.environ))
    if _LAZY_OPS_INJECTED_BY_THIS_MODULE:
        env.pop(_LAZY_OPS_ENV_KEY, None)
    if overrides:
        env.update(overrides)
    return env


# ---------------------------------------------------------------------------
# DEC-1..3 op-timeout-budget session cache (module-global, mirrors the shell
# transport's _CC_OP_TIMEOUTS_* session vars). Populated at most ONCE per Python
# process by _resolve_op_timeouts — a facade process is short-lived, so this is
# the per-process analogue of the shell's per-shell-session cache.
#   _OP_TIMEOUTS_STATE: None (unresolved) | "ok" | "absent" | "error"
# Mirrors the retired bash transport's _cc_resolve_op_timeouts (DEC-1..3).
# ---------------------------------------------------------------------------
_OP_TIMEOUTS_STATE: str | None = None
_OP_TIMEOUTS_MAP: dict[str, float] = {}
_OP_TIMEOUTS_BREADCRUMB_SHOWN: bool = False


def _reset_op_timeout_cache() -> None:
    """Reset the DEC-1..3 op-timeout session cache — test-only seam.

    The cache is a per-process singleton (resolved once); tests exercising distinct
    dump outcomes in the same process must reset it between cases.
    """
    global _OP_TIMEOUTS_STATE, _OP_TIMEOUTS_MAP, _OP_TIMEOUTS_BREADCRUMB_SHOWN
    _OP_TIMEOUTS_STATE = None
    _OP_TIMEOUTS_MAP = {}
    _OP_TIMEOUTS_BREADCRUMB_SHOWN = False


# ---------------------------------------------------------------------------
# CLAUDE_KLABAUTER_ROOT resolution — native Python ladder, no bash subprocess.
# _resolve_claude_klabauter_root() below is a from-scratch reimplementation mirroring
# coordinator-claude-klabauter-root.sh's four-rung discovery chain; it does not shell out
# to that script. The bash file remains on disk pending its own delete+repoint
# (Plan C de-bash wave R — see state/debt-backlog/ for the tracked entry); this
# comment previously claimed the opposite (subprocess-into-bash) and drifted
# from the code four lines below it.
# Review: code-reviewer — stale docstring at cc_invoke.py:116-119 contradicted
# _resolve_claude_klabauter_root()'s own docstring ("no bash subprocess anywhere in the
# ladder"); corrected to describe the native ladder it introduces.
# ---------------------------------------------------------------------------

_MLIR_MODULE = None


def _machine_local_impl_resolver():
    """Lazily import machine_local_impl_resolve, self-locating its own
    sys.path entry so this module stays standalone-invocable regardless of
    whether a caller already inserted coordinator/bin/lib. Cached after first
    call. Deliberately function-local (not a module-top import) — mirrors this
    module's own documented "no non-stdlib import above the LAZY_OPS line"
    discipline, even though machine_local_impl_resolve is not coordinator_core.
    """
    global _MLIR_MODULE
    if _MLIR_MODULE is None:
        _lib_dir = os.path.dirname(os.path.abspath(__file__))
        if _lib_dir not in sys.path:
            sys.path.insert(0, _lib_dir)
        import machine_local_impl_resolve as _mlir

        _MLIR_MODULE = _mlir
    return _MLIR_MODULE


def _claude_home() -> str:
    """Return the ~/.claude root, honoring CLAUDE_HOME for test isolation.

    Mirrors gen-claude-klabauter-root-pointer.py::_claude_home — this is the install root
    that hosts the machine-local Python reader (bin/_machine_local.py), distinct
    from the settings-home used for the rung-1.5 pointer file. Delegates to
    machine_local_impl_resolve.claude_home() (shared resolver — see that
    module's docstring).
    """
    return _machine_local_impl_resolver().claude_home()


# Cross-reference: coordinator_core/engine_root.py defines this same literal
# (plan pln-the-ceremony-tail-stops-lying-b58fb3 AC3b). The two rungs sit on
# opposite sides of a declared one-way no-import boundary and cannot share a
# symbol; the constant is duplicated deliberately and each side asserts the literal.
_REGISTRY_READ_TIMEOUT_TOKEN = "machine-local registry read timed out"

_MACHINE_LOCAL_READ_TIMEOUT_SECS = 10  # bound on the subprocess.run() call below


class _RegistryReadTimeout(RuntimeError):
    """A machine-local registry subprocess read exceeded its bound.

    Distinct from `is_timeout_error`'s IPC-engine-timeout contract: that predicate
    matches only `_TIMEOUT_MESSAGE_PREFIX`-prefixed messages from the
    cc_invoke()/cc_invoke_bare() transport layer (an engine timeout). This is a
    resolver-rung subprocess timeout — `_machine_local_get` raises nothing wrong
    happened at the engine, only that the registry read itself did not return in
    time. Raised by `_machine_local_get`, caught and re-raised (or absorbed) by
    `_resolve_claude_klabauter_root`, and threaded through `route()` to
    `_state1_remediation_message` as a named outcome (AC3) — never conflated with
    the IPC-timeout prefix/discriminator above.
    """


def _machine_local_get(key: str) -> str | None:
    """Read a machine-local registry key via a direct sys.executable subprocess.

    Native Python replacement for the bash `machine-local` forwarder: invokes
    bin/_machine_local.py (the real reader) directly with the same interpreter
    that loaded this module — no shell, no bash. Mirrors
    gen-claude-klabauter-root-pointer.py::_machine_local_get and
    coordinator_core.engine_root.coordinator_engine_root's own rung-2 lookup.

    Returns the resolved value, or None on any failure (missing impl, non-zero
    exit, empty stdout) — a registry miss is a normal fallback state here, not
    an error; the caller decides whether that's terminal.

    The one exception: a `subprocess.TimeoutExpired` on the registry-read bound
    raises `_RegistryReadTimeout` instead of collapsing to `None`. A busy box
    timing out a subprocess-bounded registry read is not the same fact as the
    key genuinely being absent, and collapsing the two here is what let the
    operator-facing ladder tell a transient reader timeout apart from a broken
    install (see `_resolve_claude_klabauter_root` / `_state1_remediation_message`).

    Settings-home first (DR-210 Amendment 2026-07-24): resolves via
    machine_local_impl_resolve.machine_local_impl_path(env_override=None) —
    `env_override=None` preserves this function's pre-existing contract of
    never honouring a MACHINE_LOCAL_IMPL test-isolation override (it never
    did before this precedence fix, and gaining that side effect here would be
    an unrelated behavior change).
    """
    impl = _machine_local_impl_resolver().machine_local_impl_path(env_override=None)
    if not os.path.isfile(impl):
        return None
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_MACHINE_LOCAL_READ_TIMEOUT_SECS,
            env=child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise _RegistryReadTimeout(
            f"{_REGISTRY_READ_TIMEOUT_TOKEN} ({_MACHINE_LOCAL_READ_TIMEOUT_SECS}s bound, "
            f"key={key!r})"
        ) from None
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


_CLAUDE_KLABAUTER_ROOT_REMEDIATION = (
    "cc_invoke: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set.\n"
    "  The machine-local registry has no 'repos.claude_klabauter' entry on this machine.\n"
    "  Remediate (choose one):\n"
    "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
    "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
    "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c"
)


# The engine-root module's own basename and entry-point name both carry the
# repo token (`claude_klabauter_root.py` / `coordinator_claude_klabauter_root_with_class`), and the
# publish transform rewrites that token throughout — so the mirror spells them
# `claude_klabauter_root.py` / `coordinator_claude_klabauter_root_with_class`.
# These two patterns are deliberately token-FREE, which makes them the one
# spelling that survives the transform byte-identically in both trees. Do not
# "fix" them to name the module directly; that is the defect, not the style.
_GATE_MODULE_GLOB_SUFFIX = "_root.py"
_GATE_ENTRY_POINT_RE = re.compile(r"^def (coordinator_\w+_root_with_class)\s*\(", re.MULTILINE)


def _gate_entry_point_by_shape(candidate: str) -> Optional[Callable[[], Tuple[str, str]]]:
    """Locate the engine-root gate entry point in `candidate` BY SHAPE, for a
    candidate whose module spelling differs from this tree's.

    Returns the resolved callable, or ``None`` when `candidate` genuinely has
    no engine-root module (a marker-only or broken checkout) — the caller
    turns that into the same RuntimeError it always raised.

    WHY THIS EXISTS. `_delegate_to_gate` above imports
    ``coordinator_core.engine_root`` from the CANDIDATE's path. That name is
    correct for a candidate spelled the way THIS tree is spelled, and wrong for
    one spelled the way the other tree is: the publish transform renames the
    module and its entry point together, so the live tree asking a published
    mirror for `coordinator_core.engine_root` can never succeed, and the mirror
    asking a live tree for its own transformed name can never succeed either.
    Rung 1 hands this function the published mirror on any DR-326 box — where
    engine dispatch resolves to the published build by design — so the
    pre-existing behaviour rejected, by construction, the very tree the
    resolver is built to reach. Symptom was a `scoped-git-commit` refusal on
    every staged `.py`: `detect-staged-rollback` could not resolve an engine.
    Backlink: state/bug-backlog/2026-08-19-cc-invoke-validates-a-candidate-root-by-a-c41f7a3e28b9.yaml

    HARD CONSTRAINT PRESERVED: no subprocess, matching `_resolve_claude_klabauter_root`'s
    own rungs-1/1.5/3 bound. Directory listing, plain reads, one import.

    Negative-spec:
      - Runs ONLY after the direct import fails, so the same-spelling path
        keeps its previous cost and behaviour exactly — this adds nothing to
        the hot path.
      - Does NOT import every `*_root.py` it finds. `coordinator_core/` also
        holds `state_root.py`, `data_root.py`, `coordinator_root.py` and
        friends; the source is text-scanned for the entry-point DEFINITION
        first and only the one match is imported. (`state_root.py` mentions
        the suffix without defining one — hence matching `^def `, not a bare
        substring.)
      - Does NOT widen what counts as a valid engine. It only lets a candidate
        answer under its own spelling; the answer still comes from that
        candidate's own gated ladder, never from a re-derivation here.
    """
    pkg_dir = os.path.join(candidate, "coordinator_core")
    try:
        entries = sorted(os.listdir(pkg_dir))
    except OSError:
        return None

    for entry in entries:
        if not entry.endswith(_GATE_MODULE_GLOB_SUFFIX) or entry.startswith("test_"):
            continue
        try:
            with open(os.path.join(pkg_dir, entry), "r", encoding="utf-8") as fh:
                match = _GATE_ENTRY_POINT_RE.search(fh.read())
        except OSError:
            continue
        if match is None:
            continue
        module_name = "coordinator_core." + entry[: -len(".py")]
        try:
            module = importlib.import_module(module_name)
        except Exception:
            # Deliberately broader than ImportError. A candidate whose
            # engine-root module matches by shape but raises on import (syntax
            # error mid-publish, a failing module-level side effect, a partial
            # checkout) is a BROKEN candidate, not this resolver's problem to
            # re-raise: the caller's contract is that an unusable candidate
            # yields the one RuntimeError naming the candidate and its source.
            # Letting an arbitrary exception escape here would surface as an
            # unrelated traceback on the commit hot path. (Review: rev-D.)
            continue
        found = getattr(module, match.group(1), None)
        if callable(found):
            return cast(Callable[[], Tuple[str, str]], found)
    return None


def _normalised_root(path: str) -> str:
    """Resolve symlinks and case-fold (Windows only) for root COMPARISON and
    for deriving the candidate-unique synthetic-module key below.

    Two spellings of one root must normalise identically; two distinct roots
    must not collide. Used by both `_is_same_tree_as_canonical` (comparison)
    and `_load_foreign_gate_entry_point` (key derivation) so the two never
    disagree about what counts as "the same root".
    """
    resolved = os.path.realpath(path)
    if os.name == "nt":
        resolved = resolved.casefold()
    return resolved


def _is_same_tree_as_canonical(candidate: str) -> bool:
    """True when `candidate` is the SAME root `coordinator_core` is already
    cached from (or nothing is cached yet — no cache to collide with).

    Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C0.
    This is the disambiguate-by-ROOT check `_delegate_to_gate` uses to decide
    between the ordinary `importlib.import_module` short-circuit (same tree —
    must stay byte-identical to the pre-C0 behaviour) and the foreign-
    candidate file-path load below (genuinely a different root).

    `coordinator_core` not yet in `sys.modules`, or cached with no
    `__file__` (a namespace-package edge case), or an unreadable path along
    the way: treated as "same tree" — there is no cache to collide with, so
    the ordinary import is safe and correct either way.
    """
    canonical = sys.modules.get("coordinator_core")
    if canonical is None:
        return True
    canonical_file = getattr(canonical, "__file__", None)
    if not canonical_file:
        return True
    try:
        canonical_root = _normalised_root(str(Path(canonical_file).resolve().parents[1]))
        candidate_root = _normalised_root(candidate)
    except OSError:
        return True
    return canonical_root == candidate_root


def _load_foreign_gate_entry_point(candidate: str) -> Optional[Callable[[], Tuple[str, str]]]:
    """Load `candidate`'s engine-root gate entry point BY FILE PATH, under a
    candidate-unique `sys.modules` key — for a candidate proven (by
    `_is_same_tree_as_canonical`) to be a genuinely DIFFERENT root than the
    one `coordinator_core` is already cached from.

    Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C0.

    LOCATING THE FILE — direct path first, shape scan as FALLBACK. Tries
    `<candidate>/coordinator_core/engine_root.py` directly (the post-rename
    spelling this plan converges on); falls back to the same shape-scan
    `_gate_entry_point_by_shape` uses (module-name-agnostic: text-scans for
    the `coordinator_\\w+_root_with_class` entry-point DEFINITION) only when
    that direct path is absent — the pre-rename / mixed-mirror transition
    window, and the case this chunk ships under today (before C1 lands).

    KNOWN AND ACCEPTED LIMIT (stated, not silently overclaimed): the loaded
    module still resolves any cross-package import (e.g.
    `from coordinator_core._settings_home import machine_local_dir`) through
    the CACHED `coordinator_core` package (root A), not candidate root B's
    own copy. Harmless today because the only such import is settings-home
    (machine state, not tree state) and `_SHIM_PATH` — the vector that
    actually mattered — derives from this module's own real `__file__`, so it
    is correctly per-root regardless. See `_resolve_claude_klabauter_root`'s "KNOWN AND
    ACCEPTED LIMIT" docstring note for the caller-facing version of this pin.

    Returns None when `candidate` has no locatable engine-root module (a
    marker-only or broken checkout) — the caller turns that into the same
    RuntimeError it always raised on a same-tree miss.
    """
    module_path = os.path.join(candidate, "coordinator_core", "engine_root.py")
    if not os.path.isfile(module_path):
        pkg_dir = os.path.join(candidate, "coordinator_core")
        try:
            entries = sorted(os.listdir(pkg_dir))
        except OSError:
            return None
        module_path = None
        for entry in entries:
            if not entry.endswith(_GATE_MODULE_GLOB_SUFFIX) or entry.startswith("test_"):
                continue
            candidate_path = os.path.join(pkg_dir, entry)
            try:
                with open(candidate_path, "r", encoding="utf-8") as fh:
                    if _GATE_ENTRY_POINT_RE.search(fh.read()):
                        module_path = candidate_path
                        break
            except OSError:
                continue
        if module_path is None:
            return None

    try:
        with open(module_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    match = _GATE_ENTRY_POINT_RE.search(text)
    if match is None:
        return None
    entry_name = match.group(1)

    digest = hashlib.sha1(_normalised_root(candidate).encode("utf-8")).hexdigest()[:16]
    synthetic_name = f"_cc_engine_root_{digest}"

    spec = importlib.util.spec_from_file_location(synthetic_name, module_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[synthetic_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        # Same posture as _gate_entry_point_by_shape's own except-broad note:
        # a candidate whose module matches by shape/path but raises on load
        # (partial checkout, mid-publish state) is a BROKEN candidate, not
        # this loader's problem to re-raise.
        sys.modules.pop(synthetic_name, None)
        return None
    found = getattr(module, entry_name, None)
    if callable(found):
        return cast(Callable[[], Tuple[str, str]], found)
    return None


def _claude_klabauter_root_gate_empty_remediation(candidate: str, *, source: str) -> str:
    """Remediation text for a candidate that imported ``coordinator_core.engine_root``
    but whose gated resolver still returned a falsy root.

    Review: engine-root-slice-2 finding 1 — the un-parameterized
    ``_CLAUDE_KLABAUTER_ROOT_REMEDIATION`` told every rung (env, registry, self-location)
    to run `machine-local set repos.claude_klabauter`, which is only the right
    instruction for Rung 2 (the case that text was written for). A bogus
    `CLAUDE_KLABAUTER_ROOT` (Rung 1) or an unimportable/unstamped self-located checkout
    (Rung 3) needs a remedy naming the candidate/source that actually failed,
    not a registry-set instruction unrelated to their problem.
    """
    if source == "machine-local repos.claude_klabauter":
        return _CLAUDE_KLABAUTER_ROOT_REMEDIATION
    return (
        f"cc_invoke: cannot resolve CLAUDE_KLABAUTER_ROOT — candidate {candidate!r} (from {source}) "
        "imported coordinator_core.engine_root but the gated ladder returned no root.\n"
        "  Remediate (choose one):\n"
        f"    Confirm {candidate!r} is a genuine, stamped claude-klabauter checkout.\n"
        "    machine-local set repos.claude_klabauter /path/to/claude-klabauter\n"
        "    Re-run /coordinator:install to populate the repos.* registry entries.\n"
        "  Reference: plugins/coordinator-claude/coordinator/docs/wiki/machine-local-registry.md §4c"
    )




def _resolve_claude_klabauter_root() -> str:
    """Resolve CLAUDE_KLABAUTER_ROOT natively — no bash subprocess anywhere in the ladder.

    Resolution order (mirrors coordinator_core.engine_root.coordinator_engine_root,
    the native port of coordinator-claude-klabauter-root.sh):
      Rung 1:   CLAUDE_KLABAUTER_ROOT already set in environment → CANDIDATE, delegated
                through the single gated ladder (see "DELEGATION" below) rather
                than answered directly.
      Rung 1.5: machine-local pointer files → cheap direct file reads, no
                subprocess spawn (docs/plans/2026-07-14-claude-klabauter-windows-
                portability.md § C1). `.claude-klabauter-root` is consulted
                FIRST and wins outright (DR-326: all engine dispatch goes to the
                published build); `.claude-klabauter-root` answers only on a box with no
                published mirror installed. Both remain a DIRECT return, not a
                delegation — see the "no longer gate-blind in the direction
                that mattered" note below for why that is safe.
      Rung 2:   machine-local registry candidate → delegated, same as Rung 1.
      Rung 3:   terminal self-location (__file__) → CANDIDATE, delegated the
                same way — see "DISPATCH axis" note below.

      DELEGATION (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md §
      C6): Rungs 1, 2, and 3 no longer ANSWER the dispatch question themselves.
      Each only supplies a CANDIDATE path capable of importing
      coordinator_core; the nested `_delegate_to_gate()` helper bootstraps that import
      and hands the actual decision to
      ``coordinator_core.engine_root.coordinator_engine_root_with_class()``
      (the DR-132/stamp-gated ladder C5 rewrote) — the single place that
      answers "which engine executes?" for every caller. A candidate that
      cannot import ``coordinator_core.engine_root`` (a marker-only or broken
      checkout) raises, rather than being trusted verbatim.

      DISPATCH axis vs LOCATOR axis: this function answers the DISPATCH
      question ("which engine executes?"), consumed by `route()`/`cc_invoke()`.
      `resolve_colocated_claude_klabauter_root()` and `resolve_engine_root()` answer a
      DIFFERENT question — "where is THIS co-located script's own tree, for
      sys.path purposes?" — and deliberately keep their own, undelegated rung
      orderings (self-location-before-env, or isdir-gated-env-before-self-
      location respectively). Do not collapse those into this function or vice
      versa; the two axes are allowed to disagree.

      PUBLISHED-ENGINE GATE COVERAGE, stated honestly: only Rungs 1, 2, and 3
      now reach the gate, via delegation. Rung 1.5 (pointer files) is the sole
      remaining direct return — the note below explains why that is not a
      gate-blind hole in practice.

      Rung 1.5 USED to be the live end of that blindness, and was the exact
      defect commit 0fdfb61d6 fixed inside `coordinator_engine_root_with_class()`
      itself — the pointer file pre-empting the DR-132 gate on every installed
      machine. That fix landed in the two-tier wrapper but did NOT reach this
      rung, so on a dual-boot box `cc_invoke` answered `X:/claude-klabauter` from
      every cwd, INCLUDING one where the gate itself would have said
      `claude-klabauter` (measured 2026-08-19, all four caller locations). Every
      engine invocation on such a box therefore ran the live working tree, whose
      warm generation token rotates on any commit by any session — the moving
      target DR-326 exists to stop.

      Resolved WITHOUT breaking the constraint that caused it: the published
      pointer is one more plain `open()`, so rung 1.5 still spawns no
      subprocess and still walks no gate — it is no longer gate-blind in the
      direction that mattered, because the answer it gives is the one the gate
      would have given (HARD CONSTRAINT preserved: no new subprocess on rungs
      1/1.5/3).
      Rung 3 (TERMINAL): self-location from THIS module's own ``__file__``,
                via the existing ``_walk_up_to_checkout`` helper — reached only
                when rungs 1, 1.5, and 2 have all missed, immediately before the
                function would otherwise raise. On a stranger's box none of the
                registry/pointer/env rungs resolve, which is why ~24 of the
                published-CLI failures this rung fixes were the single message
                "cc_invoke: cannot resolve CLAUDE_KLABAUTER_ROOT". Its answer is now
                DELEGATED (see "DELEGATION" above) rather than returned
                verbatim — hard constraint 2 (a script run by name must still
                find its own tree) is preserved because self-location still
                supplies the candidate that makes ``coordinator_core`` importable
                at all; only the FINAL answer now comes from the gate.
                LIMITATION, stated honestly: this answers with *cc_invoke.py's
                own* tree, not necessarily the caller's — on a multi-checkout
                box a bare ``_resolve_claude_klabauter_root()`` caller gets cc_invoke's
                tree, which is exactly why this is the TERMINAL rung (reached
                only when the alternative is raising) rather than an earlier
                one. A caller that wants per-caller self-location semantics
                should call ``resolve_engine_root(__file__)`` instead — that is
                not a general recommendation to migrate every caller here, just
                the honest answer for the multi-checkout case this rung cannot
                cover. In the published payload the two trees are the same,
                which is the case this rung targets.

    Returns the resolved absolute path.
    Raises RuntimeError on failure (unresolvable root — including a candidate
    from any delegated rung that cannot import
    ``coordinator_core.engine_root``) — or the RuntimeError subclass
    `_RegistryReadTimeout` specifically when the registry read at Rung 2 timed
    out and self-location (Rung 3) also missed, so a caller wanting to tell the
    two apart can `except _RegistryReadTimeout` before the general
    `except RuntimeError` (see `route()`, which does exactly this).

    C7 routing note (docs/plans/2026-08-19-an-engine-root-is-a-stamped-build.md
    § C7, docs/reference/engine-vs-locator-resolver-routing.md bucket
    4-bare-class-blind): this is the class-blind ENGINE-axis resolver every
    bare `_resolve_claude_klabauter_root()` caller reaches, and the PM's naming ruling
    (`resolve_claude_klabauter` should read as "find the source repo", not "find the
    engine") argues for renaming this symbol to say what it returns. That
    rename is deliberately NOT done here: `_delegate_to_gate`'s own comment
    above records that call-site guards elsewhere introspect
    `inspect.getsource(_resolve_claude_klabauter_root)` by this exact name, and a
    same-file wrapper/alias split would hand those guards the wrapper's
    short source instead of this function's real body — silently breaking
    them rather than fixing the naming. Renaming this symbol safely needs
    updating every guard that source-inspects it BY NAME, which is caller
    work outside C7's `writes:` scope (see the routing doc's bucket 4 file
    list) — left as an explicit exception, not a silent skip.
    """
    def _delegate_to_gate(candidate: str, *, source: str) -> str:
        """Bootstrap ``coordinator_core.engine_root`` from ``candidate`` and
        return the gated final answer from ``coordinator_engine_root_with_class()``.

        Nested (not module-level) so every DISPATCH-axis candidate rung below
        (env, registry, self-location) shares exactly ONE delegation body,
        which `inspect.getsource(_resolve_claude_klabauter_root)`-based call-site guards
        elsewhere in this tree see as part of this function's own source —
        each candidate rung now only supplies a path capable of importing
        `coordinator_core`; the actual DR-132/stamp-gated decision comes from
        exactly one place (the ladder C5 rewrote), never answered here
        directly. `source` is folded into the raised message only, on a
        candidate that cannot import `coordinator_core.engine_root` (a
        marker-only or broken checkout) — that candidate is NOT trusted
        verbatim on that failure, unlike the pre-C6 behaviour of Rungs 1 and 3.

        DISAMBIGUATES BY ROOT, NOT BY MODULE NAME (C0,
        docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md).
        `sys.modules["coordinator_core.<gate-module>"]` is a process-lifetime
        singleton keyed on NAME -- a second call with a different `candidate`
        would silently get served the FIRST call's cached module once both
        trees spell the gate module the same way. `_is_same_tree_as_canonical`
        decides which of two paths this call takes:
          - SAME root as the already-cached `coordinator_core` (or nothing
            cached yet): the ordinary `importlib`-based import below, BYTE-
            IDENTICAL to the pre-C0 behaviour -- this is the load-bearing
            short-circuit, not an optimisation (see this module's own
            docstring note on `_GATE_MEMO`/`_reset_*_memo` seams).
          - GENUINELY DIFFERENT root: `_load_foreign_gate_entry_point` loads
            candidate's own gate module by file path under a candidate-unique
            key, so it can never collide with (or be served from) the
            canonical module's cached entry.
        """
        if _is_same_tree_as_canonical(candidate):
            _injected = candidate not in sys.path
            if _injected:
                sys.path.insert(0, candidate)
            try:
                try:
                    from coordinator_core.engine_root import coordinator_engine_root_with_class
                except ImportError as exc:
                    coordinator_engine_root_with_class = _gate_entry_point_by_shape(candidate)
                    if coordinator_engine_root_with_class is None:
                        raise RuntimeError(
                            f"cc_invoke: CLAUDE_KLABAUTER_ROOT candidate {candidate!r} (from {source}) is not "
                            f"a valid claude-klabauter checkout — no coordinator_core/*_root.py under it defines "
                            f"a coordinator_*_root_with_class entry point "
                            f"(direct import also failed: {exc})"
                        ) from exc
                # Published-engine rung: coordinator_engine_root_with_class() runs the
                # DR-132 two-tier gate (published-engine-mirror vs. live-working-tree)
                # instead of the classless coordinator_engine_root(), which always
                # answered live-working-tree. The (root, resolution_class) pair is
                # returned; this rung only needs root — cc_invoke does not branch on
                # the class (that belongs to a future consumer, not this resolution
                # rung: engine.target is a read-site default, never diverted on here).
                resolved, _resolution_class = coordinator_engine_root_with_class()
            finally:
                if _injected:
                    try:
                        sys.path.remove(candidate)
                    except ValueError:
                        pass
        else:
            coordinator_engine_root_with_class = _load_foreign_gate_entry_point(candidate)
            if coordinator_engine_root_with_class is None:
                raise RuntimeError(
                    f"cc_invoke: CLAUDE_KLABAUTER_ROOT candidate {candidate!r} (from {source}) is not "
                    f"a valid claude-klabauter checkout — no coordinator_core/*_root.py under it defines "
                    f"a coordinator_*_root_with_class entry point"
                )
            resolved, _resolution_class = coordinator_engine_root_with_class()
        if not resolved:
            raise RuntimeError(_claude_klabauter_root_gate_empty_remediation(candidate, source=source))
        return resolved

    # Rung 1: already in environment — CANDIDATE only now, delegated through
    # the gate (see docstring's "DELEGATION" note) rather than answered here.
    existing = os.environ.get("CLAUDE_KLABAUTER_ROOT", "")
    if existing:
        return _delegate_to_gate(existing, source="CLAUDE_KLABAUTER_ROOT environment variable")

    # Rung 1.5 (NEW): cheap direct-file-read pointer, checked ahead of the
    # expensive bash-spawn resolver below. On Windows this avoids spawning a
    # bash subprocess on the per-invoke resolution hot path (fleet-wide
    # hook-latency fix). Plain file read only — never spawns a subprocess.
    # Writer follows reader: the install surface is expected to write
    # <settings-home>/machine-local/.claude-klabauter-root; absence here is a normal
    # fallback state, not an error — falls through to the bash resolver below.
    #
    # Settings-home precedence mirrors _machine_local.py::_settings_home()
    # (Port of: settings-home.sh's _coordinator_settings_home, DoE b644d5a9,
    # 2026-07-22) inline (kept inline here for the same single-file-module
    # reason _machine_local.py documents — no cross-file import hack across
    # the source/install-tree split):
    #   COORDINATOR_SETTINGS_HOME (explicit override) →
    #   ${CLAUDE_HOME:-$HOME}/.coordinator-claude-settings
    #
    # Spec backlink: pln-claude-klabauter-windows-portability-a48fac § C1
    _settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.environ.get("CLAUDE_HOME") or os.path.expanduser("~"),
        ".coordinator-claude-settings",
    )
    _ml_pointer_dir = os.path.join(_settings_home, "machine-local")

    def _read_pointer(name: str) -> str:
        """Plain read of a machine-local pointer file; "" when absent or empty.

        HARD CONSTRAINT (this function's own docstring, rungs 1/1.5/3): no
        subprocess. This stays a bare `open()` for that reason.
        """
        try:
            with open(os.path.join(_ml_pointer_dir, name), "r", encoding="utf-8") as _f:
                return _f.read().strip()
        except OSError:
            return ""

    # DR-326: engine dispatch resolves to the PUBLISHED build, never to the live
    # working tree. The live tree is reachable here only via Rung 1's explicit
    # CLAUDE_KLABAUTER_ROOT, which is what "claude-klabauter holds live processes only for testing"
    # means in practice. `.claude-klabauter-root` is written by the same install
    # pass that registers the mirror, so its presence IS the dual-boot signal —
    # and reading it costs one more `open()`, honouring the no-subprocess bound
    # that made this rung gate-blind in the first place.
    _published_pointer_val = _read_pointer(".claude-klabauter-root")
    if _published_pointer_val and os.path.isdir(_published_pointer_val):
        return _published_pointer_val

    # Single-tree box (no published mirror installed): the live tree is the only
    # engine there is, and this rung keeps its pre-DR-326 behaviour byte-identical.
    _pointer_val = _read_pointer(".claude-klabauter-root")
    if _pointer_val:
        return _pointer_val

    # Rung 2: native bootstrap — locate a candidate root via the machine-local
    # registry (no bash), then delegate to coordinator_core.engine_root itself
    # once it's importable, so the FINAL answer (and any future rung additions
    # to that module) come from the single native oracle, not a re-derivation
    # duplicated here.
    _registry_read_timed_out = False
    try:
        _candidate = _machine_local_get("repos.claude_klabauter")
    except _RegistryReadTimeout:
        _candidate = None
        _registry_read_timed_out = True

    if _candidate and os.path.isdir(_candidate):
        return _delegate_to_gate(_candidate, source="machine-local repos.claude_klabauter")

    # Rung 3 (terminal): self-locate from cc_invoke's OWN __file__ before
    # raising. Reached only when env, pointer, and registry all missed — see
    # the docstring's "Rung 3 (TERMINAL)" note for the limitation this rung
    # knowingly carries. Delegated the same way as every other candidate rung
    # (see docstring's "DELEGATION" note) — hard constraint 2 (a script run by
    # name must still find its own tree) is preserved by self-location still
    # supplying the candidate; only the final answer is no longer verbatim.
    _self_located = _walk_up_to_checkout(__file__)
    if _self_located:
        return _delegate_to_gate(_self_located, source="self-location (__file__)")

    if _registry_read_timed_out:
        # A transient reader timeout, not a genuinely absent/unregistered
        # checkout — propagate the distinguishable outcome (AC1/AC3)
        # instead of the clone/register text below, which is wrong here.
        raise _RegistryReadTimeout(
            f"{_REGISTRY_READ_TIMEOUT_TOKEN} ({_MACHINE_LOCAL_READ_TIMEOUT_SECS}s bound) "
            "resolving repos.claude_klabauter, and self-location also missed."
        )
    raise RuntimeError(_CLAUDE_KLABAUTER_ROOT_REMEDIATION)



# Dual-read window for the engine-root rename (docs/plans/2026-08-20-an-engine-
# root-is-not-named-for-the-repo.md). The PUBLISHED engine is transformed on the
# way out -- every `claude-klabauter` identifier becomes `claude_klabauter` -- but it still
# imports THIS module from the live tree, which is not transformed. So a published
# workstream_complete asks for `_resolve_claude_klabauter_root` and finds only
# `_resolve_claude_klabauter_root`, and the ceremony tail dies on ImportError for every
# session on the box. Exporting both names costs nothing and closes that window.
# In the mirror this line transforms into a self-assignment, which is a harmless
# no-op. Remove it only once no published engine references the old spelling.
_resolve_claude_klabauter_root = _resolve_claude_klabauter_root
def resolve_colocated_claude_klabauter_root(script_file: str) -> str:
    """Resolve CLAUDE_KLABAUTER_ROOT for a CLI that lives INSIDE the engine checkout itself.

    Self-location-first ladder for scripts under coordinator/bin/ (e.g. the
    distill-*.py CLIs): those scripts need to find their OWN repo root, which
    `Path(__file__)` answers with zero external dependency and can never be
    "unset" — unlike `_resolve_claude_klabauter_root`'s machine-local registry lookup,
    which exists to resolve a *different* repo across a repo boundary and is a
    manufactured fail-hard dependency for a co-located script.

    Rung 1: `Path(script_file).resolve().parents[2]` — for a script at
            coordinator/bin/X.py, parents[0]=coordinator/bin, parents[1]=coordinator,
            parents[2]=the engine root. Accepted only if it probes as a real
            engine checkout (has BOTH a coordinator_core/ directory AND a
            pyproject.toml — the same two-marker probe on every caller, so a
            change here can't silently diverge across the six distill CLIs).
    Rung 2: `_resolve_claude_klabauter_root()` (machine-local registry ladder) — only reached
            when rung 1's probe misses, i.e. the script has been published/vendored
            to a location outside its own engine checkout (the case the previous
            registry-only resolution was actually trying to cover).

    Raises RuntimeError (via _resolve_claude_klabauter_root's own fail-loud remediation text)
    if BOTH rungs miss.
    """
    _candidate = Path(script_file).resolve().parents[2]
    if (_candidate / "coordinator_core").is_dir() and (_candidate / "pyproject.toml").is_file():
        return str(_candidate)
    return _resolve_claude_klabauter_root()


def _walk_up_to_checkout(script_file: str) -> str | None:
    """Walk up from ``script_file`` to the nearest enclosing engine checkout.

    Depth-agnostic sibling of ``resolve_colocated_claude_klabauter_root``'s fixed
    ``parents[2]`` probe, using the same two-marker test (``coordinator_core/``
    directory AND ``pyproject.toml`` file). The fixed-depth form is correct only
    for scripts at ``coordinator/bin/X.py``; a helper module one level deeper at
    ``coordinator/bin/lib/X.py`` lands on ``coordinator/`` and silently misses.
    Walking removes the depth coupling, so a co-located caller resolves its own
    checkout regardless of where in the tree it sits.

    Returns None when no ancestor probes as a checkout (published/vendored
    outside any engine tree) — callers fall through to the registry ladder.

    Each ancestor's probe is individually guarded against OSError
    (``.is_dir()``/``.is_file()`` can raise on a broken Windows junction, a
    symlink loop, or a permission-denied parent): an unreadable ancestor is
    skipped rather than aborting the whole walk, so one bad link in the chain
    doesn't hide a real checkout further up.
    """
    try:
        parents = list(Path(script_file).resolve().parents)
    except OSError:
        return None
    for candidate in parents:
        try:
            if (candidate / "coordinator_core").is_dir() and (candidate / "pyproject.toml").is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def resolve_engine_root(script_file: str) -> str:
    """Resolve the engine checkout for a co-located CLI, override-first.

    The ladder every ``coordinator/bin`` entrypoint should use to find the
    engine it is about to import from:

      Rung 1: ``CLAUDE_KLABAUTER_ROOT`` in the environment, when it names a real
              directory — the explicit operator/test-harness override.
      Rung 2: self-location — ``_walk_up_to_checkout(script_file)``, the
              nearest enclosing checkout at any depth.
      Rung 3: ``_resolve_claude_klabauter_root()``'s remaining rungs — the
              ``<settings-home>/machine-local/.claude-klabauter-root`` pointer file, then
              the machine-local ``repos.claude_klabauter`` registry key.

    Distinct from ``resolve_colocated_claude_klabauter_root`` in rung ORDER, and the
    difference is load-bearing: that function probes self-location BEFORE the
    environment, so a caller pointing a script at a different engine checkout
    via ``CLAUDE_KLABAUTER_ROOT`` is silently served the one the script happens to sit in.
    Its existing callers are pinned to that ordering, so this is a new function
    rather than a change of semantics underneath them.

    Relative to the bare ``_resolve_claude_klabauter_root()`` this replaces at ~26 call
    sites, rung 1 is NOT byte-identical. Both still consult ``CLAUDE_KLABAUTER_ROOT``
    first and both still let it outrank every other rung — but unlike
    ``_resolve_claude_klabauter_root``, which returns any non-empty env value verbatim,
    this rung 1 is gated on ``os.path.isdir(env_root)``: a set-but-nonexistent
    ``CLAUDE_KLABAUTER_ROOT`` (e.g. stale after a cross-platform sync — ``~/.claude`` is
    shared between machines whose absolute paths differ, so a value baked on
    one box can name nothing on the other) falls through to self-location
    instead of being honored. This is deliberate: silently trusting a
    nonexistent root would reintroduce the ModuleNotFoundError this function
    exists to remove. The consequence for a caller relying on the old
    verbatim-return behavior — e.g. a test harness that deliberately pins a
    broken ``CLAUDE_KLABAUTER_ROOT`` to assert on the resulting failure — is that it
    will no longer get that broken root back; it gets self-location's answer
    (or the registry ladder's) instead.

    Otherwise: self-location is consulted ahead of the pointer file and the
    registry. That is what makes a script inside an engine checkout work on an
    install whose machine-local registry was never populated, which is the
    portability defect this exists to close: before it, a hand-set
    ``PYTHONPATH`` was the only remaining answer.

    Note the one behavior change that follows: on a box whose pointer file or
    registry names a DIFFERENT checkout than the one the script lives in, the
    script now uses its own. That is the intended reading of "co-located" and
    matches what ``resolve_colocated_claude_klabauter_root``'s callers already do; an
    operator who genuinely wants the other tree sets ``CLAUDE_KLABAUTER_ROOT`` to an
    EXISTING directory, which still outranks everything.

    Raises RuntimeError (via ``_resolve_claude_klabauter_root``'s fail-loud remediation
    text) when every rung misses.

    A caller that must fail loud when the engine is unresolvable belongs on
    THIS function, not on ``ensure_engine_on_path`` — see that function's
    docstring for why the degrading form is the wrong choice there.
    """
    env_root = os.environ.get("CLAUDE_KLABAUTER_ROOT") or ""
    if env_root and os.path.isdir(env_root):
        return env_root
    walked = _walk_up_to_checkout(script_file)
    if walked:
        return walked
    return _resolve_claude_klabauter_root()


def _front_insert_on_path(root: str) -> str:
    """Shared ``if root not in sys.path: sys.path.insert(0, root)`` body.

    The one insert primitive every path-mutating resolver wrapper in this
    module (``ensure_engine_on_path``, ``require_engine_on_path``,
    ``require_colocated_engine_on_path``) calls through, so the front-insert
    behavior — an explicit ``CLAUDE_KLABAUTER_ROOT`` outranking an ambient editable
    install of ``coordinator_core`` — lives in exactly one place. Returns
    ``root`` unchanged, so callers can end on ``return _front_insert_on_path(root)``.
    """
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def ensure_engine_on_path(script_file: str) -> str | None:
    """Resolve the engine root via ``resolve_engine_root`` and put it on ``sys.path``.

    The one-line form of the ``resolve → if not in sys.path → insert`` dance
    that was hand-rolled at every engine-touching seam in ``coordinator/bin``.
    Inserts at the FRONT, so an explicit ``CLAUDE_KLABAUTER_ROOT`` outranks an ambient
    editable install of ``coordinator_core``.

    Best-effort by design: returns None instead of raising when every rung
    misses, because the callers are CLIs that degrade gracefully on an
    engine-less install (a scaffold that needs no engine must not die on a
    resolution failure). A caller that genuinely requires the engine should
    call ``require_engine_on_path`` directly and let the RuntimeError fly.

    Catches both ``RuntimeError`` (every rung missed) and ``OSError``
    (a filesystem probe along the way — e.g. a broken junction or an
    inaccessible ancestor that ``_walk_up_to_checkout`` couldn't shield
    itself, or a registry/pointer-file read failure) so the "never raises"
    contract above actually holds; narrowing to ``RuntimeError`` alone let a
    raw ``OSError`` from a filesystem edge case escape past this best-effort
    boundary.

    Returns the resolved root, or None when unresolvable.
    """
    try:
        root = resolve_engine_root(script_file)
    except (RuntimeError, OSError):
        return None
    if not root:
        return None
    return _front_insert_on_path(root)


def require_engine_on_path(script_file: str) -> str:
    """Resolve the engine root via ``resolve_engine_root`` and put it on ``sys.path``, fail-loud.

    Env-first ladder (``resolve_engine_root``'s own rung order: an existing-directory
    ``CLAUDE_KLABAUTER_ROOT`` first, then self-location, then the pointer-file/registry rungs) — so
    an explicit operator override outranks self-location here, unlike
    ``require_colocated_engine_on_path`` below.

    Catches NOTHING: a ``RuntimeError`` from ``resolve_engine_root`` (every rung missed)
    or an ``OSError`` from a filesystem probe along the way propagates straight to the
    caller. Use this over ``ensure_engine_on_path`` when the caller genuinely requires the
    engine and an unresolvable root should be a hard failure, not a silent None.

    Returns the resolved root.
    """
    root = resolve_engine_root(script_file)
    return _front_insert_on_path(root)


def require_colocated_engine_on_path(script_file: str) -> str:
    """Resolve the engine root via ``resolve_colocated_claude_klabauter_root`` and put it on ``sys.path``, fail-loud.

    Self-location-first ladder: ``resolve_colocated_claude_klabauter_root``'s rung 1 probes
    ``Path(script_file)``'s own ``parents[2]`` as a candidate engine checkout BEFORE
    consulting the environment — while its two-marker probe hits, an explicit
    ``CLAUDE_KLABAUTER_ROOT`` is never even consulted. Only when that self-location probe misses
    does resolution fall through to ``_resolve_claude_klabauter_root()``'s ladder, where
    ``CLAUDE_KLABAUTER_ROOT`` is rung 1.

    Catches NOTHING: a ``RuntimeError`` from ``resolve_colocated_claude_klabauter_root`` (both
    rungs missed) or an ``OSError`` from a filesystem probe along the way propagates
    straight to the caller.

    Returns the resolved root.
    """
    root = resolve_colocated_claude_klabauter_root(script_file)
    return _front_insert_on_path(root)


def require_dispatch_engine_on_path() -> str:
    """Resolve the DISPATCH engine root and put it on ``sys.path``, fail-loud.

    The collapse target for the inline bootstrap preamble that ~200 CLIs under
    ``coordinator/bin`` carry verbatim::

        claude_klabauter_root = _resolve_claude_klabauter_root()
        if claude_klabauter_root not in sys.path:
            sys.path.insert(0, claude_klabauter_root)

    NOTE THE MISSING PARAMETER, because it is the whole point. Every other
    ``*_on_path`` wrapper in this module takes ``script_file`` and resolves on the
    LOCATOR axis — "where is the source checkout", answered by walking up from the
    calling file. This one takes nothing, because the DISPATCH answer — "which
    engine executes" — is a property of the box, not of the caller's location. A
    signature that cannot accept a script path cannot silently be handed one.

    WHY NOT REUSE ``require_engine_on_path``. It is the same shape one axis over
    and adopting it here looks like the obvious collapse, but on a conformant box
    with both env vars unset the two ladders return DIFFERENT ROOTS:
    ``_resolve_claude_klabauter_root()`` reaches the published mirror through the
    pointer-file/registry rung, while ``resolve_engine_root()`` reaches the live
    working tree through its self-location rung. Routing the inline copies onto the
    locator seam therefore repoints every one of them from the published engine to
    the working tree — a fleet-wide behaviour change wearing a collapse commit's
    label. Measured and reverted once already; see the plan's delivery notes.

    So this is a SECOND seam on a DIFFERENT axis, not a duplicate of the first. The
    duplication C16 forbids is two seams answering the same question.

    Catches NOTHING, matching the inline body it replaces: a ``RuntimeError`` from
    ``_resolve_claude_klabauter_root()`` (every rung missed) propagates to the caller, whose
    own ``except RuntimeError`` remediation path is usually the reason it is there.

    Returns the resolved dispatch root, so a caller that also needs to hand it to
    ``cc_invoke`` can end on ``root = require_dispatch_engine_on_path()``.

    Spec backlink: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md
    (C16), and docs/reference/engine-root-env-var-routing.md for which call sites
    are on which axis.
    """
    return _front_insert_on_path(_resolve_claude_klabauter_root())


# ---------------------------------------------------------------------------
# Seam gate — disk-presence check via find_spec. Note: find_spec on a dotted name
# imports the parent package (coordinator_core) as a side-effect — sys.path is
# restored after the probe, sys.modules is not. Intentional improvement over the
# retired shell facade's full-import probe: a broken-but-present
# engine routes native → ImportError → hard error rather than silently falling to
# legacy.
# ---------------------------------------------------------------------------

def _seam_present(claude_klabauter_root: str) -> bool:
    """Return True if coordinator_core.invoke is importable from claude_klabauter_root.

    Uses importlib.util.find_spec — disk-presence check with a sys.modules side-effect.
    Temporarily injects claude_klabauter_root onto sys.path for the probe; restores sys.path on exit.

    Note: find_spec on a dotted name ("coordinator_core.invoke") imports the parent
    package coordinator_core as an internal step when it is not already in sys.modules —
    coordinator_core may remain in sys.modules after this call. Only sys.path is restored.

    Negative-spec: does NOT execute the module or probe liveness; a broken module
    that find_spec can locate routes to the native path and raises hard on import.
    """
    # find_spec on a dotted name imports the parent pkg as a side-effect; sys.modules
    # is not restored, only sys.path (see the docstring above).

    # Module-hijack defense-in-depth: the registry-resolved root is trusted, but an
    # un-validated relative or non-directory path on sys.path[0] is the hijack vector —
    # treat it as seam-absent and route to the safe legacy default.
    if not os.path.isabs(claude_klabauter_root) or not os.path.isdir(claude_klabauter_root):
        return False

    _injected = claude_klabauter_root not in sys.path
    if _injected:
        sys.path.insert(0, claude_klabauter_root)
    try:
        spec = importlib.util.find_spec("coordinator_core.invoke")
        return spec is not None
    except (ModuleNotFoundError, ValueError):
        return False
    finally:
        if _injected:
            try:
                sys.path.remove(claude_klabauter_root)
            except ValueError:
                pass


# ---------------------------------------------------------------------------
# Shared transport helpers — used by BOTH cc_invoke() (envelope-parse convention)
# and cc_invoke_bare() (--bare convention) so the fail-closed ladder lives once.
# ---------------------------------------------------------------------------

def _read_positive_int_env(name: str, default: int) -> int:
    """Read a positive-int tuning knob from the environment, defaulting on any garbage.

    A malformed timeout/margin knob must never break the transport — defaulting is the
    resilient choice (mirrors the retired bash transport's ${VAR:-N} floors, which silently ignore a
    non-numeric override). Emits a single warn line to stderr when the value is present
    but unusable.
    """
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        if value <= 0:
            raise ValueError("non-positive")
    except ValueError:
        print(
            f"warn: cc_invoke: invalid {name}={raw!r}, using default {default}s",
            file=sys.stderr,
        )
        return default
    return value


def _should_pass_repo(op: str, claude_klabauter_root: str | None = None) -> bool:
    """Return whether `--repo` should be spawned on argv for `op`.

    DR-279 (docs/decisions/DR-279-repo-on-a-none-scoped-op-fails-loud.md) made
    coordinator_core.invoke exit non-zero when --repo is passed to a "none"-scoped
    op, but this module passed --repo unconditionally on every op — every
    none-scoped op invoked through cc_invoke died. Mirrors
    coordinator_core/invoke/__main__.py's own gate exactly (`args.op not in
    WORKTREE_SCOPED_OPS` refuses --repo): WORKTREE_SCOPED_OPS is the authoritative
    frozenset of ops whose scope is "common_dir" or "show_top" (derived from
    coordinator_core.op_scopes.OP_KEY_SCOPE); every other op — "none"/"central"
    scoped, OR simply absent from the table (OP_KEY_SCOPE.get(op, "none") in
    __main__.py) — refuses --repo the same way. Read from the table rather than
    hardcoding an op list, so this wrapper and the engine's own refusal can never
    drift apart again.

    Review: code-reviewer (P3) — a bare `from coordinator_core.op_scopes import
    ...` here relies on coordinator_core already being importable from the
    CALLING process's ambient sys.path, which is NOT guaranteed: a caller script
    living at coordinator/bin/*.py (e.g. coordinator-workflow-scaffold.py) has
    only coordinator/bin/lib on sys.path, not the engine root itself, so the
    import raised ImportError every time and this function silently fell open
    (returned True) on EVERY call — reintroducing the exact DR-279 bug this
    module exists to fix, for exactly the callers most likely to hit it. Mirrors
    `_seam_present()`'s own temporary sys.path injection: try the ambient import
    first (covers callers that already have coordinator_core importable, zero
    added cost), and only on failure try again with `claude_klabauter_root` (resolved by
    the caller, or freshly resolved here if not supplied) temporarily inserted
    onto sys.path. Still fails OPEN (returns True) if BOTH attempts fail — cc_invoke
    is on the hot path for many callers, and a broken resolution here must never
    crash the transport.
    """
    try:
        from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS

        return op in WORKTREE_SCOPED_OPS
    except Exception:
        pass

    try:
        _root = claude_klabauter_root if claude_klabauter_root is not None else _resolve_claude_klabauter_root()
    except RuntimeError:
        return True

    if not os.path.isabs(_root) or not os.path.isdir(_root):
        return True

    _injected = _root not in sys.path
    if _injected:
        sys.path.insert(0, _root)
    try:
        from coordinator_core.op_scopes import WORKTREE_SCOPED_OPS

        return op in WORKTREE_SCOPED_OPS
    except Exception:
        return True
    finally:
        if _injected:
            try:
                sys.path.remove(_root)
            except ValueError:
                pass


def _locator_axis_export() -> dict[str, str]:
    """C18: the LOCATOR-axis export, added alongside the dispatch variable.

    Spec: docs/plans/2026-08-20-an-engine-root-is-not-named-for-the-repo.md § C18.
    Axis definition: docs/decisions/DR-326.

    `CLAUDE_KLABAUTER_ROOT` in the child env carries the DISPATCH answer -- which engine
    executes -- because that is what `_resolve_claude_klabauter_root()` returns and what
    this process is about to run. A grandchild asking the LOCATOR question
    ("where is the source checkout?") reads the same variable and is handed a
    published mirror. Two facts, one variable.

    NEGATIVE SPEC -- ADDITIVE ONLY. This returns ONLY the locator key. It never
    touches `CLAUDE_KLABAUTER_ROOT`, `COORDINATOR_ENGINE_ROOT`, or `PYTHONPATH`, so the
    dispatch variable's meaning and value are byte-identical to before this
    landed and a child that ignores the new key behaves exactly as it does
    today. That property is what makes a semantic split landable across four
    version-skewed parties (live tree, published mirror, deployed settings home,
    sibling repos) -- it turns "four parties x two meanings" into "four parties
    x two variables".

    Resolves through `_machine_local_get`, NOT through `_resolve_claude_klabauter_root()`:
    the registry's `repos.claude_klabauter` IS the locator answer, whereas the
    ladder deliberately prefers the published engine. Returns `{}` when the key
    is unset or the lookup fails -- a box with no registered checkout must keep
    spawning children exactly as it does now, so this is best-effort by design
    and never raises into the spawn path.
    """
    try:
        source_root = _machine_local_get("repos.claude_klabauter")
    except Exception:
        return {}
    if not source_root:
        return {}
    return {"COORDINATOR_ENGINE_SOURCE_ROOT": source_root}


def _build_subprocess_env(claude_klabauter_root: str) -> dict[str, str]:
    """Build the subprocess env for a coordinator_core.invoke spawn.

    Passes os.environ through, sets CLAUDE_KLABAUTER_ROOT, and prepends it to PYTHONPATH only if
    not already present (idempotency fence — mirrors _cc_resolve_deps() in the shell
    transport). Shared by cc_invoke(), cc_invoke_bare(), and the op-budget dump spawn.

    AC11 (pln-the-machine-local-registry-rea-50be37 § C5): also propagates
    COORDINATOR_SETTINGS_HOME into the child env via `_settings_home_env`, so a
    child that itself resolves settings-home (directly, or by invoking a shell
    resolver that tries the `coordinator-settings-home` CLI before its disk
    fallback) hits rung 0 and skips that CLI call.
    """
    env: dict[str, str] = _settings_home_env(
        {**os.environ, "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root}, claude_klabauter_root
    )
    env.update(_locator_axis_export())
    existing_pp = env.get("PYTHONPATH", "")
    _sep = os.pathsep
    if f"{_sep}{claude_klabauter_root}{_sep}" not in f"{_sep}{existing_pp}{_sep}":
        env["PYTHONPATH"] = f"{claude_klabauter_root}{_sep}{existing_pp}" if existing_pp else claude_klabauter_root
    return env


def _settings_home_env(base_env: dict[str, str], claude_klabauter_root: str | None = None) -> dict[str, str]:
    """Return `base_env` with COORDINATOR_SETTINGS_HOME set to the resolved
    settings-home root, UNLESS `base_env` already carries the key.

    AC11 (pln-the-machine-local-registry-rea-50be37 § C5): the actual spawn seam
    that builds child env for claude-klabauter-owned fan-outs. `coordinator_core._settings_home
    .settings_home()` is a pure env/home read with zero external calls (no CLI
    spawn), so this is re-derived fresh on every call — there is no per-process
    cache to go stale across a long-lived warm engine or EM session, which
    trivially satisfies "re-derive per op-dispatch, not per process".

    Precedence: an explicitly-set child value (differently-rooted tenant, a test
    harness redirecting it, a deliberately-scoped operator shell) is NEVER
    overwritten — this only fills a gap the child env does not already carry.

    Import is function-local, matching this module's convention (see the
    eager-op-registration seam note above): no coordinator_core import sits
    above that seam at module top. `claude_klabauter_root` is inserted onto `sys.path`
    only for the duration of the import, mirroring `_is_worktree_scoped_op`'s
    own inject/finally-remove pattern — this function must never crash the
    transport, so a resolution failure falls back to `base_env` unchanged.
    """
    if base_env.get("COORDINATOR_SETTINGS_HOME"):
        return base_env

    _root = claude_klabauter_root if claude_klabauter_root is not None else os.environ.get("CLAUDE_KLABAUTER_ROOT")
    _injected = bool(_root) and _root not in sys.path
    if _injected:
        sys.path.insert(0, _root)
    try:
        from coordinator_core import _settings_home

        return _settings_home.settings_home_child_env(base_env)
    except Exception:
        return base_env
    finally:
        if _injected:
            try:
                sys.path.remove(_root)
            except ValueError:
                pass


_IMPORT_ERROR_TOKENS = ("importerror", "modulenotfounderror", "no module named")

#: Cap on the raw-stdout tail `_op_error_detail` falls back to when the child's
#: stdout is not a parseable JSON-RPC envelope. A traceback or a debug dump can
#: run to megabytes; the raised message has to stay readable in a terminal.
_OP_ERROR_DETAIL_CAP = 2000


def _op_error_detail(stdout_text: str) -> str:
    """Recover the engine's own failure text from a nonzero-exit child's STDOUT.

    ``coordinator_core.invoke`` splits its two failure channels by ORIGIN, not by
    severity, and the split is easy to misread as "errors go to stderr":

      - A PRE-dispatch failure (bad args, unresolvable repo_root) goes through
        ``_fatal_stderr``, which writes its JSON-RPC error envelope to **stderr**
        and exits 1.
      - A dispatch that COMPLETED with an op-level error is an ordinary JSON-RPC
        response: ``main()`` prints it to **stdout** and exits 1 via
        ``_exit_code_for_response``. Stderr is typically empty.

    ``_raise_on_process_failure`` only ever read stderr, so the entire second
    class — every op-level refusal, every exception escaping a handler, an
    unknown method — reached the operator as a bare
    ``invoke process exited 1 (op=X) — op or dispatch error`` followed by an
    empty ``stderr:`` line, with the reason nowhere: it was sitting on stdout,
    discarded unread. That is how ``ceremony.wsc_tail`` failed on doe-claude-em's
    Windows box with no recoverable diagnosis
    (``cross-repo/inbox/2026-08-07-doe-claude-em-windows-ceremony-cli-coordinator-core-import-break.md``),
    and why that one item's symptom looked unlike its two siblings' — those died
    in the trampoline process itself and printed a real traceback, while this one
    died behind the transport's blind side.

    Returns an indented ``  op error: ...`` line for a JSON-RPC error envelope, a
    capped ``  op stdout: ...`` line for anything else non-empty, or ``""`` when
    stdout carries nothing. Never raises — this runs on a path that is already
    failing and must not acquire a second failure mode of its own.

    To reproduce the stdout-borne error envelope this recovers, without firing a
    mutating op at a live tree: ``docs/reference/transport-failure-probes.md``
    (``diagnostics.always_refuses`` is the write-free probe for this shape).
    """
    text = (stdout_text or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            return (
                f"  op error: code={err.get('code', '?')!r} "
                f"message={err.get('message', '?')!r}"
            )
        if err:
            return f"  op error: {err!r}"
    return f"  op stdout: {text[:_OP_ERROR_DETAIL_CAP]}"


def _raise_on_process_failure(
    rc: int,
    stdout_text: str,
    stderr_text: str,
    op: str,
    claude_klabauter_root: str,
) -> None:
    """Fail-closed rungs (2) and (3) of the shell ladder — nonzero exit and empty stdout.

    Rung (1), the timeout branch, is handled at each caller's subprocess.run try/except
    (both catch TimeoutExpired) since the timeout value is caller-local. Raises
    RuntimeError (or its StructuralPinError subclass) on failure; returns None when the
    process succeeded with output.

    (2) Nonzero exit → distinguish, in precedence order: ImportError on stderr
        (engine-won't-start) > rc==2 structural contract-pin failure
        (StructuralPinError, non-self-healing) > ImportError recovered from the
        stdout error envelope > generic op-level error. EVERY rung now carries
        ``_op_error_detail``'s recovery of the child's stdout — see that
        function for the channel split this ladder used to be blind to.
    (3) Empty stdout → invoke always produces output on success.

    Negative-spec: the stdout-borne ImportError rung is deliberately ranked BELOW
    the rc==2 structural-pin rung, not folded into the stderr sniff above it. The
    pre-existing precedence — which rung an input lands on: stderr-ImportError >
    rc==2 > generic — is load-bearing and stays identical; widening the top sniff
    to stdout would let a structural-pin message that merely mentions a module
    name get reclassified as an install failure, losing the engine's own
    non-self-healing discriminator. (The raised message TEXT for rungs 1/2 is not
    byte-identical to before this change — `_op_error_detail`'s recovered detail
    is now appended to every rung, including these two — only the routing
    precedence is pinned. Review: code-reviewer P3.)

    ``docs/reference/transport-failure-probes.md`` maps each rung above to a
    write-free probe that reaches it — safe to fire at a live, dirty, shared tree
    — and names the three rungs (stderr ImportError, empty stdout, transport
    absent) that no registered op can reach, with the spawn-free unit cases that
    cover them instead.
    """
    if rc != 0:
        detail = _op_error_detail(stdout_text)

        def _engine_wont_start(token_origin: str) -> RuntimeError:
            """Build the engine-won't-start error, naming which channel accused the engine.

            ``token_origin`` is load-bearing rather than cosmetic. Two different rungs
            raise this, and only one of them is strong evidence:

              - ``stderr`` — the engine itself failed to import, and said so on the
                channel it uses for pre-dispatch failures. Trustworthy.
              - ``stdout`` — an ImportError token was recovered from a COMPLETED
                dispatch's own error envelope. That usually means the same thing, but
                it cannot mean it as certainly: the engine demonstrably started (it ran
                a handler far enough to produce an envelope), so an op merely reporting
                a module problem of its own lands here too and gets told to go check
                CLAUDE_KLABAUTER_ROOT. Naming the origin is what lets the operator tell those
                apart instead of chasing an install that is fine.

            Narrowing the stdout sniff to remove that false positive was considered and
            declined: it trades a visible-and-labelled misclassification for a silent
            missed one, and this ladder's whole purpose is that failures state what they
            actually know. Reviewed and escalated as s2/F1 (2026-08-07).
            """
            lines = [
                f"cc_invoke: engine will not import/start (op={op}, rc={rc})",
                "  ImportError — verify CLAUDE_KLABAUTER_ROOT and coordinator_core installation:",
                f"    CLAUDE_KLABAUTER_ROOT={claude_klabauter_root!r}",
                f"    ImportError token seen on: {token_origin}",
            ]
            if token_origin == "stdout":
                lines.append(
                    "    NOTE: recovered from the op's OWN error envelope, not from engine "
                    "startup — the engine did start, so if the op merely reported a module "
                    "problem of its own, this classification is wrong; read the op error below."
                )
            lines.append(f"    stderr: {stderr_text.strip()}")
            message = "\n".join(lines)
            return RuntimeError(f"{message}\n{detail}" if detail else message)

        if any(tok in stderr_text.lower() for tok in _IMPORT_ERROR_TOKENS):
            raise _engine_wont_start("stderr")
        if rc == 2:
            message = (
                f"cc_invoke: structural contract-pin failure (op={op}, rc=2) — "
                "non-self-healing, will recur on retry\n"
                f"  stderr: {stderr_text.strip()}"
            )
            raise StructuralPinError(f"{message}\n{detail}" if detail else message)
        if any(tok in detail.lower() for tok in _IMPORT_ERROR_TOKENS):
            raise _engine_wont_start("stdout")
        message = (
            f"cc_invoke: invoke process exited {rc} (op={op}) — op or dispatch error\n"
            f"  stderr: {stderr_text.strip()}"
        )
        raise RuntimeError(f"{message}\n{detail}" if detail else message)

    if not stdout_text.strip():
        raise RuntimeError(
            f"cc_invoke: empty stdout from invoke (op={op}) — invoke produced no output"
        )


def _resolve_op_timeouts(claude_klabauter_root: str, env: dict[str, str], floor: int) -> None:
    """Resolve the engine's per-op timeout budget map ONCE per process (DEC-1..3).

    Spawns `coordinator_core.invoke --dump-op-timeouts` (capped at FLOOR) and feature-
    detects three outcomes into _OP_TIMEOUTS_STATE, faithfully porting the shell
    transport's _cc_resolve_op_timeouts DEC-2a/2b split:
      "ok"     — dump succeeded; _OP_TIMEOUTS_MAP holds the {op: secs} map (incl. the
                 required "__default__" key).
      "absent" — dump surface not present (older engine repo; argparse "unrecognized" on
                 stderr) — DEC-2a, silent, expected.
      "error"  — surface present but the call failed (timeout / nonzero for another
                 reason / empty / malformed / missing "__default__") — DEC-2b, still
                 falls back to flat FLOOR but earns a once-per-process breadcrumb.

    The DEC-1 dump surface ships server-side today, so the probe always runs — once
    per process, memoized via the `_OP_TIMEOUTS_STATE is not None` guard above, so it
    never doubles the per-op subprocess/CreateProcess spawn count (the single most
    expensive syscall path on Windows) beyond that one-time cost. Older engine
    checkouts that predate the dump surface fall through the DEC-2a "absent" branch
    below (argparse "unrecognized" detection on stderr) — that is the graceful-
    degradation path this function preserves, not a feature flag.
    """
    global _OP_TIMEOUTS_STATE, _OP_TIMEOUTS_MAP
    if _OP_TIMEOUTS_STATE is not None:
        return

    _OP_TIMEOUTS_MAP = {}
    _OP_TIMEOUTS_STATE = "absent"

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coordinator_core.invoke", "--dump-op-timeouts"],  # popup-safe-env-suppressed
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=floor,
            env=env,
            cwd=claude_klabauter_root,
            **_no_console_kw(claude_klabauter_root),
        )
    except subprocess.TimeoutExpired:
        # A timeout means the surface responded (or was expected to) and wedged — DEC-2b.
        _OP_TIMEOUTS_STATE = "error"
        return

    if proc.returncode != 0:
        # DEC-2 split: an argparse-style "unrecognized" error is an older engine repo without
        # the dump surface (2a, silent); any other failure shape is a real fault (2b).
        _argparse_absent = any(
            tok in proc.stderr.lower()
            for tok in (
                "unrecognized arguments",
                "unrecognized command",
                "invalid choice",
                "no such option",
                "unknown option",
            )
        )
        _OP_TIMEOUTS_STATE = "absent" if _argparse_absent else "error"
        return

    if not proc.stdout.strip():
        _OP_TIMEOUTS_STATE = "error"
        return

    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _OP_TIMEOUTS_STATE = "error"
        return

    # Require a flat {op: number} object carrying the "__default__" key.
    if not isinstance(parsed, dict) or "__default__" not in parsed:
        _OP_TIMEOUTS_STATE = "error"
        return
    coerced: dict[str, float] = {}
    for key, val in parsed.items():
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            _OP_TIMEOUTS_STATE = "error"
            return
        coerced[key] = float(val)

    _OP_TIMEOUTS_MAP = coerced
    _OP_TIMEOUTS_STATE = "ok"


def _op_timeout_ceiling(op: str, claude_klabauter_root: str, env: dict[str, str]) -> int:
    """Per-op timeout ceiling (DEC-1..3): _t = max(FLOOR, engine_budget(op) + MARGIN).

    FLOOR (CC_INVOKE_TIMEOUT_SECS, default 10) is a floor-only knob — the minimum the
    client will ever wait, NOT a ceiling for override-less ops. MARGIN
    (CC_INVOKE_CLIENT_MARGIN_SECS, default 10) covers cold python startup + import on top
    of the engine's own dispatch budget. Falls back to flat FLOOR when the engine's
    op-budget dump is absent or errored (with a once-per-process breadcrumb on error).

    Substrate fact (2026-08-08 timeout-remedy fix): the long-unexplained "observed
    consistently at 40s" in the backlog is this formula with the default constants —
    max(FLOOR=10, engine_budget(op)=30 + MARGIN=10) = 40. See `_timeout_exceeded_message`,
    which surfaces this derivation in the TimeoutExpired remedy text instead of the old
    (and wrong, on a healthy engine) "verify CLAUDE_KLABAUTER_ROOT / installation" text.
    """
    global _OP_TIMEOUTS_BREADCRUMB_SHOWN
    floor = _read_positive_int_env("CC_INVOKE_TIMEOUT_SECS", 10)
    margin = _read_positive_int_env("CC_INVOKE_CLIENT_MARGIN_SECS", 10)

    _resolve_op_timeouts(claude_klabauter_root, env, floor)

    if _OP_TIMEOUTS_STATE == "ok":
        budget = _OP_TIMEOUTS_MAP.get(op, _OP_TIMEOUTS_MAP["__default__"])
        budget_int = int(budget)  # integer-truncate a float budget (e.g. 30.0 -> 30)
        return max(floor, budget_int + margin)

    if _OP_TIMEOUTS_STATE == "error" and not _OP_TIMEOUTS_BREADCRUMB_SHOWN:
        print(
            "cc_invoke: op-budget dump failed; using flat floor — "
            "overridden ops may hit the client cap",
            file=sys.stderr,
        )
        _OP_TIMEOUTS_BREADCRUMB_SHOWN = True
    return floor


# Stable literal prefix of every TimeoutExpired-derived RuntimeError this module raises
# (both cc_invoke() and cc_invoke_bare()) — the discriminator `is_timeout_error` matches
# on. Kept as a named constant rather than inlined so the two places that must agree on
# it (the builder below and the discriminator) cannot drift independently.
_TIMEOUT_MESSAGE_PREFIX = "cc_invoke: engine timeout after "


def is_timeout_error(exc: BaseException) -> bool:
    """True if `exc` is the TimeoutExpired-derived RuntimeError this module raises.

    Lets a caller distinguish "engine was simply busy" (never install-related) from
    every other RuntimeError this module's ladder can raise (which may legitimately be
    install-related, e.g. `_engine_wont_start`) WITHOUT re-deriving or duplicating
    `_timeout_exceeded_message`'s text. A caller that appends its own generic "verify
    CLAUDE_KLABAUTER_ROOT / installation" remedy line after any `except RuntimeError` should gate
    that line on `not is_timeout_error(exc)` — see AC7,
    docs/plans/2026-08-08-claim-index-the-commit-gate-never-had.md § C5.
    """
    return isinstance(exc, RuntimeError) and str(exc).startswith(_TIMEOUT_MESSAGE_PREFIX)


def _timeout_exceeded_message(op: str, timeout: int) -> str:
    """Build the TimeoutExpired remedy text — names the COMPUTED ceiling, not the install.

    Replaces the old "Verify CLAUDE_KLABAUTER_ROOT and coordinator_core installation" text, which
    was flatly wrong on a demonstrably healthy engine (it cost a session four retries and
    a wrong install diagnosis; reproduced on coverage.gate and ceremony.wsc_tail, neither
    lock-related). Called AFTER `_op_timeout_ceiling(op, ...)` has already run for this
    invocation, so `_OP_TIMEOUTS_STATE`/`_OP_TIMEOUTS_MAP` are already resolved — no
    second engine spawn here.

    Names the derivation (`max(FLOOR, engine_budget(op) + MARGIN)`) so the reader knows
    CC_INVOKE_TIMEOUT_SECS is a FLOOR: it only raises the ceiling when set ABOVE the
    already-computed `timeout`, and is a no-op at or below it.

    That FLOOR sentence is true but incomplete on its own: on the `_OP_TIMEOUTS_STATE ==
    "ok"` branch the binding term is usually the engine's own op budget, not the client
    floor — CC_INVOKE_TIMEOUT_SECS provably cannot clear the timeout in that case. A
    client-side floor cannot clear an engine-budget timeout; only
    COORDINATOR_DISPATCH_TIMEOUT_SECS (`coordinator_core/ipc.py::DISPATCH_TIMEOUT_SECS`)
    raises that budget. An operator who read only the FLOOR sentence set
    CC_INVOKE_TIMEOUT_SECS=300, watched the same 30s-derived timeout recur, and had to
    read ipc.py to find the real knob — see
    `cross-repo/inbox/2026-08-10-doe-claude-em-wsc-tail-exceeds-the-30s-dispatch-budget.md`.
    This function now names both knobs and which side of the wait each governs whenever
    the engine-budget derivation is known; the degraded branch (dump unavailable) still
    names both but does not assert a budget number it could not read.

    The returned text always starts with `_TIMEOUT_MESSAGE_PREFIX` — `is_timeout_error`
    depends on that invariant.
    """
    floor = _read_positive_int_env("CC_INVOKE_TIMEOUT_SECS", 10)
    margin = _read_positive_int_env("CC_INVOKE_CLIENT_MARGIN_SECS", 10)
    if _OP_TIMEOUTS_STATE == "ok":
        budget = _OP_TIMEOUTS_MAP.get(op, _OP_TIMEOUTS_MAP["__default__"])
        budget_int = int(budget)
        derivation = f"max(floor {floor}, engine budget {budget_int} + margin {margin})"
        knobs_line = (
            f"  To raise the engine budget itself (currently {budget_int}s for op={op}), set\n"
            "  COORDINATOR_DISPATCH_TIMEOUT_SECS: CC_INVOKE_TIMEOUT_SECS governs only the\n"
            "  client-side wait; COORDINATOR_DISPATCH_TIMEOUT_SECS governs the op's own budget."
        )
    else:
        derivation = f"floor {floor} (engine op-budget dump unavailable)"
        knobs_line = (
            "  CC_INVOKE_TIMEOUT_SECS governs only the client-side wait; the op's own budget\n"
            "  (raised via COORDINATOR_DISPATCH_TIMEOUT_SECS) could not be read here."
        )
    return (
        f"{_TIMEOUT_MESSAGE_PREFIX}{timeout}s (op={op}) — "
        "coordinator_core.invoke did not respond\n"
        f"  Exceeded {timeout}s = {derivation}. The engine may simply be busy.\n"
        "  CC_INVOKE_TIMEOUT_SECS is a FLOOR, not a ceiling: it only raises this number when\n"
        f"  set ABOVE {timeout}s. Setting it at or below {timeout}s changes nothing.\n"
        f"{knobs_line}"
    )


# ---------------------------------------------------------------------------
# Public: cc_invoke(op, params, repo_root) -> dict
# ---------------------------------------------------------------------------

def cc_invoke(
    op: str,
    params: dict[str, Any],
    repo_root: str,
    *,
    _claude_klabauter_root: str | None = None,
    _stderr_sink: list[str] | None = None,
) -> dict[str, Any]:
    """Spawn coordinator_core.invoke and return the bare result dict.

    Fail-closed ladder (rungs 1-3 shared with the retired bash cc_invoke — see the
    module docstring's Port of note; rung 4 is this module's own envelope-parse
    convention — see the module docstring's DR-215 ref):
      (1) Timeout → raise.
      (2) Nonzero process exit → distinguish ImportError (engine-won't-start)
          from op-error → raise either way.
      (3) Empty stdout → raise.
      (4) Parse the JSON-RPC envelope; require top-level 'result' key;
          return the BARE result dict (strips jsonrpc/id/result wrapper).

    Params transport: ALWAYS ``--params-file`` (a tempfile), never argv — matches
    cc_invoke_bare()'s own transport unconditionally, not behind a size threshold.
    Windows `CreateProcess` caps a command line at 32767 characters; a `params`
    dict carrying a `paths` list (e.g. a percolate round's changed-file set) can
    hold thousands of entries and measurably exceeds that before 1000 entries,
    raising `FileNotFoundError: [WinError 206] The filename or extension is too
    long` before the child ever starts (see the dispatch that fixed this: DoE
    percolate-round.py/scoped-git-commit's own `--pathspec-from-file` sibling
    fix, one layer up). A size-threshold branch was considered and rejected: it
    would leave the large-payload path as the rarely-exercised one, which is
    exactly how the argv form survived this long. `--params-file` is already
    unconditional in cc_invoke_bare(); this now matches it rather than
    special-casing "small" callers onto the narrower, capped transport.
    The engine-side receiver (`coordinator_core/invoke/__main__.py`) already
    accepts `--params-file` independently of `--bare` — no positional
    `params_json` argv is ever passed by this function anymore, so mode
    selection (file vs. argv) is unambiguous: the child only ever sees
    `--params-file <path>` for this call convention.

    Args:
        _claude_klabauter_root: already-resolved CLAUDE_KLABAUTER_ROOT (forwarded by route() to avoid a
            second resolution on the State-2 path). If None, resolved here via
            _resolve_claude_klabauter_root(). Keyword-only; callers outside route() should omit it.
        _stderr_sink: when provided, the child's captured stderr text is appended to
            this list on the SUCCESS return path (rung 4) if non-empty — lets a caller
            recover diagnostic text a well-formed op-level refusal envelope wrote to
            stderr (e.g. `_setup_error()`'s reason), which would otherwise be discarded
            once rc==0 and stdout parses cleanly. Never consulted on the raise paths
            above (those already fold stderr_text into their own message). Keyword-only;
            most callers omit it.

    Raises:
        RuntimeError: on any transport failure. Never returns legacy after a spawn.
    """
    # An already-resolved root is accepted from route() to avoid a double resolution
    # on the State-2 path.
    claude_klabauter_root = _claude_klabauter_root if _claude_klabauter_root is not None else _resolve_claude_klabauter_root()
    params_json = json.dumps(params, separators=(",", ":"))

    # Build subprocess env: pass through os.environ, set CLAUDE_KLABAUTER_ROOT, prepend PYTHONPATH.
    # Mirrors _cc_resolve_deps() PYTHONPATH idempotency check in the shell transport.
    env = _build_subprocess_env(claude_klabauter_root)

    # Per-op timeout ceiling (DEC-1..3): _t = max(FLOOR, engine_budget(op)+MARGIN),
    # resolved once-per-process from the engine's --dump-op-timeouts map (flat-FLOOR
    # fallback when absent/errored). Shares the ceiling path with cc_invoke_bare so a
    # composite op (e.g. session.boot_sweep, engine budget 30s) never gets a facade
    # timeout tighter than its engine-side DISPATCH_TIMEOUT_SECS budget — the flat
    # CC_INVOKE_TIMEOUT_SECS floor (default 10s) was strangling heavy ops here.
    timeout = _op_timeout_ceiling(op, claude_klabauter_root, env)

    # Spawn invoke with timeout cap.
    # stderr captured to distinguish ImportError from op-error (same purpose as _stderr_tmp in sh).
    rc: int = 0
    stdout_text: str = ""
    stderr_text: str = ""

    # params ride a temp file (--params-file), NOT argv — ARG_MAX-immune (see the
    # docstring's Params transport note above). Written, closed, passed by path,
    # and unlinked in finally so a large payload never overflows argv. Mirrors
    # cc_invoke_bare()'s identical --params-file handling below.
    _params_fd, _params_path = tempfile.mkstemp(prefix="cc-invoke-params-")
    try:
        with os.fdopen(_params_fd, "w", encoding="utf-8", newline="\n") as _pf:
            _pf.write(params_json)
        argv = [
            sys.executable, "-m", "coordinator_core.invoke", op,
            "--params-file", _params_path,
        ]
        if _should_pass_repo(op, claude_klabauter_root):
            argv += ["--repo", repo_root]

        try:
            proc = subprocess.run(
                # Review: cross-slice (DR-148) — sys.executable ensures the same interpreter that
                # loaded cc_invoke.py is used; hardcoded "python3" breaks on Windows.
                argv,  # popup-safe-env-suppressed
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                env=env,
                cwd=claude_klabauter_root,
                **_no_console_kw(claude_klabauter_root),
            )
            rc = proc.returncode
            stdout_text = proc.stdout
            stderr_text = proc.stderr
        except subprocess.TimeoutExpired:
            # (1) Timeout — mirrors the retired bash transport's cs_timeout exit 124 branch.
            raise RuntimeError(_timeout_exceeded_message(op, timeout))
    finally:
        try:
            os.unlink(_params_path)
        except OSError:
            pass

    # (2) Nonzero process exit — distinguish engine-start failure from op-level error.
    # (3) Empty stdout — invoke always produces output on success.
    # Shared fail-closed rungs (used identically by cc_invoke_bare).
    _raise_on_process_failure(rc, stdout_text, stderr_text, op, claude_klabauter_root)

    # (4) Parse the JSON-RPC envelope and extract the bare result object.
    #     Mirrors the inline python3 -c '...' parse in the retired bash transport.
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"cc_invoke: invoke stdout is not valid JSON (op={op}): {exc}"
        ) from exc

    if not isinstance(envelope, dict):
        raise RuntimeError(
            f"cc_invoke: envelope is not a JSON object (op={op}): "
            f"got {type(envelope).__name__!r}"
        )

    # Error envelope: op returned {"error": {...}} with any exit code.
    if "error" in envelope and "result" not in envelope:
        err = envelope["error"]
        if isinstance(err, dict):
            raise RuntimeError(
                f"cc_invoke: op returned JSON-RPC error envelope (op={op}): "
                f"code={err.get('code', '?')} message={err.get('message', '?')}"
            )
        raise RuntimeError(
            f"cc_invoke: op returned JSON-RPC error envelope (op={op}): {err!r}"
        )

    # Missing result key (and no error key detected above).
    if "result" not in envelope:
        top_keys = list(envelope.keys())
        raise RuntimeError(
            f"cc_invoke: envelope missing 'result' key (op={op}): "
            f"top-level keys={top_keys!r}"
        )

    # SUCCESS — return bare result dict.
    # Callers read top-level keys directly (e.g. result['out_path']).
    # NEVER result['result']['X'] — cc_invoke already stripped the wrapper.
    if _stderr_sink is not None and stderr_text.strip():
        _stderr_sink.append(stderr_text)
    return envelope["result"]


# ---------------------------------------------------------------------------
# Public: cc_invoke_bare(op, params, repo_root) -> dict
# ---------------------------------------------------------------------------

def cc_invoke_bare(
    op: str,
    params: dict[str, Any],
    repo_root: str,
    *,
    _claude_klabauter_root: str | None = None,
    _stderr_sink: list[str] | None = None,
) -> dict[str, Any]:
    """Bare-transport spawn of coordinator_core.invoke — the shared Python promotion of
    the retired bash cc_invoke (--bare ladder + DEC-1..3 op-timeout budget; see module Port of note).

    Downstream facades import THIS instead of inlining a per-facade mirror of the shell
    --bare ladder (the campaign anti-goal). Differs from cc_invoke() in three ways, each a
    faithful port of the shell oracle:
      - `--bare`: coordinator_core.invoke emits the BARE result object directly (no
        jsonrpc/id/result envelope), so stdout on rc0 IS the result dict — no envelope
        strip. cc_invoke() uses the non-bare envelope-parse convention.
      - `--params-file`: params ride a temp file, not argv — ARG_MAX-immune on
        Windows/msys, where a ~50KB+ payload overflows a bare argv arg (exit 126 HALT).
      - Per-op timeout ceiling (DEC-1..3): _t = max(FLOOR, engine_budget(op)+MARGIN) for
        every op, resolved once-per-process from the engine's --dump-op-timeouts map;
        flat-FLOOR fallback when that surface is absent (older engine repo) or errored.

    Shares the timeout / nonzero-exit ImportError-vs-op / empty-stdout fail-closed rungs
    with cc_invoke() via _raise_on_process_failure — one ladder, two call conventions.

    Args:
        _claude_klabauter_root: already-resolved CLAUDE_KLABAUTER_ROOT (forwarded by route paths to avoid a
            second resolution). Keyword-only; callers outside a router should omit it.
        _stderr_sink: when provided, the child's captured stderr text is appended to
            this list on the SUCCESS return path if non-empty — same purpose as
            cc_invoke()'s param; see its docstring. Keyword-only; most callers omit it.

    Returns the bare result dict on success. Raises RuntimeError on any transport failure;
    NEVER returns legacy after a spawn.
    """
    claude_klabauter_root = _claude_klabauter_root if _claude_klabauter_root is not None else _resolve_claude_klabauter_root()
    params_json = json.dumps(params, separators=(",", ":"))
    env = _build_subprocess_env(claude_klabauter_root)

    # Per-op timeout ceiling (DEC-1..3) — may spawn the op-budget dump once per process.
    # Resolved BEFORE the op spawn so the ceiling reflects the engine's budget for this op.
    timeout = _op_timeout_ceiling(op, claude_klabauter_root, env)

    rc: int = 0
    stdout_text: str = ""
    stderr_text: str = ""

    # params ride a temp file (--params-file), NOT argv — ARG_MAX-immune. Written, closed,
    # passed by path, and unlinked in finally so a large payload never overflows argv.
    _params_fd, _params_path = tempfile.mkstemp(prefix="cc-invoke-params-")
    try:
        with os.fdopen(_params_fd, "w", encoding="utf-8", newline="\n") as _pf:
            _pf.write(params_json)
        _argv = [
            sys.executable, "-m", "coordinator_core.invoke", op,
            "--bare", "--params-file", _params_path,
        ]
        if _should_pass_repo(op, claude_klabauter_root):
            _argv += ["--repo", repo_root]
        try:
            proc = subprocess.run(
                _argv,  # popup-safe-env-suppressed
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                env=env,
                cwd=claude_klabauter_root,
                **_no_console_kw(claude_klabauter_root),
            )
            rc = proc.returncode
            stdout_text = proc.stdout
            stderr_text = proc.stderr
        except subprocess.TimeoutExpired:
            # (1) Timeout — mirrors the retired bash transport's cs_timeout exit 124 branch.
            raise RuntimeError(_timeout_exceeded_message(op, timeout))
    finally:
        try:
            os.unlink(_params_path)
        except OSError:
            pass

    # (2) nonzero exit + (3) empty stdout — shared fail-closed rungs.
    _raise_on_process_failure(rc, stdout_text, stderr_text, op, claude_klabauter_root)

    # (4) --bare: stdout IS the bare result object already (no jsonrpc/id/result wrapper,
    #     no second strip-the-envelope spawn). The engine only reaches rc0 on a success
    #     response (a JSON-RPC error always exits nonzero, caught by rung (2)), so on this
    #     path stdout is json.dumps(response["result"]). Parse to a dict for the caller.
    try:
        result = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"cc_invoke: --bare stdout is not valid JSON (op={op}): {exc}"
        ) from exc
    if not isinstance(result, dict):
        raise RuntimeError(
            f"cc_invoke: --bare result is not a JSON object (op={op}): "
            f"got {type(result).__name__!r}"
        )
    if _stderr_sink is not None and stderr_text.strip():
        _stderr_sink.append(stderr_text)
    return result


# ---------------------------------------------------------------------------
# State-1 remediation — W0.5 Option B+C (PM-ratified 2026-07-19): the engine repo
# is a MANDATORY prerequisite of coordinator in every environment. A seam-absent
# route() call is not a legitimate "no engine installed, degrade gracefully"
# outcome anymore — it is a broken install. Prior to this, State-1 silently
# delegated to legacy_fn(), and under the big-bang bash-cutover legacy_fn is
# almost always a thin per-caller stub that raises a generic, non-actionable
# "native seam required (no bash fallback)" message (see e.g.
# the retired bash sweep-shipped-handoffs.sh's _no_fallback). This wraps any legacy_fn
# failure on the seam-absent path with the SAME four-rung remediation ladder
# _resolve_claude_klabauter_root() itself walks, so every caller gets one consistent,
# actionable error instead of N different bespoke stub messages.
# ---------------------------------------------------------------------------

def _state1_remediation_message(
    op: str,
    attempted_claude_klabauter_root: str | None,
    *,
    registry_read_timed_out: bool = False,
) -> str:
    """Build the engine-install-specific remediation text for a State-1 (seam-absent) failure.

    Enumerates the four-rung CLAUDE_KLABAUTER_ROOT resolution ladder (mirrors
    _resolve_claude_klabauter_root's own rung order) so an operator sees exactly which
    rung to fix, instead of a bare caller-specific "no fallback wired" message.

    `registry_read_timed_out` (AC1/AC3, default False so the absent-key text
    below is unchanged byte-for-byte — AC2a): when True, `_resolve_claude_klabauter_root`
    raised `_RegistryReadTimeout` rather than genuinely finding no candidate —
    a transient reader timeout, not a missing/unregistered checkout. The
    clone/register remediation below is wrong for that case, so it gets its
    own text instead of sharing the generic one.
    """
    if registry_read_timed_out:
        return (
            f"cc_invoke: native seam resolution unavailable for op={op!r} — "
            f"{_REGISTRY_READ_TIMEOUT_TOKEN} ({_MACHINE_LOCAL_READ_TIMEOUT_SECS}s bound) "
            "while resolving CLAUDE_KLABAUTER_ROOT via the machine-local registry, and self-location "
            "also missed.\n"
            "  This machine's declared load norm is 50-70 concurrent LLM sessions "
            "(CLAUDE.md § Load norm); a subprocess-bounded registry read timing out "
            "under that load is expected, not a sign claude-klabauter is unregistered.\n"
            "  Retry the operation."
        )
    root_line = (
        f"  CLAUDE_KLABAUTER_ROOT resolved to {attempted_claude_klabauter_root!r} but coordinator_core.invoke "
        "was not importable from it (broken/partial checkout).\n"
        if attempted_claude_klabauter_root
        else "  CLAUDE_KLABAUTER_ROOT could not be resolved via any rung below.\n"
    )
    return (
        f"cc_invoke: native seam unavailable for op={op!r} — claude-klabauter is a mandatory "
        "coordinator dependency in every environment (W0.5 Option B+C, 2026-07-19); there is "
        "no bash fallback under the big-bang cutover.\n"
        f"{root_line}"
        "  Resolution ladder (in order):\n"
        "    1. CLAUDE_KLABAUTER_ROOT environment variable\n"
        "    2. <settings-home>/machine-local/.claude-klabauter-root pointer file\n"
        "    3. `machine-local get repos.claude_klabauter` registry entry\n"
        "    4. coordinator_core.invoke importable from the resolved root\n"
        "  Remediation: clone claude-klabauter as a sibling repo "
        "(git clone https://github.com/dbc-oduffy/claude-klabauter) and register it — "
        "set $CLAUDE_KLABAUTER_ROOT, write the settings-home pointer file, or run "
        "`machine-local set repos.claude_klabauter /path/to/claude-klabauter` — then retry. "
        "See docs/install/AGENT.md § Fail-loud claude-klabauter resolution, or run /coordinator:setup."
    )


# ---------------------------------------------------------------------------
# Public: route(op, params, repo_root, legacy_fn) — two-state gate
# ---------------------------------------------------------------------------

def route(
    op: str,
    params: dict[str, Any],
    repo_root: str,
    legacy_fn: Callable[[], Any],
    *,
    _stderr_sink: list[str] | None = None,
) -> Any:
    """Two-state coordinator_core.invoke router.

    State-1 (seam absent — coordinator_core.invoke not importable via CLAUDE_KLABAUTER_ROOT):
        Call legacy_fn(); its return value passes through unchanged on success.
        If legacy_fn() raises, the exception is wrapped in an engine-install-specific
        remediation RuntimeError (the four-rung CLAUDE_KLABAUTER_ROOT resolution ladder) instead
        of propagating whatever generic message the caller's legacy_fn happened to
        raise (see _state1_remediation_message). No native spawn attempted either way.
        Trigger: CLAUDE_KLABAUTER_ROOT unresolvable OR find_spec("coordinator_core.invoke") returns None.

    State-2 (seam present — coordinator_core.invoke importable):
        Call cc_invoke(op, params, repo_root); propagate result or exception.
        Transport failure → raise (HARD error). NEVER fall back to legacy_fn on State-2.

    Negative-spec: transport failure after seam-confirmation is NOT a legacy trigger.
    Masking a live-but-broken engine via silent legacy fallback is the anti-pattern this
    design explicitly rejects (DR-215 anti-scope).

    Args:
        _stderr_sink: forwarded to cc_invoke() unchanged (see its docstring); no effect
            on the State-1/legacy_fn path. Keyword-only; most callers omit it.
    """
    # Resolve CLAUDE_KLABAUTER_ROOT; unresolvable root → treat as seam-absent (State-1).
    # Rationale: if the registry doesn't know about the engine repo, the seam is definitely absent.
    claude_klabauter_root: str | None
    _registry_read_timed_out = False
    try:
        claude_klabauter_root = _resolve_claude_klabauter_root()
    except _RegistryReadTimeout:
        # Caught ahead of the general RuntimeError below (subclass) — threads the
        # distinguishable outcome (AC1/AC3) to _state1_remediation_message instead
        # of collapsing to the same "unresolvable root" the absent-key case gets.
        claude_klabauter_root = None
        _registry_read_timed_out = True
    except RuntimeError:
        claude_klabauter_root = None

    # Disk-presence gate (State-1 check).
    if claude_klabauter_root is None or not _seam_present(claude_klabauter_root):
        try:
            return legacy_fn()
        except Exception as exc:
            raise RuntimeError(
                _state1_remediation_message(
                    op, claude_klabauter_root, registry_read_timed_out=_registry_read_timed_out
                )
            ) from exc

    # State-2: seam confirmed present — route native; propagate or raise.
    # HARD contract: do NOT catch exceptions and fall to legacy_fn here.
    # Forward the already-resolved claude_klabauter_root to avoid a second _resolve_claude_klabauter_root()
    # call inside cc_invoke() on this path.
    return cc_invoke(op, params, repo_root, _claude_klabauter_root=claude_klabauter_root, _stderr_sink=_stderr_sink)


# ---------------------------------------------------------------------------
# Public: route_mutation(op, params, repo_root, legacy_fn) — mutation-aware transport
# ---------------------------------------------------------------------------

class RouteMutationError(RuntimeError):
    """route_mutation refusal — carries the full offending result payload.

    The raised message alone only carries exit_code and a failed *count*; a
    caller catching this exception (or a human reading a traceback) has no way
    to recover the actual failed-item detail or error text without re-deriving
    it from logs. `.result` gives structured access to the full dict route()
    returned, mirroring the shell oracle's STDOUT PASSTHROUGH contract
    (the retired bash strangler facade's STDOUT PASSTHROUGH contract) where the full captured JSON is always
    re-emitted alongside the refusal.

    `.op_stderr` (default "") carries the child invoke process's captured stderr text,
    when any — this is where a well-formed-but-refusing op (e.g. `_setup_error()`)
    writes its own diagnostic sentence, which a bare exit_code/failed[] inspection of
    `.result` never sees. Folded into the raised message too, so a bare `str(exc)` is
    self-diagnosing without the caller needing to reach for `.op_stderr` explicitly.
    """

    def __init__(self, message: str, result: dict[str, Any], op_stderr: str = "") -> None:
        super().__init__(message)
        self.result = result
        self.op_stderr = op_stderr


def route_mutation(
    op: str,
    params: dict[str, Any],
    repo_root: str,
    legacy_fn: Callable[[], Any],
) -> Any:
    """Mutation-aware sibling of route() — honors the engine repo's in-envelope exit_code/failed/error.

    Purpose: route() returns the BARE result dict on transport success, but the engine repo's
    op-level refusals (build_setup_error_result -> {"exit_code": 1, ...}; build_act_result
    with partial/total failure -> {"exit_code": 2, "failed": [...]}) live INSIDE that
    result payload with no top-level 'error' key — cc_invoke's envelope ladder only
    raises on transport failure, so a bare route() call returns these refusals UNRAISED.
    route_mutation is the Python sibling of the shell transport's
    strangle_route_mutation() (see module Port of note): after a successful
    route(), it inspects the result for exit_code/failed/error and raises
    RouteMutationError so mutation callers (e.g. cross-repo-memo send) fail loud on an
    op-level refusal instead of proceeding as if the write succeeded. Closes the DR-215
    exit_code trap.

    Two distinct native refusal shapes are caught, mirroring strangle_route_mutation's
    documented two-shape contract:
      (1) {"exit_code": N, ...} with N != 0 — the handoff/memo _err shape.
      (2) {"error": "..."} with exit_code absent or 0 — the completion_ops/plan_ops
          shape (coordinator_core/ops/completion_ops.py, plan ops). This is DISTINCT
          from the top-level JSON-RPC error envelope cc_invoke() already raises on
          (cc_invoke.py's "error" in envelope check operates on the OUTER envelope,
          mutually exclusive with a "result" key being present) — shape (2) here is an
          "error" key nested INSIDE a present "result" payload, which cc_invoke's
          envelope ladder does not see and returns as an ordinary bare result. Without
          this branch, shape (2) would be a silent phantom success.
      The `failed`-list check (not present in the shell oracle) is this helper's own
      addition for build_act_result partial-failure shapes.

    State-1/State-2 gating and transport-fail-raises behavior are inherited unchanged
    from route() — this function only adds a post-hoc inspection of a successful result.

    Raises:
        RouteMutationError (a RuntimeError subclass with a `.result` attribute holding
            the full offending payload): if the result is a dict with a non-None,
            non-zero 'exit_code'; a truthy 'failed'; or a non-empty string 'error' with
            exit_code absent/0. `.op_stderr` carries the child invoke process's own
            stderr text when the refusing op wrote a diagnostic there (e.g.
            `_setup_error()`), and is folded into the raised message too — a well-
            formed refusal envelope's `failed[]` can be empty (nothing per-item to
            report) while stderr still names exactly what went wrong.
    """
    _stderr_sink: list[str] = []
    result = route(op, params, repo_root, legacy_fn, _stderr_sink=_stderr_sink)

    if isinstance(result, dict):
        exit_code = result.get("exit_code")
        # Coerce to int the same way the retired bash oracle did (its
        # inline python3 parser wraps `ec` in int()/except, falling back to 0 on
        # cast failure) — defends against a stringly-typed exit_code producing a
        # Python cross-type false-positive ("0" != 0 is True).
        exit_code_int: int | None
        if exit_code is None:
            exit_code_int = None
        else:
            try:
                exit_code_int = int(exit_code)
            except (TypeError, ValueError):
                exit_code_int = 0

        failed = result.get("failed")
        # `failed` may not be list-shaped on a malformed/future-drifted payload (an
        # int, a bool, ...) — guard with isinstance before len() so an unexpected
        # shape still raises the intended RouteMutationError instead of crashing
        # with an uncaught TypeError.
        failed_is_list = isinstance(failed, list)
        failed_count = len(failed) if failed_is_list else 0
        failed_truthy = bool(failed)

        # The completion_ops/plan_ops error-field refusal shape (see docstring
        # above) only TRIGGERS a refusal when exit_code is absent/0, so it doesn't
        # double-report a shape-(1) refusal as shape-(2) too. `error_text_available`
        # is broader: whenever an 'error' string is present it's folded into the
        # message regardless of which condition triggered the raise, so a shape-(1)
        # refusal carrying an 'error' key alongside its non-zero exit_code doesn't
        # discard that detail either.
        error_field = result.get("error")
        error_text_available = isinstance(error_field, str) and len(error_field) > 0
        error_field_present = exit_code_int in (None, 0) and error_text_available

        if (
            (exit_code_int is not None and exit_code_int != 0)
            or failed_truthy
            or error_field_present
        ):
            detail_parts = [f"exit_code={exit_code!r}"]
            if failed_is_list:
                detail_parts.append(f"failed={failed_count}")
            elif failed_truthy:
                detail_parts.append(f"failed={failed!r} (non-list shape)")
            if error_text_available:
                detail_parts.append(f"error={error_field!r}")
            op_stderr = "\n".join(s.strip() for s in _stderr_sink if s.strip())
            message = f"route_mutation: op={op!r} refused ({', '.join(detail_parts)})"
            if op_stderr:
                message += f"\n  op stderr: {op_stderr}"
            raise RouteMutationError(message, result, op_stderr=op_stderr)

    return result
