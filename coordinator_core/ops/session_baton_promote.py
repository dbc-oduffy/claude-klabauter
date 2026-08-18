"""
coordinator_core.ops.session_baton_promote — "session_baton.promote" op.

Purpose: promote a lazy, session-scoped baton record (C1,
``coordinator_core.session_baton.store``) into a REAL handoff artifact under
``state/handoffs/``, on an earning event only (D-A) — a pickup adopting the
session baton as a fan-in predecessor, or a ``/handoff``, ``/quick-wrap``, or
``/workstream-complete`` ceremony firing. Most sessions never reach this op;
that is the intended steady state, not a shortfall — see D-A's own measured
rates (39-92 sessions/day against ~46/day ceremony mints).

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C3.

Idempotent: promoting a session whose baton already carries ``promoted_to``
returns the EXISTING path — never scaffolds a second artifact for the same
session (mirrors C2's ``session_baton.mint`` idempotence contract).

Mechanism: composes the EXISTING ``coordinator-doc-new`` seam
(``coordinator/bin/coordinator-doc-new.py --type handoff``), the SAME
subprocess pattern ``coordinator_core.baton_assemble.apply.
_dispatch_coordinator_doc_new`` already uses — this op does not scaffold
frontmatter or the section skeleton itself, and does not add a second
scaffolder. ``coordinator-doc-new``'s own negative-spec says it does NOT
generate body prose ("the EM authors the body; the scaffolder provides the
shape") — this op honours that by writing the captured ``first_prompt`` into
the body AFTER scaffold, via the same placeholder-comment edit path an EM or
executor would use by hand: a plain string replacement of the
"## What Was Accomplished" section's placeholder HTML comment. This keeps
``coordinator-doc-new``'s scaffolder negative-spec intact rather than
widening it (see plan body, C3: "Prefer post-scaffold: it leaves the
scaffolder's negative-spec intact").

Self-registration: importing this module calls
``register_op("session_baton.promote", _handler)`` as a side-effect. Add
this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at ``start_server()`` time — deliberately NOT done by this chunk, mirroring
C2 (``session_baton_mint.py``)'s own not-yet-wired state; op-registry wiring
is a separate concern from the op's own correctness.

Op scope "none" — same convention C2 uses (``coordinator_core.ops.session.
record_pickup``). The handler's own ``repo_root`` arg is unused (always
``None`` for scope-"none" ops); this op instead accepts an optional ``cwd``
wire param, threaded verbatim into ``coordinator_core.session_baton.store``'s
own ``cwd`` kwarg AND used as the subprocess ``cwd`` for the
``coordinator-doc-new`` invocation (the worktree root — the same directory
``store``'s own git-common-dir walk resolves the session hub relative to).
``cwd`` omitted resolves against the current process cwd.

Negative-spec:
    - Does NOT scaffold frontmatter directly — ``coordinator-doc-new`` stays
      the only writer of ``state/handoffs/`` frontmatter.
    - Does NOT decide WHEN to fire. D-A's earning-event set (pickup adoption,
      the three ceremony tails) is enforced by each CALLER, not by this op —
      this op is unconditionally idempotent either way, so a caller that
      calls it too eagerly costs nothing beyond the no-op read.
    - Does NOT re-promote. A second call for an already-promoted session
      returns the existing ``promoted_to`` path without touching disk again.
    - Does NOT raise on a scaffold or baton-merge failure — matches the
      baton store's fail-open posture (D-A/D-B): an advisory record and its
      promotion must never crash a ceremony tail or a pickup flow. Failures
      come back as ``exit_code: 1`` with a populated ``error``, never a
      propagated exception.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.session_baton import store
from coordinator_core.win_portability import no_console_creationflags

_NO_CONSOLE = no_console_creationflags()

#: The exact placeholder HTML comment coordinator-doc-new's handoff
#: scaffolder emits under "## What Was Accomplished"
#: (coordinator/bin/coordinator-doc-new.py :: _scaffold_handoff). A template
#: change upstream that renames/removes this comment degrades to a silent
#: no-op below rather than risk corrupting the freshly-scaffolded file.
_ACCOMPLISHED_PLACEHOLDER = (
    "<!-- Replace with what was built, fixed, or shipped this session. -->"
)


def _err(msg: str) -> dict:
    """Return an exit_code=1 setup-error reply (mirrors session_baton_mint's
    own ``_err`` shape)."""
    return {
        "exit_code": 1,
        "error": msg,
        "session_id": None,
        "handoff_path": None,
        "already_promoted": False,
    }


def _scaffold_via_doc_new(title: str, branch: Optional[str], cwd: str) -> str:
    """Invoke ``coordinator-doc-new --type handoff`` — same subprocess
    pattern as ``coordinator_core.baton_assemble.apply.
    _dispatch_coordinator_doc_new``. Returns the scaffolded file's path
    exactly as printed to stdout (coordinator-doc-new's own output
    contract)."""
    from coordinator_core.resolution.facade import resolve_operator_config

    claude_klabauter_bin = resolve_operator_config()["claude_klabauter_bin"]
    cli = str(Path(claude_klabauter_bin) / "coordinator-doc-new.py")
    args = ["--type", "handoff", "--title", title]
    if branch:
        args += ["--branch", branch]
    proc = subprocess.run(
        [sys.executable, cli, *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        **_NO_CONSOLE,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"coordinator-doc-new --type handoff {args}: failed "
            f"(rc={proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def _write_first_prompt_into_body(
    handoff_path: Path, first_prompt: Optional[str]
) -> None:
    """Post-scaffold body edit — the same placeholder-comment seam an EM
    would use via Edit, applied here as a plain read-replace-write. Never
    touches frontmatter. A no-op when ``first_prompt`` is falsy or the
    placeholder comment is absent (upstream template drift) — degrade
    silently rather than risk corrupting the freshly-scaffolded file."""
    if not first_prompt:
        return
    try:
        text = handoff_path.read_text(encoding="utf-8")
    except OSError:
        return
    if _ACCOMPLISHED_PLACEHOLDER not in text:
        return
    replacement = (
        "<!-- Session opened with this prompt: -->\n\n" + first_prompt.strip()
    )
    text = text.replace(_ACCOMPLISHED_PLACEHOLDER, replacement, 1)
    try:
        handoff_path.write_text(text, encoding="utf-8")
    except OSError:
        pass


@register_op("session_baton.promote")
def _handler(params: dict, repo_root: Optional[str] = None) -> dict:
    """JSON-RPC "session_baton.promote" handler — see module docstring for
    the full contract.

    Params:
        session_id (str, required) — the session whose baton is being
                    promoted.
        title       (str, optional) — handoff frontmatter title; falls back
                    to the baton's own ``title`` field, then a generic
                    default (``"Session <sid>"``).
        branch      (str, optional) — handoff frontmatter branch;
                    auto-detected by coordinator-doc-new when omitted.
        cwd         (str, optional) — working directory to resolve the
                    session hub AND the coordinator-doc-new subprocess's own
                    cwd from; threaded verbatim into store's ``cwd`` kwarg.
                    Defaults to the current process cwd when omitted.

    Returns:
        exit_code        int        0=ok, 1=setup-error (bad params /
                                     unresolvable session hub / scaffold
                                     failure)
        error             str|None
        session_id        str|None
        handoff_path      str|None  the promoted artifact's path, exactly as
                                     printed by coordinator-doc-new
        already_promoted  bool      True iff this call found an existing
                                     ``promoted_to`` and scaffolded nothing
    """
    session_id = params.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return _err("session_baton.promote requires a non-empty session_id")
    session_id = session_id.strip()

    title = params.get("title")
    if title is not None and not isinstance(title, str):
        return _err("session_baton.promote: title must be a string when supplied")

    branch = params.get("branch")
    if branch is not None and not isinstance(branch, str):
        return _err("session_baton.promote: branch must be a string when supplied")

    cwd = params.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        return _err("session_baton.promote: cwd must be a string when supplied")

    record = store.read_baton(session_id, cwd)
    existing = record.get("promoted_to")
    if existing:
        return {
            "exit_code": 0,
            "error": None,
            "session_id": session_id,
            "handoff_path": existing,
            "already_promoted": True,
        }

    resolved_title = title or record.get("title") or f"Session {session_id}"
    resolved_cwd = cwd if cwd is not None else "."

    try:
        handoff_path_str = _scaffold_via_doc_new(
            resolved_title, branch, resolved_cwd
        )
    except Exception as exc:  # noqa: BLE001 -- surfaced to the caller, never raised
        return _err(f"session_baton.promote: scaffold failed: {exc}")

    if not handoff_path_str:
        return _err(
            "session_baton.promote: coordinator-doc-new printed no path to stdout"
        )

    handoff_path = Path(handoff_path_str)
    if not handoff_path.is_absolute():
        handoff_path = Path(resolved_cwd) / handoff_path_str

    _write_first_prompt_into_body(handoff_path, record.get("first_prompt"))

    merged = store.merge_baton(session_id, cwd, promoted_to=handoff_path_str)
    if merged is None:
        # Baton merge failed (e.g. unresolvable session hub) -- the handoff
        # artifact itself is still real and already on disk; report success
        # with the caveat rather than pretending the scaffold never happened.
        return {
            "exit_code": 0,
            "error": None,
            "session_id": session_id,
            "handoff_path": handoff_path_str,
            "already_promoted": False,
            "baton_merge_failed": True,
        }

    return {
        "exit_code": 0,
        "error": None,
        "session_id": session_id,
        "handoff_path": handoff_path_str,
        "already_promoted": False,
    }
