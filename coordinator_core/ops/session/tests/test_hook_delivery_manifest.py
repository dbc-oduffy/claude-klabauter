
from __future__ import annotations

from coordinator_core.ops.session.hook_delivery_manifest import (
    MANIFEST_KEY,
    HookDeliveryManifest,
    ManifestGuard,
    RetiredGuard,
    read_hook_delivery_manifest,
)


def _manifest_block(**overrides) -> dict:
    block = {
        "version": 1,
        "carriers": {
            "scripts/preuse-write-dispatch.py": {
                "guards": [
                    {
                        "id": "check_claude_md_size",
                        "script": "scripts/check-claude-md-size.py",
                        "tool_names": ["Bash"],
                    }
                ]
            }
        },
        "direct": [],
        "retired": [],
    }
    block.update(overrides)
    return {MANIFEST_KEY: block}


def test_ok_round_trip():
    hooks_json = _manifest_block()
    declared = ["scripts/preuse-write-dispatch.py"]

    result = read_hook_delivery_manifest(hooks_json, declared)

    assert result.state == "ok"
    assert result.detail == ""
    assert result.unaccounted == ()
    carrier_key = "scripts/preuse-write-dispatch.py"
    assert carrier_key in result.carriers
    assert result.carriers[carrier_key] == (
        ManifestGuard(
            id="check_claude_md_size",
            script="scripts/check-claude-md-size.py",
            tool_names=("Bash",),
        ),
    )
    assert result.script_index["scripts/check-claude-md-size.py"] == ("check_claude_md_size",)


def test_absent_no_key():
    result = read_hook_delivery_manifest({"hooks": {}}, [])
    assert result.state == "absent"
    assert result.carriers == {}
    assert result.detail != ""


def test_absent_non_dict_input():
    result = read_hook_delivery_manifest(None, [])
    assert result.state == "absent"


def test_malformed_block_not_object():
    result = read_hook_delivery_manifest({MANIFEST_KEY: "not-an-object"}, [])
    assert result.state == "malformed"


def test_malformed_missing_version():
    hooks_json = {MANIFEST_KEY: {"carriers": {}, "direct": [], "retired": []}}
    result = read_hook_delivery_manifest(hooks_json, [])
    assert result.state == "malformed"


def test_malformed_carriers_not_object():
    hooks_json = _manifest_block(carriers=[])
    result = read_hook_delivery_manifest(hooks_json, [])
    assert result.state == "malformed"


def test_version_unsupported():
    hooks_json = _manifest_block(version=99)
    result = read_hook_delivery_manifest(hooks_json, [])
    assert result.state == "version_unsupported"
    assert "99" in result.detail


def test_stale_names_specific_unaccounted_key():
    hooks_json = _manifest_block()
    declared = [
        "scripts/preuse-write-dispatch.py",
        "scripts/some-fourth-carrier.py",
    ]

    result = read_hook_delivery_manifest(hooks_json, declared)

    assert result.state == "stale"
    assert result.unaccounted == ("scripts/some-fourth-carrier.py",)
    assert "some-fourth-carrier.py" in result.detail


def test_retired_reason_newline_sanitized():
    hooks_json = _manifest_block(
        retired=[
            {
                "id": "nudge_foreground_agent",
                "script": "scripts/nudge-foreground-agent-dispatch.py",
                "reason": "deregistered 2026-07-31\nraces enforce-agent-dispatch-mode.py",
            }
        ]
    )
    declared = ["scripts/preuse-write-dispatch.py"]

    result = read_hook_delivery_manifest(hooks_json, declared)

    assert result.state == "ok"
    assert len(result.retired) == 1
    retired = result.retired[0]
    assert isinstance(retired, RetiredGuard)
    assert "\n" not in retired.reason
    assert "\r" not in retired.reason


def test_retired_reason_ansi_escape_sanitized():
    hooks_json = _manifest_block(
        retired=[
            {
                "id": "runtime_tripwire_stop_watcher",
                "script": "scripts/runtime-tripwire-stop-watcher.py",
                "reason": "stood down\x1b[31m by PM ruling\x1b[0m",
            }
        ]
    )
    declared = ["scripts/preuse-write-dispatch.py"]

    result = read_hook_delivery_manifest(hooks_json, declared)

    assert result.state == "ok"
    retired = result.retired[0]
    assert "\x1b" not in retired.reason
    assert "\x1b[" not in retired.reason


def test_never_raises_on_garbage_input():
    garbage_inputs = [
        {},
        {MANIFEST_KEY: None},
        {MANIFEST_KEY: {"version": "not-an-int"}},
        {MANIFEST_KEY: {"version": 1, "direct": "not-a-list"}},
        {MANIFEST_KEY: {"version": 1, "retired": "not-a-list"}},
        {MANIFEST_KEY: {"version": 1, "carriers": {"bad-key-no-slash": {"guards": []}}}},
        {MANIFEST_KEY: {"version": 1, "carriers": {"scripts/x.py": "not-a-dict"}}},
        {MANIFEST_KEY: {"version": 1, "carriers": {"scripts/x.py": {"guards": ["not-a-dict"]}}}},
        123,
        "a string",
        [],
    ]
    for hooks_json in garbage_inputs:
        result = read_hook_delivery_manifest(hooks_json, [])
        assert isinstance(result, HookDeliveryManifest)
        assert result.state in {"ok", "absent", "malformed", "version_unsupported", "stale"}

    result = read_hook_delivery_manifest(_manifest_block(), None)
    assert isinstance(result, HookDeliveryManifest)
    assert result.state in {"ok", "absent", "malformed", "version_unsupported", "stale"}


