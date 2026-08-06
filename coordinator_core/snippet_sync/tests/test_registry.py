"""Unit tests for coordinator_core.snippet_sync.registry.

Covers the schema validation, consumer-resolution ordering (F5), and
machine-local/file-exists conditional handling ported from the retired
`coordinator/bin/snippet-registry` bash CLI (477 LoC). Golden-diff parity
against the live example-doctrine-repo-side registry.toml + 3 bats suites
(test-snippet-registry{,-conditional,-malformed}.bats) is verified
separately at build time — this file covers unit-level edges those
integration suites don't isolate (e.g. content_root override).

Spec backlink: example-doctrine-repo scratch/subagent-sandbox/bash-to-python-engine-migration/recipe-t3a-g3.md § 6
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from coordinator_core.snippet_sync import registry as reg


def _write_registry(tmp_path: Path, body: str) -> Path:
    snippets_dir = tmp_path / "snippets"
    snippets_dir.mkdir(parents=True, exist_ok=True)
    registry_path = snippets_dir / "registry.toml"
    registry_path.write_text(textwrap.dedent(body), encoding="utf-8")
    return registry_path


def test_load_registry_unknown_schema_version_exits_3(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 99
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 3
    assert "schema_version" in str(exc_info.value)


def test_load_registry_missing_required_field(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert "consumers" in str(exc_info.value)


def test_load_registry_file_exists_forbids_condition_key(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "x.md"
        condition_type = "file-exists"
        condition_key = "should.not.be.here"
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert "FORBIDS" in str(exc_info.value)


def test_get_snippet_entry_unknown_name_exit_2(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        """,
    )
    data = reg.load_registry(registry_path)
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.get_snippet_entry(data, "not-foo")
    assert exc_info.value.exit_code == 2


def test_resolve_consumers_ordering_plugin_then_sibling(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = [
          "agents/zzz.md",
          "agents/aaa.md",
          "../sibling/zzz.md",
          "../sibling/aaa.md",
        ]
        """,
    )
    data = reg.load_registry(registry_path)
    plugin_root = tmp_path
    out = reg.resolve_consumers(data, "foo", plugin_root)
    # Group 1 (plugin-root-relative) alpha, then group 2 (sibling) alpha.
    assert out == [
        str(plugin_root / "agents/aaa.md"),
        str(plugin_root / "agents/zzz.md"),
        str(plugin_root / "../sibling/aaa.md"),
        str(plugin_root / "../sibling/zzz.md"),
    ]


def test_resolve_consumers_content_root_override_affects_only_plugin_relative(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = ["agents/x.md", "../sibling/y.md"]
        """,
    )
    data = reg.load_registry(registry_path)
    plugin_root = tmp_path / "plugin"
    content_root = tmp_path / "content-override"
    out = reg.resolve_consumers(data, "foo", plugin_root, content_root=content_root)
    assert str(content_root / "agents/x.md") in out
    # Sibling path stays anchored to plugin_root regardless of content_root.
    assert str(plugin_root / "../sibling/y.md") in out
    assert str(content_root / "../sibling/y.md") not in out


def test_resolve_consumers_file_exists_conditional_graceful_skip(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "does/not/exist.md"
        condition_type = "file-exists"
        """,
    )
    data = reg.load_registry(registry_path)
    out = reg.resolve_consumers(data, "foo", tmp_path)
    assert out == []  # graceful skip, no exit-nonzero


def test_resolve_consumers_file_exists_conditional_present(tmp_path):
    consumer = tmp_path / "target.md"
    consumer.write_text("x", encoding="utf-8")
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "target.md"
        condition_type = "file-exists"
        """,
    )
    data = reg.load_registry(registry_path)
    out = reg.resolve_consumers(data, "foo", tmp_path)
    assert out == [str(consumer)]


