"""Whole-tree frontmatter sweep behind `lint-frontmatter` with no `--file`.

A cold process reads a verdict store under the git dir, re-stats the corpus
once, and replays every verdict whose inputs are unchanged. The validator
(`schema_validate`) and `dag` are imported only to answer a miss, so a warm
run touches neither.

Store invariants:
  - An entry is reused only while its file stamp (size, mtime_ns), the
    schema that judged it, and every dependency it recorded still match.
  - A handoff entry records the filesystem probes its ref checks made
    (existence, isdir, directory mtime), an id-index digest when the index
    was consulted, and `h` when a ref rested on "never tracked in git
    history"; only `h` entries revalidate when the ref tips move.
  - A validator-module edit drops every entry; a schema edit drops only the
    entries that schema judged, unless the routing tables changed, which
    drops all.
  - Output (JSON keys, human text, exit codes) equals the retired
    `schema_validate._run_tree_walk`, including schema iteration order and
    the first-claim rule for a file two globs match.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.session.record_homes import home_dir

_LIVE_HANDOFFS = Path(home_dir("", "handoffs")).as_posix()

_STORE_NAME = 'claude-klabauter-lint-tree.json'
_STORE_VERSION = 1
_ROADMAP_ENV = 'COORDINATOR_OVERRIDE_ROADMAP_GRAPH_FIELDS'
_CORE = Path(__file__).resolve().parent.parent
_SCHEMAS_DIR = Path(__file__).resolve().parent / 'schemas'

# Modules whose bytes decide a verdict; an edit to any of them drops every entry.
_CODE_FILES = (
    'frontmatter/schema_validate.py',
    'frontmatter/lint_tree.py',
    'frontmatter/baton_class.py',
    'frontmatter/body_blocks.py',
    'frontmatter/schema_shape.py',
    'ops/fleet/memo_kinds.py',
    'ops/fleet/_outbox_frontmatter_rules.py',
    'ops/signoff_provenance.py',
    'git_scope.py',
    'dag.py',
)

_YAML_JSON_RE = re.compile(r'\.(ya?ml|json)$')
_MISSING = object()


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _sha_file(path: str) -> str:
    try:
        with open(path, 'rb') as f:
            return hashlib.sha1(f.read()).hexdigest()
    except OSError:
        return ''


def _stamp_set(old: dict, paths: dict[str, str]) -> tuple[dict, bool]:
    """Stamp every path; re-hash only a file whose (size, mtime_ns) moved.
    Returns (new map, whether any content hash or the name set differs)."""
    new: dict[str, list] = {}
    content_changed = set(old) != set(paths)
    for name, path in paths.items():
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime = 0, 0
        prev = old.get(name)
        if prev and prev[0] == size and prev[1] == mtime:
            new[name] = prev
            continue
        sha = _sha_file(path)
        new[name] = [size, mtime, sha]
        if not prev or prev[2] != sha:
            content_changed = True
    return new, content_changed


def _glob_base(glob: str) -> tuple[str, bool]:
    fixed: list[str] = []
    for part in glob.split('/'):
        if '*' in part or '?' in part:
            break
        fixed.append(part)
    return '/'.join(fixed), '**' in glob


def _roundtrips(value) -> bool:
    try:
        return json.loads(json.dumps(value)) == value
    except (TypeError, ValueError):
        return False


def _schema_sha(schema) -> str:
    try:
        text = json.dumps(schema, sort_keys=True, default=repr)
    except TypeError:
        text = repr(schema)
    return hashlib.sha1(text.encode('utf-8', 'surrogatepass')).hexdigest()


# ---------------------------------------------------------------------------
# Verdict store
# ---------------------------------------------------------------------------

def _fresh_data() -> dict:
    return {
        'v': _STORE_VERSION, 'env': '', 'code': {}, 'sfiles': {}, 'derived': None,
        'files': {}, 'hids': {}, 'tips': None,
    }


class _Store:
    """The on-disk verdict store; inert (never reads or writes) when the git dir does not resolve."""

    def __init__(self, repo_root: str) -> None:
        self.path: str | None = None
        gd = resolve_git_dir(repo_root)
        try:
            if gd.is_dir():
                self.path = str(gd / _STORE_NAME)
        except OSError:
            self.path = None
        self.data = _fresh_data()
        if self.path:
            self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or data.get('v') != _STORE_VERSION:
            return
        shape = {'env': str, 'code': dict, 'sfiles': dict, 'files': dict, 'hids': dict}
        if not all(isinstance(data.get(k), t) for k, t in shape.items()):
            return
        if not isinstance(data.get('derived'), (dict, type(None))) or not isinstance(data.get('tips'), (str, type(None))):
            return
        self.data = {**_fresh_data(), **data}

    def save(self) -> None:
        if not self.path:
            return
        tmp = f'{self.path}.{os.getpid()}.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, separators=(',', ':'))
            os.replace(tmp, self.path)
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Filesystem probe recorder
# ---------------------------------------------------------------------------

class _FsRecorder:
    """While active, `os.path.exists`, `os.path.isdir` and `os.listdir` log each probe
    (and its answer) so a handoff verdict can name the filesystem it rested on.
    Any other probe the ref checks make (`isfile`, `Path.exists`, `os.stat`) is invisible
    here and would leave a warm verdict stale."""

    def __init__(self, root: str) -> None:
        self._root = os.path.normcase(os.path.normpath(root))
        self.deps: dict[tuple[str, str], object] = {}
        self._orig: tuple | None = None

    def _rel(self, p) -> str:
        norm = os.path.normpath(os.fspath(p))
        folded = os.path.normcase(norm)
        if folded.startswith(self._root + os.sep):
            return norm[len(self._root) + 1:].replace('\\', '/')
        return norm

    def _note(self, kind: str, p, value) -> None:
        self.deps.setdefault((kind, self._rel(p)), value)

    def __enter__(self) -> '_FsRecorder':
        orig_exists, orig_isdir, orig_listdir = os.path.exists, os.path.isdir, os.listdir
        self._orig = (orig_exists, orig_isdir, orig_listdir)

        def exists(p):
            v = orig_exists(p)
            self._note('e', p, v)
            return v

        def isdir(p):
            v = orig_isdir(p)
            self._note('d', p, v)
            return v

        def listdir(p='.'):
            try:
                result = orig_listdir(p)
            except OSError:
                self._note('l', p, None)
                raise
            try:
                stamp = os.stat(p).st_mtime_ns
            except OSError:
                stamp = None
            self._note('l', p, stamp)
            return result

        os.path.exists, os.path.isdir, os.listdir = exists, isdir, listdir
        return self

    def __exit__(self, *exc) -> None:
        os.path.exists, os.path.isdir, os.listdir = self._orig


class _History(set):
    """Lazy git-history membership set; `negative` records that a lookup answered "never tracked"."""

    def __init__(self, repo_root: str) -> None:
        super().__init__()
        self._root: str | None = repo_root
        self.complete = False
        self.negative = False

    def __contains__(self, item) -> bool:
        if self._root is not None:
            root, self._root = self._root, None
            from coordinator_core.dag import build_persisted_git_history_cache
            cache = build_persisted_git_history_cache(root)
            if cache is not None:
                self.update(cache)
                self.complete = cache.complete
        hit = super().__contains__(item)
        if not hit:
            self.negative = True
        return hit


class _IdIndex(dict):
    """handoff_id -> logical path, built on the first `get`; `used` records that a ref consulted it."""

    def __init__(self, sweep: '_Sweep') -> None:
        super().__init__()
        self._sweep: '_Sweep | None' = sweep
        self.used = False

    def get(self, key, default=None):
        self.used = True
        if self._sweep is not None:
            sweep, self._sweep = self._sweep, None
            self.update(sweep.build_id_index())
        return super().get(key, default)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

class _Sweep:
    def __init__(self, repo_root: str, strict_refs: bool) -> None:
        self.root = repo_root
        self.strict = strict_refs
        self.store = _Store(repo_root)
        self.dirty = False
        self._sv = None
        self._schemas = None
        self._scans: dict[tuple[str, bool], list] = {}
        self._probe_memo: dict[tuple[str, str], object] = {}
        self._digest: str | None = None
        self._tips = _MISSING
        self._hids_fresh = False
        self._history: _History | None = None
        self._index: _IdIndex | None = None

    # -- lazy heavy imports -------------------------------------------------

    @property
    def sv(self):
        if self._sv is None:
            from coordinator_core.frontmatter import schema_validate
            self._sv = schema_validate
        return self._sv

    def schemas(self) -> dict:
        if self._schemas is None:
            self._schemas = self.sv.load_schemas(_SCHEMAS_DIR)
        return self._schemas

    # -- fingerprint and derived tables --------------------------------------

    def _derive(self) -> dict:
        sv = self.sv
        schemas = self.schemas()
        globs = []
        shas = {}
        for name in (k for k in schemas if k not in ('_byGlob', '_byKind')):
            schema = schemas[name]
            shas[name] = _schema_sha(schema)
            glob = sv._GLOB_OVERRIDES.get('handoff-archived') if name == 'handoff-archived' else schema.get('applies_to')
            if not glob or schema.get('match_mode') == 'inline-tag-per-entry':
                continue
            globs.append([name, glob, sv._glob_to_regex(glob).pattern])
        derived = {
            'globs': globs,
            'sidecar': sv._LINT_SIDECAR_RE.pattern,
            'routing': {'byGlob': schemas['_byGlob'], 'byKind': schemas['_byKind'], 'globs': globs},
            'shas': shas,
        }
        return json.loads(json.dumps(derived, default=repr))

    def prepare(self) -> None:
        d = self.store.data
        code_paths = {name: str(_CORE / name) for name in _CODE_FILES}
        new_code, code_content_changed = _stamp_set(d['code'], code_paths)
        try:
            with os.scandir(_SCHEMAS_DIR) as it:
                schema_paths = {e.name: e.path for e in it if e.is_file()}
        except OSError:
            schema_paths = {}
        new_sfiles, schema_changed = _stamp_set(d['sfiles'], schema_paths)
        env = os.environ.get(_ROADMAP_ENV, '0')
        code_changed = code_content_changed or env != d['env'] or d['derived'] is None

        if new_code != d['code'] or new_sfiles != d['sfiles'] or env != d['env']:
            self.dirty = True
        d['code'], d['sfiles'], d['env'] = new_code, new_sfiles, env

        if code_changed:
            d['files'], d['hids'], d['tips'] = {}, {}, None
            d['derived'] = self._derive()
            self.dirty = True
        elif schema_changed:
            old = d['derived']
            new = self._derive()
            d['derived'] = new
            self.dirty = True
            if new['routing'] != old['routing']:
                d['files'] = {}
            else:
                changed = {n for n, sha in old['shas'].items() if new['shas'].get(n) != sha}
                changed |= set(new['shas']) - set(old['shas'])
                d['files'] = {k: v for k, v in d['files'].items() if v.get('n') not in changed}

    # -- discovery -----------------------------------------------------------

    def scan(self, base: str, recursive: bool) -> list:
        """Files under `base` as (repo_rel, name, size, mtime_ns), in sorted depth-first order."""
        key = (base, recursive)
        cached = self._scans.get(key)
        if cached is not None:
            return cached
        out: list = []

        def walk(abs_dir: str, rel_prefix: str) -> None:
            try:
                with os.scandir(abs_dir) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                return
            for entry in entries:
                rel = rel_prefix + entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if recursive:
                            walk(entry.path, rel + '/')
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat()
                        out.append((rel, entry.name, st.st_size, st.st_mtime_ns))
                except OSError:
                    continue

        abs_base = os.path.join(self.root, *base.split('/')) if base else self.root
        walk(abs_base, base + '/' if base else '')
        self._scans[key] = out
        return out

    # -- handoff-id index ------------------------------------------------------

    def _shelf(self) -> list:
        """(repo_rel, is_archive, name, size, mtime_ns) in the order the id index claims them."""
        live = [(r, False, n, s, m) for r, n, s, m in self.scan(_LIVE_HANDOFFS, False) if n.endswith('.md')]
        arch = [(r, True, n, s, m) for r, n, s, m in self.scan('archive/handoffs', True) if n.endswith('.md')]
        return live + arch

    def _handoff_id_of(self, content: str):
        parsed = self.sv.parse_frontmatter(content)
        fm = parsed['frontmatter']
        hid = fm.get('handoff_id') if fm else None
        return hid if isinstance(hid, str) else None

    def refresh_hids(self) -> None:
        if self._hids_fresh:
            return
        self._hids_fresh = True
        old = self.store.data['hids']
        new: dict[str, list] = {}
        for rel, _arch, _name, size, mtime in self._shelf():
            prev = old.get(rel)
            if prev and prev[0] == size and prev[1] == mtime:
                new[rel] = prev
                continue
            try:
                content = Path(os.path.join(self.root, *rel.split('/'))).read_text(encoding='utf-8')
            except OSError:
                continue
            new[rel] = [size, mtime, self._handoff_id_of(content)]
        if new != old:
            self.dirty = True
        self.store.data['hids'] = new

    def id_digest(self) -> str:
        if self._digest is None:
            self.refresh_hids()
            hids = self.store.data['hids']
            h = hashlib.sha1()
            for rel, is_arch, name, _s, _m in self._shelf():
                ent = hids.get(rel)
                if ent and ent[2]:
                    key = f'{_LIVE_HANDOFFS}/{name}' if is_arch else rel
                    h.update(f"{'A' if is_arch else 'L'}\0{key}\0{ent[2]}\n".encode('utf-8', 'surrogatepass'))
            self._digest = h.hexdigest()
        return self._digest

    def build_id_index(self) -> dict[str, str]:
        """handoff_id -> logical path from stored ids, under `schema_validate`'s collision rules."""
        self.refresh_hids()
        hids = self.store.data['hids']
        return self.sv._resolve_handoff_id_claims(
            (ent[2], f'{_LIVE_HANDOFFS}/{name}' if is_arch else rel, is_arch)
            for rel, is_arch, name, _s, _m in self._shelf()
            if (ent := hids.get(rel)) and ent[2]
        )

    # -- dependency validation ------------------------------------------------

    def _probe(self, kind: str, path: str):
        k = (kind, path)
        v = self._probe_memo.get(k, _MISSING)
        if v is _MISSING:
            abs_path = path if os.path.isabs(path) else os.path.join(self.root, path)
            if kind == 'e':
                v = os.path.exists(abs_path)
            elif kind == 'd':
                v = os.path.isdir(abs_path)
            else:
                try:
                    v = os.stat(abs_path).st_mtime_ns
                except OSError:
                    v = None
            self._probe_memo[k] = v
        return v

    def _current_tips(self):
        if self._tips is _MISSING:
            from coordinator_core.git.git_state import ref_tips_digest
            self._tips = ref_tips_digest(self.root)
        return self._tips

    def _entry_valid(self, ent: dict) -> bool:
        for kind, path, value in ent.get('d', ()):
            if self._probe(kind, path) != value:
                return False
        stored_digest = ent.get('i')
        if stored_digest is not None and self.id_digest() != stored_digest:
            return False
        if ent.get('h'):
            cur = self._current_tips()
            if cur is None or cur != self.store.data['tips']:
                return False
        return True

    # -- cold path: one file ----------------------------------------------------

    def _check_file(self, rel: str, full_path: str, name: str, stamp: list, is_shelf: bool):
        """Validate one file the way the retired tree walk did. Returns (entry, read_error);
        `entry` is None only when the file could not be read."""
        sv = self.sv
        schemas = self.schemas()
        try:
            content = Path(full_path).read_text(encoding='utf-8')
        except OSError as exc:
            return None, {
                'file': rel, 'schema': name,
                'errors': [{'field': '(read)', 'error': 'could not read file', 'hint': str(exc)}],
            }

        parsed = sv.parse_frontmatter(content)
        fm = parsed['frontmatter']
        if is_shelf:
            hid = fm.get('handoff_id') if fm else None
            self.store.data['hids'][rel] = [stamp[0], stamp[1], hid if isinstance(hid, str) else None]
        dup_errors = sv._dup_key_errors(parsed)

        declares_unregistered_kind = (
            fm is not None
            and fm.get('kind') is not None
            and sv._schema_name_for_kind(schemas, str(fm['kind'])) is None
        )
        if declares_unregistered_kind or sv._is_plan_dir_index_routing_excluded(rel.replace('\\', '/'), fm):
            return {'s': stamp}, None

        resolved = sv.match_schema(rel, fm, schemas)
        eff_name = resolved['schemaName'] if resolved else name
        eff_schema = resolved['schema'] if resolved else schemas[name]

        result = sv._tolerate_handoff_kind_aliases_in_result(
            sv.validate_frontmatter_obj(fm, eff_schema), eff_name, eff_schema, fm,
        )
        base_errors = (result.get('errors') or []) if not result.get('ok') else []

        ent: dict = {'s': stamp, 'n': eff_name}
        errors = list(dup_errors) + list(base_errors)
        if errors:
            ent['e'] = errors
        if eff_name == 'handoff':
            if self._history is None:
                self._history = _History(self.root)
                self._index = _IdIndex(self)
            history, index = self._history, self._index
            history.negative = False
            index.used = False
            from coordinator_core import dag  # noqa: F401  -- imported before probing starts
            with _FsRecorder(self.root) as rec:
                disagree, dangling = sv._check_handoff_refs(fm, self.root, full_path, rel, history, index, False)
            if disagree:
                ent['r'] = disagree
            if dangling:
                ent['w'] = dangling
            if rec.deps:
                ent['d'] = [[k, p, v] for (k, p), v in rec.deps.items()]
            if index.used:
                ent['i'] = self.id_digest()
            if history.negative:
                ent['h'] = True
        return ent, None

    # -- the walk -------------------------------------------------------------

    def run(self) -> tuple[list, list]:
        self.prepare()
        d = self.store.data
        derived = d['derived']
        sidecar_re = re.compile(derived['sidecar'])
        old_files = d['files']
        new_files: dict[str, dict] = {}
        violations: list[dict] = []
        ref_warnings: list[dict] = []
        seen: set[str] = set()
        misses = 0

        for name, glob, pattern in derived['globs']:
            base, recursive = _glob_base(glob)
            rx = re.compile(pattern)
            shelf_bases = ((_LIVE_HANDOFFS, False), ('archive/handoffs', True))
            is_shelf_scan = (base, recursive) in shelf_bases
            for rel, fname, size, mtime in self.scan(base, recursive):
                if rel in seen or not rx.match(rel):
                    continue
                if sidecar_re.search(rel) or _YAML_JSON_RE.search(rel):
                    continue
                seen.add(rel)
                stamp = [size, mtime]

                ent = old_files.get(rel)
                try:
                    hit = (
                        ent is not None and ent['s'][0] == size and ent['s'][1] == mtime
                        and self._entry_valid(ent)
                    )
                except (KeyError, TypeError, IndexError, ValueError):
                    hit = False

                if not hit:
                    full_path = os.path.join(self.root, *rel.split('/'))
                    ent, read_error = self._check_file(
                        rel, full_path, name, stamp, is_shelf_scan and fname.endswith('.md'),
                    )
                    if read_error is not None:
                        violations.append(read_error)
                        continue
                    misses += 1
                    if not _roundtrips(ent):
                        self._emit(ent, rel, violations, ref_warnings)
                        continue

                new_files[rel] = ent
                self._emit(ent, rel, violations, ref_warnings)

        d['files'] = new_files
        if misses or new_files.keys() != old_files.keys():
            self.dirty = True
        has_history_entries = any(e.get('h') for e in new_files.values())
        tips = self._current_tips() if has_history_entries else None
        if tips != d['tips']:
            d['tips'] = tips
            self.dirty = True
        if self.dirty:
            self.store.save()
        return violations, ref_warnings

    def _emit(self, ent: dict, rel: str, violations: list, ref_warnings: list) -> None:
        schema = ent.get('n')
        if schema is None:
            return
        errors = list(ent.get('e', ())) + list(ent.get('r', ()))
        warnings = list(ent.get('w', ()))
        if self.strict:
            errors += warnings
            warnings = []
        for w in warnings:
            ref_warnings.append({'file': rel, 'schema': schema, 'warning': w})
        if errors:
            violations.append({'file': rel, 'schema': schema, 'errors': errors})


