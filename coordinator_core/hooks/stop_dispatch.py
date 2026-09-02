"""
coordinator_core.hooks.stop_dispatch — Stop-event fan-in, eight legs (not six).

Purpose: warm-engine composition for DoE-claude's `stop-dispatch.py`, the
single `Stop` hooks.json registration that currently pays one interpreter
start (measured 53.1ms of process time, see the plan's own § Problem) to run
its fan-in of folded scripts every Stop, fleet-wide. This module is the
engine-side op that registration can eventually point at (per this plan's
own exit criterion — the `type`/`url` edit itself is DoE's, not ours).

THE COUNT IS EIGHT, NOT SIX. `stop-dispatch.py`'s own `REGISTRY` (read at
DoE-claude HEAD `3331187b9cd5b806942e6dba290e5985c7dbfc4c`, unchanged at
current HEAD `7b9b78f4b211023e34a9d53f2feaacd45ed98154` — same 478 lines)
carries eight `StopGuard` entries, not the six the classification table's
prose names — the "six" there is inherited docstring prose this plan's own
Anti-scope forbids re-deriving from. Disposition per leg:

  1. `runtime_tripwire_em_check` — CONSUMED, not re-derived. C6 already
     built `hooks.runtime_tripwire_em_check` (`coordinator_core/hooks/
     runtime_tripwire_em_check.py`) for the PostToolUse(Agent) registration.
     Its handler body is NOT event-gated for the three legs it ported
     (subagent-detect, push-failure check, hooks.json-staleness check) — see
     that module's own "COVERAGE ESTABLISHED FIRST" docstring section — so
     the exact same handler is correct for the Stop leg too, matching
     `stop-dispatch.py`'s own `_pre_em_check` reusing the identical script.
     Called here via `_handler({"payload": payload})`, mirroring the
     in-process composition pattern that module's own docstring already
     uses for `git.push_failure_verdict`.

  2. `watchdog_undischarged_next_move` — CONSUMED, not re-derived. C4's
     `hooks.watchdog_undischarged_next_move` already dispatches on payload
     shape: a truthy `tool_name` routes to the PostToolUse(Skill|Agent) leg,
     a payload carrying `transcript_path` (a Stop payload's own shape, no
     `tool_name`) routes to `_handle_stop` — the Stop leg is already built.
     Called here via the same `_handler({"payload": payload})` shape.

  3. `guard_manufactured_blocker` — NOT PORTED. BLOCKING-class per
     `DR-warm-hook-miss-policy` ("Blocking hooks refuse on a miss. Settled
     prior behavior, unchanged by this decision"). `coordinator_core/warm/
     hook_http.py::BLOCKING_EVENTS` is `frozenset({"PreToolUse"})` only —
     `Stop` is NOT a member, so an `http`-flipped Stop registration gets NO
     `unreachable_response` fail-closed treatment on a miss; it fails OPEN
     silently against a dead engine, which is exactly the semantic a
     blocking guard must never have. Widening `BLOCKING_EVENTS` past
     PreToolUse is out of this plan's scope (§ Out of scope) — the answer
     this leg needed ("can it survive today's transport?") is "no", so it
     stays a `command` hook and no engine op is built for it here. A
     `hooks.guard_manufactured_blocker` op existing would not change this
     registration's transport safety, so building one is deferred rather
     than performed as unused residue.

  4. `guard_kira_verdict_routed` — PORTED (residue; no existing op or
     library module covered this script before this chunk). Full verbatim
     port of `guard-kira-verdict-routed.py`'s frontmatter-only decision
     logic below (`_guard_kira_verdict_routed` + helpers). Registered as its
     own op, `hooks.guard_kira_verdict_routed`, for independent testability,
     and composed into the fan-in below.

  5. `stop_em_report_altitude` — COMPOSED via a thin `@register_op` wrapper.
     `coordinator_core/hooks/em_report_altitude.py` already carries the
     full detector logic behind a bare `op(payload) -> dict | None`
     function; its own docstring states plainly it carries no `@register_op`
     handler because Stop events are not routed through the IPC daemon path
     for its ORIGINAL (DoE stdin/stderr shim) transport. That module is
     outside this chunk's `writes:` scope, so the registration wrapper lives
     HERE instead of inside it — `_stop_em_report_altitude_handler` below
     imports and calls `em_report_altitude.op` unchanged.

  6. `nudge_harness_directive_dispatch` — same shape as (5): library `op()`
     exists, no handler, module out of this chunk's `writes:` scope. Wrapped
     here as `hooks.nudge_harness_directive_dispatch`.

  7. `nudge_unrouted_sizing` — same shape as (5)/(6). Wrapped here as
     `hooks.nudge_unrouted_sizing`.

  8. `receiver_state_sensor` — CONSUMED. Already a registered op
     (`hooks.receiver_state_sensor`, `coordinator_core/hooks/
     receiver_state_sensor.py`). The source script's own comment names it a
     PRODUCER: always exits 0 with empty stdout, so it cannot change this
     fan-in's aggregate verdict — composed for its write side-effect only,
     its return value is not folded into the aggregate.

AGGREGATION CONTRACT: mirrors `stop-dispatch.py`'s own CONCATENATE-ALL
(never first-fires-wins) — every composable leg above (all but #3, #8) runs
regardless of whether an earlier leg already produced a block/advisory; one
leg raising is isolated to that leg alone (fail-open for it specifically,
matching the source script's own per-guard `try/except BaseException`).
Return-shape normalisation (`_extract_advisory`) reads every leg's own
`hooks._envelope`-shaped or flat `{"message": str}`-shaped return uniformly:
a `deny()` shape (`permissionDecision: "deny"`) is a BLOCK; anything else
carrying text is an ADVISORY; `no_advisory()`/`None`/no text is silent. If
any leg blocks, the aggregate is `deny("Stop", <joined block reasons>)`;
otherwise, if any leg has advisory text, the aggregate is
`post_advisory(<joined advisory text>)`; otherwise `no_advisory()`. Transport
mapping (this aggregate dict back onto stderr+exit2 / stdout+exit0 for a
DoE-side Stop hook) is explicitly not this chunk's job — see the plan's own
exit criterion ("a one-line type/url edit in a repo we do not own").

Every input comes from `params["payload"]` — never `os.environ` or this
process's own `cwd`/session, matching every other payload-cwd-resolving
`hooks.*` op in this family.

Spec: docs/plans/2026-08-31-six-hook-scripts-become-engine-ops.md, chunk C3
Dispatch brief: state/dispatch-briefs/2026-08-31-six-hook-scripts-become-engine-ops/C3.md
DoE source: coordinator/hooks/scripts/stop-dispatch.py,
    coordinator/hooks/scripts/guard-kira-verdict-routed.py
"""
from __future__ import annotations

