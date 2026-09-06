"""Standing regrowth gate: a console-suppressed spawn must not silently
swallow its child's output.

WHY THIS IS A SEPARATE MODULE, not an edit to
`test_no_bare_hot_path_spawn.py`: that gate and this one assert OPPOSITE
properties over the same call sites, and merging them would make each one's
failure message ambiguous. That gate asks "does this spawn suppress the
console?" and fails when `creationflags` is ABSENT. This gate asks "does a
suppressed spawn still pass output through?" and fails only when
`creationflags` is PRESENT and every std-stream kwarg is absent. A site can
satisfy one and violate the other -- indeed every site this gate flags is
green under that one, which is exactly how the defect class grew: the
existing gate counts *does it suppress the console*, while the thing that
actually breaks is *does it still pass output through*. Same enumeration,
different assertion -- the reuse shape that gate's own docstring already
cites for `write_guards/nudge_shell_shaped_spawn.py`.

THE DEFECT, measured (not inferred) on a real Windows host:
CPython sets `STARTF_USESTDHANDLES` only when at least one of
`stdin=`/`stdout=`/`stderr=`/`capture_output=` is passed. Without it, a
`CREATE_NO_WINDOW` child binds its standard handles to the fresh window-less
console the flag allocates instead of inheriting the parent's -- so
everything the child prints goes into a console nobody can read. Probe
results on this box: plain spawn -> the child's line was captured;
`no_console_creationflags()` spawn with no stream kwarg -> `''`.

Passing any ONE stream kwarg is sufficient: CPython then fills the
UNSPECIFIED handles from the parent's own real handles. Measured:
`stdout=PIPE` with `stderr` unspecified captured stdout correctly AND the
child's stderr still reached the parent's console. That is why this gate's
predicate is "no stream kwarg AT ALL" rather than "not capture_output" --
the looser predicate would flag several hundred correct sites.

FALSE-NEGATIVE OVER FALSE-POSITIVE, and note the direction is the OPPOSITE
of the sibling gate's for the same reason both are conservative: an
unresolvable `**splat` might CARRY a stream kwarg, so a call with an extra
splat this collector cannot trace is treated as COMPLIANT here (the sibling
gate treats an untraceable splat as bare/violating, because there the
unknown kwarg would be the thing that saves it). In both modules the
unknown resolves toward "do not fail the build on a guess".

TEST TREE EXCLUDED, deliberately: a test that wanted the child's output
would have captured it, so losing it cannot move an assertion. Partitioned
via `spawn_policy.is_test_tree_site`, the same seam the sibling gate uses.

Spec backlink: pln-no-window-subprocess-primitive-750d2d § C6,
AC5 (this gate closes the property that AC5's gate does not measure).
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

from coordinator_core.spawn_policy import SpawnParseError, is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

from coordinator_core.tests.test_no_bare_hot_path_spawn import (
    _EXEMPTION_TAG,
    _SubprocessImportResolver,
    _collect_no_console_names,
    _is_no_console_shaped,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Trees this gate scans. Broader than the sibling gate's `coordinator_core`
#: because the defect is not confined to the engine package -- 7 of the
#: sites in the landing survey were operator-facing CLIs under
#: `coordinator/bin/`, where a swallowed child is MOST visible to a human.
#: `scripts/` and `coordinator/scripts/` were added 2026-08-25 after a census found
#: FOUR live violations in `scripts/setup.py` alone -- `register_claude_klabauter_root`,
#: `offer_warm_opt_in`, `install_machine_identity`,
#: `_seed_fleet_env_root_from_klabauter`, every one a `machine-local set` spawn
#: discarding the child's own reason for failing. `find_output_swallowing_spawns(
#: [REPO_ROOT / "scripts"])` returned `[]` against that directory: unwalked roots
#: do not read as clean, they read as absent, and the gate reported green over a
#: population it could not see. Add a root here rather than trusting that the
#: install surface is somebody else's problem.
_SCAN_ROOTS: tuple[str, ...] = (
    "coordinator_core",
    "coordinator/bin",
    "coordinator/lib",
    "coordinator/scripts",
    "scripts",
    "bin",
)

#: Spawn functions that accept std-stream kwargs. `os.system`/`os.popen` are
#: absent on purpose: neither takes `creationflags`, so neither can reach the
#: shape this gate describes.
_SPAWN_NAMES: frozenset[str] = frozenset({"run", "Popen", "check_output", "call", "check_call"})

#: Any ONE of these is sufficient to set STARTF_USESTDHANDLES -- see the
#: module docstring's measurement.
#:
#: `input` belongs here and was missing: `subprocess.run` converts it to
#: `stdin=PIPE` before `Popen` ever sees it (`subprocess.py`, the
#: `if input is not None: kwargs["stdin"] = PIPE` arm), so a call passing
#: `input=` is as safe as one passing `stdin=` and flagging it is a FALSE
#: POSITIVE -- `install/substrate.py :: _fnm_step`'s `bash -s` spawn was
#: reading as a violation on that axis alone.
_STREAM_KWARGS: frozenset[str] = frozenset(
    {"stdin", "stdout", "stderr", "capture_output", "input"}
)

#: Sites known to violate, deliberately NOT fixed, each with the reason and
#: the owner who holds the fix. This is a DEFERRAL LEDGER, not an
#: allowlist: `test_deferred_sites_are_still_broken` below fails if an entry
#: stops violating (fixed -> delete the entry) or stops existing (moved ->
#: re-point it), so an entry cannot quietly become a permanent exemption.
#: EMPTY BY CONSTRUCTION at land -- every violation the landing survey found
#: was fixed rather than deferred. The mechanism stays because the honesty
#: test below is what makes a future deferral safe to grant; an empty ledger
#: with a live keeper beats no ledger and an ad-hoc `# noqa` later.
_DEFERRED: dict[tuple[str, int], str] = {}


@dataclasses.dataclass(frozen=True)
class OutputSwallowingSite:
    """A spawn that suppresses the console and specifies no std stream."""

    path: str
    lineno: int
    enclosing: str


def _carries_no_console_signal(
    node: ast.Call, source_lines: list[str], resolved: frozenset[str] = frozenset()
) -> bool:
    """True if this call passes console-suppressing creationflags.

    Matched four ways: an explicit `creationflags=` keyword, a `**` splat whose
    callee/name is `no_console_*`-shaped, a `**` splat naming an ALIAS resolved to
    the primitive by `_collect_no_console_names`, or a literal CREATE_NO_WINDOW
    constant spelled in the call's source span.

    THE ALIAS ARM IS THE ONE THIS GATE WAS MISSING, and it is not a corner case.
    `_is_no_console_shaped` is a name-SHAPE test — `identifier.lstrip("_").
    startswith("no_console")` — so it returns False for `_NO_CONSOLE`, the
    module-constant spelling this repo uses most. The literal-span fallback does
    not rescue it either: a `**_NO_CONSOLE` splat's own source span contains no
    "CREATE_NO_WINDOW" text, because the constant is spelled at the assignment and
    not at the call. Every `**_NO_CONSOLE`-shaped site in the tree was therefore
    invisible here — reported green, never actually examined. The sibling gate does
    not have the bug because `_collect_no_console_names` is precisely what it uses;
    this gate simply never called it.
    """
    for kw in node.keywords:
        if kw.arg == "creationflags":
            return True
        if kw.arg is None:
            ident = _splat_identifier(kw.value) or ""
            if _is_no_console_shaped(ident) or ident in resolved:
                return True

    span = "\n".join(source_lines[node.lineno - 1 : (node.end_lineno or node.lineno)])
    return "CREATE_NO_WINDOW" in span or "0x08000000" in span


def _splat_identifier(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_passthrough_shaped(identifier: str) -> bool:
    """True for the sanctioned fix primitive and its thin local wrappers.

    `no_console_passthrough_kwargs()` returns the creationflags AND the std
    fds, so a call splatting it is compliant by construction. Matched by
    identifier shape, not by resolving the callee -- consistent with the
    sibling gate's static-AST-only design, and with `_is_no_console_shaped`
    which it deliberately mirrors.
    """
    tail = identifier.rsplit(".", 1)[-1].lstrip("_")
    return "passthrough" in tail and ("kw" in tail or "spawn" in tail)


def _collect_compliant_kwargs_names(stmts: list[ast.stmt]) -> set[str]:
    """Names bound to a kwargs mapping that ALREADY carries a std stream.

    The counterpart to the sibling gate's `_collect_no_console_names`, and the
    reason this gate needs one of its own. That collector answers "does this
    name contribute console suppression?" and answers YES for
    `no_console_passthrough_kwargs()` -- correctly, since the passthrough
    primitive returns the creationflags too. This gate then read the resulting
    alias as a bare no-console splat, because `_is_passthrough_shaped` is a
    name-SHAPE test applied to the SPLAT's identifier, and a local called
    `_passthrough` or `spawn_kwargs` is not shaped like the primitive it was
    assigned from. Every compose-then-splat site therefore read as a violation
    while being compliant by construction:

        spawn_kwargs = dict(stdout=PIPE, stderr=PIPE, **no_console_creationflags())
        subprocess.Popen(argv, **spawn_kwargs)

    Resolution is deliberately the same three shapes, and the same traversal
    contract, `_collect_no_console_names` uses -- assignment from a
    passthrough-shaped call, a `dict(...)`/`{...}` display carrying a stream
    kwarg or splatting a compliant source, and `NAME["stdout"] = ...`
    subscript assignment (which is how a caller adds a stream inside one
    branch of a conditional). Descends into `if`/`try`/`with`/`for`/`while`
    bodies, never into a nested function, so a same-named local elsewhere
    cannot leak in.
    """

    names: set[str] = set()

    def _value_is_compliant(value: ast.expr) -> bool:
        if isinstance(value, ast.Name):
            return value.id in names
        if isinstance(value, ast.Call):
            func_name = _splat_identifier(value.func) or ""
            if _is_passthrough_shaped(func_name):
                return True
            for kw in value.keywords:
                if kw.arg in _STREAM_KWARGS:
                    return True
                if kw.arg is None and _value_is_compliant(kw.value):
                    return True
            return False
        if isinstance(value, ast.Dict):
            for key, item in zip(value.keys, value.values):
                if key is None:
                    if _value_is_compliant(item):
                        return True
                elif isinstance(key, ast.Constant) and key.value in _STREAM_KWARGS:
                    return True
            return False
        return False

    def _walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # new scope -- do not resolve names into or out of it

            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = stmt.value
                if value is not None and _value_is_compliant(value):
                    for target in targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.value, ast.Name)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in _STREAM_KWARGS
                    ):
                        names.add(target.value.id)

            for field in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, field, None)
                if isinstance(nested, list):
                    _walk([s for s in nested if isinstance(s, ast.stmt)])
            for handler in getattr(stmt, "handlers", []) or []:
                _walk(handler.body)

    _walk(stmts)
    return names


def _has_untraceable_splat(
    node: ast.Call, resolved: frozenset[str] = frozenset()
) -> bool:
    """True if a `**splat` that is NOT the no-console primitive is present.

    Such a splat may itself carry `stdout=`/`stderr=`, which this AST-only
    collector cannot see -- so its presence buys the call the benefit of the
    doubt. See the module docstring's false-negative preference.
    """
    for kw in node.keywords:
        if kw.arg is None:
            ident = _splat_identifier(kw.value) or ""
            # A splat resolved to the primitive is TRACEABLE — it contributes
            # creationflags and nothing else. Without this arm every
            # `**_NO_CONSOLE` site took this skip-on-doubt exit as WELL as failing
            # `_carries_no_console_signal`, so the blind spot was covered twice
            # over and no single fix would have lifted it.
            if not _is_no_console_shaped(ident) and ident not in resolved:
                return True
    return False


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        source_lines: list[str],
        resolver: _SubprocessImportResolver,
        module_aliases: frozenset[str] = frozenset(),
        module_compliant: frozenset[str] = frozenset(),
    ) -> None:
        self._lines = source_lines
        self._resolver = resolver
        self._enclosing: list[str] = []
        self.sites: list[tuple[int, str]] = []
        self._scope_compliant: list[frozenset[str]] = [module_compliant]
        # Two-pass alias resolution, matching the sibling gate's own contract:
        # module scope once, then each function scope unioned on top for the
        # duration of that scope. A same-named local inside another function must
        # not leak across scopes, which is why this is a stack and not a set.
        self._scope_aliases: list[frozenset[str]] = [module_aliases]

    def _visit_scope(self, node) -> None:
        self._enclosing.append(node.name)
        local = _collect_no_console_names(getattr(node, "body", []))
        self._scope_aliases.append(self._scope_aliases[-1] | frozenset(local))
        local_compliant = _collect_compliant_kwargs_names(getattr(node, "body", []))
        self._scope_compliant.append(self._scope_compliant[-1] | frozenset(local_compliant))
        self.generic_visit(node)
        self._scope_compliant.pop()
        self._scope_aliases.pop()
        self._enclosing.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_subprocess_spawn(node):
            span = "\n".join(self._lines[node.lineno - 1 : (node.end_lineno or node.lineno)])
            kwnames = {kw.arg for kw in node.keywords if kw.arg}
            splats = [
                _splat_identifier(kw.value) or "" for kw in node.keywords if kw.arg is None
            ]
            if (
                _EXEMPTION_TAG not in span
                and _carries_no_console_signal(node, self._lines, self._scope_aliases[-1])
                and not (kwnames & _STREAM_KWARGS)
                and not any(_is_passthrough_shaped(name) for name in splats)
                and not any(name in self._scope_compliant[-1] for name in splats)
                and not _has_untraceable_splat(node, self._scope_aliases[-1])
            ):
                self.sites.append((node.lineno, self._enclosing[-1] if self._enclosing else "<module>"))
        self.generic_visit(node)

    def _is_subprocess_spawn(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Attribute):
            base = func.value
            return (
                isinstance(base, ast.Name)
                and self._resolver.module_aliases.get(base.id) == "subprocess"
                and func.attr in _SPAWN_NAMES
            )
        if isinstance(func, ast.Name):
            return self._resolver.function_aliases.get(func.id) == "subprocess"
        return False


def find_output_swallowing_spawns(roots: list[pathlib.Path]) -> list[OutputSwallowingSite]:
    """Return every non-test spawn under `roots` that suppresses the console
    and specifies no std-stream kwarg.

    Run against the real tree by the standing test below, and against an
    isolated `tmp_path` fixture by this gate's own teeth self-tests.
    """
    sites: list[OutputSwallowingSite] = []
    for root in roots:
        if not root.is_dir():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for relpath, file_path in discovered:
            if is_test_tree_site(relpath):
                continue
            source = file_path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source, filename=str(file_path))
            except SyntaxError as exc:
                raise SpawnParseError(relpath, str(exc)) from exc

            resolver = _SubprocessImportResolver()
            resolver.visit(tree)
            if not resolver.module_aliases and not resolver.function_aliases:
                continue

            visitor = _Visitor(
                source.splitlines(),
                resolver,
                frozenset(_collect_no_console_names(tree.body)),
                frozenset(_collect_compliant_kwargs_names(tree.body)),
            )
            visitor.visit(tree)
            if not visitor.sites:
                continue
            repo_rel = file_path.resolve().relative_to(REPO_ROOT).as_posix()
            for lineno, enclosing in visitor.sites:
                sites.append(
                    OutputSwallowingSite(path=repo_rel, lineno=lineno, enclosing=enclosing)
                )
    return sites


def _scan_roots() -> list[pathlib.Path]:
    return [REPO_ROOT / rel for rel in _SCAN_ROOTS]


def _format(site: OutputSwallowingSite) -> str:
    return (
        f"{site.path}:{site.lineno} ({site.enclosing}) -- console-suppressed spawn with no "
        "stdin=/stdout=/stderr=/capture_output=. On Windows this child's output is written "
        "into the window-less console CREATE_NO_WINDOW allocates and is LOST. Fix: use "
        "coordinator_core.win_portability.no_console_passthrough_kwargs() if the output is "
        "meant to reach the operator, or pass capture_output=True/PIPE if this caller "
        "consumes it."
    )


def test_no_output_swallowing_no_console_spawn():
    """Standing gate: no production spawn may suppress the console while
    specifying no std stream."""
    violations = [
        site
        for site in find_output_swallowing_spawns(_scan_roots())
        if (site.path, site.lineno) not in _DEFERRED
    ]
    assert violations == [], "\n\n".join(_format(site) for site in violations)


def test_deferred_sites_are_still_broken():
    """Keeps `_DEFERRED` honest: an entry that no longer violates (fixed, or
    moved) must be deleted or re-pointed, not left as a standing exemption.

    Without this, the ledger degrades into exactly the kind of allowlist that
    lets a gate read green over a defect it was built to catch.
    """
    found = {(site.path, site.lineno) for site in find_output_swallowing_spawns(_scan_roots())}
    stale = sorted(key for key in _DEFERRED if key not in found)
    assert stale == [], (
        "_DEFERRED entries that no longer correspond to a live violation — delete "
        f"(if fixed) or re-point (if moved): {stale}"
    )


def _plant(tmp_path: pathlib.Path, body: str) -> pathlib.Path:
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "planted.py").write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import no_console_creationflags\n"
        "\n"
        "def go():\n" + body,
        encoding="utf-8",
    )
    return pkg


def _plant_and_scan(tmp_path: pathlib.Path, body: str) -> list[OutputSwallowingSite]:
    pkg = _plant(tmp_path, body)
    sites = find_output_swallowing_spawns([pkg])
    return sites


def test_gate_detects_a_planted_output_swallowing_spawn(tmp_path, monkeypatch):
    """Teeth. Without this, a clean tree would make the gate sound by absence
    rather than by verification."""
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(tmp_path, "    subprocess.run(['x'], **no_console_creationflags())\n")
    assert [s.enclosing for s in sites] == ["go"]


def test_gate_ignores_a_spawn_that_captures_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    subprocess.run(['x'], capture_output=True, **no_console_creationflags())\n",
    )
    assert sites == []


def test_gate_ignores_a_spawn_specifying_only_stdout(tmp_path, monkeypatch):
    """Partial specification is genuinely safe -- CPython fills the
    unspecified handles from the parent's real ones once
    STARTF_USESTDHANDLES is set. Measured; see module docstring."""
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    subprocess.run(['x'], stdout=subprocess.PIPE, **no_console_creationflags())\n",
    )
    assert sites == []


def test_gate_ignores_an_unsuppressed_spawn(tmp_path, monkeypatch):
    """A spawn with no creationflags at all is the SIBLING gate's business
    (`test_no_bare_hot_path_spawn`), not this one's -- flagging it here would
    make the two gates' failure messages ambiguous over the same site."""
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(tmp_path, "    subprocess.run(['x'])\n")
    assert sites == []


