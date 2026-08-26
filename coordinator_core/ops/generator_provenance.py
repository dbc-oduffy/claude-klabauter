"""
coordinator_core.ops.generator_provenance — discovers generator modules by
write behaviour and reads their declared `GENERATES` provenance.

Purpose: a generator's output going stale unnoticed is a coverage problem
before it is a staleness problem — you cannot ask "which generators failed
to declare their outputs?" by reading artifacts, because an undeclared
generator's artifact is exactly the one you never find. This module answers
it from the generator side: sweep for modules that actually write files,
read whatever provenance they declare, and report the ones that declare
nothing as `Verdict.UNDECLARED` BY NAME rather than silently skipping them.

AC1 — discovery is by write behaviour, parsed from the AST, never a
filename-family match (`emit|generat|regenerat`-style name matching hits
roughly sixty non-test modules against about five real declarations, and
misses real emitters like `write_surface_manifest.py`,
`render_template_tree.py`, `deliverable_ledger_write.py`; its survival
response is `GENERATES = []` pasted everywhere, the forbidden
hand-maintained registry respelled as boilerplate). A module is a generator
iff its source contains a write to a repo-relative path: `Path.write_text`,
`Path.open(..., "w")`, builtin `open(..., "w"/"a")`, `os.fdopen(..., "w")`,
or `json.dump` to a non-tmp target.

A module-level `GENERATES` declaration is AUTHORITATIVE independent of
write-behaviour detection: a declared generator's pairs resolve whether or
not `_module_is_generator` recognises its write, because a declaration is a
strictly stronger signal than an inferred one and is what makes a thin CLI
shim declarable at all (a delegating shim's write is invisible to any
write-behaviour sweep). Write-behaviour discovery keeps its narrower job —
finding writers that FAILED to declare — for modules carrying no
`GENERATES`.

For those undeclared candidates, a write only counts if its literal write
target resolves to a path `git ls-files` reports as tracked (resolved once
per sweep, not per module); a write whose target cannot be determined
statically (a variable, not a literal) is still counted, since the sweep
cannot rule it in or out. This keeps a module that only ever touches a
runtime cache, state file, or log out of the UNDECLARED tally — noise that
otherwise incentivises pasting `GENERATES = []` everywhere, the
hand-maintained-registry failure mode this module exists to avoid. Narrowing
is by tracked-path resolution, plus one fail-open exemption: a module the
repo's own pytest `python_files` config declares to be a test is exempted
from the unresolved-write basis only, never from a resolved tracked write,
because a test module's writes are fixture writes, not repo artifacts.

Nine mechanical narrowing rules cut WRITE_TARGET_UNRESOLVED false positives,
none of them by deleting a write site — a tmp/derivative rule always
SUBSTITUTES the real destination or leaves the site counted exactly as
before; nothing here removes a genuine write from the swept population:

  - R1 (stdout/stderr/in-memory sinks): a `json.dump` whose file argument is
    `sys.stdout`, `sys.stderr`, or a direct `io.StringIO()`/`io.BytesIO()`
    call is not a file write and contributes no signal.
  - R2 (derivative json.dump): a `json.dump` whose file argument is a Name
    bound in the same module by an `open`/`Path.open`/`os.fdopen` call is not
    an independent write site — it is still counted via that underlying
    open/fdopen call, which fires on its own.
  - R3 (atomic-write idiom, follow to destination): a `tempfile.mkstemp(...)`
    tuple-assignment marks its path variable as a temp handle; a later
    `os.replace(<that var>, dest)` / `Path(<that var>).replace(dest)` is
    resolved and classified as if `dest` were the original write target
    (tracked-path resolution, same as any other target). The temp-handle
    write itself is never dropped — if the destination cannot be resolved to
    a literal, the module keeps whatever basis the temp write already
    established.
  - R4 (`.tmp`-sibling f-string): a target built as an f-string whose first
    interpolated value is a base expression followed by a `.tmp`-shaped
    literal suffix (e.g. `f"{artifact_path}.tmp.{os.getpid()}"`) is resolved
    by substituting the base expression and classifying THAT, never by
    deleting the site.
  - R5 (bases provably outside the tracked tree): a write target rooted in
    `tempfile.gettempdir`/`mkdtemp`/`mkstemp`/`TemporaryDirectory`/
    `NamedTemporaryFile`, a pytest `tmp_path`/`tmp_path_factory` fixture
    Name, `sessions_dir(...)`, `git_common_dir(...)`, `Path.home()`, or
    `os.devnull` cannot produce a tracked repo artifact and is excluded from
    the population entirely (neither tracked nor unresolved). `os.devnull` is
    R1's discard-sink class reached through the target position rather than a
    `json.dump` file argument.
  - R6 (test-scaffolding exemption, extended): the existing `python_files`
    glob exemption is extended, by directory shape rather than basename, to
    `conftest.py`, any module under a `tests/` directory, and any module
    under `coordinator_core/testing/` or `coordinator_core/benchmarks/`.
  - R7 (scratch mkstemp descriptor): `os.fdopen(fd, "w")` where `fd` is the
    descriptor half of a `fd, path = tempfile.mkstemp(...)` unpack whose
    `path` half is NEVER promoted by a later `replace` (aliases followed).
    R5's criterion applied to the one shape R5 cannot read: an integer.
    Scoped to the un-promoted case precisely so R3's contract above holds —
    a temp file that goes on to BECOME the artifact keeps its site counted,
    unresolved destination and all.
  - R8 (single-assignment local names): a write target that is still a bare
    Name after R4 is substituted with its bound expression when that name is
    assigned exactly once in the innermost function containing the write and
    is not one of that function's parameters, then re-classified by R5 and
    the literal check. R5's exclusions are written against expressions, so
    without this they cover only writes that inline their own base:
    `p = os.path.join(tempfile.gettempdir(), ...)` then `open(p, "w")` was
    unresolved despite being provably a temp write. Scope-correctness is
    load-bearing, not tidiness — see `_ScopeBindings`.
  - R9 (`os.devnull`): the kernel's bit bucket is not a path in any tree.
    Same standing as R1's stdio sinks.

AC6 — discovery reads source text only, via `ast.parse`; it never imports a
swept module. This is also what keeps
`coordinator_core/contract/cockpit_schema/emit_schema.py` — DoE's frozen
release path — from being *executed* by discovery.

AC1 (declaration half) — a discovered generator carrying no `GENERATES` is
reported by name; silence is the failure mode being designed out. A
resolved-tracked write reports `Verdict.UNDECLARED` — a confirmed
undeclared repo-artifact write. An unresolved write reports
`Verdict.WRITE_TARGET_UNRESOLVED` — a write whose destination the sweep could not
determine, so it is not known to be a repo artifact at all. `GENERATES = []`
is DECLARED-EMPTY (a genuine non-emitter, e.g. one that writes only to
stdout), distinct from either, and is the explicit way to say so at the
site rather than by absence.

AC10 — a declared pair whose `sources` is empty, is not a list, or names a
path absent from the repo resolves to `Verdict.UNDECLARED`, never to
something a caller would read as FRESH via a silently-empty since-range.
`check_import_budget_staleness.py` carries a review comment about this exact
failure mode on `measured_paths`; `sources` here is the same shape of
hand-written pathspec and gets the same loud treatment.

A module-level `MUTATES` declaration is a third, distinct provenance form
for a class `GENERATES` cannot express: a corpus MUTATOR that rewrites
however many tracked files currently match a data-dependent predicate (a
fixer that rewrites whichever tracked files contain a stale absolute path, a
normalizer that rewrites whichever records match a schema shape) rather than
emitting a fixed set of artifacts. `GENERATES` takes concrete artifact paths
with a `stamp_key` and `sources` because it exists to support staleness
computation — an artifact is stale when it is older than its sources — and
that question is undefined for a mutator: there is no fixed artifact and no
fixed sources for it to be stale relative to. `MUTATES` is reached for
instead of `GENERATES` precisely when the output set is NOT knowable and
fixed ahead of time; when it is, `GENERATES` is required and `MUTATES` is
the wrong tool. `MUTATES` is a non-empty list of non-empty glob-style
pathspecs naming the tracked files a module rewrites; it is validated the
same way `sources` is (AC10 loudness, never silent) — malformed, empty, a
pattern set that matches nothing currently tracked, a pattern carrying no
literal (non-wildcard) directory segment and no literal file extension
(`*`, `**`, `**/*`, `*/*` — a catch-all that silences the module completely,
`GENERATES = []` in disguise), or a pattern carrying no wildcard
metacharacter at all (`*`, `?`, `[`) — a concrete filename in pattern's
clothing, matching exactly one tracked path and naming a genuine fixed
artifact that owes `GENERATES`, not `MUTATES` — resolves to
`Verdict.UNDECLARED`, never to something read as fine. A valid `MUTATES` is
DECLARED and bypasses write-behaviour discovery exactly as `GENERATES`
does. It is not exclusive with `GENERATES` — a module may emit a stamped
artifact AND separately mutate a corpus, and both declarations are honoured
independently. A module declaring `MUTATES` with no `GENERATES` reports
`Verdict.MUTATES_DECLARED`: a declared, healthy state that carries no
staleness contract at all, distinct from `UNDECLARED`,
`WRITE_TARGET_UNRESOLVED`, and the empty-pairs `GENERATES = []` case.

Negative-spec:
  - This module does not add or edit any generator's `GENERATES` list —
    that is C2's write set. Its own tests use synthetic fixture modules
    under `tmp_path`, never the real ones.
  - This module does not author `staleness_git.py` (C0's file) — it imports
    `Verdict` and `git_root` from it and does not reimplement or shadow
    either.
  - This module is not a central registry of generator/artifact pairs. The
    hand-maintained pair list is the failure class this detector exists to
    catch; discovery is always re-derived from the swept source, never
    cached into a file this module then trusts instead of re-sweeping.
  - This module does not compute freshness/staleness itself (no `git log`,
    no since-range walk) — that is C3/C6's `check_generator_output_staleness.py`.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from coordinator_core.ops.staleness_git import Verdict

_SWEEP_DIRS = ("coordinator/bin", "bin", "coordinator_core")

_TMP_MARKERS = ("tmp", "temp", "tempfile", "/tmp/", "\\tmp\\")

_DEFAULT_TEST_MODULE_GLOBS = ("test_*.py",)


def _test_module_globs(repo_root: Path) -> tuple[str, ...]:
    """Resolve the repo's declared pytest `python_files` globs, once per sweep.

    Read from `[tool.pytest.ini_options] python_files` in `pyproject.toml` —
    the same file the repo's own suite is collected by — rather than a
    hardcoded literal, so this stays in lockstep with whatever the repo
    actually declares. Falls back to `("test_*.py",)` on any absence or
    parse failure: a malformed `pyproject.toml` must not crash discovery.
    """
    pyproject_path = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return _DEFAULT_TEST_MODULE_GLOBS

    python_files = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("python_files")
    if not isinstance(python_files, list) or not python_files:
        return _DEFAULT_TEST_MODULE_GLOBS
    if not all(isinstance(glob, str) and glob for glob in python_files):
        return _DEFAULT_TEST_MODULE_GLOBS
    return tuple(python_files)


_TEST_SCAFFOLDING_DIR_NAMES = ("tests", "testing", "benchmarks")


def _is_test_module(rel_path: str, test_module_globs: tuple[str, ...]) -> bool:
    """True when *rel_path* is exempted from the unresolved-write basis.

    Two independent bases, kept visibly separate rather than folded into one
    glob list:

      - the repo's own pytest `python_files` glob (e.g. `test_*.py`), matched
        against the basename only, as before R6;
      - a directory-shape exemption (R6): `conftest.py` by basename, or any
        module living under a `tests/`, `coordinator_core/testing/`, or
        `coordinator_core/benchmarks/` directory. This is deliberately NOT
        merged into `test_module_globs` — it does not come from
        `pyproject.toml` and must read as a separate rule at the call site.
    """
    basename = PureWindowsPath(rel_path).name
    if any(fnmatch.fnmatch(basename, glob) for glob in test_module_globs):
        return True
    if basename == "conftest.py":
        return True
    parts = PureWindowsPath(rel_path).parts
    return any(part in _TEST_SCAFFOLDING_DIR_NAMES for part in parts[:-1])


@dataclass(frozen=True)
class Pair:
    generator: str
    artifact: str
    stamp_key: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class GeneratorRecord:
    generator: str
    pairs: tuple[Pair, ...]
    verdict: Verdict | None
    detail: str
    mutates: tuple[str, ...] = ()


def _looks_like_tmp(target: str) -> bool:
    lowered = target.lower()
    return any(marker in lowered for marker in _TMP_MARKERS)


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _write_target_expr(call: ast.Call) -> ast.AST | None:
    """Return the AST expression naming a write call's target, unresolved.

    The target's location depends on call shape:
      - `Path(<expr>).write_text(...)` / `Path(<expr>).open(..., "w")`
        (receiver form): the expression lives in the RECEIVER call's args[0]
        (`func.value`), never in this call's own args.
      - builtin `open(<expr>, "w")`: the expression is this call's own
        args[0].
      - `os.fdopen(fd, "w")`: args[0] is a file descriptor, never a path —
        must not be read as a target.
      - `json.dump(obj, <file-ish>)`: no target path argument exists here;
        handled separately by the R1/R2 file-argument checks.
    Returns None when no target expression is recoverable at all; callers
    treat that as an unresolved (still-counted) write, never as "no write
    occurred".
    """
    func = call.func

    if isinstance(func, ast.Attribute):
        if func.attr in ("write_text", "open"):
            receiver = func.value
            if isinstance(receiver, ast.Call) and receiver.args:
                return receiver.args[0]
            if isinstance(receiver, ast.Call):
                return None
            # Not a bare `Path(<expr>)` call -- e.g. `(Path(x) / "y")`, a
            # `BinOp`/`Compare` receiver chain. No literal target lives here,
            # but R5's exclusion check still needs the whole expression to
            # look for a `tempfile.*`/`sessions_dir()`/`Path.home()` root.
            return receiver
        return None

    if isinstance(func, ast.Name) and func.id == "open" and call.args:
        return call.args[0]

    return None


def _is_write_mode(node: ast.AST) -> bool:
    value = _str_const(node)
    return value is not None and ("w" in value or "a" in value)


def _json_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Names the module binds to the `json` module object and to `json.dump`
    itself via `import ... as` / `from json import ...`, so `_call_is_write`
    can match `import json as j; j.dump(...)` and `from json import dump;
    dump(...)`, not only the literal spellings `json` and `json.dump`.
    # Review: coordinator:code-reviewer — P3, json.dump detection missed the
    # aliased-import and bare-name-import call shapes.
    """
    module_aliases: set[str] = set()
    dump_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "json":
                for alias in node.names:
                    if alias.name == "dump":
                        dump_names.add(alias.asname or alias.name)
    return frozenset(module_aliases), frozenset(dump_names)