def test_resolve_consumers_sibling_plugin_file_exists_anchors_to_home_regardless_of_plugin_root(
    tmp_path, monkeypatch
):
    """Regression for the claude-klabauter-consolidation drop: a example-game-workbench-repo
    sibling-plugin file-exists conditional must resolve against the live-install
    plugins root ($CLAUDE_HOME-or-$HOME/.claude/plugins/example-game-workbench-repo/...)
    in BOTH production contexts — plugin_root == the live-install itself, and
    plugin_root == the example-doctrine-repo SOURCE tree (the real --plugin-dir resolution path).
    Prior to the fix, only the live-install plugin_root happened to resolve
    (by coincidence of relative-path arithmetic); the source-tree plugin_root
    silently dropped the consumer.
    """
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    live_install_target = (
        fake_home / ".claude" / "plugins" / "example-game-workbench-repo" / "game-dev" / "agents" / "x.md"
    )
    live_install_target.parent.mkdir(parents=True, exist_ok=True)
    live_install_target.write_text("x", encoding="utf-8")

    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "../../example-game-workbench-repo/game-dev/agents/x.md"
        condition_type = "file-exists"
        """,
    )
    data = reg.load_registry(registry_path)

    # Context 1: plugin_root IS the live-install path (grandparent == plugins/).
    live_install_plugin_root = (
        fake_home / ".claude" / "plugins" / "coordinator-claude" / "coordinator"
    )
    out_live = reg.resolve_consumers(data, "foo", live_install_plugin_root)
    assert out_live == [str(live_install_target)]

    # Context 2: plugin_root IS the example-doctrine-repo SOURCE tree — a wholly different
    # location bearing no relative-path relationship to the live install.
    source_tree_plugin_root = tmp_path / "some-other-checkout" / "example-doctrine-repo" / "coordinator"
    out_source = reg.resolve_consumers(data, "foo", source_tree_plugin_root)
    assert out_source == [str(live_install_target)]


def test_resolve_consumers_sibling_plugin_file_exists_home_fallback_when_no_claude_home(
    tmp_path, monkeypatch
):
    """CLAUDE_HOME unset falls back to $HOME, per the established
    doe_root_pointer.py / trusted_root_guard.py convention.
    """
    fake_home = tmp_path / "plain-home"
    monkeypatch.delenv("CLAUDE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    live_install_target = (
        fake_home / ".claude" / "plugins" / "example-game-workbench-repo" / "game-dev" / "agents" / "x.md"
    )
    live_install_target.parent.mkdir(parents=True, exist_ok=True)
    live_install_target.write_text("x", encoding="utf-8")

    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "../../example-game-workbench-repo/game-dev/agents/x.md"
        condition_type = "file-exists"
        """,
    )
    data = reg.load_registry(registry_path)
    out = reg.resolve_consumers(data, "foo", tmp_path / "any" / "plugin_root")
    assert out == [str(live_install_target)]


def test_list_for_sibling_plugin_file_exists_matches_in_both_contexts(tmp_path, monkeypatch):
    fake_home = tmp_path / "fake-home"
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
    live_install_target = (
        fake_home / ".claude" / "plugins" / "example-game-workbench-repo" / "game-dev" / "agents" / "x.md"
    )
    live_install_target.parent.mkdir(parents=True, exist_ok=True)
    live_install_target.write_text("x", encoding="utf-8")

    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [[snippet.foo.conditional_consumer]]
        path = "../../example-game-workbench-repo/game-dev/agents/x.md"
        condition_type = "file-exists"
        """,
    )
    data = reg.load_registry(registry_path)

    source_tree_plugin_root = tmp_path / "some-other-checkout" / "example-doctrine-repo" / "coordinator"
    assert reg.list_for(data, str(live_install_target), source_tree_plugin_root) == ["foo"]
    live_install_plugin_root = fake_home / ".claude" / "plugins" / "coordinator-claude" / "coordinator"
    assert reg.list_for(data, str(live_install_target), live_install_plugin_root) == ["foo"]


def test_list_snippets_alphabetical(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.zzz]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [snippet.aaa]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        """,
    )
    data = reg.load_registry(registry_path)
    assert reg.list_snippets(data) == ["aaa", "zzz"]


def test_list_for_reverse_lookup(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = ["agents/shared.md"]
        [snippet.bar]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = ["agents/other.md"]
        """,
    )
    data = reg.load_registry(registry_path)
    assert reg.list_for(data, "agents/shared.md", tmp_path) == ["foo"]
    assert reg.list_for(data, "agents/nope.md", tmp_path) == []


def test_get_snippet_meta_defaults_and_t3a_g3f_fields(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.defaults]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        [snippet.custom]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        header_style = "fixed-2-line"
        fence_aware = true
        allow_insert = true
        consumer_source = "scan"
        search_scope = "parent-of-plugin-root"
        in_fence_consumers = true
        """,
    )
    data = reg.load_registry(registry_path)

    defaults = reg.get_snippet_meta(data, "defaults")
    assert defaults == {
        "header_style": "sentinel-embedded",
        "delivery": "paste",
        "fence_aware": False,
        "allow_insert": False,
        "consumer_source": "registry",
        "search_scope": "plugin-root",
        "in_fence_consumers": False,
        "excluded_consumer": [],
        "eligible_glob": None,
    }

    custom = reg.get_snippet_meta(data, "custom")
    assert custom == {
        "header_style": "fixed-2-line",
        "delivery": "paste",
        "fence_aware": True,
        "allow_insert": True,
        "consumer_source": "scan",
        "search_scope": "parent-of-plugin-root",
        "in_fence_consumers": True,
        "excluded_consumer": [],
        "eligible_glob": None,
    }


