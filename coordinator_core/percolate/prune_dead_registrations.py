"""prune-dead-registrations transform-kind: derive the payload's own op registry.

Closes the defect docs/plans/2026-08-10-the-payload-derives-its-own-op-registrations.md
describes: `coordinator_core/ops/__init__.py` ships into a percolated payload
byte-for-byte, so its `_EAGER_OP_MODULES` list keeps naming modules the publish
allowlist deliberately excludes from that same payload. Three of those rows
(`percolate.run`, `percolate.validate_store`, `percolate.check_inverse_drift`)
print an ImportError traceback to stderr on every CLI invocation on a published
install -- the source tree's registration list was never asymmetric from the
source tree's own file set, only from the PAYLOAD's.

This module owns the ONE resolver + transform pair for that asymmetry. `resolve_
module_file` is the SHARED SEAM: C2's `registration-liveness` guard kind imports
it directly rather than reimplementing module-path-to-file resolution -- a
divergent second resolver is exactly how this defect class returns (see the
`## Contract` section of the plan chunk this module implements).

Extended (bug: state/bug-backlog/2026-08-26-the-published-mirror-advertises-5-
ops-it-c85f7d77431d.yaml) to ALSO prune `coordinator_core/ops/_registry_map.py`
's `OP_MODULE_MAP` -- a second dotted-path registry naming the same modules
`_EAGER_OP_MODULES` names, shipped verbatim alongside it, and susceptible to
the exact same asymmetry (5 percolate ops advertised in the published mirror's
map with no owning module in that mirror's payload). Sharing this one
transform (`prune_dead_registrations`, `resolve_module_file`) rather than a
second pruner keeps the two registries from silently diverging again.

Negative-spec (anti-transliteration, AC1): this module carries zero repo-specific
scrub content -- no persona names, no codenames, no consumer-home literals. It
operates purely on Python AST structure and filesystem existence checks against
a caller-supplied `target_root`.

Spec backlink: pln-the-payload-derives-its-own-op-83f0ce § C1.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

#: The single module-level list this transform rewrites. Kept as a named
#: constant (not re-derived per call) so a future rename of the source list is
#: a one-line change here rather than a scattered string literal.
_TARGET_ASSIGN_NAME = '_EAGER_OP_MODULES'

#: Repo-relative path (posix-style, joined via Path for OS-correctness) to the
#: eager-import registry this transform prunes. Resolved against `target_root`,
#: never against the source tree (§ module docstring, AC1's silent-no-op trap).
_OPS_INIT_RELATIVE_PARTS = ('coordinator_core', 'ops', '__init__.py')

#: Repo-relative path to the lazy-import name->module seed
#: (`coordinator_core/ops/_registry_map.py::OP_MODULE_MAP`) this transform ALSO
#: prunes, alongside `_EAGER_OP_MODULES` above. Bug: state/bug-backlog/
#: 2026-08-26-the-published-mirror-advertises-5-ops-it-c85f7d77431d.yaml -- this
#: file was shipped VERBATIM by the mirror while the modules it names (e.g.
#: `coordinator_core.ops.percolate_run`) were excluded from the payload, so the
#: SAME asymmetry this module was written to close (§ module docstring) reopened
#: here because this second registry was never covered by the original
#: transform. Sharing this one pruner (rather than a second mechanism) keeps the
#: two registries from drifting apart the way `_EAGER_OP_MODULES` and
#: `OP_MODULE_MAP` themselves already can (§ `_registry_map.py`'s own
#: "best-effort" maintenance note).
_REGISTRY_MAP_RELATIVE_PARTS = ('coordinator_core', 'ops', '_registry_map.py')

# Generator-provenance declaration (coordinator_core/ops/generator_census). prune_dead_registrations
# writes only to `<target_root>/coordinator_core/ops/__init__.py`, where target_root is
# a caller-supplied publish-payload/mirror-tree path -- never this repo's own tracked
# ops/__init__.py.
GENERATES = []

#: The dotted-path prefix every `_EAGER_OP_MODULES` row carries in production
#: (`coordinator_core.ops.ping`, ...). Stripped from a row's module path when
#: `target_root` IS the `coordinator_core` payload itself (shape 'is', see
#: `resolve_ops_registry`) so resolution stays scoped to `target_root` instead
#: of walking up to `target_root.parent` -- the sandbox-escape hazard named in
#: the module docstring.
_PACKAGE_PREFIX = 'coordinator_core'


class AmbiguousOpsRegistryShapeError(RuntimeError):
    """Raised when `target_root` matches BOTH the 'contains' and 'is' shapes
    AND `target_root`'s own basename is `coordinator_core` -- the one layout
    where the 'contains' precedence (Review: code-reviewer, Finding 1, P2) is
    not provably safe (§ `resolve_ops_registry` docstring).
    """


def resolve_ops_registry(target_root: "str | Path") -> "tuple[Path | None, str]":
    """Locate `.../ops/__init__.py` under `target_root`, whichever of the two
    shapes `target_root` is in.

    `target_root` either CONTAINS `coordinator_core/` (most publish rows) or
    IS the `coordinator_core` payload itself (row 1 -- `engine.py::
    _repo_relative_path`'s docstring: `target_root` is correct on its own ONLY
    for the toplevel row; for every other row it is a dest SUBDIR). Probes the
    'contains' shape first, then the 'is' shape, and returns which shape
    matched so the caller can resolve each row's module path correctly (§
    `strip_payload_prefix`) -- never by resolving against `target_root.parent`,
    which would escape the publish-staging sandbox and resolve rows against
    the live mirror repo root instead (the guard's false-PASS hazard named in
    the module docstring).

    Returns `(None, 'contains')` when neither shape's `ops/__init__.py`
    exists -- the 'contains' default keeps the no-match case's shape stable
    for callers that only branch on `path is None`.

    Fail-loud invariant (Review: code-reviewer, Finding 1, P2): the 'contains'
    precedence above rests on the assumption that a row-1 payload
    (`target_root` IS the `coordinator_core` payload itself) never itself
    contains a subdirectory literally named `coordinator_core` -- nothing else
    enforces that assumption, and silently preferring 'contains' on a target
    that is actually row-1 shape would resolve every row against the WRONG
    (nested) `ops/__init__.py`, exactly the silent-wrong-shape defect class
    this module exists to close. So when `target_root`'s own basename IS
    `coordinator_core` (the row-1 marker) AND both shapes' `ops/__init__.py`
    exist, that assumption is demonstrably false for this target and this
    function raises `AmbiguousOpsRegistryShapeError` rather than silently
    picking one. Every OTHER target_root basename keeps today's 'contains'-
    first precedence unchanged -- this only narrows the one case where "both
    exist" is actually ambiguous instead of merely coincidental.
    """
    return _resolve_payload_file(
        target_root, _OPS_INIT_RELATIVE_PARTS, ('ops', '__init__.py'),
    )


def resolve_registry_map(target_root: "str | Path") -> "tuple[Path | None, str]":
    """Locate `.../ops/_registry_map.py` under `target_root`, mirroring
    `resolve_ops_registry`'s two-shape probe (§ that function's docstring) for
    the SECOND registry this transform prunes (§ `_REGISTRY_MAP_RELATIVE_PARTS`
    docstring). Kept as its own function -- rather than a param on
    `resolve_ops_registry` -- because the two files can (in principle) resolve
    to different shapes if a future publish row ships one but not the other;
    collapsing them into one call would hide that possibility.
    """
    return _resolve_payload_file(
        target_root, _REGISTRY_MAP_RELATIVE_PARTS, ('ops', '_registry_map.py'),
    )


def _resolve_payload_file(
    target_root: "str | Path",
    contains_relative_parts: "tuple[str, ...]",
    is_shape_relative_parts: "tuple[str, ...]",
) -> "tuple[Path | None, str]":
    """Shared two-shape probe behind `resolve_ops_registry`/`resolve_registry_map`
    (§ their docstrings for the 'contains'/'is' shape semantics and the
    ambiguous-basename fail-loud invariant)."""
    root = Path(target_root)
    contains_path = root.joinpath(*contains_relative_parts)
    is_shape_path = root.joinpath(*is_shape_relative_parts)
    if (
        root.name == _PACKAGE_PREFIX
        and contains_path.is_file()
        and is_shape_path.is_file()
    ):
        raise AmbiguousOpsRegistryShapeError(
            f'{root} is ambiguous: its own basename is {_PACKAGE_PREFIX!r} '
            f'(the row-1 "is the payload" marker) but it ALSO contains a '
            f'nested {contains_path} -- both {contains_path} and '
            f'{is_shape_path} exist, so resolve_ops_registry cannot tell '
            f'whether this target is row-1 shape (with a coincidentally '
            f'named nested subdir) or genuinely "contains" shape. Rename '
            f'the ambiguous subdirectory or resolve manually.'
        )
    if contains_path.is_file():
        return contains_path, 'contains'
    if is_shape_path.is_file():
        return is_shape_path, 'is'
    return None, 'contains'


def strip_payload_prefix(module_path: str, shape: str) -> str:
    """Strip the leading `coordinator_core.` segment from `module_path` when
    `shape == 'is'` (`target_root` IS the `coordinator_core` payload), so a
    subsequent `resolve_module_file(stripped, target_root)` call stays scoped
    to `target_root` rather than needing `target_root.parent` (§
    `resolve_ops_registry` docstring hazard). A no-op for shape 'contains',
    and a no-op for a module path that does not carry the prefix (defensive;
    every real `_EAGER_OP_MODULES` row does).
    """
    prefix = _PACKAGE_PREFIX + '.'
    if shape == 'is' and module_path.startswith(prefix):
        return module_path[len(prefix):]
    return module_path


def resolve_module_file(module_path: str, root: "str | Path") -> "Path | None":
    """Resolve a dotted module path to an existing file under `root`, or None.

    THE SHARED SEAM (§ module docstring): `a.b.c` -> `root/a/b/c.py`, falling
    back to `root/a/b/c/__init__.py` for a package row (e.g. the
    `coordinator_core.plugin_health.*`/`coordinator_core.install.*`/
    `coordinator_core.goals.*`/`coordinator_core.probes.*` rows this transform
    must also cover, per AC6 -- resolution here is general, never `ops/`-scoped).

    Resolution is always against the CALLER-SUPPLIED `root` -- never against
    this module's own source tree -- so a caller checking payload liveness gets
    a real answer instead of the source tree's "everything exists" no-op.
    """
    root_path = Path(root)
    rel_parts = module_path.split('.')
    module_file = root_path.joinpath(*rel_parts[:-1], rel_parts[-1] + '.py')
    if module_file.is_file():
        return module_file
    package_init = root_path.joinpath(*rel_parts, '__init__.py')
    if package_init.is_file():
        return package_init
    return None


def find_eager_op_modules_assign(tree: ast.Module) -> "ast.Assign | ast.AnnAssign | None":
    """Locate the module-level `_EAGER_OP_MODULES = [...]` assignment node.

    Matches both the plain `ast.Assign` shape (`_EAGER_OP_MODULES = [...]`,
    used by test fixtures) and the `ast.AnnAssign` shape (`_EAGER_OP_MODULES:
    List[Tuple[str, str]] = [...]`, the REAL production declaration in
    `coordinator_core/ops/__init__.py`). An `ast.Assign`-only walk silently
    matches zero rows against production -- the defect this transform exists
    to close (§ module docstring) -- so both node kinds must stay covered.

    THE SHARED SEAM (§ module docstring): `guards.check_registration_liveness`
    imports this finder directly rather than reimplementing the AST walk, so
    the two node-shape checks cannot drift apart again.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id == _TARGET_ASSIGN_NAME:
            return node
    return None


