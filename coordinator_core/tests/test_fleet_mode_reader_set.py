"""
coordinator_core.tests.test_fleet_mode_reader_set — the floor: a positive
allowlist of every PRODUCTION module whose TRANSITIVE IMPORT CLOSURE reaches
the fleet record (``coordinator_core.session.fleet_mode`` /
``.mode_resolution``), red in both directions.

WHY AN ALLOWLIST, NOT A PROHIBITION. This plan's exit criterion ("no value
in that file can change how a denial-shaped guard behaves") holds only
while the reader set stays closed: an absent reader is a real property
here because a mode key has exactly the consumers this plan wired and no
others. The moment a consumer outside that set reads the file, the
absence stops being an absence and the floor is gone with no other signal.
The allowlist below is the assertion of that precondition — a prohibition
("no guard may read the fleet file") asserts something strictly weaker and
cannot detect the precondition failing, because it says nothing about a
non-guard module quietly becoming a reader. See
``state/lessons/2026-08-28-a-guard-surface-is-not-a-boundary.md`` and
``guard_unlock_sentinel``'s own standing negative-spec ("must never
generalize into 'all guards off for this session'") — both are about the
same shape of mistake: treating a guard's ABSENCE from a surface as a
boundary the surface itself enforces, when nothing enforces it but this
test.

WHY TEST MODULES ARE NOT IN THE ALLOWLIST. A test module that reaches the
record imports code that does; it measures that code and never consumes a
resolved mode value to change runtime behaviour. Listing each one made the
floor go red on every new test file and was hand-widened every time — a
churn trap that guards nothing, because the production reader it rides on is
already listed. ``is_test_module`` is the rule: a dotted name with a ``tests``
package segment, a ``test_*`` leaf, or a ``conftest`` leaf. The floor compares
``production_readers(reader_closure(...))`` against ``EXPECTED_READERS``;
``reader_closure`` itself still returns every reader, test or not, and
``test_no_denial_shaped_guard_reaches_the_record`` walks that FULL closure, so
no module is exempted from the guard check by its name.

WHY THE WALK IS TRANSITIVE, NOT DIRECT-IMPORT-ONLY. A module one import hop
away from the record (an indirect reader) is exactly what a direct-import
walk misses, and reachability — not directness — is the property this test
asserts. ``coordinator_core.write_guards.nudge_em_code_dispatch`` is the
concrete case: its own source names neither ``fleet_mode`` nor
``mode_resolution`` (confirmed by probe: importing it places
``coordinator_core.hooks.nudge_em_code_dispatch`` in ``sys.modules``), so a
direct-import-or-basename walk would report it clean while it transitively
wraps the exact hook C3 converts.

WHY ``nudge_em_code_dispatch`` IS ALLOWLISTED RATHER THAN A REASON TO
RESTRUCTURE C3. Its own module docstring declares its CLASS advisory — it
offers a better path via ``additionalContext`` and never denies — so it is
neither an irreversible-harm nor an ask-before-external-action guard, and
restructuring C3 to dodge a shim would distort the hook conversion for a
naming artifact rather than a safety one. No other production module may be
added to EXPECTED_READERS without the same allowlist-vs-restructure analysis
recorded inline (not merely re-run).

"GUARD" IS DEFINED BY CLASS, NOT BY DIRECTORY. The prime exit criterion is
a statement about DENIAL-SHAPED guards, not about every module under
``write_guards/``/``bash_guards/`` — those directories also hold
advisory-class modules, one of which (above) legitimately reaches the
record. ``test_no_denial_shaped_guard_reaches_the_record`` below derives
each candidate's module-level ``CLASS`` STATICALLY (never by importing it)
and fails loud — not "treat as advisory" — when a guard-directory module in
the reader closure has no derivable ``CLASS`` at all: an unclassifiable
module is an unresolved question, matching the guard directories' own
stated convention ("DEFAULT POSTURE ON AMBIGUITY IS DENY",
``coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py``).

THE RESIDUAL THIS STATIC WALK CANNOT CLOSE. The basename leg of the walk
sees only a LITERAL ``fleet-mode.json`` string in source text — it cannot
see a path handed across a runtime boundary: a module that receives the
fleet-mode.json path as a string from ``fleet_mode_path()``, re-exports it,
or composes it from ``settings_home()`` at runtime, names neither the
module nor the basename statically. This is an accepted residual, not a
claim of exhaustiveness — it is exactly why C1 exposes
``fleet_mode_path()`` as a function rather than a raw string constant, and
why no module outside EXPECTED_READERS may re-export it.

IMPLEMENTATION CONSTRAINTS THAT MAKE "RED IN BOTH DIRECTIONS" REAL RATHER
THAN TAUTOLOGICAL:
    1. ``EXPECTED_READERS`` below is a HARDCODED LITERAL TUPLE, never
       derived from the walk itself — a derived-then-compared set would
       make the assertion pass forever, which is the original vacuity
       wearing the allowlist's clothes.
    2. ``reader_closure()`` is a FUNCTION TAKING A ROOT PATH, never a
       module that hardcodes the repo root — this is the seam that lets
       ``TestReaderClosureSelfVerification`` below run the red-in-both-
       directions property against a FIXTURE tree, never the real one.

HARD CONSTRAINTS THIS MODULE OBSERVES: no commit, no edit to any guard
module (this file only observes them), static analysis only (no import of
any guard module, no execution of any guard).

Spec backlink: state/dispatch-briefs/2026-08-28-the-fleet-gets-one-file-
and-the-floor-moves-to-the-reader/C5.md
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# --- targets ----------------------------------------------------------------

_TARGET_MODULES = (
    "coordinator_core.session.fleet_mode",
    "coordinator_core.session.mode_resolution",
)
_TARGET_BASENAME = "fleet-mode.json"


def is_test_module(dotted: str) -> bool:
    """True for a module that measures code rather than being it: any
    ``tests`` package segment, a ``test_*`` leaf, or a ``conftest`` leaf.
    Operates on dotted names, never OS paths."""
    parts = dotted.split(".")
    return "tests" in parts or parts[-1].startswith("test_") or parts[-1] == "conftest"


def production_readers(closure: frozenset[str]) -> frozenset[str]:
    """The floor's subject: every reader in ``closure`` that is not a test module."""
    return frozenset(m for m in closure if not is_test_module(m))


