"""coordinator_core.git.ignore_match -- in-process gitignore matching that reproduces which
untracked paths `git status --ignored=matching` reports as `!`, with no spawn.

`IgnoreMatcher(root).ignored(rel, is_dir)` answers one path; `.report(entries)` applies the
`--ignored=matching` collapse (an ignored directory is one `dir/` entry, git does not descend).
Every verdict is `Optional`: `None` means a shape this matcher does not reproduce, and the caller
must keep its `git` spawn. The error direction is always `None`, never a guess.

Precedence reproduced: in-tree `.gitignore` files (deepest directory wins, last match wins within
a file), then `$GIT_DIR/info/exclude` (common gitdir), then `core.excludesFile` (default
`$XDG_CONFIG_HOME/git/ignore`). A path under an ignored directory is ignored and cannot be
re-included. Declared gaps (`None`): config `include`/`includeif`, an `excludesFile` that is not
absolute or `~/`-rooted, POSIX bracket classes, a trailing backslash, an unreadable ignore file,
an ignored directory the caller says holds tracked files.

No process-lifetime cache: per-call file reads go through `call_io_scope.read_bytes`; verdict
memos live on the matcher instance.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from coordinator_core.git import call_io_scope
from coordinator_core.git import content_hash
from coordinator_core.git.git_dir import resolve_git_common_dir

__all__ = ["IgnoreMatcher"]

_BOOL_TRUE = {"true", "yes", "on", "1"}


class _Gap(Exception):
    """A shape the matcher does not reproduce; surfaces as `None` at the public API."""


def _read_text(path: Path) -> Optional[str]:
    """Decoded text of `path`; `None` when absent; `_Gap` on any other read failure."""
    try:
        data = call_io_scope.read_bytes(path)
    except OSError as exc:
        raise _Gap(f"unreadable {path}: {exc}") from exc
    if data is None:
        return None
    return data.decode("utf-8", errors="replace")


def _config_files(root: Path) -> List[Path]:
    """Config layers in git's precedence order (later wins), as `content_hash` reads them."""
    if os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system: Tuple[Path, ...] = ()
    elif os.environ.get("GIT_CONFIG_SYSTEM"):
        system = (Path(os.environ["GIT_CONFIG_SYSTEM"]),)
    else:
        system = content_hash._system_gitconfig_paths()
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [
        *system,
        Path(xdg) / "git" / "config",
        Path.home() / ".gitconfig",
        resolve_git_common_dir(root) / "config",
    ]


def _read_config(root: Path) -> Dict[str, str]:
    """`section.key -> value` over the layer stack, last wins; plain `[section]` only."""
    values: Dict[str, str] = {}
    for path in _config_files(root):
        text = _read_text(path)
        if text is None:
            continue
        section: Optional[str] = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line[0] in "#;":
                continue
            if line.startswith("["):
                match = re.match(r'\[\s*([A-Za-z0-9.-]+)\s*("[^"]*")?\s*\]', line)
                if not match:
                    raise _Gap("config: unparseable section header")
                name = match.group(1).lower()
                if name in ("include", "includeif"):
                    raise _Gap("config: include section")
                section = None if match.group(2) else name
                line = line[match.end():].strip()
                if not line or line[0] in "#;":
                    continue
            if section is None:
                continue
            key, eq, value = line.partition("=")
            values[f"{section}.{key.strip().lower()}"] = value.strip().strip('"') if eq else "true"
    return values


def _bracket(pattern: str, i: int) -> Tuple[Optional[str], int]:
    """Regex for the `[...]` starting at `pattern[i]`, or `(None, len)` for an unclosed one."""
    j = i + 1
    negate = False
    if j < len(pattern) and pattern[j] in "!^":
        negate = True
        j += 1
    k = j
    if k < len(pattern) and pattern[k] == "]":
        k += 1
    while k < len(pattern) and pattern[k] != "]":
        if pattern[k] == "[" and pattern[k + 1:k + 2] == ":":
            raise _Gap("ignore: posix class")
        if pattern[k] == "\\":
            k += 1
        k += 1
    if k >= len(pattern):
        return None, len(pattern)
    body = re.sub(r"([\\^\[])", r"\\\1", pattern[j:k])
    return "[" + ("^" if negate else "") + body + "]", k + 1


def _compile(pattern: str, ignore_case: bool) -> "re.Pattern[str]":
    """gitignore wildmatch -> regex: `*`/`?` never cross `/`; `**` only as a whole segment."""
    out: List[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                segment_start = i == 0 or pattern[i - 1] == "/"
                after = pattern[i + 2:i + 3]
                if segment_start and after == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if segment_start and after == "":
                    out.append(".*")
                    i += 2
                    continue
                while i < n and pattern[i] == "*":
                    i += 1
                out.append("[^/]*")
                continue
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            regex, nxt = _bracket(pattern, i)
            if regex is None:
                out.append(re.escape("["))
                i += 1
            else:
                out.append(regex)
                i = nxt
        elif c == "\\":
            if i + 1 >= n:
                raise _Gap("ignore: trailing backslash")
            out.append(re.escape(pattern[i + 1]))
            i += 2
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out), re.IGNORECASE if ignore_case else 0)


