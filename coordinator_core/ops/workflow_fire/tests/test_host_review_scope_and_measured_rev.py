"""Host review leg halts typed on an empty slice with declared code paths; the host verify leg records measured-rev."""

import json

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.workflow_fire import headless

RUN = "warp-witness-run"
DECLARED = ["example_retrieval_repo_core/metadata_backfill.py", "tests/test_metadata_backfill_cli.py"]


def _manifest(repo, **fields):
    run_dir = repo / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(fields), encoding="utf-8")
    return run_dir


def _inventory(edited):
    return {"inventories": [{"edited": edited, "created": [], "owned": [], "tripwires": [],
                             "tripwires_not_selected": [], "error": None}], "error": None}


def test_empty_review_slice_with_declared_code_paths_is_typed(tmp_path, monkeypatch):
    _manifest(tmp_path, review_code_paths=DECLARED, review_declared_paths=DECLARED)
    monkeypatch.setattr(headless, "_invoke_source", lambda *_a: _inventory([]))
    result = headless._reverify_test_leg(
        {"base": "a" * 40, "slice_prefixes": ["prep"], "run_key": RUN, "verify_scope_targets": {}},
        str(tmp_path), tmp_path / "ops", 30,
    )
    assert result["review_scope_empty"] is True
    assert result["reverify_error"].startswith("review_scope_empty:")
    assert result["declared_code_paths"] == DECLARED


def test_no_declared_code_paths_keeps_the_prior_receipt(tmp_path, monkeypatch):
    _manifest(tmp_path, review_code_paths=[], review_declared_paths=[])
    monkeypatch.setattr(headless, "_invoke_source", lambda *_a: _inventory([]))
    result = headless._reverify_test_leg(
        {"base": "a" * 40, "slice_prefixes": ["prep"], "run_key": RUN}, str(tmp_path), tmp_path / "ops", 30,
    )
    assert "review_scope_empty" not in result and result["status_before"] == "not_run"


def test_review_scope_empty_halts_the_emitted_workflow():
    js = ask_compose._review_test_infra_halt_js(None)
    assert "review_scope_empty" in js and "_reviewReverify?.review_scope_empty === true" in js


def test_verify_leg_records_measured_rev_over_declared_paths(tmp_path, monkeypatch):
    _manifest(tmp_path, review_declared_paths=DECLARED, test_targets=[])
    calls = []
    monkeypatch.setattr(headless, "_invoke_source", lambda op, params, *_a: calls.append((op, params)) or {"sha": "x"})
    assert headless._record_measured_rev(
        json.loads((tmp_path / "state/warp" / RUN / "manifest.json").read_text()), tmp_path, RUN, tmp_path / "a", 30,
    ) is None
    assert calls == [("dispatch.measured_rev", {"run_id": RUN, "paths": DECLARED})]


def test_measured_rev_refusal_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_invoke_source", lambda *_a: {"error": "no HEAD", "refused": "measured-rev"})
    assert headless._record_measured_rev({"review_declared_paths": DECLARED}, tmp_path, RUN, tmp_path / "a", 30) == "no HEAD"
    assert headless._record_measured_rev({"review_declared_paths": []}, tmp_path, RUN, tmp_path / "a", 30) is None