# The floor: PRODUCTION readers only, as a hardcoded literal tuple -- never
# derived from the walk below, never holding a test module (a test module is
# exempt by `is_test_module`, and `test_no_test_module_in_expected_readers`
# keeps it out). Each entry reaches the record through an edge measured by
# `reader_closure`; none gates a tool call on a mode value except through the
# sanctioned resolver.
#
#   * Hooks and guards (`hooks.*`, `bash_guards._firing_shape`,
#     `write_guards.nudge_em_code_dispatch`): `nudge_em_code_dispatch` is
#     advisory; `postuse_advisory_dispatch` reaches the record only by
#     `_resolve_subagent_identity` re-use (no mode value reaches its decision),
#     and `runtime_tripwire_em_check` / `stop_dispatch` ride that one edge.
#     `stop_dispatch` IS denial-capable, outside the CLASS test's top-level
#     scope; the symbol-level fact above, not its directory, is the analysis.
#     Retiring the cluster means moving `_resolve_subagent_identity` into a
#     leaf importing neither target. `_firing_shape` is a mechanism helper with
#     no MATCHERS, reaching the record through a test-specimen import.
#     `session_start_announce_job_mode` / `sessionstart_dispatch` announce the
#     resolved mode as SessionStart context only.
#   * Benchmarks (`benchmarks.*`): time the hooks / stage entry, never gate.
#   * `sizing_assemble` and its importers (`plan_assemble*`, `ops.sizing_resize`,
#     `ops.roadmap_blitz_stage`, `roadmap.blitz_stage`, `ops.dispatch_emit.*`,
#     `ops.plan_chain.*`, `ops.workflow_fire.op`, `ops.plan_backfill_warp_executing`,
#     `hooks.submit_*`): ONE edge, `sizing_assemble`'s lazy `mode_resolution`
#     import, counted per module. They compute a route, script or staging
#     manifest; `hooks.submit_*` rewrite or deny a Workflow submission on its
#     own args shape, and no mode value reaches that decision. None is under
#     `write_guards/` or `bash_guards/`.
#   * `ops.fleet.mode_control`: the op that writes the record.
EXPECTED_READERS = (
    "coordinator_core.bash_guards._firing_shape",
    "coordinator_core.benchmarks.ask_stage_size_l_profile",
    "coordinator_core.benchmarks.bash_dispatch_probe",
    "coordinator_core.benchmarks.hook_entry_cost",
    "coordinator_core.benchmarks.shim_prototype_dispatcher",
    "coordinator_core.hooks.nudge_em_code_dispatch",
    "coordinator_core.hooks.postuse_advisory_dispatch",
    "coordinator_core.hooks.runtime_tripwire_em_check",
    "coordinator_core.hooks.session_start_announce_job_mode",
    "coordinator_core.hooks.sessionstart_dispatch",
    "coordinator_core.hooks.stop_dispatch",
    "coordinator_core.hooks.submit_bug_blitz",
    "coordinator_core.hooks.submit_engine_ask",
    "coordinator_core.ops.dispatch_emit.ask_compose",
    "coordinator_core.ops.dispatch_emit.ask_gate",
    "coordinator_core.ops.dispatch_emit.ask_roadmap",
    "coordinator_core.ops.dispatch_emit.ask_stage",
    "coordinator_core.ops.dispatch_emit.cli",
    "coordinator_core.ops.dispatch_emit.mark_landed",
    "coordinator_core.ops.dispatch_emit.op",
    "coordinator_core.ops.dispatch_emit.plan_route",
    "coordinator_core.ops.fleet.mode_control",
    "coordinator_core.ops.plan_backfill_warp_executing",
    "coordinator_core.ops.plan_chain",
    "coordinator_core.ops.plan_chain.driver",
    "coordinator_core.ops.plan_chain.plan_stage",
    "coordinator_core.ops.roadmap_blitz_stage",
    "coordinator_core.ops.sizing_resize",
    "coordinator_core.ops.workflow_fire.op",
    "coordinator_core.plan_assemble",
    "coordinator_core.plan_assemble.predicates.substrate_seven_dim",
    "coordinator_core.plan_assemble.residue",
    "coordinator_core.roadmap.blitz_stage",
    "coordinator_core.sizing_assemble",
    "coordinator_core.write_guards.nudge_em_code_dispatch",
)

