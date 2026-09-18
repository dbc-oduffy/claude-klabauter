"""coordinator_core.hooks.pickup_autofire — UserPromptExpansion auto-fire
hook for baton grabs.

Port of: DoE-claude `coordinator/hooks/scripts/pickup-autofire.py`
(docs/plans/2026-09-18-doe-holds-no-scripts.md chunk W4-C12). Shape per the
W4-C1 verdict: command/native-door — no coordinator/bin shim, no http
registration. 1353 lines in the DoE source — this row's own body names a
measure-first rule for this file specifically: the per-fire process
time/spawn count (through `hook-run`, the path this row lands on) is what
decides whether this hook stays at this size, not a line-count judgment
made here. Nothing in this port ADDS a spawn or a code path beyond the DoE
source; the two shape changes below are both spawn REDUCTIONS.

Purpose (unchanged from DoE source): when the EM (or a peer session) types
`/pickup <artifact-path>`, this hook fires ahead of `UserPromptSubmit`,
computes the pickup decision object via the engine-side `pickup-assemble
brief` CLI, and injects a rendered summary as `additionalContext`. When the
computed decision reads `coast: clear` with zero unresolved
`judgment_points`, the hook ALSO fires the mutating `pickup-assemble apply`
half, propagating the event payload's own `session_id` explicitly.

The same firing covers an autonomous-run command handed batons on its
argument line (`_BATON_GRAB_COMMAND_NAMES` — `mise-en-place` /
`warp-speed-execute`, kept literally identical to `mise_autofire.py`'s own
set).

Shape changes, both forced by this row's own op contract and both spawn
REDUCTIONS, never a behaviour change:
  (a) stdin/stdout JSON I/O becomes the `params`-dict-in / envelope-dict-out
      contract; `compute_context` takes the already-parsed payload dict
      directly.
  (b) `_capture_producer`'s cross-plane `_engine_root.resolve_claude_klabauter_root()`
      + `sys.path` insertion (reached from OUTSIDE this repo) collapses to
      a plain same-repo import of `coordinator_core.session.shape` —
      already this tree, no plane boundary to cross, no "engine
      unresolvable"/"engine unimportable" failure mode (those two
      `_log_producer_capture_failure` reasons can no longer fire from this
      call site; left in place below only because `producer_set` itself
      may still raise for an out-of-contract value, which every other
      branch already guards).
  (c) `_resolve_repo_root`'s local zero-spawn `.git` walk is UNCHANGED
      (already zero-spawn in the DoE source) — kept verbatim rather than
      swapped for `coordinator_core.git.repo_root.show_toplevel` so this
      hook's own cwd-inheritance contract with the `pickup-assemble`
      subprocess (documented in the function's own docstring: the child
      inherits this process's OS cwd, not `payload["cwd"]`) stays the
      exact walk it was proven against.
  (d) `context_envelope(...)` (a stdout-printing wrapper) becomes
      `coordinator_core.hooks._envelope.context_only`, returning the
      envelope dict directly instead of printing it.

`pickup-assemble brief`/`apply` remain real subprocess calls (unchanged
from the DoE source) — no in-process op equivalent exists in this tree for
that CLI's baton-grab/claim-lifecycle logic; both are already vetted,
timeout-bounded, and fail-open per the safety envelope below.

Safety envelope (AC9, unchanged from DoE source), each clause load-bearing:
  (a) SESSION-ID PROPAGATION, not event filtering — see `_apply_argv`'s
      analogue, `_fire_apply`, and `_run_pickup_assemble`.
  (b) An absent or unrecognized `coast` verdict reads as HOLD, never
      permission — see `coast_verdict`/`should_apply`.
  (c) Hook errors / timeouts / transport failures degrade to compute-only
      or total silence, and NEVER block `/pickup`.
  (d) `additionalContext` never exceeds `_CONTEXT_BUDGET_CHARS` (10,000).
      See `render_additional_context`'s own docstring for the full
      degrade-priority ladder.

Cross-repo consumer contract (negative-spec, unchanged from DoE source):
this hook is a HARD consumer of the engine repo's `pickup-assemble
brief`/`apply` CLI's output shape — see `decode_decision_payload`'s own
docstring for the pinned N==1/N>1 shapes. A future engine-side change to
that shape breaks this hook silently (fail-open swallows the parse failure
into "no output") unless a round-trip contract fixture also pins it.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W4-C12.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Tuple

from coordinator_core.hooks._envelope import context_only, no_advisory
from coordinator_core.hooks.support.forwarder_resolve import forwarder_argv, resolve_forwarder
from coordinator_core.hooks.support.skill_invocation import read_invocation
from coordinator_core.ipc import register_op

# --- Constants -------------------------------------------------------------

_PICKUP_COMMAND_NAMES = frozenset({"pickup"})

# Kept literally identical to `mise_autofire.py :: _MISE_COMMAND_NAMES`; a
# verb in one and not the other starts the run half-wired, silently.
_BATON_GRAB_COMMAND_NAMES = frozenset({"mise-en-place", "warp-speed-execute"})

_BATON_PATH_FAMILIES = (
    "state/handoffs/",
    "archive/handoffs/",
    "state/cross-repo/inbox/",
    "state/cross-repo/archive/",
    "cross-repo/inbox/",
    "cross-repo/archive/",
)

_CONTEXT_BUDGET_CHARS = 10_000

_PROSE_SPLIT_RE = re.compile(r"\s+--\s+|\s+--$")


def split_prose_tail(command_args: str) -> Tuple[str, Optional[str]]:
    """Split `command_args` into `(path_string, prose_or_None)` on the FIRST
    match of `_PROSE_SPLIT_RE` (DEC-1)."""
    parts = _PROSE_SPLIT_RE.split(command_args, maxsplit=1)
    if len(parts) == 2:
        left, right = parts
        prose = right.strip()
        return left.strip(), (prose if prose else None)
    return command_args, None


_AND_SPLIT_RE = re.compile(r"\s+AND\s+", flags=re.IGNORECASE)


def _is_baton_path(token: str) -> bool:
    """True iff `token` names an artifact family that carries a claim
    lifecycle."""
    normalized = PureWindowsPath(token).as_posix()
    return any(family in normalized for family in _BATON_PATH_FAMILIES)


def extract_baton_paths(command_args: str) -> str:
    """Extract the baton paths from a MIXED argument string, re-joined with
    ` AND ` for `pickup-assemble brief`."""
    tokens: List[str] = []
    for chunk in _AND_SPLIT_RE.split(command_args):
        tokens.extend(chunk.split())
    return " AND ".join(token for token in tokens if _is_baton_path(token))


_BRIEF_TIMEOUT_SECONDS = 12
_APPLY_TIMEOUT_SECONDS = 20

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PROBE_LOG_NAME = "pickup-autofire-events.jsonl"
_PROBE_LOG_PATH_ENV = "PICKUP_AUTOFIRE_PROBE_LOG_PATH"


def resolve_settings_home() -> Path:
    """Resolve the coordinator-claude settings-home root — unchanged
    precedence from the DoE source and its `mise_autofire.py` sibling."""
    override = os.environ.get("COORDINATOR_SETTINGS_HOME")
    if override:
        return Path(override)
    base = os.environ.get("CLAUDE_HOME") or str(Path.home())
    return Path(base) / ".coordinator-claude-settings"


def resolve_pickup_assemble_bin(settings_home: Path) -> Optional[Path]:
    """Resolve the installed `pickup-assemble` forwarder under
    `settings_home`. Returns None -- the caller treats that as a transport
    failure and fails open (AC9c)."""
    return resolve_forwarder(settings_home / "bin", "pickup-assemble")


def pickup_assemble_argv(script_path: Path, tail: list) -> list:
    return forwarder_argv(script_path, tail)


class _TransportFailure(Exception):
    """Raised internally when a `pickup-assemble` invocation could not be
    completed at all (binary unresolvable, spawn failure, or timeout)."""


def _run_pickup_assemble(
    script_path: Path, tail: list, session_id: str, timeout: float
) -> subprocess.CompletedProcess:
    """Run the resolved forwarder, propagating `session_id` into the child
    environment (`COORDINATOR_SESSION_ID`) as a belt-and-suspenders
    companion to the explicit `--session-id` argument. Raises
    `_TransportFailure` on ANY failure to complete the subprocess."""
    env = dict(os.environ)
    if session_id:
        env["COORDINATOR_SESSION_ID"] = session_id
    try:
        argv = pickup_assemble_argv(script_path, tail)
        return subprocess.run(
            argv,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _TransportFailure(str(exc)) from exc


def decode_decision_payload(stdout: str) -> List[dict]:
    """Parse a `pickup-assemble brief`/`apply` stdout blob into a list of
    decision-object dicts (DEC-2), normalizing the N==1/N>1 cross-repo
    output shapes (see module docstring's cross-repo consumer contract)."""
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(obj, dict):
        return [obj]
    if isinstance(obj, list):
        return [item for item in obj if isinstance(item, dict)]
    return []


# --- Decision-object predicates (AC9b) ---------------------------------------


def coast_verdict(decision: dict) -> Optional[str]:
    """The `gates.coast.verdict` string, or None when absent/malformed."""
    gates = decision.get("gates")
    if not isinstance(gates, dict):
        return None
    coast = gates.get("coast")
    if not isinstance(coast, dict):
        return None
    verdict = coast.get("verdict")
    return verdict if isinstance(verdict, str) else None


def judgment_points_are_empty(decision: dict) -> bool:
    jps = decision.get("judgment_points")
    return isinstance(jps, list) and len(jps) == 0


def should_apply(decision: dict) -> bool:
    """Apply ONLY on `coast == clear` AND `judgment_points == []`."""
    return coast_verdict(decision) == "clear" and judgment_points_are_empty(decision)


# --- additionalContext rendering (AC9d) --------------------------------------


def _unclaimed_summary(decisions: list, multi: bool, subagent_guard: bool = False) -> Optional[str]:
    """One line per briefed baton this run did NOT claim, naming why."""
    lines = []
    for index, decision in enumerate(decisions):
        would_apply = should_apply(decision)
        if would_apply and not subagent_guard:
            continue
        if would_apply and subagent_guard:
            artifact = decision.get("artifact") if isinstance(decision, dict) else None
            name = (artifact or {}).get("path") if isinstance(artifact, dict) else None
            label = f"baton {index + 1}" if multi else "baton"
            lines.append(
                f"  - {name or label}: not claimed: a claim from a subagent's tool "
                "call is not the main session's deliberate grab, whatever "
                "session_id it carries"
            )
            continue
        gates = decision.get("gates") if isinstance(decision, dict) else None
        grant = (gates or {}).get("claim_grant") if isinstance(gates, dict) else None
        grant = grant if isinstance(grant, dict) else {}
        artifact = decision.get("artifact") if isinstance(decision, dict) else None
        name = (artifact or {}).get("path") if isinstance(artifact, dict) else None
        holder = grant.get("holder")
        reason = str(grant.get("reason") or "").strip()
        if holder and not grant.get("held_by_self"):
            live = "live" if grant.get("holder_live") else "not live in this box's registry"
            why = f"claimed by session {holder} ({live})"
        elif reason:
            why = reason
        else:
            why = "not claimable, and the brief carried no claim_grant reason to quote"
        label = f"baton {index + 1}" if multi else "baton"
        lines.append(f"  - {name or label}: {why}")
    if not lines:
        return None
    return "Briefed but NOT claimed by this run:\n" + "\n".join(lines)


def _resolve_repo_root() -> Optional[Path]:
    """Zero-spawn mirror of the engine repo's `apply.py::resolve_repo_root`.
    Reimplemented as a pure-Python upward walk for a `.git` entry rather
    than shelling out to `git rev-parse` -- unchanged from the DoE source.
    Returns None when undeterminable."""
    try:
        cwd = Path.cwd()
    except OSError:
        return None
    try:
        for candidate in (cwd, *cwd.parents):
            if (candidate / ".git").exists():
                return candidate
    except OSError:
        return None
    return None


def _sanitize_for_filename(value: str) -> str:
    """Byte-for-byte mirror of the engine repo's
    `coordinator_core/pickup_assemble/apply.py::_sanitize_for_filename`."""
    return value.replace("/", "__").replace("\\", "__")


def _session_decision_file_path(repo_root: Path, session_id: str, artifact_path: str) -> Path:
    """Byte-for-byte mirror of the engine repo's
    `apply.py::_session_decision_file_path`/`_session_decision_file_dir`."""
    name = f"{_sanitize_for_filename(session_id)}__{_sanitize_for_filename(artifact_path)}.json"
    return repo_root / ".git" / "coordinator-sessions" / "decisions" / name


def _write_decision_files(decisions: List[dict], session_id: str) -> List[Path]:
    """Best-effort: write EACH decoded decision to its own engine-computed
    path, keyed off that baton's OWN resolved `artifact.path`. Never
    raises."""
    repo_root = _resolve_repo_root()
    if repo_root is None:
        return []
    tag = session_id or "unknown-session"
    written: List[Path] = []
    try:
        for decision in decisions:
            artifact = decision.get("artifact")
            artifact_path = (artifact or {}).get("path") if isinstance(artifact, dict) else None
            if not (isinstance(artifact_path, str) and artifact_path):
                continue
            path = _session_decision_file_path(repo_root, tag, artifact_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")
            written.append(path)
    except OSError:
        return []
    return written


def _your_call_text(decision: dict) -> Optional[str]:
    """Render the guidance-bearing `judgment_points[].dispositions[]`
    entries as a "Your call" prose block."""
    jps = decision.get("judgment_points")
    if not isinstance(jps, list):
        return None
    lines: List[str] = []
    for jp in jps:
        if not isinstance(jp, dict):
            continue
        question = jp.get("question")
        question_prefix = f"{question} — " if isinstance(question, str) and question else ""
        dispositions = jp.get("dispositions")
        if not isinstance(dispositions, list):
            continue
        for disp in dispositions:
            if not isinstance(disp, dict):
                continue
            value = disp.get("value")
            guidance = disp.get("guidance")
            if not (isinstance(value, str) and value):
                continue
            if not (isinstance(guidance, str) and guidance):
                continue
            lines.append(f"- {question_prefix}`{value}`: {guidance}")
    if not lines:
        return None
    return "Your call:\n" + "\n".join(lines)


def _evidence_judgment_points(decision: dict) -> Optional[list]:
    """The `judgment_points` slice retained in the droppable evidence tail:
    each judgment_point's `dispositions[]` has its guidance-bearing entries
    stripped (promoted to `_your_call_text`)."""
    jps = decision.get("judgment_points")
    if not isinstance(jps, list):
        return jps
    trimmed = []
    for jp in jps:
        if not isinstance(jp, dict):
            trimmed.append(jp)
            continue
        dispositions = jp.get("dispositions")
        if not isinstance(dispositions, list):
            trimmed.append(jp)
            continue
        kept_dispositions = [
            disp
            for disp in dispositions
            if not (
                isinstance(disp, dict)
                and isinstance(disp.get("guidance"), str)
                and disp.get("guidance")
            )
        ]
        trimmed.append({**jp, "dispositions": kept_dispositions})
    return trimmed


def _evidence_is_informative(decision: dict) -> bool:
    """True iff the Evidence tail would carry a fact beyond what
    `verdict_text` already states."""
    jps = decision.get("judgment_points")
    if isinstance(jps, list) and jps:
        return True
    gates = decision.get("gates")
    if not isinstance(gates, dict):
        return bool(gates)
    if set(gates.keys()) - {"coast"}:
        return True
    coast = gates.get("coast")
    if isinstance(coast, dict):
        return bool(set(coast.keys()) - {"verdict"})
    return bool(coast)


def _decision_segments(
    decision: dict, index: int, multi: bool
) -> Tuple[Optional[str], str, Optional[str], Optional[str], Optional[str]]:
    """Compute `(narration_text, verdict_text, next_move_text,
    your_call_text, evidence_text)` for ONE decoded decision object."""
    narration = decision.get("narration")
    narration_text = narration if isinstance(narration, str) and narration else None

    verdict = coast_verdict(decision)
    jps = decision.get("judgment_points")
    jp_count = len(jps) if isinstance(jps, list) else "?"
    verdict_text = f"Verdict: coast={verdict!r}, judgment_points={jp_count}"

    next_move = decision.get("next_move")
    next_move_text = f"Next move: {next_move}" if isinstance(next_move, str) and next_move else None

    your_call_text = _your_call_text(decision)

    evidence_text = None
    if _evidence_is_informative(decision):
        evidence_payload = {
            "gates": decision.get("gates"),
            "judgment_points": _evidence_judgment_points(decision),
        }
        try:
            evidence_text = "Evidence:\n" + json.dumps(evidence_payload, indent=2, sort_keys=True)
        except (TypeError, ValueError):
            evidence_text = None

    if multi:
        prefix = f"[Baton {index + 1}] "
        if narration_text:
            narration_text = prefix + narration_text
        verdict_text = prefix + verdict_text
        if next_move_text:
            next_move_text = prefix + next_move_text
        if your_call_text:
            your_call_text = prefix + your_call_text
        if evidence_text:
            evidence_text = prefix + evidence_text

    return narration_text, verdict_text, next_move_text, your_call_text, evidence_text


def render_additional_context(
    decisions: List[dict],
    pointer_paths: List[Path],
    prose: Optional[str] = None,
    subagent_guard: bool = False,
) -> str:
    """Render the `additionalContext` string per the AC9(d)/AC14/AC15/DEC-4
    priority list, generalized to N decoded batons -- see the DoE source's
    own extensive docstring for the full degrade-priority ladder (unchanged
    logic, reproduced verbatim below)."""
    if not decisions:
        return ""

    multi = len(decisions) > 1
    prose_text = f"EM note: {prose}" if prose else None

    per_baton = [_decision_segments(d, i, multi) for i, d in enumerate(decisions)]

    protected: List[Tuple[str, Optional[str]]] = []
    if prose_text:
        protected.append(("prose", prose_text))
    unclaimed_text = _unclaimed_summary(decisions, multi, subagent_guard=subagent_guard)
    if unclaimed_text:
        protected.append(("unclaimed", unclaimed_text))
    baton_offset = len(protected)
    narration_slots: List[int] = []
    for narration_text, verdict_text, next_move_text, your_call_text, _ in per_baton:
        if narration_text is not None:
            narration_slots.append(len(protected))
        protected.append(("narration", narration_text))
        protected.append(("verdict", verdict_text))
        protected.append(("next_move", next_move_text))
        protected.append(("your_call", your_call_text))

    if not pointer_paths:
        pointer_text = None
    elif len(pointer_paths) == 1:
        pointer_text = f"Full decision object: {pointer_paths[0]}"
    else:
        listing = "\n".join(f"  - {p}" for p in pointer_paths)
        pointer_text = f"Full decision payload (N={len(pointer_paths)}):\n{listing}"

    droppable: List[Tuple[str, Optional[str]]] = []
    if pointer_text:
        droppable.append(("pointer", pointer_text))
    for _, _, _, _, evidence_text in per_baton:
        if evidence_text:
            droppable.append(("evidence", evidence_text))

    def _join(segs: List[Tuple[str, Optional[str]]]) -> str:
        return "\n\n".join(text for _, text in segs if text)

    kept = protected + droppable
    rendered = _join(kept)

    while len(rendered) > _CONTEXT_BUDGET_CHARS and len(kept) > len(protected):
        kept.pop()
        rendered = _join(kept)

    if len(rendered) > _CONTEXT_BUDGET_CHARS:
        kept = list(protected)
        if not narration_slots:
            num_batons = len(per_baton)
            for keep_count in range(num_batons, 0, -1):
                trial = protected[: baton_offset + keep_count * 4]
                trial_rendered = _join(trial)
                if len(trial_rendered) <= _CONTEXT_BUDGET_CHARS or keep_count == 1:
                    return trial_rendered[:_CONTEXT_BUDGET_CHARS]
            return _join(kept)[:_CONTEXT_BUDGET_CHARS]

        texts = {i: (kept[i][1] or "") for i in narration_slots}
        rest = _join([seg for j, seg in enumerate(kept) if j not in narration_slots])
        while True:
            live = [t for t in texts.values() if t]
            separator_slack = 2 * len(live) if rest else 0
            budget_for_narrations = max(_CONTEXT_BUDGET_CHARS - len(rest) - separator_slack, 0)
            if sum(len(t) for t in texts.values()) <= budget_for_narrations:
                break
            if not any(texts.values()):
                break
            longest = max(texts, key=lambda i: len(texts[i]))
            texts[longest] = texts[longest][:-1]

        for i in narration_slots:
            orig = kept[i][1] or ""
            truncated = texts[i]
            if truncated and len(truncated) < len(orig):
                truncated = truncated[:-1] + "…" if truncated else "…"
            kept[i] = (kept[i][0], truncated if truncated else None)
        rendered = _join(kept)

    return rendered[:_CONTEXT_BUDGET_CHARS]


# --- Skill-tool-firing measurement instrumentation ---------------------------


def _probe_log_path() -> Path:
    """Resolve the probe-log path, honoring `_PROBE_LOG_PATH_ENV`."""
    override = os.environ.get(_PROBE_LOG_PATH_ENV)
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / _PROBE_LOG_NAME


def _log_probe_event(payload: dict) -> None:
    """Best-effort, near-zero-marginal-cost instrumentation: append one JSON
    line per hook firing. Never raises."""
    try:
        command_name = payload.get("command_name")
        if command_name is None:
            tool_input = payload.get("tool_input")
            if isinstance(tool_input, dict):
                for key in ("skill", "command"):
                    value = tool_input.get(key)
                    if isinstance(value, str) and value:
                        command_name = value
                        break
        record = {
            "ts": time.time(),
            "command_name": command_name,
            "command_source": payload.get("command_source"),
            "expansion_type": payload.get("expansion_type"),
            "session_id": payload.get("session_id"),
            "hook_event_name": payload.get("hook_event_name"),
        }
        log_path = _probe_log_path()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def _log_producer_capture_failure(session_id: str, typed_command: Optional[str], reason: str) -> None:
    """Best-effort append to the SAME probe log `_log_probe_event` already
    writes. Never raises."""
    try:
        record = {
            "ts": time.time(),
            "event": "producer_capture_failed",
            "session_id": session_id,
            "typed_command": typed_command,
            "reason": reason,
        }
        log_path = _probe_log_path()
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass


def _capture_producer(payload: dict, command_name: str, session_id: str, cwd: Optional[str]) -> None:
    """Capture the typed command for THIS turn into `session-shape.json`'s
    namespaced `producer` record, via the engine's `producer_set`
    entrypoint. Imported directly in-process (no cross-plane root
    resolution — see module docstring, shape change (b)). Never raises."""
    if payload.get("expansion_type") != "slash_command":
        return  # not a confirmed slash-command turn -- leave prior value (D3)

    typed_command = command_name if command_name else "unresolved"

    if not session_id:
        _log_producer_capture_failure(session_id or "", typed_command, "missing_session_id")
        return

    try:
        from coordinator_core.session import shape as _shape
    except Exception:
        _log_producer_capture_failure(session_id, typed_command, "engine_unimportable")
        return

    try:
        ok = _shape.producer_set(session_id, typed_command=typed_command, cwd=cwd)
    except Exception as exc:
        _log_producer_capture_failure(
            session_id, typed_command, f"producer_set_raised:{type(exc).__name__}"
        )
        return

    if not ok:
        _log_producer_capture_failure(session_id, typed_command, "lock_failed")


# --- Mutating half (AC9a) ----------------------------------------------------


def _fire_apply(script_path: Path, artifact_path: str, session_id: str) -> None:
    """Best-effort apply — never raises, never surfaces its own exit code."""
    if not session_id:
        return
    try:
        _run_pickup_assemble(
            script_path,
            ["apply", artifact_path, "--session-id", session_id],
            session_id,
            _APPLY_TIMEOUT_SECONDS,
        )
    except _TransportFailure:
        pass


# --- Entry point --------------------------------------------------------------


def compute_context(payload: dict) -> Optional[str]:
    """Compute the bare `additionalContext` prose for ONE hook firing.
    Reads either entry-path shape via `read_invocation`. Returns `None`
    whenever there is nothing to inject. Never raises."""
    if not isinstance(payload, dict):
        payload = {}

    _log_probe_event(payload)

    inv = read_invocation(payload)
    if inv is None:
        return None  # unrecognized payload shape -- silent pass

    try:
        _capture_producer(payload, inv.command_name, inv.session_id, inv.cwd or None)
    except Exception as exc:
        _log_producer_capture_failure(
            inv.session_id,
            inv.command_name if inv.command_name else "unresolved",
            f"capture_raised:{type(exc).__name__}",
        )

    is_pickup = inv.command_name in _PICKUP_COMMAND_NAMES
    is_baton_grab = inv.command_name in _BATON_GRAB_COMMAND_NAMES
    if not (is_pickup or is_baton_grab):
        return None  # not a baton-taking verb -- silent pass

    command_args = inv.command_args
    if not command_args:
        return None  # nothing to compute a brief against

    path_string, prose = split_prose_tail(command_args)
    if is_baton_grab:
        path_string = extract_baton_paths(path_string)
    if not path_string:
        return None  # prose-only invocation -- nothing to compute a brief against

    settings_home = resolve_settings_home()
    script_path = resolve_pickup_assemble_bin(settings_home)
    if script_path is None:
        return None  # transport failure (AC9c) -- CLI unresolvable, fail open

    try:
        result = _run_pickup_assemble(
            script_path, ["brief", path_string], inv.session_id, _BRIEF_TIMEOUT_SECONDS
        )
    except _TransportFailure:
        return None  # AC9c

    decisions = decode_decision_payload(result.stdout)
    if not decisions:
        return None  # AC9c -- unparseable/empty output is a transport failure too

    subagent = inv.agent_id is not None

    if not subagent:
        for decision in decisions:
            if not should_apply(decision):
                continue
            artifact = decision.get("artifact")
            apply_path = (artifact or {}).get("path") if isinstance(artifact, dict) else None
            if isinstance(apply_path, str) and apply_path:
                _fire_apply(script_path, apply_path, inv.session_id)

    pointer_paths = [] if subagent else _write_decision_files(decisions, inv.session_id)
    additional_context = render_additional_context(
        decisions, pointer_paths, prose, subagent_guard=subagent
    )
    if not additional_context:
        return None

    return additional_context


@register_op("hooks.pickup_autofire")
def _handler(params: dict, repo_root=None) -> dict:
    """UserPromptExpansion / PreToolUse(Skill) op: compute and inject the
    `/pickup`/baton-grab brief; fire the mutating `apply` half on a clear
    coast. `params` IS the raw payload dict."""
    try:
        additional_context = compute_context(params if isinstance(params, dict) else {})
    except Exception:
        # Total-function guard (AC9c) -- must never raise into the caller;
        # every internal step already fails open on its own.
        additional_context = None

    if additional_context is None:
        return no_advisory()
    return context_only("UserPromptExpansion", additional_context)
