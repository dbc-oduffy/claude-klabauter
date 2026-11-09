"""Cold public CLI descriptions expose usable runner request contracts."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import jsonschema
import pytest


@pytest.fixture(scope="module")
def descriptions():
    root = Path(__file__).resolve().parents[3]
    result = {}
    for name in ("codex_runner.start", "codex_runner.result"):
        done = subprocess.run([sys.executable, "-B", "-m", "coordinator_core.invoke", name, "--describe"],
                              cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=10)
        assert done.returncode == 0, done.stderr
        contract = json.loads(done.stdout)
        assert contract["name"] == name
        assert contract["module"] == "coordinator_core.ops.codex_runner"
        jsonschema.Draft202012Validator.check_schema(contract["params_schema"])
        result[name] = contract
    return result


def test_start_cold_describe_covers_all_existing_modes_and_real_wire_fields(descriptions):
    from coordinator_core.codex_runner.contract import MODES
    schema = descriptions["codex_runner.start"]["params_schema"]
    assert schema["properties"]["mode"]["enum"] == list(MODES)
    assert set(schema["properties"]) == {"mode", "repo_root", "artifact", "plan", "chunk",
                                         "parent_session_id", "persona", "task_file", "tier_map", "provider", "assignment"}
    validate = jsonschema.Draft202012Validator(schema).validate
    for mode in ("review", "agy-review"):
        validate({"mode": mode, "repo_root": "/checkout", "artifact": "review.md"})
    validate({"mode": "execute", "repo_root": "/checkout", "plan": "plan.md", "chunk": "C1"})
    grant = {"session_id": "caller", "run": "review", "declared_peak_mb": 128}
    validate({"mode": "persona", "repo_root": "/checkout", "persona": "shared-role.md",
              "task_file": "task.txt", "tier_map": "reviewed-map.json", "provider": "openai", "assignment": grant})
    validate({"mode": "worker", "repo_root": "/checkout", "task_file": "task.txt",
              "assignment": {**grant, "source": "pm", "branch": "codex/assigned", "quiet_box": True,
                             "quiet_max_sessions": 2, "evidence": "PM assignment"}})
    assert "installed Codex model/effort defaults" in descriptions["codex_runner.start"]["description"]


@pytest.mark.parametrize("wire_params", [
    {"mode": "worker", "repo_root": "/checkout", "task_file": "task.txt"},
    {"mode": "persona", "repo_root": "/checkout", "persona": "role.md", "task_file": "task.txt"},
    {"mode": "review", "repo_root": "/checkout"},
    {"mode": "execute", "repo_root": "/checkout", "plan": "plan.md"},
    {"mode": "invented", "repo_root": "/checkout"},
])
def test_described_mode_specific_requirements_reject_missing_contract_fields(descriptions, wire_params):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(wire_params, descriptions["codex_runner.start"]["params_schema"])


def test_result_cold_describe_requires_the_actual_run_identifier(descriptions):
    schema = descriptions["codex_runner.result"]["params_schema"]
    assert schema["required"] == ["run_id"]
    jsonschema.validate({"run_id": "worker-0123456789ab"}, schema)
    for invalid in ({}, {"run_id": "../escape"}, {"run_id": ".."}, {"run_id": ""}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(invalid, schema)