# The one allowlist addition the brief itself pre-disposed (see module
# docstring's "WHY nudge_em_code_dispatch IS ALLOWLISTED" section). It is
# also the only EXPECTED_READERS member that sits at the TOP LEVEL of a
# guard directory, which is the scope `test_no_denial_shaped_guard_reaches_
# the_record` below actually enumerates CLASS over -- see that test's own
# docstring for why top-level-only mirrors the brief's own measurement.
_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE = "coordinator_core.write_guards.nudge_em_code_dispatch"

# Top-level-only: matches exactly `write_guards/*.py` / `bash_guards/*.py`
# (no further dot), the same non-recursive scope the brief's own "71
# classified / 15 unclassified" measurement was taken over. A module under
# `write_guards/tests/` or `bash_guards/tests/` is test-support code, never
# itself a registered guard (no MATCHERS/PRIORITY), so it is out of this
# check's scope by construction, not by omission.
_GUARD_TOP_LEVEL_PREFIXES = (
    "coordinator_core.write_guards.",
    "coordinator_core.bash_guards.",
)

_DENIAL_SHAPED_CLASSES = frozenset({"hard-deny"})


def _is_top_level_guard_module(dotted: str) -> bool:
    for prefix in _GUARD_TOP_LEVEL_PREFIXES:
        if dotted.startswith(prefix) and "." not in dotted[len(prefix):]:
            return True
    return False