def _call_is_write(
    call: ast.Call,
    json_module_aliases: frozenset[str] = frozenset({"json"}),
    json_dump_names: frozenset[str] = frozenset(),
) -> bool:
    """Detect a write-mode call.

    The Attribute-form `open`/`fdopen` branch must distinguish two call
    shapes that share an attribute name but not an argument layout:
      - `os.fdopen(fd, "w")` — a module-function call with both args
        explicit; the mode lives at `args[1]`.
      - `<receiver>.open("w", ...)` — a bound-method call with an implicit
        receiver; the mode lives at `args[0]`, and `args[1:]` is empty.
    Treating both as `args[1:]` silently misses every bound `.open()` write
    (e.g. `hooks_json_path.open("w", encoding="utf-8")` in `doctor.py`).
    # Review: coordinator:code-reviewer — P1, confirmed live impact on
    # doctor.py:406, invisible to discover_generators before this fix.
    """
    func = call.func

    if isinstance(func, ast.Attribute):
        attr = func.attr
        if attr == "write_text":
            return True
        if attr in ("open", "fdopen"):
            receiver = func.value
            is_os_fdopen = (
                attr == "fdopen" and isinstance(receiver, ast.Name) and receiver.id == "os"
            )
            args_to_check = call.args[1:] if is_os_fdopen else call.args[0:]
            for arg in args_to_check:
                if _is_write_mode(arg):
                    return True
            for kw in call.keywords:
                if kw.arg == "mode" and _is_write_mode(kw.value):
                    return True
            return False
        if attr == "dump":
            value_expr = getattr(func, "value", None)
            if isinstance(value_expr, ast.Name) and value_expr.id in json_module_aliases:
                return True
            return False
        return False

    if isinstance(func, ast.Name):
        if func.id in ("open", "fdopen"):
            for arg in call.args[1:]:
                if _is_write_mode(arg):
                    return True
            for kw in call.keywords:
                if kw.arg == "mode" and _is_write_mode(kw.value):
                    return True
            return False
        if func.id in json_dump_names:
            return True

    return False


