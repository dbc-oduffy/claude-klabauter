"""Every op name the eager-import table ADVERTISES must actually resolve.

The failure this exists to stop has now happened three times, and MEMORY.md
records two of them ("killed op names live on in string-keyed guards", "a new
hooks op needs an eager import entry"). The third, found 2026-08-26: the eager
import table in `coordinator_core/ops/__init__.py` claimed
`coordinator_core.ops.completion_ops` "registers completion.reconcile_commits"
for three days after that op was killed and rebuilt from scratch under a PM
ruling. The module's own docstring said plainly it was gone. The registration
table -- the surface a reader checks FIRST to learn what exists -- went on
advertising it, and nothing noticed.

That is the whole class: an op name written down in one place and served from
another, with no mechanical link between them. A stale entry is not cosmetic --
it is the difference between "this op is unreachable" and "this op is
unguarded", and a reader cannot tell which without dispatching to find out.

This guard closes the annotation half specifically. It does NOT try to prove the
reverse (that every registered op is annotated): the annotations are prose and
deliberately partial, many entries carry "" on purpose, and demanding
completeness there would be a documentation mandate rather than a correctness
one. Advertising something that does not exist is the defect; saying nothing is
not.

→ docs/research/2026-08-26-the-ceremony-budget-is-spent-on-one-git-status.md
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler

#: `registers "a.b", "c.d" (provenance)` -- the shape every annotating entry in
#: the eager-import table uses. Only quoted, dotted names are treated as claims;
#: prose around them is ignored.
_OP_NAME_RE = re.compile(r'"([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)"')


def _advertised_ops():
    """(module_path, op_name) for every op name an annotation claims."""
    from coordinator_core.ops import _EAGER_OP_MODULES  # noqa: PLC0415

    out = []
    for entry in _EAGER_OP_MODULES:
        module_path, note = entry[0], (entry[1] if len(entry) > 1 else "")
        if not note or "registers" not in note:
            continue
        # Only the text AFTER the word "registers" names ops; provenance tails
        # routinely mention other ops as context.
        claim = note.split("registers", 1)[1]
        for name in _OP_NAME_RE.findall(claim):
            out.append((module_path, name))
    return out


def test_the_table_advertises_something():
    """Guard the guard: a parse that silently matches nothing would make every
    assertion below vacuously true, which is how this class of test rots."""
    advertised = _advertised_ops()
    assert len(advertised) > 20, f"parsed only {len(advertised)} advertised ops -- parser drift?"


@pytest.mark.parametrize("module_path,op_name", _advertised_ops())
def test_advertised_op_resolves(module_path, op_name):
    """An op the table names must be dispatchable, or the table is lying.

    A killed op is the common case and it fails here loudly: `get_op_handler`
    raises `OpSuspendedError` for one that was suspended, and returns None for
    one whose name no longer exists at all. Both are the same defect from a
    reader's side -- the table says it is there and it is not.

    Remedy is always the annotation, never a resurrection: strike the name.
    """
    try:
        handler = get_op_handler(op_name, {})
    except Exception as exc:  # noqa: BLE001 -- the message IS the finding
        pytest.fail(
            f"{module_path} advertises {op_name!r}, which does not dispatch: "
            f"{type(exc).__name__}: {exc}\n"
            f"Strike the name from the eager-import table's annotation; do not "
            f"resurrect the op to satisfy this test."
        )
    assert handler is not None, (
        f"{module_path} advertises {op_name!r}, but the registry does not serve "
        f"it (METHOD_NOT_FOUND). Strike the name from the annotation."
    )


# ---------------------------------------------------------------------------
# The HEAD leg: an annotation cannot be committed ahead of the op it names
# ---------------------------------------------------------------------------
#
# The worktree leg above is green on a tree whose HEAD is red, and that is not
# a corner case -- it is how the fourth recurrence happened, hours after the
# guard shipped. On 2026-08-26 commit `1e1f9f50d` added
# `registers "session.audit_unreapable"` to the eager-import table and
# published it, while that op's implementation (`ops/session/reap.py`,
# `_registry_map.py`, `op_scopes.py`) sat uncommitted in a peer's working tree.
# `get_op_handler` resolved the name -- from the worktree -- so every assertion
# above passed. The published engine, built from HEAD, returned
# METHOD_NOT_FOUND, and `reap.py` there contained zero occurrences of the op
# its own annotation advertised.
#
# On a branch ~50 concurrent sessions share, "the worktree has it" says nothing
# about whether HEAD does. What the fleet dispatches into is built from HEAD,
# so HEAD is the tree this claim has to be true of.
#
# Content check, not an import: HEAD's code is not importable in-process, and
# an op may register through `_REGISTRY_MAP` or through a decorator in its own
# module. Requiring the quoted name to appear in HEAD's copy of EITHER is
# robust to both registration styles and still catches the exact signature
# above -- zero occurrences anywhere.
#
# Two git spawns total (`ls-tree`, then one batched `cat-file --batch`), never
# one per advertised op: a per-item spawn here is what
# `coordinator_core.tests.test_no_unbatched_per_item_git_spawn` is watching.

_REGISTRY_MAP_PATH = "coordinator_core/ops/_registry_map.py"


def _repo_root():
    return Path(__file__).resolve().parents[3]


def _module_candidate_paths(module_path):
    """The repo-relative files that could hold *module_path*'s registrations."""
    stem = module_path.replace(".", "/")
    return (f"{stem}.py", f"{stem}/__init__.py")


