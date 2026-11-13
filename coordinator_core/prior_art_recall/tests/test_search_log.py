from __future__ import annotations

import copy

import yaml

from coordinator_core.prior_art_recall.search_log import ALWAYS_ON_CORPORA, validate_sidecar


def make_log(claims: int = 3) -> dict:
    cells = [
        {"claim": c, "corpus": k, "query": f"q{c}", "hits": 1}
        for c in range(1, claims + 1)
        for k in ALWAYS_ON_CORPORA
    ]
    return {
        "plan_claims_total": claims,
        "claims_checked": claims,
        "grouping": "none",
        "cap": "none",
        "cells": cells,
        "improvement_queue": {"method": "query-records --type improvement", "enumerated": 12},
        "code_anchors": {
            "status": "checked",
            "checks": [{"anchor": "pkg/mod.py", "tool": "project_symbol", "result": "found"}],
        },
    }


def render(log: dict) -> str:
    body = yaml.safe_dump({"search_log": log}, sort_keys=False)
    return f"---\nkind: prior-art-check\n---\n## Prior-Art Verification\n\n## Search Log\n\n```yaml\n{body}```\n"


PLAN = "Edit `pkg/mod.py` to do the thing."


def test_complete_log_passes():
    assert validate_sidecar(render(make_log()), PLAN) == []


def test_missing_section_refused():
    assert "no '## Search Log' section" in validate_sidecar("## Prior-Art Verification\n")[0]


def test_missing_cell_names_claim_and_corpus():
    log = make_log()
    log["cells"] = [c for c in log["cells"] if not (c["claim"] == 2 and c["corpus"] == "lessons")]
    errs = validate_sidecar(render(log), PLAN)
    assert any("claim 2 x lessons" in e for e in errs)


def test_absent_corpus_needs_named_reason():
    log = make_log()
    log["cells"][0] = {"claim": 1, "corpus": "project-wikis", "status": "absent", "reason": "n/a"}
    assert any("without a named reason" in e for e in validate_sidecar(render(log), PLAN))
    log["cells"][0]["reason"] = "docs/wiki does not exist in this repo"
    assert validate_sidecar(render(log), PLAN) == []


def test_cell_without_query_refused():
    log = make_log()
    log["cells"][0] = {"claim": 1, "corpus": "project-wikis", "hits": 0}
    assert any("non-empty query" in e for e in validate_sidecar(render(log), PLAN))


def test_unchecked_claims_without_cap_refused():
    log = make_log(14)
    log["claims_checked"] = 8
    assert any("claims_checked=8" in e for e in validate_sidecar(render(log), PLAN))


def test_cap_needs_named_reason_and_exempts_tail():
    log = make_log(5)
    log["plan_claims_total"] = 9
    log["claims_checked"] = 5
    log["cap"] = {"limit": 5, "reason": "x"}
    assert any("capped without a named reason" in e for e in validate_sidecar(render(log), PLAN))
    log["cap"]["reason"] = "remaining four are mechanical path rows, listed in Silent areas"
    assert validate_sidecar(render(log), PLAN) == []


def test_grouping_needs_named_reason():
    log = make_log()
    log["grouping"] = [{"claims": [1, 2], "reason": "same"}]
    assert any("grouped without a named reason" in e for e in validate_sidecar(render(log), PLAN))


def test_queue_must_be_enumerated():
    log = make_log()
    del log["improvement_queue"]
    assert any("improvement_queue block missing" in e for e in validate_sidecar(render(log), PLAN))
    log["improvement_queue"] = {"method": "grep", "enumerated": 0}
    assert any("empty_reason" in e for e in validate_sidecar(render(log), PLAN))


def test_plan_anchor_must_be_rag_checked():
    log = make_log()
    log["code_anchors"]["checks"][0]["anchor"] = "other.py"
    assert any("pkg/mod.py" in e for e in validate_sidecar(render(log), PLAN))


def test_anchor_check_must_use_example_retrieval_repo_tool():
    log = make_log()
    log["code_anchors"]["checks"][0]["tool"] = "grep"
    assert any("project_* tool" in e for e in validate_sidecar(render(log), PLAN))


def test_none_status_refused_when_plan_cites_code():
    log = make_log()
    log["code_anchors"] = {"status": "none", "reason": "the plan is prose only, no code touched"}
    assert any("plan cites code anchors" in e for e in validate_sidecar(render(log), PLAN))
    assert validate_sidecar(render(log), "No code here.") == []


def test_rag_down_needs_reason():
    log = make_log()
    log["code_anchors"] = {"status": "unavailable"}
    assert any("named reason" in e for e in validate_sidecar(render(log), PLAN))
    log["code_anchors"]["reason"] = "example-retrieval-repo ECONNREFUSED on staleness_check, retried once"
    assert validate_sidecar(render(log), PLAN) == []


def test_validator_leaves_input_untouched():
    log = make_log()
    snap = copy.deepcopy(log)
    validate_sidecar(render(log), PLAN)
    assert log == snap
