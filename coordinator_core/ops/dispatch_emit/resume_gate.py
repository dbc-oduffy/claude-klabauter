"""Resume gate: the checks a refire of a halted run passes before it resumes that run, and the takeover log.

A run that is cancelled, superseded or closed is refused; a run whose recorded owners (run-stage.json
`session_id`, `submitter_session_id`, `driver_session_ids`, `adopted_from`: the submitting session and the
headless child it launched count as one owner; a record stamped before `submitter_session_id` existed adds the
session its emission receipt names) do not include the caller is refused unless the caller names a takeover
reason. A run with no recorded driver is never refused. Marker and record reads only, no spawn.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit import halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import SUBMITTER_FIELD, read_run_stage
from coordinator_core.ops.dispatch_emit.halted_hold_release import supersession_reason

TAKEOVER_LOG = "takeover.jsonl"

REFUSE_CANCELLED = "resume-cancelled"
REFUSE_SUPERSEDED = "resume-superseded"
REFUSE_CLOSED = "resume-closed"
REFUSE_FOREIGN_DRIVER = "resume-foreign-driver"
REFUSE_BATON_UNRESOLVED = "resume-baton-unresolved"
BATON_CHAIN_MAX = 8


class ResumeRefused(ValueError):
    """A resume the gate refuses; `reason` is one of the `REFUSE_*` codes, `str(exc)` the agent-facing text."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def takeover_reason(args: object) -> Optional[str]:
    """The non-empty `takeover.reason` text of a refire's `args`, else None."""
    take = args.get("takeover") if isinstance(args, dict) else None
    text = take.get("reason") if isinstance(take, dict) else None
    return text.strip() if isinstance(text, str) and text.strip() else None


def _owners(stage: dict, root: Optional[Path] = None, run_id: Optional[str] = None) -> set:
    """Every session a run-stage record names as an owner: `session_id`, `submitter_session_id`,
    `driver_session_ids` and `adopted_from`. A legacy record (no `submitter_session_id`) with `root` and
    `run_id` also counts the session its emission receipt names, the one that submitted it."""
    out = {stage.get("session_id"), stage.get(SUBMITTER_FIELD)}
    if not stage.get(SUBMITTER_FIELD) and root is not None and run_id:
        out.add(halted_run.recorded_emitter(Path(root), run_id))
    for key in ("driver_session_ids", "adopted_from"):
        val = stage.get(key)
        out.update(val if isinstance(val, list) else ())
    return {s for s in out if isinstance(s, str) and s}