def _tracked_paths(repo_root: Path) -> frozenset[str] | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return frozenset(
        PureWindowsPath(line.strip()).as_posix()
        for line in result.stdout.splitlines()
        if line.strip()
    )


def _normalize_target(target: str) -> str:
    """Reduce a source-literal write target to a repo-relative POSIX-shaped key.

    `PureWindowsPath` rather than `PurePath` because the literal comes from swept
    source that may spell either separator regardless of the platform doing the
    sweeping; the Windows flavour accepts both, the POSIX flavour treats a
    backslash as an ordinary filename character. Sharp edge, accepted: a bare
    colon in a non-drive-letter position (e.g. a URL- or timestamp-shaped
    literal coincidentally passed as a write target) can be misparsed by the
    Windows path grammar; no live occurrence found, and the deliberate
    either-separator acceptance above is the correct tradeoff to keep.
    """
    normalized = PureWindowsPath(target).as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


_OPEN_LIKE_ATTRS = ("open", "fdopen")


def _is_open_like_call(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _OPEN_LIKE_ATTRS:
        return True
    if isinstance(func, ast.Name) and func.id in _OPEN_LIKE_ATTRS:
        return True
    return False


def _handle_names(tree: ast.AST) -> frozenset[str]:
    """Collect names bound to an `open`/`Path.open`/`os.fdopen` call result.

    R2: a `json.dump` whose file argument is one of these names is a
    derivative write of the handle's own already-counted open/fdopen call,
    not an independent site.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.withitem) and isinstance(node.optional_vars, ast.Name):
            call = node.context_expr
            if isinstance(call, ast.Call) and _is_open_like_call(call):
                names.add(node.optional_vars.id)
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Call) and _is_open_like_call(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return frozenset(names)


def _is_stdio_sink(node: ast.AST) -> bool:
    """True for `sys.stdout`, `sys.stderr`, or a direct `StringIO()`/`BytesIO()` call (R1)."""
    if isinstance(node, ast.Attribute) and node.attr in ("stdout", "stderr"):
        value = node.value
        if isinstance(value, ast.Name) and value.id == "sys":
            return True
        return False
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        return name in ("StringIO", "BytesIO")
    return False


def _dump_file_arg(call: ast.Call) -> ast.AST | None:
    if len(call.args) > 1:
        return call.args[1]
    for kw in call.keywords:
        if kw.arg == "fp":
            return kw.value
    return None


_TMP_SIBLING_MARKERS = ("tmp", "temp")


def _tmp_var_info(tree: ast.AST) -> tuple[dict[str, ast.AST], frozenset[str]]:
    """Track candidate "temp handle" variables for R3/R4.

    Two independent sources feed the same map, both keyed by variable name:

      - R3: `fd, tmp_name = tempfile.mkstemp(...)` — both tuple-unpacked
        names are recorded in `mkstemp_names`; the module has no literal
        base to substitute (the real target only appears at the later
        `os.replace`/`Path.replace` call, handled by `_replace_destination_exprs`).
      - R4: `tmp_name = f"{base}.tmp.{os.getpid()}"` — an f-string whose
        first interpolated value is a base expression, followed by a
        `.tmp`-shaped literal tail. `tmp_name` maps to that base expression
        directly in `bases`, so any later reference to `tmp_name` (as a
        write target or a `replace` source) can substitute the base instead
        of giving up unresolved.
    """
    bases: dict[str, ast.AST] = {}
    mkstemp_names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        value = node.value

        if (
            isinstance(target, ast.Tuple)
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "mkstemp"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "tempfile"
        ):
            for elt in target.elts:
                if isinstance(elt, ast.Name):
                    mkstemp_names.add(elt.id)
            continue

        if isinstance(target, ast.Name) and isinstance(value, ast.JoinedStr):
            substituted = _substitute_tmp_sibling(value)
            if substituted is not None:
                bases[target.id] = substituted

    return bases, frozenset(mkstemp_names)


def _substitute_tmp_sibling(joined: ast.JoinedStr) -> ast.AST | None:
    """R4: for a `.tmp`-sibling f-string, return the base expression.

    Only fires when the first interpolated value is the base and the
    remaining literal text (all `ast.Constant` string parts) contains a tmp
    marker — matching the real idiom `f"{base}.tmp.{os.getpid()}"`, never a
    bare `f"{base}"` with no tmp shape at all.
    """
    if not joined.values or not isinstance(joined.values[0], ast.FormattedValue):
        return None
    literal_tail = "".join(
        part.value
        for part in joined.values
        if isinstance(part, ast.Constant) and isinstance(part.value, str)
    )
    if not _looks_like_tmp(literal_tail):
        return None
    return joined.values[0].value


_EXCLUDED_TEMPFILE_ATTRS = (
    "gettempdir",
    "mkdtemp",
    "mkstemp",
    "TemporaryDirectory",
    "NamedTemporaryFile",
)
_EXCLUDED_HELPER_CALL_NAMES = ("sessions_dir", "git_common_dir")
_EXCLUDED_FIXTURE_NAMES = ("tmp_path", "tmp_path_factory")


def _is_excluded_base(node: ast.AST) -> bool:
    """R5: True when *node* is provably rooted outside the tracked tree.

    Walks the whole expression subtree (not just its outermost node) so a
    composed target like `Path(sessions_dir()) / "sub"` or
    `tempfile.gettempdir() + "/x"` is caught, not only a bare call.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Attribute):
                if (
                    func.attr in _EXCLUDED_TEMPFILE_ATTRS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "tempfile"
                ):
                    return True
                if (
                    func.attr == "home"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "Path"
                ):
                    return True
            elif isinstance(func, ast.Name) and func.id in _EXCLUDED_HELPER_CALL_NAMES:
                return True
        elif (
            isinstance(sub, ast.Attribute)
            and sub.attr == "devnull"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "os"
        ):
            # A discard sink, not a destination -- the same class R1 already
            # excludes for `sys.stdout`/`StringIO`, reached through the target
            # position instead of a `json.dump` file argument
            # (`open(os.devnull, "w")` in `coordinator_core/warm/server.py`).
            return True
        elif isinstance(sub, ast.Name) and sub.id in _EXCLUDED_FIXTURE_NAMES:
            return True
        elif (
            isinstance(sub, ast.Attribute)
            and sub.attr == "devnull"
            and isinstance(sub.value, ast.Name)
            and sub.value.id == "os"
        ):
            # R9: `os.devnull` is the kernel's bit bucket, not a path in any
            # tree. Same standing as R1's stdio sinks -- a write there cannot
            # produce an artifact, tracked or otherwise.
            return True
    return False


