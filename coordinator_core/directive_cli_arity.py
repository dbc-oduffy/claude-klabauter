"""coordinator_core.directive_cli_arity -- detect undispatchable directives
and engine/CLI argv flag-parity skew.

An assembler's `directives[]` entry names a `cli` and an `args` list. If that
`cli` resolves to a `coordinator/bin/<cli>.py` script whose argparse parser
declares `add_subparsers(..., required=True)`, the script CANNOT run without
a subcommand token as `args[0]`. A directive naming such a CLI with `args=[]`
(or with a first token that isn't one of the declared subcommand names) is
silently undispatchable -- it will fail at invocation time, every time,
looking identical to a gate that always passes because it never runs.

This module derives "does this CLI require a subcommand" by INTROSPECTING
the target script's own argparse configuration (static AST walk over its
`add_subparsers`/`add_parser` calls) -- never a hand-maintained set of "CLIs
that need subcommands", which rots the moment a new subparsers-bearing CLI
ships and nobody remembers to add it to the list.

Spec backlink: instance bug fixed at `ddb8d03` in
`coordinator_core/workweek_complete/brief.py` (`d_step4b_4k_drift_guards`
split into four per-subcommand directives) -- this module generalizes the
check that would have caught it, per that fix's own follow-up task.

A second, related class lives here too: an assembler module COMPOSES the
argv it hands a `coordinator/bin/` entrypoint; that entrypoint parses it with
its own independent `argparse` parser. Nothing enforced the two stay in sync
-- `argv_parity_report` is the static oracle that does, per the incident at
`docs/plans/2026-08-15-bind-the-klabauter-publish-rows-into-a-parity-group.md`
(mirror `directives_commit_tail.py` emitted flags mirror `wsc-tail.py` had
not yet been published to accept; a strict `parse_args` rejected the whole
invocation). See that plan's C1 task body for the derivation this
implements.

Negative-spec:
    - Does NOT execute any target script or its argparse parser -- pure
      static AST inspection, so introspecting a CLI never has that CLI's own
      side effects (git mutation, network, etc).
    - Does NOT special-case `.required = True` (attribute-assignment) forms
      of marking a subparsers group required -- the codebase's own
      convention (verified at authoring time) only ever uses the
      `add_subparsers(required=True)` keyword form. A future CLI using the
      attribute form would silently read as "not required" here; if that
      shape appears, extend `_subparsers_required` rather than hand-list it.
    - A dotted `cli` name (e.g. `handoff.author_fork`) is an in-process op
      dispatch, not a `coordinator/bin/` script -- callers must exclude
      these before calling `requires_subcommand`, matched via
      `looks_like_in_process_op`.
    - A directive dict/builder site keyed `"op"` instead of `"cli"` (the
      migrated form -- see
      `docs/plans/2026-08-19-directives-name-an-op-not-a-cli.md`) is ALWAYS
      an in-process op dispatch, regardless of whether its value contains a
      `.` -- unlike a `"cli"`-keyed value, dot-containment is not the
      discriminator for an `"op"`-keyed site; key presence alone is. Such a
      site is still discovered by `_module_sites`/`emitted_option_tokens`
      (so it stays inside the publish gate's domain rather than silently
      vanishing from it) and is then excluded from the bin-script argv
      parity check the same way a dotted `"cli"` is.
    - `argv_parity_report` never imports or executes a module or a target
      script -- AST-only, same as the rest of this file. A pairing it cannot
      resolve statically is reported `unresolved`, NEVER silently `clean`;
      an unreadable pairing counting as passing is the exact failure this
      oracle exists to prevent.
    - A `Starred` element sitting in a flag's VALUE position (`["--flag",
      *[str(p) for p in paths]]`) is deliberately NOT treated with the same
      fail-safe caution as a `Starred` in flag-candidate position -- it is
      read as that flag's `nargs="+"`-style value list and does not force
      `unresolved`. This is a NARROW, DELIBERATE trade for coverage of the
      common readable multi-value-flag shape, not a claim of safety: a
      splice whose contents happen to include a dash-leading string
      (`*["--sneaky", "x"]`) is silently unseen -- it would neither surface
      as an emitted flag nor be flagged unresolved. If that shape is ever
      authored in this codebase, this module will not catch it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple, Optional

__all__ = [
    "looks_like_in_process_op",
    "resolve_bin_script",
    "requires_subcommand",
    "declared_option_strings",
    "declared_required_option_strings",
    "emitted_option_tokens",
    "argv_parity_report",
    "ParityPairing",
    "ParityReport",
]


def looks_like_in_process_op(cli_name: str) -> bool:
    """A dotted `cli` name (`handoff.author_fork`, `handoff.stamp_phase`,
    `handoff.supersede_predecessor`) names an in-process op dispatched
    through a different seam than a `coordinator/bin/` script -- shelling it
    out exits 127 (module docstring's own worked example). Any `cli` value
    containing a literal `.` is treated as this shape and excluded from the
    subcommand-arity check entirely; it never resolves to a bin script."""
    return "." in cli_name


def resolve_bin_script(cli_name: str, bin_dir: Path) -> Optional[Path]:
    """`cli_name` -> `<bin_dir>/<cli_name>.py`, or the bareword
    launcher-shim `<bin_dir>/<cli_name>` (no extension) -- the same
    two-candidate literal lookup every `_resolve_cli`/`_run_py_script`
    call site in this codebase already uses (`workweek_complete.apply`,
    `workday_complete.apply`, `workstream_complete.apply`,
    `merge_assemble.apply`, `baton_assemble.apply`, ...). Returns `None`
    when neither candidate exists on disk -- the CLI is not a
    `coordinator/bin/` script at all (a git-only handler like
    `worktree-prune`, or genuinely missing)."""
    py_path = bin_dir / f"{cli_name}.py"
    if py_path.is_file():
        return py_path
    bare_path = bin_dir / cli_name
    if bare_path.is_file():
        return bare_path
    return None


def _subparsers_required(tree: ast.AST) -> tuple[bool, set[str]]:
    """Walk `tree` for `<parser>.add_subparsers(..., required=True)` calls;
    collect the subcommand names registered on the SAME target variable via
    `<target>.add_parser("name", ...)`. Returns `(required, names)` --
    `required` is True iff at least one `add_subparsers` call in the module
    carries a literal `required=True` keyword; `names` is the union of every
    subcommand name found for any subparsers-holding variable in the module
    (best-effort -- a module with only one `add_subparsers` call, the
    observed shape everywhere in this codebase, resolves exactly)."""
    subparser_vars: set[str] = set()
    required = False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_subparsers"):
            continue
        is_required = any(
            kw.arg == "required" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        if is_required:
            required = True
        # Find the assignment target this call's result is bound to, e.g.
        # `sub = parser.add_subparsers(...)` -- walk parents is unavailable
        # on a bare ast.walk, so we re-scan Assign nodes whose value IS this
        # call object (identity match works since ast.walk yields the same
        # node objects the tree already holds).
        for assign in ast.walk(tree):
            if isinstance(assign, ast.Assign) and assign.value is node:
                for target in assign.targets:
                    if isinstance(target, ast.Name):
                        subparser_vars.add(target.id)

    names: set[str] = set()
    if subparser_vars:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "add_parser"):
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id in subparser_vars):
                continue
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                names.add(node.args[0].value)

    return required, names


def requires_subcommand(script_path: Path) -> tuple[bool, set[str]]:
    """`(required, subcommand_names)` for the argparse parser `script_path`
    builds, derived by static AST inspection -- never runs the script.

    `required` is True iff the script's own source declares
    `add_subparsers(..., required=True)` anywhere. `subcommand_names` is the
    set of literal subcommand names registered via `.add_parser("name")` on
    that subparsers object, best-effort (empty if none could be resolved,
    which happens for a dynamically-built subcommand set -- callers should
    treat an empty set as "presence-only checkable", not "no valid args
    exist").
    """
    source = script_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(script_path))
    return _subparsers_required(tree)


# ---------------------------------------------------------------------------
# Flag-parity oracle -- engine-module -> bin-script argv pairings.
# ---------------------------------------------------------------------------

#: Directories excluded from the assembler-module sweep, same discipline as
#: `test_directive_cli_subcommand_arity.py`'s `_iter_source_files`.
_EXCLUDED_DIR_NAMES = {"fact_contract_gate", "__pycache__"}

#: A whole-arg, list-expanding token naming a sibling directive id in the
#: SAME module -- `build_wsc_tail_directive`'s trailing
#: `args.append("{d-close-tail-args.argv}")` is the motivating shape. Match
#: group 1 is the producer's own `id_`.
_PRODUCER_REF_RE = re.compile(r"^\{(d-[A-Za-z0-9_.-]+)\.argv\}$")


class ParityPairing(NamedTuple):
    """One statically resolvable engine-module -> bin-script pairing.

    `unresolved` is True when the pairing (either side) could not be
    statically read in full -- `unaccepted`/`undeclared_required` are then
    EMPTY and MUST NOT be read as "clean"; callers check `unresolved` first.
    """

    module: str
    directive_id: str
    cli: str
    unresolved: bool
    unaccepted: frozenset
    undeclared_required: frozenset


class ParityReport(NamedTuple):
    pairings: tuple


def _iter_source_files(core_root: Path):
    """Production assembler sources under `core_root` -- excludes test
    files/dirs and `_EXCLUDED_DIR_NAMES`, same population
    `test_directive_cli_subcommand_arity.py` scans for `directives[]`
    literals."""
    for path in sorted(core_root.rglob("*.py")):
        if any(part in _EXCLUDED_DIR_NAMES for part in path.parts):
            continue
        if "tests" in path.parts:
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        yield path


def _module_level_str_literals(tree: ast.Module) -> dict[str, str]:
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.targets[0].id] = node.value.value
    return consts


def _resolve_str_const(node: Optional[ast.AST], module_consts: dict[str, str]) -> Optional[str]:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in module_consts:
        return module_consts[node.id]
    return None


def _looks_like_flag(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith("-") and value not in ("-", "--")


def _tokens_from_elts(elts: list, module_consts: dict[str, str]):
    """Reads one args-list-literal's elements as a `[flag, value, flag,
    value, ...]` sequence -- the convention every assembler in this
    codebase uses (a literal flag string followed by its, usually dynamic,
    value). Returns `(tokens, unresolved, positionals, producer_refs)`.

    A Constant string starting with `-` (not bare `-`/`--`) is a flag
    token. The NEXT element in this same list is treated as that flag's
    value and skipped regardless of its own shape (this is what lets
    `["--sid", sid]` resolve even though `sid` is a runtime Name -- it is a
    VALUE position, not a flag-candidate position) -- UNLESS that next
    element itself resolves to a literal dash-prefixed Constant, in which
    case it is NOT consumed as a value: it is re-processed as its own flag
    token. This is load-bearing for two consecutive literal `store_true`
    flags in one list (`args += ["--foo", "--bar"]`) -- treating the
    second as a swallowed "value" would silently drop it from the emitted
    set, exactly the false-clean the HARD CONSTRAINT below forbids. A
    Constant string not starting with `-` is a positional/subcommand
    token. Any OTHER element in a flag-candidate position (a Name not
    bound to a module-level literal, an f-string, a helper call, a bare
    `Starred`) forces `unresolved` -- HARD CONSTRAINT: it is never
    silently dropped, since a dynamically computed flag token sitting
    there would otherwise vanish from the emitted set and a real skew
    would read clean. A bare `Starred` sitting in a flag's VALUE position
    (`["--deleted-paths", *[str(p) for p in paths]]`, the `nargs="+"`-style
    multi-value-flag shape) is DIFFERENT from a `Starred` flag candidate
    and is NOT held to the same constraint: it is consumed as that flag's
    value list and does not itself set `unresolved` -- argparse consumes
    those tokens as the preceding option's values by construction, so
    treating the splice as a possible hidden flag is over-conservative for
    the single most common multi-value-flag shape in this codebase.
    Scanning resumes normally after a value-position `Starred` --
    `["--a", *xs, "--b"]` still yields BOTH `--a` and `--b`; the splice
    consumes only its own value slot, not the rest of the list. A
    value-position splice whose CONTENTS happen to include a dash-leading
    string (`*["--sneaky", "x"]`) is NOT detected -- a deliberate, narrow
    trade for coverage of the common readable shape, not a claim of
    safety; see this module's negative-spec block.
    """
    tokens: set[str] = set()
    positionals: set[str] = set()
    producer_refs: set[str] = set()
    unresolved = False
    expect_value = False

    for el in elts:
        is_starred = isinstance(el, ast.Starred)
        value = None if is_starred else _resolve_str_const(el, module_consts)

        if expect_value and is_starred:
            # A `*[str(p) for p in paths]` splice sitting in a flag's VALUE
            # position is consumed as that flag's value list -- argparse
            # (a `nargs="+"` option, the shape every such splice in this
            # codebase composes for) consumes exactly those runtime tokens
            # as the preceding flag's values by construction, so this is
            # NOT held to the flag-candidate `Starred` constraint above.
            # Scanning resumes normally on the NEXT element -- the splice
            # consumes only its own value slot, not the remainder of the
            # list, so a trailing literal flag after it is still read.
            expect_value = False
            continue
        if expect_value and not _looks_like_flag(value):
            expect_value = False
            continue
        expect_value = False

        if is_starred:
            unresolved = True
            continue
        if value is None:
            unresolved = True
            continue
        ref = _PRODUCER_REF_RE.match(value)
        if ref:
            producer_refs.add(ref.group(1))
        elif _looks_like_flag(value):
            tokens.add(value)
            expect_value = True
        else:
            positionals.add(value)

    return tokens, unresolved, positionals, producer_refs


_EMPTY_VAR = (set(), False, set(), set())


def _collect_list_var_tokens(func: ast.AST, module_consts: dict[str, str]) -> dict[str, tuple]:
    """For every local `Name` in `func` built up as a list of argv tokens
    (`x = [...]`, `x: list[str] = [...]`, `x += [...]`, `x.append(...)`,
    `x.extend([...])`), unions every contributing statement's tokens --
    including ones nested under `if`/`for`, since a conditionally-composed
    flag is still a flag the target script must accept on SOME call. Order
    of statement visitation does not matter: each contribution is merged by
    set union, never overwritten."""
    vars_: dict[str, tuple] = {}

    def merge(name: str, contribution: tuple) -> None:
        cur = vars_.get(name, _EMPTY_VAR)
        tokens, unresolved, positionals, producer_refs = contribution
        vars_[name] = (
            cur[0] | tokens,
            cur[1] or unresolved,
            cur[2] | positionals,
            cur[3] | producer_refs,
        )

    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            value = node.value
            if not isinstance(target, ast.Name) or value is None:
                continue
            if isinstance(value, ast.List):
                merge(target.id, _tokens_from_elts(value.elts, module_consts))
            elif isinstance(value, ast.Name) and value.id in vars_:
                merge(target.id, vars_[value.id])
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
            if isinstance(node.value, ast.List):
                merge(node.target.id, _tokens_from_elts(node.value.elts, module_consts))
            else:
                merge(node.target.id, (set(), True, set(), set()))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            name = node.func.value.id
            attr = node.func.attr
            if attr == "append" and node.args:
                merge(name, _tokens_from_elts([node.args[0]], module_consts))
            elif attr == "extend" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.List):
                    merge(name, _tokens_from_elts(arg0.elts, module_consts))
                else:
                    merge(name, (set(), True, set(), set()))

    return vars_


def _resolve_args_expr(node: ast.AST, module_consts: dict[str, str], list_vars: dict[str, tuple]) -> tuple:
    if isinstance(node, ast.List):
        return _tokens_from_elts(node.elts, module_consts)
    if isinstance(node, ast.Name) and node.id in list_vars:
        return list_vars[node.id]
    return (set(), True, set(), set())


def _find_directive_dict_builders(tree: ast.Module) -> dict[str, dict]:
    """Discovers any module-level helper function whose `return {...}` maps
    `"cli"`/`"args"` (and optionally `"id"`/`"id_"`) keys straight through
    to its OWN parameters -- e.g. `directives_commit_tail._directive(id_,
    cli, args, ...)`. A call to such a helper elsewhere in the module is
    then a directive-composing site, recognised generically by this shape
    rather than by hand-listing the helper's name."""
    builders: dict[str, dict] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        params = [a.arg for a in node.args.args]
        cli_param = args_param = id_param = None
        is_op_key = False
        for stmt in ast.walk(node):
            if not (isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict)):
                continue
            d = stmt.value
            for key, val in zip(d.keys, d.values):
                if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                    continue
                if key.value == "cli" and isinstance(val, ast.Name) and val.id in params:
                    cli_param = val.id
                elif key.value == "op" and isinstance(val, ast.Name) and val.id in params:
                    # Migrated form, same discriminator as
                    # `_site_from_dict_literal` -- key presence, not dots.
                    cli_param = val.id
                    is_op_key = True
                elif key.value == "args" and isinstance(val, ast.Name) and val.id in params:
                    args_param = val.id
                elif key.value in ("id", "id_") and isinstance(val, ast.Name) and val.id in params:
                    id_param = val.id
        if cli_param and args_param:
            builders[node.name] = {
                "cli": cli_param,
                "args": args_param,
                "id": id_param,
                "params": params,
                "is_op_key": is_op_key,
            }
    return builders


def _site_from_dict_literal(node: ast.Dict, module_consts: dict[str, str], list_vars: dict[str, tuple]):
    cli_node = args_node = id_node = None
    is_op_key = False
    for key, val in zip(node.keys, node.values):
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if key.value == "cli":
            cli_node = val
        elif key.value == "op":
            # Migrated form -- see module docstring's negative-spec entry on
            # `"op"`-keyed sites: ALWAYS in-process, key presence alone is
            # the discriminator, not dot-containment of the value.
            cli_node = val
            is_op_key = True
        elif key.value == "args":
            args_node = val
        elif key.value in ("id", "id_"):
            id_node = val
    if cli_node is None or args_node is None:
        return None
    cli = _resolve_str_const(cli_node, module_consts)
    directive_id = _resolve_str_const(id_node, module_consts)
    tokens, unresolved, positionals, producer_refs = _resolve_args_expr(args_node, module_consts, list_vars)
    if cli is None:
        unresolved = True
    return {
        "id": directive_id,
        "cli": cli,
        "is_op_key": is_op_key,
        "tokens": tokens,
        "unresolved": unresolved,
        "positionals": positionals,
        "producer_refs": producer_refs,
    }


def _site_from_builder_call(node: ast.Call, builders: dict[str, dict], module_consts: dict[str, str], list_vars: dict[str, tuple]):
    if not (isinstance(node.func, ast.Name) and node.func.id in builders):
        return None
    sig = builders[node.func.id]
    params = sig["params"]

    def resolve_param(param_name: Optional[str]) -> Optional[ast.AST]:
        if param_name is None or param_name not in params:
            return None
        idx = params.index(param_name)
        if idx < len(node.args):
            return node.args[idx]
        for kw in node.keywords:
            if kw.arg == param_name:
                return kw.value
        return None

    cli_expr = resolve_param(sig["cli"])
    args_expr = resolve_param(sig["args"])
    id_expr = resolve_param(sig["id"])
    if cli_expr is None or args_expr is None:
        return {
            "id": None,
            "cli": None,
            "is_op_key": sig.get("is_op_key", False),
            "tokens": set(),
            "unresolved": True,
            "positionals": set(),
            "producer_refs": set(),
        }
    cli = _resolve_str_const(cli_expr, module_consts)
    directive_id = _resolve_str_const(id_expr, module_consts)
    tokens, unresolved, positionals, producer_refs = _resolve_args_expr(args_expr, module_consts, list_vars)
    if cli is None:
        unresolved = True
    return {
        "id": directive_id,
        "cli": cli,
        "is_op_key": sig.get("is_op_key", False),
        "tokens": tokens,
        "unresolved": unresolved,
        "positionals": positionals,
        "producer_refs": producer_refs,
    }


def _has_add_argument_call(tree: ast.AST) -> bool:
    """True iff `tree` contains at least one `<obj>.add_argument(...)` call
    anywhere -- distinguishes "this file builds an argparse parser with
    zero currently-declared flags" (legitimately empty) from "this file
    builds no parser at all" (a bin-script trampoline forwarding argv to an
    imported `main`, the shape `declared_option_strings` and
    `declared_required_option_strings` cannot read a flag set from
    directly)."""
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
        for node in ast.walk(tree)
    )


def _resolve_trampoline_target(script_path: Path, tree: ast.AST) -> Optional[Path]:
    """One-hop resolution for a `coordinator/bin/<cli>.py` trampoline that
    declares no argparse parser of its own: find a
    `from coordinator_core.<pkg...> import main` (any alias) import
    anywhere in `tree` -- the shape every such trampoline in this codebase
    uses (`lint-frontmatter.py` -> `coordinator_core.frontmatter.
    schema_validate.main`, worked example in this module's spec backlink)
    -- and resolve that dotted module path to its file, relative to the
    repo root inferred from `script_path`'s own location
    (`<repo_root>/coordinator/bin/<cli>.py`, so `repo_root` is two parents
    above the script).

    Returns `None` when no such import exists, or the resolved file is not
    on disk -- callers MUST treat that as "cannot resolve", never as "the
    target module declares zero flags" (see the HARD CONSTRAINT on
    `declared_option_strings`).

    One hop only, by design (module docstring's scope note) -- this is not
    a general import-graph walker: an import chain nested two levels deep
    (trampoline -> module A -> module B's parser) is NOT followed and
    reads as unresolved.
    """
    repo_root = script_path.parents[2]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not (node.module == "coordinator_core" or node.module.startswith("coordinator_core.")):
            continue
        if not any(alias.name == "main" for alias in node.names):
            continue
        module_path = repo_root.joinpath(*node.module.split("."))
        candidate = module_path.with_suffix(".py")
        if candidate.is_file():
            return candidate
        init_candidate = module_path / "__init__.py"
        if init_candidate.is_file():
            return init_candidate
    return None


def _own_or_trampoline_tree(script_path: Path) -> Optional[ast.Module]:
    """The AST to read a declared/declared-required flag set from:
    `script_path`'s own tree if it declares at least one `add_argument`
    call, else the one-hop trampoline target's tree (`None` if no hop
    resolves, or the hop target itself declares no `add_argument` either --
    both read as "cannot determine the flag set", never as "zero flags
    declared")."""
    source = script_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(script_path))
    if _has_add_argument_call(tree):
        return tree
    target = _resolve_trampoline_target(script_path, tree)
    if target is None:
        return None
    target_source = target.read_text(encoding="utf-8", errors="replace")
    target_tree = ast.parse(target_source, filename=str(target))
    if not _has_add_argument_call(target_tree):
        return None
    return target_tree


def declared_option_strings(script_path: Path) -> tuple[set[str], bool]:
    """`(declared, saw_unresolved)` -- every literal `--flag`/`-f` option
    string declared via `add_argument(...)` anywhere in `script_path` (any
    parser or subparser object), by static AST walk. A non-literal
    positional argument to `add_argument` (a dynamically-built flag string)
    is not counted as declared and flips `saw_unresolved` True, so a caller
    can distinguish "no unknown flags" from "could not tell".

    HARD CONSTRAINT: when `script_path` itself declares no parser (a
    trampoline forwarding argv to an imported `main`), this follows ONE hop
    via `_own_or_trampoline_tree` to read the flag set from the resolved
    target instead. When neither `script_path` nor a resolved hop target
    declares any `add_argument` call, this returns `(set(), True)` --
    `saw_unresolved`, NEVER a bare empty declared set standing in for "this
    CLI accepts nothing". An empty-but-resolved declared set (a real
    argparse parser with zero flags) is the only case that legitimately
    returns `(set(), False)`.
    """
    tree = _own_or_trampoline_tree(script_path)
    if tree is None:
        return set(), True
    declared: set[str] = set()
    saw_unresolved = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value.startswith("-") and arg.value not in ("-", "--"):
                    declared.add(arg.value)
            else:
                saw_unresolved = True
    return declared, saw_unresolved


def _subparser_variable_to_subcommand(tree: ast.AST) -> tuple[dict[str, str], set[str]]:
    """`(mapping, ambiguous)` -- `mapping` maps each parser-object variable
    name created via `<subparsers_holder>.add_parser("name", ...)` (and
    itself bound to a `Name` target, e.g. `p_a = sub.add_parser("a")`) back
    to the literal subcommand name it was registered under. `ambiguous` is
    the set of variable names this walk could NOT attribute to a single
    subcommand with confidence -- callers MUST treat an `add_argument` call
    whose owner variable is in `ambiguous` as unresolved, never as
    top-level-by-default or as whichever subcommand happens to be in
    `mapping`. A name lands in `ambiguous` (and is removed from `mapping`,
    if present) for either of two reasons, both real authoring shapes:

    1. A loop-built or otherwise non-literally-named `add_parser` call
       (`for name in names: p = sub.add_parser(name)`) -- the subcommand
       name is a runtime value, not statically knowable, so the bound
       variable can be registered under any subcommand depending on which
       loop iteration produced it.
    2. Variable-name reuse/shadowing: the SAME variable name is bound to
       TWO (or more) `add_parser(...)` calls registering DIFFERENT literal
       subcommand names (`p = sub.add_parser("a"); ...; p =
       sub.add_parser("b")`) -- a plausible copy-paste/builder-loop pattern
       for 3+ subcommands. Keying by variable-name string alone would let
       the second assignment silently overwrite the first (last-write-wins),
       misattributing one subcommand's required flags to the other -- the
       exact false-positive/false-negative class the subcommand-scoping fix
       this module implements was meant to close.

    Reuses the SAME subparsers-holder discovery `_subparsers_required`
    performs (never re-derived) so this stays in lockstep with which
    variables the rest of the module already treats as subparsers objects.

    A `.add_parser(...)` call whose own return value is never bound to a
    `Name` (e.g. immediately chained: `sub.add_parser("a").add_argument(...)`)
    contributes no mapping entry -- callers must treat an `add_argument`
    call whose owner variable resolves to neither a top-level parser var nor
    a key of `mapping` as unresolved, never as "top-level", per this
    module's fail-safe discipline."""
    subparser_vars: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_subparsers"):
            continue
        for assign in ast.walk(tree):
            if isinstance(assign, ast.Assign) and assign.value is node:
                for target in assign.targets:
                    if isinstance(target, ast.Name):
                        subparser_vars.add(target.id)

    mapping: dict[str, str] = {}
    ambiguous: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_parser"):
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id in subparser_vars):
            continue
        target_names = [
            target.id
            for assign in ast.walk(tree)
            if isinstance(assign, ast.Assign) and assign.value is node
            for target in assign.targets
            if isinstance(target, ast.Name)
        ]
        if not target_names:
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            subcommand = node.args[0].value
            for name in target_names:
                if name in ambiguous:
                    continue
                if name in mapping and mapping[name] != subcommand:
                    ambiguous.add(name)
                    del mapping[name]
                else:
                    mapping[name] = subcommand
        else:
            # Non-literal subcommand name -- the bound variable(s) cannot be
            # attributed to a single subcommand at all.
            for name in target_names:
                ambiguous.add(name)
                mapping.pop(name, None)
    return mapping, ambiguous


def _required_flag_from_add_argument_call(node: ast.Call) -> Optional[tuple[Optional[str], bool]]:
    """`None` if `node` is not a `required=True` `add_argument(...)` call;
    else `(flag_or_None, flag_unresolved)` -- `flag_unresolved` True means
    the call IS `required=True` but its option-string positional could not
    be read as a literal (a dynamically-built flag name), which callers
    MUST fold into their own `saw_unresolved`, never drop silently."""
    is_required = any(
        kw.arg == "required" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        for kw in node.keywords
    )
    if not is_required:
        return None
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("-") and arg.value not in ("-", "--"):
            return arg.value, False
    return None, True


def declared_required_option_strings(script_path: Path, subcommand: Optional[str] = None) -> tuple[set[str], bool]:
    """`(required, saw_unresolved)` -- literal option strings declared
    `add_argument(..., required=True)` in `script_path`, SCOPED to a
    directive's actual invocation: a required flag declared on the
    TOP-LEVEL parser (an `add_argument` call whose owning variable is not a
    subcommand-parser object -- e.g. never routed through `.add_parser(...)`)
    always applies, regardless of `subcommand`. A required flag declared on
    a subparser's OWN parser object (`sub.add_parser("a")`'s returned,
    Name-bound object) applies only when `subcommand` equals that
    subparser's registered name -- a directive invoking subcommand A must
    never be flagged for subcommand B's required flags, and vice versa.

    `subcommand=None` (the default, and the only sensible value for a
    script with no `add_subparsers` at all) reads top-level-required flags
    only, matching this function's pre-scoping behavior for any script
    without subparsers.

    Same one-hop trampoline resolution and unresolved-not-empty contract as
    `declared_option_strings` -- see that function's HARD CONSTRAINT. An
    `add_argument` call whose owning variable cannot be statically read (a
    chained call with no `Name` target, e.g. `parser.add_argument(...)` on
    an inline expression) also flips `saw_unresolved` True -- ownership,
    like the flag string itself, is never guessed."""
    tree = _own_or_trampoline_tree(script_path)
    if tree is None:
        return set(), True
    subparser_var_to_subcommand, ambiguous_subparser_vars = _subparser_variable_to_subcommand(tree)
    required: set[str] = set()
    saw_unresolved = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"):
            continue
        result = _required_flag_from_add_argument_call(node)
        if result is None:
            continue
        flag, flag_unresolved = result
        if flag_unresolved:
            saw_unresolved = True
            continue
        owner = _add_argument_owner_var(node)
        if owner is None:
            saw_unresolved = True
            continue
        if owner in ambiguous_subparser_vars:
            # A loop-built, non-literally-named, or shadowed/reused
            # subparser variable -- cannot attribute this flag to a single
            # subcommand (or confidently to none), so the whole aspect must
            # read unresolved rather than fall through to "top-level".
            saw_unresolved = True
            continue
        owner_subcommand = subparser_var_to_subcommand.get(owner)
        if owner_subcommand is None or owner_subcommand == subcommand:
            required.add(flag)
    return required, saw_unresolved


def _add_argument_owner_var(node: ast.Call) -> Optional[str]:
    """The `Name` id `node` (an `add_argument(...)` call) is invoked on,
    e.g. `"parser"` for `parser.add_argument(...)` -- `None` for any other
    shape (a chained call, a subscript, an attribute chain), which callers
    must treat as unresolved ownership, never as top-level-by-default."""
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id
    return None


def emitted_option_tokens(module_path: Path) -> dict[str, set[str]]:
    """`cli_name -> emitted flag-token set` for every statically resolvable
    directive-composing site in `module_path` -- the `directives[]`
    dict-literal shape (`"cli"` + `"args"` sibling keys) plus the
    generalized helper-function-call shape (see
    `_find_directive_dict_builders`) that `directives_commit_tail.py`'s
    `_directive(id_, cli, args, ...)` uses. A `cli` that resolves to more
    than one site is unioned across sites (best-effort key collision, not
    expected in practice -- each directive id is authored once)."""
    result: dict[str, set[str]] = {}
    for site in _module_sites(module_path):
        cli = site["cli"]
        if cli is None:
            continue
        result.setdefault(cli, set())
        result[cli] |= site["tokens"]
    return result


def _module_sites(module_path: Path) -> list[dict]:
    source = module_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(module_path))
    module_consts = _module_level_str_literals(tree)
    builders = _find_directive_dict_builders(tree)

    sites: list[dict] = []
    for func in tree.body:
        if not isinstance(func, ast.FunctionDef):
            continue
        if func.name in builders:
            # This function IS a directive-dict template (e.g. `_directive`
            # itself) -- its own `return {...}` is the shape being
            # recognised, not an invocation of it.
            continue
        list_vars = _collect_list_var_tokens(func, module_consts)
        for node in ast.walk(func):
            site = None
            if isinstance(node, ast.Dict):
                site = _site_from_dict_literal(node, module_consts, list_vars)
            elif isinstance(node, ast.Call):
                site = _site_from_builder_call(node, builders, module_consts, list_vars)
            if site is not None:
                sites.append(site)

    # Phase 2: resolve whole-arg producer-token references against sibling
    # directive ids in the SAME module -- REQUIRED, see `_PRODUCER_REF_RE`.
    # This is a fixed-point (recursive, memoized) resolution, NOT a single
    # pass over `sites` in list order: a 2+-hop producer chain (A refs B,
    # B refs C) must fold C's tokens into A regardless of whether B was
    # visited before A in `sites` -- a single pass silently under-unions
    # whichever side of the chain happens to be resolved first, which is
    # exactly the false-clean class this oracle exists to prevent. A cycle
    # (including a direct self-reference) MUST terminate and resolve to
    # `unresolved`, never loop or silently drop the cycle's own tokens.
    by_id = {s["id"]: s for s in sites if s["id"]}
    resolved_cache: dict[int, tuple[set, bool]] = {}

    def resolve(site: dict, visiting: frozenset) -> tuple[set, bool]:
        key = id(site)
        cached = resolved_cache.get(key)
        if cached is not None:
            return cached
        if key in visiting:
            # Cycle/self-reference: cannot be resolved to a fixed point --
            # never cached, since the answer here is path-dependent; the
            # top-level caller for this site still resolves and caches its
            # own (unresolved) result once recursion unwinds.
            return set(), True
        tokens = set(site["tokens"])
        unresolved = site["unresolved"]
        next_visiting = visiting | {key}
        for ref_id in site["producer_refs"]:
            producer = by_id.get(ref_id)
            if producer is None:
                unresolved = True
                continue
            producer_tokens, producer_unresolved = resolve(producer, next_visiting)
            tokens |= producer_tokens
            unresolved = unresolved or producer_unresolved
        result = (tokens, unresolved)
        resolved_cache[key] = result
        return result

    for site in sites:
        tokens, unresolved = resolve(site, frozenset())
        site["tokens"] = tokens
        site["unresolved"] = unresolved

    return sites


def argv_parity_report(repo_root: Path) -> ParityReport:
    """The flag-parity oracle: every statically resolvable engine-module ->
    bin-script pairing under `repo_root/coordinator_core`, each reduced to
    `unaccepted` (emitted minus declared), `undeclared_required` (declared-
    required minus emitted), or `unresolved` when either side could not be
    read in full. Never imports or executes a target script -- AST only."""
    core_root = repo_root / "coordinator_core"
    bin_dir = repo_root / "coordinator" / "bin"
    pairings: list[ParityPairing] = []

    for module_path in _iter_source_files(core_root):
        rel_module = str(module_path.relative_to(repo_root)).replace("\\", "/")
        for site in _module_sites(module_path):
            cli = site["cli"]
            directive_id = site["id"] or ""
            if cli is None:
                pairings.append(ParityPairing(rel_module, directive_id, "", True, frozenset(), frozenset()))
                continue
            if site.get("is_op_key") or looks_like_in_process_op(cli):
                continue
            script_path = resolve_bin_script(cli, bin_dir)
            if script_path is None:
                pairings.append(ParityPairing(rel_module, directive_id, cli, True, frozenset(), frozenset()))
                continue
            declared, declared_unresolved = declared_option_strings(script_path)
            sub_required, sub_names = requires_subcommand(script_path)

            # Scope the required-flag check to the subcommand THIS directive
            # actually invokes (its `args[0]` positional) -- never the union
            # across every subparser the script declares (that is the false-
            # positive class this fix removes, module docstring's spec
            # backlink). Zero matches means the directive omits the
            # subcommand token entirely -- a genuine, determinable finding,
            # not an oracle limitation, so it still falls through to the
            # `<subcommand-required>` sentinel below. Two or more matches
            # means the positional set is ambiguous -- cannot statically
            # tell which subparser applies, so the WHOLE pairing reports
            # `unresolved`, never a verdict this oracle cannot stand behind.
            target_subcommand: Optional[str] = None
            subcommand_ambiguous = False
            if sub_required:
                matched = site["positionals"] & sub_names
                if len(matched) == 1:
                    target_subcommand = next(iter(matched))
                elif len(matched) > 1:
                    subcommand_ambiguous = True

            required, required_unresolved = declared_required_option_strings(
                script_path, subcommand=target_subcommand
            )
            if site["unresolved"] or declared_unresolved or required_unresolved or subcommand_ambiguous:
                pairings.append(ParityPairing(rel_module, directive_id, cli, True, frozenset(), frozenset()))
                continue
            # Explicit guard (not an emergent property of the two flags
            # above): an empty declared set must never by itself produce a
            # nonempty `unaccepted`, however that emptiness arose --
            # `declared_option_strings` already fails safe to
            # `saw_unresolved` for every case it recognizes, but this is a
            # second, independent backstop so a future regression there
            # cannot resurrect the false-positive class this oracle exists
            # to prevent (module docstring's HARD CONSTRAINT).
            if not declared and site["tokens"]:
                pairings.append(ParityPairing(rel_module, directive_id, cli, True, frozenset(), frozenset()))
                continue
            unaccepted = site["tokens"] - declared
            undeclared_required = set(required - site["tokens"])
            if sub_required and target_subcommand is None:
                undeclared_required.add("<subcommand-required>")
            pairings.append(
                ParityPairing(
                    rel_module,
                    directive_id,
                    cli,
                    False,
                    frozenset(unaccepted),
                    frozenset(undeclared_required),
                )
            )

    return ParityReport(tuple(pairings))