def _advertised_at_head(table):
    """`_advertised_ops`' logic against a table read from HEAD instead of memory."""
    out = []
    for entry in table:
        module_path, note = entry[0], (entry[1] if len(entry) > 1 else "")
        if not note or "registers" not in note:
            continue
        claim = note.split("registers", 1)[1]
        for name in _OP_NAME_RE.findall(claim):
            out.append((module_path, name))
    return out


def _parse_cat_file_batch(stdout, paths):
    """`git cat-file --batch` output -> {repo-relative path: decoded source}.

    Records arrive in request order, each as ``<sha> <type> <size>\\n`` followed
    by ``<size>`` bytes and a newline; a missing object emits a single
    ``<request> missing`` line instead, consuming one request with no payload.
    """
    out = {}
    pos = 0
    for path in paths:
        nl = stdout.find(b"\n", pos)
        if nl == -1:
            break
        header = stdout[pos:nl].decode("utf-8", errors="replace")
        pos = nl + 1
        if header.endswith(" missing"):
            continue
        try:
            size = int(header.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            break
        out[path] = stdout[pos:pos + size].decode("utf-8", errors="replace")
        pos += size + 1
    return out


def _head_annotation_failures(advertised, contents):
    """Every advertised name HEAD's own sources do not mention.

    Split out of the test body so the comparison is exercisable against
    synthetic content -- a guard whose only run is against a green tree cannot
    show it would go red on a broken one.
    """
    registry_src = contents.get(_REGISTRY_MAP_PATH, "")

    failures = []
    for module_path, name in advertised:
        candidates = [contents.get(p, "") for p in _module_candidate_paths(module_path)]
        if not any(candidates):
            failures.append(
                f"{module_path} is advertised at HEAD but no such module is committed"
            )
            continue
        if any(
            f'"{name}"' in src or f"'{name}'" in src
            for src in (*candidates, registry_src)
        ):
            continue
        failures.append(
            f"HEAD advertises {name!r} (from {module_path}), but neither HEAD's copy "
            f"of that module nor {_REGISTRY_MAP_PATH} mentions it -- the annotation "
            f"was committed ahead of the op. Strike the name; it belongs in the "
            f"commit that lands the op."
        )
    return failures


def test_head_leg_goes_red_on_the_shape_it_exists_to_catch():
    """The 2026-08-26 signature, reconstructed: the annotation names an op the
    committed module and the committed registry map both say nothing about."""
    advertised = [("coordinator_core.ops.session.reap", "session.audit_unreapable")]

    served = {
        "coordinator_core/ops/session/reap.py": 'register_op("session.audit_unreapable")',
        _REGISTRY_MAP_PATH: "{}",
    }
    assert _head_annotation_failures(advertised, served) == []

    unserved = {
        "coordinator_core/ops/session/reap.py": 'register_op("session.reap")',
        _REGISTRY_MAP_PATH: "{}",
    }
    failures = _head_annotation_failures(advertised, unserved)
    assert len(failures) == 1
    assert "session.audit_unreapable" in failures[0]

    # Registered through the map rather than a decorator in the module: served.
    via_map = {
        "coordinator_core/ops/session/reap.py": "def audit(): ...",
        _REGISTRY_MAP_PATH: '{"session.audit_unreapable": "coordinator_core.ops.session.reap"}',
    }
    assert _head_annotation_failures(advertised, via_map) == []

    # The module named by the annotation was never committed at all.
    assert len(_head_annotation_failures(advertised, {_REGISTRY_MAP_PATH: "{}"})) == 1