def run(repo_root: str, *, as_json: bool, strict_refs: bool) -> int:
    """Sweep every schema-governed record under `repo_root`; print the report; 1 when any violation, else 0."""
    violations, ref_warnings = _Sweep(repo_root, strict_refs).run()

    if as_json:
        print(json.dumps({'ok': len(violations) == 0, 'violations': violations, 'refWarnings': ref_warnings}, indent=2))
    else:
        if not violations:
            print(f'lint-frontmatter: all files valid (root: {repo_root})')
        else:
            print(f'lint-frontmatter: {len(violations)} violation(s) (root: {repo_root})\n')
            for v in violations:
                print(f"  {v['file']}  [{v['schema']}]")
                for e in v['errors']:
                    print(f"    - {e['field']}: {e['error']}")
                    if e.get('hint'):
                        print(f"      hint: {e['hint']}")
        if ref_warnings:
            print(f"\nlint-frontmatter: {len(ref_warnings)} dangling handoff-DAG ref warning(s) "
                  "(non-blocking; pass --strict-refs at a cadence gate to escalate)\n")
            for rw in ref_warnings:
                print(f"  {rw['file']}  [{rw['schema']}]")
                print(f"    - {rw['warning']['field']}: {rw['warning']['error']}")
                if rw['warning'].get('hint'):
                    print(f"      hint: {rw['warning']['hint']}")

    return 1 if violations else 0


def _parse_tree_argv(argv: list[str]) -> tuple[str | None, bool, bool] | None:
    """(root, as_json, strict_refs) for a whole-tree argv; None for any other shape."""
    root: str | None = None
    as_json = strict = False
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == '--root' and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
        elif tok == '--json':
            as_json = True
            i += 1
        elif tok == '--strict-refs':
            strict = True
            i += 1
        else:
            return None
    return root, as_json, strict


def main(argv: list[str]) -> int:
    """CLI entry: whole-tree argv runs here; every other argv goes to `schema_validate.main`."""
    parsed = _parse_tree_argv(argv)
    if parsed is None:
        from coordinator_core.frontmatter import schema_validate
        return schema_validate.main(argv)
    hint, as_json, strict = parsed
    if hint:
        root = str(Path(hint).resolve())
        if not os.path.isdir(root):
            print(f'lint-frontmatter: --root does not resolve to a directory: {root}', file=sys.stderr)
            return 2
    else:
        from coordinator_core.git import repo_root
        top = repo_root.show_toplevel()
        root = top.replace('\\', '/') if top else os.getcwd()
    return run(root, as_json=as_json, strict_refs=strict)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
