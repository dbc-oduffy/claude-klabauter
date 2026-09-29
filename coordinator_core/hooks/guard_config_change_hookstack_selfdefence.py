"""coordinator_core.hooks.guard_config_change_hookstack_selfdefence —
ConfigChange warm-engine op.

Port of: coordinator-content-repo coordinator/hooks/scripts/guard-config-change-hookstack-
selfdefence.py. Flags exactly two `source == "local_settings"` ConfigChange
shapes: an out-of-band edit to `.claude/settings.local.json`, and the sharper
case where that edit set `disableAllHooks: true`. FLAG, NEVER BLOCK — this op
always returns an advisory `additionalContext` envelope or a no-op; it never
denies (a live probe proved `decision: "block"` has no observable effect on
this event).

No module-level mutable state; the changed file is re-read fresh on every
call, and this hook does not sentinel/dedupe across fires (each qualifying
ConfigChange in a session gets its own flag).
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core._hook_envelope import no_advisory, payload_of
from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks._payload import field
from coordinator_core.hooks.support import message_envelope as _envelope
from coordinator_core.ipc import register_op

_WIKI_ANCHOR = "coordinator/docs/wiki/claude-code-extension-surface.md"

_TARGET_SOURCE = "local_settings"


def _compose_disable_all_hooks_offer() -> "_envelope.Message":
    prose = (
        "[ConfigChange self-defence] this session's own hookstack was just "
        "disabled (`disableAllHooks: true`) by a write outside the tool "
        "pipeline -- `decision: \"block\"` is not honored on this event "
        "(proven, not assumed), so this is a flag, not a revert. Verify the "
        "write was intentional before trusting any guard silence for the "
        "rest of this session."
    )
    return _envelope.compose(prose, anchor=_WIKI_ANCHOR)


def _compose_out_of_band_edit_offer() -> "_envelope.Message":
    prose = (
        "[ConfigChange self-defence] this session's local settings file "
        "(`.claude/settings.local.json`) was just edited by a process "
        "outside the tool pipeline. `decision: \"block\"` is not honored on "
        "this event (proven, not assumed), so this is a flag, not a revert "
        "-- review the change before trusting the rest of this session's "
        "hook behaviour."
    )
    return _envelope.compose(prose, anchor=_WIKI_ANCHOR)


def _file_carries_disable_all_hooks(file_path: object) -> bool:
    if not isinstance(file_path, str) or not file_path.strip():
        return False
    try:
        raw = Path(file_path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    return data.get("disableAllHooks") is True


@register_op("hooks.guard_config_change_hookstack_selfdefence")
def _handler(params: dict, repo_root=None) -> dict:
    params = payload_of(params)
    if field(params, "source") != _TARGET_SOURCE:
        return no_advisory()

    file_path = params.get("file_path")

    try:
        disabled = _file_carries_disable_all_hooks(file_path)
    except Exception:
        disabled = False

    try:
        message = (
            _compose_disable_all_hooks_offer()
            if disabled
            else _compose_out_of_band_edit_offer()
        )
        text = _envelope.render(message)
    except Exception:
        return no_advisory()

    return context_only("ConfigChange", text)
