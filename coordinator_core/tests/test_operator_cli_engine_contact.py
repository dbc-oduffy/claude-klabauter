"""Phase-3 regrowth gate for DR-276.

The operator-CLI surface was triaged once, in full, on 2026-08-06
(`state/audits/operator-cli-triage/`). This gate exists so that triage does not
have to happen again: it fails when a NEW entrypoint appears under
`coordinator/bin/` that reaches an op by plain in-process import without going
through `coordinator_core.cli_entry.run_op_main`, which is what records the
session scope-touch that keeps its writes out of `orphans` at the
`scoped_git_commit` sink.

Shape: baseline-and-new, the same idiom as
`coordinator_core/tests/test_home_resolution_lint.py`. The already-known
trampolines (enumerated in `operator_cli_inprocess_baseline.txt`, which is the
sole authority on the count — it shrinks as conversion proceeds, so any figure
quoted in prose here is stale by construction) are a baseline, not a failure —
converting them is incremental work,
and a gate that went red on all of them the day it landed would be turned off
rather than obeyed (CLAUDE.md § north star: ergonomics over enforcement, make
the correct path cheaper rather than walling off the wrong one). What fails the
build is **regrowth**: an entrypoint not in the baseline that bypasses the seam.

Negative-spec (RAG-bait):
    This gate does NOT require an entrypoint to be a registered op. DR-276
    explicitly rejects migrating CLIs into the op registry. It requires only
    that a CLI reaching an op in-process routes through the declare-write seam.

    An entrypoint that never touches the engine at all (bucket (a) of the
    triage: bootstrap-time, one-shot migrations, self-contained capabilities)
    is out of scope by construction and is not listed anywhere here — absence
    from the baseline is not a claim about it.

Spec backlink: docs/decisions/DR-276-operator-clis-record-session-writes-at-a.md
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BIN = REPO_ROOT / "coordinator" / "bin"
BASELINE_PATH = Path(__file__).parent / "operator_cli_inprocess_baseline.txt"

_SHEBANG = re.compile(rb"^#!.*python")


def _is_python_source(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as handle:
            return bool(_SHEBANG.match(handle.readline()))
    except OSError:
        return False


def _is_test(path: Path) -> bool:
    rel = path.relative_to(BIN)
    return "tests" in rel.parts or path.name.startswith("test_")


def _entrypoints() -> list[Path]:
    """Non-test, non-`lib/`, top-level sources under `coordinator/bin/`.

    This is the ~356 population the atlas names and the triage classified — see
    `state/audits/operator-cli-triage/00-census-and-registry-teeth.md` §1 for
    why the `*.py`-only denominator (474) is a different population and must not
    be substituted here.
    """
    return sorted(
        p
        for p in BIN.rglob("*")
        if p.is_file() and _is_python_source(p) and not _is_test(p) and p.parent == BIN
    )


def _is_private_ops_module(dotted: str) -> bool:
    """True when the final segment of an `coordinator_core.ops.*` path is private.

    `coordinator_core.ops.fleet._common` is a shared helper module; the op that
    lives beside it is `coordinator_core.ops.fleet.memo_send`. The leading
    underscore is this package's own marker for "not an op entrypoint".
    """
    return dotted.rsplit(".", 1)[-1].startswith("_")


def _imports_an_op_in_process(source: str) -> bool:
    """True when the file imports an op ENTRYPOINT from `coordinator_core.ops.*`.

    AST-based, never a substring scan: the triage's own first census was wrong
    because it keyed on the literal `coordinator_core` token and so counted
    docstring mentions as imports while missing `cc_invoke`-routed callers
    entirely. Do not replace this with a grep.

    A PRIVATE import from that package is not an op reach, and crediting it as
    one made this gate unfixable at three of its four live findings. The
    specimens: `coordinator/bin/cross-repo-memo` imports
    `ops.fleet._outbox_frontmatter_rules.validate_outbox_frontmatter`,
    `handoff-reconcile-close-terminal.py` imports
    `ops.fleet._common.handoff_archive_dest`, and
    `coordinator-harvest-deferrals` imports
    `ops.plan_status_transition._refuse_if_live_foreign_holder` — a
    frontmatter-rule table, a path derivation, and a read-only refusal
    predicate. All three route every actual mutation through
    `cc_invoke.route_mutation`, which spawns `coordinator_core.invoke` as a
    subprocess, so the ENGINE declares those writes and nothing lands in
    `orphans`. None of the three has a `main` to route, so the remedy this
    gate names (`run_op_main("coordinator_core.ops.<name>", ...)`) is not
    merely unnecessary for them but inapplicable — a gate whose only available
    disposition is its own baseline is measuring the wrong property.

    Fail-closed narrowness: the exemption requires the import to be private,
    either in the module path (`ops.fleet._common`) or in every name bound
    (`from ops.plan_status_transition import _helper`). A public name out of a
    public ops module — `from ops.cmd_autorun_guard import main` — still
    counts, aliased or not, and a plain `import coordinator_core.ops.<public>`
    still counts. Residual, stated rather than hidden: a private helper that
    itself writes undeclared files is outside this gate's reach. It always was
    — `run_op_main` can only route a module-level entrypoint — so the honest
    scope is op entrypoints, which is what this now measures.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if not module.startswith("coordinator_core.ops"):
                continue
            if _is_private_ops_module(module):
                continue
            if all(alias.name.startswith("_") for alias in node.names):
                continue
            return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("coordinator_core.ops") and not _is_private_ops_module(
                    alias.name
                ):
                    return True
    return False


