"""coordinator_core.symbol_knowledge.workstate_index -- map symbols and files to the plans, DRs,
lessons and daily logs that cite them.

Purpose: one ``git ls-files -s -z`` gives every tracked path and blob oid; a cache beside the index
keeps ``doc oid -> extracted citations`` so only docs whose blob changed are read (one
``git cat-file --batch`` for those, none when nothing changed). Writes
``state/symbol-knowledge/workstate-index.json`` (symbol/file -> docs), the NDJSON edge batch
``workstate-anchors.ndjson`` (``workstate_anchor`` lines for example-retrieval-repo's edge-batch door) and the
cache.

Negative-spec:
  - Never writes into example-retrieval-repo's store and never runs SCIP: ``symbol_key`` is whatever the
    resolver (``anchors.resolve``, through example-retrieval-repo symbol tools) returns, else null.
  - A path cited by a record counts only when it is a tracked file; a symbol only when it resolves
    to a named def/class/constant of a tracked ``.py`` file (``cartography.symbols.build_symbols``)
    or a ``definitions.json`` key. Citations of record homes themselves (docs/plans, docs/decisions,
    state/, archive/) are not code anchors.
  - ``confidence`` is ``explicit`` only for a literal path or a dotted ``module.name`` citation; a
    bare name found in a file the same record cites is ``inferred``.
  - Daily logs appear in the JSON index only: the edge-batch ``record_kind`` has no log value. Plans
    contribute edge-batch lines only for a symbol or a cited line; a bare plan-to-file citation stays
    in the JSON index, and bare-name inference runs for decisions and lessons only, never plans.
  - Outputs are sorted and written atomically only when their bytes changed.
  - File symbol tables are read from the worktree and keyed by the indexed blob oid, so an unstaged
    edit shows in line numbers only after it is staged or committed.
"""

from __future__ import annotations

import json
import posixpath
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from coordinator_core.session.record_homes import home_rel
from coordinator_core.symbol_knowledge import sidecar
from coordinator_core.symbol_knowledge.contract import (
    DOC_KIND_DR,
    DOC_KIND_LESSON,
    DOC_KIND_LOG,
    DOC_KIND_PLAN,
)

PathLike = Union[str, Path]
Resolver = Callable[[str, str], Optional[str]]

_DIR = posixpath.dirname(sidecar.SIDECAR_REL)
INDEX_REL = f"{_DIR}/workstate-index.json"
ANCHORS_REL = f"{_DIR}/workstate-anchors.ndjson"
CACHE_REL = f"{_DIR}/workstate-cache.json"
PRODUCER = "claude-klabauter/code_intel.workstate_index@1"
SCHEMA_VERSION = 1

# (path prefix, WorkstateRef doc_kind, edge-batch record_kind or None, relation for an explicit edge)
DOC_ROOTS: "Tuple[Tuple[str, str, Optional[str], str], ...]" = (
    ("docs/plans/", DOC_KIND_PLAN, "plan", "implements"),
    ("docs/decisions/", DOC_KIND_DR, "decision", "decides"),
    (home_rel("lessons") + "/", DOC_KIND_LESSON, "lesson", "cites"),
    ("archive/daily-summaries/", DOC_KIND_LOG, None, "mentions"),
)
_DOC_SUFFIXES = (".md", ".yaml", ".yml")
_DOC_PREFIXES = tuple(r[0] for r in DOC_ROOTS)
_NON_CODE_PREFIXES = ("docs/plans/", "docs/decisions/", "state/", "archive/", ".coordinator-local/")
MAX_RESOLVES_PER_RUN = 200
_MAX_REFS_PER_DOC = 400
_MAX_BLOB_BYTES = 2_000_000
_MAX_CITED_FILES = 25

_PATH_RE = re.compile(
    r"(?<![\w./~-])((?:[\w.\-]+/)+[\w.\-]+\.[A-Za-z0-9]{1,8})(?::(\d+)(?:-(\d+))?)?(?![\w/])"
)
_TICK_RE = re.compile(r"`([^`\n]{1,120})`")
_DOTTED_RE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")
_ID_RE = re.compile(r"^id:\s*[\"']?([^\"'\n#]+?)[\"']?\s*$", re.MULTILINE)
_LS_ENTRY_RE = re.compile(r"\d{6} (\w+) \d\t([^\0]+)\0")
_DEF_KEY_RE =re.compile(r"`([^`]+)`/(.+)$")


def _git(root: Path, *args: str, stdin: Optional[bytes] = None) -> bytes:
    proc = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args],
        input=stdin,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout


