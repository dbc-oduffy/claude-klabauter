"""workflow.revise on an emitted bug-blitz run appends to the run's revisions file."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bug_blitz.contract import blitz_revisions_rel, blitz_script_rel
from coordinator_core.ops.dispatch_emit import pipeline_run
from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.workflow_scaffold import _js_string_literal

STAMP = "20261121T030943Z"


def _emit_blitz(root: Path, stamp: str = STAMP, *, marker: bool = True, escaped: bool = True) -> Path:
    script = root / blitz_script_rel(stamp)
    script.parent.mkdir(parents=True, exist_ok=True)
    preamble = pipeline_run.REVISIONS_MARKER + " Revisions file: " + blitz_revisions_rel(stamp) if marker else "none"
    body = _js_string_literal(preamble) if escaped else repr(preamble)
    script.write_text(f"const PREAMBLE = {body};\n", encoding="utf-8")
    return script


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_blitz_revise_appends_verbatim_to_revisions_file(repo: Path) -> None:
    _emit_blitz(repo)
    outcome: dict = {}
    ledger, _dir, added = rl.revise(repo, STAMP, verbatim="measure, do not bump the RSS bound", sender="em", outcome=outcome)
    assert added is None
    text = (repo / blitz_revisions_rel(STAMP)).read_text(encoding="utf-8")
    assert "measure, do not bump the RSS bound" in text and "from em (verbatim)" in text
    assert outcome["pipeline_revisions"] == blitz_revisions_rel(STAMP)


def test_blitz_revise_note_is_delivered_and_appends_in_order(repo: Path) -> None:
    _emit_blitz(repo)
    rl.revise(repo, STAMP, verbatim="first")
    rl.revise(repo, STAMP, note="second")
    text = (repo / blitz_revisions_rel(STAMP)).read_text(encoding="utf-8")
    assert text.index("first") < text.index("second") and "(note)" in text


def test_blitz_run_without_parent_record_is_still_accepted(repo: Path) -> None:
    _emit_blitz(repo)
    assert not rl._parent_lists_run(repo, STAMP)
    rl.revise(repo, STAMP, verbatim="go")


def test_blitz_revise_op_reply_shape(repo: Path) -> None:
    _emit_blitz(repo)
    reply = rl._handler({"run_id": STAMP, "verbatim": "delete dead tests"}, repo_root=repo)
    assert "error" not in reply, reply
    assert reply["delivered"] is True and reply["revisions"] == blitz_revisions_rel(STAMP)


def test_blitz_revise_refuses_ledger_only_kwargs(repo: Path) -> None:
    _emit_blitz(repo)
    with pytest.raises(rl.LedgerRefused, match="bug-blitz run"):
        rl.revise(repo, STAMP, verbatim="x", build_only=True)


def test_blitz_script_without_marker_is_refused(repo: Path) -> None:
    _emit_blitz(repo, marker=False)
    with pytest.raises(rl.LedgerRefused, match="predates revision delivery"):
        rl.revise(repo, STAMP, verbatim="x")
    assert not (repo / blitz_revisions_rel(STAMP)).exists()


def test_blitz_peer_relay_is_refused(repo: Path) -> None:
    _emit_blitz(repo)
    with pytest.raises(rl.LedgerRefused, match="cannot message peers"):
        rl.revise(repo, STAMP, verbatim="SendMessage the G-EM when done")


def test_unemitted_stamp_still_refused_as_unknown(repo: Path) -> None:
    with pytest.raises(rl.LedgerRefused, match="unknown run"):
        rl.revise(repo, "20261121T999999Z", verbatim="x")


def test_unsafe_run_id_is_not_a_blitz_run(repo: Path) -> None:
    with pytest.raises(rl.LedgerRefused):
        rl.revise(repo, "../x", verbatim="x")


def test_grind_preamble_escaped_marker_passes_reads_revisions() -> None:
    escaped = _js_string_literal(pipeline_run.REVISIONS_MARKER)
    assert "\\'" in escaped and pipeline_run.REVISIONS_MARKER not in escaped
    assert pipeline_run.reads_revisions(escaped)
    assert not pipeline_run.reads_revisions("Read the run\\'s notes")
