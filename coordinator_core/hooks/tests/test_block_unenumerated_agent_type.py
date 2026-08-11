"""coordinator_core.hooks.tests.test_block_unenumerated_agent_type -- coverage
for the PreToolUse(Agent) unenumerated-`subagent_type` deny guard.

Two families of test here:
    - Controlled roster tests (tmp_path fixtures + `resolve_roster(doe_root=...,
      home=...)` injection) -- isolate the deny/allow/fail-closed/override
      logic from this machine's real example-doctrine-repo checkout and `~/.claude`
      state.
    - ONE live-resolution regression test, run against the REAL example-doctrine-repo
      sibling checkout and this machine's real `~/.claude/plugins/`, that
      pins the three-source union directly (AC3): `Explore`, `Plan`,
      `general-purpose`, and `game-dev:staff-game-dev` must all resolve as
      members. Without this, a re-narrowing of `resolve_roster()` back to
      the coordinator-authored two sources would ship silently even though
      every controlled test above still passes (they inject a synthetic
      roster and never touch the real union path at all).

Spec backlink: docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md § C1
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import coordinator_core.hooks.block_unenumerated_agent_type as mod


# ---------------------------------------------------------------------------
# Fixtures — a synthetic example-doctrine-repo-shaped tree under tmp_path.
# ---------------------------------------------------------------------------


def _write_policy_yaml(doe_root: Path, extra_type: str = "coordinator:executor") -> None:
    policy_dir = doe_root / "coordinator"
    policy_dir.mkdir(parents=True, exist_ok=True)
    (policy_dir / "subagent-sandbox-policy.yaml").write_text(
        "report_sidecar:\n"
        f"  - {extra_type}\n"
        "report_type_map:\n"
        f"  {extra_type}: run-report\n"
        "contract_blocks:\n"
        f"  {extra_type}: []\n"
        "dispatch_tier:\n"
        f"  {extra_type}: review-execution\n",
        encoding="utf-8",
    )


def _write_agents_dir(doe_root: Path, names: "list[str]") -> None:
    agents_dir = doe_root / "coordinator" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (agents_dir / f"{name}.md").write_text(
            f'---\nname: {name}\ndescription: "test agent"\n---\nbody\n',
            encoding="utf-8",
        )


@pytest.fixture()
def doe_root(tmp_path: Path) -> Path:
    root = tmp_path / "example-doctrine-repo"
    _write_policy_yaml(root)
    _write_agents_dir(root, ["executor", "code-reviewer"])
    return root


def _write_agent_md(path: Path, name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


@pytest.fixture()
def plugin_home(tmp_path: Path) -> Path:
    """A synthetic `~/.claude` tree exercising all FOUR plugin layouts
    (see `_load_plugin_roster`'s docstring), plus the `_pre-refresh-
    snapshots` exclusion, in one fixture so `test_resolve_roster_unions_all_three_sources`
    below pins the whole union in a single assertion.
    """
    home = tmp_path / "home"
    plugins = home / ".claude" / "plugins"

    # Leg 1 -- repo-style: <repo>/<namespace>/agents/*.md
    _write_agent_md(plugins / "example-game-workbench-repo" / "game-dev" / "agents" / "staff-game-dev.md", "staff-game-dev")

    # Leg 2 -- cache-style: cache/<marketplace>/<plugin>/<version>/agents/*.md
    _write_agent_md(
        plugins / "cache" / "claude-plugins-official" / "feature-dev" / "abdaf0fadd68" / "agents" / "code-explorer.md",
        "code-explorer",
    )
    # a second cached version of the SAME plugin -- must union, not overwrite
    _write_agent_md(
        plugins / "cache" / "claude-plugins-official" / "feature-dev" / "0371a29ed35d" / "agents" / "code-explorer.md",
        "code-explorer",
    )

    # Leg 3 -- marketplace-style: marketplaces/<marketplace>/plugins/<plugin>/agents/*.md
    _write_agent_md(
        plugins / "marketplaces" / "claude-plugins-official" / "plugins" / "agent-sdk-dev" / "agents" / "sdk-reviewer.md",
        "sdk-reviewer",
    )

    # Leg 4 -- manifest: installed_plugins.json installPath (+ plugin/ nesting)
    manifest_install_root = tmp_path / "manifest-installs" / "example-retrieval-repo"
    _write_agent_md(manifest_install_root / "plugin" / "agents" / "example-retrieval-repo-researcher.md", "example-retrieval-repo-researcher")
    plugins.mkdir(parents=True, exist_ok=True)
    (plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "example-retrieval-repo@example-retrieval-repo": [
                        {"scope": "user", "installPath": str(manifest_install_root), "version": "0.1.0"}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    # a snapshot dir that must NEVER resurrect a deleted agent into the roster
    _write_agent_md(
        plugins / "_pre-refresh-snapshots" / "game-dev" / "agents" / "deleted-ghost.md", "deleted-ghost"
    )

    return home


def _agent_payload(subagent_type: str, name: str = "", prompt: str = "do the thing") -> dict:
    tool_input = {"subagent_type": subagent_type, "prompt": prompt}
    if name:
        tool_input["name"] = name
    return {"tool_name": "Agent", "tool_input": tool_input}


# ---------------------------------------------------------------------------
# resolve_roster() — controlled roster tests
# ---------------------------------------------------------------------------


def test_resolve_roster_unions_all_three_sources(doe_root: Path, plugin_home: Path) -> None:
    roster, reason = mod.resolve_roster(doe_root=str(doe_root), home=str(plugin_home))
    assert reason is None
    assert roster is not None
    assert "coordinator:executor" in roster
    assert "coordinator:code-reviewer" in roster
    assert "Explore" in roster
    assert "Plan" in roster
    assert "general-purpose" in roster
    # (c) plugin leg -- all four layouts represented
    assert "game-dev:staff-game-dev" in roster  # leg 1, repo-style
    assert "feature-dev:code-explorer" in roster  # leg 2, cache-style
    assert "agent-sdk-dev:sdk-reviewer" in roster  # leg 3, marketplace-style
    assert "example-retrieval-repo:example-retrieval-repo-researcher" in roster  # leg 4, manifest + plugin/ nesting
    assert "game-dev:deleted-ghost" not in roster  # snapshot dir excluded


def test_repo_style_plugin_leg_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_agent_md(
        home / ".claude" / "plugins" / "example-game-workbench-repo" / "example-game-repo-control" / "agents" / "ue-asset-author.md",
        "ue-asset-author",
    )
    names = mod._repo_style_plugin_roster(home / ".claude" / "plugins")
    assert "example-game-repo-control:ue-asset-author" in names


def test_cache_style_plugin_leg_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_agent_md(
        home / ".claude" / "plugins" / "cache" / "claude-plugins-official" / "feature-dev" / "abdaf0fadd68" / "agents" / "code-explorer.md",
        "code-explorer",
    )
    names = mod._cache_style_plugin_roster(home / ".claude" / "plugins")
    assert "feature-dev:code-explorer" in names
    assert "abdaf0fadd68:code-explorer" not in names  # the concrete regression: version, not plugin


def test_marketplace_style_plugin_leg_alone(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_agent_md(
        home / ".claude" / "plugins" / "marketplaces" / "claude-plugins-official" / "plugins" / "agent-sdk-dev" / "agents" / "sdk-reviewer.md",
        "sdk-reviewer",
    )
    names = mod._marketplace_style_plugin_roster(home / ".claude" / "plugins")
    assert "agent-sdk-dev:sdk-reviewer" in names


def test_manifest_style_plugin_leg_with_plugin_subdir_nesting(tmp_path: Path) -> None:
    plugins_root = tmp_path / "home" / ".claude" / "plugins"
    install_root = tmp_path / "manifest-installs" / "example-retrieval-repo"
    _write_agent_md(install_root / "plugin" / "agents" / "example-retrieval-repo-researcher.md", "example-retrieval-repo-researcher")
    plugins_root.mkdir(parents=True, exist_ok=True)
    (plugins_root / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "example-retrieval-repo@example-retrieval-repo": [{"scope": "user", "installPath": str(install_root)}]
                },
            }
        ),
        encoding="utf-8",
    )
    names = mod._manifest_style_plugin_roster(plugins_root)
    assert "example-retrieval-repo:example-retrieval-repo-researcher" in names


def test_manifest_style_plugin_leg_malformed_json_degrades_to_empty(tmp_path: Path) -> None:
    plugins_root = tmp_path / "home" / ".claude" / "plugins"
    plugins_root.mkdir(parents=True, exist_ok=True)
    (plugins_root / "installed_plugins.json").write_text("{not valid json", encoding="utf-8")
    assert mod._manifest_style_plugin_roster(plugins_root) == set()


def test_manifest_leg_failure_does_not_blank_filesystem_legs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plugins_root = home / ".claude" / "plugins"
    _write_agent_md(
        plugins_root / "example-game-workbench-repo" / "game-dev" / "agents" / "staff-game-dev.md",
        "staff-game-dev",
    )
    plugins_root.mkdir(parents=True, exist_ok=True)
    (plugins_root / "installed_plugins.json").write_text("{not valid json", encoding="utf-8")
    names = mod._load_plugin_roster(str(home))
    assert "game-dev:staff-game-dev" in names


def test_resolve_roster_doe_root_unresolved_fails_closed() -> None:
    roster, reason = mod.resolve_roster(doe_root="", home=None)
    assert roster is None
    assert reason is not None
    assert "MISSING ENTIRELY" in reason
    assert mod._OVERRIDE_MARKER_PREFIX in reason


def test_resolve_roster_policy_yaml_missing_fails_closed_as_missing(tmp_path: Path) -> None:
    root = tmp_path / "example-doctrine-repo"
    _write_agents_dir(root, ["executor"])  # policy yaml deliberately absent
    roster, reason = mod.resolve_roster(doe_root=str(root), home=None)
    assert roster is None
    assert "subagent-sandbox-policy.yaml" in reason
    assert "MISSING ENTIRELY" in reason


def test_resolve_roster_policy_yaml_unparseable_fails_closed_as_unparseable(tmp_path: Path) -> None:
    root = tmp_path / "example-doctrine-repo"
    policy_dir = root / "coordinator"
    policy_dir.mkdir(parents=True)
    (policy_dir / "subagent-sandbox-policy.yaml").write_text(
        "report_sidecar: [unterminated\n", encoding="utf-8"
    )
    _write_agents_dir(root, ["executor"])
    roster, reason = mod.resolve_roster(doe_root=str(root), home=None)
    assert roster is None
    assert "UNPARSEABLE" in reason
    assert "subagent-sandbox-policy.yaml" in reason


def test_resolve_roster_agents_dir_missing_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "example-doctrine-repo"
    _write_policy_yaml(root)  # agents dir deliberately absent
    roster, reason = mod.resolve_roster(doe_root=str(root), home=None)
    assert roster is None
    assert "coordinator" in reason and "agents" in reason
    assert "MISSING ENTIRELY" in reason


def test_resolve_roster_plugin_dir_absent_degrades_not_fatal(doe_root: Path) -> None:
    # No plugin_home fixture at all — real example-doctrine-repo sources present, home unresolved.
    roster, reason = mod.resolve_roster(doe_root=str(doe_root), home=None)
    assert reason is None
    assert roster is not None
    assert "coordinator:executor" in roster
    assert "Explore" in roster  # harness builtins still present


# ---------------------------------------------------------------------------
# check() — deny / allow / override, roster injected via monkeypatch
# ---------------------------------------------------------------------------


def _patch_roster(monkeypatch: pytest.MonkeyPatch, roster, reason=None) -> None:
    def _fake_resolve_roster(*, doe_root=None, home=None):
        del doe_root, home  # signature-compatible stand-in; args intentionally unused
        return (roster, reason)

    monkeypatch.setattr(mod, "resolve_roster", _fake_resolve_roster)


def test_named_and_unnamed_invented_type_both_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))

    unnamed = mod.check(_agent_payload("hookprobe-invented"))
    named = mod.check(_agent_payload("hookprobe-invented", name="hookprobe-named"))

    assert unnamed is not None
    assert named is not None
    assert unnamed["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert named["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_enumerated_coordinator_type_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    assert mod.check(_agent_payload("coordinator:executor")) is None


def test_unreadable_roster_denies_with_ac10_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = (
        "roster source MISSING ENTIRELY (path/install defect): "
        "/fake/coordinator/agents is not a directory. Override (last "
        "resort, clears THIS dispatch only): add a line "
        f"`{mod._OVERRIDE_MARKER_PREFIX} <reason>` to the dispatch prompt."
    )
    _patch_roster(monkeypatch, None, reason)
    envelope = mod.check(_agent_payload("coordinator:executor"))
    assert envelope is not None
    reason_text = envelope["hookSpecificOutput"]["permissionDecisionReason"]
    assert "MISSING ENTIRELY" in reason_text
    assert mod._OVERRIDE_MARKER_PREFIX in reason_text


def test_unreadable_roster_reason_distinguishes_missing_vs_unparseable() -> None:
    missing_reason = None
    unparseable_reason = None
    try:
        raise mod._RosterError("roster source MISSING ENTIRELY (path/install defect): x")
    except mod._RosterError as exc:
        missing_reason = exc.reason
    try:
        raise mod._RosterError(
            "roster source present but UNPARSEABLE (peer-repo edit in flight?): x"
        )
    except mod._RosterError as exc:
        unparseable_reason = exc.reason
    assert "MISSING ENTIRELY" in missing_reason
    assert "UNPARSEABLE" in unparseable_reason
    assert missing_reason != unparseable_reason


def test_override_marker_suppresses_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    prompt = (
        "Do the task.\n"
        f"{mod._OVERRIDE_MARKER_PREFIX} testing the escape hatch\n"
    )
    payload = _agent_payload("hookprobe-invented", prompt=prompt)
    assert mod.check(payload) is None


def test_override_marker_with_empty_reason_does_not_suppress(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset({"coordinator:executor"}))
    prompt = f"Do the task.\n{mod._OVERRIDE_MARKER_PREFIX}\n"
    payload = _agent_payload("hookprobe-invented", prompt=prompt)
    envelope = mod.check(payload)
    assert envelope is not None
    assert envelope["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_non_agent_tool_name_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset())
    payload = {"tool_name": "Bash", "tool_input": {"subagent_type": "invented"}}
    assert mod.check(payload) is None


def test_absent_subagent_type_out_of_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_roster(monkeypatch, frozenset())
    payload = {"tool_name": "Agent", "tool_input": {"prompt": "no type given"}}
    assert mod.check(payload) is None


# ---------------------------------------------------------------------------
# Live-resolution regression test (AC3) — no roster injection, real sources.
# ---------------------------------------------------------------------------


#: Pinned sibling checkout used ONLY by these two live-regression tests --
#: never read by production code (which resolves it via
#: `read_doe_root_pointer()`). abs-path-ok: fixed test-only path, matches
#: the coordinator's own live measurement that caught the AC3 regression;
#: skipped outright (never silently passed) when absent on the host.
_LIVE_DOE_ROOT = "X:/example-doctrine-repo"


def _live_doe_root_or_skip() -> str:
    if not Path(_LIVE_DOE_ROOT).is_dir():
        pytest.skip(f"example-doctrine-repo sibling checkout not present on this host: {_LIVE_DOE_ROOT}")
    return _LIVE_DOE_ROOT


@pytest.mark.real_home
def test_live_roster_allows_harness_builtins_and_regression_plugin_types() -> None:
    # real_home: read-only oracle against the real ~/.claude/plugins tree --
    # see coordinator_core/conftest.py's _quarantine_real_home docstring.
    # Without this marker the suite-root autouse fixture redirects
    # HOME/USERPROFILE into a per-test quarantine dir, and this test would
    # always see an empty plugin roster regardless of the code under test.
    doe_root = _live_doe_root_or_skip()
    roster, reason = mod.resolve_roster(doe_root=doe_root)
    assert reason is None, reason
    assert roster is not None
    for expected in ("Explore", "Plan", "general-purpose"):
        assert expected in roster, f"{expected!r} missing from live roster — three-source union regressed"
    # The concrete regression a coordinator measurement caught live (cache/
    # dir wrongly excluded, namespace read from the wrong path segment):
    assert "example-retrieval-repo:example-retrieval-repo-researcher" in roster
    assert "feature-dev:code-explorer" in roster


@pytest.mark.real_home
def test_live_roster_allows_via_check_not_just_resolve_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    # real_home: see the marker note on the sibling test above.
    doe_root = _live_doe_root_or_skip()
    # check() calls resolve_roster() with no args (real resolution); pin the
    # example-doctrine-repo root deterministically via read_doe_root_pointer rather than
    # relying on this pytest process's own registry/env state, matching the
    # coordinator's ask that this test be deterministic in the one
    # environment that matters.
    monkeypatch.setattr(mod, "read_doe_root_pointer", lambda: doe_root)
    for subagent_type in (
        "Explore",
        "Plan",
        "general-purpose",
        "example-retrieval-repo:example-retrieval-repo-researcher",
        "feature-dev:code-explorer",
    ):
        envelope = mod.check(_agent_payload(subagent_type))
        assert envelope is None, f"check() denied enumerated type {subagent_type!r}"