class _ScopeBindings:
    """R8's per-scope single-assignment index.

    Scope-correct by construction: a name is looked up ONLY in the innermost
    function containing the write site, and a name that is a PARAMETER of
    that function is never substituted. A flat module-wide map gets this
    wrong in a way that silently manufactures findings -- `path` assigned
    once in one helper would be substituted into a different helper whose
    own `path` is a parameter, and the sweep would then report that second
    helper as writing whatever the first one wrote. Measured live on
    `coordinator_core/tests/test_review_brightline_gate.py`, which a flat map
    reported as an UNDECLARED writer of a production module it only ever
    reads.
    """

    def __init__(self, tree: ast.AST) -> None:
        self._scopes: list[tuple[int, int, dict[str, ast.AST]]] = []
        self._index_scope(tree, is_module=True)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._index_scope(node, is_module=False)

    @staticmethod
    def _own_body_nodes(scope: ast.AST):
        """Walk *scope*'s own body without descending into nested function or
        class bodies -- a nested scope's assignments are not this scope's."""
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            yield node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            stack.extend(ast.iter_child_nodes(node))

    def _index_scope(self, scope: ast.AST, is_module: bool) -> None:
        counts: dict[str, int] = {}
        values: dict[str, ast.AST] = {}
        for node in self._own_body_nodes(scope):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        counts[target.id] = counts.get(target.id, 0) + 1
                        values[target.id] = node.value
            elif isinstance(node, ast.withitem) and isinstance(
                node.optional_vars, ast.Name
            ):
                name = node.optional_vars.id
                counts[name] = counts.get(name, 0) + 1
                values[name] = node.context_expr

        params: set[str] = set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            spec = scope.args
            for arg in (
                list(spec.posonlyargs)
                + list(spec.args)
                + list(spec.kwonlyargs)
                + ([spec.vararg] if spec.vararg else [])
                + ([spec.kwarg] if spec.kwarg else [])
            ):
                params.add(arg.arg)

        binds = {
            name: value
            for name, value in values.items()
            if counts[name] == 1 and name not in params
        }
        if not binds:
            return
        start = 0 if is_module else getattr(scope, "lineno", 0)
        end = (1 << 30) if is_module else (getattr(scope, "end_lineno", None) or start)
        self._scopes.append((start, end, binds))

    def anywhere(self, name: str) -> ast.AST | None:
        """The binding for *name* from any indexed scope, innermost first.

        Only for the alias closure in `_promoted_tmp_names`, where the
        question is "was this temp handle ever promoted anywhere in the
        module" -- a scope-blind question by construction. Write-target
        resolution uses `at()` and stays scope-correct.
        """
        best: ast.AST | None = None
        best_span = None
        for start, end, binds in self._scopes:
            if name not in binds:
                continue
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = binds[name], span
        return best

    def at(self, lineno: int) -> dict[str, ast.AST]:
        """The binds of the INNERMOST indexed scope containing *lineno*."""
        best: dict[str, ast.AST] | None = None
        best_span = None
        for start, end, binds in self._scopes:
            if not (start <= lineno <= end):
                continue
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = binds, span
        return best or {}


