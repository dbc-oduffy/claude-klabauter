"""caused_only and the version-token rule reach the production gates: the land reverify verdict, classify_reds."""
import json

from coordinator_core.ops.dispatch_emit import landing_integrity as li
from coordinator_core.ops.dispatch_emit.self_correction import classify_reds, finding_files

_HEAD = "a" * 40


def _repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text(_HEAD + "\n", encoding="utf-8")
    (tmp_path / "t.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def _write_fail(root, caused):
    path = li.fresh_verdict_path(root, "r1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "run_id": "r1", "head": _HEAD, "tests": ["t.py"], "covered": li.landing_fingerprint(root, ["t.py"]),
        "status": "fail", "tests_failed": len(caused), "baseline": {"caused": caused},
    }), encoding="utf-8")


def test_reverify_fail_whose_only_id_is_base_red_is_reported_not_refused(tmp_path):
    root = _repo(tmp_path)
    _write_fail(root, ["t.py::a"])
    findings, check = li.landing_test_gate(root, {"failing_at_base": ["t.py::a"]}, "r1")
    assert check == "pre-existing-only"
    assert [f for f in findings] == []
    assert findings.foreign_evidence == {"t.py::a": [li.FAILING_AT_BASE]}


def test_reverify_fail_with_a_caused_id_outside_base_red_still_refuses(tmp_path):
    root = _repo(tmp_path)
    _write_fail(root, ["t.py::a", "t.py::b"])
    findings, check = li.landing_test_gate(root, {"failing_at_base": ["t.py::a"]}, "r1")
    assert check == "failed" and findings
    assert "t.py::b" in findings[0].detail


def test_reverify_fail_with_no_base_evidence_is_not_moved_to_reported(tmp_path):
    root = _repo(tmp_path)
    _write_fail(root, ["t.py::b"])
    findings, check = li.landing_test_gate(root, {}, "r1")
    assert check == "failed" and findings and findings[0].reason == li.TARGETED_TESTS_FAILED


def test_classify_reds_base_red_node_is_foreign_whatever_its_message_names():
    node = "coordinator/tests/test_x.py::test_a"
    detail = {node: "own/file.py:12 assertion"}
    split = classify_reds([node], ["own/file.py"], pre_existing=[node], detail=detail)
    assert split.foreign == (node,) and split.owned == ()
    split = classify_reds([node], ["own/file.py"], detail=detail)
    assert split.owned == (node,)


def test_finding_files_rejects_version_like_tokens_and_keeps_real_files():
    assert finding_files("python3.11:2") == ()
    assert finding_files("v1.2.3:4") == ()
    assert finding_files("1.2.3:4") == ()
    assert finding_files("saw 3.11.rc1:2 here") == ()
    assert finding_files("coordinator/bin/unimatrix-peer-router.py:1304") == (
        "coordinator/bin/unimatrix-peer-router.py",
    )
