"""canvas_harvest.lift - one sequential transcript read yields a session's main-loop reads of one artifact.

Reads each transcript once in binary, parses only lines that hold an Artifact tool name or a
tool-use id already seen, skips sidechain records (except those of the one named
`reader_agent_id`) and a final line with no newline (a record mid-write), and never spawns.
The result is whole or a named model.HarvestError.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.canvas_harvest import model

_TOOL_NAMES = ("Artifact", "ArtifactComments")
_READ_ACTIONS = frozenset(  # actions that read; any other action is a write and is skipped
{"read", "list", "files", "get", "view", "comments"})


def _loads(line: bytes):
    try:
        return json.loads(line)
    except ValueError:
        return None


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "") for block in content if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    return ""


def _input_binds(inp: dict, artifact_id: str) -> bool:
    url = inp.get("url")
    if isinstance(url, str) and model.artifact_id_from_url(url) == artifact_id:
        return True
    return inp.get("artifact_id") == artifact_id


def _is_read_call(name: str, inp: dict) -> bool:
    if name == "ArtifactComments":
        return inp.get("action", "read") in _READ_ACTIONS
    action = inp.get("action")
    if isinstance(action, str):
        return action in _READ_ACTIONS
    return "paths" in inp or "path" in inp


_BEGIN_MARK = "=== BEGIN ARTIFACT COMMENTS "
_END_MARK = "=== END ARTIFACT COMMENTS"
_TOOL_ROWS = ("[on page]", "[location]", "[anchor detail]", "[presence when sent]")


def _is_attribution(row: str) -> bool:
    if not row.startswith("["):
        return False
    return not (row.startswith(_TOOL_ROWS) or "size cap" in row or "could not be read" in row)


def _rendered_texts(text: str) -> dict:
    """Per thread id, the comment texts one rendered comments result carries, in comment order.

    Keyed on the `Thread <id>` header, the BEGIN marker's nonce and `<nonce>| ` lines; a result with no
    nonce marker yields {}.
    """
    start = text.find(_BEGIN_MARK)
    if start < 0:
        return {}
    tail = text[start + len(_BEGIN_MARK):].split(None, 1)
    if not tail:
        return {}
    nonce = tail[0]
    prefix = nonce + "|"
    out: dict = {}
    comments = None
    cur = None
    for raw in text[start:].splitlines():
        if raw.startswith("Thread ") and len(raw.split()) == 2:
            comments = out.setdefault(raw.split()[1], [])
            cur = None
            continue
        if comments is None:
            continue
        if raw.startswith(_END_MARK):
            comments = None
            continue
        row = raw.strip()
        if row.startswith(prefix):
            if cur is not None:
                body = row[len(prefix):]
                cur.append(body[1:] if body.startswith(" ") else body)
        elif _is_attribution(row):
            cur = []
            comments.append(cur)
    return {tid: ["\n".join(lines).rstrip("\n") for lines in blocks] for tid, blocks in out.items()}


def _text_score(texts: list) -> tuple:
    return (sum(1 for t in texts if t), len(texts))


class _State:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        self.version: Optional[str] = None
        self.version_at = ""
        self.reads: dict = {}
        self.listing: Optional[dict] = None
        self.chain: Optional[dict] = None
        self.comments_seen = False
        self.error: Optional[model.ReadError] = None
        self.session_id: Optional[str] = None
        self.read_dir: Optional[Path] = None
        self.last_dir: Optional[Path] = None
        self.rendered: list = []
        self.texts: dict = {}

    def bound(self, entry: dict, directory: Path) -> None:
        sid = entry.get("sessionId")
        if isinstance(sid, str) and sid:
            self.session_id = sid
        self.last_dir = directory

    def note_version(self, ver, at: str) -> None:
        if not isinstance(ver, str) or not ver:
            return
        if ver != self.version:
            self.version = ver
            self.reads = {}
            self.listing = None
        self.version_at = at

    def on_files(self, payload: dict, files: list, at: str, directory: Path) -> None:
        ver = payload.get("ver")
        self.note_version(ver, at)
        if ver != self.version:
            return
        slot = self.reads.setdefault("frames", {})
        for item in files:
            if isinstance(item, dict) and model.is_project_file(item.get("path")):
                slot[item["path"]] = model.FrameSource(
                    path=item["path"],
                    saved_to=str(item.get("saved_to", "")),
                    sha256=str(item.get("sha256", "")),
                )
        info = {"url": str(payload.get("url", "")), "title": str(payload.get("title", "")), "at": at}
        kind = payload.get("type")
        info["type"] = str(kind.get("title", "")) if isinstance(kind, dict) else ""
        self.reads["info"] = info
        self.read_dir = directory

    def on_listing(self, payload: dict, at: str) -> None:
        ver = payload.get("ver")
        self.note_version(ver, at)
        if ver != self.version:
            return
        kind = payload.get("type")
        self.listing = {
            "paths": [
                f["path"] for f in payload.get("files", ()) if isinstance(f, dict) and model.is_project_file(f.get("path"))
            ],
            "type": str(kind.get("title", "")) if isinstance(kind, dict) else "",
        }

    def on_comments(self, call: dict, result: dict, text: str, at: str) -> None:
        self.comments_seen = True
        inp = call["input"]
        threads = result.get("threads")
        if not isinstance(threads, list):
            return
        if text not in self.rendered:
            self.rendered.append(text)
        for tid, texts in _rendered_texts(text).items():
            old = self.texts.get(tid)
            if old is None or _text_score(texts) > _text_score(old):
                self.texts[tid] = texts
        cursor = result.get("cursor")
        cursor = cursor if isinstance(cursor, str) and cursor else None
        if inp.get("thread_id") or "thread_filter" in result:
            return
        if inp.get("cursor"):
            chain = self.chain
            if chain is not None and chain["next"] == inp["cursor"]:
                chain["threads"].extend(threads)
                chain["next"] = cursor
                chain["at"] = at
            return
        self.chain = {"threads": list(threads), "next": cursor, "at": at}


def lift(transcripts, artifact_id: str, *, reader_agent_id: Optional[str] = None) -> model.Lifted:
    """The main-loop reads of `artifact_id` found in `transcripts`, in order; raises HarvestError when partial or absent.

    `reader_agent_id` admits sidechain records whose `agentId` equals it; every other sidechain stays skipped.
    """
    paths = [Path(p) for p in transcripts]
    if not paths:
        raise model.HarvestError(model.NO_TRANSCRIPT, "no transcript given")
    state = _State(artifact_id)
    opened = False
    for path in paths:
        pending: dict = {}
        try:
            handle = path.open("rb")
        except OSError:
            continue
        opened = True
        directory = path.parent
        with handle:
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                has_call = b"Artifact" in line and b'"tool_use"' in line
                if not has_call and not (pending and any(tid in line for tid in pending)):
                    continue
                entry = _loads(line)
                if not isinstance(entry, dict):
                    continue
                if entry.get("isSidechain") is True and not (
                    reader_agent_id is not None and entry.get("agentId") == reader_agent_id
                ):
                    continue
                message = entry.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                at = str(entry.get("timestamp", ""))
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("name") in _TOOL_NAMES:
                        inp = block.get("input")
                        inp = inp if isinstance(inp, dict) else {}
                        pending[str(block.get("id", "")).encode("utf-8")] = {
                            "name": block["name"],
                            "input": inp,
                            "by_input": _input_binds(inp, artifact_id),
                        }
                    elif block.get("type") == "tool_result":
                        call = pending.pop(str(block.get("tool_use_id", "")).encode("utf-8"), None)
                        if call is not None:
                            _on_result(state, call, block, entry, at, directory)
    if not opened:
        raise model.HarvestError(model.NO_TRANSCRIPT, "no transcript could be opened")
    return _finish(state)


def _on_result(state: _State, call: dict, block: dict, entry: dict, at: str, directory: Path) -> None:
    result = entry.get("toolUseResult")
    text = _text_of(block.get("content"))
    if block.get("is_error") is True:
        if call["by_input"] and _is_read_call(call["name"], call["input"]):
            state.bound(entry, directory)
            if not text and isinstance(result, str):
                text = result
            first = text.lstrip().split("\n", 1)[0]
            state.error = model.ReadError(kind=model.classify_error(text), first_line=first, at=at)
        return
    if not isinstance(result, dict):
        return
    for key in ("files_read", "file_read", "file_list"):
        payload = result.get(key)
        if isinstance(payload, dict):
            break
    else:
        payload = None
        key = None
    result_url = payload.get("url") if payload else result.get("url")
    by_result = isinstance(result_url, str) and model.artifact_id_from_url(result_url) == state.artifact_id
    if not (call["by_input"] or by_result):
        return
    if key is None and not isinstance(result.get("threads"), list):
        return
    state.bound(entry, directory)
    state.error = None
    if key == "files_read":
        files = payload.get("files")
        state.on_files(payload, files if isinstance(files, list) else [], at, directory)
    elif key == "file_read":
        state.on_files(payload, [payload], at, directory)
    elif key == "file_list":
        state.on_listing(payload, at)
    else:
        state.on_comments(call, result, text, at)


def _finish(state: _State) -> model.Lifted:
    artifact_read = None
    frames = state.reads.get("frames")
    if frames:
        info = state.reads["info"]
        listing = state.listing
        kind = (listing["type"] if listing and listing["type"] else info["type"]) or ""
        artifact_read = model.ArtifactRead(
            url=info["url"],
            artifact_id=state.artifact_id,
            title=info["title"],
            type=kind,
            version=state.version,
            frames=tuple(frames.values()),
            read_at=info["at"],
        )
    if state.listing and state.listing["paths"]:
        missing = [p for p in state.listing["paths"] if p not in (frames or {})]
        if missing:
            raise model.HarvestError(
                model.FRAMES_READ_INCOMPLETE,
                f"{len(missing)} listed path(s) not read at version {state.version}: {missing[0]}",
            )
    comments = None
    chain = state.chain
    if chain is not None:
        if chain["next"] is not None:
            raise model.HarvestError(model.COMMENTS_READ_INCOMPLETE, "cursor chain does not end")
        threads, empty, first = [], 0, ""
        for entry in chain["threads"]:
            rendered = state.texts.get(entry.get("id")) if isinstance(entry, dict) else None
            items = entry.get("comments") if isinstance(entry, dict) else None
            if isinstance(items, list):
                filled = []
                fits = rendered is not None and len(rendered) == len(items)
                for i, item in enumerate(items):
                    if isinstance(item, dict) and not item.get("text") and fits and rendered[i]:
                        item = {**item, "text": rendered[i]}
                    if not (isinstance(item, dict) and item.get("text")):
                        empty += 1
                        first = first or str(entry.get("id"))
                    filled.append(item)
                entry = {**entry, "comments": filled}
            threads.append(entry)
        if empty:
            raise model.HarvestError(
                model.COMMENTS_TEXT_MISSING, f"{empty} comment(s) with no text; first in thread {first}"
            )
        comments = model.CommentsRead(
            threads=threads, rendered_text="\n".join(state.rendered), read_at=chain["at"]
        )
    elif state.comments_seen and state.error is None:
        raise model.HarvestError(model.COMMENTS_READ_INCOMPLETE, "no unfiltered comments read")
    version = model.VersionRead(version=state.version, read_at=state.version_at) if state.version else None
    if artifact_read is None and comments is None and version is None and state.error is None:
        raise model.HarvestError(model.NO_MAIN_LOOP_READ, f"no main-loop read of {state.artifact_id}")
    return model.Lifted(
        artifact_read=artifact_read,
        comments=comments,
        version=version,
        error=state.error,
        transcript_dir=state.read_dir or state.last_dir,
        session_id=state.session_id,
    )
