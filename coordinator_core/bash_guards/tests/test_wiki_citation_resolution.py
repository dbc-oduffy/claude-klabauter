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
    assert _DOE_HOOKS_DIR is not None
    module_path = _DOE_HOOKS_DIR / "_message_envelope.py"
    spec = importlib.util.spec_from_file_location(
        "_doe_message_envelope_for_parity_test", module_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


#: `guard_doctrine_surface_bash_write._WIKI_ANCHOR`'s own flat literal.
_FLAT_ANCHOR = _guard._WIKI_ANCHOR

_NESTED_ANCHOR = "coordinator/docs/wiki/coordinator-tripwires/some-page.md#slug"


@pytest.mark.skipif(_DOE_ROOT is None, reason=_SKIP_REASON)
class TestParityWithCold:
    def test_flat_anchor_resolves_to_cold_absolute_path(self) -> None:
        cold = _load_cold_message_envelope()
        expected = cold.resolve_wiki_citation(_FLAT_ANCHOR)
        assert expected != _FLAT_ANCHOR

        actual = dispatch.resolve_wiki_citation(_FLAT_ANCHOR, str(Path(_DOE_ROOT) / "coordinator"))
        assert actual == expected

    def test_nested_anchor_matches_colds_own_resolution(self) -> None:
        cold = _load_cold_message_envelope()
        expected = cold.resolve_wiki_citation(_NESTED_ANCHOR)

        actual = dispatch.resolve_wiki_citation(_NESTED_ANCHOR, str(Path(_DOE_ROOT) / "coordinator"))
        assert actual == expected


class TestFailOpen:

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
