"""Resolve an emitted research-pipeline run and append revisions to its revisions file.

A pipeline run's stages read ``revisions_rel(run_id)`` at stage start when the emitted script
carries ``REVISIONS_MARKER``; ``workflow.revise`` appends here.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

from coordinator_core.atomic_append import append_line
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT, RUN_ID_PREFIX
from coordinator_core.session.declared_writes import declare_write

__all__ = [
    "REVISIONS_MARKER", "FINISHED_REL", "emitted_pipeline_run", "run_name", "revisions_rel", "finished_rel",
    "pipeline_run_state", "reads_revisions", "append_revision", "asks_peer_relay", "executing_run",
    "stage_mailboxes", "mail_revision",
]

#: The preamble sentence an emitted pipeline script carries when its stages read the revisions file.
REVISIONS_MARKER = "Read the run's revisions file before acting; its newest entries override earlier scope."

_ESCAPED_MARKER = REVISIONS_MARKER.replace("'", "\\'")

_SCRIPT_SUFFIX = ".workflow.mjs"
_EMITTED_SUFFIX = _SCRIPT_SUFFIX + ".emitted.json"


def emitted_pipeline_run(root: Path, run_id: str) -> Optional[Path]:
    """Script path of an emitted pipeline run under ``root``, or None (wrong prefix, unsafe id, no receipt)."""
    if not isinstance(run_id, str) or not run_id.startswith(RUN_ID_PREFIX) or not safe_id(run_id):
        return None
    base = Path(root) / RUN_DIR_ROOT
    receipt = contained_path(base / f"{run_id}{_EMITTED_SUFFIX}", [base])
    if receipt is None or not receipt.is_file():
        return None
    return base / f"{run_id}.workflow.mjs"


def run_name(pipeline: str, run_id: str) -> str:
    """The run's directory and script-stem name: ``run_id`` when already a ``pipeline-`` id, else prefixed by ``pipeline``.

    Trap: a ``pipeline-``-prefixed id of another pipeline (``pipeline-research-...``) stays verbatim; re-prefixing it
    names a directory ``workflow.revise`` never writes.
    """
    return run_id if run_id.startswith(RUN_ID_PREFIX) else f"{RUN_ID_PREFIX}{pipeline}-{run_id}"


def revisions_rel(run_id: str) -> str:
    """Repo-relative path of the run's revisions file."""
    return f"{RUN_DIR_ROOT}/{run_id}/revisions.md"


#: Terminal marker the last composed stage writes under the run dir; its presence means the run finished.
FINISHED_REL = "finished.md"


def finished_rel(run_id: str) -> str:
    """Repo-relative path of the run's terminal marker."""
    return f"{RUN_DIR_ROOT}/{run_id}/{FINISHED_REL}"


def pipeline_run_state(root: Path, run_id: str) -> str:
    """"unknown" (no receipt or bad id), "finished" (terminal marker present) or "live".

    Trap: "live" means no marker yet, not running; a failed final stage never writes it.
    """
    if emitted_pipeline_run(root, run_id) is None:
        return "unknown"
    return "finished" if (Path(root) / finished_rel(run_id)).is_file() else "live"


def reads_revisions(script: str) -> bool:
    """True when the emitted script text carries ``REVISIONS_MARKER``, verbatim or JS-single-quote escaped.

    Trap: a grind PREAMBLE is emitted through ``_js_string_literal``, which writes the marker's apostrophe as ``\\'``.
    """
    return REVISIONS_MARKER in script or _ESCAPED_MARKER in script


#: Scripts `executing_run` reads, newest first; a research run older than this many fires is not the live one.
EXECUTING_SCAN_CAP = 40

_RELAY_VERB = r"(?:relay|forward|notify|ping|tell)"
_PEER = r"(?:peers?|sessions?|teammates?|siblings?|claude-[\w-]*-\d+)"
_PEER_RELAY = re.compile(
    rf"send[\s_-]*message|\b{_RELAY_VERB}\b[^.\n]{{0,80}}?\b{_PEER}(?![\w-])", re.IGNORECASE
)


def asks_peer_relay(text: str) -> bool:
    """True when ``text`` asks a stage to SendMessage, relay to or notify a peer session.

    Lexical and lenient toward prose that merely mentions a peer: a relay verb must govern the peer
    within one clause.
    """
    return _PEER_RELAY.search(str(text)) is not None


def executing_run(root: Path, sizing_rel: str) -> Optional[str]:
    """The live research ``pipeline-`` run id whose emitted script closes against ``sizing_rel``, else None.

    The script carries its ``research.close`` params (``"sizing": <rel>``), so the ask run that
    reached it through a sizing is found by that path alone; newest script wins.
    """
    base = Path(root) / RUN_DIR_ROOT
    needle = f'"sizing": {json.dumps(sizing_rel)}'
    try:
        scripts = sorted(
            base.glob(f"{RUN_ID_PREFIX}research-*{_SCRIPT_SUFFIX}"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return None
    for script in scripts[:EXECUTING_SCAN_CAP]:
        run_id = script.name[: -len(_SCRIPT_SUFFIX)]
        if pipeline_run_state(root, run_id) != "live":
            continue
        try:
            if needle in script.read_text(encoding="utf-8"):
                return run_id
        except OSError:
            continue
    return None


def append_revision(
    root: Path, run_id: str, text: str, sender: str, kind: str, *, rel: Optional[str] = None
) -> Path:
    """Append one stamped block to the run's revisions file in a single write; returns the file path.

    ``rel`` overrides the repo-relative file (a blitz run's); default is ``revisions_rel(run_id)``.
    """
    path = Path(root) / (rel if rel is not None else revisions_rel(run_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = re.sub(r"(?m)^## ", r"\\## ", str(text).strip("\n"))
    block = f"## {stamp} from {sender} ({kind})\n\n{body}\n\n"
    append_line(path, block.encode("utf-8"))
    declare_write(path)
    return path


#: The run's upward mailbox; a revision is never mailed to it.
_EM_MAILBOX = "_em.jsonl"


def stage_mailboxes(root: Path, run_id: str) -> Optional[list[Path]]:
    """Mailbox files of the stages dispatched so far (``<run dir>/mail/*.jsonl`` minus the EM's), sorted.

    None when the run has no ``mail`` directory: the stages cannot be enumerated from run state.
    """
    mail = Path(root) / RUN_DIR_ROOT / run_id / "mail"
    try:
        return sorted(p for p in mail.glob("*.jsonl") if p.name != _EM_MAILBOX and p.is_file())
    except OSError:
        return None


def mail_revision(root: Path, run_id: str, text: str, sender: str, kind: str) -> tuple[list[str], list[str]]:
    """Append the revision as one ``{"from","text"}`` line to every dispatched stage's mailbox.

    Returns ``(delivered, unreached)`` stage names; ``unreached`` holds each mailbox the write failed on,
    or the single entry ``"<stages not enumerable: no mail directory>"`` when the run has none.
    """
    boxes = stage_mailboxes(root, run_id)
    if not (Path(root) / RUN_DIR_ROOT / run_id / "mail").is_dir() or boxes is None:
        return [], ["<stages not enumerable: no mail directory>"]
    line = json.dumps({"from": sender, "text": f"REVISION ({kind}): {text}"}, ensure_ascii=False) + "\n"
    delivered: list[str] = []
    unreached: list[str] = []
    for box in boxes:
        try:
            append_line(box, line.encode("utf-8"))
            declare_write(box)
            delivered.append(box.stem)
        except OSError:
            unreached.append(box.stem)
    return delivered, unreached
