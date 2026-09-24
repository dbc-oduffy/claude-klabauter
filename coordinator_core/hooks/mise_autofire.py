"""coordinator_core.hooks.mise_autofire — auto-fire for the wide run's run-id
minting, the mise-side twin of `pickup_autofire.py`.

Port of: DoE-claude `coordinator/hooks/scripts/mise-autofire.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration.

Fires on either entry path a mise-en-place-shaped invocation reaches this
hook by: a typed `UserPromptExpansion` (`/mise-en-place`) or a model-invoked
`PreToolUse` call on the `Skill` tool naming the same verb (`_MISE_COMMAND_
NAMES` — `mise-en-place` / `warp-speed-execute`) — adapted to one shape via
`coordinator_core.hooks.support.skill_invocation.read_invocation`, the
package-relative equivalent of DoE's sibling-script `_skill_invocation`
import (already ported W4-C4).

Mints a fresh Phase-0 run id via the engine plane's `backlog-grind-assemble
mint-run-id mise-en-place` CLI, immediately briefs that same id back through
`backlog-grind-assemble brief mise-en-place --run-id <minted>`, and returns
both the minted id (plus the inventory path it implies, and the brief's
decision object) as `additionalContext`. Both calls are READ-ONLY — this
hook has NO mutating half and must never grow one.

Co-fires with `pickup_autofire.py` by design and without conflict — see that
module's docstring for the shared `_MISE_COMMAND_NAMES`/`_BATON_GRAB_
COMMAND_NAMES` overlap rationale (unchanged from the DoE source, reused
verbatim, not re-derived here).

Safety envelope, each clause load-bearing (unchanged from DoE source):
  (a) NEVER fires a mutating call — `mint-run-id` and `brief` are both
      read-only; that is the whole surface this hook touches.
  (b) Every subprocess call and JSON decode fails open; `_handler` never
      raises. A failed mint means the EM mints by hand.
  (c) `additionalContext` never exceeds `_CONTEXT_BUDGET_CHARS` (10,000).
  (d) `async: false` semantics — the minted id and briefed decision must
      land in the SAME turn the prompt is being expanded into.

Negative-spec: this module still shells out to `backlog-grind-assemble`
(a real cross-plane subprocess call, not a zero-spawn read) — the
measure-first rule this chunk's body names applies to `pickup_autofire.py`'s
1353 lines specifically, not to this smaller, already-narrow surface; no
new spawn was introduced by this port, the two subprocess calls are
unchanged from the DoE source.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import context_only, no_advisory, payload_of
from coordinator_core.hooks.support.forwarder_resolve import forwarder_argv, resolve_forwarder
from coordinator_core.hooks.support.skill_invocation import read_invocation
from coordinator_core.ipc import register_op

_MISE_COMMAND_NAMES = frozenset({"mise-en-place", "warp-speed-execute"})
_CADENCE = "mise-en-place"
_CONTEXT_BUDGET_CHARS = 10_000
_MINT_TIMEOUT_SECONDS = 12
_BRIEF_TIMEOUT_SECONDS = 12

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def resolve_settings_home() -> Path:
    """Resolve the coordinator-claude settings-home root. Precedence: an
    explicit `COORDINATOR_SETTINGS_HOME` override, then `CLAUDE_HOME` (or
    `HOME`) joined with `.coordinator-claude-settings` — unchanged from the
    DoE source's own copy."""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_HOME") or str(Path.home())
    return Path(base) / ".coordinator-claude-settings"


def resolve_backlog_grind_assemble_bin(settings_home: Path) -> Optional[Path]:
    """Resolve the installed `backlog-grind-assemble` forwarder under
    `settings_home`. Returns None when unresolvable — the caller treats that
    as a transport failure and fails open."""
    return resolve_forwarder(settings_home / "bin", "backlog-grind-assemble")