#: Back-compat alias for the pre-fix private name -- kept only for any
#: external caller that imported the underscore-prefixed spelling directly.
_find_eager_op_modules_assign = find_eager_op_modules_assign


_REGISTRY_MAP_ASSIGN_NAME = 'OP_MODULE_MAP'


def find_op_module_map_assign(tree: ast.Module) -> "ast.Assign | ast.AnnAssign | None":
    """Locate the module-level `OP_MODULE_MAP = {...}` assignment node in
    `coordinator_core/ops/_registry_map.py` -- the dict-literal sibling of
    `find_eager_op_modules_assign` above (§ `_REGISTRY_MAP_RELATIVE_PARTS`
    docstring). Matches both plain `ast.Assign` and the real production
    `ast.AnnAssign` shape (`OP_MODULE_MAP: Dict[str, str] = {...}`), for the
    same reason `find_eager_op_modules_assign` matches both.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                continue
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if isinstance(target, ast.Name) and target.id == _REGISTRY_MAP_ASSIGN_NAME:
            return node
    return None


def _is_live(lookup_path: str, target_root: "str | Path", liveness_root: "str | Path | None") -> bool:
    """True when the module ships: present under `target_root`, or -- for a sparse
    staged round -- already present under `liveness_root`, the row's real destination.

    Trap: a staged `target_root` holds only the paths this round changed, so resolving
    against it alone prunes every untouched op module (r17, 2026-10-09, shipped a
    registry with ~280 of 282 rows dropped). The destination is the full payload the
    previous complete round landed, so presence there is the same liveness answer a
    full round computes.
    """
    if resolve_module_file(lookup_path, target_root) is not None:
        return True
    return liveness_root is not None and resolve_module_file(lookup_path, liveness_root) is not None


def _prune_registry_map(
    target_root: "str | Path", liveness_root: "str | Path | None" = None
) -> list[str]:
    """Drop every `OP_MODULE_MAP` entry whose module value is unresolvable
    under `target_root` -- the `_registry_map.py` analogue of the
    `_EAGER_OP_MODULES` pruning `prune_dead_registrations` performs, sharing
    its splice-by-lineno technique (AC4: surviving entries preserved
    VERBATIM) and its resolver (`resolve_module_file`/`strip_payload_prefix`).
    Returns the sorted list of dropped module dotted-paths (the dict VALUE,
    matching the shape `prune_dead_registrations` already returns for
    `_EAGER_OP_MODULES` rows, so both files' drops can be reported through one
    caller-facing contract). No-op when `_registry_map.py` is absent, when
    `OP_MODULE_MAP` is not found, or when its value is not a dict literal.
    """
    registry_map_path, shape = resolve_registry_map(target_root)
    if registry_map_path is None:
        return []

    source = registry_map_path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    assign_node = find_op_module_map_assign(tree)
    if assign_node is None:
        return []

    dict_node = assign_node.value
    if not isinstance(dict_node, ast.Dict):
        return []

    dropped_line_numbers: set[int] = set()
    dropped_module_paths: list[str] = []

    for key_node, value_node in zip(dict_node.keys, dict_node.values):
        if key_node is None:
            continue  # a `**spread` entry -- not a real op-name row.
        if not isinstance(value_node, ast.Constant) or not isinstance(value_node.value, str):
            continue
        module_path = value_node.value
        lookup_path = strip_payload_prefix(module_path, shape)

        if _is_live(lookup_path, target_root, liveness_root):
            continue

        dropped_module_paths.append(module_path)
        start_line = key_node.lineno
        end_line = value_node.end_lineno or value_node.lineno
        dropped_line_numbers.update(range(start_line, end_line + 1))

    if not dropped_module_paths:
        return []

    source_lines = source.splitlines(keepends=True)
    kept_lines = [
        line for line_number, line in enumerate(source_lines, start=1)
        if line_number not in dropped_line_numbers
    ]
    registry_map_path.write_text(''.join(kept_lines), encoding='utf-8', newline="\n")

    dropped_module_paths.sort()
    print(
        f'prune_dead_registrations: pruned {len(dropped_module_paths)} dead '
        f'OP_MODULE_MAP row(s) from {registry_map_path} (not present under '
        f'this payload): {dropped_module_paths}',
        file=sys.stderr,
    )
    return dropped_module_paths


def prune_dead_registrations(
    target_root: "str | Path", *, liveness_root: "str | Path | None" = None
) -> list[str]:
    """Drop every `_EAGER_OP_MODULES` row unresolvable under `target_root`.

    Locates `ops/__init__.py` under `target_root` via `resolve_ops_registry`
    (whichever of the two shapes `target_root` is in -- see that function's
    docstring), `ast.parse`s it, and identifies every `_EAGER_OP_MODULES`
    tuple entry whose module path (§ `resolve_module_file`, via `strip_
    payload_prefix`) does not resolve to a file present under `target_root`.
    Surviving rows are preserved VERBATIM (AC4) -- dropped rows are spliced
    out by their own `lineno`/`end_lineno` line span, never via `ast.unparse`
    of the whole module (which would reformat every surviving row and destroy
    the byte-level diffability the mirror's audits depend on).

    No-op (returns `[]`, writes nothing) when neither shape's `ops/__init__.py`
    is present under `target_root`, or when every row already resolves --
    idempotent by construction: a second call over an already-pruned file finds
    nothing left to drop.

    Emits a one-line stderr summary of what was pruned (never a traceback) so a
    publish operator can see it and a caller can measure before/after (AC9).

    Known limitation (Review: code-reviewer, Finding 3, P3): a dropped row's line
    span is the AST tuple's own `lineno`..`end_lineno`, which does not include any
    comment line immediately preceding/between `_EAGER_OP_MODULES` entries -- an
    entry with an explanatory comment directly above it would leave that comment
    orphaned (still valid Python, still byte-verbatim on survivors, so this does
    not violate AC4, but the comment now describes nothing). Not present in the
    real `coordinator_core/ops/__init__.py` list today; no comment-reattachment
    machinery is implemented for it.

    Returns the sorted list of dropped module dotted-paths, from BOTH
    `_EAGER_OP_MODULES` (this file) and `OP_MODULE_MAP` (§ `_prune_registry_map`)
    -- one caller-facing contract for both registries this transform keeps
    live, rather than a second entry point a caller (`engine.py`'s
    `prune-dead-registrations` hook, `guards.py`'s defence-in-depth check)
    would have to remember to also call.

    `liveness_root` (§ `_is_live`) is required for a staged round and must be the
    row's real destination, in the same shape as `target_root`.
    """
    registry_map_dropped = _prune_registry_map(target_root, liveness_root)

    ops_init_path, shape = resolve_ops_registry(target_root)
    if ops_init_path is None:
        return registry_map_dropped

    source = ops_init_path.read_text(encoding='utf-8')
    tree = ast.parse(source)
    assign_node = find_eager_op_modules_assign(tree)
    if assign_node is None:
        return registry_map_dropped

    list_node = assign_node.value
    if not isinstance(list_node, ast.List):
        return registry_map_dropped

    dropped_line_numbers: set[int] = set()
    dropped_module_paths: list[str] = []

    for element in list_node.elts:
        if not isinstance(element, ast.Tuple) or not element.elts:
            continue
        module_path_node = element.elts[0]
        if not isinstance(module_path_node, ast.Constant) or not isinstance(module_path_node.value, str):
            continue
        module_path = module_path_node.value
        lookup_path = strip_payload_prefix(module_path, shape)

        if _is_live(lookup_path, target_root, liveness_root):
            continue

        dropped_module_paths.append(module_path)
        start_line = element.lineno
        end_line = element.end_lineno or element.lineno
        dropped_line_numbers.update(range(start_line, end_line + 1))

    if not dropped_module_paths:
        return registry_map_dropped

    source_lines = source.splitlines(keepends=True)
    kept_lines = [
        line for line_number, line in enumerate(source_lines, start=1)
        if line_number not in dropped_line_numbers
    ]
    ops_init_path.write_text(''.join(kept_lines), encoding='utf-8', newline="\n")

    dropped_module_paths.sort()
    print(
        f'prune_dead_registrations: pruned {len(dropped_module_paths)} dead '
        f'_EAGER_OP_MODULES row(s) from {ops_init_path} (not present under '
        f'this payload): {dropped_module_paths}',
        file=sys.stderr,
    )
    return sorted(dropped_module_paths + registry_map_dropped)