def test_gate_ignores_a_passthrough_helper_spawn(tmp_path, monkeypatch):
    """The sanctioned fix shape must read as compliant, or the gate would
    push callers straight back off it."""
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "planted.py").write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import no_console_passthrough_kwargs\n"
        "\n"
        "def go():\n"
        "    subprocess.run(['x'], **no_console_passthrough_kwargs())\n",
        encoding="utf-8",
    )
    assert find_output_swallowing_spawns([pkg]) == []


def test_gate_ignores_a_composed_kwargs_dict_carrying_a_stream(tmp_path, monkeypatch):
    """The compose-then-splat shape reads by what the mapping CONTAINS.

    `_collect_compliant_kwargs_names`'s reason for existing: the splat's own
    identifier (`spawn_kwargs`) is shaped like neither primitive, so before it
    this correct site read as a violation.
    """
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    spawn_kwargs = dict(\n"
        "        stdout=subprocess.PIPE,\n"
        "        stderr=subprocess.PIPE,\n"
        "        **no_console_creationflags(),\n"
        "    )\n"
        "    subprocess.Popen(['x'], **spawn_kwargs)\n",
    )
    assert sites == []


def test_gate_still_flags_a_composed_kwargs_dict_with_no_stream(tmp_path, monkeypatch):
    """The teeth on the arm above: composing the mapping is not itself a fix.

    Same shape, minus the stream kwargs -- this must stay red, or
    `_collect_compliant_kwargs_names` would have blinded the gate to every
    site that builds its kwargs a statement early.
    """
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    spawn_kwargs = dict(env=None, **no_console_creationflags())\n"
        "    subprocess.Popen(['x'], **spawn_kwargs)\n",
    )
    assert [s.enclosing for s in sites] == ["go"]


def test_gate_ignores_a_stream_added_by_subscript_assignment(tmp_path, monkeypatch):
    """`NAME["stderr"] = PIPE` inside one branch is how a caller adds a stream
    conditionally -- the shape `coordinator-lesson-add.py :: main` uses."""
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    kw = dict(**no_console_creationflags())\n"
        "    if True:\n"
        "        kw['stderr'] = subprocess.PIPE\n"
        "    subprocess.run(['x'], **kw)\n",
    )
    assert sites == []


def test_gate_honours_the_last_resort_exemption_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    sites = _plant_and_scan(
        tmp_path,
        "    subprocess.run(['x'], **no_console_creationflags())  # popup-intentional-last-resort\n",
    )
    assert sites == []


def test_gate_ignores_an_unrelated_dot_run_call(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.tests.test_no_output_swallowing_no_console_spawn.REPO_ROOT", tmp_path
    )
    pkg = tmp_path / "coordinator_core"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "planted.py").write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import no_console_creationflags\n"
        "\n"
        "def go(guard):\n"
        "    guard.run(['x'], **no_console_creationflags())\n",
        encoding="utf-8",
    )
    assert find_output_swallowing_spawns([pkg]) == []
