"""Incremental, blob-sha-cached front end to the amplification gate's scan.

Layers, checked top-down (a hit serves without the work below it): L0 the whole
tree (head blob only), L1 a per-file `_FileSummary`, X the cross-file merge and
route g's fixed point (recomputed from summaries, never parsed), L3 a per-file
verdict valid while every cross-file fact it observed fingerprints equal. The
scan is zero-spawn: an `os.scandir` stat walk plus in-process content hashes.
Cached verdicts are pre-suppression; the registers apply after.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import marshal
import pathlib
import sys
import time
from collections.abc import Mapping

import pytest

from coordinator_core.spawn_policy.detect import (
    DEFAULT_EXCLUDE,
    SpawnParseError,
    _is_python_source,
    is_test_tree_site,
    sites_in_source,
    walk_candidate_files,
)
from coordinator_core.tests import _amp_scan_store as store
from coordinator_core.tests import test_no_unbatched_per_item_git_spawn as gate
from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import AmpSite

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_SPAWN_POLICY_DIR = _TESTS_DIR.parent / "spawn_policy"

#: Every source whose bytes decide a verdict; a change to any invalidates the whole cache.
_ANALYZER_SOURCES: tuple[pathlib.Path, ...] = (
    pathlib.Path(gate.__file__).resolve(),
    _SPAWN_POLICY_DIR / "__init__.py",
    _SPAWN_POLICY_DIR / "allowlist.py",
    _SPAWN_POLICY_DIR / "detect.py",
    _SPAWN_POLICY_DIR / "marker_check.py",
    _SPAWN_POLICY_DIR / "spawn_names.py",
    _SPAWN_POLICY_DIR / "wrapper_resolution.py",
    _TESTS_DIR / "__init__.py",
    _TESTS_DIR / "_amp_scan_store.py",
    _TESTS_DIR.parent / "atomic_replace.py",
    pathlib.Path(__file__).resolve(),
)

_NON_PYTHON = "!"


@dataclasses.dataclass(frozen=True)
class IncrementalScanStats:
    """What one scan_incremental call recomputed, as repo-relative posix paths."""

    tree_hit: bool
    summarised: tuple[str, ...]
    reanalysed: tuple[str, ...]
    parsed: tuple[str, ...]


def gate_cache_dir(config: pytest.Config) -> pathlib.Path | None:
    """Cache directory for the gate, or None when the cacheprovider plugin is disabled."""
    cache = getattr(config, "cache", None)
    if cache is None:
        return None
    try:
        return pathlib.Path(cache.mkdir("amp-scan"))
    except OSError:
        return None


def _analyzer_digest() -> str:
    extra = f"{sys.version_info[:2]}|{store.FORMAT_VERSION}".encode()
    return store.analyzer_digest(_ANALYZER_SOURCES, extra)


# ---------------------------------------------------------------------------
# Summaries <-> marshal-able tuples
# ---------------------------------------------------------------------------


def _summary_to_obj(s):
    return (
        s.relpath,
        s.spawn_linenos,
        s.imported,
        s.raw_imports,
        s.param_defaults,
        tuple(
            (
                f.name,
                f.direct_spawner,
                f.verb_gated,
                f.runner_param,
                f.digest,
                (f.flow.params, f.flow.positional, f.flow.invoked, f.flow.calls),
            )
            for f in s.funcs
        ),
    )


def _summary_from_obj(o):
    relpath, spawn_linenos, imported, raw_imports, param_defaults, funcs = o
    return gate._FileSummary(
        relpath,
        spawn_linenos,
        imported,
        raw_imports,
        param_defaults,
        tuple(
            gate._FuncSummary(n, d, v, r, g, gate._FlowFacts(*flow))
            for n, d, v, r, g, flow in funcs
        ),
    )


def _site_to_obj(s: AmpSite) -> tuple:
    return (s.path, s.lineno, s.enclosing, s.route, s.callee, s.ordinal)


# ---------------------------------------------------------------------------
# Recorded read-set: what a file's verdict pass observed of other files
# ---------------------------------------------------------------------------


def _norm(value):
    """Order-free, marshal-able form of an index value, so equal facts fingerprint equal."""
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(_norm(v) for v in value))
    if isinstance(value, dict):
        return tuple(sorted((k, _norm(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_norm(v) for v in value)
    return value


#: Fields whose key does not name the owning file, or whose value is derived from other files
#: even under the owning file's own key.
_ALWAYS_FOREIGN = frozenset(
    {
        "funcs_by_name",
        "direct_spawn_funcs",
        "runner_shaped_funcs",
        "resolved_imports_by_file",
        "spawn_bearing_params",
    }
)


class _Facts:
    """The fresh cross-file layer, as fingerprints. A function body fingerprints as its
    digest and a file's spawn lines as their sorted tuple, so no AST is needed to validate."""

    def __init__(self, index, digests, linenos):
        self.index = index
        self.digests = digests
        self.linenos = linenos

    def fingerprint(self, field, key):
        if field == "func_defs":
            return self.digests.get(key)
        if field == "spawn_linenos":
            return tuple(sorted(self.linenos.get(key, ())))
        if field == "spawn_bearing_params":
            return key in self.index.spawn_bearing_params
        return _norm(getattr(self.index, field).get(key))