#: The structural mark of a module that can actually be DISPATCHED as a guard.
#: Living in `write_guards/` or `bash_guards/` does not make a module a guard --
#: both directories also hold mechanism and helper modules that no dispatcher
#: can ever fire. `MATCHERS` is what the dispatcher keys on, so declaring it is
#: the decidable, structural answer to "is this a guard", and it does not depend
#: on a naming convention or a docstring.
_GUARD_DISPATCH_MARKER = "MATCHERS"


# The one module this walk exempts from the inverted default below: a
# mechanism helper at the TOP LEVEL of a guard directory whose absence of a
# dispatch surface is UNAMBIGUOUS, not merely undetected. `_firing_shape.py`
# carries no MATCHERS and no PRIORITY -- it is the classification mechanism
# the firing-shape gate itself runs, not a registered guard -- and it reaches
# the fleet record only through a function-local import of
# `nudge_em_code_dispatch` used as a test specimen. No other module may be
# added here without the same unambiguous-absence analysis recorded inline.
_UNAMBIGUOUS_NON_GUARD_MODULES = frozenset({
    "coordinator_core/bash_guards/_firing_shape.py",
})


def _declares_guard_dispatch_surface(path: Path) -> bool:
    """True unless `path` can be shown to POSITIVELY LACK a dispatchable
    ``MATCHERS`` surface -- i.e. ambiguity resolves to IS-A-GUARD, never to
    not-a-guard, matching the guard directories' own stated convention
    ("default posture on ambiguity is deny",
    ``coordinator_core/bash_guards/block_disarm_marker_sentinel_creation.py``).

    WHY THIS EXISTS, so it is not "simplified" back into a directory check.
    Without it, `_module_class()` returning None collapses two facts that carry
    opposite obligations: "this is a guard whose CLASS I could not read" (an
    unresolved question -- fail loud, default posture on ambiguity is deny) and
    "this is not a guard, so it has no CLASS to read" (nothing to resolve).

    INVERTED DEFAULT (review finding, 2026-08-29). The original version of
    this function matched only a literal top-level `MATCHERS = ...` /
    `MATCHERS: T = ...` statement in `tree.body`, and returned False -- "not a
    guard" -- for everything else, including a `MATCHERS` bound by
    `from ... import MATCHERS`, by a conditional top-level assignment inside
    an `if`/`try` block, or dynamically via `globals()["MATCHERS"] = ...`.
    Any of those shapes lands the module in `non_guard_readers`, where CLASS
    is never checked at all -- exactly the "validated key instead of an
    absent reader" failure this file's docstring says it exists to prevent,
    and once such a module is allowlisted the tests go permanently green with
    zero further pushback on its denial-shaped nature. The fix is not to
    enumerate more binding forms and hope the list is complete: this function
    now returns True (treat as guard, subject to the CLASS check) for
    anything it cannot positively prove lacks a MATCHERS surface. The only
    modules exempted from that default are named explicitly in
    `_UNAMBIGUOUS_NON_GUARD_MODULES` above, with the reasoning recorded
    inline -- never inferred structurally.

    This narrows the SUBJECT of the denial-shaped check, never its STRENGTH: a
    module declaring MATCHERS with no derivable CLASS still fails loud below.
    Measured over the tree at the time of writing, `write_guards/` has 48
    dispatchable guards and every one declares CLASS; `bash_guards/` has 29, of
    which exactly two (`guard_grep_via_bash`, `guard_powershell_via_bash`)
    declare no CLASS -- those two are precisely what the fail-loud path is for,
    and neither reaches the fleet record today.

    Non-guard modules are NOT thereby exempt from the floor: they still have to
    appear in `EXPECTED_READERS`, so a helper newly reaching the record still
    turns `test_reader_set_matches_allowlist_exactly` red. Only the CLASS
    question stops applying to them, because they have no CLASS to ask about.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return True  # unreadable -- cannot prove absence, default to guard
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return True  # unparseable -- cannot prove absence, default to guard

    # Positive detection, over the WHOLE tree (not just tree.body), so a
    # conditional top-level assignment inside `if`/`try` is not missed: a
    # direct MATCHERS bind, a `from ... import MATCHERS` re-export, or the
    # bare string literal "MATCHERS" appearing anywhere (a dynamic bind via
    # `globals()["MATCHERS"] = ...` / `setattr(..., "MATCHERS", ...)` cannot
    # be proven a bind statically, so its mere presence is treated as
    # ambiguous-therefore-guard rather than an attempt to prove intent).
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == _GUARD_DISPATCH_MARKER:
                    return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if _GUARD_DISPATCH_MARKER in (alias.name, alias.asname):
                    return True
        elif isinstance(node, ast.Constant) and node.value == _GUARD_DISPATCH_MARKER:
            return True

    posix_path = path.as_posix().replace("\\", "/")
    if any(posix_path.endswith(exempt) for exempt in _UNAMBIGUOUS_NON_GUARD_MODULES):
        return False
    return True


# This test module inevitably names "fleet-mode.json" in its own docstring
# and error messages -- exclude it from its own basename-reader detection
# rather than let prose about the mechanism count as use of it.
_SELF_MODULE = "coordinator_core.tests.test_fleet_mode_reader_set"


# --- static import-graph walk (never imports anything) ----------------------


def _dotted_module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _package_of(module_name: str, *, is_package: bool) -> str:
    if is_package:
        return module_name
    parts = module_name.split(".")
    return ".".join(parts[:-1])


def _resolve_from_import(current_module: str, is_package: bool, node: ast.ImportFrom) -> set[str]:
    targets: set[str] = set()
    if node.level == 0:
        base = node.module or ""
    else:
        pkg_parts = _package_of(current_module, is_package=is_package).split(".")
        # level=1 -> same package; level=2 -> one package up; etc.
        up = node.level - 1
        anchor_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
        anchor = ".".join(p for p in anchor_parts if p)
        base = f"{anchor}.{node.module}" if node.module else anchor
    if not base:
        return targets
    targets.add(base)
    for alias in node.names:
        if alias.name == "*":
            continue
        targets.add(f"{base}.{alias.name}")
    return targets


def _module_imports(path: Path, root: Path) -> tuple[set[str], bool]:
    """Returns (imported-module-candidates, names-the-fleet-basename-literal)."""
    source = path.read_text(encoding="utf-8")
    names_basename = _TARGET_BASENAME in source
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return set(), names_basename

    current_module = _dotted_module_name(root, path)
    is_package = path.name == "__init__.py"
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                dotted = alias.name
                imports.add(dotted)
                pieces = dotted.split(".")
                for i in range(1, len(pieces)):
                    imports.add(".".join(pieces[:i]))
        elif isinstance(node, ast.ImportFrom):
            imports |= _resolve_from_import(current_module, is_package, node)
    return imports, names_basename


def reader_closure(root: Path, package: str = "coordinator_core") -> frozenset[str]:
    """Static, non-importing walk of every ``.py`` file under ``root/package``.

    Returns the dotted-module-name set of every module whose TRANSITIVE
    import closure reaches one of ``_TARGET_MODULES``, unioned with every
    module that names ``_TARGET_BASENAME`` as a literal in its own source.
    Test modules are included; the floor filters them with
    ``production_readers``. Takes a root path (never hardcodes the repo root)
    so it can run against a fixture tree -- see
    ``TestReaderClosureSelfVerification`` below.
    """
    pkg_root = root / package
    forward_edges: dict[str, set[str]] = {}
    basename_readers: set[str] = set()

    for path in sorted(pkg_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        module_name = _dotted_module_name(root, path)
        imports, names_basename = _module_imports(path, root)
        forward_edges[module_name] = imports
        if names_basename:
            basename_readers.add(module_name)

    reverse_edges: dict[str, set[str]] = {}
    for importer, targets in forward_edges.items():
        for target in targets:
            reverse_edges.setdefault(target, set()).add(importer)

    reached: set[str] = set()
    frontier = list(_TARGET_MODULES)
    seen_targets = set(_TARGET_MODULES)
    while frontier:
        current = frontier.pop()
        for importer in reverse_edges.get(current, ()):
            if importer in _TARGET_MODULES:
                continue
            if importer not in reached:
                reached.add(importer)
                if importer not in seen_targets:
                    seen_targets.add(importer)
                    frontier.append(importer)

    reached |= {m for m in basename_readers if m not in _TARGET_MODULES}
    reached.discard(_SELF_MODULE)
    return frozenset(reached)


# --- CLASS derivation (static, never imports) --------------------------------


def _module_class(path: Path) -> str | None:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "CLASS" for t in node.targets):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path(root: Path, dotted: str) -> Path:
    return root.joinpath(*dotted.split(".")).with_suffix(".py")


# --- the floor, over the real tree -------------------------------------------


def test_reader_set_matches_allowlist_exactly():
    """RED in both directions: a new production reader appearing, or a listed
    reader disappearing, both fail this equality -- never a subset/superset
    check. Test modules are exempt by `is_test_module`."""
    root = _repo_root()
    computed = production_readers(reader_closure(root))
    expected = frozenset(EXPECTED_READERS)

    unexpected = computed - expected
    missing = expected - computed

    assert not unexpected, (
        f"module(s) {sorted(unexpected)} now reach the fleet record "
        "(coordinator_core.session.fleet_mode / .mode_resolution) but are "
        "not in EXPECTED_READERS -- someone wired a new reader. Either "
        "widen EXPECTED_READERS with the same allowlist-vs-restructure "
        "analysis this file's docstring records for "
        f"{_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE}, or remove the new import."
    )
    assert not missing, (
        f"module(s) {sorted(missing)} are in EXPECTED_READERS but no "
        "longer reach the fleet record -- the reader was deleted (or its "
        "import removed) and the floor became vacuous again without "
        "anyone noticing. Update EXPECTED_READERS only if the removal was "
        "deliberate."
    )


def test_no_test_module_in_expected_readers():
    """Test modules are exempt by rule; listing one would be dead weight the
    floor's missing-direction check could never retire."""
    listed = sorted(m for m in EXPECTED_READERS if is_test_module(m))
    assert not listed, (
        f"test module(s) {listed} are in EXPECTED_READERS; they are exempt by "
        "`is_test_module` -- remove them."
    )


