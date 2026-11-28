"""Host review leg halts typed on an empty slice with declared code paths; the host verify leg records measured-rev."""

import json

import pytest

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
    monkeypatch.setattr(headless, "_invoke_source", lambda op, params, *_a: calls.append((op, params)) or {"sha": "a" * 40})
    assert headless._record_measured_rev(
        json.loads((tmp_path / "state/warp" / RUN / "manifest.json").read_text()), tmp_path, RUN, tmp_path / "a", 30,
    ) == ("a" * 40, None)
    assert calls == [("dispatch.measured_rev", {"run_id": RUN, "paths": DECLARED})]


def test_measured_rev_refusal_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.setattr(headless, "_invoke_source", lambda *_a: {"error": "no HEAD", "refused": "measured-rev"})
    assert headless._record_measured_rev({"review_declared_paths": DECLARED}, tmp_path, RUN, tmp_path / "a", 30) == (None, "no HEAD")
    assert headless._record_measured_rev({"review_declared_paths": []}, tmp_path, RUN, tmp_path / "a", 30) == (None, None)


@pytest.mark.parametrize("candidate, later", [(7, 4), (4, 7)])
@pytest.mark.parametrize("stage", ["review", "verify"])
def test_host_verify_runs_tests_at_measured_revision(tmp_path, monkeypatch, candidate, later, stage):
    import subprocess

    def git(*args):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=tmp_path, text=True,
        ).strip()

    git("init", "-q")
    product = tmp_path / "product.py"
    product.write_text("value = 4\n", encoding="utf-8")
    target = "test_public.py"
    (tmp_path / target).write_text("from product import value\ndef test_value(): assert value == 7\n")
    git("add", "product.py", target)
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    product.write_text(f"value = {candidate}\n", encoding="utf-8")
    git("add", "product.py")
    tree = git("write-tree")
    sha = git("commit-tree", tree, "-p", base, "-m", "measured fixture")
    git("reset", "-q", "HEAD", "--", "product.py")
    run_dir = _manifest(tmp_path, review_declared_paths=["product.py"], test_targets=[target], head_sha=base)
    product.write_text(f"value = {later}\n", encoding="utf-8")
    (run_dir / "touched-blobs.json").write_text(json.dumps({
        "product.py": {"base": git("rev-parse", "HEAD:product.py"), "run": git("hash-object", "product.py")},
    }))
    (run_dir / "measured-rev.json").write_text(json.dumps({"sha": sha}))
    inventory = {"inventories": [{"edited": ["product.py"], "owned": [target]}]}
    monkeypatch.setattr(headless, "_invoke_source", lambda op, *_: inventory if op == "review.reverify" else {"sha": sha})
    artifacts = tmp_path / "ops"
    if stage == "review":
        result = headless._reverify_test_leg(
            {"run_key": RUN, "slice_prefixes": ["prep"], "base": base},
            str(tmp_path), artifacts, 60, run_id=RUN,
        )
        status = result["status_before"]
    else:
        result = headless._verify_test_leg(
            {"out_path": f"state/warp/{RUN}/test-leg.json", "targets": [target], "base": base},
            str(tmp_path), artifacts, 60, run_id=RUN,
        )
        status = result["status"]
    assert (status, result["tests_run"]) == ("pass" if candidate == 7 else "fail", 1), result
    argv = json.loads((artifacts / "test-leg-start.json").read_text())["argv"]
    assert "--measured" in argv
    assert json.loads((artifacts / "test-leg-result.json").read_text())["own_tree"]["at"] == sha
    assert argv[argv.index("--run-id") + 1] == RUN
    assert json.loads((artifacts / "test-leg-result.json").read_text())["own_tree"]["overlaid"] == []
    assert git("rev-parse", "HEAD") == base
