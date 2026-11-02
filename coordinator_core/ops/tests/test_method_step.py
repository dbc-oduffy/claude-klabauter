"""method_step: row marker, trace parser, and trace_refusal on both sides of each decision."""
from __future__ import annotations

from coordinator_core.ops import method_step as ms

SOURCE = "map the scanned landmarks into MetaHuman expected-value space"
ROW = {"id": "R1.b", "source_text": SOURCE, "anchor": ms.mark_anchor("relayed", "likeness", "b")}
GOOD_MECH = "map scan landmarks into MetaHuman expected-value space via ExpectedValueMapper.map"


def _fence(body: str) -> str:
    return "intro\n\n```yaml method-trace\n" + body + "```\n\ntail\n"


def _plan(mechanism=GOOD_MECH, at="{path: src/x.py, contains: \"def map(\"}", extra="", row="R1.b"):
    body = f"row: {row}\nmechanism: \"{mechanism}\"\n"
    if at:
        body += f"at: {at}\n"
    return _fence(body + extra)


def _tree(tmp_path, text="def map(x):\n    return x\n"):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text(text, encoding="utf-8")
    return tmp_path


def test_anchor_round_trip():
    anchor = ms.mark_anchor("relayed", "likeness", "b")
    assert anchor == "relayed#method-step:likeness/b"
    assert ms.split_anchor(anchor) == ("relayed", "likeness/b")
    assert ms.split_anchor("pm_verbatims turn 3") == ("pm_verbatims turn 3", None)
    assert ms.split_anchor(None) == ("", None)


def test_is_method_step_both_sides():
    assert ms.is_method_step(ROW)
    assert not ms.is_method_step({"id": "R2", "anchor": "relayed"})


def test_parse_traces_keys_by_row_and_skips_malformed():
    text = _plan() + "\n```yaml method-trace\nrow: [unclosed\n```\n\n```yaml method-trace\nmechanism: x\n```\n"
    traces = ms.parse_traces(text)
    assert list(traces) == ["R1.b"]
    assert traces["R1.b"]["at"]["path"] == "src/x.py"


def test_non_step_row_never_refused(tmp_path):
    assert ms.trace_refusal({"id": "R2", "anchor": "relayed"}, "", tmp_path) is None


def test_matching_trace_passes(tmp_path):
    assert ms.trace_refusal(ROW, _plan(), _tree(tmp_path)) is None


def test_missing_trace_refused(tmp_path):
    reason = ms.trace_refusal(ROW, "no fence here", _tree(tmp_path))
    assert reason and "R1.b" in reason and "method-trace" in reason


def test_trace_for_other_row_is_missing(tmp_path):
    assert ms.trace_refusal(ROW, _plan(row="R9"), _tree(tmp_path))


def test_name_only_mechanism_refused(tmp_path):
    reason = ms.trace_refusal(ROW, _plan(mechanism="likeness step b"), _tree(tmp_path))
    assert reason and "names the step" in reason


def test_unmatched_mechanism_refused_without_deviation(tmp_path):
    reason = ms.trace_refusal(ROW, _plan(mechanism="conform the MetaHuman mesh to the scan"), _tree(tmp_path))
    assert reason and "under half" in reason


def test_unmatched_mechanism_with_ratified_deviation_passes(tmp_path):
    dev = "deviation: {delivers_by: mesh conform, reason: no mapper, ratified_by: apm-ruling-1}\n"
    plan = _plan(mechanism="conform the MetaHuman mesh to the scan", extra=dev)
    assert ms.trace_refusal(ROW, plan, _tree(tmp_path)) is None


def test_deviation_without_ratified_by_refused(tmp_path):
    dev = "deviation: {delivers_by: mesh conform, reason: no mapper}\n"
    plan = _plan(mechanism="conform the MetaHuman mesh to the scan", extra=dev)
    reason = ms.trace_refusal(ROW, plan, _tree(tmp_path))
    assert reason and "ratified_by" in reason


def test_deviation_does_not_excuse_name_only(tmp_path):
    dev = "deviation: {delivers_by: x, reason: y, ratified_by: z}\n"
    assert ms.trace_refusal(ROW, _plan(mechanism="likeness b", extra=dev), _tree(tmp_path))


def test_probe_literal_absent_refused(tmp_path):
    reason = ms.trace_refusal(ROW, _plan(), _tree(tmp_path, "def other():\n    pass\n"))
    assert reason and "absent" in reason


def test_probe_missing_at_refused(tmp_path):
    reason = ms.trace_refusal(ROW, _plan(at=""), _tree(tmp_path))
    assert reason and "at:" in reason


def test_probe_missing_file_refused(tmp_path):
    assert ms.trace_refusal(ROW, _plan(at="{path: src/none.py, contains: x}"), _tree(tmp_path))


def test_probe_path_escape_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "outside.py").write_text("def map(", encoding="utf-8")
    reason = ms.trace_refusal(ROW, _plan(at="{path: ../outside.py, contains: \"def map(\"}"), repo)
    assert reason and "escapes" in reason
