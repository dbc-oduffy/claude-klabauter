"""In-process check that the group-EM watch-state files are ignored and untracked.

`watch_state_ignore(repo_root) -> (exit_code, text)`. Report-only: writes no `.gitignore`,
touches no index. Calling repo only. Tracked-ness is index membership (`read_index`) and wins
over ignore coverage. Ignore coverage is a plain root `.gitignore` read; only files that read
leaves uncovered are confirmed by ONE batched `git check-ignore --stdin`. Empty text means
healthy.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from coordinator_core.git.git_state import IndexParseError, read_index
from coordinator_core.win_portability import no_console_creationflags

WATCH_STATE_FILES: tuple[str, ...] = (
    "state/group-em-watch.json",
    "state/group-em-watch-parked.json",
    "state/group-em-watch-spool.jsonl",
)

_Rule = tuple[bool, bool, bool, "re.Pattern[str]"]  # negated, dir_only, anchored, regex


def _glob_regex(glob: str) -> "re.Pattern[str]":
    out: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 2)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : j]
                if body[:1] == "!":
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        elif c == "\\" and i + 1 < len(glob):
            i += 1
            out.append(re.escape(glob[i]))
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _parse_rules(text: str) -> list[_Rule]:
    rules: list[_Rule] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        line = re.sub(r"(?<!\\)\s+$", "", line)
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        line = line.lstrip("/")
        rules.append((negated, dir_only, anchored, _glob_regex(line)))
    return rules


def _verdict(rules: list[_Rule], rel: str, is_dir: bool) -> bool:
    """Last matching rule wins; no match is not ignored."""
    ignored = False
    for negated, dir_only, anchored, rx in rules:
        if dir_only and not is_dir:
            continue
        target = rel if anchored else rel.rsplit("/", 1)[-1]
        if rx.match(target):
            ignored = not negated
    return ignored


def _covered(rules: list[_Rule], path: str) -> bool:
    """An ignored parent directory ignores everything under it, whatever the file's own rules."""
    parts = path.split("/")
    for n in range(1, len(parts)):
        if _verdict(rules, "/".join(parts[:n]), True):
            return True
    return _verdict(rules, path, False)


def _confirm_ignored(repo_root: Path, paths: list[str]) -> set[str]:
    """One batched `git check-ignore --stdin`; paths it echoes are ignored."""
    done = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=str(repo_root), input=("\n".join(paths) + "\n").encode(),
        capture_output=True, **no_console_creationflags(),
    )
    if done.returncode > 1:
        raise RuntimeError(f"git check-ignore failed ({done.returncode}): {done.stderr.decode(errors='replace').strip()}")
    return {ln.strip() for ln in done.stdout.decode(errors="replace").splitlines() if ln.strip()}


def watch_state_ignore(repo_root: Path) -> tuple[int, str]:
    root = Path(repo_root)
    try:
        tracked = read_index(root)
    except IndexParseError:
        return 0, "watch-state: tracked-ness could not be determined (index unreadable)"
    lines: list[str] = []
    candidates: list[str] = []
    for path in WATCH_STATE_FILES:
        if path in tracked:
            lines.append(f"{path}: tracked (fix: git rm --cached {path})")
        else:
            candidates.append(path)
    if candidates:
        try:
            rules = _parse_rules((root / ".gitignore").read_text(encoding="utf-8", errors="replace"))
        except OSError:
            rules = []
        uncovered = [p for p in candidates if not _covered(rules, p)]
        if uncovered:
            ignored = _confirm_ignored(root, uncovered)
            lines.extend(
                f"{p}: not ignored (fix: add to .gitignore)" for p in uncovered if p not in ignored
            )
    return 0, "\n".join(lines)
