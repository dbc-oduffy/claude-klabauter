"""coordinator_core.git.scoped_worktree_status -- the in-process stand-in for the
`git status --porcelain=v2 --untracked-files=all --ignored=matching` spawn that
`dispatch_emit.worktree_snapshot.worktree_snapshot` issues, plus the cheap gate
that decides whether an in-process answer may be attempted at all.

Contract: `scoped_worktree_status` returns `(changed, ignored)` equal to what
`_worktree_snapshot` parses out of the spawn for the same arguments, or `None`
when ANY gap is hit; `None` means the caller spawns, argv unchanged, for the
whole call. A snapshot is never part in-process and part spawned.

`gap_reason` is the repo-wide and request-shaped pre-gate. It reads only the
index header, gitdir entry names, config files and env vars, and it names the
gap-register row it hit as `row N: ...`. Per-entry and per-path gaps (rows 6,
7 flags on an entry, 8 gitlinks, 9, 10, 13 symlinks, 15) need the index walk
and stay with the scoped changed-set reader.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from coordinator_core.git import call_io_scope
from coordinator_core.git.content_hash import _system_gitconfig_paths
from coordinator_core.git.git_dir import resolve_git_common_dir, resolve_git_dir

__all__ = ["gap_reason", "scoped_worktree_status"]

_GATED_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL",
)
_INERT_CONFIG_SECTIONS = frozenset({"help", "web", "browser"})
_GLOB_META = "*?["
_IS_WINDOWS = os.name == "nt"
_BOOL_TRUE =("true", "yes", "on", "1")

_Config = Dict[Tuple[str, str], str]


def scoped_worktree_status(
    root: Path, literals: list, roots: list, scope_specs: list
) -> Optional[Tuple[set, list]]:
    """`(changed, ignored)` for the request, or `None` to make the caller spawn.

    `changed` is tracked paths differing from HEAD or the index plus untracked
    non-ignored files; `ignored` is the `--ignored=matching` records (an
    ignored directory is one `dir/` entry). Any gap in the gate, the changed-set
    reader or the matcher returns `None` for the whole call."""
    if gap_reason(root, literals, roots, scope_specs) is not None:
        return None
    from coordinator_core.git.ignore_match import IgnoreMatcher
    from coordinator_core.git.scoped_changed import scoped_changed_detail

    all_roots = [r.rstrip("/") for r in roots]
    glob_dirs: List[str] = []
    for spec in scope_specs:
        if spec.startswith(":(glob)"):
            body = spec[len(":(glob)"):]
            glob_dirs.append("" if body == "*" else _unescape_glob(body[:-2]) or "")
        else:
            all_roots.append(spec[len(":(literal)"):].rstrip("/"))

    matcher = IgnoreMatcher(root)
    blocked = [False]

    def descend(rel: str) -> bool:
        verdict = matcher.ignored(rel, True)
        if verdict is None:
            blocked[0] = True
            return False
        return not verdict

    detail = scoped_changed_detail(root, literals, all_roots, glob_dirs, descend=descend)
    if detail is None or blocked[0]:
        return None
    entries = [(c[:-1], True) if c.endswith("/") else (c, False) for c in detail.candidates]
    ignored = matcher.report(entries, detail.tracked_dirs)
    if ignored is None:
        return None
    tops = [e[:-1] for e in ignored if e.endswith("/")]
    if tops:
        detail = scoped_changed_detail(
            root, literals, all_roots, glob_dirs, extra_prefixes=tops, descend=descend
        )
        if detail is None or blocked[0]:
            return None
        ignored = matcher.report(entries, detail.tracked_dirs)
        if ignored is None:
            return None
    changed = set(detail.changed)
    for rel, is_dir in entries:
        if is_dir:
            continue
        verdict = matcher.ignored(rel, False)
        if verdict is None:
            return None
        if not verdict:
            changed.add(rel)
    return changed, ignored


def gap_reason(
    root: Path, literals: list, roots: list, scope_specs: list
) -> Optional[str]:
    """Name the first repo-wide or request-shaped gap, else `None` (may proceed).

    Spawns nothing. Ordered cheapest first: env, request shape, path bytes,
    gitdir and index header, then config."""
    root = Path(root)
    for name in _GATED_ENV:
        if name != "GIT_CONFIG_COUNT" and os.environ.get(name):
            return f"row 16: {name} is set"
    injected = _injected_config_gap()
    if injected is not None:
        return injected

    shape = _request_gap(literals, roots, scope_specs)
    if shape is not None:
        return shape

    if (root / ".gitmodules").exists():
        return "row 8: .gitmodules present (submodules)"
    for rel in _dirs_to_probe(literals, roots, scope_specs):
        if os.path.lexists(root / rel / ".git"):
            return f"row 8: nested .git under {rel}"

    git_dir = resolve_git_dir(root)
    common_dir = resolve_git_common_dir(root)
    versioned = _index_gap(git_dir)
    if versioned is not None:
        return versioned
    shared = _sharedindex_gap(git_dir, common_dir)
    if shared is not None:
        return shared

    return _config_gap(root, common_dir)


def _injected_config_gap() -> Optional[str]:
    """`GIT_CONFIG_COUNT` injection gates unless every key sits in a section that cannot
    change status, ignore or index behaviour (the test-suite browser mitigation)."""
    raw = os.environ.get("GIT_CONFIG_COUNT")
    if not raw:
        return None
    try:
        count = int(raw)
    except ValueError:
        return "row 16: GIT_CONFIG_COUNT is set"
    for i in range(count):
        key = os.environ.get(f"GIT_CONFIG_KEY_{i}", "")
        if key.split(".", 1)[0].lower() not in _INERT_CONFIG_SECTIONS:
            return "row 16: GIT_CONFIG_COUNT is set"
    return None


def _request_gap(literals: list, roots: list, scope_specs: list) -> Optional[str]:
    for path in list(literals) + [r.rstrip("/") for r in roots]:
        bad = _path_gap(path)
        if bad is not None:
            return bad
    for spec in scope_specs:
        if spec.startswith(":(glob)"):
            body = spec[len(":(glob)"):]
            if body == "*":
                continue
            if not body.endswith("/*"):
                return f"row 14: unsupported glob scope spec {spec!r}"
            literal = _unescape_glob(body[:-2])
            if literal is None:
                return f"row 14: unsupported glob scope spec {spec!r}"
            bad = _path_gap(literal)
            if bad is not None:
                return bad
        elif spec.startswith(":(literal)"):
            bad = _path_gap(spec[len(":(literal)"):].rstrip("/"))
            if bad is not None:
                return bad
        else:
            return f"row 14: unsupported scope spec {spec!r}"
    return None


def _path_gap(path: str) -> Optional[str]:
    if not path:
        return "row 14: empty path in request"
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return f"row 17: non-UTF-8 path bytes in {path!r}"
    parts = path.split("/")
    if (
        path.startswith("/") or "\\" in path or ":" in parts[0]
        or any(p in ("", ".", "..") for p in parts)
    ):
        return f"row 14: non-canonical path {path!r}"
    return None


def _unescape_glob(text: str) -> Optional[str]:
    """The literal directory a `_glob_literal`-escaped glob prefix names, or
    `None` when it carries an unescaped wildcard."""
    out: List[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                return None
            out.append(text[i + 1])
            i += 2
            continue
        if ch in _GLOB_META:
            return None
        out.append(ch)
        i += 1
    return "".join(out)


def _dirs_to_probe(literals: list, roots: list, scope_specs: list) -> List[str]:
    dirs: Dict[str, None] = {}
    for r in roots:
        dirs[r.rstrip("/")] = None
    for p in literals:
        parent = p.rpartition("/")[0]
        if parent:
            dirs[parent] = None
    for spec in scope_specs:
        if spec.startswith(":(glob)") and spec.endswith("/*"):
            literal = _unescape_glob(spec[len(":(glob)"):-2])
            if literal:
                dirs[literal] = None
    return list(dirs)


def _index_gap(git_dir: Path) -> Optional[str]:
    try:
        with open(git_dir / "index", "rb") as fh:
            header = fh.read(12)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"row 11: index unreadable ({exc.__class__.__name__})"
    if len(header) < 12 or header[:4] != b"DIRC":
        return "row 11: index header unreadable"
    version = int.from_bytes(header[4:8], "big")
    if version not in (2, 3):
        return f"row 11: index version {version}"
    return None


def _sharedindex_gap(git_dir: Path, common_dir: Path) -> Optional[str]:
    for directory in dict.fromkeys((git_dir, common_dir)):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith("sharedindex."):
                        return "row 11: split index (sharedindex file present)"
        except OSError:
            continue
    return None


def _config_gap(root: Path, common_dir: Path) -> Optional[str]:
    layers = _config_layers(common_dir)
    merged: _Config = {}
    for path in layers:
        text = _read_text(path)
        if text is None:
            continue
        parsed = _parse_config(text)
        if isinstance(parsed, str):
            return f"row 3: {path} {parsed}"
        merged.update(parsed)

    if ("extensions", "worktreeconfig") in merged and _is_true(
        merged[("extensions", "worktreeconfig")]
    ):
        return "row 16: extensions.worktreeConfig set"
    for key, label in (
        (("core", "splitindex"), "split index"),
        (("index", "sparse"), "sparse index"),
        (("core", "sparsecheckout"), "sparse checkout"),
    ):
        if key in merged and _is_true(merged[key]):
            return f"row {'7' if key[1] == 'sparsecheckout' else '11'}: {label} configured"
    if os.path.exists(common_dir / "info" / "sparse-checkout"):
        return "row 7: sparse-checkout file present (skip-worktree entries)"
    if ("core", "filemode") in merged and _is_true(merged[("core", "filemode")]) and _IS_WINDOWS:
        return "row 13: core.filemode=true on Windows"

    excludes = merged.get(("core", "excludesfile"))
    if excludes is not None and not _excludes_parseable(excludes):
        return f"row 3: core.excludesFile value {excludes!r} is not an absolute or ~/ path"
    return None


def _config_layers(common_dir: Path) -> List[Path]:
    if os.environ.get("GIT_CONFIG_NOSYSTEM"):
        system: Tuple[Path, ...] = ()
    elif os.environ.get("GIT_CONFIG_SYSTEM"):
        system = (Path(os.environ["GIT_CONFIG_SYSTEM"]),)
    else:
        system = _system_gitconfig_paths()
    xdg_base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return [
        *system,
        Path(xdg_base) / "git" / "config",
        Path.home() / ".gitconfig",
        common_dir / "config",
    ]


def _read_text(path: Path) -> Optional[str]:
    try:
        data = call_io_scope.read_bytes(path)
    except OSError:
        return None
    return None if data is None else data.decode("utf-8", errors="replace")


def _parse_config(text: str):
    """`{(section, key): value}` last-wins, or a reason string naming why the
    layer cannot be read faithfully (includes, continuation lines)."""
    out: _Config = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if line.endswith("\\"):
            return "has a continuation line"
        if line.startswith("["):
            header = line[1:].split("]", 1)[0].strip().lower()
            section = header.split(None, 1)[0] if header else ""
            if section in ("include", "includeif"):
                return "uses include/includeIf"
            continue
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if not sep:
            value = "true"
        else:
            value = _strip_inline_comment(value.strip())
        if not key or " " in key:
            continue
        out[(section, key)] = value
    return out


def _strip_inline_comment(value: str) -> str:
    quoted = False
    for i, ch in enumerate(value):
        if ch == '"':
            quoted = not quoted
        elif ch in "#;" and not quoted:
            return value[:i].rstrip()
    return value


def _is_true(value: str) -> bool:
    return value.strip().lower() in _BOOL_TRUE


def _excludes_parseable(value: str) -> bool:
    if not value or '"' in value or "%(" in value:
        return False
    if value.startswith("~/"):
        return True
    return os.path.isabs(value) or (len(value) > 2 and value[1] == ":" and value[2] in "/\\")
