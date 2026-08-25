"""
coordinator_core.cartography.tree — tracked-file tree + per-file {lang, loc}.

Purpose: deterministic replacement for the agentic "read every file to build a
skeleton" pass (docs/problems/2026-07-12-architecture-survey-dogfood-observations.md
§ 6.1). Walks `git ls-files` under a caller-supplied ``target_root`` and, for each
tracked file, computes a cheap {lang, loc} pair: language from the file extension
(best-effort, extensionless/unknown files map to ``"unknown"``) and line count via
a plain newline count (binary-safe: undecodable files get ``loc: None`` rather than
raising).

Load-bearing regression (AC5): this primitive must reproduce
``coordinator_core/authz/classification.py`` = 932 lines — the atlas's inflated
1,462-line figure (state/scratch/deep-architecture-survey dogfood run,
2026-07-12) was a Haiku miscount this deterministic walk corrects.

Spec backlink: pln-makima-cartography-substrate-a-26eb2e § C2

Negative-spec:
  - Does NOT classify files into architectural "systems" — that's
    ``cartography.file_index``'s job; this module only emits {path, lang, loc}.
  - Does NOT shell out for anything but ``git ls-files`` (read-only; DR-208's
    "all subprocess calls are read-only git queries" precedent, coverage.gate).
  - Does NOT walk untracked files — `git ls-files` is the tracked-file boundary
    by design (mirrors what a fresh clone/checkout actually contains).
  - Does NOT filter by post-hoc classification (system/language) — the
    optional ``scope`` param is a set of git pathspecs applied at
    `git ls-files` enumeration time, not a filter over a built result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.cartography._guard import path_guard

#: Extension -> language label. Best-effort; unmapped extensions fall back to
#: "unknown" rather than raising (a repo tree may contain arbitrary file types).
_EXTENSION_LANG: dict = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".sh": "shell",
    ".bash": "shell",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".html": "html",
    ".css": "css",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".sql": "sql",
    ".txt": "text",
}


def _lang_for(path: Path) -> str:
    """Return the language label for `path` based on its suffix, or "unknown"."""
    return _EXTENSION_LANG.get(path.suffix.lower(), "unknown")


def _loc_for(path: Path) -> Optional[int]:
    """Return the newline-count LOC for `path`, or None if it can't be decoded as text.

    Uses a plain "\\n" count over the raw bytes decoded as UTF-8 (errors="strict")
    plus one if the file has trailing content with no final newline — equivalent to
    `wc -l` semantics for a well-formed text file, and fails closed (None, not a
    raised exception) for binary/undecodable content so the tree walk never aborts
    on a single unreadable file.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text == "":
        return 0
    lines = text.count("\n")
    if not text.endswith("\n"):
        lines += 1
    return lines


def list_tracked_files(
    target_root: str | Path, scope: Optional[Sequence[str]] = None
) -> list:
    """Return the sorted list of git-tracked file paths (repo-relative) under `target_root`.

    Shells out to `git ls-files` (read-only git query — DR-208 / coverage.gate
    precedent). Raises RuntimeError if `target_root` is not inside a git worktree
    or the git invocation otherwise fails.

    Args:
        target_root: root of the tracked-file tree (must be inside a git worktree).
        scope: optional sequence of git pathspecs (repo-relative paths or
            directory prefixes) to narrow the walk. Passed straight through
            to `git ls-files -- <scope...>`, so filtering happens inside
            git's own enumeration rather than after building the full list.
            Absent/empty (the default), behaviour is unchanged from before
            this parameter existed — the full tracked-file set.
    """
    root = path_guard(target_root, ".")
    cmd = ["git", "ls-files"]
    if scope:
        cmd.append("--")
        cmd.extend(scope)
    from coordinator_core.win_portability import no_console_creationflags

    result = subprocess.run(
        cmd,
        cwd=root,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed under {root!r} (exit {result.returncode}): {result.stderr.strip()}"
        )
    return sorted(line for line in result.stdout.splitlines() if line)


def build_tree(target_root: str | Path, scope: Optional[Sequence[str]] = None) -> dict:
    """Build the cartography tree: {path: {lang, loc}} for every tracked file.

    Args:
        target_root: root of the tracked-file tree (must be inside a git worktree).
        scope: optional sequence of git pathspecs narrowing the walk to a
            subset of the tracked-file tree — see `list_tracked_files`.
            Absent/empty, output is byte-identical to the pre-scope-param
            behaviour (the full tracked-file set).

    Returns:
        {
            "target_root": str(resolved target_root),
            "files": {
                "<repo-relative path>": {"lang": str, "loc": int | None},
                ...
            },
            "file_count": int,
        }
    """
    root = path_guard(target_root, ".")
    tracked = list_tracked_files(root, scope=scope)

    files = {}
    for relpath in tracked:
        candidate = root / relpath
        files[relpath] = {
            "lang": _lang_for(Path(relpath)),
            "loc": _loc_for(candidate),
        }

    return {
        "target_root": str(root),
        "files": files,
        "file_count": len(files),
    }