class _View:
    """A read-recording stand-in for one index map. A read keyed by the owning file is
    covered by that file's content key and is not recorded; every other read is."""

    __slots__ = ("_field", "_real", "_own", "_facts", "_reads")

    def __init__(self, field, real, own, facts, reads):
        self._field = field
        self._real = real
        self._own = own
        self._facts = facts
        self._reads = reads

    def _see(self, key):
        field = self._field
        if field not in _ALWAYS_FOREIGN and (key if isinstance(key, str) else key[0]) == self._own:
            return
        slot = (field, key)
        if slot not in self._reads:
            self._reads[slot] = self._facts.fingerprint(field, key)

    def get(self, key, default=None):
        self._see(key)
        return self._real.get(key, default)

    def __contains__(self, key):
        self._see(key)
        return key in self._real

    def __getitem__(self, key):
        self._see(key)
        return self._real[key]


_INDEX_FIELDS = (
    "direct_spawn_funcs",
    "runner_shaped_funcs",
    "same_module_direct_spawn",
    "imported_names_by_file",
    "param_runner_defaults",
    "verb_gated_spawn_verbs",
    "func_defs",
    "funcs_by_name",
    "spawn_bearing_params",
    "resolved_imports_by_file",
)


class _IndexView:
    """Duck-typed `_FuncIndex` whose every field is a recording `_View`. Iteration is not
    offered: the verdict pass reads by key only, and a whole-map read must fail loudly
    rather than go unrecorded."""

    def __init__(self, index, facts, own, reads):
        for field in _INDEX_FIELDS:
            setattr(self, field, _View(field, getattr(index, field), own, facts, reads))


class _LazyFuncDefs(Mapping):
    """`_FuncIndex.func_defs` whose key set comes from the summaries and whose values parse
    the owning file on first access."""

    def __init__(self, keys, nodes_of):
        self._keys = keys
        self._nodes_of = nodes_of

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        return self._nodes_of(key[0])[key]

    def __contains__(self, key):
        return key in self._keys

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)


# ---------------------------------------------------------------------------
# One scan's parses
# ---------------------------------------------------------------------------


class _Scan:
    """Per-call parse state: one `ast.parse` per file, shared by the file's own verdict pass
    and every dependant that walks its functions. `unstable` marks a file whose bytes no
    longer hash to the key the scan read it under; such a scan writes no cache."""

    def __init__(self, paths, keys):
        self.paths = paths
        self.keys = keys
        self.parsed_order: list[str] = []
        self.unstable = False
        self._loaded: dict = {}
        self._records: dict = {}
        self._nodes: dict = {}

    def load(self, relpath):
        if relpath in self._loaded:
            return self._loaded[relpath]
        result = None
        path = self.paths[relpath]
        try:
            data = path.read_bytes()
        except OSError:
            self.unstable = True
        else:
            if store.content_key(data) != self.keys[relpath]:
                self.unstable = True
            text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            self.parsed_order.append(relpath)
            try:
                result = (text, ast.parse(text, filename=str(path)))
            except SyntaxError:
                result = None
        self._loaded[relpath] = result
        return result

    def record(self, relpath):
        if relpath in self._records:
            return self._records[relpath]
        record = None
        loaded = self.load(relpath)
        if loaded is not None:
            text, tree = loaded
            try:
                sites = sites_in_source(text, relpath, tree) if gate._may_hold_spawn_site(text) else []
                record = gate._FileRecord(relpath, self.paths[relpath], text, tree, sites)
            except SpawnParseError:
                record = None
        self._records[relpath] = record
        return record

    def func_nodes(self, relpath):
        nodes = self._nodes.get(relpath)
        if nodes is None:
            loaded = self.load(relpath)
            nodes = {}
            if loaded is not None:
                for node in loaded[1].body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        nodes[(relpath, node.name)] = node
            self._nodes[relpath] = nodes
        return nodes


