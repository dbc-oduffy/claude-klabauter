"""Shared transcript and git-repo builders for the kept-blob recovery tests (helper, not collected)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Dict, Iterable, Tuple

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def write_transcript(path: Path, records: Iterable[tuple]) -> Path:
    """Write JSONL. Records: ("use", id, name, input, ts), ("result", id, is_error), ("raw", dict)."""
    lines = []
    for rec in records:
        kind = rec[0]
        if kind == "use":
            _, tid, name, inp, ts = rec
            lines.append({
                "type": "assistant",
                "timestamp": ts,
                "message": {"role": "assistant", "content": [
                    {"type": "tool_use", "id": tid, "name": name, "input": inp}]},
            })
        elif kind == "result":
            _, tid, is_error = rec
            lines.append({
                "type": "user",
                "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid, "is_error": bool(is_error), "content": ""}]},
            })
        elif kind == "raw":
            lines.append(rec[1])
        else:
            raise ValueError(f"unknown record kind {kind!r}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in lines), encoding="utf-8")
    return path


def _git(repo: Path, *args: str, data: bytes | None = None, env: dict | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=True,
        env=env,
        creationflags=_NOWIN,
    ).stdout.decode().strip()


def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "core.autocrlf", "false")
    return repo


def commit_at(repo: Path, rel: str, data: bytes, when_iso: str) -> Tuple[str, str]:
    """Commit `data` at `rel` dated `when_iso`; returns (commit_sha, blob_sha)."""
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    env = {**os.environ, "GIT_COMMITTER_DATE": when_iso, "GIT_AUTHOR_DATE": when_iso}
    _git(repo, "add", rel, env=env)
    _git(repo, "commit", "-q", "-m", f"c {rel}", env=env)
    commit = _git(repo, "rev-parse", "HEAD")
    blob = _git(repo, "rev-parse", f"HEAD:{rel}")
    return commit, blob


def kept(repo: Path, run: str, paths: Dict[str, str]) -> None:
    """Write state/warp/<run>/kept.json in the shape test_doctor_kept_blobs._kept writes."""
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True)
    (d / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": paths}}, "post_checkpoint": {}}))
