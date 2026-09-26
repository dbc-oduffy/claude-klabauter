
from __future__ import annotations

from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session_baton import store


def _err(msg: str) -> dict:
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
                    call — no capture-once guard. NOTE: JSON-RPC cannot
                    distinguish an omitted ``title`` from an explicit
                    ``title: null`` on the wire — both decode to Python
                    ``None`` and both no-op here. There is no way to clear a
                    stored title through this op; that capability, if ever
                    needed, would require a distinct sentinel param.
        intent      (str, optional) — EM-supplied session intent. Same
                    omitted-means-untouched, always-overwritable contract as
                    ``title`` — including the same JSON ``null``-vs-omitted
                    caveat above.

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