class _Rules:
    """The patterns of one ignore file; `base` is its directory relative to the root."""

    def __init__(self, text: str, base: str, ignore_case: bool) -> None:
        self.base = base
        self.rules: List[Tuple[bool, bool, bool, "re.Pattern[str]"]] = []
        for raw in text.split("\n"):
            line = raw.rstrip("\r")
            if not line or line.startswith("#"):
                continue
            while line.endswith(" ") and not line.endswith("\\ "):
                line = line[:-1]
            negate = False
            if line.startswith("!"):
                negate = True
                line = line[1:]
            elif line.startswith("\\!") or line.startswith("\\#"):
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line.rstrip("/")
            if not line:
                continue
            anchored = "/" in line
            line = line.lstrip("/")
            if not line:
                continue
            self.rules.append((negate, dir_only, anchored, _compile(line, ignore_case)))

    def verdict(self, rel: str, is_dir: bool) -> Optional[bool]:
        """Last matching rule: True ignored, False re-included, None no rule matched."""
        if self.base:
            if not rel.startswith(self.base + "/"):
                return None
            sub = rel[len(self.base) + 1:]
        else:
            sub = rel
        name = sub.rpartition("/")[2]
        result: Optional[bool] = None
        for negate, dir_only, anchored, regex in self.rules:
            if dir_only and not is_dir:
                continue
            if regex.fullmatch(sub if anchored else name):
                result = not negate
        return result


class IgnoreMatcher:
    """Ignore verdicts for paths under `root`, reproducing git's exclude stack."""

    def __init__(self, root: Path, ignore_case: Optional[bool] = None) -> None:
        self.root = Path(root)
        self._ignore_case = ignore_case
        self._low: Optional[List[_Rules]] = None
        self._dir_files: Dict[str, Optional[_Rules]] = {}
        self._memo: Dict[Tuple[str, bool], bool] = {}

    def _setup(self) -> List[_Rules]:
        if self._low is not None:
            return self._low
        config = _read_config(self.root)
        if self._ignore_case is None:
            self._ignore_case = config.get("core.ignorecase", "false").lower() in _BOOL_TRUE
        excludes = config.get("core.excludesfile")
        if excludes:
            if excludes.startswith("~/"):
                excludes_path = Path.home() / excludes[2:]
            elif Path(excludes).is_absolute():
                excludes_path = Path(excludes)
            else:
                raise _Gap("config: core.excludesFile is not absolute")
        else:
            xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
            excludes_path = Path(xdg) / "git" / "ignore"
        low: List[_Rules] = []
        for path in (excludes_path, resolve_git_common_dir(self.root) / "info" / "exclude"):
            text = _read_text(path)
            if text is not None:
                low.append(_Rules(text, "", bool(self._ignore_case)))
        self._low = low
        return low

    def _dir_rules(self, directory: str) -> Optional[_Rules]:
        if directory not in self._dir_files:
            text = _read_text(self.root / directory / ".gitignore" if directory else self.root / ".gitignore")
            self._dir_files[directory] = (
                None if text is None else _Rules(text, directory, bool(self._ignore_case))
            )
        return self._dir_files[directory]

    def _own_verdict(self, rel: str, is_dir: bool) -> bool:
        """Does a rule ignore exactly `rel`, ignoring its ancestors."""
        key = (rel, is_dir)
        if key in self._memo:
            return self._memo[key]
        low = self._setup()
        parts = rel.split("/")
        result = False
        found = False
        for k in range(len(parts) - 1, -1, -1):
            rules = self._dir_rules("/".join(parts[:k]))
            if rules is None:
                continue
            verdict = rules.verdict(rel, is_dir)
            if verdict is not None:
                result, found = verdict, True
                break
        if not found:
            for rules in reversed(low):
                verdict = rules.verdict(rel, is_dir)
                if verdict is not None:
                    result = verdict
                    break
        self._memo[key] = result
        return result

    def _topmost_ignored(self, rel: str, is_dir: bool) -> Optional[str]:
        """The shallowest ignored ancestor directory of `rel`, else `rel` itself when it is
        ignored, else `None`; ancestors win because git never descends into them."""
        parts = rel.split("/")
        for k in range(1, len(parts)):
            ancestor = "/".join(parts[:k])
            if self._own_verdict(ancestor, True):
                return ancestor
        return rel if self._own_verdict(rel, is_dir) else None

    def ignored(self, rel: str, is_dir: bool) -> Optional[bool]:
        """Is the untracked path `rel` (`/`-separated, root-relative) ignored; `None` on a gap."""
        try:
            return self._topmost_ignored(rel, is_dir) is not None
        except _Gap:
            return None

    def report(
        self,
        entries: Iterable[Tuple[str, bool]],
        tracked_dirs: Iterable[str] = (),
    ) -> Optional[List[str]]:
        """The `!` records `--ignored=matching` reports for untracked `(rel, is_dir)` entries.

        An entry under an ignored directory collapses to that directory as `dir/`; an ignored
        file or directory reports as itself (`dir/` for a directory); a non-ignored entry
        reports nothing. Order of first appearance, deduplicated. `tracked_dirs` names
        directories holding tracked files: git reports no `dir/` collapse for those, so an
        ignored one returns `None`. Any gap returns `None` for the whole report.
        """
        tracked = set(tracked_dirs)
        out: Dict[str, None] = {}
        try:
            for rel, is_dir in entries:
                top = self._topmost_ignored(rel, is_dir)
                if top is None:
                    continue
                collapsed_dir = is_dir or top != rel
                if collapsed_dir and top in tracked:
                    return None
                out[top + "/" if collapsed_dir else top] = None
        except _Gap:
            return None
        return list(out)
