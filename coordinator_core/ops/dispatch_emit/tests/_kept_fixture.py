"""Shared fixtures for kept-work hash-mode tests: a core.autocrlf repo holding a CRLF file, and a held run."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import write_object
from coordinator_core.ops.dispatch_emit import ask_contract, halted_run, halted_withheld_index, kept_work

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def git(root: Path, *args: str, stdin: bytes | None = None) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=str(root),
        input=stdin,
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    return done.stdout.decode("utf-8").strip()


def crlf_repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    """``(root, rel, lf_committed_sha, crlf_raw_sha)``: ``core.autocrlf=true`` repo-locally, an LF file
    committed, then rewritten with CRLF bytes whose raw blob is persisted in the object store."""
    root = Path(tmp_path) / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "core.autocrlf", "true")
    rel = "docs/crlf.txt"
    lf = b"alpha\nbeta\n"
    crlf = b"alpha\r\nbeta\r\n"
    (root / "docs").mkdir()
    (root / rel).write_bytes(lf)
    git(root, "add", rel)
    git(root, "commit", "-q", "-m", "lf")
    (root / rel).write_bytes(crlf)
    committed = git(root, "hash-object", "--path", rel, "--stdin", stdin=crlf)
    raw = write_object(resolve_git_common_dir(root), b"blob", crlf)
    return root, rel, committed, raw


def hold_kept(root: Path, run_id: str, paths: Iterable[str], post: Iterable[str] = ()) -> Path:
    """A still-halted run ``run_id`` whose ``kept.json`` lists ``paths`` as one chunk and ``post`` as
    ``post_checkpoint``, both fingerprinted from the worktree; index entry and halted marker written."""
    paths, post = list(paths), list(post)
    record_dir = ask_contract.record_dir(root, run_id)
    record_dir.mkdir(parents=True, exist_ok=True)
    halted_run.write_halt_marker(record_dir, halted="refusal", sizing_rel=None)

    def prints(rels: list[str]) -> dict[str, str]:
        return {r: kept_work.fingerprint(root, r) or "0" * 40 for r in rels}

    kept_work.write(
        record_dir,
        {"c1": {"paths": prints(paths)}} if paths else {},
        None,
        prints(post) or None,
    )
    halted_withheld_index.add_run(root, run_id, paths + post)
    (halted_withheld_index._dir(root) / halted_withheld_index.SENTINEL).write_bytes(b"")
    return record_dir
