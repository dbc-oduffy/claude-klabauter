"""Channel contract for a run's terminal-commit params, shared by emitter and guard.

The commit-stage prompt carries the params as a sentinel-fenced block the agent never copies;
the run-token leg of `block_subagent_commit` reads that block back from the spawn's own
transcript (its computed-task record, which may follow a relayed user-request record) and
writes the params file itself. The agent types only the file path.

Commit hot path: stdlib, `ask_contract.RUN_DIR_ROOT` and the run-token id shape only. No
subprocess, no emit-side import.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import BinaryIO, Optional

from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

PARAMS_FILE_NAME = "terminal-commit-params.json"
BLOCK_OPEN = "-----BEGIN TERMINAL-COMMIT PARAMS-----"
BLOCK_CLOSE = "-----END TERMINAL-COMMIT PARAMS-----"
RELAY_HEADER = "[Workflow harness — user request]"
READ_CAP_BYTES = 64 * 1024

# Emitted into the composed script; must stay byte-equal to `render_params_block`
# (pinned by the node-gated agreement test). JSON.stringify never emits a raw newline.
PARAMS_BLOCK_JS = (
    "function _terminalCommitParamsBlock(p) {\n"
    f'  return "{BLOCK_OPEN}\\n" + JSON.stringify(p) + "\\n{BLOCK_CLOSE}";\n'
    "}"
)

_REQUIRED_STR_KEYS = ("run_id", "run_token", "script_path")


def params_path(repo_root: object, run_id: object) -> Optional[Path]:
    """Canonical params file for a run, or None when `run_id` is not a valid run id."""
    if not isinstance(run_id, str) or not run_token._RUN_ID_RE.match(run_id):
        return None
    return Path(str(repo_root)) / RUN_DIR_ROOT / run_id / PARAMS_FILE_NAME


def render_params_block(params: dict) -> str:
    body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
    return f"{BLOCK_OPEN}\n{body}\n{BLOCK_CLOSE}"


def prompt_params(prompt: object) -> Optional[dict]:
    """The params dict from the prompt's single block, or None on anything else.

    Lines are stripped because the harness indents computed task text. Split on `\n` only:
    `str.splitlines` also breaks on U+2028/U+2029/U+0085, which `JSON.stringify` leaves raw
    inside a string value, so a params note holding one would orphan the block.
    """
    if not isinstance(prompt, str):
        return None
    lines = [ln.strip() for ln in prompt.split("\n")]
    if lines.count(BLOCK_OPEN) != 1 or lines.count(BLOCK_CLOSE) != 1:
        return None
    start = lines.index(BLOCK_OPEN)
    if lines.index(BLOCK_CLOSE) != start + 2:
        return None
    try:
        params = json.loads(lines[start + 1])
    except ValueError:
        return None
    if not isinstance(params, dict):
        return None
    if not all(isinstance(params.get(k), str) for k in _REQUIRED_STR_KEYS):
        return None
    return params


def _is_workflow_spawn_path(transcript_path: str) -> bool:
    parts = transcript_path.replace("\\", "/").split("/")
    return any(a == "subagents" and b == "workflows" for a, b in zip(parts, parts[1:]))


def spawn_transcript_path(payload: dict) -> Optional[str]:
    """The workflow spawn's own transcript for a subagent hook payload, or None.

    A subagent's PreToolUse payload hands over the parent session's transcript, whose
    sibling `<session>/subagents/workflows/<wf>/agent-<agent_id>.jsonl` is the spawn's own;
    `agent_transcript_path` and a transcript already under `subagents/workflows/` are taken as is.
    One bounded glob of that directory, keyed on the harness-supplied agent id.
    """
    for key in ("agent_transcript_path", "transcript_path"):
        value = payload.get(key)
        if isinstance(value, str) and _is_workflow_spawn_path(value):
            return value
    session = payload.get("transcript_path")
    agent_id = payload.get("agent_id")
    if not (isinstance(session, str) and session.endswith(".jsonl")):
        return None
    if not (isinstance(agent_id, str) and agent_id.isalnum()):
        return None
    try:
        hits = sorted(Path(session[: -len(".jsonl")]).glob(f"subagents/workflows/*/agent-{agent_id}.jsonl"))
    except OSError:
        return None
    return str(hits[0]) if len(hits) == 1 else None


def _read_record_content(fh: BinaryIO) -> Optional[str]:
    """Content of the next capped transcript line when it is a `type: user` string record."""
    line = fh.readline(READ_CAP_BYTES + 1)
    if len(line) > READ_CAP_BYTES:
        return None
    record = json.loads(line.decode("utf-8"))
    if not isinstance(record, dict) or record.get("type") != "user":
        return None
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else None


def read_spawn_prompt(transcript_path: object) -> Optional[str]:
    """The spawn's computed-task record, or None. Never raises.

    Record 0, unless it is the harness's relayed user request: then record 1, and record 0's
    content is discarded unsearched because relayed user text can quote a block. At most two capped lines
    of the path the hook payload hands over: no listing, no third record.
    """
    if not isinstance(transcript_path, str) or not _is_workflow_spawn_path(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as fh:
            first = _read_record_content(fh)
            if first is not None and first.startswith(RELAY_HEADER):
                return _read_record_content(fh)
            return first
    except (OSError, ValueError):
        return None


def materialize(path: Path, params: dict) -> bool:
    """Write `params` as ASCII-only JSON via temp file plus `os.replace`; False on failure."""
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(params, ensure_ascii=True).encode("ascii")
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tcp-", suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
        return True
    except (OSError, ValueError, TypeError):
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        return False
