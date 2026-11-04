"""Pins the host-neutral hook contract against the coordinator-content-repo research at 856b1d1f46.

Falsifier, read before writing contract.py. Quoted lines:

agy -- docs/research/2026-11-04-peer-hook-surfaces/agy.md
  session field: "`conversationId` (string): Unique UUID of the conversation session"
  tool shape:    "`toolCall` (object): `name` ... `args` ... (for shell commands: `{"CommandLine": "..."}`"
  cwd source:    "`workspacePaths` (array of strings): Absolute paths of active workspaces"
  deny shape:    "The hook outputs `{"decision": "deny", "reason": "<explanation>"}`."
  ask:           "`\"ask\"`: Prompt user for confirmation (respects \"Always Allow\" cache)."
  no verdict:    "outputs invalid JSON (or an empty JSON object `{}` omitting the required `decision`
                  field), the tool call is denied" -- so agy's neutral answer is `ask`, never `{}`.

Codex -- docs/research/2026-11-04-doe-doctrine-codex/install-mechanics.md § Hook map
  deny shape:    "emit `permissionDecision: \"deny\"` with a reason"
  ask:           "`ask` and several older fields are parsed without providing the expected effect."
  no verdict:    "A callback failure or malformed response can continue the operation" -- `{}` continues.
  tool shape:    "Shell and patch inputs use `tool_input.command`"

GAP, reported to the EM: install-mechanics.md does not name Codex's session field or print `{}` as
the no-verdict response. `session_id` is the plan's assertion (and the Claude-compatible payload
key); nothing in the cited sources contradicts it, so it is built as specified.
"""
from __future__ import annotations

import dataclasses

import pytest

from coordinator_core.host_hooks.contract import (
    Adapter,
    HostEvent,
    neutral_fallback,
)


def test_host_event_is_frozen_with_payload_session():
    ev = HostEvent("codex", "PreToolUse", "s-1", "Bash", {"command": "ls"}, "/w")
    assert ev.session_id == "s-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        ev.session_id = "other"  # type: ignore[misc]


def test_neutral_fallback_codex_is_empty_object():
    assert neutral_fallback("codex") == {}


def test_neutral_fallback_agy_asks_and_is_never_empty():
    fb = neutral_fallback("agy")
    assert fb["decision"] == "ask"
    assert fb != {}


def test_neutral_fallback_returns_fresh_dict():
    a = neutral_fallback("codex")
    a["x"] = 1
    assert neutral_fallback("codex") == {}


def test_neutral_fallback_unknown_host_refuses():
    with pytest.raises(ValueError):
        neutral_fallback("claude")


def test_adapter_protocol_accepts_conforming_class_and_rejects_other():
    class Good:
        def adapt_event(self, payload):
            return HostEvent("codex", "PreToolUse", payload["session_id"])

        def render(self, host_event, verdict):
            return {}

    class Bad:
        def adapt_event(self, payload):
            return None

    assert isinstance(Good(), Adapter)
    assert not isinstance(Bad(), Adapter)
    assert Good().adapt_event({"session_id": "s"}).session_id == "s"
