"""
coordinator_core.cli_entry — the in-process entrypoint for `coordinator/bin/`
trampolines, giving them the session scope-touch recording that
`ipc.dispatch_message` already gives the subprocess route.

Purpose: `run_op_main()` replaces the `_import_main()` + `sys.exit(op_main(argv))`
tail that the near-byte-identical trampolines under `coordinator/bin/` share. It
resolves the op module, runs its `main(argv)` inside a declare-write collection,
and hands whatever the op declared to the single existing recorder.

Why (DR-276): the trampolines reach ops by plain in-process import, which never
passes `ipc.dispatch_message` — the sole process-level chokepoint, and the only
place a handler's declared writes become a `session.scope.touch()` claim. Files
those CLIs write therefore carry no claim, land in `orphans`, and are refused at
the `scoped_git_commit` sink
(`state/improvement-queue/2026-08-03-sanctioned-mutating-clis-record-no-sessi-dedd1f017d02.yaml`,
filed with an observed instance).

C13 (the warm engine's entry-path convergence): this is one of four engine
entry paths, and the only one of the four that is authored in this repo's
`coordinator_core/warm/`-adjacent scope rather than `ipc.py`. Its declared-
write COLLECTION now opens through `warm.entry_seam.per_request_state`
rather than `session.declared_writes.collecting()` directly, so this path
and `ipc._dispatch_message_impl`'s own per-request scoping (C11) converge on
one shared mechanism instead of each holding its own copy. RECORDING is
unchanged — still `_record()` below, handing off to `ipc._record_self_
reported_touches` verbatim. See `coordinator_core/warm/entry_seam.py`'s
module docstring for the full four-path inventory.

Negative-spec (RAG-bait):
    This is NOT an RPC invoke and does NOT spawn a subprocess. It is the
    in-process path, deliberately — the trampolines' existing docstrings
    ("a plain in-process import, not an RPC invoke, so cc_invoke's
    subprocess-spawn transport is deliberately NOT used") state the rationale
    and it is unchanged by DR-276. What changes is only that the writes get
    claimed.

    This module adds NO second recorder dialect. Recording is delegated to
    `ipc._record_self_reported_touches` verbatim, via the same
    `_scope_touch_paths` envelope the subprocess path builds, so path
    normalization, cross-repo containment, the 16-path cap, and the fail-open
    guarantee all have exactly one implementation.

    An op that declares nothing behaves exactly as it did before adoption. No
    op is required to adopt `declare_write`.

Spec backlink: docs/decisions/DR-276-operator-clis-record-session-writes-at-a.md
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
from typing import Iterator, List, Optional, Sequence

from coordinator_core.warm.entry_seam import per_request_state

__all__ = ["run_op_main", "recording_declared_writes"]

_log = logging.getLogger(__name__)


def _record(declared: List[str], cwd: Optional[str]) -> None:
    """Hand `declared` to the one existing recorder.

    Reuses `ipc._record_self_reported_touches` by handing it the same
    `{_scope_touch_paths: [...]}` envelope shape the wire path builds, so the
    in-process path inherits every rule that contract already enforces rather
    than restating any of them here.

    Fail-open, unconditionally: a failure anywhere in recording must never fail
    an op that already succeeded — the same discipline the wire path applies.
    """
    if not declared:
        return
    try:
        from coordinator_core.ipc import (  # noqa: PLC0415 — deferred: ipc is heavy
            _SCOPE_TOUCH_PATHS_KEY,
            _record_self_reported_touches,
        )

        _record_self_reported_touches({_SCOPE_TOUCH_PATHS_KEY: list(declared)}, cwd)
    except Exception:  # noqa: BLE001 — fail-open is the contract
        _log.debug("cli_entry: scope-touch recording failed; op result unaffected", exc_info=True)


@contextlib.contextmanager
def recording_declared_writes(*, cwd: Optional[str] = None) -> Iterator[List[str]]:
    """Collect `declare_write()` calls made inside the `with` body and record
    them through the SAME `_record` path `run_op_main` uses — the one
    recorder, factored so both callers share it.

    Exists for operator CLIs whose own `main()` cannot route through
    `run_op_main` at all: a multi-zone orchestrator that calls an op's
    internals directly (e.g. `changelog_ops.append_day`) rather than
    delegating to that op's own `main(argv)`, because routing it through
    `run_op_main` would re-enter a DIFFERENT op's CLI contract. Without this
    helper, such a caller's only options were either forking its own
    collect-then-record dance around `ipc._record_self_reported_touches`
    directly — a bin-side CLI reaching into a private engine function,
    which is exactly the second recording dialect DR-276 set out not to
    create — or leaving its writes unclaimed as orphans. The live instance
    is `coordinator/bin/workday-complete-step9-append-changelog.py`, whose
    Zone B write calls `changelog_ops.append_day` directly rather than
    passing through `changelog_ops.main()`.

    Recording happens on `__exit__` unconditionally — clean exit, non-zero
    result, or an exception escaping the body — matching `run_op_main`'s own
    record-even-on-nonzero / record-even-on-raise behaviour exactly. An
    exception raised inside the body propagates unchanged; this context
    manager never suppresses it.

    Yields the (initially empty) list that fills with declared paths as the
    body runs; the caller does not need to read it, it exists so `_record`
    has something to hand to `ipc._record_self_reported_touches` on exit.
    """
    resolved_cwd = cwd if cwd is not None else os.getcwd()
    declared: List[str] = []
    try:
        with per_request_state(declared):
            yield declared
    finally:
        _record(declared, resolved_cwd)


def run_op_main(
    op_module: str,
    argv: Sequence[str],
    *,
    cwd: Optional[str] = None,
    entrypoint: str = "main",
) -> int:
    """Import `op_module`, run its `main(argv)` in-process, record its declared
    writes, and return the op's exit code.

    Args:
        op_module: Fully-qualified module path exposing `main(argv) -> int`,
            e.g. `"coordinator_core.ops.append_integrator_dispositions"`.
        argv: Argument vector WITHOUT the program name (`sys.argv[1:]`).
        cwd: Directory whose repo the session claim belongs to. Defaults to the
            process cwd, which is the caller's own worktree for every
            `coordinator/bin/` trampoline.
        entrypoint: Attribute name on `op_module` to call as `entrypoint(argv)`.
            Defaults to `"main"`, matching every existing trampoline. Exists
            because some operator CLIs deliberately expose a different, wider
            entrypoint than `main` — the live instance is
            `coordinator/bin/install-meta-repo-precommit-hook.py`, which calls
            `main_install_all` because it drives the pre-commit AND
            post-merge/post-checkout gates together; that module's own
            docstring warns against repointing it back at bare `main`, since
            that reintroduces the narrower gate set. Routing such a CLI
            through `run_op_main` with the default `entrypoint` would silently
            regress it to the narrower entrypoint — this parameter exists so
            that conversion doesn't have to happen.

    Returns:
        The op's exit code, verbatim — including when the entrypoint returns
        None, which is normalized to 0 to match `sys.exit(None)` semantics.

    Raises:
        ImportError: if `op_module` is not importable, or exposes no
            `entrypoint`. Deliberately NOT caught — an unresolvable op module
            is a transport failure the trampoline reports with its own exit
            code, not something to swallow into a success.

    Declarations are recorded even when the op exits non-zero: a handler that
    wrote a file and then failed still wrote that file, and leaving it unclaimed
    is precisely the orphan this seam exists to prevent.
    """
    module = importlib.import_module(op_module)
    op_main = getattr(module, entrypoint, None)
    if op_main is None:
        raise ImportError(f"{op_module} exposes no {entrypoint}(argv) entrypoint")

    with recording_declared_writes(cwd=cwd):
        code = op_main(list(argv))

    return 0 if code is None else int(code)