_SUBSTITUTION_HOPS = 3


def _substitute_local_names(expr: ast.AST, binds: dict[str, ast.AST]) -> ast.AST:
    """R8's substitution pass: replace bare Names with their single bound
    expression, up to `_SUBSTITUTION_HOPS` times so a two-step binding
    (`tmp = Path(tmpdir)`, `f = tmp / "x"`) resolves. Bounded rather than
    fixpointed because a self-referential binding would otherwise spin."""
    class _Substituter(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:  # noqa: N802 (ast API name)
            bound = binds.get(node.id)
            if bound is None or isinstance(bound, ast.Name):
                return node
            return bound

    for _ in range(_SUBSTITUTION_HOPS):
        rendered = ast.unparse(expr)
        reparsed = ast.parse(rendered).body[0]
        if not isinstance(reparsed, ast.Expr):
            break
        expr = _Substituter().visit(reparsed.value)
        if ast.unparse(expr) == rendered:
            break
    return expr


def _promoted_tmp_names(tree: ast.AST, scope_binds: "_ScopeBindings") -> frozenset[str]:
    """Names used as the SOURCE of an `os.replace`/`Path.replace` -- i.e. temp
    files that become a real artifact rather than staying scratch.

    Aliases count. `fd, tmp_name = tempfile.mkstemp(...)` followed by
    `tmp_path = Path(tmp_name)` and `os.replace(tmp_path, dest)` promotes
    `tmp_name` just as directly as replacing it by name would; reading only
    the literal replace argument would call that fd scratch and drop a write
    site R3 exists to keep (`coordinator_core/percolate/manifest.py`).
    """
    promoted: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "replace"
        ):
            continue
        receiver = node.func.value
        if isinstance(receiver, ast.Name) and receiver.id == "os":
            if node.args and isinstance(node.args[0], ast.Name):
                promoted.add(node.args[0].id)
            continue
        if isinstance(receiver, ast.Name):
            promoted.add(receiver.id)
        elif isinstance(receiver, ast.Call) and receiver.args and isinstance(
            receiver.args[0], ast.Name
        ):
            promoted.add(receiver.args[0].id)

    # Alias closure: a promoted name bound to an expression naming other
    # locals promotes those too (`tmp_path = Path(tmp_name)`).
    for _ in range(_SUBSTITUTION_HOPS):
        widened = set(promoted)
        for name in promoted:
            bound = scope_binds.anywhere(name)
            if bound is None:
                continue
            widened.update(
                sub.id for sub in ast.walk(bound) if isinstance(sub, ast.Name)
            )
        if widened == promoted:
            break
        promoted = widened
    return frozenset(promoted)


