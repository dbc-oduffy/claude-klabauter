"""coordinator_core.hooks.sessionstart_async_dispatch — SessionStart async
fan-in op: composes every ASYNC (registered `async: true`) leg this row's
own `writes:` footprint carries.

Arrival note (W4-C10, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from coordinator-content-repo `coordinator/hooks/scripts/sessionstart-async-dispatch.py` —
folded originally for the identical reason its sync sibling
(`sessionstart_dispatch.py`) was: multiple `hooks.json` registrations sharing
one `python3` interpreter start. As with the sync dispatcher, the subprocess-
level fold (per-guard `importlib.util` import, byte-capture stdout/stderr
re-emission) has no analogue here — every composed leg is an already-
registered same-repo op, composed via direct handler import and CONCATENATE-
ALL aggregation. Async transport discards this op's returned context
regardless, but the aggregation is kept uniform with the sync dispatcher's
own shape (never a bare fire-and-forget loop) so a future transport change
that DOES surface async output needs no rewrite here.

COMPOSED HERE, in DoE's REGISTRY order, each paired with its pinned `sources`
set (all five of `startup`, `resume`, `clear`, `compact`, `fork`, except where
noted):
  - `session_start_register_content_root_root`
  - `session_start_repair_prepare_commit_msg_hook` (`startup` only)
  - `session_start_register_published_engine`
  - `sessionstart_ensure_http_forwarder`
  - `session_start_write_plugin_root_breadcrumb`
  - `session_start_ensure_precommit_hook` — calls
    `install_content_root_precommit_hook.main([<payload cwd>])` with stdout and
    stderr captured; the captured text surfaces only on a non-zero return code.

SOURCE-GATING lives here, because the caller is an http entry on the union
matcher. The handler reads `payload["source"]`: a missing source runs no leg; a
non-empty source in no leg's set runs no leg and returns the unmatched-source
breadcrumb as context; otherwise each leg runs only when the source is in its
own set.

AGGREGATION CONTRACT: CONCATENATE-ALL, identical shape to the sync
dispatcher. Every leg is fully side-effect-driven (registry writes, a
forwarder ensure, a breadcrumb write) — none is expected to return
meaningful `additionalContext` under normal operation (async transport
discards it anyway), but a leg that DOES return context (e.g. the http-
forwarder-ensure disclosure path) is still folded in, never dropped.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C10
"""

from __future__ import annotations

import asyncio
import contextlib
import io
from typing import Callable, Optional

from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.session_start_register_content_root_root import (
    _handler as _session_start_register_content_root_root_handler,
)
from coordinator_core.hooks.session_start_register_published_engine import (
    _handler as _session_start_register_published_engine_handler,
)
from coordinator_core.hooks.session_start_repair_prepare_commit_msg_hook import (
    _handler as _session_start_repair_prepare_commit_msg_hook_handler,
)
from coordinator_core.hooks.session_start_write_plugin_root_breadcrumb import (
    _handler as _session_start_write_plugin_root_breadcrumb_handler,
)
from coordinator_core.hooks.sessionstart_ensure_http_forwarder import (
    _handler as _sessionstart_ensure_http_forwarder_handler,
)
from coordinator_core.ipc import register_op
from coordinator_core.ops import install_content_root_precommit_hook as _install_precommit


def _extract_context(result) -> "Optional[str]":
    if not isinstance(result, dict):
        return None
    hso = result.get("hookSpecificOutput")
    if not isinstance(hso, dict):
        return None
    text = hso.get("additionalContext")
    return text if isinstance(text, str) and text else None


_ALL_SOURCES = frozenset({"startup", "resume", "clear", "compact", "fork"})
_STARTUP_ONLY = frozenset({"startup"})


def _ensure_precommit_hook(leg_params: dict) -> Optional[dict]:
    cwd = payload_of(leg_params).get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = _install_precommit.main([cwd])
    if rc == 0:
        return None
    text = (out.getvalue() + err.getvalue()).strip()
    return context_only("SessionStart", text) if text else None


# Legs resolve through module globals at call time so tests can patch them.
_LEGS: "tuple[tuple[str, frozenset, Callable[[dict], object]], ...]" = (
    ("session_start_register_content_root_root", _ALL_SOURCES,
     lambda p: _session_start_register_content_root_root_handler(p)),
    ("session_start_repair_prepare_commit_msg_hook", _STARTUP_ONLY,
     lambda p: _session_start_repair_prepare_commit_msg_hook_handler(p)),
    ("session_start_register_published_engine", _ALL_SOURCES,
     lambda p: _session_start_register_published_engine_handler(p)),
    ("sessionstart_ensure_http_forwarder", _ALL_SOURCES,
     lambda p: _sessionstart_ensure_http_forwarder_handler(p)),
    ("session_start_write_plugin_root_breadcrumb", _ALL_SOURCES,
     lambda p: _session_start_write_plugin_root_breadcrumb_handler(p)),
    ("session_start_ensure_precommit_hook", _ALL_SOURCES,
     lambda p: _ensure_precommit_hook(p)),
)


def _unmatched_source_breadcrumb(source: object) -> str:
    return (
        f"[sessionstart-async-dispatch] source={source!r} matches no guard in "
        "REGISTRY -- every guard skipped for this boot. If the harness added a "
        "source value, add it to the per-guard `sources` sets, not just the "
        "hooks.json matcher."
    )


@register_op("hooks.sessionstart_async_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    payload = dict(payload_of(params))
    leg_params = {"payload": payload}

    source = payload.get("source")
    if not source:
        return no_advisory()
    if not isinstance(source, str) or not any(source in s for _, s, _ in _LEGS):
        return context_only("SessionStart", _unmatched_source_breadcrumb(source))

    texts: "list[str]" = []
    for _key, sources, leg_call in _LEGS:
        if source not in sources:
            continue
        try:
            result = await asyncio.to_thread(leg_call, leg_params)
        except Exception:
            continue
        text = _extract_context(result)
        if text:
            texts.append(text)

    if not texts:
        return no_advisory()
    return context_only("SessionStart", "\n".join(texts))
