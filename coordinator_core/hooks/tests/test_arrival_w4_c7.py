"""coordinator_core/hooks/tests/test_arrival_w4_c7.py -- the W4-C7 arrival
gate for the write-path guards landed under `coordinator_core/hooks/`.

Subject: the fourteen modules this row's `writes` list names (thirteen
`.py` modules plus this file itself) -- `preuse_write_dispatch`,
`guard_python_syntax_on_write`, `guard_posix_invocation_doctrine_write`,
`posix_invocation_detect`, `guard_test_tree_git_fixture_spawn`,
`guard_handoff_summary_cap_on_write`, `guard_repo_setup_claude_home_refusal`,
`guard_phantom_staged_deletion_precommit`, `phantom_staged_deletion`,
`guard_review_integrator_sidecar_intake`, `nudge_plan_test_surface_tier`,
`emit_effective_delivery`, `fanin_registries` -- ported from DoE-claude
`coordinator/hooks/scripts/` per docs/plans/2026-09-18-doe-holds-no-scripts.md
§ W4-C7.

Shape coverage: every `hooks.<name>` op module registers under exactly the
name this row's own body prescribes; the three pure-support modules
(`posix_invocation_detect`, `phantom_staged_deletion`, `fanin_registries`)
and the one native git pre-commit hook
(`guard_phantom_staged_deletion_precommit`) register no op at all -- see
each module's own arrival-note docstring for why.

Behaviour coverage: one fast (wrong-tool-name / no-target) fail-open path
per handler, plus at least one substantive positive path per module where
that is cheap to construct without a git checkout -- pure predicate logic
(`posix_invocation_detect`, `phantom_staged_deletion`) gets full positive/
negative coverage; the two guards whose real behaviour depends on `git`
subprocess reads (`guard_test_tree_git_fixture_spawn`,
`guard_review_integrator_sidecar_intake`) are exercised only on their
fast fail-open leg here -- their own predicate helpers, not this file, own
deeper coverage.

`emit_effective_delivery` gets its own section: `tail_key()`/`_check_string()`
unit coverage (pure), plus one end-to-end `build_block()` smoke test against
this box's own resolved DoE-claude checkout, skipped when that checkout
is not resolvable (CI/cloud boxes without a DoE sibling clone) rather than
failing -- this module's own cross-repo read is the one thing in this row
that cannot be faked without a real `hooks.json`.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops.session import emit_effective_delivery
from coordinator_core.hooks import (
    fanin_registries,
    guard_handoff_summary_cap_on_write,
    guard_phantom_staged_deletion_precommit,
    guard_posix_invocation_doctrine_write,
    guard_python_syntax_on_write,
    guard_repo_setup_claude_home_refusal,
    guard_review_integrator_sidecar_intake,
    guard_test_tree_git_fixture_spawn,
    nudge_plan_test_surface_tier,
    phantom_staged_deletion,
    posix_invocation_detect,
    preuse_write_dispatch,
)


# ---------------------------------------------------------------------------
# register_op shape: op modules register under hooks.<name>; support
# modules and the native git hook register nothing.
# ---------------------------------------------------------------------------

_OP_MODULES = {
    "preuse_write_dispatch": preuse_write_dispatch,
    "guard_python_syntax_on_write": guard_python_syntax_on_write,
    "guard_posix_invocation_doctrine_write": guard_posix_invocation_doctrine_write,
    "guard_test_tree_git_fixture_spawn": guard_test_tree_git_fixture_spawn,
    "guard_handoff_summary_cap_on_write": guard_handoff_summary_cap_on_write,
    "guard_repo_setup_claude_home_refusal": guard_repo_setup_claude_home_refusal,
    "guard_review_integrator_sidecar_intake": guard_review_integrator_sidecar_intake,
    "nudge_plan_test_surface_tier": nudge_plan_test_surface_tier,
}

_NON_OP_MODULES = [
    posix_invocation_detect,
    phantom_staged_deletion,
    fanin_registries,
    guard_phantom_staged_deletion_precommit,
]


@pytest.mark.parametrize("op_name,module", sorted(_OP_MODULES.items()))
def test_op_module_registers_hooks_op(op_name, module):
    from coordinator_core.ipc import get_op_handler

    handler = get_op_handler(f"hooks.{op_name}")
    assert handler is not None, f"hooks.{op_name} not registered by {module.__name__}"


@pytest.mark.parametrize("module", _NON_OP_MODULES)
def test_non_op_module_has_no_handler(module):
    assert not hasattr(module, "_handler")


def test_precommit_wrapper_has_main_not_handler():
    assert callable(guard_phantom_staged_deletion_precommit.main)
    assert not hasattr(guard_phantom_staged_deletion_precommit, "_handler")


# ---------------------------------------------------------------------------
# preuse_write_dispatch
# ---------------------------------------------------------------------------


def test_preuse_write_dispatch_delegates_to_evaluate(monkeypatch):
    sentinel = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny"}}

    def _fake_evaluate(params, skipped_out=None, **kwargs):
        assert params["tool_name"] == "Write"
        return sentinel

    monkeypatch.setattr(preuse_write_dispatch, "evaluate", _fake_evaluate)
    result = asyncio.run(preuse_write_dispatch._handler({"tool_name": "Write", "tool_input": {}}))
    assert result is sentinel


def test_preuse_write_dispatch_fails_open_on_engine_exception(monkeypatch):
    def _raise(params, skipped_out=None, **kwargs):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(preuse_write_dispatch, "evaluate", _raise)
    result = asyncio.run(preuse_write_dispatch._handler({"tool_name": "Write", "tool_input": {}}))
    assert result == {}


# ---------------------------------------------------------------------------
# guard_python_syntax_on_write
# ---------------------------------------------------------------------------


def test_python_syntax_guard_scope_predicate():
    from pathlib import Path

    assert guard_python_syntax_on_write.is_in_scope(
        Path("/repo/coordinator_core/hooks/foo.py")
    )
    assert not guard_python_syntax_on_write.is_in_scope(Path("/repo/coordinator_core/hooks/foo.md"))
    assert not guard_python_syntax_on_write.is_in_scope(Path("/repo/docs/foo.py"))


def test_python_syntax_guard_allows_wrong_tool():
    result = asyncio.run(guard_python_syntax_on_write._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


def test_python_syntax_guard_denies_unparseable_write(tmp_path):
    target = tmp_path / "coordinator_core" / "hooks" / "broken.py"
    target.parent.mkdir(parents=True)
    params = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "def broken(:\n    pass\n"},
    }
    result = asyncio.run(guard_python_syntax_on_write._handler(params))
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_python_syntax_guard_allows_valid_write(tmp_path):
    target = tmp_path / "coordinator_core" / "hooks" / "fine.py"
    target.parent.mkdir(parents=True)
    params = {
        "tool_name": "Write",
        "tool_input": {"file_path": str(target), "content": "def fine():\n    return 1\n"},
    }
    result = asyncio.run(guard_python_syntax_on_write._handler(params))
    assert result == {}


# ---------------------------------------------------------------------------
# posix_invocation_detect -- pure predicate, full coverage
# ---------------------------------------------------------------------------


def test_posix_invocation_detect_positive_hit():
    text = (
        "source \"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}"
        "/bin/coordinator-doc-new\""
    )
    hits = posix_invocation_detect.find_posix_forwarder_invocations(text)
    assert len(hits) == 1
    assert hits[0].cli == "coordinator-doc-new"
    assert posix_invocation_detect.has_posix_forwarder_invocation(text)


def test_posix_invocation_detect_no_hit_on_plain_expansion():
    text = "${SOME_VAR:-default value with no forwarder mentioned at all}"
    assert posix_invocation_detect.find_posix_forwarder_invocations(text) == []
    assert not posix_invocation_detect.has_posix_forwarder_invocation(text)


def test_posix_invocation_detect_dropped_when_shape_w_sibling_present():
    text = (
        "POSIX host:\n"
        '  ${COORDINATOR_SETTINGS_HOME:-$HOME}/bin/coordinator-doc-new\n'
        "PowerShell host (rung 0):\n"
        '  & "$env:COORDINATOR_SETTINGS_HOME\\bin\\coordinator-doc-new.cmd"\n'
    )
    assert posix_invocation_detect.find_posix_forwarder_invocations(text) == []


def test_posix_invocation_detect_nested_expansion_reported_once():
    text = "${COORDINATOR_SETTINGS_HOME:-${CLAUDE_HOME:-$HOME}/.x}/bin/coordinator-doc-new"
    hits = posix_invocation_detect.find_posix_forwarder_invocations(text)
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# guard_posix_invocation_doctrine_write
# ---------------------------------------------------------------------------


def test_posix_doctrine_write_guard_allows_wrong_tool():
    result = asyncio.run(
        guard_posix_invocation_doctrine_write._handler({"tool_name": "Read", "tool_input": {}})
    )
    assert result == {}


def test_posix_doctrine_write_guard_advises_on_hit(tmp_path):
    target = tmp_path / "coordinator" / "skills" / "example" / "SKILL.md"
    target.parent.mkdir(parents=True)
    content = "run: ${COORDINATOR_SETTINGS_HOME:-$HOME}/bin/coordinator-doc-new"
    params = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    result = asyncio.run(guard_posix_invocation_doctrine_write._handler(params))
    assert result.get("hookSpecificOutput", {}).get("permissionDecision") == "allow"


# ---------------------------------------------------------------------------
# guard_test_tree_git_fixture_spawn / guard_review_integrator_sidecar_intake
# -- fast fail-open leg only; deep behaviour is git-backed and out of this
# file's remit (see module docstring).
# ---------------------------------------------------------------------------


def test_test_tree_git_fixture_guard_allows_wrong_tool():
    result = asyncio.run(guard_test_tree_git_fixture_spawn._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


def test_review_integrator_sidecar_guard_allows_wrong_tool():
    result = asyncio.run(guard_review_integrator_sidecar_intake._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


# ---------------------------------------------------------------------------
# guard_handoff_summary_cap_on_write
# ---------------------------------------------------------------------------


def test_handoff_summary_cap_guard_allows_short_summary(tmp_path):
    target = tmp_path / "state" / "handoffs" / "2026-09-18-x.md"
    target.parent.mkdir(parents=True)
    content = "---\nsummary: short\n---\nbody\n"
    params = {"tool_name": "Write", "tool_input": {"file_path": str(target), "content": content}}
    result = asyncio.run(guard_handoff_summary_cap_on_write._handler(params))
    assert result == {}


def test_handoff_summary_cap_guard_allows_wrong_tool():
    result = asyncio.run(guard_handoff_summary_cap_on_write._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


# ---------------------------------------------------------------------------
# guard_repo_setup_claude_home_refusal
# ---------------------------------------------------------------------------


def test_repo_setup_claude_home_refusal_allows_wrong_tool():
    result = asyncio.run(guard_repo_setup_claude_home_refusal._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


# ---------------------------------------------------------------------------
# nudge_plan_test_surface_tier
# ---------------------------------------------------------------------------


def test_plan_test_surface_tier_allows_wrong_tool():
    result = asyncio.run(nudge_plan_test_surface_tier._handler({"tool_name": "Read", "tool_input": {}}))
    assert result == {}


# ---------------------------------------------------------------------------
# phantom_staged_deletion -- pure predicate, full coverage
# ---------------------------------------------------------------------------


def test_phantom_staged_deletion_parses_name_status_z():
    raw = "D\0gone.md\0A\0new.md\0"
    rows = phantom_staged_deletion.parse_name_status_z(raw)
    assert rows == [("D", "gone.md"), ("A", "new.md")]


def test_phantom_staged_deletion_parses_rename_by_destination():
    raw = "R100\0old.md\0new.md\0"
    rows = phantom_staged_deletion.parse_name_status_z(raw)
    assert rows == [("R", "new.md")]


def test_phantom_staged_deletion_classify_detects_phantom():
    rows = [("D", "still-here.md")]
    findings = phantom_staged_deletion.classify(
        rows,
        exists_on_disk=lambda p: True,
        disk_matches_head=lambda p: True,
    )
    assert len(findings) == 1
    assert findings[0].path == "still-here.md"
    assert findings[0].disk_matches_head is True


def test_phantom_staged_deletion_classify_quiet_on_ordinary_deletion():
    rows = [("D", "really-gone.md")]
    findings = phantom_staged_deletion.classify(
        rows,
        exists_on_disk=lambda p: False,
        disk_matches_head=lambda p: None,
    )
    assert findings == []


def test_phantom_staged_deletion_classify_quiet_on_delete_then_readd():
    rows = [("D", "x.md"), ("A", "x.md")]
    findings = phantom_staged_deletion.classify(
        rows,
        exists_on_disk=lambda p: True,
        disk_matches_head=lambda p: True,
    )
    assert findings == []


def test_phantom_staged_deletion_classify_quiet_when_not_in_head():
    rows = [("D", "untracked-shadow.md")]
    findings = phantom_staged_deletion.classify(
        rows,
        exists_on_disk=lambda p: True,
        disk_matches_head=lambda p: None,
    )
    assert findings == []


def test_phantom_staged_deletion_render_report_names_override_env():
    finding = phantom_staged_deletion.Finding(path="x.md", disk_matches_head=True)
    report = phantom_staged_deletion.render_report([finding], "COORDINATOR_OVERRIDE_X")
    assert "COORDINATOR_OVERRIDE_X" in report
    assert "x.md" in report


# ---------------------------------------------------------------------------
# guard_phantom_staged_deletion_precommit -- main() over injected env, no
# real git spawn (fast override leg only; the git-backed leg is this
# guard's own runtime behaviour, exercised in the field, not here -- see
# this file's own module docstring on the git-backed pair above).
# ---------------------------------------------------------------------------


def test_phantom_precommit_main_short_circuits_on_override(monkeypatch):
    monkeypatch.setenv(guard_phantom_staged_deletion_precommit.OVERRIDE_ENV, "1")
    assert guard_phantom_staged_deletion_precommit.main() == 0


# ---------------------------------------------------------------------------
# fanin_registries
# ---------------------------------------------------------------------------


def test_fanin_dispatchers_down_selected_to_three_landed_carriers():
    assert set(fanin_registries.FANIN_DISPATCHERS) == {
        "preuse_write_dispatch",
        "stop_dispatch",
        "postuse_advisory_dispatch",
    }


def test_fanin_load_carrier_imports_by_module_name():
    module = fanin_registries.load_carrier("preuse_write_dispatch")
    assert module is preuse_write_dispatch


def test_fanin_carried_guards_raises_on_unenrolled_name():
    with pytest.raises(AttributeError, match="not an enrolled fan-in carrier"):
        fanin_registries.carried_guards("not-a-real-carrier")


def test_fanin_carried_guards_raises_on_undeclared_carrier():
    # preuse_write_dispatch is enrolled but declares no CARRIED_GUARDS
    # today -- AttributeError, never a fabricated empty list.
    with pytest.raises(AttributeError, match="CARRIED_GUARDS"):
        fanin_registries.carried_guards("preuse_write_dispatch")


def test_fanin_all_carried_guards_skips_undeclared_carriers():
    # None of the three landed carriers declare CARRIED_GUARDS today, so
    # the union is empty -- not an error, per all_carried_guards()'s own
    # docstring contract.
    assert fanin_registries.all_carried_guards() == {}


# ---------------------------------------------------------------------------
# emit_effective_delivery -- pure-unit coverage plus one gated end-to-end
# smoke test against this box's resolved DoE-claude checkout.
# ---------------------------------------------------------------------------


def test_emit_effective_delivery_tail_key_two_segments():
    assert (
        emit_effective_delivery.tail_key(
            "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/preuse-write-dispatch.py"
        )
        == "scripts/preuse-write-dispatch.py"
    )


def test_emit_effective_delivery_tail_key_raises_on_bare_name():
    with pytest.raises(emit_effective_delivery.EmitterError):
        emit_effective_delivery.tail_key("bare-name.py")


def test_emit_effective_delivery_check_string_rejects_multiline():
    with pytest.raises(emit_effective_delivery.EmitterError):
        emit_effective_delivery._check_string("x", "line one\nline two")


def test_emit_effective_delivery_check_string_rejects_over_cap():
    with pytest.raises(emit_effective_delivery.EmitterError):
        emit_effective_delivery._check_string("x", "a" * 201)


def test_emit_effective_delivery_check_string_passes_ok_value():
    assert emit_effective_delivery._check_string("x", "fine") == "fine"


def test_emit_effective_delivery_matcher_tool_names_splits_and_drops_empty():
    assert emit_effective_delivery._matcher_tool_names("Bash|PowerShell") == ["Bash", "PowerShell"]
    assert emit_effective_delivery._matcher_tool_names("") == []


def test_emit_effective_delivery_provenance_keys_shape():
    assert emit_effective_delivery.PROVENANCE_KEYS == (
        "generated_from_sha",
        "generated_at",
        "generated_from_dirty_tree",
    )


#: A minimal but structurally complete `hooks.json` -- one registration per
#: carrier this generator's four dedicated builders need, one retired-tail
#: exclusion, and nothing else. Deliberately NOT read off this box's real
#: DoE-claude checkout: `coordinator_core/conftest.py`'s suite-wide home
#: quarantine seeds only a NAMED allowlist of DoE-side files into its stub
#: root (the registry manifest, the cross-repo-memo schema) -- `hooks.json`
#: is not one of them, by that fixture's own "narrow, deliberate, one file
#: at a time" design, and widening that allowlist is outside this row's own
#: footprint. Building a synthetic `hooks.json` here keeps this suite
#: self-contained and honest under quarantine rather than silently skipped
#: on every machine (which a `.doe-root`-gated skip would have been, since
#: quarantine strips that resolution on every test by construction).
def _synthetic_hooks_json() -> dict:
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
                    "hooks": [
                        {
                            "type": "command",
                            "args": [
                                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/preuse-write-dispatch.py"
                            ],
                        }
                    ],
                },
                {
                    "matcher": "Bash|PowerShell",
                    "hooks": [
                        {
                            "type": "command",
                            "args": [
                                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/preuse-bash-dispatch.py"
                            ],
                        }
                    ],
                },
                {
                    "matcher": "Write",
                    "hooks": [
                        {
                            "type": "command",
                            "args": ["${CLAUDE_PLUGIN_ROOT}/hooks/scripts/some-direct-guard.py"],
                        }
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "args": [
                                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/postuse-stop-family-dispatch.py"
                            ],
                        }
                    ],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "args": [
                                "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/postuse-advisory-dispatch.py"
                            ],
                        }
                    ],
                }
            ],
        }
    }


def _seed_synthetic_doe_content_root(tmp_path) -> "tuple":
    """Writes a synthetic `hooks.json` (and its required
    `scripts/postuse-advisory-dispatch.py` sibling, read only for the
    regex cross-check) under `tmp_path`, and returns
    `(hooks_json_path, manifest_path)` in the exact shape
    `_resolve_manifest_paths()` returns."""
    import json as _json

    content_root = tmp_path / "coordinator"
    scripts_dir = content_root / "hooks" / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    hooks_json_path = content_root / "hooks" / "hooks.json"
    hooks_json_path.write_text(_json.dumps(_synthetic_hooks_json()), encoding="utf-8")
    (scripts_dir / "postuse-advisory-dispatch.py").write_text(
        "# synthetic stub, no dict-literal method call sites\n", encoding="utf-8"
    )
    manifest_path = content_root / "hooks" / "effective-delivery.json"
    return hooks_json_path, manifest_path


def _build_block_against_synthetic_root(tmp_path, monkeypatch):
    hooks_json_path, manifest_path = _seed_synthetic_doe_content_root(tmp_path)
    monkeypatch.setattr(
        emit_effective_delivery,
        "_resolve_manifest_paths",
        lambda: (hooks_json_path, manifest_path),
    )
    # `_emission_provenance` reads git in the DoE repo -- this synthetic
    # content root is a bare tmp_path with no `.git`, so it is stubbed to
    # isolate this test to the `hooks.json`-parsing/carrier-building logic.
    monkeypatch.setattr(
        emit_effective_delivery,
        "_emission_provenance",
        lambda hooks_json_path: {
            "generated_from_sha": "f" * 40,
            "generated_at": "2026-09-19T00:00:00Z",
            "generated_from_dirty_tree": False,
        },
    )
    return emit_effective_delivery.build_block()


def test_emit_effective_delivery_build_block_against_synthetic_hooks_json(tmp_path, monkeypatch):
    block = _build_block_against_synthetic_root(tmp_path, monkeypatch)
    assert block["version"] == 1
    assert set(block["carriers"]) == {
        "scripts/preuse-write-dispatch.py",
        "scripts/preuse-bash-dispatch.py",
        "scripts/postuse-stop-family-dispatch.py",
        "scripts/postuse-advisory-dispatch.py",
    }
    assert [d["script"] for d in block["direct"]] == ["scripts/some-direct-guard.py"]
    assert isinstance(block["retired"], list) and block["retired"]
    # bash_dispatch carrier's guards come from this repo's own real
    # guard_roster() -- same-repo read, no monkeypatch needed (see module
    # docstring's "WHAT CHANGED -- the cross-plane reads").
    bash_carrier = block["carriers"]["scripts/preuse-bash-dispatch.py"]
    assert bash_carrier["guards"], "expected at least one real bash guard in the roster"
    for key in emit_effective_delivery.PROVENANCE_KEYS:
        assert key in block
    rendered = emit_effective_delivery.render_block(block)
    assert emit_effective_delivery.MANIFEST_KEY in rendered


def test_emit_effective_delivery_render_block_is_idempotent_modulo_provenance(tmp_path, monkeypatch):
    block_a = _build_block_against_synthetic_root(tmp_path, monkeypatch)
    block_b = _build_block_against_synthetic_root(tmp_path, monkeypatch)
    stripped_a = {k: v for k, v in block_a.items() if k not in emit_effective_delivery.PROVENANCE_KEYS}
    stripped_b = {k: v for k, v in block_b.items() if k not in emit_effective_delivery.PROVENANCE_KEYS}
    assert stripped_a == stripped_b


def test_emit_effective_delivery_build_block_fails_closed_on_missing_hooks_json(tmp_path, monkeypatch):
    missing = tmp_path / "coordinator" / "hooks" / "hooks.json"
    manifest_path = tmp_path / "coordinator" / "hooks" / "effective-delivery.json"
    monkeypatch.setattr(
        emit_effective_delivery,
        "_resolve_manifest_paths",
        lambda: (missing, manifest_path),
    )
    with pytest.raises(emit_effective_delivery.EmitterError):
        emit_effective_delivery.build_block()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
