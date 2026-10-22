"""Repo-declared comment-only verifier for non-`.py` blob drift in `dispatch.terminal_commit`.

A repo declares, in `coordinator.local.md` frontmatter, an argv and the path suffixes it covers.
The contract is `docs/reference/comment-only-verifier-contract.md`.

Invariant: anything but exit 0 from a trusted declared verifier is real drift.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Collection, Optional, Sequence

DECL_KEY = "comment_only_verifier"
SUFFIX_KEY = "comment_only_verifier_suffixes"
_VERIFIER_TIMEOUT_S = 0.5


@dataclass(frozen=True)
class VerifierDecl:
    """A repo's declared verifier: `argv` as given, `suffixes` lower-cased with leading dot."""

    argv: tuple[str, ...]
    suffixes: frozenset[str]


def declared_verifier(worktree_root: Path) -> Optional[VerifierDecl]:
    """The declaration in `worktree_root/coordinator.local.md`, or None when absent, empty or
    malformed. Reads the root only, never walks up; caches nothing."""
    from coordinator_core.ops.doc_registry import _parse_flow_list
    from coordinator_core.resolve_validation_cmd import cs_read_local_md_key

    root = str(worktree_root)
    argv_raw = cs_read_local_md_key(root, DECL_KEY)
    suffix_raw = cs_read_local_md_key(root, SUFFIX_KEY)
    if not argv_raw.strip() or not suffix_raw.strip():
        return None
    if not (argv_raw.strip().startswith("[") and suffix_raw.strip().startswith("[")):
        return None
    argv = _parse_flow_list(argv_raw)
    suffixes = _parse_flow_list(suffix_raw)
    if not argv or not suffixes:
        return None
    if any(not t.strip() for t in argv) or any(not s.startswith(".") or len(s) < 2 for s in suffixes):
        return None
    return VerifierDecl(argv=tuple(argv), suffixes=frozenset(s.lower() for s in suffixes))


def covers(decl: VerifierDecl, rel_path: str) -> bool:
    """True iff the lower-cased suffix of `rel_path` is in `decl.suffixes`."""
    return Path(rel_path).suffix.lower() in decl.suffixes


def verify_pairs(
    worktree_root: Path,
    decl: VerifierDecl,
    pairs: Sequence[tuple[str, bytes, bytes]],
    untrusted_paths: Collection[str],
) -> bool:
    """True iff the declared verifier, run once over `pairs` (path, before, after), exits 0.
    False on every other outcome, including a verifier named in `untrusted_paths`.

    Spawns at most once, and never on a False decided before step 4. A repo file naming the
    verifier must equal its HEAD blob (spawn-free); `coordinator.local.md` must equal HEAD only
    when HEAD tracks it."""
    if not pairs:
        return False
    root = Path(worktree_root)
    untrusted = {str(p).replace("\\", "/") for p in untrusted_paths}
    if "coordinator.local.md" in untrusted:
        return False

    repo_files = _repo_file_tokens(root, decl.argv)
    if any(rel in untrusted for rel in repo_files):
        return False
    if not _clean_at_head(root, repo_files, tracked_only=("coordinator.local.md",)):
        return False

    exe = shutil.which(decl.argv[0])
    if exe is None:
        cand = root / decl.argv[0]
        if not cand.is_file():
            return False
        exe = str(cand)
    if Path(exe).suffix.lower() in (".cmd", ".bat"):
        return False

    try:
        payload = {
            "version": 1,
            "pairs": [
                {"path": path, "before": _fold(before), "after": _fold(after)}
                for path, before, after in pairs
            ],
        }
        data = json.dumps(payload).encode("utf-8")
    except UnicodeDecodeError:
        return False

    try:
        proc = subprocess.run(
            [exe, *decl.argv[1:]],
            cwd=str(root),
            input=data,
            capture_output=True,
            timeout=_VERIFIER_TIMEOUT_S,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _fold(raw: bytes) -> str:
    return raw.decode("utf-8").replace("\r\n", "\n")


def _repo_file_tokens(root: Path, argv: Sequence[str]) -> list[str]:
    """Forward-slashed repo-relative paths of argv tokens that are existing files under `root`."""
    out: list[str] = []
    try:
        base = root.resolve()
    except OSError:
        return out
    for tok in argv:
        try:
            cand = (root / tok)
            if not cand.is_file():
                continue
            out.append(cand.resolve().relative_to(base).as_posix())
        except (OSError, ValueError):
            continue
    return out


def _blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest()


def _clean_at_head(root: Path, argv_files: Sequence[str], tracked_only: Sequence[str]) -> bool:
    """True iff every `argv_files` entry equals its HEAD blob and each `tracked_only` file that
    HEAD tracks equals its HEAD blob. Untracked argv files and unreadable files fail closed."""
    from coordinator_core.git.git_state import head_blobs

    wanted = list(dict.fromkeys([*argv_files, *tracked_only]))
    try:
        heads = head_blobs(root, wanted)
    except Exception:
        return False
    for rel in wanted:
        entry = heads.get(rel)
        if entry is None:
            if rel in argv_files:
                return False
            continue
        try:
            raw = (root / rel).read_bytes()
        except OSError:
            return False
        if _blob_sha(raw) != entry[1]:
            return False
    return True
