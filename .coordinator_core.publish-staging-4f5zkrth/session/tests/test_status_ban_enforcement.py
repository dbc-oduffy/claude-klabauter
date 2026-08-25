"""
coordinator_core.session.tests.test_status_ban_enforcement — executable form
of the status-ban negative-spec in `harness_registry.py`'s module docstring.

Spec backlink: `coordinator_core/session/harness_registry.py` module
docstring, "Negative-spec" block (updatedAt/statusUpdatedAt bullet) and its
2026-08-14 EM ruling.

The prose in that docstring is AUTHORITATIVE. If this test and that prose
ever drift, the docstring wins and this file is updated to match it — never
the reverse.

Two legs, both scoped to `coordinator_core/session/` (this package):

  (a) `updatedAt` / `statusUpdatedAt` appear nowhere in this package except
      inside `harness_registry.py`'s own module docstring.
  (b) No liveness/reachability/claim-verdict module reads `.status` off a
      registry record (a name bound from `snapshot()` / `lookup()` /
      `self_record()`, or a `record`/`rec`-named loop variable).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SESSION_DIR = Path(__file__).resolve().parent.parent
THIS_FILE = Path(__file__).resolve()

BANNED_TOKENS = ("updatedAt", "statusUpdatedAt")

VERDICT_MODULES = (
    "liveness.py",
    "reachability.py",
    "stale_claims.py",
    "claims.py",
    "claim_index.py",
)


def _all_session_py_files():
    return sorted(
        p
        for p in SESSION_DIR.rglob("*.py")
        if p.resolve() != THIS_FILE and "__pycache__" not in p.parts
    )


def test_updatedAt_only_in_harness_registry_docstring():
    """
    Enforces the negative-spec bullet: "`updatedAt` and `statusUpdatedAt`
    are read NOWHERE in this module, at any call site, forever" — extended
    here to the whole `session/` package per the 2026-08-14 EM ruling. A hit
    means someone reintroduced a busy/idle-transition timestamp as a
    liveness signal, which this fleet has already paid for twice (DoE
    642195ba, follow-up 88929bea).
    """
    hr_path = SESSION_DIR / "harness_registry.py"
    violations = []

    for path in _all_session_py_files():
        text = path.read_text(encoding="utf-8")

        if path == hr_path:
            tree = ast.parse(text)
            docstring = ast.get_docstring(tree, clean=False) or ""
            for token in BANNED_TOKENS:
                if token not in docstring:
                    continue
                # Confirm every occurrence in the file falls inside the
                # module docstring by removing it once and re-checking.
                stripped = text.replace(docstring, "", 1)
                if token in stripped:
                    violations.append(
                        f"{path}: {token!r} appears outside the module "
                        f"docstring's negative-spec block"
                    )
            continue

        for token in BANNED_TOKENS:
            if token in text:
                violations.append(f"{path}: banned token {token!r} found")

    assert not violations, (
        "status-ban negative-spec violated (harness_registry.py docstring, "
        "updatedAt/statusUpdatedAt bullet): " + "; ".join(violations)
    )


class _RegistryStatusReadFinder(ast.NodeVisitor):
    """
    Tracks names bound to registry-record-shaped values (dict values from
    `snapshot()`, results of `lookup()`/`self_record()`, or `record`/`rec`
    loop variables) and flags any `.status` attribute read off one of them.
    """

    _RECORD_CALL_SUFFIXES = ("snapshot", "lookup", "self_record")
    _RECORD_NAME_HINTS = ("record", "rec")

    def __init__(self):
        self.suspect_names: set[str] = set()
        self.hits: list[int] = []

    def _call_looks_like_registry_call(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Attribute):
            return func.attr in self._RECORD_CALL_SUFFIXES
        if isinstance(func, ast.Name):
            return func.id in self._RECORD_CALL_SUFFIXES
        return False

    def _mark_name(self, target: ast.AST):
        if isinstance(target, ast.Name):
            self.suspect_names.add(target.id)

    def visit_Assign(self, node: ast.Assign):
        value = node.value
        is_registry_ish = self._call_looks_like_registry_call(value)
        # x = snapshot().get(sid) / lookup(sid) / self_record()
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
            if value.func.attr in ("get",):
                inner = value.func.value
                if self._call_looks_like_registry_call(inner):
                    is_registry_ish = True
        if is_registry_ish:
            for t in node.targets:
                self._mark_name(t)
        self.generic_visit(node)

    def visit_For(self, node: ast.For):
        target = node.target
        # for k, v in snapshot().items(): v is record-ish.
        iter_node = node.iter
        iter_is_registry_items = False
        if isinstance(iter_node, ast.Call) and isinstance(iter_node.func, ast.Attribute):
            if iter_node.func.attr in ("items", "values"):
                inner = iter_node.func.value
                if self._call_looks_like_registry_call(inner):
                    iter_is_registry_items = True

        if iter_is_registry_items:
            if isinstance(target, ast.Tuple) and len(target.elts) == 2:
                self._mark_name(target.elts[1])
            else:
                self._mark_name(target)

        # Loop variable literally named record/rec (or *_record/*_rec),
        # regardless of source — the allowance the spec's own prose grants
        # display-only consumers still applies; this only flags a `.status`
        # *read*, not the binding itself.
        if isinstance(target, ast.Name) and any(
            target.id == hint or target.id.endswith(f"_{hint}")
            for hint in self._RECORD_NAME_HINTS
        ):
            self._mark_name(target)

        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr == "status" and isinstance(node.value, ast.Name):
            if node.value.id in self.suspect_names:
                self.hits.append(node.lineno)
        self.generic_visit(node)


@pytest.mark.parametrize("filename", VERDICT_MODULES)
def test_no_verdict_module_reads_status_off_registry_record(filename):
    """
    Enforces the negative-spec's `status` exception boundary: `status` "must
    NEVER be read as an input to any liveness, reachability, or
    claim-verdict computation anywhere in this tree." A hit here means a
    verdict module bound a name to a registry record (via `snapshot()`,
    `lookup()`, `self_record()`, or a `record`/`rec` loop variable) and then
    read `.status` off it.
    """
    path = SESSION_DIR / filename
    if not path.exists():
        pytest.skip(f"{filename} does not exist in coordinator_core/session/")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    finder = _RegistryStatusReadFinder()
    finder.visit(tree)

    assert not finder.hits, (
        f"status-ban negative-spec violated (harness_registry.py docstring, "
        f"`status` display-only exception): {filename} reads `.status` off "
        f"a registry-record-shaped name at line(s) {finder.hits} — status "
        f"must never feed a liveness/reachability/claim-verdict computation"
    )
