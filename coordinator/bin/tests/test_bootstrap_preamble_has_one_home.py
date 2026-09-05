"""AC18: the dispatch bootstrap preamble exists in exactly one place.

C16 collapsed ~190 verbatim copies of

    claude_klabauter_root = _resolve_claude_klabauter_root()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)

onto `cc_invoke.require_dispatch_engine_on_path()`. Without a guard the idiom
grows back one CLI at a time: it is three obvious lines, and every new
trampoline is written by copying the nearest existing one.

TWO SHAPES ARE CHECKED, because two were collapsed:

1. The CANONICAL body above (regex).
2. The TRY-WRAPPED variant (AST) --

       try:
           root = _resolve_claude_klabauter_root()
       except RuntimeError:
           ...never falls through...
       if root not in sys.path:
           sys.path.insert(0, root)

   Collapsing this moves the insert inside the try, which is equivalent ONLY
   when the handler cannot fall through. That condition is why shape 2 is an
   AST check and not a second regex: a handler that falls through reaches the
   insert with whatever it left in `root`, and the seam form does not.

WHAT THIS DOES NOT ASSERT, so a green run is not read as more than it is. Not
"the symbol `_resolve_claude_klabauter_root` is unused" -- it has legitimate remaining
callers: the seam itself, the ladder, tests that mock it, and the files in
`_KNOWN_DIVERGENT`. That map is the residual C16 did not collapse, each with the
property that made it unsafe. It is a worklist, not an amnesty: a file leaves it
by being collapsed, never by having its reason softened.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

# `.git/` and `.coordinator-local/` are machinery, not source. `.git/` earns its
# place here twice over: the artifact-claim convention names each claim DIRECTORY
# after the artifact it claims, so `.git/coordinator-sessions/artifact-claims/`
# holds real directories called `commit_v2.py` and `safe_commit_offer.py`. rglob
# matches those, and opening a directory raises PermissionError on Windows — this
# scan died on a peer's live claim rather than on anything in the repo.
_EXCLUDE_PREFIXES = (
    "state/", "archive/", "cross-repo/", "tasks/", "docs/",
    ".git/", ".coordinator-local/",
)

# The canonical body, tolerant of the local name and indentation.
_INLINE_PREAMBLE = re.compile(
    r"(?P<indent>[ \t]*)(?P<var>\w+)\s*=\s*_resolve-claude-klabauter-root\(\)\n"
    r"(?P=indent)if\s+(?P=var)\s+not\s+in\s+sys\.path:\n"
    r"(?P=indent)[ \t]+sys\.path\.insert\(0,\s*(?P=var)\)\n"
)

# The one file allowed to contain the body: the seam's own module, where the
# equivalent lives inside `_front_insert_on_path`.
_SEAM_MODULE = "coordinator/bin/lib/cc_invoke.py"

# C16's residual: files whose preamble is NOT a mechanical no-op to collapse.
# Each entry names the property that made it unsafe, so the next person can
# re-check the property rather than re-derive the whole judgement.
_KNOWN_DIVERGENT: dict[str, str] = {
    "coordinator/bin/handoff-loe-summary.py":
        "the except handler FALLS THROUGH (`claude_klabauter_root = None`) and the code after it "
        "depends on that; the seam form would raise instead of yielding None",
    "coordinator/bin/cutover-cli.py":
        "second site guards with `if claude_klabauter_root and claude_klabauter_root not in sys.path` — the "
        "truthiness leg is only dead code if _resolve_claude_klabauter_root never returns falsy, "
        "which holds today but is a cross-module invariant no test asserts",
    "coordinator/bin/reap-sessions.py":
        "same truthiness-guarded shape as cutover-cli's second site",
    "coordinator/bin/workday-complete-step9-append-changelog.py":
        "deliberately verifies coordinator_core LIVES under the resolved root before "
        "trusting the import, so the insert is not the whole of what it does",
    "coordinator/scripts/install-maximalist.py":
        "installs the engine FROM the resolved root (pip install -e) and re-execs under a "
        "pinned interpreter; the root outlives the sys.path concern",
}


def _live_python_files():
    for p in sorted(_REPO_ROOT.rglob("*.py")):
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith(_EXCLUDE_PREFIXES):
            continue
        # A `*.py` match is not necessarily a file — see _EXCLUDE_PREFIXES.
        # Belt-and-braces behind the prefix skip: a directory named `<x>.py`
        # anywhere else would fail the same way, and this scan's job is to
        # read source, so a non-file is simply not its subject.
        if not p.is_file():
            continue
        yield rel, p


def _never_falls_through(handler: ast.ExceptHandler) -> bool:
    if not handler.body:
        return False
    last = handler.body[-1]
    if isinstance(last, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
        return True
    if isinstance(last, ast.Expr) and isinstance(last.value, ast.Call):
        fn = last.value.func
        return (getattr(fn, "attr", None) or getattr(fn, "id", None)) == "exit"
    return False


def _resolve_only_try(node: ast.Try) -> str | None:
    if len(node.body) != 1 or node.orelse or node.finalbody:
        return None
    stmt = node.body[0]
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    if not isinstance(stmt.targets[0], ast.Name) or not isinstance(stmt.value, ast.Call):
        return None
    fn = stmt.value.func
    if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "_resolve_claude_klabauter_root":
        return None
    if not node.handlers or not all(_never_falls_through(h) for h in node.handlers):
        return None
    return stmt.targets[0].id


def _canonical_insert(node: ast.stmt, var: str) -> bool:
    if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], ast.NotIn):
        return False
    if not (isinstance(test.left, ast.Name) and test.left.id == var):
        return False
    cmp = test.comparators[0]
    if not (isinstance(cmp, ast.Attribute) and cmp.attr == "path"):
        return False
    inner = node.body[0]
    if not (isinstance(inner, ast.Expr) and isinstance(inner.value, ast.Call)):
        return False
    return isinstance(inner.value.func, ast.Attribute) and inner.value.func.attr == "insert"


def _has_collapsible_try_variant(text: str) -> bool:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for i, node in enumerate(body[:-1]):
            if isinstance(node, ast.Try):
                var = _resolve_only_try(node)
                if var and _canonical_insert(body[i + 1], var):
                    return True
    return False


@pytest.fixture(scope="module")
def try_variant_offenders():
    found = []
    this_file = pathlib.Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    for rel, p in _live_python_files():
        if rel in (_SEAM_MODULE, this_file) or rel in _KNOWN_DIVERGENT:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "_resolve_claude_klabauter_root" not in text:
            continue
        if _has_collapsible_try_variant(text):
            found.append(rel)
    return found


def test_no_cli_carries_a_collapsible_try_wrapped_preamble(try_variant_offenders):
    """Shape 2: a try/except whose handler cannot fall through, then the canonical
    insert. Provably equivalent to the seam, so there is no reason to keep one."""
    assert not try_variant_offenders, (
        "these wrap the resolve in a non-falling-through try and then do their own "
        "sys.path insert; call require_dispatch_engine_on_path() inside the try "
        "instead:\n  " + "\n  ".join(try_variant_offenders)
    )


@pytest.fixture(scope="module")
def offenders():
    this_file = pathlib.Path(__file__).resolve().relative_to(_REPO_ROOT).as_posix()
    found = []
    for rel, p in _live_python_files():
        # This module quotes the preamble in its own docstring, as the thing it
        # forbids. A test that asserts a shape is gone must contain that shape.
        if rel in (_SEAM_MODULE, this_file) or rel in _KNOWN_DIVERGENT:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "_resolve_claude_klabauter_root" not in text:
            continue
        if _INLINE_PREAMBLE.search(text):
            found.append(rel)
    return found


def test_the_scan_reaches_real_files():
    """Non-vacuity: a guard that scans nothing passes for the wrong reason."""
    adopters = [
        rel
        for rel, p in _live_python_files()
        if "require_dispatch_engine_on_path" in p.read_text(encoding="utf-8", errors="replace")
    ]
    assert len(adopters) > 100, (
        f"only {len(adopters)} files reference the seam — the collapse is not in the tree "
        "this test is scanning, so a green result here means nothing"
    )


def test_no_cli_carries_its_own_copy_of_the_preamble(offenders):
    """The collapse target, restated as an invariant.

    Fix by calling `require_dispatch_engine_on_path()` — NOT by adopting
    `require_engine_on_path`, which resolves on the locator axis and would
    repoint the CLI from the published engine to the working tree.
    """
    assert not offenders, (
        "these files re-introduce the inline dispatch bootstrap instead of calling "
        "cc_invoke.require_dispatch_engine_on_path():\n  " + "\n  ".join(offenders)
    )


def test_the_seam_module_still_owns_the_idiom():
    """The one permitted home actually contains the insert primitive."""
    text = (_REPO_ROOT / _SEAM_MODULE).read_text(encoding="utf-8", errors="replace")
    assert "def _front_insert_on_path" in text
    assert "def require_dispatch_engine_on_path" in text


def test_known_divergent_entries_are_real_and_reasoned():
    """A tail entry must name an existing file and say why it is exempt."""
    for rel, reason in _KNOWN_DIVERGENT.items():
        assert (_REPO_ROOT / rel).exists(), f"{rel} is exempted but does not exist"
        assert len(reason.strip()) >= 20, f"{rel}'s exemption gives no checkable reason"