import os
from typing import Mapping, Optional

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.repo_root import show_toplevel
from coordinator_core.hooks._envelope import deny, no_advisory, post_advisory
from coordinator_core.hooks.em_report_altitude import op as _em_report_altitude_op
from coordinator_core.hooks.nudge_harness_directive_dispatch import (
    op as _nudge_harness_directive_dispatch_op,
)
from coordinator_core.hooks.nudge_unrouted_sizing import op as _nudge_unrouted_sizing_op
from coordinator_core.hooks.receiver_state_sensor import _handler as _receiver_state_sensor_handler
from coordinator_core.hooks.runtime_tripwire_em_check import _handler as _runtime_tripwire_em_check_handler
from coordinator_core.hooks.watchdog_undischarged_next_move import _handler as _watchdog_undischarged_next_move_handler
from coordinator_core.ipc import register_op
from coordinator_core.session.machinery_paths import share_dir as _share_dir

# ---------------------------------------------------------------------------
# guard-kira-verdict-routed.py — verbatim port of its frontmatter-only
# decision logic (no YAML dependency, column-zero-only line-scan). See the
# source script's own module docstring (DoE-claude) for the full worked
# rationale behind each helper below; kept in lockstep with that file's
# helper names so a future diff against the source is mechanical.
# ---------------------------------------------------------------------------

