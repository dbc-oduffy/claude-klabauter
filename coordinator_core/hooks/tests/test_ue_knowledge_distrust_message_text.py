"""
Message-text regression for chunk C5b of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

`ue_knowledge_distrust.run`'s UE PROJECT DETECTED banner is injected into agent
context on every session in a UE project. This pins that the rendered banner
never names a private repo codename (or its OSS-scrub placeholder) as a place
to go — the `example-game-repo` codename in particular, since the banner used to read
"...via example-game-repo-docs MCP.", a substring whose scrub-token adjacency
(`example-game-repo` immediately followed by `-docs`) was measured (see run-report
sidecar) to rewrite to the mangled compound "example-game-repo-docs" under
the real `setup/percolate-hooks/percolate-store.yaml` transform for target
`claude-klabauter`. `_BOOTSTRAP_KEYS` plugin-key strings (e.g.
"example-game-repo-control@example-game-workbench-repo") are functional identifiers the
reader writes into settings.json verbatim and are deliberately NOT covered by
this banner-only check.

Spec backlink: docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md, chunk C5b.
"""

from __future__ import annotations

from coordinator_core.hooks import ue_knowledge_distrust as mod

_FORBIDDEN_SUBSTRINGS = (
    "example-doctrine-repo",
    "example-doctrine-repo",
    "cockpit",
    "example-fleet",
    "machine-b",
    "example-game-repo",
    "example-doctrine-repo",
    "example-retrieval-repo",
    "example-fleet",
    "example-game-repo",
    "machine-b",
    "cockpit",
)


def _assert_clean(banner: str) -> None:
    lowered = banner.lower()
    for hit in _FORBIDDEN_SUBSTRINGS:
        assert hit.lower() not in lowered, f"banner names a repo codename/placeholder: {hit!r} in {banner!r}"


def test_banner_names_no_repo(tmp_path):
    (tmp_path / "MyGame.uproject").write_text("{}")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")

    result = mod.run(str(tmp_path), "unused-plugin-root")

    assert result.banner
    assert "UE PROJECT DETECTED" in result.banner
    _assert_clean(result.banner)


def test_banner_drops_named_adjacency_hazard_string(tmp_path):
    """The specific hazard string named in the dispatch brief -- "...via
    example-game-repo-docs MCP." -- must not appear at all: the fix drops the
    MCP-server-name prose rather than leaving a string the scrub could still
    mangle into "example-game-repo-docs" (measured, see run-report sidecar)."""
    (tmp_path / "MyGame.uproject").write_text("{}")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")

    result = mod.run(str(tmp_path), "unused-plugin-root")

    assert "via example-game-repo-docs MCP" not in result.banner
    assert "example-game-repo-docs" not in result.banner
