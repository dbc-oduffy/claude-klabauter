"""AC1: inherited_scope maps the predecessor's withheld and reviewer-applied paths to their origin."""

from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit.refire_inherited_scope import (
    ORIGIN_COMPLETED_ROW,
    ORIGIN_PREDECESSOR_WITHHELD,
    ORIGIN_REVIEWER_APPLIED,
    inherited_scope,
)

RUN = "ask-20261025T000000-aaaaaa"


def _record(tmp_path: Path) -> Path:
    d = tmp_path / "state" / "warp" / RUN
    d.mkdir(parents=True)
    return d


def _halt(tmp_path: Path, withheld: dict) -> None:
    marker = {"status": "halted", "halted": "ledger-check", "run_artifacts": {"withheld": withheld}}
    (_record(tmp_path) / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


def _review_sidecar(tmp_path: Path, rows: list) -> str:
    rel = "wave-review.md"
    (tmp_path / rel).write_text(
        "---\nfindings_ledger: {verified_at: '2026-10-25T10:00:00Z'}\n---\n"
        "## Findings Ledger\n\n```json\n" + json.dumps(rows) + "\n```\n",
        encoding="utf-8",
    )
    return rel


def _outcome(tmp_path: Path, review: dict) -> None:
    d = tmp_path / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    (d / "run-outcome.json").write_text(json.dumps({"inline_review": review}), encoding="utf-8")


def test_no_halted_predecessor_is_empty(tmp_path):
    assert inherited_scope(tmp_path, {}) == {}
    assert inherited_scope(tmp_path, None) == {}
    assert inherited_scope(tmp_path, {"run_id": "../escape"}) == {}
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {}


def test_withheld_from_halt_marker(tmp_path):
    _halt(tmp_path, {"a/x.py": "content-withheld"})
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {"a/x.py": ORIGIN_PREDECESSOR_WITHHELD}


def test_withheld_from_resume_artifacts_wins_over_marker(tmp_path):
    _halt(tmp_path, {"marker.py": "r"})
    resume = {"run_id": RUN, "run_artifacts": {"withheld": {"carried.py": "r"}}}
    assert inherited_scope(tmp_path, resume) == {"carried.py": ORIGIN_PREDECESSOR_WITHHELD}


def test_reviewer_applied_fix_from_predecessor_record(tmp_path):
    side = _review_sidecar(tmp_path, [{"verified": True, "file": "pkg/fixed.py", "before": "a", "after": "b"}])
    _outcome(tmp_path, {"fixes": 1, "wave_sidecar_paths": [side]})
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {"pkg/fixed.py": ORIGIN_REVIEWER_APPLIED}


def test_unverified_or_rejected_fix_is_not_inherited(tmp_path):
    side = _review_sidecar(tmp_path, [
        {"verified": False, "file": "pkg/a.py", "before": "a", "after": "b"},
        {"verified": True, "status": "rejected", "file": "pkg/b.py", "before": "a", "after": "b"},
    ])
    _outcome(tmp_path, {"fixes": 1, "wave_sidecar_paths": [side]})
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {}


def test_review_with_no_fixes_inherits_nothing(tmp_path):
    side = _review_sidecar(tmp_path, [{"verified": True, "file": "pkg/fixed.py", "before": "a", "after": "b"}])
    _outcome(tmp_path, {"fixes": 0, "wave_sidecar_paths": [side]})
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {}


def test_both_origins_union_and_withheld_wins_a_shared_path(tmp_path):
    _halt(tmp_path, {"pkg/fixed.py": "r", "only/withheld.py": "r"})
    side = _review_sidecar(tmp_path, [
        {"verified": True, "file": "pkg/fixed.py", "before": "a", "after": "b"},
        {"verified": True, "file": "pkg/other.py", "before": "a", "after": "b"},
    ])
    _outcome(tmp_path, {"fixes": 2, "wave_sidecar_paths": [side]})
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {
        "pkg/fixed.py": ORIGIN_PREDECESSOR_WITHHELD,
        "only/withheld.py": ORIGIN_PREDECESSOR_WITHHELD,
        "pkg/other.py": ORIGIN_REVIEWER_APPLIED,
    }


def test_completed_row_writes_inherit_with_lowest_precedence(tmp_path):
    _halt(tmp_path, {"shared/w.py": "r"})
    side = _review_sidecar(tmp_path, [{"verified": True, "file": "shared/r.py", "before": "a", "after": "b"}])
    _outcome(tmp_path, {"fixes": 1, "wave_sidecar_paths": [side]})
    manifest = {"rows": [
        {"id": "C1", "writes": ["only/done.py", "shared/w.py", "shared/r.py"]},
        {"id": "C2", "writes": ["other/done.py"]},
    ]}
    (tmp_path / "state" / "warp" / RUN / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {
        "shared/w.py": ORIGIN_PREDECESSOR_WITHHELD,
        "shared/r.py": ORIGIN_REVIEWER_APPLIED,
        "only/done.py": ORIGIN_COMPLETED_ROW,
        "other/done.py": ORIGIN_COMPLETED_ROW,
    }


def test_malformed_manifest_inherits_nothing(tmp_path):
    d = _record(tmp_path)
    (d / "manifest.json").write_text("{not json", encoding="utf-8")
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {}
    (d / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": "a.py"}, 3]}), encoding="utf-8")
    assert inherited_scope(tmp_path, {"run_id": RUN}) == {}


# land_run refire: the reviewer-applied origin through the real run-outcome reader, and a non-inherited
# footprint breach on the same refire. Neither `inherited_scope` nor `_ledger_gate` is stubbed.
import pytest  # noqa: E402

from coordinator_core.ops.dispatch_emit import land_run  # noqa: E402
from coordinator_core.ops.dispatch_emit.ask_contract import (  # noqa: E402
    ASK_MANIFEST_MARKER,
    DELIVERY_NOTE_DIR,
    RECORD_DIR_ROOT,
    RUN_DIR_ROOT,
    StageManifest,
)
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod  # noqa: E402
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: E402,F401  (fixture)

_OWN = "pkg/model.py"
_FIXED = "pkg/fixed.py"
_BREACH = "pkg/breach.py"
_SIZING = "state/sizings/2026-10-25-inherit.yaml"


@pytest.fixture(autouse=False)
def _live(monkeypatch):
    from coordinator_core.session import touch_record

    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: True)


def _stage_refire(root, extra: tuple = ()) -> None:
    note = f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md"
    for rel in (_OWN, *extra):
        prod._author(root, rel, f"# {rel}\n")
    prod._author(root, note, "## What shipped\n\n" + "".join(f"- `{p}`\n" for p in (_OWN, *extra)))
    marker = f"{RUN_DIR_ROOT}/{prod.RUN}/commit-request.txt"
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="x1", paths=(_OWN, *extra)), ChunkCommit(id="docs", title="docs", paths=(note,))),
        plan_path=None,
    )
    prod._write(root, marker, render_marker(request) + "\n")
    manifest = StageManifest(run_dir=f"{RUN_DIR_ROOT}/{prod.RUN}", rows=(), review_declared_paths=(), marker_path=marker)
    prod._write(root, f"{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json", json.dumps(manifest.to_json()))
    prod._write(root, prod.SCRIPT, f"{ASK_MANIFEST_MARKER}{RECORD_DIR_ROOT}/{prod.RUN}/manifest.json\n")