def test_never_raises_on_declared_script_keys_element_garbage():
    garbage_elements = [
        [{"a": 1}],
        [["nested", "list"]],
        [None],
        [{"a": 1}, "scripts/preuse-write-dispatch.py", None],
    ]
    for declared in garbage_elements:
        result = read_hook_delivery_manifest(_manifest_block(), declared)
        assert isinstance(result, HookDeliveryManifest)
        assert result.state in {"ok", "absent", "malformed", "version_unsupported", "stale"}

    result = read_hook_delivery_manifest(
        _manifest_block(), [{"a": 1}, "scripts/preuse-write-dispatch.py"]
    )
    assert result.state == "ok"


def test_null_effective_delivery_key_is_malformed_not_absent():
    result = read_hook_delivery_manifest({MANIFEST_KEY: None}, [])
    assert result.state == "malformed"


def test_tool_names_parsed_clean_list():
    hooks_json = _manifest_block(
        direct=[
            {
                "id": "some_guard",
                "script": "scripts/some-guard.py",
                "tool_names": ["Bash", "PowerShell"],
            }
        ]
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "ok"
    direct_guard = result.direct[0]
    assert direct_guard.tool_names == ("Bash", "PowerShell")


def test_tool_names_element_violating_string_contract_is_malformed():
    hooks_json = _manifest_block(
        direct=[
            {
                "id": "some_guard",
                "script": "scripts/some-guard.py",
                "tool_names": ["Bash", "Write\n"],
            }
        ]
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "malformed"


def test_tool_names_missing_or_malformed_is_malformed():
    non_list_value = _manifest_block(
        direct=[
            {"id": "some_guard", "script": "scripts/some-guard.py", "tool_names": "not-a-list"}
        ]
    )
    result = read_hook_delivery_manifest(non_list_value, [])
    assert result.state == "malformed"

    missing = _manifest_block(
        direct=[{"id": "some_guard", "script": "scripts/some-guard.py"}]
    )
    result = read_hook_delivery_manifest(missing, [])
    assert result.state == "malformed"

    non_string_element = _manifest_block(
        direct=[
            {
                "id": "some_guard",
                "script": "scripts/some-guard.py",
                "tool_names": ["Bash", 123],
            }
        ]
    )
    result = read_hook_delivery_manifest(non_string_element, [])
    assert result.state == "malformed"


def test_dual_delivered_guard_is_ok_not_malformed():
    hooks_json = _manifest_block(
        direct=[
            {
                "id": "check_claude_md_size",
                "script": "scripts/check-claude-md-size.py",
                "tool_names": ["Bash"],
            },
        ]
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "ok"
    assert result.script_index["scripts/check-claude-md-size.py"] == (
        "check_claude_md_size",
    )


def test_fan_in_module_hosting_many_guards_is_ok_not_malformed():
    hooks_json = _manifest_block(
        carriers={
            "scripts/preuse-write-dispatch.py": {
                "guards": [
                    {
                        "id": f"dispatch_check_{n}",
                        "script": "bash_guards/dispatch_checks.py",
                        "tool_names": ["Bash"],
                    }
                    for n in range(16)
                ]
            }
        }
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "ok"
    assert len(result.script_index["bash_guards/dispatch_checks.py"]) == 16


def test_duplicate_guard_id_within_one_surface_is_malformed():
    hooks_json = _manifest_block(
        direct=[
            {
                "id": "runtime_tripwire_em_check",
                "script": "scripts/runtime-tripwire-em-check.py",
                "tool_names": ["Bash"],
            },
            {
                "id": "runtime_tripwire_em_check",
                "script": "scripts/runtime-tripwire-em-check.py",
                "tool_names": ["Bash"],
            },
        ]
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "malformed"
    assert "runtime_tripwire_em_check" in result.detail
    assert "direct" in result.detail


def test_guard_id_in_both_live_and_retired_is_malformed():
    hooks_json = _manifest_block(
        retired=[
            {
                "id": "check_claude_md_size",
                "script": "scripts/check-claude-md-size.py",
                "reason": "stood down",
            }
        ]
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "malformed"
    assert "check_claude_md_size" in result.detail


def test_retired_guard_sharing_a_fan_in_module_with_live_guards_is_ok():
    hooks_json = _manifest_block(
        carriers={
            "scripts/preuse-write-dispatch.py": {
                "guards": [
                    {
                        "id": "dispatch_check_live",
                        "script": "bash_guards/dispatch_checks.py",
                        "tool_names": ["Bash"],
                    }
                ]
            }
        },
        retired=[
            {
                "id": "dispatch_check_retired",
                "script": "bash_guards/dispatch_checks.py",
                "reason": "stood down by PM ruling",
            }
        ],
    )
    result = read_hook_delivery_manifest(hooks_json, [])

    assert result.state == "ok"


def test_retired_guard_accounts_for_declared_script():
    hooks_json = _manifest_block(
        retired=[
            {
                "id": "runtime_tripwire_stop_watcher",
                "script": "scripts/runtime-tripwire-stop-watcher.py",
                "reason": "stood down by PM ruling",
            }
        ]
    )
    declared = [
        "scripts/preuse-write-dispatch.py",
        "scripts/runtime-tripwire-stop-watcher.py",
    ]

    result = read_hook_delivery_manifest(hooks_json, declared)

    assert result.state == "ok"
    assert result.unaccounted == ()
