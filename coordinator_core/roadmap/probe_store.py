"""coordinator_core.roadmap.probe_store — on-disk store of each plan's probe verdict.

The store is derived machine state under `.coordinator-local/cache/`, rebuildable
by running the probe, and never a source of truth. A record is checked against
every key leg before use: the plan's bytes, the upgrade flag, HEAD, the engine's
code, the repo root, and the stat identity of every external input the verdict
could have read. A lost, stale or torn store costs a probe on the next run and
never a wrong verdict.

Racy-clean rule (git's): a stored identity is trusted only when its
(size, mtime_ns, ino) equal the file's stat AND the stored mtime_ns is strictly
older than the moment the identity was computed. `_identity_matches` applies the
comparison over that tuple. Records here take
their computed-at moment from `ProbeStore` construction, which precedes every stat
the caller makes, so a file touched during the run is never trusted.

Read-site audit (the pin lives in tests/test_probe_store.py): every file read or
existence check made for one plan by `gate_plan_with_corpus`, `read_stamp` and
`upgrade_plan(check=True)` takes its path from a string the plan names or from a
fixed global, never from another file's contents beyond first order.

  prep_gate.py (8 counted sites)
    repo_root_names / repo_nested_names   root listing; second-level dirs   "." and, when a path's first segment is absent, every top-level dir
    repo_gitignored_roots                 <root>/.gitignore                 GLOBAL_INPUTS
    new-DR collision scan                 docs/decisions listing            GLOBAL_INPUTS
    spine writes directory check          (root / <writes string>).is_dir() string leaf
    derived_from sizing register          <derived_from string>             string leaf
    sizing register bytes                 <sizing string>                   string leaf
    plan text                             the plan itself                   plan_sha
    (uncounted) _UPGRADE_SCRIPT.is_file() fixed engine file                 engine_key
  mise-prep-upgrade.py (7 counted sites)
    plan_repo `.git` ancestor probe       fixed, and the repo root is a store leg
    plan_report / apply_derivable /
    main argv reads                       the plan itself                   plan_sha
  read_spine edge targets
    depends_on_plan target                <plan string>, content only       string leaf
    archived_plan_path                    archive/specs/*/<basename>        derived from the plan string; input_paths adds the month dirs
    checkpoint rows landed                git objects at HEAD               head leg
    memo_receipt_exists                   memo-outbox/sent/<slug>.md        derived from the row's own slug; input_paths adds it
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

from coordinator_core.engine_root import ENGINE_CHECKOUT_ROOT
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.session.machinery_paths import cache_dir

GENERATES = []

STORE_SCHEMA = 1

GLOBAL_INPUTS = (
    ".",
    ".gitignore",
    "docs/decisions",
    "coordinator_core/frontmatter/schemas/plan.schema.json",
)

_PLAN_SCHEMA_REL = "coordinator_core/frontmatter/schemas/plan.schema.json"
_BIN_SCRIPTS = ("mise-prep-upgrade.py",)
_IMPORTED_AT_NS = time.time_ns()
_PATH_LEAF_RE = re.compile(r"^[^\s\"'`<>|]{1,240}$")
_GLOB_CHARS = "*?["


def store_path(repo_root: Path) -> Path:
    """The store file: the main worktree's cache dir, shared by every linked worktree."""
    return Path(cache_dir(str(main_worktree_root(Path(repo_root))))) / "probe-store.json"


def plan_sha(data: bytes) -> str:
    """sha256 hex over the plan file's bytes; stands for its body and frontmatter."""
    return hashlib.sha256(data).hexdigest()


def _stat_identity(path: str) -> Optional[list]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def _identity_matches(stored: Any, current: Optional[list], computed_at_ns: Any) -> bool:
    """`current` equals `stored` and `stored` is not racy against `computed_at_ns`."""
    if stored is None or current is None:
        return stored is None and current is None
    try:
        size, mtime_ns, ino = stored
    except (TypeError, ValueError):
        return False
    if [size, mtime_ns, ino] != current:
        return False
    return isinstance(computed_at_ns, int) and mtime_ns < computed_at_ns


def _record_key(rel: str, upgrade: bool) -> str:
    return f"{rel}|{int(bool(upgrade))}"