def test_no_denial_shaped_guard_reaches_the_record():
    """The complement this plan's first unknown resolved to: no TOP-LEVEL
    module under write_guards/ or bash_guards/ (the non-recursive scope the
    brief's own "71 classified / 15 unclassified" measurement was taken
    over -- a module under either directory's tests/ subpackage is test-
    support code, never itself a registered guard) whose CLASS is denial-
    shaped may reach the fleet record, and an unclassifiable top-level
    guard-directory module in the reader closure fails loud rather than
    being treated as advisory by default. Walks the FULL closure, so a
    guard-directory module is never exempted by a test-shaped name."""
    root = _repo_root()
    computed = reader_closure(root)

    # Living in a guard directory is not being a guard. Split the reachers on
    # the dispatch marker BEFORE asking any of them about CLASS -- see
    # `_declares_guard_dispatch_surface` for why collapsing these two is what
    # made this test fail loud on a mechanism module.
    guard_dir_readers = sorted(m for m in computed if _is_top_level_guard_module(m))
    guard_readers = [
        m for m in guard_dir_readers if _declares_guard_dispatch_surface(_module_path(root, m))
    ]
    non_guard_readers = [m for m in guard_dir_readers if m not in guard_readers]

    # Non-guard modules in these directories are out of the CLASS question's
    # subject, never out of the floor: each one still has to be in
    # EXPECTED_READERS, which `test_reader_set_matches_allowlist_exactly`
    # enforces. Assert that here too so this test names its own residual
    # rather than silently ignoring a set it deliberately does not grade.
    unlisted_non_guards = sorted(set(non_guard_readers) - set(EXPECTED_READERS))
    assert not unlisted_non_guards, (
        f"non-guard module(s) {unlisted_non_guards} under write_guards/ or "
        "bash_guards/ reach the fleet record without appearing in "
        "EXPECTED_READERS. They carry no CLASS to grade, but they are still "
        "readers and the allowlist has to name them."
    )

    # CLASS derivation runs BEFORE the list-equality assert below, so the
    # specific "CLASS cannot be statically derived" message can actually
    # fire -- previously this loop ran AFTER `assert guard_readers == [...]`,
    # which already failed the test for any non-allowlisted entry, making
    # this branch dead code (review finding: the docstring's claim about how
    # this fails loud was false because the branch could never execute).
    non_allowlisted_guard_readers = [
        m for m in guard_readers if m != _ALLOWLISTED_TOP_LEVEL_GUARD_MODULE
    ]
    for dotted in non_allowlisted_guard_readers:
        path = _module_path(root, dotted)
        cls = _module_class(path)
        if cls is None:
            # Every other dispatchable guard reaching the record must fail
            # loud: an unclassifiable guard is an unresolved question, never
            # silently treated as advisory (default posture on ambiguity is
            # deny).
            pytest.fail(
                f"{dotted} reaches the fleet record but its module-level CLASS "
                "cannot be statically derived -- an unclassifiable module is an "
                "unresolved question, never silently treated as advisory "
                "(default posture on ambiguity is deny). This is a known, "
                "unremediated finding (see this chunk's run report) -- "
                "disposing of it (classify or restructure the import away) is "
                "out of this file's scope."
            )

    assert guard_readers == [_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE], (
        f"dispatchable guard(s) reaching the fleet record: {guard_readers}. "
        f"Only {_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE} is allowlisted, on the "
        "allowlist-vs-restructure analysis this file's module docstring "
        "records. A guard that is not on this list must not reach the record "
        "at all -- the floor is an absent reader, not a validated key."
    )

    allowlisted_path = _module_path(root, _ALLOWLISTED_TOP_LEVEL_GUARD_MODULE)
    allowlisted_cls = _module_class(allowlisted_path)
    assert allowlisted_cls == "advisory", (
        f"{_ALLOWLISTED_TOP_LEVEL_GUARD_MODULE} is allowlisted on the "
        f"assumption its CLASS is 'advisory'; it is now {allowlisted_cls!r}. "
        "Re-run the allowlist-vs-restructure analysis in this file's module "
        "docstring -- the allowlisting was conditioned on this CLASS."
    )


