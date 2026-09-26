
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Sequence

from coordinator_core.cartography._guard import path_guard

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
    return _EXTENSION_LANG.get(path.suffix.lower(), "unknown")


def _loc_for(path: Path) -> Optional[int]:
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
