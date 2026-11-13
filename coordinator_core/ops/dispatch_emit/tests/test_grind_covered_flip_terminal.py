"""A node where a `dedupe` or `discard` verdict enters never tells its verifier to fail a covered row, and the
Python and emitted-script twins agree on which nodes those are. The end-to-end replays live in
test_grind_covered_replay.py."""
import re

import yaml

from coordinator_core.ops.dispatch_emit import grind_compose as gc
from coordinator_core.ops.dispatch_emit import grind_profile as gp
from coordinator_core.ops.dispatch_emit import grind_stages as stages

from .test_grind_verify_mode import _FIXTURE_PROFILE_DIR, _script

_FAIL_ON_COVERED = "report fail and set `covered_by`"

_ROUTING = {
    "triage": {
        "kind": "triage",
        "edges": {"discard": "verify-retire", "dedupe": "verify-retire", "bug": "verify-fix"},
        "on_fail": "needs-judgment",
    },
    "verify-retire": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": "verify-failed"},
    "verify-fix": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": "verify-failed"},
    "fix": {"kind": "fix", "edges": {"done": "verify"}, "on_fail": "baton"},
    "verify": {"kind": "verify", "edges": {"pass": "commit"}, "on_fail": "verify-failed"},
    "commit": {"kind": "commit", "edges": {}, "on_fail": "commit-failed"},
}

_KW = dict(label="v", phase_title="Grind", routing=True, profile="lessons", digest_js="row.digest", run_id_js="RUN_ID")


def test_a_retire_routing_verify_prompt_never_says_to_fail_a_covered_row():
    retire = stages.compose_verify_agent_call(retire=True, **_KW)
    assert _FAIL_ON_COVERED not in retire
    assert "does the artifact the triage evidence cites exist at HEAD" in retire
    assert "--verdict dedupe" not in retire


def test_a_fix_bound_routing_verify_prompt_still_says_to_fail_a_covered_row():
    fix_bound = stages.compose_verify_agent_call(**_KW)
    assert _FAIL_ON_COVERED in fix_bound
    assert "--verdict dedupe" in fix_bound


def test_the_post_fix_verify_prompt_still_says_to_fail_a_covered_row():
    post_fix = stages.compose_verify_agent_call(label="v", phase_title="Grind", profile="lessons")
    assert _FAIL_ON_COVERED in post_fix


def test_a_retire_node_is_one_a_dedupe_or_discard_verdict_enters_and_no_fix_follows():
    assert gc.is_covered_retire_node(_ROUTING, "triage", "verify-retire")
    assert not gc.is_covered_retire_node(_ROUTING, "triage", "verify-fix")
    assert not gc.is_covered_retire_node(_ROUTING, "triage", "fix")
    assert not gc.is_covered_retire_node(_ROUTING, "triage", None)


def test_a_discard_node_that_re_enters_fix_is_not_a_retire_node():
    routing = {**_ROUTING, "verify-retire": {"kind": "verify", "edges": {"pass": "fix"}, "on_fail": "verify-failed"}}
    assert not gc.is_covered_retire_node(routing, "triage", "verify-retire")


def _profile_with_retire_node(tmp_path):
    doc = yaml.safe_load((_FIXTURE_PROFILE_DIR / "fixture.yaml").read_text(encoding="utf-8"))
    g = doc["graph"]
    doc["verdicts"].append("discard")
    g["triage"]["edges"]["discard"] = "verify-retire"
    g["verify-retire"] = {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "close_retire"}}
    g["close_retire"] = {"kind": "refute-close", "edges": {"confirmed": "commit", "refuted": "needs-judgment"}}
    g["verify-fix"] = {"kind": "verify", "verify": {"default": "agent"}, "edges": {"pass": "fix"}}
    doc["verdicts"].append("fix-bound")
    g["triage"]["edges"]["fix-bound"] = "verify-fix"
    (tmp_path / "fixture.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    profile = gp.load_profile("fixture", tmp_path)
    gp.validate_graph(profile)
    return profile


def test_the_emitted_script_lists_the_retire_nodes_the_python_engine_names(tmp_path):
    profile = _profile_with_retire_node(tmp_path)
    table = gc.build_routing_table(profile)
    expected = sorted(n for n in gc.pre_fix_verify_nodes(table) if gc.is_covered_retire_node(table, "triage", n))
    assert expected == ["verify-retire"]
    script = _script(profile)
    assert 'const VERIFY_RETIRE = ["verify-retire"];' in script
    assert "function _isCoveredRetire(" in script


def test_the_emitted_script_carries_the_fail_on_covered_clause_only_outside_the_retire_branch(tmp_path):
    script = _script(_profile_with_retire_node(tmp_path))
    start = script.index("if (VERIFY_RETIRE.includes(row.node)) {")
    retire_end = script.index("\n    }\n", start)
    assert _FAIL_ON_COVERED not in script[start:retire_end]
    assert _FAIL_ON_COVERED in script[retire_end:]


def test_a_script_with_no_retire_node_emits_no_retire_branch(tmp_path):
    script = _script(gp.load_profile("fixture", _FIXTURE_PROFILE_DIR))
    assert "const VERIFY_RETIRE = [];" in script
    assert "VERIFY_RETIRE.includes" not in script
    assert re.search(r"const VERIFY_RETIRE = \[\];", script)