def test_delivery_inject_is_read_back(tmp_path):
    """An `inject` row's delivery survives the reader — the signal an orphan check
    needs to know a pasted sentinel for it is an orphan by construction."""
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 3
        [snippet.injected]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = ["agents/staff-eng.md"]
        delivery = "inject"
        """,
    )
    data = reg.load_registry(registry_path)
    assert reg.get_snippet_meta(data, "injected")["delivery"] == "inject"


def test_delivery_required_at_schema_v3_but_not_before(tmp_path):
    """v3 makes `delivery` required so a new row cannot silently mean "paste" by
    omission; v1/v2 registries predate the field and keep the paste default."""
    body = """
        schema_version = {v}
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        """
    v2 = reg.load_registry(_write_registry(tmp_path / "v2", body.format(v=2)))
    assert reg.get_snippet_meta(v2, "foo")["delivery"] == "paste"

    with pytest.raises(reg.RegistryError, match="missing required field 'delivery'"):
        reg.load_registry(_write_registry(tmp_path / "v3", body.format(v=3)))


def test_delivery_rejects_scan_which_is_a_consumer_source_value(tmp_path):
    """`scan` is a DISCOVERY axis, not a delivery mechanism — a scan-discovered
    snippet is still pasted. Conflating the two is the shape this enum rejects."""
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 3
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        delivery = "scan"
        """,
    )
    data = reg.load_registry(registry_path)
    with pytest.raises(reg.RegistryError, match="unknown delivery 'scan'"):
        reg.get_snippet_meta(data, "foo")


