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

import re

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
