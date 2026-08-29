"""coordinator_core.bash_guards.tests.test_wiki_citation_resolution --
regression cover for `dispatch.resolve_wiki_citation`.

WHY THIS FILE EXISTS. `guard-doctrine-surface-bash-write`'s deny text ends in
a `coordinator/docs/wiki/guard-message-concision.md#...` citation. DoE's
cold twin runs that literal through `_message_envelope.resolve_wiki_citation()`
and rewrites it to an absolute local path; the port used to emit the bare
literal unchanged, which 404s for a reader outside the DoE-claude checkout.
This file pins the fix: the CALLER (`dispatch.py`) now resolves the citation
per call, off that call's own `plugin_root`, and passes the result down to
`check()` -- the guard module itself stays free of resolution machinery.

RENAMED (2026-08-29) from `resolve_doctrine_surface_wiki_citation` to
`resolve_wiki_citation`: the function is no longer doctrine-surface-specific
-- it is now also threaded into `guard-host-subagent-bash-ban` and
`guard-host-subagent-bash-spawn-shapes` the same caller-resolves way.

NO SCOPE BOUNDARY ANYMORE ON THE NESTED-ANCHOR CASE. A prior revision of
this file pinned that a nested anchor (`docs/wiki/coordinator-tripwires/
<page>.md`) must stay an unresolved no-op here, matching cold's own
then-current regex, which could only match a flat `docs/wiki/<page>.md`
anchor (state/audits/2026-08-29-unverified-parity-findings-measured.md
FINDING B). That measurement went stale the same day: DoE widened
`_WIKI_CITATION_RE` to admit nested segments, so cold now resolves the exact
anchor this repo's two subagent guards carry. `_WIKI_CITATION_RE` above was
widened to match, and the parity assertion below is now LIVE -- compared
against DoE-claude's OWN resolver run on the identical input, never a
hand-written expected string, so a future re-narrowing or re-widening on
DoE's side is caught by re-running this test, not by re-reading a comment.

PARITY, NOT A BETTER REGEX. Assertions compare the resolved output against
DoE-claude's OWN `_message_envelope.resolve_wiki_citation()` run on the
identical input, imported directly from the sibling checkout (never a
hand-written expected string) -- matching cold's regex semantics is the
pinned criterion, whatever that regex currently is.

Opt-in on the DoE-claude sibling checkout, same shape as
`test_folded_guard_transport_parity.py`: every case importing the cold
resolver skips (never silently passes) on an install with no sibling repo
resolved by `coordinator_doe_root()`.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as _guard
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root

_DOE_ROOT = coordinator_doe_root()
_DOE_HOOKS_DIR = Path(_DOE_ROOT) / "coordinator" / "hooks" / "scripts" if _DOE_ROOT else None

_SKIP_REASON = (
    "opt-in fixture: no DoE-claude sibling checkout resolved by "
    "coordinator_doe_root() -- this file compares the warm resolver's "
    "output against cold's own `_message_envelope.resolve_wiki_citation()`, "
    "which lives only in that sibling repo."
)


def _load_cold_message_envelope() -> Any:
    """Import DoE-claude's `_message_envelope.py` directly (a non-package
    module, not importable by dotted path) via `importlib.util`, never a
    `sys.path.insert` left dangling for later tests -- loaded fresh under a
    private module name each call so this file never pollutes `sys.modules`
    for anything else importing a same-named module."""
    assert _DOE_HOOKS_DIR is not None
    module_path = _DOE_HOOKS_DIR / "_message_envelope.py"
    spec = importlib.util.spec_from_file_location(
        "_doe_message_envelope_for_parity_test", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered in `sys.modules` BEFORE `exec_module`: the cold module's
    # `@dataclass`-decorated classes carry string annotations, and
    # `dataclasses._is_type` resolves those via `sys.modules.get(cls.
    # __module__)` -- an unregistered module makes that lookup `None` and
    # crashes with an unrelated `AttributeError` deep inside `dataclasses`,
    # not the module's own code.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


#: `guard_doctrine_surface_bash_write._WIKI_ANCHOR`'s own flat literal.
_FLAT_ANCHOR = _guard._WIKI_ANCHOR

#: A nested anchor shaped like `guard-host-subagent-bash-ban`'s own literal
#: -- used ONLY to exercise the nested-segment regex branch, not that
#: guard's real literal (this file has no business reading that module).
_NESTED_ANCHOR = "coordinator/docs/wiki/coordinator-tripwires/some-page.md#slug"


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
class TestParityWithCold:
    def test_flat_anchor_resolves_to_cold_absolute_path(self) -> None:
        cold = _load_cold_message_envelope()
        expected = cold.resolve_wiki_citation(_FLAT_ANCHOR)
        # Cold must actually have resolved something (not a same-string
        # no-op) for this assertion to be meaningful.
        assert expected != _FLAT_ANCHOR

        actual = dispatch.resolve_wiki_citation(_FLAT_ANCHOR, str(Path(_DOE_ROOT) / "coordinator"))
        assert actual == expected

    def test_nested_anchor_matches_colds_own_resolution(self) -> None:
        """LIVE comparison against cold's own resolver, never a hand-written
        expected string -- this property (not any particular regex text) is
        what let the parity oracle catch DoE's same-day regex widening; see
        module docstring."""
        cold = _load_cold_message_envelope()
        expected = cold.resolve_wiki_citation(_NESTED_ANCHOR)

        actual = dispatch.resolve_wiki_citation(_NESTED_ANCHOR, str(Path(_DOE_ROOT) / "coordinator"))
        assert actual == expected


class TestFailOpen:
    """No sibling-checkout dependency: these pin the resolver's own
    unconditional fail-open contract, never cold-compared."""

    def test_unresolvable_plugin_root_returns_literal_unchanged(self) -> None:
        assert dispatch.resolve_wiki_citation(_FLAT_ANCHOR, None) == _FLAT_ANCHOR

    def test_empty_string_plugin_root_returns_literal_unchanged(self) -> None:
        assert dispatch.resolve_wiki_citation(_FLAT_ANCHOR, "") == _FLAT_ANCHOR

    def test_nested_anchor_resolves_with_a_resolvable_root(self, tmp_path: Path) -> None:
        actual = dispatch.resolve_wiki_citation(_NESTED_ANCHOR, str(tmp_path))
        assert actual != _NESTED_ANCHOR
        assert str(tmp_path) in actual

    def test_no_citation_text_returns_input_unchanged(self, tmp_path: Path) -> None:
        text = "BLOCKED: this looks commit-shaped, but a write marker sits outside the message."
        assert dispatch.resolve_wiki_citation(text, str(tmp_path)) == text

    @pytest.mark.parametrize("plugin_root", [None, "", "/does/not/exist"])
    def test_never_raises(self, plugin_root: Optional[str]) -> None:
        for text in (_FLAT_ANCHOR, _NESTED_ANCHOR, "", "no citation here at all"):
            dispatch.resolve_wiki_citation(text, plugin_root)


class TestCheckThreadsResolverOnDenyPathOnly:
    """Pins `check()`'s own `resolve_wiki_citation` parameter contract: the
    guard module never resolves anything itself, only invokes what it is
    handed, and only from its own deny path."""

    _PAYLOAD = {
        "tool_name": "Bash",
        "tool_input": {"command": "cat > CLAUDE.md <<'EOF'\nx\nEOF"},
    }
    _GOVERNED_SURFACES = ["CLAUDE.md"]

    def test_resolver_is_invoked_and_its_output_lands_in_the_message(self) -> None:
        calls = []

        def _resolver(citation: str) -> str:
            calls.append(citation)
            return "RESOLVED-CITATION-MARKER"

        result = _guard.check(self._PAYLOAD, self._GOVERNED_SURFACES, resolve_wiki_citation=_resolver)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert "RESOLVED-CITATION-MARKER" in reason
        assert calls == [_guard._WIKI_ANCHOR]

    def test_no_resolver_leaves_bare_literal(self) -> None:
        result = _guard.check(self._PAYLOAD, self._GOVERNED_SURFACES)
        assert result is not None
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        assert _guard._WIKI_ANCHOR in reason

    def test_resolver_never_invoked_on_allow_path(self) -> None:
        calls = []

        def _resolver(citation: str) -> str:
            calls.append(citation)
            return citation

        allow_payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "cat CLAUDE.md"},
        }
        result = _guard.check(allow_payload, self._GOVERNED_SURFACES, resolve_wiki_citation=_resolver)
        assert result is None
        assert calls == []
