"""
coordinator_core.ops.session_baton_mint — "session_baton.mint" op.

Purpose: mint or update a session's lazy baton record
(``coordinator_core.session_baton.store``, C1) with the session's first user
prompt. Idempotent — designed to be invoked from a ``UserPromptSubmit`` hook,
NEVER ``SessionStart`` (whose input carries no prompt field at all — see
``docs/plans/2026-08-18-a-session-always-has-a-baton.md`` § Anti-scope). The
op itself takes the prompt as a plain argument and does not care which hook
supplies it; the hook wiring choice stays the caller's (DoE-side).

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C2

Self-registration: importing this module calls
``register_op("session_baton.mint", _handler)`` as a side-effect. Add this
module to ``coordinator_core/ops/__init__.py`` to trigger registration at
``start_server()`` time.

Op scope "none" (same fleet-generic per-repo target-resolution convention as
``coordinator_core.ops.session.record_pickup``): the handler's own
``repo_root`` arg is unused (always ``None`` for scope-"none" ops). This op
instead accepts an optional ``cwd`` wire param, threaded verbatim into
``coordinator_core.session_baton.store``'s own ``cwd`` kwarg — the SAME
optional-cwd convention that store already exposes (C1), so a caller
resolving a repo other than claude-klabauter's own dispatching tree (or a test
fixture) can pin it explicitly. ``cwd`` omitted resolves against the current
process cwd, exactly as store's own functions do.

Budget (D-A: this fires on the first prompt of EVERY session — 39-92/day in
this repo alone, 34 live concurrently at measurement): one file write, no
subprocess, no git invocation of this op's own. It delegates entirely to
store's read/merge; store's own git-common-dir resolution
(``coordinator_core.git.repo_root :: git_common_dir``) walks parent
directories for a ``.git`` entry rather than spawning ``git rev-parse`` in
the ordinary case (see that module's own docstring) — this op adds no
further process-spawning on top of that. Asserted in the test surface via a
``subprocess.run`` monkeypatch that raises if the git-spawn fallback is ever
reached.

Idempotent (C2's own contract): a second call for the SAME ``session_id``
updates the record in place — ``store.merge_baton`` read-modify-writes ONE
file, never duplicates. The FIRST prompt this op ever sees for a session is
preserved permanently: once ``first_prompt`` is set (non-``None``), a later
call supplying a different ``prompt`` value does NOT overwrite it — this is
what makes the field honestly mean "first prompt of the session", not merely
"most recently minted", and protects the record against a caller that (in
spite of the documented once-per-session ``UserPromptSubmit`` contract) fires
this op more than once in a session.

Negative-spec:
    - Does NOT write anything outside ``.git/coordinator-sessions/<sid>/`` —
      inherits C1's store's own hard constraint verbatim; this op has no
      write path of its own beyond delegating to store.
    - ``title``/``intent`` ARE accepted (this chunk takes the escape clause
      below) — but only ever threaded into ``merge_baton`` when the caller
      actually supplies them; an omitted param must never reach the call
      (``store``'s ``_UNSET`` sentinel means an explicit ``None`` would
      overwrite). Unlike ``first_prompt``, neither has a capture-once guard:
      a later call with a new value replaces the stored one by design.
    - Does NOT promote a baton into a real handoff artifact — that is C3
      (``ops/session_baton_promote.py``); this op has no knowledge of
      promotion and never reads/writes ``promoted_to``.
    - Does NOT overwrite an already-set ``first_prompt`` — see "Idempotent"
      above; this is a deliberate first-wins policy, not an oversight.
"""

from __future__ import annotations

from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session_baton import store


def _err(msg: str) -> dict:
    """Return an exit_code=1 setup-error reply."""
    return {
        "exit_code": 1,
        "error": msg,
        "session_id": None,
        "baton_path": None,
        "created": False,
        "first_prompt": None,
    }


@register_op("session_baton.mint")
def _handler(params: dict, repo_root: Optional[str] = None) -> dict:
    """JSON-RPC "session_baton.mint" handler — see module docstring for the
    full contract.

    Params:
        session_id (str, required) — the session whose baton is being
                    minted/updated.
        prompt      (str, optional) — the prompt to capture as
                    ``first_prompt``. Ignored (silently) when the record
                    already carries a non-``None`` ``first_prompt`` — see
                    module docstring, "Idempotent": the field is captured
                    once, permanently.
        cwd         (str, optional) — working directory to resolve the
                    session hub from; threaded verbatim into store's own
                    ``cwd`` kwarg. Defaults to the current process cwd
                    (store's own default) when omitted.
        title       (str, optional) — EM-supplied session title. Threaded
                    into ``merge_baton`` only when supplied; an omitted
                    ``title`` leaves any existing stored value untouched.
                    Unlike ``prompt``/``first_prompt``, overwritable on every
                    call — no capture-once guard.
        intent      (str, optional) — EM-supplied session intent. Same
                    omitted-means-untouched, always-overwritable contract as
                    ``title``.

    Returns:
        exit_code    int        0=ok, 1=setup-error (bad params /
                                 unresolvable session hub)
        error        str|None
        session_id   str|None
        baton_path   str|None   absolute path to the written baton.json
        created      bool       True iff this call minted the record for the
                                 first time (no prior created_at on disk) —
                                 unaffected by whether this call also carried
                                 a title/intent
        first_prompt str|None   the record's first_prompt AFTER this call
    """
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return _err("session_baton.mint requires a non-empty session_id")
    session_id = session_id.strip()

    prompt = params.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        return _err("session_baton.mint: prompt must be a string when supplied")

    cwd = params.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return _err("session_baton.mint: cwd must be a string when supplied")

    title = params.get("title")
    if title is not None and not isinstance(title, str):
        return _err("session_baton.mint: title must be a string when supplied")

    intent = params.get("intent")
    if intent is not None and not isinstance(intent, str):
        return _err("session_baton.mint: intent must be a string when supplied")

    existing = store.read_baton(session_id, cwd)
    created = existing.get("created_at") is None

    merge_kwargs = {}
    if prompt is not None and existing.get("first_prompt") is None:
        merge_kwargs["first_prompt"] = prompt
    if title is not None:
        merge_kwargs["title"] = title
    if intent is not None:
        merge_kwargs["intent"] = intent

    merged = store.merge_baton(session_id, cwd, **merge_kwargs)
    if merged is None:
        # `None` has two causes since C6 of docs/plans/2026-08-19-batons-unify-into-
        # one-successor.md removed the store's `mkdir`: an unresolvable session hub,
        # and a session directory that does not exist yet (`cs_init` not having run).
        # Reporting the second as the first sends an operator hunting a git-repo
        # problem that is not there.
        if store.baton_path(session_id, cwd) is None:
            return _err(
                "session_baton.mint: could not resolve the session hub for "
                f"session_id={session_id!r} (not a git repo?)"
            )
        return _err(
            "session_baton.mint: no session directory for "
            f"session_id={session_id!r}; cs_init has not run for this session"
        )

    baton_path = store.baton_path(session_id, cwd)
    return {
        "exit_code": 0,
        "error": None,
        "session_id": session_id,
        "baton_path": str(baton_path) if baton_path is not None else None,
        "created": created,
        "first_prompt": merged.get("first_prompt"),
    }