def _scratch_mkstemp_fds(tree: ast.AST, scope_binds: "_ScopeBindings") -> frozenset[str]:
    """R7: fd names from `fd, path = tempfile.mkstemp(...)` whose PATH
    partner is never promoted by a later `replace`.

    The pairing is what makes this safe. R3's contract (see module docstring)
    is that a temp write is never dropped when the temp file goes on to
    BECOME the artifact -- `os.replace(tmp, dest)` with an unresolvable
    `dest` must stay `WRITE_TARGET_UNRESOLVED`, because a real artifact was
    written to a place the sweep cannot name. A scratch temp with no replace
    at all is the opposite case: it is deleted, and nothing it wrote ever
    reaches the tree.
    """
    promoted = _promoted_tmp_names(tree, scope_binds)
    scratch: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        value = node.value
        if not (
            isinstance(target, ast.Tuple)
            and len(target.elts) == 2
            and isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "mkstemp"
            and isinstance(value.func.value, ast.Name)
            and value.func.value.id == "tempfile"
        ):
            continue
        fd_elt, path_elt = target.elts
        if not (isinstance(fd_elt, ast.Name) and isinstance(path_elt, ast.Name)):
            continue
        if path_elt.id not in promoted:
            scratch.add(fd_elt.id)
    return frozenset(scratch)


def _is_fdopen_of_scratch_fd(call: ast.Call, scratch_fds: frozenset[str]) -> bool:
    """R7: True for `os.fdopen(<scratch mkstemp fd>, "w", ...)`.

    `_write_target_expr` cannot answer for an fdopen -- an fd is a number,
    not a path -- so every such write previously fell through as unresolved.
    A descriptor from `tempfile.mkstemp` that is never promoted by a
    `replace` is a temp file BY CONSTRUCTION, which is R5's criterion applied
    to the one shape R5 cannot see: an integer.
    """
    func = call.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == "fdopen"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    ):
        return False
    if not call.args:
        return False
    fd_arg = call.args[0]
    return isinstance(fd_arg, ast.Name) and fd_arg.id in scratch_fds


def _resolve_target_expr(
    expr: ast.AST,
    tmp_bases: dict[str, ast.AST],
    local_binds: dict[str, ast.AST] | None = None,
) -> tuple[str, str | None]:
    """Classify a raw target expression, applying R4's substitution first,
    then R8's single-assignment name substitution when the expression is
    still neither excluded nor literal.

    Returns `("excluded", None)`, `("literal", <string>)`, or
    `("unresolved", None)`.
    """
    resolved = expr
    if isinstance(resolved, ast.Name) and resolved.id in tmp_bases:
        resolved = tmp_bases[resolved.id]
    elif isinstance(resolved, ast.JoinedStr):
        substituted = _substitute_tmp_sibling(resolved)
        if substituted is not None:
            resolved = substituted

    if _is_excluded_base(resolved):
        return "excluded", None

    literal = _str_const(resolved)
    if literal is not None:
        return "literal", literal

    if local_binds:
        # R8 runs LAST: R4's `.tmp`-sibling substitution and R5's inline
        # exclusions both answer on the expression as written, and only a
        # target still carrying an unresolved Name reaches here.
        widened = _substitute_local_names(resolved, local_binds)
        if ast.unparse(widened) != ast.unparse(resolved):
            if _is_excluded_base(widened):
                return "excluded", None
            literal = _str_const(widened)
            if literal is not None:
                return "literal", literal

    return "unresolved", None


def _replace_destination_exprs(
    tree: ast.AST, tmp_var_names: frozenset[str]
) -> list[ast.AST]:
    """R3: collect destination expressions from `os.replace`/`Path.replace`
    calls whose source is a tracked temp-handle variable name.

    Matches two call shapes:
      - `os.replace(<tmp var>, dest)` — module-function form, args[0] is
        the source, args[1] the destination.
      - `<tmp var>.replace(dest)` / `Path(<tmp var>).replace(dest)` —
        bound-method form; the destination is the call's own args[0].
    """
    destinations: list[ast.AST] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace"):
            continue
        receiver = node.func.value

        if isinstance(receiver, ast.Name) and receiver.id == "os":
            if len(node.args) >= 2 and isinstance(node.args[0], ast.Name) and node.args[0].id in tmp_var_names:
                destinations.append(node.args[1])
            continue

        src_name: str | None = None
        if isinstance(receiver, ast.Name):
            src_name = receiver.id
        elif isinstance(receiver, ast.Call) and receiver.args and isinstance(receiver.args[0], ast.Name):
            src_name = receiver.args[0].id

        if src_name is not None and src_name in tmp_var_names and node.args:
            destinations.append(node.args[0])

    return destinations