def _head_sha(root: Path) -> str:
    """HEAD's sha read from ``.git`` without a spawn; one ``rev-parse`` when ``.git`` is a file."""
    git_dir = root / ".git"
    try:
        if git_dir.is_dir():
            head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
            if not head.startswith("ref: "):
                return head
            ref = head[5:]
            try:
                return (git_dir / ref).read_text(encoding="utf-8").strip()
            except OSError:
                for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0]
    except OSError:
        pass
    return _git(root, "rev-parse", "HEAD").decode("ascii").strip()


def _tracked(root: Path) -> "Dict[str, str]":
    """Every tracked path -> blob oid, from one ``git ls-files -s -z``."""
    out = _git(root, "ls-files", "-s", "-z").decode("utf-8", "surrogateescape")
    return {path: oid for oid, path in _LS_ENTRY_RE.findall(out)}


def _doc_spec(path: str) -> "Optional[Tuple[str, Optional[str], str]]":
    if not path.startswith(_DOC_PREFIXES) or not path.endswith(_DOC_SUFFIXES):
        return None
    for prefix, doc_kind, record_kind, relation in DOC_ROOTS:
        if path.startswith(prefix):
            return doc_kind, record_kind, relation
    return None


def _read_blobs(root: Path, oids: "List[str]") -> "Dict[str, str]":
    """oid -> text for each oid, in one ``git cat-file --batch``."""
    if not oids:
        return {}
    data = _git(root, "cat-file", "--batch", stdin=("\n".join(oids) + "\n").encode("ascii"))
    out: "Dict[str, str]" = {}
    pos = 0
    while pos < len(data):
        eol = data.index(b"\n", pos)
        parts = data[pos:eol].split(b" ")
        if len(parts) != 3:
            pos = eol + 1
            continue
        size = int(parts[2])
        out[parts[0].decode("ascii")] = data[eol + 1 : eol + 1 + min(size, _MAX_BLOB_BYTES)].decode(
            "utf-8", "replace"
        )
        pos = eol + 1 + size + 1
    return out


def _slug(heading: str) -> str:
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", heading.strip().lower())).strip("-")


def _worth_keeping(token: str) -> bool:
    return "." in token or "_" in token or token[:1].isupper()


def extract_citations(text: str) -> "List[list]":
    """Citations in *text*, first occurrence of each: ``["p", path, section, line_start, line_end]``
    for a path-shaped token and ``["s", token, section]`` for a backticked dotted name."""
    refs: "List[list]" = []
    seen: "set[tuple]" = set()
    section: Optional[str] = None
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        heading = None if in_fence else _HEADING_RE.match(line)
        if heading:
            section = _slug(heading.group(1)) or None
            continue
        for m in _PATH_RE.finditer(line):
            key = ("p", m.group(1))
            if key not in seen and not m.group(1).startswith(_NON_CODE_PREFIXES):
                seen.add(key)
                ls = int(m.group(2)) if m.group(2) else None
                le = int(m.group(3)) if m.group(3) else None
                refs.append(["p", m.group(1), section, ls, le])
        if in_fence:
            continue
        for m in _TICK_RE.finditer(line):
            token = m.group(1).strip()
            if token.endswith("()"):
                token = token[:-2]
            if _DOTTED_RE.match(token) and _worth_keeping(token) and ("s", token) not in seen:
                seen.add(("s", token))
                refs.append(["s", token, section])
        if len(refs) >= _MAX_REFS_PER_DOC:
            break
    return refs[:_MAX_REFS_PER_DOC]


def _record_id(path: str, text: str) -> str:
    m = _ID_RE.search("\n".join(text.splitlines()[:40]))
    return m.group(1).strip() if m else posixpath.splitext(posixpath.basename(path))[0]


def _definition_keys(root: Path) -> "Dict[Tuple[str, str], str]":
    """(module dotted, qualname) -> definitions.json key."""
    out: "Dict[Tuple[str, str], str]" = {}
    for key in sidecar.read_sidecar(root):
        m = _DEF_KEY_RE.match(key)
        if not m:
            continue
        desc = m.group(2).rstrip(".").replace("()", "").replace("#", ".")
        if desc and "(" not in desc:
            out[(m.group(1), desc)] = key
    return out


def _module_of(path: str) -> str:
    stem = path[:-3] if path.endswith(".py") else path
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _file_names(root: Path, path: str) -> "Dict[str, int]":
    """qualname -> line for the functions, classes, methods and constants of one ``.py`` file."""
    from coordinator_core.cartography.symbols import build_symbols

    try:
        entry = build_symbols(root, [path])["files"][0]
    except Exception:
        return {}
    names: "Dict[str, int]" = {}
    for const in entry.get("constants", []):
        names[const["name"]] = const.get("lineno") or 0
    for fn in entry.get("functions", []):
        names[fn["name"]] = fn.get("lineno") or 0
    for cls in entry.get("classes", []):
        names[cls["name"]] = cls.get("lineno") or 0
        for meth in cls.get("methods", []):
            names[f"{cls['name']}.{meth['name']}"] = meth.get("lineno") or 0
    return names


