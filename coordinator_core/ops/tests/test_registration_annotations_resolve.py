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

_OP_NAME_RE = re.compile(r'"([a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)"')


def _advertised_ops():
    from coordinator_core.ops import _EAGER_OP_MODULES  # noqa: PLC0415

    out = []
    for entry in _EAGER_OP_MODULES:
        module_path, note = entry[0], (entry[1] if len(entry) > 1 else "")
        if not note or "registers" not in note:
            continue
        claim = note.split("registers", 1)[1]
        for name in _OP_NAME_RE.findall(claim):
            out.append((module_path, name))
    return out


def test_the_table_advertises_something():
    advertised = _advertised_ops()
    assert len(advertised) > 20, f"parsed only {len(advertised)} advertised ops -- parser drift?"


@pytest.mark.parametrize("module_path,op_name", _advertised_ops())
def test_advertised_op_resolves(module_path, op_name):
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


# METHOD_NOT_FOUND, and `reap.py` there contained zero occurrences of the op
# an op may register through `_REGISTRY_MAP` or through a decorator in its own

_REGISTRY_MAP_PATH = "coordinator_core/ops/_registry_map.py"


def _repo_root():
    return Path(__file__).resolve().parents[3]


def _module_candidate_paths(module_path):
    stem = module_path.replace(".", "/")
    return (f"{stem}.py", f"{stem}/__init__.py")


def _advertised_at_head(table):
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

    via_map = {
        "coordinator_core/ops/session/reap.py": "def audit(): ...",
        _REGISTRY_MAP_PATH: '{"session.audit_unreapable": "coordinator_core.ops.session.reap"}',
    }
    assert _head_annotation_failures(advertised, via_map) == []

    assert len(_head_annotation_failures(advertised, {_REGISTRY_MAP_PATH: "{}"})) == 1
