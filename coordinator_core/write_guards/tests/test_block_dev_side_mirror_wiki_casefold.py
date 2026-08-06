"""Bypass-proof for `block_dev_side_mirror_wiki.py`'s casefold fix
(2026-08-05 fast-follow to `test_casefold_bypass_lint.py`, commit
`223e04b7bf2e`).

No dedicated behavioral test file existed for this hard-deny guard before
this fix (`test_deny_text_reachable_override.py` explicitly lists it as
KNOWN-UNCOVERED, and `test_guard_registry_manifest.py` only checks that the
module is discoverable, not its `check()` decision) -- this is a new file,
not an addition to cover that gap for the one behavior this dispatch
touched.

The defect: `check()` computed `dev_wiki_norm` from `$HOME` and compared it
against the candidate path with a plain (non-folded) `.startswith()`. On a
case-insensitive-but-case-preserving filesystem (macOS APFS, Windows), a
candidate path that differs only in case from `$HOME/.claude/docs/wiki`
(e.g. `$HOME/.Claude/docs/wiki/foo.md`) resolves to the SAME real directory
on disk but failed the case-sensitive string comparison -- the guard
silently ALLOWED exactly the mirroring write it exists to block.

The fix folds only local copies of the comparison operands
(`casefold_path(file_path_expanded)` / `casefold_path(dev_wiki_norm)`) --
`file_path_expanded` itself stays original-case because it is also used
(further down in `check()`) to derive `wiki_filename` for a real
`os.path.isfile()` lookup against the bundled wiki tree, which must not be
lowercased on a case-sensitive filesystem (Linux, most CI).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.write_guards import block_dev_side_mirror_wiki as guard


@pytest.fixture
def _wiki_fixture(tmp_path, monkeypatch):
    """A fake $HOME with a dev-side wiki dir, plus a trusted plugin root
    with a same-named bundled copy -- the exact "mirror" shape the guard
    exists to deny."""
    home = tmp_path / "home"
    (home / ".claude" / "docs" / "wiki").mkdir(parents=True)

    plugin_root = tmp_path / "plugin"
    bundled_wiki = plugin_root / "docs" / "wiki"
    bundled_wiki.mkdir(parents=True)
    (bundled_wiki / "example.md").write_text("bundled copy\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv(guard._PLUGIN_ROOT_TRUSTED_ENV_VAR, "1")
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)

    return home


def _payload(file_path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path}}


def test_same_case_dev_wiki_write_denied(_wiki_fixture):
    """Control: the exact-case path was already caught before this fix.

    Reclassified 2026-08-06 (B5 write-guard classification pass) from
    hard-deny to advisory -- "caught" now means an ``additionalContext``
    flag, not a blocked write (see this guard's own module docstring)."""
    home = _wiki_fixture
    result = guard.check(_payload(str(home / ".claude" / "docs" / "wiki" / "example.md")))
    assert result is not None
    assert "additionalContext" in result["hookSpecificOutput"]


def test_differently_cased_dev_wiki_write_now_denied(_wiki_fixture):
    """The bypass: before the casefold fix, a `.Claude` (capital C) segment
    failed the plain `.startswith()` prefix check and the guard silently
    returned None (ALLOW) -- even though this path lands in the identical
    real directory as the control case on any case-insensitive-but-case-
    preserving filesystem. After the fix, the comparison is casefolded and
    the write is flagged exactly like the control case (advisory, per the
    2026-08-06 reclassification -- the write itself is never blocked)."""
    home = _wiki_fixture
    differently_cased = str(home / ".Claude" / "Docs" / "WIKI" / "example.md")
    result = guard.check(_payload(differently_cased))
    assert result is not None, (
        "casefold bypass reopened: a differently-cased dev-wiki path slipped "
        "past the mirror-write advisory"
    )
    assert "example.md" in result["hookSpecificOutput"]["additionalContext"]


def test_unrelated_path_outside_dev_wiki_still_allowed(_wiki_fixture):
    """Casefolding the comparison must not widen the guard to unrelated
    paths that merely share a casefolded substring accidentally."""
    home = _wiki_fixture
    unrelated = str(home / "not-the-wiki" / "example.md")
    assert guard.check(_payload(unrelated)) is None