# ---------------------------------------------------------------------------
# Discovery: stat walk, memoised content keys
# ---------------------------------------------------------------------------


def _root_prefix(root: pathlib.Path) -> str:
    """Repo-relative posix prefix of `root`, mirroring the gate's `_relpath`."""
    try:
        rel = root.resolve().relative_to(gate._REPO_ROOT).as_posix()
    except ValueError:
        return ""
    return "" if rel == "." else rel


def _fs_now_ns(cache_dir: pathlib.Path | None) -> int:
    """The filesystem's own clock, sampled before any bytes are read."""
    if cache_dir is None:
        return time.time_ns()
    try:
        probe = cache_dir / "clock"
        probe.write_bytes(str(time.time_ns()).encode())
        return probe.stat().st_mtime_ns
    except OSError:
        return time.time_ns()


def _hash_and_classify(candidate) -> str:
    """`sha1(bytes)`, prefixed `!` when the file is not Python source; "" when unreadable."""
    try:
        data = candidate.path.read_bytes()
    except OSError:
        return ""
    key = store.content_key(data)
    is_python = candidate.path.suffix == ".py" or _is_python_source(
        candidate.path, pathlib.PurePosixPath(candidate.rel_posix)
    )
    return key if is_python else _NON_PYTHON + key


def _collect(roots, old_memo, now_ns):
    """Python source files under `roots` in the uncached discovery order, as
    `(relpath, path, content key)`, plus the refreshed memo and whether it changed."""
    memo = store.StatMemo()
    entries = []
    rehashed = 0
    for root in roots:
        if not root.exists():
            continue
        prefix = _root_prefix(root)
        candidates, _ = walk_candidate_files(root, exclude=DEFAULT_EXCLUDE)
        for c in candidates:
            relpath = f"{prefix}/{c.rel_posix}" if prefix else c.rel_posix
            if is_test_tree_site(relpath):
                continue
            key = old_memo.trust(relpath, c.size, c.mtime_ns)
            if key is not None:
                memo.entries[relpath] = old_memo.entries[relpath]
            else:
                key = _hash_and_classify(c)
                if not key:
                    continue
                memo.record(relpath, c.size, c.mtime_ns, key, now_ns)
                rehashed += 1
            if key[0] != _NON_PYTHON:
                entries.append((relpath, c.path, key))
    dirty = bool(rehashed) or len(memo.entries) != len(old_memo.entries)
    return memo, entries, dirty