class _TransportFailure(Exception):
    """Raised internally when a `backlog-grind-assemble` invocation could
    not be completed at all (binary unresolvable, spawn failure, or
    timeout)."""


def _run_backlog_grind_assemble(
    script_path: Path, tail: list, timeout: float
) -> subprocess.CompletedProcess:
    try:
        argv = forwarder_argv(script_path, tail)
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _TransportFailure(str(exc)) from exc


def decode_mint_payload(stdout: str) -> Optional[dict]:
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    run_id = obj.get("run_id")
    inventory_path = obj.get("inventory_path")
    if not (isinstance(run_id, str) and run_id):
        return None
    if not (isinstance(inventory_path, str) and inventory_path):
        return None
    return {"run_id": run_id, "inventory_path": inventory_path}


def decode_brief_payload(stdout: str) -> Optional[dict]:
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def render_additional_context(run_id: str, inventory_path: str, brief: dict) -> str:
    lines = [
        f"Mise-en-place run id (engine-minted, not EM-guessed): {run_id}",
        f"Inventory path this id implies (does not exist yet): {inventory_path}",
    ]

    narration = brief.get("narration")
    if isinstance(narration, str) and narration:
        lines.append(narration)

    next_move = brief.get("next_move")
    if isinstance(next_move, str) and next_move:
        lines.append(f"Next move: {next_move}")

    try:
        lines.append("Brief decision object:\n" + json.dumps(brief, indent=2, sort_keys=True))
    except (TypeError, ValueError):
        pass  # unserializable brief; the narration lines above still render

    rendered = "\n\n".join(lines)
    return rendered[:_CONTEXT_BUDGET_CHARS]


def compute_context(payload: dict) -> Optional[str]:
    """Compute the bare `additionalContext` prose for a single invocation,
    or `None` when nothing should be emitted (a non-matching verb, or any
    fail-open step along the mint/brief chain)."""
    inv = read_invocation(payload if isinstance(payload, dict) else {})
    if inv is None:
        return None
    if inv.command_name not in _MISE_COMMAND_NAMES:
        return None

    settings_home = resolve_settings_home()
    script_path = resolve_backlog_grind_assemble_bin(settings_home)
    if script_path is None:
        return None

    try:
        mint_result = _run_backlog_grind_assemble(
            script_path, ["mint-run-id", _CADENCE], _MINT_TIMEOUT_SECONDS
        )
    except _TransportFailure:
        return None

    if mint_result.returncode != 0:
        return None

    minted = decode_mint_payload(mint_result.stdout)
    if minted is None:
        return None

    run_id = minted["run_id"]
    inventory_path = minted["inventory_path"]

    try:
        brief_result = _run_backlog_grind_assemble(
            script_path,
            ["brief", _CADENCE, "--run-id", run_id],
            _BRIEF_TIMEOUT_SECONDS,
        )
    except _TransportFailure:
        return None

    if brief_result.returncode != 0:
        return None

    brief = decode_brief_payload(brief_result.stdout)
    if brief is None:
        return None

    additional_context = render_additional_context(run_id, inventory_path, brief)
    if not additional_context:
        return None

    return additional_context


@register_op("hooks.mise_autofire")
def _handler(params: dict, repo_root=None) -> dict:
    """IPC/dispatch_message adapter over `compute_context()`. `params` IS
    the raw UserPromptExpansion or PreToolUse(Skill) payload dict.

    Returns `context_only("UserPromptExpansion", ...)` when a mise-en-place
    verb was matched and a mint+brief pair could be computed;
    `no_advisory()` otherwise (silent pass — matches the DoE source's own
    "nothing otherwise" stdout contract).
    """
    params = payload_of(params)
    try:
        additional_context = compute_context(params)
    except Exception:
        # Defense-in-depth — must never raise; every internal step already
        # fails open on its own.
        additional_context = None

    if additional_context is None:
        return no_advisory()
    return context_only("UserPromptExpansion", additional_context)
