"""coordinator_core.ops.review_reachability — JSON-RPC "review.reachability".

The wired-up gate's compute-only check: for each entry point a diff adds (exported function, cli,
hook, api-route, page) is it referenced from production code, and does each declared click path
reach its action. The result is the `reachability-result` shape on stdout; nothing is written.

Cost shape: one `git diff -U0` spawn, one in-process index read, one bytes-regex scan over the
production code files.

Negative-spec:
  - Default exports are not symbols: an import may rename them, so a by-name search is unsound.
  - `unreachable` is returned for an exported function ONLY, and only when its name appears as a
    whole word in no other production file and no unmodelled registration surface (a barrel
    `export * from`, `__all__`, a manifest or settings file) could carry it. Everything the op
    does not model is `undecidable`, because a false `unreachable` fails a run.
  - A function whose name appears again in its own file beyond the definition is `undecidable`:
    same-file reachability is not modelled.
  - With declared writes (`plan` or `paths`), a diff file outside them is another run's and
    contributes no entry; with neither, every diff file counts.
  - An untracked referrer is scanned only when the plan declares it or it sits beside a changed file.
  - `doc` (markdown) and `op` (`@register_op` module) entries are default kinds. `cli`, `hook`,
    `doc` and `op` verdicts come from `reachability_docs` and `reachability_registration`, which read
    only the diff's added lines, the one shared `read_index` list, ancestor index files and named
    wiring surfaces, never the whole tree and never a second spawn.
  - `unreachable` is also returned for a hook body (in a repo holding a hook wiring surface), bin
    script, doc or op the range CREATED and nothing modelled names. A pre-existing hook or bin script
    stays `undecidable`: its wiring may live in another repo.
  - Click paths are traced on the Next.js app router only; any other stack is `undecidable`.
  - A middleware/proxy rewrite or redirect is not modelled: with one present, a step the trace
    cannot resolve is `undecidable`, never `broken`.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.frontmatter.primitives import read_fm_field_unquoted
from coordinator_core.git.git_state import read_index
from coordinator_core.git.run import run_git
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.plan_tasks_render import load_rows
from coordinator_core.ops.reachability_docs import doc_entries, doc_verdicts
from coordinator_core.ops.reachability_registration import registration_entries, registration_verdicts

_CODE_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".py", ".rs")
_JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs")
_OTHER_STACK_EXT = (".go", ".java", ".kt", ".cs", ".rb", ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".php")
_TEST_RE = re.compile(
    r"(\.test\.|\.spec\.|(^|/)tests?/|(^|/)__tests__/|(^|/)test_[^/]*\.py$|_test\.py$"
    r"|(^|/)fixtures?/|(^|/)node_modules/)"
)
_TS_EXPORT = re.compile(
    r"^export\s+(?:async\s+)?(?:function\*?|const|let)\s+([A-Za-z_$][\w$]*)", re.M
)
# A class is a type, not an entry point: its consumers reach it through the function that builds it.
_PY_DEF = re.compile(r"^(?:async\s+)?def\s+([A-Za-z]\w*)", re.M)
_RS_PUB = re.compile(r"^pub\s+(?:async\s+)?fn\s+(\w+)", re.M)
_ROUTE_VERBS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_ROUTE_FILE = re.compile(r"(^|/)app/(.*/)?route\.(ts|js)$")
_PAGE_FILE = re.compile(r"(^|/)app/(.*/)?page\.(tsx|jsx)$")
_CONVENTION_FILE = re.compile(
    r"(^|/)(app/(.*/)?(layout|loading|error|global-error|not-found|template|default|sitemap|robots"
    r"|manifest|opengraph-image|icon)\.\w+|(src/)?(middleware|proxy|instrumentation)\.\w+)$"
)
_BARREL = re.compile(rb"export\s*\*\s*(?:as\s+\w+\s+)?from\s*['\"]([^'\"]+)['\"]")
_MANIFESTS = (
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "hooks.json", "plugin.json",
    "manifest.json", ".claude/settings.json", ".claude/settings.local.json",
)
_ALL_ENTRY_KINDS = ("exported-function", "cli", "hook", "api-route", "page", "doc", "op")
_REGISTRATION_KINDS = ("cli", "hook", "op")
_WORD = re.compile(rb"[\w$]")

_LINK_RE = re.compile(r"""<Link[^>]*?href=\{?['"`]([^'"`]+)['"`]\}?[^>]*>([^<]*)<""")
_PUSH_RE = re.compile(r"""(?:router\.push|redirect)\(\s*['"`]([^'"`]+)['"`]""")
_IMPORT_RE = re.compile(r"""from\s+['"](\.{1,2}/[^'"]+)['"]""")
# A request-interception rewrite or redirect can serve any page at any URL, so a step the nav trace
# cannot resolve is undecidable once one exists. Next.js 16 renamed `middleware` to `proxy`.
_INTERCEPT_FILES = tuple(f"{d}{n}.{e}" for d in ("", "src/") for n in ("middleware", "proxy") for e in ("ts", "js"))
_INTERCEPT_RE = re.compile(r"NextResponse\.(?:rewrite|redirect)\(")


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _empty(error: str) -> dict:
    return {
        "entries": [], "click_paths": [], "summary": "reachability not computed",
        "cost": {}, "error": error[:300],
    }


def _diff_added(
    root: Path, base: str, head: Optional[str], worktree: bool
) -> Optional[tuple[dict[str, list[str]], set[str]]]:
    """({path: added lines}, {paths the range created}) from one `git diff -U0`; None when git refuses.

    A file header is the span from `diff --git` to the first `@@`; `--- /dev/null` counts only there,
    since a removed content line `-- /dev/null` is indistinguishable from it under -U0."""
    args = ["-C", str(root), "-c", "core.quotepath=off", "diff", "-U0", "--no-color",
            "--no-ext-diff", "--no-renames", base]
    if not worktree:
        args.append(head or "HEAD")
    res = run_git(args + ["--"])
    if res.returncode != 0:
        return None
    return parse_diff_added(res.stdout)


def diff_names(text: str) -> list[str]:
    """Every path a `git diff -U0 --no-renames` text names, from its `diff --git a/X b/X` headers
    (deletions and binary files included); a C-quoted header (a `"` or backslash in the path) is
    unquoted."""
    names: list[str] = []
    for ln in text.splitlines():
        if ln.startswith('diff --git "a/'):
            rest = ln[len("diff --git "):]
            cut = rest.find('" "b/')
            if cut > 0:
                names.append(re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n"}.get(m.group(1), m.group(1)), rest[3:cut]))
        elif ln.startswith("diff --git a/"):
            rest = ln[len("diff --git "):]
            n = (len(rest) - 5) // 2
            if n > 0 and rest[2 + n:5 + n] == " b/" and rest[2:2 + n] == rest[5 + n:]:
                names.append(rest[2:2 + n])
    return names


def parse_diff_added(text: str) -> tuple[dict[str, list[str]], set[str]]:
    """`_diff_added`'s parse of a caller-supplied `git diff -U0` text."""
    added: dict[str, list[str]] = {}
    created: set[str] = set()
    cur = None
    in_header = False
    from_null = False
    for ln in text.splitlines():
        if in_header:
            if ln.startswith("--- "):
                from_null = ln.startswith("--- /dev/null")
                continue
            if ln.startswith("+++ "):
                cur = ln[6:].split("\t")[0] if ln.startswith("+++ b/") else None
                if cur is not None:
                    added.setdefault(cur, [])
                    if from_null:
                        created.add(cur)
                continue
            if ln.startswith("@@"):
                in_header = False
            continue
        if ln.startswith("diff --git "):
            in_header, from_null, cur = True, False, None
        elif ln.startswith("+") and cur is not None:
            added[cur].append(ln[1:])
    return added, created


def _classify(rel: str, text: str) -> list[tuple[str, str]]:
    """[(kind, symbol)] for one changed non-test file; symbol '' when the stack is not modelled."""
    if rel.endswith(".rs"):
        return [("exported-function", s) for s in _RS_PUB.findall(text)] or [("exported-function", "")]
    if rel.endswith(_OTHER_STACK_EXT):
        return [("exported-function", "")]
    if rel.endswith(".py"):
        syms = [s for s in _PY_DEF.findall(text) if not s.startswith("_")]
    elif rel.endswith(_JS_EXT):
        syms = _TS_EXPORT.findall(text)
    else:
        return []
    if _ROUTE_FILE.search(rel):
        return [("api-route", s) for s in syms if s in _ROUTE_VERBS]
    if _PAGE_FILE.search(rel):
        return [("page", "")]
    if _CONVENTION_FILE.search(rel):
        return [("exported-function", "")]
    if re.search(r"(^|/)bin/", rel):
        return [("cli", Path(rel).stem)]
    if re.search(r"(^|/)hooks/", rel):
        return [("hook", Path(rel).stem)]
    return [("exported-function", s) for s in syms]


def _route_of(rel: str) -> tuple[str, bool]:
    m = re.search(r"app/(.*?)/?(?:page|route)\.\w+$", rel)
    seg = [s for s in (m.group(1).split("/") if m and m.group(1) else [])
           if not (s.startswith("(") and s.endswith(")"))]
    return "/" + "/".join(seg), any(s.startswith(("[", "@")) for s in seg)


def _scan(root: Path, extra: list[str], symbols: set[str], routes: set[str], sym_counts: dict, index: list[str]):
    """One pass over production code files -> (symbol hits, route-hit set, barrels).

    symbol hits: {symbol: {file}}; barrels: [(file, spec)] for `export * from`.
    `sym_counts` is filled with {(symbol, file): whole-word occurrences}, the definition included.
    `index` is the handler's single `read_index` result.
    """
    files = [p for p in index if p.endswith(_CODE_EXT) and not _TEST_RE.search(p)]
    seen = set(files)
    files += [e for e in dict.fromkeys(extra) if e not in seen and e.endswith(_CODE_EXT) and not _TEST_RE.search(e)]
    # Plain-substring pre-filter: a file holding no wanted name skips the regex engine entirely.
    sym_bytes = [s.encode() for s in symbols]
    route_bytes = [r.encode() for r in routes]
    sym_re = None
    if symbols:
        sym_re = re.compile(b"(" + b"|".join(re.escape(s.encode()) for s in sorted(symbols, key=len, reverse=True)) + b")")
    route_re = None
    if routes:
        route_re = re.compile(
            rb"""['"`](""" + b"|".join(re.escape(r.encode()) for r in sorted(routes, key=len, reverse=True))
            + rb""")(?:['"`/?#]|$)""", re.M
        )
    sym_hits: dict[str, set[str]] = {}
    route_hits: dict[str, set[str]] = {}
    barrels: list[tuple[str, str]] = []
    for f in files:
        try:
            b = (root / f).read_bytes()
        except OSError:
            continue
        if sym_re is not None:
            for m in (sym_re.finditer(b) if any(s in b for s in sym_bytes) else ()):
                i, j = m.span()
                if (i and _WORD.match(b, i - 1)) or _WORD.match(b, j):
                    continue
                name = m.group(1).decode()
                sym_hits.setdefault(name, set()).add(f)
                sym_counts[(name, f)] = sym_counts.get((name, f), 0) + 1
            if b"export" in b:
                barrels += [(f, m.group(1).decode("utf-8", "replace")) for m in _BARREL.finditer(b)]
        if route_re is not None and any(r in b for r in route_bytes):
            for m in route_re.finditer(b):
                route_hits.setdefault(m.group(1).decode(), set()).add(f)
    return sym_hits, route_hits, barrels


def _manifest_text(root: Path) -> str:
    return "\n".join(_read(root, m) for m in _MANIFESTS)


def _verdicts(root, found, sym_hits, route_hits, barrels, manifest_text, sym_counts, resolved=None) -> list[dict]:
    """`resolved` carries {(kind, sym, rel): verdict} from the doc and registration resolvers."""
    ents = []
    manifest = None
    resolved = resolved or {}
    for kind, sym, rel in found:
        ref = f"{rel}:{sym}" if sym else rel
        if (kind, sym, rel) in resolved:
            v = resolved[(kind, sym, rel)]
        elif kind == "exported-function" and sym and rel.endswith(_CODE_EXT[:-1]):
            if sym_hits.get(sym, set()) - {rel}:
                v = "reachable"
            elif sym_counts.get((sym, rel), 0) > 1:
                v = "undecidable"
            else:
                stem = Path(rel).stem
                if manifest is None:
                    manifest = manifest_text()
                text = _read(root, rel)
                if (
                    any(Path(spec.rstrip("/")).name == stem for _, spec in barrels)
                    or re.search(r"^__all__\b", text, re.M)
                    or re.search(r"(?<![\w$])" + re.escape(sym) + r"(?![\w$])", manifest)
                    or re.search(r"(?<![\w$])" + re.escape(stem) + r"(?![\w$])", manifest)
                ):
                    v = "undecidable"
                else:
                    v = "unreachable"
        elif kind == "api-route":
            route, dynamic = _route_of(rel)
            v = "reachable" if not dynamic and route_hits.get(route, set()) - {rel} else "undecidable"
        elif kind == "page":
            route, dynamic = _route_of(rel)
            v = "reachable" if not dynamic and route_hits.get(route, set()) - {rel} else "undecidable"
        else:
            v = "undecidable"
        ents.append({"kind": kind, "ref": ref[:300], "verdict": v})
    return ents


def _resolve_import(root: Path, frm: str, spec: str) -> Optional[str]:
    base = Path(os.path.normpath(str(Path(frm).parent / spec))).as_posix()
    for c in (base, base + ".tsx", base + ".ts", base + "/index.tsx"):
        if (root / c).is_file():
            return c
    return None


def _chain(root: Path, start: Optional[str], depth: int = 2) -> list[str]:
    seen: list[str] = []
    front = [start]
    for _ in range(depth + 1):
        nxt = []
        for f in front:
            if not f or f in seen:
                continue
            seen.append(f)
            nxt += [_resolve_import(root, f, s) for s in _IMPORT_RE.findall(_read(root, f))]
        front = nxt
    return seen


def _page_for(root: Path, href: str) -> Optional[str]:
    parts = [s for s in href.split("?")[0].strip("/").split("/") if s]
    for prefix in ("app/", "src/app/"):
        c = prefix + "/".join(parts + ["page.tsx"])
        if (root / c).is_file():
            return c
    return None


def _trace_click_path(root: Path, role: str, steps: list[str]) -> dict:
    layout = next((c for c in ("app/layout.tsx", "src/app/layout.tsx") if (root / c).is_file()), None)
    if layout is None or not steps:
        return {"role": role, "verdict": "undecidable", "broken_at": None}
    front = _chain(root, layout)
    arrival: set[str] = set()
    for i, step in enumerate(steps[1:], 1):
        s = step.lower()
        if s in arrival:
            arrival = set()
            continue
        found, nxt = False, front
        for f in front:
            txt = _read(root, f)
            for href, label in _LINK_RE.findall(txt):
                if label.strip().lower() == s or href.rstrip("/").split("/")[-1].lower() == s:
                    pg = _page_for(root, href)
                    found, nxt = True, (_chain(root, pg) if pg else [])
                    arrival = {x.lower() for x in href.strip("/").split("/")}
            for href in _PUSH_RE.findall(txt):
                if href.rstrip("/").split("/")[-1].lower() == s:
                    pg = _page_for(root, href)
                    found, nxt = True, (_chain(root, pg) if pg else [])
            if not found and re.search(r">\s*" + re.escape(step) + r"\s*<", txt, re.I):
                found, nxt = True, front
            if found:
                break
        if not found:
            if any(_INTERCEPT_RE.search(_read(root, f)) for f in _INTERCEPT_FILES):
                return {"role": role, "verdict": "undecidable", "broken_at": None}
            where = "nav" if i == 1 else steps[i - 1]
            return {"role": role, "verdict": "broken", "broken_at": f"{where}: no {step} entry"[:300]}
        front = nxt
    return {"role": role, "verdict": "reachable", "broken_at": None}


def _plan_inputs(root: Path, plan_raw: str) -> tuple[list[str], list[dict]]:
    """(spine `writes`, sizing-object `exit_criterion.click_paths`) for a plan path."""
    p = contained_path(root / plan_raw if not Path(plan_raw).is_absolute() else Path(plan_raw), [root])
    if p is None or not p.is_file():
        raise FileNotFoundError(plan_raw)
    text = p.read_text(encoding="utf-8", errors="replace")
    writes: list[str] = []
    for row in load_rows(text).rows:
        w = row.get("writes") or []
        writes += [str(x).replace("\\", "/").removeprefix("./") for x in (w if isinstance(w, list) else [w])]
    clicks: list[dict] = []
    so = read_fm_field_unquoted(text, "sizing_object")
    if so and so != "null":
        sp = contained_path(root / so, [root])
        if sp is not None and sp.is_file():
            stext = sp.read_text(encoding="utf-8", errors="replace")
            try:
                doc = yaml.safe_load(stext)
            except yaml.YAMLError:
                doc = None
            if not isinstance(doc, dict) and stext.startswith("---"):
                try:
                    doc = yaml.safe_load(stext.split("---", 2)[1])
                except (yaml.YAMLError, IndexError):
                    doc = None
            ec = (doc or {}).get("exit_criterion") if isinstance(doc, dict) else None
            clicks = (ec or {}).get("click_paths") or [] if isinstance(ec, dict) else []
    return writes, [c for c in clicks if isinstance(c, dict)]


def _summary(ents: list[dict], cps: list[dict]) -> str:
    def counts(items, verdicts):
        return [(sum(1 for i in items if i["verdict"] == v), v) for v in verdicts]
    parts = [f"{n} {'entry point' if n == 1 else 'entry points'} {v}" for n, v in
             counts(ents, ("reachable", "unreachable", "undecidable")) if n]
    parts += [f"{n} click {'path' if n == 1 else 'paths'} {v}" for n, v in
              counts(cps, ("reachable", "broken", "undecidable")) if n]
    return (", ".join(parts) or "no entry points or click paths in the diff")[:300]


@register_op("review.reachability")
def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """Params: `repo_root`, `base_sha` (required); `worktree`, `plan`, `paths`, `head_sha`,
    `click_paths`, `entry_kinds`, `diff_text` (optional; a `git diff -U0 --no-renames` the caller
    already ran, which replaces this op's own git spawn). Returns the `reachability-result` shape; any failure
    is the same shape with empty lists and `error` set."""
    t0, cpu0 = time.perf_counter(), time.process_time()
    root_raw = params.get("repo_root") or (str(repo_root) if repo_root else "")
    base = (params.get("base_sha") or "").strip()
    if not root_raw or not base:
        return _empty("review.reachability requires repo_root and base_sha")
    root = Path(root_raw)
    if not root.is_dir():
        return _empty(f"repo_root is not a directory: {root_raw}")
    worktree = bool(params.get("worktree"))
    kinds = set(params.get("entry_kinds") or _ALL_ENTRY_KINDS)

    writes: list[str] = []
    plan_clicks: list[dict] = []
    if params.get("plan"):
        try:
            writes, plan_clicks = _plan_inputs(root, str(params["plan"]))
        except OSError:
            return _empty(f"plan not readable: {params['plan']}")
    if params.get("paths"):
        writes = [str(x).replace("\\", "/").removeprefix("./") for x in params["paths"]]
    clicks = params.get("click_paths") if params.get("click_paths") is not None else plan_clicks

    supplied = params.get("diff_text")
    diffed = (parse_diff_added(supplied) if isinstance(supplied, str)
              else _diff_added(root, base, params.get("head_sha"), worktree))
    if diffed is None:
        return _empty(f"git diff failed against base_sha {base}")
    added, created = diffed
    # An untracked declared write is invisible to `git diff`; a tracked one absent from the diff
    # added nothing in this range, so its pre-existing symbols are not this run's entry points.
    index_box: list[list[str]] = []

    def _index() -> list[str]:
        if not index_box:
            index_box.append(read_index(root))
        return index_box[0]

    tracked = None
    for w in writes:
        if w in added or not (root / w).is_file():
            continue
        if tracked is None:
            tracked = set(_index())
        if w not in tracked:
            added[w] = _read(root, w).splitlines()
            created.add(w)

    declared = set(writes) if writes else None

    def _declared(rel: str) -> bool:
        return rel in declared or any(
            fnmatch.fnmatchcase(rel, w) or (w.endswith("/") and rel.startswith(w)) for w in declared
        )

    found: list[tuple[str, str, str]] = []
    for rel, lines in added.items():
        if _TEST_RE.search(rel) or (declared is not None and not _declared(rel)):
            continue
        text = "\n".join(lines)
        entries = _classify(rel, text) + doc_entries(rel) + registration_entries(rel, text)
        for kind, sym in dict.fromkeys(entries):
            if kind in kinds:
                found.append((kind, sym, rel))

    symbols = {s for k, s, r in found if k == "exported-function" and s and r.endswith(_CODE_EXT[:-1])}
    routes = {_route_of(r)[0] for k, s, r in found if k in ("api-route", "page")}
    sibling_dirs = {str(Path(r).parent) for _, _, r in found}
    for d in sibling_dirs:
        try:
            writes += [(Path(d) / n).as_posix() for n in os.listdir(root / d) if n.endswith(_CODE_EXT)]
        except OSError:
            pass
    sym_counts: dict[tuple[str, str], int] = {}
    sym_hits, route_hits, barrels = (
        ({}, {}, []) if not (symbols or routes) else _scan(root, writes, symbols, routes, sym_counts, _index())
    )
    resolved: dict[tuple[str, str, str], str] = {}
    doc_rels = [r for k, _, r in found if k == "doc"]
    reg_found = [f for f in found if f[0] in _REGISTRATION_KINDS]
    if doc_rels:
        resolved.update({("doc", "", r): v for r, v in doc_verdicts(root, doc_rels, added, _index()).items()})
    if reg_found:
        resolved.update(registration_verdicts(root, reg_found, added, _index(), new=frozenset(created)))
    ents = _verdicts(root, found, sym_hits, route_hits, barrels, lambda: _manifest_text(root), sym_counts, resolved)

    touches_next = any(
        re.match(r"(src/)?app/", r) or r.endswith((".tsx", ".ts")) for _, _, r in found
    ) and ((root / "app").is_dir() or (root / "src" / "app").is_dir())
    cps = []
    for c in clicks:
        role = str(c.get("role", ""))[:200]
        steps = [str(s) for s in (c.get("steps") or [])]
        cps.append(_trace_click_path(root, role, steps) if touches_next
                   else {"role": role, "verdict": "undecidable", "broken_at": None})

    return {
        "entries": ents,
        "click_paths": cps,
        "summary": _summary(ents, cps),
        "cost": {
            "process_ms": round((time.process_time() - cpu0) * 1000, 1),
            "wall_ms": round((time.perf_counter() - t0) * 1000, 1),
            "spawns": 1,
        },
        "error": None,
    }