def _default_resolver() -> "Optional[Resolver]":
    try:
        from coordinator_core.symbol_knowledge import anchors
    except ImportError:
        return None
    return getattr(anchors, "resolve", None)


def _load_cache(root: Path) -> "Dict[str, Any]":
    try:
        data = json.loads((root / CACHE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
        return {"v": SCHEMA_VERSION, "docs": {}, "files": {}, "scip": {}}
    for field in ("docs", "files", "scip"):
        if not isinstance(data.get(field), dict):
            data[field] = {}
    return data


def _write_if_changed(root: Path, rel: str, text: str) -> bool:
    from coordinator_core.atomic_replace import atomic_write_bytes

    path = root / rel
    blob = text.encode("utf-8")
    try:
        if path.read_bytes() == blob:
            return False
    except OSError:
        pass
    atomic_write_bytes(path, blob)
    return True


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def stale_decision_anchors(
    anchors: "Iterable[Mapping[str, Any]]", moved_symbol_keys: "Iterable[str]"
) -> "List[Mapping[str, Any]]":
    """Anchors from a decision or plan (``decides`` / ``implements``) whose ``symbol_key`` is in
    *moved_symbol_keys*, the keys whose body hash moved since the record was written."""
    moved = set(moved_symbol_keys)
    return [
        a
        for a in anchors
        if a.get("relation") in ("decides", "implements") and (a.get("anchor") or {}).get("symbol_key") in moved
    ]


def workstate_index(
    root: PathLike, resolver: "Optional[Resolver]" = None, now: "Optional[str]" = None
) -> "Dict[str, Any]":
    """Rebuild the workstate index under ``state/symbol-knowledge/``; returns a summary dict.

    *resolver* maps ``(path, qualname)`` to a SCIP symbol string or None (default
    ``anchors.resolve`` when that module exists, else every ``symbol_key`` is null)."""
    root = Path(root)
    resolve = resolver if resolver is not None else _default_resolver()
    stamp = now or _now()
    tracked = _tracked(root)
    commit_sha = _head_sha(root)
    cache = _load_cache(root)
    docs_cache: "Dict[str, Any]" = cache["docs"]

    doc_paths = sorted(p for p in tracked if _doc_spec(p))
    stale = [p for p in doc_paths if (docs_cache.get(p) or {}).get("oid") != tracked[p]]
    blobs = _read_blobs(root, sorted({tracked[p] for p in stale}))
    for path in stale:
        text = blobs.get(tracked[path], "")
        refs = extract_citations(text)
        if _doc_spec(path)[1] == "plan":  # type: ignore[index]
            for r in refs:  # a bare plan-to-file citation emits no edge-batch line, so no section
                if r[0] == "p" and r[3] is None:
                    r[2] = None
        docs_cache[path] = {
            "oid": tracked[path],
            "id": _record_id(path, text),
            "observed_at": stamp,
            "refs": refs,
        }
    for path in [p for p in docs_cache if p not in tracked or not _doc_spec(p)]:
        del docs_cache[path]

    defs = _definition_keys(root)
    names_cache: "Dict[str, Any]" = cache["files"]
    used_files: "set[str]" = set()

    def names_of(path: str) -> "Dict[str, int]":
        used_files.add(path)
        hit = names_cache.get(path)
        if not hit or hit.get("oid") != tracked[path]:
            hit = {"oid": tracked[path], "names": _file_names(root, path)}
            names_cache[path] = hit
        return hit["names"]

    def is_code(path: str) -> bool:
        return path in tracked and not path.startswith(_NON_CODE_PREFIXES)

    def module_file(parts: "List[str]") -> "Optional[str]":
        base = "/".join(parts)
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in tracked:
                return cand
        return None

    def resolve_dotted(token: str) -> "Optional[Tuple[str, Optional[str], Optional[int]]]":
        """(file, qualname|None, line) for ``module`` or ``module.name`` in a tracked ``.py`` file."""
        parts = token.split(".")
        for i in range(len(parts), 0, -1):
            fpath = module_file(parts[:i])
            if fpath is None or not is_code(fpath):
                continue
            rest = ".".join(parts[i:])
            if not rest:
                return fpath, None, None
            line = names_of(fpath).get(rest)
            return (fpath, rest, line) if line is not None else None
        return None

    dotted: "Dict[str, Any]" = {}
    # edge: (doc_path, file_path, qualname|None, line_start, line_end, confidence, section)
    edges: "List[tuple]" = []
    specs = {doc: _doc_spec(doc) for doc in doc_paths}
    for doc in doc_paths:
        entry = docs_cache[doc]
        inferable = specs[doc][1] != "plan"
        cited_py = [r[1] for r in entry["refs"] if r[0] == "p" and r[1].endswith(".py") and is_code(r[1])]
        for ref in entry["refs"]:
            if ref[0] == "p":
                if is_code(ref[1]):
                    edges.append((doc, ref[1], None, ref[3], ref[4], "explicit", ref[2]))
                continue
            token, section = ref[1], ref[2]
            if token not in dotted:
                dotted[token] = resolve_dotted(token)
            hit = dotted[token]
            if hit is not None:
                edges.append((doc, hit[0], hit[1], hit[2], None, "explicit", section))
            elif inferable:
                for fpath in cited_py[:_MAX_CITED_FILES]:
                    line = names_of(fpath).get(token)
                    if line is not None:
                        edges.append((doc, fpath, token, line, None, "inferred", section))
    for path in [p for p in names_cache if p not in used_files]:
        del names_cache[path]

    scip_cache: "Dict[str, Any]" = cache["scip"]
    calls = 0
    live_keys: "set[str]" = set()

    def symbol_key(fpath: str, qual: str) -> "Optional[str]":
        nonlocal calls
        ck = f"{fpath}::{qual}"
        live_keys.add(ck)
        hit = scip_cache.get(ck)
        if hit and hit[0] == tracked[fpath]:
            return hit[1]
        if resolve is None or calls >= MAX_RESOLVES_PER_RUN:
            return None
        calls += 1
        try:
            key = resolve(fpath, qual)
        except Exception:
            return None
        scip_cache[ck] = [tracked[fpath], key]
        return key

    repo = root.resolve().name
    index: "Dict[str, set]" = {}
    anchors: "List[dict]" = []
    for doc, fpath, qual, ls, le, confidence, section in edges:
        doc_kind, record_kind, relation = specs[doc]
        if qual is None:
            idx_key = fpath
        else:
            idx_key = defs.get((_module_of(fpath), qual)) or f"{fpath}::{qual}"
        index.setdefault(idx_key, set()).add((doc, doc_kind))
        if record_kind is None or (record_kind == "plan" and qual is None and ls is None):
            continue
        anchors.append(
            {
                "kind": "workstate_anchor",
                "v": 1,
                "repo": repo,
                "commit_sha": commit_sha,
                "anchor": {
                    "file_path": fpath,
                    "symbol_key": symbol_key(fpath, qual) if qual is not None else None,
                    "line_start": ls,
                    "line_end": le,
                },
                "record": {
                    "record_kind": record_kind,
                    "record_path": doc,
                    "record_id": docs_cache[doc]["id"],
                    "section_anchor": section,
                },
                "relation": relation if confidence == "explicit" else "mentions",
                "confidence": confidence,
                "producer": PRODUCER,
                "observed_at": docs_cache[doc]["observed_at"],
            }
        )
    for ck in [k for k in scip_cache if k not in live_keys]:
        del scip_cache[ck]

    anchors.sort(
        key=lambda a: (
            a["anchor"]["file_path"],
            a["anchor"]["line_start"] or 0,
            a["record"]["record_path"],
            a["relation"],
            str(a["anchor"]["symbol_key"]),
        )
    )
    index_doc = {
        "schema": SCHEMA_VERSION,
        "refs": {k: [list(v) for v in sorted(index[k])] for k in sorted(index)},
    }
    wrote_index = _write_if_changed(root, INDEX_REL, json.dumps(index_doc, ensure_ascii=False, separators=(",", ":")) + "\n")
    wrote_anchors = _write_if_changed(
        root,
        ANCHORS_REL,
        "".join(json.dumps(a, ensure_ascii=False, separators=(",", ":")) + "\n" for a in anchors),
    )
    _write_if_changed(root, CACHE_REL, json.dumps(cache, sort_keys=True, separators=(",", ":")) + "\n")
    return {
        "commit_sha": commit_sha,
        "docs": len(doc_paths),
        "docs_reread": len(stale),
        "refs": len(index),
        "anchors": len(anchors),
        "scip_calls": calls,
        "scip_resolved": sum(1 for a in anchors if a["anchor"]["symbol_key"]),
        "index_path": INDEX_REL,
        "anchors_path": ANCHORS_REL,
        "wrote": wrote_index or wrote_anchors,
    }