def test_land_run_refire_lands_a_reviewer_applied_path_through_the_real_reader(repo, _live):  # noqa: F811
    _stage_refire(repo)
    prod._author(repo, _FIXED, "# fixed\n")
    side = _review_sidecar(repo, [{"verified": True, "file": _FIXED, "before": "a", "after": "b"}])
    _outcome(repo, {"fixes": 1, "wave_sidecar_paths": [side]})
    assert land_run.inherited_scope(repo, {"run_id": RUN}) == {_FIXED: ORIGIN_REVIEWER_APPLIED}
    prod._write(repo, f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md", f"## What shipped\n\n- `{_OWN}`\n- `{_FIXED}`\n")

    out = prod._land(repo, resume_ledger={"run_id": RUN, "sizing": _SIZING})

    assert out["committed"] is True, out
    assert _FIXED in prod._tree(repo)


def test_land_run_refire_still_withholds_a_non_inherited_footprint_breach(repo, _live, monkeypatch):  # noqa: F811
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    _stage_refire(repo, (_BREACH,))
    prod._write(
        repo, _SIZING,
        "schema: sizing-object\nintent: Test intent.\nestimate:\n  tshirt: S\n  provisional: true\n"
        "route: spec-dispatch\ndetents: []\nfork: null\nxl_exit: null\nstatus: routed\n"
        "premise:\n  provenance: read\n  evidence: fixture\npm_verbatims:\n- turn: 1\n  text: ship it\n",
    )
    rl.start(repo, prod.RUN, all_of_pm_ask=False)
    rl.bind(repo, prod.RUN, _SIZING)
    monkeypatch.setattr(land_run, "_footprint_fence", lambda *a, **k: {"recorded": True, "breach": [_BREACH]})

    out = prod._land(
        repo, resume_ledger={"run_id": RUN, "sizing": _SIZING},
        ledger_verdict={"items": [{"id": "R1", "status": "met", "wired": True}]},
    )

    # An unbacked shipped claim is provenance, not a refusal: the own file lands, the breach stays out.
    assert out.get("committed") is True and out.get("refused") is None, out
    assert [f["reason"] for f in out["provenance"]] == ["shipped-claim-unbacked"], out
    assert out["footprint_breach"] == [_BREACH], out
    assert out["stranded"] == {"X1": [_BREACH]}, out
    assert _BREACH not in prod._tree(repo)