def check_resume(root: Path, run_id: str, caller_session: Optional[str], takeover: Optional[str]) -> Optional[str]:
    """Refuse a resume of `run_id` or return the prior driver session when the caller takes the run over.

    Raises `ResumeRefused` for a cancelled, superseded or closed run, and for a driver other than
    `caller_session` when `takeover` is empty. Returns the prior driver session id only for an allowed
    takeover (the caller then calls `record_takeover`); None otherwise."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    rd = record_dir(Path(root), run_id)
    marker = halted_run._read(rd) or {}
    fresh = "Fire the sizing fresh (`args.fresh: true`)."
    if marker.get("status") == halted_run.STATUS_HALTED and marker.get("halted") == "cancelled":
        raise ResumeRefused(REFUSE_CANCELLED, f"Run {run_id} was cancelled. {fresh}")
    if marker.get("status") == halted_run.STATUS_CLOSED:
        raise ResumeRefused(REFUSE_CLOSED, f"Run {run_id} is closed. {fresh}")
    if marker.get("status") == halted_run.STATUS_SUPERSEDED or supersession_reason(Path(root), run_id):
        raise ResumeRefused(REFUSE_SUPERSEDED, f"Run {run_id} is superseded. {fresh}")
    stage = read_run_stage(Path(root), run_id) or {}
    driver = stage.get("session_id")
    if not isinstance(driver, str) or not driver or not caller_session or caller_session in _owners(stage, Path(root), run_id):
        return None
    if takeover:
        return driver
    raise ResumeRefused(
        REFUSE_FOREIGN_DRIVER,
        f"Run {run_id} is driven by session {driver}. Pass `args.takeover: {{reason: \"<why>\"}}` to take it over, "
        "or fire the sizing fresh (`args.fresh: true`).",
    )


def takeover_recorded(rd: Path) -> bool:
    """True when `rd` (a run's record dir) holds a takeover log row."""
    return (Path(rd) / TAKEOVER_LOG).is_file()


def record_takeover(rd: Path, from_session: str, to_session: str, reason: str) -> None:
    """Append `{at, from_session, to_session, reason}` to the run dir's takeover log."""
    row = {
        "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from_session": from_session, "to_session": to_session, "reason": reason,
    }
    with open(Path(rd) / TAKEOVER_LOG, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _continued_into_rel(root: Path, rel: str) -> Optional[str]:
    """Repo-relative path of the record `rel`'s `continued_into` names, or None when it names nothing that resolves."""
    from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
    from coordinator_core.ops.dispatch_emit.run_baton_cascade import resolve_run_baton
    from coordinator_core.ops.handoff_stamp import _resolve_continued_into
    from coordinator_core.ops.resolve_swept_baton import _find_first_match

    try:
        split = split_frontmatter((root / rel).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    value = read_fm_field_unquoted(split.fm_text, "continued_into") if split is not None else None
    value = value.strip().replace("\\", "/") if isinstance(value, str) else ""
    if not value or value in ("null", "~") or not _resolve_continued_into(root, value):
        return None
    state = resolve_run_baton(root, value)
    if state is not None:
        return state.rel
    hit = _find_first_match(root, value.rsplit("/", 1)[-1])
    return hit.relative_to(root).as_posix() if hit is not None else None


def resolve_baton_record(root: Path, rel: str) -> Optional[str]:
    """The live-or-terminal record at the end of `rel`'s `continued_into` chain, else None when `rel` resolves nowhere."""
    from coordinator_core.ops.dispatch_emit.run_baton_cascade import resolve_run_baton

    root = Path(root)
    state = resolve_run_baton(root, rel)
    if state is None:
        return None
    seen = {state.rel}
    for _ in range(BATON_CHAIN_MAX):
        nxt = _continued_into_rel(root, state.rel)
        successor = resolve_run_baton(root, nxt) if nxt else None
        if successor is None or successor.rel in seen:
            break
        state = successor
        seen.add(state.rel)
    return state.rel


def _rebind_baton(root: Path, run_id: str) -> None:
    """Point the run's recorded baton at its live-or-terminal record; refuse when it resolves nowhere or the rewrite fails."""
    from coordinator_core.ops.dispatch_emit.ask_contract import read_run_baton, write_run_baton

    recorded = read_run_baton(root, run_id)
    if recorded is None:
        return
    resolved = resolve_baton_record(root, recorded)
    if resolved is None:
        raise ResumeRefused(
            REFUSE_BATON_UNRESOLVED,
            f"Run {run_id}'s baton {recorded} resolves to no live or archived record. "
            "Fire the sizing fresh (`args.fresh: true`); that drops the run's kept work (landed rows and kept chunks).",
        )
    if resolved != recorded:
        try:
            write_run_baton(root, run_id, resolved)
        except (OSError, ValueError) as exc:
            raise ResumeRefused(
                REFUSE_BATON_UNRESOLVED,
                f"Run {run_id}'s baton {recorded} could not be rebound to {resolved} ({exc}). "
                "Fire the sizing fresh (`args.fresh: true`); that drops the run's kept work (landed rows and kept chunks).",
            ) from exc


def gate_resume(root: Path, run_id: str, caller_session: Optional[str], args: object, sizing_rel: Optional[str]) -> None:
    """The one gate every resume of `run_id` under its existing id passes: `check_resume`, then, on a takeover,
    the log row and a live escrowed run token for the caller. `args` is the refire's `args` or the op's params
    (`takeover.reason` read from either). Raises `ResumeRefused`; a log or token write failure never blocks."""
    reason = takeover_reason(args)
    prior = check_resume(root, run_id, caller_session, reason)
    if not (prior and reason and caller_session):
        return
    _rebind_baton(Path(root), run_id)
    from coordinator_core.ops.dispatch_emit import run_token
    from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT, record_dir

    try:
        record_takeover(record_dir(Path(root), run_id), prior, caller_session, reason)
        if run_token.escrowed(Path(root), run_id) is None:
            token = run_token.mint(
                Path(root), run_id, f"{RUN_DIR_ROOT}/{run_id}.workflow.mjs",
                session_id=caller_session, sizing_rel=sizing_rel, carry_prior=True,
            )
            run_token.escrow(Path(root), run_id, token)
    except (OSError, ValueError):
        pass
