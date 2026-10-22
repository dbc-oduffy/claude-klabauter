"""An all-memo-lifecycle spine stages as a zero-row manifest naming its memo close; other empty spines still refuse."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.ask_stage import AskStageError, manifest_payload, stage

PLAN_REL = "docs/plans/p.md"
MEMO = "state/cross-repo/inbox/2026-10-22-coordinator-content-repo-em-refire-stage-receipt-missing.md"


def _plan(tmp_path: Path, rows: str) -> None:
    (tmp_path / ".git").mkdir(exist_ok=True)
    plan = tmp_path / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        f"---\ntitle: p\nsource_memo: {MEMO}\n---\n\n# p\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n",
        encoding="utf-8",
        newline="\n",
    )


_MEMO_ROW = (
    f"- id: X1\n  title: confirm already fixed and reply\n  change_kind: doc-edit\n  surface: {MEMO}\n"
    f"  writes:\n    - {MEMO}\n"
)
_CODED_ROW = (
    "- id: A1\n  title: tA1\n  change_kind: doc-edit\n  surface: docs/x/A1.md\n"
    "  writes:\n    - docs/x/A1.md\n  disposition: coded\n"
)


def test_memo_only_spine_stages_zero_row_manifest_naming_the_memo_close(tmp_path):
    _plan(tmp_path, _MEMO_ROW)
    manifest = stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
    assert manifest.rows == ()
    assert manifest.memo_close == ({"id": "X1", "memo": MEMO},)
    assert manifest_payload(tmp_path, manifest)["memo_close"] == [{"id": "X1", "memo": MEMO}]


def test_non_memo_empty_spine_still_refuses(tmp_path):
    _plan(tmp_path, _CODED_ROW)
    with pytest.raises(AskStageError, match="the spine derives zero dispatchable rows"):
        stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)


def test_mixed_memo_and_coded_spine_does_not_take_the_memo_close_path(tmp_path):
    _plan(tmp_path, _MEMO_ROW + _CODED_ROW)
    with pytest.raises(AskStageError, match="the spine derives zero dispatchable rows"):
        stage(tmp_path, run_id="r1", plan_rel=PLAN_REL)
