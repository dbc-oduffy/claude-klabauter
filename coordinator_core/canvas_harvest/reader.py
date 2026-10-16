"""canvas_harvest.reader - the one-shot reader dispatch and the transcript-to-reader binding.

`dispatch` is pure: it names the single Agent-tool subagent the EM fires to read a
canvas. `named_reader` accepts a subagent transcript only when its sibling
`agent-<id>.meta.json` carries the description `dispatch` emitted for that artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.canvas_harvest import model

# Cross-repo pin: coordinator-content-repo's block-unharvested-canvas-dispatch gate exempts exactly
# `canvas-reader ([A-Za-z0-9_-]+)` (fullmatch). Any suffix loses the exemption.
READER_DESCRIPTION_PREFIX = "canvas-reader "

_STEM_PREFIX = "agent-"

# The prompt cites the canonical url rebuilt from the id: a bare id, a query or a fragment
# in the caller's url never reaches the subagent.
_CANVAS_URL = "https://claude.ai/artifact/{id}"

_PROMPT = """\
Read one claude.ai canvas completely and report nothing else.

Canvas: {url}

1. If the Artifact and ArtifactComments tools are deferred, load them with ToolSearch first.
2. Call Artifact to list the files of the canvas above.
3. Call Artifact ONCE with a `paths` input that names every file whose path starts with project/ (every project/** path), reading them all in that single call.
4. Call ArtifactComments on the canvas above. If the result carries a `cursor`, call it again with that cursor, and repeat until a result carries no cursor.
5. Perform no write action of any kind: no edit, no comment, no reply, no resolve.
6. Reply with only the word "done".
"""


def dispatch(url: str) -> dict:
    """The exact Agent call to fire for `url`: subagent_type, description, prompt, harvest_argv.

    Raises ValueError for anything that is not a canvas artifact url. `harvest_argv[0]`
    is a placeholder naming the settings-home bin dir: no coordinator CLI is on PATH.
    """
    artifact_id = model.artifact_id_from_url(url)
    if artifact_id is None:
        raise ValueError(f"not a canvas artifact url: {url!r}")
    return {
        "subagent_type": "general-purpose",
        "description": READER_DESCRIPTION_PREFIX + artifact_id,
        "prompt": _PROMPT.format(url=_CANVAS_URL.format(id=artifact_id)),
        "harvest_argv": [
            "<coordinator-bin>/canvas-harvest",
            "harvest",
            "--url", url,
            "--out", "<OUT>",
            "--reader",
            "--transcript", "<session-dir>/subagents/agent-<agentId>.jsonl",
        ],
    }


def named_reader(transcript: Path, artifact_id: str) -> str:
    """The agentId of `transcript` when its sibling meta.json names the reader for `artifact_id`.

    Raises HarvestError(READER_NOT_NAMED, <which check>) on a wrong stem, a missing or
    unparseable meta.json, or a description other than the emitted one.
    """
    transcript = Path(transcript)
    stem = transcript.stem
    agent_id = stem[len(_STEM_PREFIX):] if stem.startswith(_STEM_PREFIX) else ""
    if not agent_id:
        raise model.HarvestError(model.READER_NOT_NAMED, f"transcript stem {stem!r} is not agent-<id>")
    meta_path = transcript.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise model.HarvestError(model.READER_NOT_NAMED, f"meta.json unreadable: {meta_path.name}: {exc}")
    except ValueError as exc:
        raise model.HarvestError(model.READER_NOT_NAMED, f"meta.json unparseable: {meta_path.name}: {exc}")
    description = meta.get("description") if isinstance(meta, dict) else None
    if description != READER_DESCRIPTION_PREFIX + artifact_id:
        raise model.HarvestError(
            model.READER_NOT_NAMED,
            f"meta.json description {description!r} is not the reader for {artifact_id!r}",
        )
    return agent_id