def _tree_digest(analyzer: str, roots, entries) -> str:
    h = hashlib.sha1(analyzer.encode())
    for root in roots:
        h.update(b"\0root\0" + _root_prefix(root).encode())
    for relpath, _path, key in entries:
        h.update(b"\0" + relpath.encode() + b"\0" + key.encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _sections(body):
    if not isinstance(body, dict):
        return {}, {}
    summaries, verdicts = body.get("sum"), body.get("ver")
    return (summaries if isinstance(summaries, dict) else {}), (
        verdicts if isinstance(verdicts, dict) else {}
    )


#: `_FuncIndex` fields the cross-file merge computes; `func_defs` is attached lazily instead.
_MERGED_FIELDS = tuple(f.name for f in dataclasses.fields(gate._FuncIndex) if f.name != "func_defs")


def _merge_key(new_summaries) -> str:
    """Digest of every summary (not its content key) in discovery order: equal keys merge to an
    equal index."""
    return store.content_key(marshal.dumps([obj for _key, obj in new_summaries.values()]))


def _cached_merge(body, merge_key):
    """The cached merged index when its inputs are unchanged, else None."""
    entry = body.get("idx") if isinstance(body, dict) else None
    try:
        if entry[0] != merge_key:
            return None
        index = gate._FuncIndex()
        for name, value in zip(_MERGED_FIELDS, entry[1], strict=True):
            setattr(index, name, value)
        return index
    except Exception:
        return None


def _merge_to_obj(index) -> tuple:
    return tuple(getattr(index, name) for name in _MERGED_FIELDS)


def _cached_summary(cache, relpath, key):
    """(hit, summary-or-None); a malformed entry is a miss."""
    entry = cache.get(relpath)
    try:
        if entry[0] != key:
            return False, None
        return True, (None if entry[1] is None else _summary_from_obj(entry[1]))
    except Exception:
        return False, None


def _cached_verdict(cache, relpath, key, facts):
    """The cached `(readset, site tuples)` when the file's bytes and every fact it observed
    are unchanged; else None."""
    entry = cache.get(relpath)
    try:
        if entry[0] != key:
            return None
        readset, sites = entry[1], entry[2]
        for field, fkey, fp in readset:
            if facts.fingerprint(field, fkey) != fp:
                return None
        return readset, sites
    except Exception:
        return None


@gate._gc_paused
def scan_incremental(
    roots: tuple[pathlib.Path, ...], cache_dir: pathlib.Path | None
) -> tuple[list[AmpSite], IncrementalScanStats]:
    """Scan roots for unbatched per-item spawns, reusing cached per-file results.

    Sites are pre-suppression and in the uncached collector's order, identical to
    find_unbatched_per_item_spawns on a cold cache. cache_dir=None computes
    everything and writes nothing.
    """
    gate._WALK_CACHE.clear()
    analyzer = _analyzer_digest()
    head = store.load_head(cache_dir, analyzer) if cache_dir is not None else None
    old_memo = store.StatMemo.from_obj(head.get("memo") if head else None)
    memo, entries, memo_dirty = _collect(roots, old_memo, _fs_now_ns(cache_dir))
    gate._assert_not_self_scanned(
        [(rp, p) for rp, p, _ in entries if p.name == gate._THIS_FILE.name]
    )
    tree = _tree_digest(analyzer, roots, entries)

    if head is not None and head.get("tree") == tree:
        try:
            sites = [AmpSite(*t) for t in head["sites"]]
        except Exception:
            sites = None
        if sites is not None:
            if memo_dirty:
                body = store.load_body(cache_dir, head)
                if body is not None:
                    store.save(cache_dir, {**head, "memo": memo.to_obj()}, body, analyzer)
            return sites, IncrementalScanStats(True, (), (), ())

    body = store.load_body(cache_dir, head) if head is not None else None
    cached_summaries, cached_verdicts = _sections(body)
    scan = _Scan({rp: p for rp, p, _ in entries}, {rp: k for rp, _, k in entries})

    new_summaries: dict = {}
    summaries = []
    summarised: list[str] = []
    for relpath, _path, key in entries:
        hit, summary = _cached_summary(cached_summaries, relpath, key)
        if not hit:
            record = scan.record(relpath)
            summary = gate._summarise(record) if record is not None else None
            summarised.append(relpath)
        new_summaries[relpath] = (key, None if summary is None else _summary_to_obj(summary))
        if summary is not None:
            summaries.append(summary)

    merge_key = _merge_key(new_summaries)
    index = _cached_merge(body, merge_key)
    if index is None:
        index = gate._merge_summaries(summaries)
    digests = {(s.relpath, f.name): f.digest for s in summaries for f in s.funcs}
    linenos = {
        s.relpath: {ln for _enclosing, lns in s.spawn_linenos for ln in lns} for s in summaries
    }
    index.func_defs = _LazyFuncDefs(set(digests), scan.func_nodes)
    facts = _Facts(index, digests, linenos)

    new_verdicts: dict = {}
    sites: list[AmpSite] = []
    reanalysed: list[str] = []
    for summary in summaries:
        relpath = summary.relpath
        key = scan.keys[relpath]
        hit = _cached_verdict(cached_verdicts, relpath, key, facts)
        if hit is not None:
            readset, site_objs = hit
            sites.extend(AmpSite(*t) for t in site_objs)
        else:
            reads: dict = {}
            file_sites = gate._file_verdicts(
                scan.record(relpath),
                _IndexView(index, facts, relpath, reads),
                _View("spawn_linenos", linenos, relpath, facts, reads),
            )
            readset = tuple((f, k, fp) for (f, k), fp in reads.items())
            site_objs = tuple(_site_to_obj(s) for s in file_sites)
            sites.extend(file_sites)
            reanalysed.append(relpath)
        new_verdicts[relpath] = (key, readset, site_objs)

    if cache_dir is not None and not scan.unstable:
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        store.save(
            cache_dir,
            {"memo": memo.to_obj(), "tree": tree, "sites": [_site_to_obj(s) for s in sites]},
            {"sum": new_summaries, "ver": new_verdicts, "idx": (merge_key, _merge_to_obj(index))},
            analyzer,
        )
    return sites, IncrementalScanStats(
        False, tuple(summarised), tuple(reanalysed), tuple(scan.parsed_order)
    )
