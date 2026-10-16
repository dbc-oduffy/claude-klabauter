"""ts_import_closure detector: a committed TS/JS file may not import a first-party file the post-commit tree lacks.

Post-commit presence of a path = it is a gate path that is a file in the worktree, or it is in HEAD
and not a gate path. Specifiers resolve as relative paths or through the nearest tsconfig.json's
`compilerOptions.paths` (`baseUrl` honoured, `extends` not followed); everything else is external
and skipped. A committed file is parsed only when an added line looks like an import. Zero spawns.
Wholly absent targets (in neither HEAD nor the worktree) are not findings: generated or asset paths
would false-refuse. Not wired into `WIRED_DETECTORS`.
"""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from coordinator_core.authoring_leaks import HeadView, LeakFinding, added_lines

NAME = "ts_import_closure"
KIND = "ts_import_closure"

_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_JS_TO_TS = {".js": (".ts", ".tsx"), ".jsx": (".ts", ".tsx"), ".mjs": (".mts",), ".cjs": (".cts",)}

# `from "s"`, `import "s"`, `import("s")`, `require("s")`; a multi-line import matches on its `from` line.
_SPEC = re.compile(r"""\b(?:from|import|require)\s*\(?\s*(["'])([^"'\n]+)\1""")


def candidates(gate_paths: Sequence[str]) -> Iterable[str]:
    """Gate TS/JS paths: their HEAD text feeds the added-line prefilter."""
    return [p for p in gate_paths if p.endswith(_EXTS)]


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments outside strings, then trailing commas."""
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i : j + 1])
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


class _Aliases:
    """`(pattern, targets)` pairs from the nearest tsconfig.json, resolved against the repo root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._by_dir: Dict[str, Optional[Tuple[str, List[Tuple[str, List[str]]]]]] = {}

    def _load(self, directory: str) -> Optional[Tuple[str, List[Tuple[str, List[str]]]]]:
        if directory in self._by_dir:
            return self._by_dir[directory]
        found = None
        text = _read_text(self.root / directory / "tsconfig.json")
        if text is not None:
            try:
                opts = json.loads(_strip_jsonc(text)).get("compilerOptions") or {}
                base = posixpath.normpath(posixpath.join(directory, opts.get("baseUrl") or "."))
                paths = opts.get("paths") or {}
                found = (base, [(k, [t for t in v if isinstance(t, str)]) for k, v in paths.items() if isinstance(v, list)])
            except (ValueError, AttributeError, TypeError):
                found = None
        self._by_dir[directory] = found
        return found

    def for_file(self, rel: str) -> Optional[Tuple[str, List[Tuple[str, List[str]]]]]:
        """Aliases of the nearest tsconfig.json at or above `rel`'s directory (`.` = repo root)."""
        directory = posixpath.dirname(rel) or "."
        while True:
            if (self.root / directory / "tsconfig.json").is_file():
                return self._load(directory)
            if directory == ".":
                return None
            directory = posixpath.dirname(directory) or "."


def _alias_bases(spec: str, config: Tuple[str, List[Tuple[str, List[str]]]]) -> List[str]:
    base_dir, patterns = config
    out: List[str] = []
    for pattern, targets in patterns:
        if "*" in pattern:
            prefix, _, suffix = pattern.partition("*")
            if not (spec.startswith(prefix) and spec.endswith(suffix) and len(spec) >= len(prefix) + len(suffix)):
                continue
            star = spec[len(prefix) : len(spec) - len(suffix)]
            out.extend(posixpath.normpath(posixpath.join(base_dir, t.replace("*", star))) for t in targets)
        elif spec == pattern:
            out.extend(posixpath.normpath(posixpath.join(base_dir, t)) for t in targets)
    return out


def _bases(spec: str, rel: str, aliases: _Aliases) -> List[str]:
    if spec.startswith(("./", "../")):
        return [posixpath.normpath(posixpath.join(posixpath.dirname(rel), spec))]
    config = aliases.for_file(rel)
    return _alias_bases(spec, config) if config else []


def _target_files(base: str) -> List[str]:
    files = [base] + [base + e for e in _EXTS] + [f"{base}/index{e}" for e in _EXTS]
    stem, ext = posixpath.splitext(base)
    files.extend(stem + e for e in _JS_TO_TS.get(ext, ()))
    return files


def detect(root: Path, gate_paths: Sequence[str], head: HeadView) -> List[LeakFinding]:
    root = Path(root)
    gate = set(gate_paths)
    parsed: List[Tuple[str, int, List[str]]] = []
    aliases = _Aliases(root)
    for rel in sorted(candidates(gate)):
        wt_text = _read_text(root / rel)
        if wt_text is None:
            continue
        if not any(_SPEC.search(text) for _, text in added_lines(head.head_text(rel), wt_text)):
            continue
        for lineno, line in enumerate(wt_text.splitlines(), start=1):
            for m in _SPEC.finditer(line):
                spec = m.group(2)
                bases = [b for b in _bases(spec, rel, aliases) if b != ".." and not b.startswith("../")]
                if bases:
                    parsed.append((rel, lineno, [spec] + bases))
    if not parsed:
        return []
    needed = {f for _, _, sb in parsed for b in sb[1:] for f in _target_files(b)}
    view = HeadView(root, needed)

    def worktree_has(path: str) -> bool:
        return (root / path).is_file()

    def post_has(path: str) -> bool:
        return worktree_has(path) if path in gate else view.in_head(path)

    findings: List[LeakFinding] = []
    for rel, lineno, sb in parsed:
        spec, bases = sb[0], sb[1:]
        files = [f for b in bases for f in _target_files(b)]
        if any(post_has(f) for f in files):
            continue
        wt = next((f for f in files if worktree_has(f)), None)
        if wt is not None:
            reason, target = "uncommitted in the worktree", wt
        else:
            hd = next((f for f in files if view.in_head(f)), None)
            if hd is None:
                continue
            reason, target = "deleted by this commit", hd
        findings.append(LeakFinding(rel, lineno, KIND, f"imports {spec}; {target} is {reason}"))
    return findings
