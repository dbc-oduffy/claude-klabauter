"""coordinator_core.tests.test_no_hand_built_legacy_share_root — the standing
gate on the sidecar share-root class: no non-test module may spell the RETIRED
share root ``state/subagent-share`` by hand.

WHY THIS EXISTS. The 2026-09 machinery relocation moved sidecar provisioning
from ``state/subagent-share/`` to ``<machinery_root>/subagent-share/`` and
repointed the writers. Four guards and six readers were never repointed, and
because every one of them fails open on a missing directory, each went
SILENTLY DEAD rather than erroring: ``block_em_hand_edit_pending_review_
integration`` and ``nudge_sentinel_retained_review_sidecar`` could not fire at
all, ``block_subagent_plan_body_write``'s narrowed deny went permanently
inert, and the close-ceremony review-receipt gate read a real receipt as
absent. Their own suites stayed green throughout, because the fixtures wrote
under the same stale root the code read.

WHAT REPLACES THE LITERAL. ``session.machinery_paths`` owns every spelling:
``share_dir``/``share_root`` for a WRITER (current root only) and
``share_dirs``/``share_roots`` for a READER (both roots, current first).
A reader that consults one root is the defect this gate makes unspellable.

WHY AST, NOT GREP. A substring scan over this corpus returns overwhelmingly
docstrings and prose — the legacy root is named in dozens of historical
citations and spec backlinks that are correct as written. Only a value
materialized as a Python constant can reach a path join, so this walks
``ast.Constant`` and skips docstring statements, mirroring
``test_no_legacy_touch_record_literal.py``'s discipline in this same corpus.

Allowlist: every entry is a relpath, named and dated, never a rationale-fit
match. Re-measured 2026-09-06 against this tree.
"""

from __future__ import annotations

import ast
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_SCAN_ROOTS = ("coordinator_core", os.path.join("coordinator", "bin"))

#: The retired spelling, in both separator dialects a constant can carry.
_LEGACY_FORMS = ("state/subagent-share", "state" + chr(92) + "subagent-share")

#: The adjacent-segment shape — `os.path.join(root, "state", "subagent-share")`
#: and `Path(root, "state", "subagent-share")` — which no single constant
#: spells but which builds the same path.
_LEGACY_SEGMENTS = ("state", "subagent-share")

_EXEMPT = {
    # The OWNER of both spellings. `LEGACY_SHARE_RELDIR` and
    # `legacy_share_root` are the one place the retired root is named.
    "coordinator_core/session/machinery_paths.py",
    # Extracts historical `state/subagent-share/` CITATIONS out of prose for
    # the pre-rewrite audit — the legacy root is its subject matter, not its
    # resolution target.
    "coordinator_core/ops/extract_cited_sidecars.py",
    # The relocation sweep itself: its bucket list names what it MOVES.
    "coordinator_core/ops/fleet_machinery_sweep.py",
    # Diff-noise filter over TRACKED `state/` lifecycle paths. The machinery
    # root is gitignored, so it can never appear in the diff this filters.
    "coordinator_core/ops/review_brightline_gate.py",
    # Citation surfaces that deliberately list BOTH roots on adjacent lines —
    # a citation to either was valid when it was written.
    "coordinator_core/ops/dispatch_emit/emit.py",
    "coordinator_core/ops/session/fix_concrete_path_citations.py",
    "coordinator_core/ops/session/guard_concrete_path_citations.py",
    "coordinator_core/write_guards/nudge_session_display_name_as_identifier.py",
    # FROZEN BYTES. `contract.cockpit_schema`/`emit_memo_schema` emit a schema
    # a sibling repo consumes; `test_emit_schema_pin.py` refuses any change to
    # the emitted bytes. Both hits are historical citations inside descriptions
    # (a dated eng-director ruling sidecar), not a resolution target.
    "coordinator_core/contract/emit_memo_schema.py",
    # Names WHERE the C12 slice sidecars were written at the time — a pointer
    # into the pre-relocation corpus, correct as history.
    "coordinator/bin/classify-engine-root-residue.py",
}


def _relpath(path: str) -> str:
    return os.path.relpath(path, _REPO_ROOT).replace(os.sep, "/")


def _iter_modules():
    for scan_root in _SCAN_ROOTS:
        base = os.path.join(_REPO_ROOT, scan_root)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "tests")]
            for name in filenames:
                if not name.endswith(".py") or name.startswith("test_"):
                    continue
                yield os.path.join(dirpath, name)


def _docstring_constant_ids(tree: ast.AST) -> set:
    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _adjacent_segment_hits(tree: ast.AST) -> list:
    """Constant sequences that spell the retired root as adjacent segments."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            values = node.args
        elif isinstance(node, ast.Tuple):
            values = node.elts
        else:
            continue
        literals = [
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else None
            for v in values
        ]
        for i in range(len(literals) - 1):
            if (literals[i], literals[i + 1]) == _LEGACY_SEGMENTS:
                hits.append(node.lineno)
    return hits


def _violations(path: str) -> list:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return []
    docstrings = _docstring_constant_ids(tree)
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and any(form in node.value for form in _LEGACY_FORMS)
        ):
            found.append(node.lineno)
    found.extend(_adjacent_segment_hits(tree))
    return sorted(set(found))


def test_no_module_hand_builds_the_legacy_share_root():
    offenders = {}
    for path in _iter_modules():
        rel = _relpath(path)
        if rel in _EXEMPT:
            continue
        lines = _violations(path)
        if lines:
            offenders[rel] = lines
    assert not offenders, (
        "these modules spell the retired share root `state/subagent-share` by "
        "hand: "
        + "; ".join(f"{rel}:{lines}" for rel, lines in sorted(offenders.items()))
        + ". Use coordinator_core.session.machinery_paths — share_dir/share_root "
        "for a writer, share_dirs/share_roots for a reader (both roots)."
    )


@pytest.mark.parametrize("rel", sorted(_EXEMPT))
def test_every_exemption_still_exists_and_still_needs_one(rel):
    """An exemption that no longer matches is a stale allowlist entry, not a
    harmless one — it hides the next real offender under the same path."""
    path = os.path.join(_REPO_ROOT, *rel.split("/"))
    assert os.path.isfile(path), f"exempt module no longer exists: {rel}"
    assert _violations(path), (
        f"{rel} no longer spells the retired share root — drop its _EXEMPT entry"
    )
