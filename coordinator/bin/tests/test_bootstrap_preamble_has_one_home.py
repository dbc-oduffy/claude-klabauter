"""AC18: the dispatch bootstrap preamble exists in exactly one place.

C16 collapsed ~190 verbatim copies of

    claude_klabauter_root = _resolve_claude_klabauter_root()
    if claude_klabauter_root not in sys.path:
        sys.path.insert(0, claude_klabauter_root)

onto `cc_invoke.require_dispatch_engine_on_path()`. Without a guard the idiom
grows back one CLI at a time: it is three obvious lines, and every new
trampoline is written by copying the nearest existing one.

WHAT THIS DOES NOT ASSERT, stated so a green run is not read as more than it is:

- Not "the symbol `_resolve_claude_klabauter_root` is unused". It has legitimate remaining
  callers: the seam itself, the ladder, and tests that mock it.
- **Not the variant tail.** The regex below matches the CANONICAL body only. At
  the time of the collapse ~29 further files carried a preamble that does extra
  work between the resolve and the insert -- a try/except, a class-aware call, a
  validity probe. Those are C16's remaining work; this guard does not see them
  and a green run here is not evidence they are done. `docs/reference/
  engine-root-env-var-routing.md` is where the live count lives.

`_KNOWN_DIVERGENT` is for a file that must keep an inline copy for a stated
reason, so an exemption stays visible and countable rather than dissolving into
a widened regex.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

_EXCLUDE_PREFIXES = ("state/", "archive/", "cross-repo/", "tasks/", "docs/")

# The canonical body, tolerant of the local name and indentation.
_INLINE_PREAMBLE = re.compile(
    r"(?P<indent>[ \t]*)(?P<var>\w+)\s*=\s*_resolve-claude-klabauter-root\(\)\n"
    r"(?P=indent)if\s+(?P=var)\s+not\s+in\s+sys\.path:\n"
    r"(?P=indent)[ \t]+sys\.path\.insert\(0,\s*(?P=var)\)\n"
)

# The one file allowed to contain the body: the seam's own module, where the
# equivalent lives inside `_front_insert_on_path`.
_SEAM_MODULE = "coordinator/bin/lib/cc_invoke.py"

# Files whose preamble does extra work between the resolve and the insert, so the
# collapse is not a mechanical no-op for them. C16's remaining tail.
_KNOWN_DIVERGENT: dict[str, str] = {}


def _live_python_files():
    for p in sorted(_REPO_ROOT.rglob("*.py")):
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel.startswith(_EXCLUDE_PREFIXES):
            continue
        yield rel, p


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