# --- self-verification: the mechanism itself is red in both directions ------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestReaderClosureSelfVerification:
    """Proves reader_closure()'s red-in-both-directions property against a
    disposable fixture tree -- never the real repo -- per this file's own
    HARD CONSTRAINTS (the walk takes a root path for exactly this reason)."""

    def _base_fixture(self, tmp_path: Path) -> Path:
        root = tmp_path / "fixture_repo"
        _write(root / "coordinator_core" / "__init__.py", "")
        _write(root / "coordinator_core" / "session" / "__init__.py", "")
        _write(
            root / "coordinator_core" / "session" / "fleet_mode.py",
            "def read_fleet_mode():\n    return {}\n",
        )
        _write(
            root / "coordinator_core" / "session" / "mode_resolution.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        _write(root / "coordinator_core" / "unrelated" / "__init__.py", "")
        _write(
            root / "coordinator_core" / "unrelated" / "other.py",
            "import json\n",
        )
        return root

    def test_absent_reader_is_absent(self, tmp_path):
        root = self._base_fixture(tmp_path)
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.other" not in closure

    def test_direct_reader_appears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "direct_reader.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.direct_reader" in closure

    def test_indirect_reader_one_hop_away_appears(self, tmp_path):
        """The direct-import-only failure mode this walk is built to avoid:
        a module that imports a reader, not the record itself."""
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "direct_reader.py",
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        _write(
            root / "coordinator_core" / "unrelated" / "indirect_reader.py",
            "from coordinator_core.unrelated.direct_reader import read_fleet_mode\n",
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.indirect_reader" in closure

    def test_basename_only_reader_appears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        _write(
            root / "coordinator_core" / "unrelated" / "basename_reader.py",
            'PATH = "fleet-mode.json"\n',
        )
        closure = reader_closure(root)
        assert "coordinator_core.unrelated.basename_reader" in closure

    def test_removed_reader_disappears(self, tmp_path):
        root = self._base_fixture(tmp_path)
        reader_path = root / "coordinator_core" / "unrelated" / "direct_reader.py"
        _write(
            reader_path,
            "from coordinator_core.session.fleet_mode import read_fleet_mode\n",
        )
        assert "coordinator_core.unrelated.direct_reader" in reader_closure(root)

        # Remove the import (the reader is deleted / no longer reaches the
        # record) -- the floor's second direction: a listed reader must be
        # able to fall OUT of the closure, or the walk is a one-way ratchet.
        _write(reader_path, "import json\n")
        assert "coordinator_core.unrelated.direct_reader" not in reader_closure(root)

    def test_new_test_module_is_exempt_and_new_production_module_is_not(self, tmp_path):
        """Both sides of the exemption, through the floor's own composition
        (`production_readers(reader_closure(root))`): a new test module
        reaching the record leaves the floor's subject unchanged; a new
        production module reaching it lands in the subject, so the equality
        against EXPECTED_READERS would go red."""
        root = self._base_fixture(tmp_path)
        pkg = root / "coordinator_core" / "unrelated"
        reach = "from coordinator_core.session.fleet_mode import read_fleet_mode\n"
        _write(pkg / "tests" / "__init__.py", "")
        _write(pkg / "tests" / "test_new_thing.py", reach)
        _write(pkg / "test_sibling.py", reach)
        _write(pkg / "conftest.py", reach)

        full = reader_closure(root)
        assert "coordinator_core.unrelated.tests.test_new_thing" in full
        assert production_readers(full) == frozenset()

        _write(pkg / "new_production_reader.py", reach)
        subject = production_readers(reader_closure(root))
        assert subject == frozenset({"coordinator_core.unrelated.new_production_reader"})
        assert subject - frozenset(EXPECTED_READERS)

    def test_guard_directory_module_is_never_exempt_by_leaf_name(self):
        """`is_test_module` keys on the dotted name; a guard module is not a
        test module unless it sits under a `tests` package or is `test_*`."""
        assert not is_test_module("coordinator_core.write_guards.nudge_em_code_dispatch")
        assert not is_test_module("coordinator_core.bash_guards._firing_shape")
        assert not is_test_module("coordinator_core.benchmarks.hook_entry_cost")
        assert is_test_module("coordinator_core.write_guards.tests.test_x")
        assert is_test_module("coordinator_core.ops.plan_chain.tests._chain_repo")
        assert is_test_module("coordinator_core.sizing_assemble.conftest")
        assert is_test_module("coordinator_core.hooks.test_auto_push")

    def test_expected_readers_tuple_is_not_derived_from_the_walk(self):
        """Guards against the exact vacuity this file's docstring names:
        EXPECTED_READERS must be assigned from a literal tuple of string
        constants, never a call into reader_closure() (or anything else
        computed). If this ever becomes `EXPECTED_READERS =
        reader_closure(...)`, the equality test above would pass forever."""
        source = Path(__file__).read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(Path(__file__)))
        assign_node = None
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "EXPECTED_READERS" for t in node.targets
            ):
                assign_node = node
                break
        assert assign_node is not None, "EXPECTED_READERS assignment not found"
        assert isinstance(assign_node.value, ast.Tuple), (
            "EXPECTED_READERS must be a literal tuple expression, not a "
            f"computed value ({type(assign_node.value).__name__})."
        )
        for element in assign_node.value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
                "every EXPECTED_READERS element must be a literal string "
                f"constant, found {ast.dump(element)}"
            )