class ProbeStore:
    """In-memory view of the store file; `save` persists it when something changed."""

    def __init__(self, path: Optional[Path] = None, root: Optional[Path] = None,
                 records: Optional[dict] = None) -> None:
        self._path = path
        self._root = Path(root) if root is not None else None
        self._records: dict = records if records is not None else {}
        self._dirty = False
        self._born_ns = time.time_ns()
        self._stats: dict = {}

    @classmethod
    def load(cls, repo_root: Path) -> "ProbeStore":
        """The stored view, or an empty one when the file is missing, unreadable,
        corrupt or of another schema. Never raises."""
        try:
            path = store_path(repo_root)
        except Exception:  # noqa: BLE001 — an unresolvable root has no store
            return cls()
        root = Path(repo_root)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
                return cls(path, root)
            records = data.get("records")
            if not isinstance(records, dict):
                return cls(path, root)
            return cls(path, root, records)
        except Exception:  # noqa: BLE001
            return cls(path, root)

    def _current(self, rel: str) -> Optional[list]:
        if rel not in self._stats:
            self._stats[rel] = _stat_identity(str(self._root / rel))
        return self._stats[rel]

    def get(self, rel: str, plan_sha: str, upgrade: bool, head: Optional[str],
            engine_key: Optional[str]) -> Optional[dict]:
        """The stored reply (single-form keys, less process_ms) only when every leg
        equals and every recorded input re-stats equal under the racy-clean rule;
        else None. Stat and dict work only: no YAML, no gate, no spawn."""
        if self._root is None or engine_key is None:
            return None
        rec = self._records.get(_record_key(rel, upgrade))
        if not isinstance(rec, dict):
            return None
        if (rec.get("plan_sha") != plan_sha or rec.get("head") != head
                or rec.get("engine_key") != engine_key or rec.get("root") != str(self._root)):
            return None
        inputs, at, reply = rec.get("inputs"), rec.get("at"), rec.get("reply")
        if not isinstance(inputs, dict) or not isinstance(reply, dict):
            return None
        for path, stored in inputs.items():
            if not _identity_matches(stored, self._current(path), at):
                return None
        return dict(reply)

    def put(self, rel: str, plan_sha: str, upgrade: bool, head: Optional[str],
            engine_key: Optional[str], inputs: dict, reply: dict) -> None:
        """Record a verdict. `inputs` maps each input path to [size, mtime_ns, ino],
        or None when absent, stat'd at the miss. A None engine_key is never stored."""
        if self._root is None or engine_key is None:
            return
        rec = {
            "plan_sha": plan_sha,
            "head": head,
            "engine_key": engine_key,
            "root": str(self._root),
            "at": self._born_ns,
            "inputs": {p: (list(v) if v is not None else None) for p, v in inputs.items()},
            "reply": {k: v for k, v in reply.items() if k != "process_ms"},
        }
        key = _record_key(rel, upgrade)
        if self._records.get(key) != rec:
            self._records[key] = rec
            self._dirty = True

    def save(self) -> None:
        """Persist when something changed: temp file in the same directory, then
        os.replace. OSError is swallowed; the last concurrent writer wins."""
        if not self._dirty or self._path is None:
            return
        self._drop_gone_plans()
        tmp = self._path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"schema": STORE_SCHEMA, "records": self._records}, sort_keys=True)
            tmp.write_text(payload, encoding="utf-8", newline="\n")
            os.replace(tmp, self._path)
            self._dirty = False
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _drop_gone_plans(self) -> None:
        """Forget records of a rel no longer under docs/plans."""
        if self._root is None:
            return
        for key in [k for k in self._records
                    if not os.path.lexists(self._root / k.rsplit("|", 1)[0])]:
            del self._records[key]