def test_get_snippet_meta_unknown_header_style_fails_loud(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 2
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        consumers = []
        header_style = "not-a-real-style"
        """,
    )
    data = reg.load_registry(registry_path)
    with pytest.raises(reg.RegistryError):
        reg.get_snippet_meta(data, "foo")


# ---------------------------------------------------------------------------
# schema_version 4 — excluded_consumer + eligible_glob (example-doctrine-repo 355255cc3)
#
# Both fields are ADDITIVE-OPTIONAL: the v3-shaped-row case below is as
# load-bearing as the violation cases, because "a v3 row is still valid at v4"
# is the property that makes this a field-SET bump rather than a value break.
# ---------------------------------------------------------------------------

_V4_VALID = """
    schema_version = 4
    [snippet.foo]
    sentinel_begin = "b"
    sentinel_end = "e"
    delivery = "paste"
    eligible_glob = "agents/*.md"
    consumers = ["agents/enrolled.md"]

    [[snippet.foo.excluded_consumer]]
    path   = "agents/bespoke.md"
    reason = "sanctioned per-persona narrowing"
    """


def test_v4_row_with_both_fields_loads_and_reads_back(tmp_path):
    data = reg.load_registry(_write_registry(tmp_path, _V4_VALID))
    meta = reg.get_snippet_meta(data, "foo")
    assert meta["eligible_glob"] == "agents/*.md"
    assert meta["excluded_consumer"] == [
        {"path": "agents/bespoke.md", "reason": "sanctioned per-persona narrowing"}
    ]


def test_v3_shaped_row_is_still_valid_at_v4(tmp_path):
    """Additive-optional means a row carrying NEITHER new field parses clean at
    v4 and reads back the same defaults it had at v3 — the bump is a field-SET
    change, not a value-shape break for existing rows."""
    data = reg.load_registry(
        _write_registry(
            tmp_path,
            """
            schema_version = 4
            [snippet.foo]
            sentinel_begin = "b"
            sentinel_end = "e"
            delivery = "inject"
            consumers = ["agents/a.md"]
            """,
        )
    )
    meta = reg.get_snippet_meta(data, "foo")
    assert meta["excluded_consumer"] == []
    assert meta["eligible_glob"] is None
    assert meta["delivery"] == "inject"


def test_v4_schema_version_is_supported(tmp_path):
    """Regression pin for the break this fix closes: a v4 registry must not
    fail-loud with the unknown-schema_version exit 3."""
    data = reg.load_registry(_write_registry(tmp_path, _V4_VALID))
    assert data["schema_version"] == 4


@pytest.mark.parametrize(
    "excl_block, fragment",
    [
        pytest.param('path = "agents/x.md"\n', "missing required field 'reason'", id="no-reason"),
        pytest.param(
            'path = "agents/x.md"\nreason = ""\n', "empty 'reason'", id="empty-reason"
        ),
        pytest.param(
            'path = "agents/x.md"\nreason = "   "\n', "empty 'reason'", id="whitespace-reason"
        ),
        pytest.param('reason = "why"\n', "missing required field 'path'", id="no-path"),
    ],
)
def test_excluded_consumer_reason_and_path_are_required(tmp_path, excl_block, fragment):
    registry_path = _write_registry(
        tmp_path,
        f"""
        schema_version = 4
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumers = []

        [[snippet.foo.excluded_consumer]]
        {excl_block}
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert fragment in str(exc_info.value)


def test_excluded_consumer_path_also_in_consumers_is_contradictory(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 4
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumers = ["agents/x.md"]

        [[snippet.foo.excluded_consumer]]
        path   = "agents/x.md"
        reason = "cannot be both"
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert "contradictory declaration" in str(exc_info.value)


@pytest.mark.parametrize(
    "field_block",
    [
        pytest.param('eligible_glob = "agents/*.md"', id="eligible_glob"),
        pytest.param(
            '[[snippet.foo.excluded_consumer]]\npath = "agents/x.md"\nreason = "r"',
            id="excluded_consumer",
        ),
    ],
)
def test_v4_fields_forbidden_on_scan_row(tmp_path, field_block):
    """sentinel-presence-on-disk IS enrolment on a scan row, so a declared
    exclusion (or a declared universe) is incoherent, not merely unused."""
    registry_path = _write_registry(
        tmp_path,
        f"""
        schema_version = 4
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumer_source = "scan"
        consumers = []
        {field_block}
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert "FORBIDDEN" in str(exc_info.value)


def test_v4_fields_rejected_below_schema_version_4(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 3
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumers = []
        eligible_glob = "agents/*.md"
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert "requires schema_version >= 4" in str(exc_info.value)


def test_eligible_glob_must_be_root_relative(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 4
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumers = []
        eligible_glob = "/etc/*.md"
        """,
    )
    with pytest.raises(reg.RegistryError) as exc_info:
        reg.load_registry(registry_path)
    assert exc_info.value.exit_code == 1
    assert "content-root-RELATIVE" in str(exc_info.value)


def _v4_glob_tree(tmp_path: Path) -> Path:
    agents = tmp_path / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for stem in ("enrolled", "bespoke", "undeclared"):
        (agents / f"{stem}.md").write_text("x\n", encoding="utf-8")
    return tmp_path


def test_eligible_glob_gaps_reports_only_undeclared_members(tmp_path):
    registry_path = _write_registry(tmp_path, _V4_VALID)
    data = reg.load_registry(registry_path)
    _v4_glob_tree(tmp_path)
    assert reg.eligible_glob_gaps(data, "foo", tmp_path) == ["agents/undeclared.md"]


def test_eligible_glob_gaps_empty_when_universe_fully_declared(tmp_path):
    registry_path = _write_registry(tmp_path, _V4_VALID)
    data = reg.load_registry(registry_path)
    _v4_glob_tree(tmp_path)
    (tmp_path / "agents" / "undeclared.md").unlink()
    assert reg.eligible_glob_gaps(data, "foo", tmp_path) == []


def test_eligible_glob_gaps_honour_content_root(tmp_path):
    """The completeness check must glob the SAME tree the consumer set resolves
    against — globbing the true plugin root under a COORDINATOR_CONTENT_ROOT
    redirect is the split `effective_content_root` exists to close, and it would
    report every real file of the wrong tree as undeclared."""
    plugin_root = tmp_path / "plugin"
    registry_path = _write_registry(plugin_root, _V4_VALID)
    data = reg.load_registry(registry_path)
    # The REAL tree carries a file that is undeclared; the redirect target does not.
    _v4_glob_tree(plugin_root)
    content_root = tmp_path / "redirect"
    (content_root / "agents").mkdir(parents=True)
    (content_root / "agents" / "enrolled.md").write_text("x\n", encoding="utf-8")

    assert reg.eligible_glob_gaps(data, "foo", plugin_root) == ["agents/undeclared.md"]
    assert reg.eligible_glob_gaps(data, "foo", plugin_root, content_root=content_root) == []


def test_eligible_glob_absent_means_no_gaps(tmp_path):
    registry_path = _write_registry(
        tmp_path,
        """
        schema_version = 4
        [snippet.foo]
        sentinel_begin = "b"
        sentinel_end = "e"
        delivery = "paste"
        consumers = []
        """,
    )
    data = reg.load_registry(registry_path)
    _v4_glob_tree(tmp_path)
    assert reg.eligible_glob_gaps(data, "foo", tmp_path) == []