def _module_is_generator(
    tree: ast.AST, tracked: frozenset[str] | None, is_test_module: bool = False
) -> str | None:
    """Return the basis on which this module was inferred to be a generator.

    Two bases exist and they carry very different strength, so the caller
    reports them differently rather than collapsing both into one claim:

      - `tracked:<path>` — the write target resolved to a literal that
        `git ls-files` reports as tracked. The strong basis: the module
        demonstrably writes a repo artifact. Applies to a test module
        exactly as to any other — a resolved tracked write is never
        exempted.
      - `unresolved` — the write target is an expression, not a literal, so
        the sweep can neither rule it in nor out and counts it (see this
        module's header). The weak basis: it establishes that a write
        happens, never that its destination is a tracked artifact. A module
        the repo's own pytest `python_files` config declares to be a test is
        exempted from this basis — its writes are fixture writes (typically
        through a `tmp_path`-derived variable), not repo artifacts.

    R1-R6 (see module docstring) narrow this further: a write can also
    resolve to `"excluded"` internally (R1/R5) and contribute no signal at
    all, and an unresolved temp-handle write can be upgraded to `tracked:`
    by following R3/R4's atomic-write idiom to its real destination.

    Returns None when no write was found. Truthiness is unchanged from the
    prior bool return, so the caller's `not _module_is_generator(...)` guard
    keeps its meaning.
    """
    json_module_aliases, json_dump_names = _json_bindings(tree)
    handle_names = _handle_names(tree)
    tmp_bases, mkstemp_names = _tmp_var_info(tree)
    tmp_var_names = mkstemp_names | frozenset(tmp_bases)
    scope_binds = _ScopeBindings(tree)
    scratch_fds = _scratch_mkstemp_fds(tree, scope_binds)

    unresolved_seen = False

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_is_write(node, json_module_aliases, json_dump_names)):
            continue

        func = node.func
        is_dump = isinstance(func, ast.Attribute) and func.attr == "dump"
        if is_dump:
            file_arg = _dump_file_arg(node)
            if file_arg is not None:
                if _is_stdio_sink(file_arg):  # R1
                    continue
                if isinstance(file_arg, ast.Name) and file_arg.id in handle_names:  # R2
                    continue
            unresolved_seen = True
            continue

        if _is_fdopen_of_scratch_fd(node, scratch_fds):  # R7
            continue

        expr = _write_target_expr(node)
        if expr is None:
            unresolved_seen = True
            continue

        kind, literal = _resolve_target_expr(
            expr, tmp_bases, scope_binds.at(node.lineno)
        )
        if kind == "excluded":  # R5 (widened by R8)
            continue
        if kind == "unresolved":
            unresolved_seen = True
            continue

        assert literal is not None
        if _looks_like_tmp(literal):
            continue
        if tracked is not None and _normalize_target(literal) not in tracked:
            continue
        return f"tracked:{_normalize_target(literal)}"

    if tmp_var_names:
        for dest_expr in _replace_destination_exprs(tree, tmp_var_names):  # R3
            kind, literal = _resolve_target_expr(dest_expr, tmp_bases)
            if kind != "literal" or literal is None:
                continue
            if _looks_like_tmp(literal):
                continue
            if tracked is not None and _normalize_target(literal) not in tracked:
                continue
            return f"tracked:{_normalize_target(literal)}"

    return "unresolved" if (unresolved_seen and not is_test_module) else None


def _extract_generates(tree: ast.Module) -> object | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "GENERATES" in names:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "GENERATES" and node.value is not None:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
    return None


def _extract_mutates(tree: ast.Module) -> object | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "MUTATES" in names:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "MUTATES" and node.value is not None:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
    return None


def _valid_mutates_shape(mutates: object) -> bool:
    """AC-parallel to `_valid_sources`: MUTATES must be a non-empty list of
    non-empty strings. No filesystem existence check (a pathspec, not a
    concrete artifact) -- that is `_mutates_any_tracked_match`'s job."""
    if mutates == "__MALFORMED__":
        return False
    if not isinstance(mutates, list) or len(mutates) == 0:
        return False
    return all(isinstance(pattern, str) and pattern for pattern in mutates)


def _mutates_any_tracked_match(patterns: list[str], tracked: frozenset[str] | None) -> bool:
    """True when at least one *patterns* glob matches at least one currently
    tracked path. `tracked is None` (git unusable) is treated as "cannot
    check", so it does not itself invalidate an otherwise well-formed
    declaration -- mirrors how `_module_is_generator` treats an unresolved
    `tracked` set elsewhere in this module."""
    if tracked is None:
        return True
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns for path in tracked)


_WILDCARD_CHARS = ("*", "?", "[")


def _has_literal_element(pattern: str) -> bool:
    """True when *pattern* carries at least one literal (non-wildcard)
    path segment, or a literal file extension on its final segment.

    A pathspec matching the whole tracked corpus (`*`, `**`, `**/*`,
    `*/*`) carries neither and is rejected by the caller -- the
    hand-maintained-registry failure mode respelled as a MUTATES
    catch-all. `**/*.py` passes on its literal `.py` extension even
    though every segment before it is a wildcard; `state/**/*.yaml`
    passes on its literal `state` segment alone.
    """
    segments = pattern.split("/")
    if any(segment and not any(ch in segment for ch in _WILDCARD_CHARS) for segment in segments):
        return True
    last = segments[-1] if segments else ""
    if "." in last:
        suffix = last.rsplit(".", 1)[1]
        if suffix and not any(ch in suffix for ch in _WILDCARD_CHARS):
            return True
    return False


def _mutates_broad_patterns(patterns: list[str]) -> list[str]:
    """Return the subset of *patterns* carrying no literal path segment or
    extension -- a catch-all pathspec that would silence the module
    completely, `GENERATES = []` in disguise (see module docstring)."""
    return [pattern for pattern in patterns if not _has_literal_element(pattern)]


def _has_wildcard(pattern: str) -> bool:
    return any(ch in pattern for ch in _WILDCARD_CHARS)


def _mutates_concrete_patterns(patterns: list[str]) -> list[str]:
    """Return the subset of *patterns* carrying no wildcard metacharacter at
    all -- a bare concrete filename, not a pattern. `_has_literal_element`
    alone lets a pathspec like `["docs/exec-summary.md"]` through, because a
    wildcard-free segment reads as "literal" there too: it matches a
    tracked path trivially and declares a genuine fixed-artifact generator
    as a mutator, exactly the evasion DR-305 forbids. A fixed, enumerable
    output set is `GENERATES`'s job, not `MUTATES`'s."""
    return [pattern for pattern in patterns if not _has_wildcard(pattern)]