_SEAM_NAMES = {"run_op_main", "recording_declared_writes"}
_SEAM_MODULE = "coordinator_core.cli_entry"


def _uses_the_seam(source: str) -> bool:
    """True when the file actually CALLS the seam, not merely names it.

    AST-based, never a substring scan: a bare `_SEAM not in source` check is a
    string search over the whole file, comments and docstrings included. The
    live instance that exposed this: `coordinator/bin/orphan-branch-sweep.py`
    still imports its op directly, but carries a comment reading "DR-276: NOT
    converted to run_op_main. This op is read-only..." — that comment contains
    the literal seam token, so a substring check reports the file as converted
    and silently drops it from the bypass set. A regrowth gate silenced by a
    comment stating the conversion was NOT done fails toward false-clean, and
    the more conscientious the note, the more certainly it disables the check.
    Do not replace this with a grep.

    An import of `run_op_main`/`recording_declared_writes` alone does not
    count: an entrypoint can import the name and never call it, which is the
    same false-clean shape as a comment mention. This requires an actual call
    to `run_op_main(...)` or a `with recording_declared_writes(...)` use.

    A bare name match on `Call.func` is its own false-clean hole: a locally
    defined shadow (`def run_op_main(*a, **k): ...`) or an unrelated
    attribute call (`anything.run_op_main()`) matches the literal string
    without ever touching `coordinator_core.cli_entry`. The name is bound to
    the module's actual import — `from coordinator_core.cli_entry import
    run_op_main` (including `as X` aliasing) for the bare-name form, or a
    module-level `import coordinator_core.cli_entry [as Y]` for the
    `Y.run_op_main(...)` attribute form. An import shape this cannot resolve
    is NOT credited as using the seam — fail closed, not open.

    A bound bare name is further checked for a module-scope REBINDING —
    `def run_op_main(...):`, `class run_op_main:`, or a plain `run_op_main =
    ...` assignment — anywhere in the file after (or before) the import. This
    catches the deeper false-clean shape the two independent `ast.walk`
    passes above cannot see on their own: a file that imports the real seam
    and then shadows the same local name with its own definition still
    contains both "an import of the seam" and "a call to a Name matching
    that local alias" — the two facts these passes each check for — even
    though the call actually reaches the shadow, not the import. Any
    module-scope rebinding of a credited bare name revokes its credit
    entirely; this is a narrow lint, not general binding-liveness/dataflow
    analysis, so a rebound name is disqualified outright rather than
    partially trusted.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    # Bare names bound to a `from coordinator_core.cli_entry import <seam>
    # [as <local>]` — maps the LOCAL name actually used at call sites back to
    # the real seam name it aliases.
    bound_bare_names: dict[str, str] = {}
    # Module aliases bound to `import coordinator_core.cli_entry [as <local>]`
    # — enables crediting `<local>.run_op_main(...)` attribute calls.
    module_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _SEAM_MODULE:
                for alias in node.names:
                    if alias.name in _SEAM_NAMES:
                        local = alias.asname or alias.name
                        bound_bare_names[local] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _SEAM_MODULE:
                    module_aliases.add(alias.asname or alias.name)

    if not bound_bare_names and not module_aliases:
        return False

    # A module-scope rebinding of a credited bare name — `def`/`async def`,
    # `class`, or a plain assignment target — shadows the import and revokes
    # its credit outright. Only module-scope statements are considered: a
    # rebinding nested inside a function/class body doesn't touch the
    # module-level name the import bound.
    rebound_names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if stmt.name in bound_bare_names:
                rebound_names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id in bound_bare_names:
                    rebound_names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id in bound_bare_names:
                rebound_names.add(stmt.target.id)

    for name in rebound_names:
        del bound_bare_names[name]

    if not bound_bare_names and not module_aliases:
        return False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in bound_bare_names:
                return True
        elif isinstance(func, ast.Attribute):
            if func.attr in _SEAM_NAMES and isinstance(func.value, ast.Name):
                if func.value.id in module_aliases:
                    return True
    return False


def _bypassing() -> list[str]:
    """Entrypoints importing an op in-process without routing through the seam.

    Paths are emitted POSIX-spelled (`coordinator/bin/x.py`), matching the
    baseline file's on-disk spelling. A native `str(PurePath)` here yields
    backslashes on Windows, which shares no element with the forward-slash
    baseline: every entry then reads as simultaneously NEW (regrowth failure)
    and STALE (delete-me warning), and the gate is red on the whole population
    regardless of the property it measures. Do not reintroduce `str(...)`.
    """
    found = []
    for path in _entrypoints():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _imports_an_op_in_process(source) and not _uses_the_seam(source):
            found.append(path.relative_to(REPO_ROOT).as_posix())
    return sorted(found)


def _baseline() -> set[str]:
    if not BASELINE_PATH.exists():
        return set()
    return {
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_seam_mention_in_comment_does_not_count_as_using_it():
    """The live regression: a comment naming the seam is not a call to it."""
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "# DR-276: NOT converted to run_op_main. This op is read-only...\n"
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_seam_mention_in_docstring_or_string_literal_does_not_count():
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        '"""This module has not been converted to run_op_main yet."""\n'
        '_NOTE = "see run_op_main for the converted shape"\n'
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_genuine_run_op_main_call_counts_as_using_the_seam():
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "from coordinator_core.cli_entry import run_op_main\n"
        "run_op_main('coordinator_core.ops.orphan_branch_sweep', ['x'])\n"
    )
    assert _imports_an_op_in_process(source)
    assert _uses_the_seam(source)


def test_genuine_recording_declared_writes_use_counts_as_using_the_seam():
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "from coordinator_core.cli_entry import recording_declared_writes\n"
        "with recording_declared_writes(cwd='.') as touched:\n"
        "    _op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert _uses_the_seam(source)


def test_local_shadow_of_seam_name_does_not_count_as_using_it():
    """A locally-defined `run_op_main` stub is not the real seam.

    Regression for the bare-`Call.func`-name-match hole: a file can define
    its own `run_op_main`/`recording_declared_writes` (or expose an unrelated
    `.run_op_main()` attribute) sitting alongside a direct op `main()` call,
    bypassing the recording seam entirely while still satisfying a check that
    matches on the literal name string. Same false-clean shape as the
    comment/docstring holes above, one level deeper.
    """
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "\n"
        "def run_op_main(*args, **kwargs):\n"
        "    pass\n"
        "\n"
        "run_op_main('x', [])\n"
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_imported_seam_shadowed_by_local_def_does_not_count():
    """A file that imports the real seam AND shadows it with a local `def`
    of the same name, then calls the shadow, must not be credited.

    Regression for the deeper false-clean hole aac8a34d0's hardening left
    open: two independent `ast.walk` passes each asked "is there an import
    of the seam anywhere" and "is there a call to a Name matching that local
    alias anywhere" without connecting the two through scope/order, so this
    shape satisfied both checks even though the call at runtime invokes the
    local stub, not `coordinator_core.cli_entry.run_op_main`.
    """
    source = (
        "from coordinator_core.cli_entry import run_op_main\n"
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "\n"
        "def run_op_main(*a, **k):\n"
        "    pass\n"
        "\n"
        "run_op_main('x')\n"
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_imported_seam_shadowed_by_reassignment_does_not_count():
    """Same shape as above, but the shadow is a plain assignment, not a
    `def` — the check must not narrowly special-case `FunctionDef`."""
    source = (
        "from coordinator_core.cli_entry import run_op_main\n"
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "\n"
        "run_op_main = lambda *a, **k: None\n"
        "\n"
        "run_op_main('x')\n"
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_unrelated_attribute_call_named_run_op_main_does_not_count():
    """`anything.run_op_main()` on an unrelated object is not the seam either."""
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "\n"
        "class _Unrelated:\n"
        "    def run_op_main(self):\n"
        "        pass\n"
        "\n"
        "_Unrelated().run_op_main()\n"
        "_op_main()\n"
    )
    assert _imports_an_op_in_process(source)
    assert not _uses_the_seam(source)


def test_aliased_seam_import_still_counts_as_using_it():
    """`from ... import run_op_main as X` must still be credited."""
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "from coordinator_core.cli_entry import run_op_main as _run\n"
        "_run('coordinator_core.ops.orphan_branch_sweep', ['x'])\n"
    )
    assert _imports_an_op_in_process(source)
    assert _uses_the_seam(source)


def test_module_attribute_seam_call_still_counts_as_using_it():
    """`cli_entry.run_op_main(...)` (module-attribute form) must be credited."""
    source = (
        "from coordinator_core.ops.orphan_branch_sweep import main as _op_main\n"
        "import coordinator_core.cli_entry as cli_entry\n"
        "cli_entry.run_op_main('coordinator_core.ops.orphan_branch_sweep', ['x'])\n"
    )
    assert _imports_an_op_in_process(source)
    assert _uses_the_seam(source)


def test_private_helper_import_from_an_ops_module_is_not_an_op_reach():
    """`from ops.<public> import _helper` is a helper import, not an op call.

    The live shape: `coordinator-harvest-deferrals` imports
    `plan_status_transition._refuse_if_live_foreign_holder`, a read-only
    refusal predicate, and routes its writes out through subprocess CLIs.
    There is no entrypoint here for `run_op_main` to route.
    """
    source = (
        "from coordinator_core.ops.plan_status_transition import "
        "_refuse_if_live_foreign_holder\n"
        "_refuse_if_live_foreign_holder('p', 'r', None)\n"
    )
    assert not _imports_an_op_in_process(source)


def test_import_from_a_private_ops_module_is_not_an_op_reach():
    """`ops.fleet._common` / `ops.fleet._outbox_frontmatter_rules` are shared
    helper modules; the underscore is the package's own not-an-op marker."""
    source = (
        "from coordinator_core.ops.fleet._common import handoff_archive_dest\n"
        "handoff_archive_dest(root, path)\n"
    )
    assert not _imports_an_op_in_process(source)