# The commit/date C1's terminal-stamp contract lands at — see the source
# script's own CONTRACT_EPOCH section. Delete this constant and
# `_postdates_epoch` once no session predating 2026-08-30 can still close.
_KIRA_CONTRACT_EPOCH_ISO = "2026-08-30T00:00:00Z"

# Pinned against a REAL provisioned sidecar's stamped `agent_type` (see
# source script docstring) — every real sidecar on disk carries the
# `coordinator:` namespace prefix; the bare form is compared too via
# `_kira_normalize_agent_type`.
_KIRA_AGENT_TYPE = "overengineering-reviewer"


# Review: overengineering-reviewer (Kira) — this hand-rolled an upward
# `.git`-existence walk while the module already imports `show_toplevel`
# (used ~380 lines below for the receiver_state_sensor leg) and the sibling
# C4 module wraps the same helper as `_repo_root_for` for this exact job;
# delegated to it, matching `watchdog_undischarged_next_move._repo_root_for`.
def _kira_repo_root(payload: dict) -> "Optional[str]":
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return None
    return show_toplevel(cwd)


def _kira_read_frontmatter(path: str) -> dict:
    """Flat, stdlib-only top-level `key: value` line-scan of the YAML
    frontmatter block — verbatim port of the source script's own
    `_read_frontmatter`. Only COLUMN-ZERO keys are read; returns `{}` on any
    read/shape failure — a guard that cannot prove a fact must never block
    on it."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}

    body = lines[1:end]
    meta: dict = {}
    i = 0
    while i < len(body):
        line = body[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if line[0].isspace():
            # Indented — belongs to a nested block (e.g. `divergence:`'s own
            # sub-keys), never a top-level fact. Skip it.
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if "#" in rest:
            rest = rest.split("#", 1)[0].strip()
        if rest.startswith("[") and rest.endswith("]"):
            inner = rest[1:-1]
            meta[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
            i += 1
            continue
        if rest in ("", "{}"):
            # Possible block-list continuation (`key:` then `  - item` lines).
            items: list = []
            j = i + 1
            while j < len(body):
                candidate = body[j]
                if not candidate.strip() or candidate.lstrip().startswith("#"):
                    j += 1
                    continue
                if not candidate.lstrip().startswith("- "):
                    break
                items.append(candidate.strip()[2:].strip().strip("'\""))
                j += 1
            if items:
                meta[key] = items
                i = j
                continue
            meta[key] = rest
            i += 1
            continue
        meta[key] = rest.strip("'\"")
        i += 1
    return meta


def _kira_postdates_epoch(meta: dict) -> bool:
    spawned = meta.get("spawned_at")
    if not isinstance(spawned, str) or not spawned:
        return True
    return spawned >= _KIRA_CONTRACT_EPOCH_ISO


def _kira_to_int(value) -> "Optional[int]":
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _kira_stem(filename: str) -> str:
    return filename[:-3] if filename.endswith(".md") else filename


def _kira_normalize_agent_type(agent_type) -> "Optional[str]":
    if not isinstance(agent_type, str) or not agent_type:
        return None
    if ":" in agent_type:
        return agent_type.split(":", 1)[1]
    return agent_type


def _kira_is_kira(filename: str, meta: dict) -> bool:
    return _kira_normalize_agent_type(meta.get("agent_type")) == _KIRA_AGENT_TYPE


def _kira_is_review_activity(filename: str, meta: dict) -> bool:
    if "findings_count" in meta:
        return True
    kind = meta.get("kind")
    if kind in ("review-findings", "staff-eng-review"):
        return True
    agent_type = meta.get("agent_type", "")
    if isinstance(agent_type, str) and "review" in agent_type.lower():
        return True
    return "review" in filename.lower()


def _kira_block_condition_1(in_scope: list) -> bool:
    kira_present = any(_kira_is_kira(f, m) for f, m in in_scope)
    if kira_present:
        return False
    return any(
        _kira_is_review_activity(f, m) and not _kira_is_kira(f, m) for f, m in in_scope
    )


def _kira_find_answers(kira_filename: str, in_scope: list) -> list:
    stem = _kira_stem(kira_filename)
    answers: list = []
    for f, m in in_scope:
        if f == kira_filename:
            continue
        integrated = m.get("integrated_from")
        if isinstance(integrated, str):
            integrated = [integrated] if integrated.strip() else []
        if not isinstance(integrated, list):
            continue
        if stem not in integrated and kira_filename not in integrated:
            continue
        answers.append(f)
    return answers


def _kira_unstamped_integrators(in_scope: list) -> list:
    return [
        f
        for f, m in in_scope
        if "integrator_receipt" in m and not m.get("integrated_from")
    ]


_KIRA_BLOCK_HEADER = (
    "[guard] This close carries an unrouted Kira (overengineering-reviewer) "
    "verdict.\n"
)


def _guard_kira_verdict_routed(payload: dict) -> dict:
    """Stop guard: hard-stop a close whose Kira (overengineering-reviewer)
    verdict was never routed anywhere. Verbatim decision port of
    `guard-kira-verdict-routed.py::main()` — see that script's module
    docstring for the full THE PROBLEM / TRIGGER SCOPE / THE DECISION
    write-up this function implements without re-deriving it.

    Returns `deny("Stop", <reasons>)` when it fires, `post_advisory(<text>)`
    on a fail-OPEN could-not-evaluate path (mirrors the source script's own
    stdout breadcrumb, never blocking on its own inability to read a fact),
    `no_advisory()` otherwise.
    """
    if not isinstance(payload, dict):
        return no_advisory()

    # Trigger scope, verbatim: a subagent's own Stop (Kira's included) and a
    # re-entrant Stop replay must never see this guard evaluate at all.
    if payload.get("agent_id"):
        return no_advisory()
    if payload.get("stop_hook_active"):
        return no_advisory()

    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return post_advisory(
            "[guard] guard-kira-verdict-routed could not evaluate: "
            "no session_id in the Stop payload"
        )

    repo_root = _kira_repo_root(payload)
    if repo_root is None:
        return post_advisory(
            "[guard] guard-kira-verdict-routed could not evaluate: "
            "could not resolve repo root from cwd"
        )

    share_dir = _share_dir(repo_root, session_id)
    try:
        filenames = [
            f
            for f in os.listdir(share_dir)
            if f.endswith(".md") and not f.endswith(".blocks.md")
        ]
    except OSError:
        return post_advisory(
            f"[guard] guard-kira-verdict-routed could not evaluate: "
            f"could not list share dir {share_dir}"
        )

    entries = []
    for fname in filenames:
        meta = _kira_read_frontmatter(os.path.join(share_dir, fname))
        entries.append((fname, meta))

    if not entries:
        return no_advisory()

    in_scope = [(f, m) for f, m in entries if _kira_postdates_epoch(m)]
    if not in_scope:
        return no_advisory()

    reasons: list = []

    if _kira_block_condition_1(in_scope):
        reasons.append(
            "- Other review activity ran this session, but no Kira "
            "(overengineering-reviewer) sidecar is present. Kira fires on "
            "every close (SKILL.md); dispatch her before closing."
        )

    kira_entries = [(f, m) for f, m in in_scope if _kira_is_kira(f, m)]
    for kira_file, kira_meta in kira_entries:
        findings_count = _kira_to_int(kira_meta.get("findings_count"))
        answers = _kira_find_answers(kira_file, in_scope)

        if findings_count is not None and findings_count > 0 and not answers:
            ran_but_unstamped = _kira_unstamped_integrators(in_scope)
            if ran_but_unstamped:
                named = ", ".join(sorted(ran_but_unstamped))
                reasons.append(
                    f"- {kira_file} stamps findings_count={findings_count} with no "
                    f"sibling sidecar's integrated_from naming it. An integrator "
                    f"was dispatched ({named} carries an integrator_receipt, "
                    f"which the engine splices AT SPAWN) — so it is either "
                    f"still in flight or finished having skipped only the "
                    f"stamp — do NOT re-dispatch it. If it is still running, "
                    f"wait; hand-stamping now would attest dispositions that "
                    f"do not exist yet. Once it has finished, add a top-level "
                    f"`integrated_from: [{_kira_stem(kira_file)}]` to that "
                    f"sidecar's frontmatter at column zero, verify its "
                    f"dispositions are the ones you actually landed, and re-close."
                )
            else:
                reasons.append(
                    f"- {kira_file} stamps findings_count={findings_count} with no "
                    f"sibling sidecar's integrated_from naming it. Owed route: "
                    f"review-integrator, or a refactor executor if the verdict "
                    f"recommended a rebuild."
                )

    if not reasons:
        return no_advisory()

    return deny("Stop", _KIRA_BLOCK_HEADER + "\n".join(reasons))


# Review: overengineering-reviewer (Kira) — no registration, dispatch site,
# or cross-module caller found for this op key anywhere in claude-klabauter or
# DoE-claude (DoE's guard-kira-verdict-routed.py shim, if it exists, would
# import this module's function directly, not the IPC registry); the
# fan-in below calls `_guard_kira_verdict_routed_handler` as a plain
# Python object. @register_op removed; the function itself is unchanged.
def _guard_kira_verdict_routed_handler(params: dict, repo_root=None) -> dict:
    """Registered wrapper for `_guard_kira_verdict_routed` — scope "none",
    `repo_root` handler arg unused (this guard resolves its own repo root
    from `payload["cwd"]`, matching every other payload-cwd-resolving
    `hooks.*` op in this family). Fail-open: any exception degrades to
    `no_advisory()`, never propagated."""
    try:
        payload = params.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        return _guard_kira_verdict_routed(dict(payload))
    except Exception:
        return no_advisory()


# ---------------------------------------------------------------------------
# Thin @register_op wrappers for the three library modules whose own `op()`
# carries no handler (out of this chunk's `writes:` scope — see module
# docstring items 5-7).
# ---------------------------------------------------------------------------


def _wrap_flat_op(op_fn) -> dict:
    """Call a flat `op(payload) -> dict | None` function and normalise its
    return to a hooks._envelope-shaped dict for uniform aggregation below.
    These three library ops are NON-BLOCKING BY CONSTRUCTION (see each
    module's own docstring) — their `{"message": str}` return is always an
    advisory, never a block."""
    def _handler(params: dict, repo_root=None) -> dict:
        try:
            payload = params.get("payload")
            if not isinstance(payload, Mapping):
                payload = {}
            result = op_fn(dict(payload))
        except Exception:
            return no_advisory()
        if not isinstance(result, dict):
            return no_advisory()
        message = result.get("message")
        if not isinstance(message, str) or not message:
            return no_advisory()
        return post_advisory(message)
    return _handler


# Review: overengineering-reviewer (Kira) — these three keys had no
# registration, dispatch site, or cross-module caller (grepped across
# claude-klabauter and DoE-claude); DoE's own hook shims import
# `coordinator_core.hooks.<module>.op` directly and never go through
# `_REGISTRY`. @register_op removed from all three; the plain functions
# the fan-in below actually calls are unchanged.
_stop_em_report_altitude_handler = _wrap_flat_op(_em_report_altitude_op)
_nudge_harness_directive_dispatch_handler = _wrap_flat_op(
    _nudge_harness_directive_dispatch_op
)
_nudge_unrouted_sizing_handler = _wrap_flat_op(_nudge_unrouted_sizing_op)


# ---------------------------------------------------------------------------
# Aggregation: normalise every leg's own return shape uniformly, then
# CONCATENATE-ALL per the source dispatcher's own contract (see module
# docstring "AGGREGATION CONTRACT").
# ---------------------------------------------------------------------------


def _extract_advisory(result) -> "tuple[bool, Optional[str]]":
    """Return (is_block, text) from any hooks.* envelope-shaped or flat
    {"message": str}-shaped return used by this fan-in's composed legs.

    A `deny()` shape (`permissionDecision: "deny"`) is a BLOCK; any other
    shape carrying non-empty text (`additionalContext`, or a flat
    `message`) is an ADVISORY; `no_advisory()` / `None` / no text is silent.
    """
    if not isinstance(result, dict):
        return False, None
    hso = result.get("hookSpecificOutput")
    if isinstance(hso, dict):
        if hso.get("permissionDecision") == "deny":
            reason = hso.get("permissionDecisionReason")
            return True, reason if isinstance(reason, str) and reason else None
        text = hso.get("additionalContext")
        return False, text if isinstance(text, str) and text else None
    message = result.get("message")
    if isinstance(message, str) and message:
        return False, message
    return False, None


@register_op("hooks.stop_dispatch")
async def _handler(params: dict, repo_root=None) -> dict:
    """Stop fan-in: compose the seven composable legs (all but the excluded
    BLOCKING-class `guard_manufactured_blocker`) and aggregate CONCATENATE-
    ALL, per module docstring.

    `repo_root` (the framework-supplied handler argument) is unused — every
    composed leg resolves its own repo root from `params["payload"]["cwd"]`,
    matching every other payload-cwd-resolving `hooks.*` op in this family.
    One leg raising is isolated to that leg alone (fail-open for it
    specifically); this handler itself never raises.
    """
    payload = params.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    payload = dict(payload)
    leg_params = {"payload": payload}

    block_reasons: list = []
    advisories: list = []

    for leg_call in (
        lambda: _runtime_tripwire_em_check_handler(leg_params),
        lambda: _watchdog_undischarged_next_move_handler(leg_params),
        lambda: _guard_kira_verdict_routed_handler(leg_params),
        lambda: _stop_em_report_altitude_handler(leg_params),
        lambda: _nudge_harness_directive_dispatch_handler(leg_params),
        lambda: _nudge_unrouted_sizing_handler(leg_params),
    ):
        try:
            result = leg_call()
        except Exception:
            continue
        is_block, text = _extract_advisory(result)
        if is_block:
            # Review: coordinator:code-reviewer — a block must survive the
            # fold even with a falsy reason; decoupling is_block from text
            # would let a deny("Stop", "") evaporate silently.
            block_reasons.append(text or "<no reason given>")
        elif text:
            advisories.append(text)

    # receiver_state_sensor — PRODUCER only (see module docstring item 8):
    # composed for its write side-effect; its return is never folded into
    # this aggregate's verdict. It is "common_dir"-scoped, so under normal
    # IPC dispatch its `repo_root` handler arg is `git_common_dir(request_
    # repo)` (ipc.py::resolve_op_repo_key) — resolved here explicitly from
    # the SAME payload["cwd"] every other leg above already reads, since
    # this in-process call bypasses that resolution. Never the ambient
    # process cwd: leaving `repo_root` at its own default here previously
    # wrote a `sess-1` entry into THIS repo's own `.git/coordinator-sessions/`
    # from a `tmp_path`-rooted test payload (caught by
    # coordinator_core/conftest.py's live-session-hub litter guard).
    try:
        cwd = payload.get("cwd")
        common_dir = None
        if isinstance(cwd, str) and cwd:
            git_root = show_toplevel(cwd)
            if git_root:
                common_dir = resolve_git_common_dir(git_root)
        sensor_params = {
            "session_id": payload.get("session_id") or "",
            "transcript_path": payload.get("transcript_path") or "",
            "delegation_evidence": "false",
        }
        await _receiver_state_sensor_handler(sensor_params, repo_root=common_dir)
    except Exception:
        pass

    if block_reasons:
        return deny("Stop", "\n\n".join(block_reasons))
    if advisories:
        return post_advisory("\n\n".join(advisories))
    return no_advisory()