def _valid_sources(sources: object, repo_root: Path) -> bool:
    if not isinstance(sources, list) or len(sources) == 0:
        return False
    for source in sources:
        if not isinstance(source, str) or not source:
            return False
        if not (repo_root / source).exists():
            return False
    return True


def _build_record(
    rel_path: str,
    generates: object,
    repo_root: Path,
    basis: str | None = None,
    mutates: object = None,
    tracked: frozenset[str] | None = None,
) -> GeneratorRecord:
    mutates_patterns: tuple[str, ...] = ()
    mutates_error: str | None = None
    if mutates is not None:
        if not _valid_mutates_shape(mutates):
            mutates_error = (
                f"{rel_path} has a MUTATES declaration that is not a non-empty list "
                f"of non-empty strings: {mutates!r}"
            )
        else:
            patterns = list(mutates)
            if not _mutates_any_tracked_match(patterns, tracked):
                mutates_error = (
                    f"{rel_path} MUTATES pattern(s) match no currently-tracked path: "
                    f"{patterns!r}"
                )
            else:
                broad_patterns = _mutates_broad_patterns(patterns)
                concrete_patterns = _mutates_concrete_patterns(patterns)
                if broad_patterns:
                    mutates_error = (
                        f"{rel_path} MUTATES pattern(s) {broad_patterns!r} match the whole corpus; "
                        f"a pathspec needs a literal directory segment or file extension"
                    )
                elif concrete_patterns:
                    if len(concrete_patterns) == 1:
                        mutates_error = (
                            f"{rel_path} MUTATES pattern '{concrete_patterns[0]}' names a "
                            f"concrete path; a fixed artifact declares GENERATES"
                        )
                    else:
                        mutates_error = (
                            f"{rel_path} MUTATES pattern(s) {concrete_patterns!r} name a "
                            f"concrete path; a fixed artifact declares GENERATES"
                        )
                else:
                    mutates_patterns = tuple(patterns)

        if generates is None:
            if mutates_error is not None:
                return GeneratorRecord(
                    generator=rel_path,
                    pairs=(),
                    verdict=Verdict.UNDECLARED,
                    detail=mutates_error,
                )
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.MUTATES_DECLARED,
                detail=f"{rel_path} declares MUTATES = {list(mutates_patterns)!r} (corpus mutator, no staleness contract)",
                mutates=mutates_patterns,
            )

    if generates is None:
        if basis is not None and basis.startswith("tracked:"):
            path_part = basis[len("tracked:") :]
            detail = f"{rel_path} writes tracked path '{path_part}' but declares no GENERATES"
            verdict = Verdict.UNDECLARED
        else:
            detail = f"{rel_path} writes through an unresolved path expression and declares no GENERATES"
            verdict = Verdict.WRITE_TARGET_UNRESOLVED
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=verdict,
            detail=detail,
        )

    if generates == "__MALFORMED__":
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=Verdict.UNDECLARED,
            detail=f"{rel_path} has a GENERATES assignment that is not a literal list",
        )

    if not isinstance(generates, list):
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=Verdict.UNDECLARED,
            detail=f"{rel_path} GENERATES is not a list",
        )

    if len(generates) == 0:
        detail = f"{rel_path} declares GENERATES = [] (declared-empty, no artifacts)"
        if mutates_error is not None:
            detail = f"{detail}; {mutates_error}"
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=None,
            detail=detail,
            mutates=mutates_patterns,
        )

    pairs: list[Pair] = []
    for entry in generates:
        if not isinstance(entry, dict):
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry is not a mapping: {entry!r}",
            )

        artifact = entry.get("artifact")
        stamp_key = entry.get("stamp_key")
        sources = entry.get("sources")

        if not isinstance(artifact, str) or not artifact:
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry missing a valid artifact: {entry!r}",
            )

        if not isinstance(stamp_key, str) or not stamp_key:
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry missing a valid stamp_key: {entry!r}",
            )

        if not _valid_sources(sources, repo_root):
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=(
                    f"{rel_path} GENERATES entry for {artifact!r} has malformed "
                    f"sources (empty, not a list, or naming an absent path): {sources!r}"
                ),
            )

        pairs.append(
            Pair(
                generator=rel_path,
                artifact=artifact,
                stamp_key=stamp_key,
                sources=tuple(sources),
            )
        )

    detail = f"{rel_path} declares {len(pairs)} pair(s)"
    if mutates_error is not None:
        detail = f"{detail}; {mutates_error}"
    return GeneratorRecord(
        generator=rel_path,
        pairs=tuple(pairs),
        verdict=None,
        detail=detail,
        mutates=mutates_patterns,
    )


def discover_generators(repo_root: Path) -> list[GeneratorRecord]:
    records: list[GeneratorRecord] = []
    tracked = _tracked_paths(repo_root)
    test_module_globs = _test_module_globs(repo_root)

    for sweep_dir in _SWEEP_DIRS:
        root = repo_root / sweep_dir
        if not root.exists():
            continue

        for path in sorted(root.rglob("*.py")):
            if not path.is_file():
                continue

            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                continue

            rel_path = path.relative_to(repo_root).as_posix()
            generates = _extract_generates(tree)
            mutates = _extract_mutates(tree)

            basis: str | None = None
            if generates is None and mutates is None:
                is_test_module = _is_test_module(rel_path, test_module_globs)
                basis = _module_is_generator(tree, tracked, is_test_module)
                if not basis:
                    continue

            records.append(_build_record(rel_path, generates, repo_root, basis, mutates=mutates, tracked=tracked))

    return records
