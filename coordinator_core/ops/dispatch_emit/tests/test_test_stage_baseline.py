"""The terminal test stage's baseline attribution: the brief block, the schema, the fail set.

DoE's test-runner buckets its failures against an export of `run_base_sha`; the stage fails on
`caused` + `unverified` only, and a heuristic baseline never clears a red run.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import jsonschema
import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import wake_digest as wd
from coordinator_core.win_portability import no_console_creationflags

SHA = "a" * 40


def test_a_full_run_base_sha_puts_baseline_context_in_the_brief():
    call = emit._test_agent_call_expr(["a/tests/test_one.py"], review_edits_base=SHA)
    assert "BASELINE_CONTEXT" in call and f'"run_base_sha": "{SHA}"' in call


@pytest.mark.parametrize("base", [None, "HEAD"])
def test_no_sha_means_no_baseline_context(base):
    assert "BASELINE_CONTEXT" not in emit._test_agent_call_expr(["a/tests/test_one.py"], review_edits_base=base)


def test_the_test_result_schema_accepts_the_baseline_fields():
    schema = json.loads(wd.stage_schema_literal("test_result"))
    jsonschema.validate(
        {
            "status": "fail", "tests_run": 3, "tests_failed": 1, "sidecar_path": "s.md",
            "baseline": {"pre_existing": ["t::a"], "caused": [], "unverified": [{"id": "t::b", "why": "absent at base"}]},
            "baseline_method": "export",
        },
        schema,
    )


def _status(result: dict) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable to evaluate the status expression")
    expr = wd._tests_status_expr("_t", "_v", "not_run")
    js = f"const _t = {json.dumps(result)}; const _v = []; console.log({expr});"
    out = subprocess.run([node, "-e", js], capture_output=True, text=True, timeout=30, **no_console_creationflags())
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _red(baseline, method="export", failed=1):
    return {"status": "fail", "tests_run": 3, "tests_failed": failed, "sidecar_path": "s.md",
            "baseline": baseline, "baseline_method": method}


@pytest.mark.spawns_process
@pytest.mark.parametrize(
    "result, expected",
    [
        (_red({"pre_existing": ["t::a"], "caused": [], "unverified": []}), "pass"),
        (_red({"pre_existing": ["t::a"], "caused": ["t::b"], "unverified": []}, failed=2), "fail"),
        (_red({"pre_existing": ["t::a"], "caused": [], "unverified": [{"id": "t::b", "why": "x"}]}, failed=2), "fail"),
        (_red({"pre_existing": ["t::a"], "caused": [], "unverified": []}, method="heuristic"), "fail"),
        (_red({"pre_existing": ["t::a"], "caused": [], "unverified": []}, failed=2), "fail"),
        (_red(None), "fail"),
    ],
)
def test_the_stage_fails_on_caused_and_unverified_only(result, expected):
    assert _status(result) == expected


def test_the_digest_relays_every_register_row_key_the_judge_returns():
    from coordinator_core.ops.review_mint.execute_review import _widen_judge_schema

    judge = json.loads(_widen_judge_schema(json.dumps({"type": "object", "properties": {}})))
    judge_keys = set(judge["properties"]["register_rows"]["items"]["properties"])
    def rows(node):
        if isinstance(node, dict):
            if "register_rows" in (node.get("properties") or {}):
                yield node["properties"]["register_rows"]["items"]["properties"]
            for v in node.values():
                yield from rows(v)
        elif isinstance(node, list):
            for v in node:
                yield from rows(v)

    found = list(rows(wd.load_schema()))
    assert found and all(set(r) == judge_keys for r in found)
