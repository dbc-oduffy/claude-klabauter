"""coordinator_core/hooks/tests/test_arrival_w4_c6.py — the W4-C6 arrival gate
for `coordinator_core.hooks.oss_operative_strings`.

Subject: `coordinator_core/hooks/oss_operative_strings.py` — the ratified
classification data (shape rules + the irreducible-literal residue table)
DoE-claude's DR-141 predicate cluster imports over the wire, per
`docs/plans/2026-09-18-doe-holds-no-scripts.md`'s W4-C6 row. The cluster
itself (`_prompt_surface_locality.py`, `guard-oss-payload-locality.py`, and
the rest) stays DoE-resident — this file carries only the data/shape-rule
module the row's own body names as its remaining write.

Ported behavior coverage against DoE-claude's own
`coordinator/tests/test_oss_payload_locality.py` assertions on these public
symbols (`is_identifier_shape_operative`, `is_stable_artifact_id`,
`SIBLING_REPO_RECORD`/`SIBLING_REPO_NAMES`, `IRREDUCIBLE_LITERALS`,
`mcp_tool_prefixes`) at DoE-claude `d4122a0a2`, plus new coverage for this
arrival's own shape change: `_resolve_mcp_topology_path` resolves the
doctrine-asset YAML through the plugin content root / `.doe-root` pointer
rather than a `Path(__file__)`-relative walk (this module no longer sits
three directories under a DoE-repo root), and the dynamic, sys.path-driven
`_oss_payload` import in `_engine_sibling_record` still degrades correctly
when only DoE-claude's own tree — not claude-klabauter's — carries that module.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from coordinator_core.hooks import oss_operative_strings as ops


def test_module_registers_no_op():
    assert not hasattr(ops, "register_op")


def test_module_is_not_in_eager_hook_modules():
    from coordinator_core.hooks import _EAGER_HOOK_MODULES

    assert "coordinator_core.hooks.oss_operative_strings" not in _EAGER_HOOK_MODULES


def test_import_is_fast():
    mod_name = "coordinator_core.hooks.oss_operative_strings"
    sys.modules.pop(mod_name, None)
    start = time.perf_counter()
    import coordinator_core.hooks.oss_operative_strings  # noqa: F401

    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"import took {elapsed:.3f}s, over the 500ms brightline"


def test_env_var_shape_is_operative():
    assert ops.is_identifier_shape_operative("REPO_DOE_CLAUDE", sibling_names=("DoE-claude",))


def test_dotted_registry_key_shape_is_operative():
    assert ops.is_identifier_shape_operative(
        "plugin.mirrors.doe-claude.source_path", sibling_names=("DoE-claude",)
    )


def test_bare_sibling_name_is_not_shape_operative():
    assert not ops.is_identifier_shape_operative("DoE-claude", sibling_names=("DoE-claude",))


def test_trailing_sentence_period_is_stripped():
    assert not ops.is_identifier_shape_operative("DoE.", sibling_names=("DoE",))
    assert ops.is_identifier_shape_operative("REPO_DOE.", sibling_names=("DoE",))


def test_stable_artifact_id_component_matches_sibling():
    token = "hnd-doe-claude-something-abc123"
    assert ops.is_stable_artifact_id(token, sibling_names=("DoE-claude",))
    assert ops.is_identifier_shape_operative(token, sibling_names=("DoE-claude",))


def test_bare_sibling_name_is_not_a_stable_artifact_id():
    assert not ops.is_stable_artifact_id("DoE-claude", sibling_names=("DoE-claude",))


def test_unrelated_hyphenated_word_does_not_false_positive():
    assert not ops.is_identifier_shape_operative(
        "some-other-thing", sibling_names=("DoE-claude",)
    )


# SIBLING_REPO_RECORD / SIBLING_REPO_NAMES / IRREDUCIBLE_LITERALS


def test_pinned_unreachable_names_always_present():
    assert "DoE-claude" in ops.SIBLING_REPO_NAMES
    assert "DoE" in ops.SIBLING_REPO_NAMES
    assert ops.SIBLING_REPO_RECORD["DoE-claude"]["oss_reachable"] is False
    assert ops.SIBLING_REPO_RECORD["DoE-claude"]["case_sensitive"] is True


def test_engine_sibling_arm_fails_open_without_oss_payload():
    record = ops._sibling_repo_record()
    assert "claude-klabauter" not in record
    assert set(record) == {"DoE-claude", "DoE"}


def test_engine_sibling_arm_resolves_when_oss_payload_importable(monkeypatch):
    import types

    fake_oss_payload = types.ModuleType("_oss_payload")
    fake_oss_payload._ENGINE_REPO_NAME = "claude-klabauter"
    monkeypatch.setitem(sys.modules, "_oss_payload", fake_oss_payload)

    record = ops._sibling_repo_record()
    assert record["claude-klabauter"]["is_engine_sibling"] is True
    assert record["claude-klabauter"]["short_forms"] == ("claude-klabauter",)
    assert record["claude-klabauter"]["oss_reachable"] is False


def test_irreducible_literals_nonempty_and_scoped():
    assert ops.IRREDUCIBLE_LITERALS
    literal, file_, line, reason = ops.IRREDUCIBLE_LITERALS[0]
    assert literal == "claude-klabauter"
    assert file_.endswith("_engine_root.py")
    assert isinstance(line, int) and line > 0
    assert reason


def test_mcp_tool_prefixes_fails_open_when_topology_unresolvable(monkeypatch):
    monkeypatch.setattr(ops, "_resolve_mcp_topology_path", lambda: None)
    assert ops.mcp_tool_prefixes() == frozenset()


def test_mcp_tool_prefixes_fails_open_on_malformed_yaml(tmp_path, monkeypatch):
    bad = tmp_path / "mcp-topology.yaml"
    bad.write_text("not: [valid, yaml: because", encoding="utf-8")
    monkeypatch.setattr(ops, "_resolve_mcp_topology_path", lambda: bad)
    assert ops.mcp_tool_prefixes() == frozenset()


def test_mcp_tool_prefixes_derives_from_config_keys(tmp_path, monkeypatch):
    topo = tmp_path / "mcp-topology.yaml"
    topo.write_text(
        "servers:\n"
        "  - configKey: example-retrieval-repo\n"
        "  - configKey: example-game-repo-control\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ops, "_resolve_mcp_topology_path", lambda: topo)
    prefixes = ops.mcp_tool_prefixes()
    assert prefixes == frozenset({"mcp__project-rag__", "mcp__example_game_repo-control__"})


def test_mcp_tool_prefix_span_matches_derived_prefix():
    assert ops.MCP_TOOL_PREFIX_SPAN.match("mcp__project-rag__project_file")


def test_resolve_mcp_topology_path_returns_path_or_none():
    result = ops._resolve_mcp_topology_path()
    assert result is None or isinstance(result, Path)


def test_resolve_mcp_topology_path_fails_open_on_import_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _blocking_import(name, *args, **kwargs):
        if name in (
            "coordinator_core._settings_home",
            "coordinator_core.data_root",
            "coordinator_core.doe_root_pointer",
        ):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocking_import)
    assert ops._resolve_mcp_topology_path() is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
