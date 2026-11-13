"""verify-coverage exempts point-in-time records by class: implemented or old plans,
state/roadmap and state/review-findings; a live doc quoting the same ref still halts."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from coordinator_core.ops.verify_coverage import is_historical_plan, main

QUOTE = "The retired agent `coordinator:gone-agent` was removed.\n"


def _write(path, content):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        fh.write(content)


def _plan(status, created):
    return f"---\ntitle: x\nstatus: {status}\ncreated: {created}\n---\n\n{QUOTE}"


def _sweep(tmp_path, files, capsys):
    root = tmp_path / "plugin-root"
    os.makedirs(root / "coordinator" / ".claude-plugin", exist_ok=True)
    _write(str(root / "coordinator" / ".claude-plugin" / "plugin.json"), "{}\n")
    sweep = tmp_path / "sweep"
    for rel, content in files.items():
        _write(sweep / rel, content)
    code = main(["--root", str(root), "--sweep-root", str(sweep), "--json"])
    out = json.loads(capsys.readouterr().out)
    return code, sorted(v["file"] for v in out["violations"])


def test_historical_classes_exempt_and_live_doc_still_halts(tmp_path, capsys):
    recent = date.today().isoformat()
    old = (date.today() - timedelta(days=200)).isoformat()
    code, flagged = _sweep(tmp_path, {
        "docs/plans/implemented.md": _plan("implemented", recent),
        "docs/plans/old-draft.md": _plan("draft", old),
        "state/roadmap/x/record.md": QUOTE,
        "state/review-findings/run/chunk.md": QUOTE,
        "docs/live.md": QUOTE,
    }, capsys)
    assert code == 1
    assert flagged == [os.path.join("docs", "live.md")]


def test_delivery_records_and_archived_deferred_plans_exempt(tmp_path, capsys):
    recent = date.today().isoformat()
    code, flagged = _sweep(tmp_path, {
        "docs/delivery/ask-1.md": QUOTE,
        "docs/delivery/sub/warp-2.md": QUOTE,
        "docs/plans/archived.md": _plan("archived", recent),
        "docs/plans/deferred.md": _plan("deferred", recent),
        "docs/plans/live.md": _plan("executing", recent),
        "docs/live.md": QUOTE,
    }, capsys)
    assert code == 1
    assert flagged == sorted([
        os.path.join("docs", "live.md"),
        os.path.join("docs", "plans", "live.md"),
    ])


def test_queue_grind_evidence_exempt_but_other_evidence_dirs_halt(tmp_path, capsys):
    code, flagged = _sweep(tmp_path, {
        "state/queue-grind/bug/evidence/20261111T004924Z/a.verify.md": QUOTE,
        "docs/evidence/live.md": QUOTE,
    }, capsys)
    assert code == 1
    assert flagged == [os.path.join("docs", "evidence", "live.md")]


def test_recent_unfinished_plan_still_halts(tmp_path, capsys):
    code, flagged = _sweep(tmp_path, {
        "docs/plans/live.md": _plan("executing", date.today().isoformat()),
    }, capsys)
    assert code == 1
    assert flagged == [os.path.join("docs", "plans", "live.md")]


def test_plan_review_sidecar_inherits_parent_verdict(tmp_path, capsys):
    old = (date.today() - timedelta(days=200)).isoformat()
    recent = date.today().isoformat()
    code, flagged = _sweep(tmp_path, {
        "docs/plans/old.md": _plan("deferred", old),
        "docs/plans/old.md.sid-review.md": QUOTE,
        "docs/plans/live.md": _plan("executing", recent),
        "docs/plans/live.md.sid-review.md": QUOTE,
        "docs/plans/orphan.md.sid-review.md": QUOTE,
        "docs/live.md": QUOTE,
    }, capsys)
    assert code == 1
    assert flagged == sorted([
        os.path.join("docs", "live.md"),
        os.path.join("docs", "plans", "live.md"),
        os.path.join("docs", "plans", "live.md.sid-review.md"),
        os.path.join("docs", "plans", "orphan.md.sid-review.md"),
    ])


def test_plan_predicate_boundaries(tmp_path):
    sweep = str(tmp_path)
    f = os.path.join(sweep, "docs", "plans", "p.md")
    today = date(2026, 11, 13)
    assert is_historical_plan(f, sweep, _plan("landed", "2026-11-12"), today)
    assert is_historical_plan(f, sweep, _plan("draft", "2026-07-03"), today)
    assert not is_historical_plan(f, sweep, _plan("draft", "2026-10-01"), today)
    assert not is_historical_plan(f, sweep, QUOTE, today)
    assert not is_historical_plan(
        os.path.join(sweep, "docs", "other", "p.md"), sweep, _plan("implemented", "2026-11-12"), today
    )