def _leaves(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _leaves(item)


def _as_rel(leaf: str, root: str) -> Optional[str]:
    """`leaf` as a repo-relative forward-slash path under `root`, or None."""
    text = leaf.strip().replace("\\", "/")
    if not _PATH_LEAF_RE.match(text) or text.startswith("/") or "://" in text:
        return None
    if "/" not in text and "." not in text[1:]:
        return None
    cut = min((text.find(c) for c in _GLOB_CHARS if c in text), default=-1)
    if cut >= 0:
        text = text[:cut].rsplit("/", 1)[0] if "/" in text[:cut] else ""
    norm = os.path.normpath(os.path.join(root, text)) if text else ""
    if not norm or not (norm + os.sep).startswith(root.rstrip(os.sep) + os.sep) or norm == root:
        return None
    return os.path.relpath(norm, root).replace(os.sep, "/")


def input_paths(fm: dict, spine_rows: list, repo_root: Path) -> list:
    """Every path-shaped string leaf of the parsed frontmatter and spine, repo-relative
    and under the root, plus GLOBAL_INPUTS; deduplicated. Over-approximates: a leaf
    that is no path costs a stat, a missed input costs a stale verdict."""
    from coordinator_core.ops.dispatch_emit.memo_rows import memo_row_slug
    from coordinator_core.ops.fleet.memo_wire import memo_send_performed_paths

    root = os.path.normpath(str(repo_root))
    out: dict = {g: None for g in GLOBAL_INPUTS}
    needs_nested = False
    try:
        root_names = set(os.listdir(root))
    except OSError:
        root_names = set()
    plan_edge = False
    for leaf in list(_leaves(fm)) + list(_leaves(spine_rows)):
        rel = _as_rel(leaf, root)
        if rel is None:
            continue
        out[rel] = None
        if rel.split("/", 1)[0] not in root_names:
            needs_nested = True
        if rel.startswith("docs/plans/") and rel.endswith(".md"):
            plan_edge = True
    for row in spine_rows or []:
        if isinstance(row, dict):
            slug = memo_row_slug(row)
            if slug:
                out[memo_send_performed_paths(slug)[0]] = None
    if plan_edge:
        out["archive/specs"] = None
        try:
            for entry in os.scandir(os.path.join(root, "archive", "specs")):
                if entry.is_dir():
                    out[f"archive/specs/{entry.name}"] = None
        except OSError:
            pass
    if needs_nested:
        for name in sorted(root_names):
            if not name.startswith(".") and os.path.isdir(os.path.join(root, name)):
                out[name] = None
    return list(out)


_PROBE_ROOT_REL = "coordinator_core/ops/plan_mise_prep_probe.py"
_BIN_SEARCH_RELS = ("coordinator/bin/lib", "coordinator/bin")

#: {file path: ((size, mtime_ns), dep paths)}; a file is re-parsed only when its stat changes.
_IMPORT_PARSE_CACHE: dict = {}


def _module_files(root: str, parts: list) -> list:
    """Existing files that importing the dotted `parts` runs: each package `__init__`
    on the way and the module file itself. Top-level names other than coordinator_core
    resolve against the bin script search dirs."""
    bases = [root] if parts[0] == "coordinator_core" else [
        os.path.join(root, *rel.split("/")) for rel in _BIN_SEARCH_RELS
    ]
    out = []
    for base in bases:
        cur = base
        found = False
        for i, part in enumerate(parts):
            cur = os.path.join(cur, part)
            pkg_init = os.path.join(cur, "__init__.py")
            if os.path.isfile(pkg_init):
                out.append(pkg_init)
                found = True
            elif i == len(parts) - 1 and os.path.isfile(cur + ".py"):
                out.append(cur + ".py")
                found = True
            elif not os.path.isdir(cur):
                break
        if found:
            break
    return out


def _package_parts(root: str, path: str) -> list:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    return rel.split("/")[:-1]


def _import_targets(root: str, path: str) -> Optional[list]:
    """Engine files `path` imports at any nesting level, or None when it cannot be parsed.
    Over-approximates: a computed importlib name adds every file of the importer's package."""
    try:
        tree = ast.parse(Path(path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    pkg = _package_parts(root, path) if path.startswith(root) else []
    names: list = []
    widen = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name.split(".") for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else list(pkg)
                mod = base + (node.module.split(".") if node.module else [])
            else:
                mod = node.module.split(".") if node.module else []
            if not mod:
                continue
            names.append(mod)
            names.extend(mod + [a.name] for a in node.names if a.name != "*")
        elif isinstance(node, ast.Call):
            fn = node.func
            called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if called in ("import_module", "__import__"):
                arg = node.args[0] if node.args else None
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.append(arg.value.split("."))
                else:
                    widen = True
    out: dict = {}
    for parts in names:
        if parts and parts[0]:
            for f in _module_files(root, parts):
                out[f] = None
    if widen:
        folder = os.path.dirname(path)
        try:
            for entry in os.scandir(folder):
                if entry.name.endswith(".py"):
                    out[entry.path] = None
        except OSError:
            pass
    return list(out)


def _graph_path(root: str) -> Optional[Path]:
    try:
        return Path(cache_dir(str(main_worktree_root(Path(root))))) / "probe-import-graph.json"
    except Exception:  # noqa: BLE001 — no resolvable root, no disk layer
        return None


def _load_graph(root: str) -> None:
    """Seed the parse cache from disk, so a fresh process stats the closure rather than
    re-parsing it. A missing, torn or other-root file seeds nothing."""
    path = _graph_path(root)
    if path is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != STORE_SCHEMA or data.get("root") != root:
            return
        for file, (size, mtime_ns, deps) in data["files"].items():
            _IMPORT_PARSE_CACHE.setdefault(file, ((size, mtime_ns), list(deps)))
    except Exception:  # noqa: BLE001
        return


def _save_graph(root: str, files: list) -> None:
    path = _graph_path(root)
    if path is None:
        return
    payload = {
        "schema": STORE_SCHEMA, "root": root,
        "files": {f: [*_IMPORT_PARSE_CACHE[f][0], _IMPORT_PARSE_CACHE[f][1]]
                  for f in files if f in _IMPORT_PARSE_CACHE},
    }
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


_GRAPH_SEEDED: set = set()


def _closure(root: str, stats: Optional[dict] = None) -> Optional[list]:
    """The sorted engine files in the static import closure of the probe and the two bin
    scripts, plus those roots and plan.schema.json; None when a closure file will not parse.
    Per-file parses persist in the cache dir, so only a file whose stat changed is re-parsed.
    A `stats` dict is filled with `{path: stat_result}` for every file the walk stat'd."""
    if root not in _GRAPH_SEEDED:
        _GRAPH_SEEDED.add(root)
        _load_graph(root)
    roots = [os.path.join(root, *_PROBE_ROOT_REL.split("/"))]
    roots.extend(os.path.join(root, "coordinator", "bin", name) for name in _BIN_SCRIPTS)
    seen: dict = {r: None for r in roots}
    queue = list(roots)
    reparsed = False
    while queue:
        path = queue.pop()
        try:
            st = os.stat(path)
        except OSError:
            continue
        if stats is not None:
            stats[path] = st
        sig = (st.st_size, st.st_mtime_ns)
        hit = _IMPORT_PARSE_CACHE.get(path)
        if hit is None or hit[0] != sig:
            deps = _import_targets(root, path)
            if deps is None:
                return None
            hit = _IMPORT_PARSE_CACHE[path] = (sig, deps)
            reparsed = True
        for dep in hit[1]:
            if dep not in seen:
                seen[dep] = None
                queue.append(dep)
    seen[os.path.join(root, *_PLAN_SCHEMA_REL.split("/"))] = None
    ordered = sorted(seen)
    if reparsed:
        _save_graph(root, ordered)
    return ordered


def engine_key(engine_sha: Optional[str]) -> Optional[str]:
    """The engine sha plus a digest of (path, size, mtime_ns) over the probe's code
    closure: the engine files its module, the two bin scripts and their imports reach
    by a static `ast` walk, plus plan.schema.json. A function of the files alone, never
    of sys.modules, so equal across calls in one process and across processes. None
    when any closure file is newer than this module's import time, or will not parse:
    the caller then neither reads nor writes the store. A file outside the closure
    never changes the key."""
    root = os.path.normpath(str(ENGINE_CHECKOUT_ROOT))
    walked: dict = {}
    files = _closure(root, walked)
    if files is None:
        return None
    digest = hashlib.sha256()
    prefix = root + os.sep
    cut = len(prefix)
    for path in files:
        rel = path[cut:] if path.startswith(prefix) else os.path.relpath(path, root)
        rel = rel.replace(os.sep, "/")
        st = walked.get(path)
        if st is None:
            try:
                st = os.stat(path)
            except OSError:
                digest.update(f"{rel}\0absent\n".encode())
                continue
        if st.st_mtime_ns > _IMPORTED_AT_NS:
            return None
        digest.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
    return f"{engine_sha}:{digest.hexdigest()[:32]}"
