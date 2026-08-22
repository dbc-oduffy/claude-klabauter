"""
coordinator_core.subagent_sandbox.tests.test_provision_report_root_resolution
-- C4 (state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-costs/):
proves the `contract_blocks` assembly leg resolves its snippet registry off
the coordinator-claude PLUGIN's own content root, not the spawning session's
git root.

Two-repo control, not a single-path assertion (this is what found the
defect, per C4's brief): the SAME payload, the SAME agent type, assembled
across a DoE-claude-SHAPED session cwd (where `git_root` and the plugin
content root used to coincide, masking the bug) and an unrelated,
non-DoE-shaped session cwd (every other real dispatch's actual shape) --
both must assemble the identical non-empty `injected_prompt_blocks` string.
Before the fix, only the DoE-shaped cwd composed anything; the other
returned ``None``.

`resolve_git_root` is monkeypatched to an identity stub (returns whatever
`cwd` it's handed) rather than exercised for real -- this test's subject is
the PLUGIN-root resolution axis, not git-root resolution, and a real `git
rev-parse` spawn against synthetic tmp_path trees would just add
noise/flakiness the substitution avoids. `CLAUDE_PLUGIN_ROOT` is
monkeypatched to a synthetic fixture plugin content root carrying a minimal
real `snippets/registry.toml` + one snippet body -- resolution off the env
var, not off any real DoE-claude checkout, keeps this test runnable with no
sibling-repo dependency.

Module under test: coordinator_core/subagent_sandbox/provision_report.py
Spec backlink: state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-costs/C4.md
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.subagent_sandbox import provision_report


_SNIPPET_NAME = "fixture-block"

_REGISTRY_TOML = f"""\
schema_version = 3

[snippet.{_SNIPPET_NAME}]
sentinel_begin = "<!-- BEGIN {_SNIPPET_NAME} -->"
sentinel_end = "<!-- END {_SNIPPET_NAME} -->"
consumers = []
delivery = "inject"
header_style = "comment-block"
"""

_SNIPPET_BODY = (
    "<!-- header line 1 -->\n"
    "<!-- header line 2 -->\n"
    "Fixture contract block body.\n"
)


def _make_fixture_plugin_root(tmp_path: Path) -> Path:
    """A synthetic plugin CONTENT root: `<root>/snippets/registry.toml` +
    `<root>/snippets/<name>.md`, matching the shape
    `resolve_plugin_root()` + `_assemble_contract_blocks` expect directly
    under the resolved root (no extra `coordinator/` join)."""
    plugin_root = tmp_path / "plugin-root"
    snippets_dir = plugin_root / "snippets"
    snippets_dir.mkdir(parents=True)
    (snippets_dir / "registry.toml").write_text(_REGISTRY_TOML, encoding="utf-8")
    (snippets_dir / f"{_SNIPPET_NAME}.md").write_text(_SNIPPET_BODY, encoding="utf-8")
    return plugin_root


def test_resolve_plugin_root_prefers_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_root = _make_fixture_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    assert provision_report.resolve_plugin_root() == str(plugin_root)


def test_resolve_plugin_root_returns_none_on_full_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(
        provision_report,
        "claude_config_dir",
        lambda: Path("does-not-exist-anywhere"),
        raising=False,
    )

    # No CLAUDE_PLUGIN_ROOT and an unresolvable claude_config_dir()-relative
    # probe -- both legs miss, so the resolver must fail open to None rather
    # than raise or fabricate a path.
    assert provision_report.resolve_plugin_root() is None


@pytest.mark.parametrize(
    "session_cwd_name",
    ["DoE-claude", "some-unrelated-session-repo"],
)
def test_assemble_contract_blocks_composes_regardless_of_session_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_cwd_name: str
) -> None:
    """The two-repo control: a DoE-claude-SHAPED session cwd and an
    unrelated one must both assemble the identical non-empty
    `injected_prompt_blocks` string, since the snippet registry now
    resolves off the plugin's own content root (env-injected), never off
    the session's git_root.
    """
    plugin_root = _make_fixture_plugin_root(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    session_cwd = tmp_path / session_cwd_name
    session_cwd.mkdir()
    # A REAL `.git` entry, not a monkeypatched resolver. This test previously
    # stubbed `provision_report.resolve_git_root` to an identity function; C2
    # repointed root resolution at the non-spawning walker
    # (`git.repo_root.show_toplevel`), so that stub stopped sitting on the call
    # path and the walker correctly found no repo above a bare tmp dir.
    # Pinning the behaviour to whichever resolver happens to be wired up is the
    # proxy-probe failure this plan already hit once, in C4's own resolver;
    # giving the fixture a real repo marker exercises the real path and stays
    # true across any future repoint.
    (session_cwd / ".git").mkdir()

    payload = {
        "session_id": "test-session",
        "agent_type": "coordinator:some-agent",
        "contract_blocks": [_SNIPPET_NAME],
    }

    assembled = provision_report.assemble_contract_blocks_for_payload(
        payload, cwd=str(session_cwd), report_sidecar_path=None
    )

    assert assembled is not None
    assert "Fixture contract block body." in assembled