def test_public_op_entrypoint_import_still_counts_even_when_aliased():
    """The narrowing must not leak: a public name from a public ops module is
    an op reach however it is spelled, which is the whole population this gate
    was built to watch."""
    assert _imports_an_op_in_process(
        "from coordinator_core.ops.cmd_autorun_guard import main\n"
    )
    assert _imports_an_op_in_process(
        "from coordinator_core.ops.cmd_autorun_guard import main as _op_main\n"
    )
    assert _imports_an_op_in_process("import coordinator_core.ops.cmd_autorun_guard\n")


def test_mixed_public_and_private_names_still_count_as_an_op_reach():
    """The exemption requires EVERY bound name to be private — one public name
    alongside a private one is still an op reach (fail closed)."""
    source = (
        "from coordinator_core.ops.plan_status_transition import _helper, main\n"
    )
    assert _imports_an_op_in_process(source)


def test_no_new_entrypoint_bypasses_the_declare_write_seam():
    """Regrowth gate: a NEW in-process op caller must route through the seam."""
    current = set(_bypassing())
    baseline = _baseline()
    new = sorted(current - baseline)

    assert not new, (
        f"{len(new)} entrypoint(s) import coordinator_core.ops in-process without "
        f"routing through coordinator_core.cli_entry.run_op_main, so every file "
        f"they write lands unclaimed in `orphans` and is refused at the "
        f"scoped_git_commit sink:\n"
        + "\n".join(f"  {p}" for p in new)
        + "\n\nFix: call `run_op_main(\"coordinator_core.ops.<name>\", sys.argv[1:])` "
        "instead of importing `<name>.main` and calling it directly — see "
        "coordinator/bin/append-integrator-dispositions.py for the converted shape. "
        "If this entrypoint genuinely must not use the seam, add it to "
        f"{BASELINE_PATH.name} WITH a reason comment; an unexplained baseline entry "
        "is what this gate exists to prevent."
    )


def test_baseline_shrinks_and_never_silently_grows():
    """Converted entrypoints must leave the baseline, so it decays to empty.

    A stale baseline entry is not merely untidy: it re-authorizes a bypass that
    no longer exists, so the next regrowth in that file passes unnoticed.
    """
    current = set(_bypassing())
    baseline = _baseline()
    stale = sorted(baseline - current)

    if stale:
        warnings.warn(
            f"[operator-cli-engine-contact] {len(stale)} baseline entr(y/ies) no "
            f"longer bypass the seam and should be deleted from "
            f"{BASELINE_PATH.name}: {', '.join(stale[:10])}"
            + (" …" if len(stale) > 10 else ""),
            UserWarning,
            stacklevel=2,
        )


@pytest.mark.skipif(not BASELINE_PATH.exists(), reason="no baseline recorded yet")
def test_every_baseline_entry_still_exists_on_disk():
    """A baseline naming a deleted file is a carve-out nobody can audit."""
    missing = sorted(e for e in _baseline() if not (REPO_ROOT / e).exists())
    assert not missing, (
        "baseline names path(s) that no longer exist — delete them:\n"
        + "\n".join(f"  {p}" for p in missing)
    )
