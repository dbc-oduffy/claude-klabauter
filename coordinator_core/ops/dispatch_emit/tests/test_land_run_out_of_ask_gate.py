"""dispatch.land_run commits a path carrying an unresolved structured out-of-ask finding and names the finding in the reply provenance, with or without the halted-hold release."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/execute_review.py"
SIZING = "state/sizings/s.yaml"
FINDING = "failing_detail changes in execute_review.py belong to another run"


def _review(root, status: str | None = None) -> None:
    share = root / ".coordinator-local" / "subagent-share" / prod.SELF
    share.mkdir(parents=True, exist_ok=True)
    (share / f"{prod.RUN}.review-wave-bookkeeping.md").write_text(
        "---\nintegrated_from: [\"rev-1\"]\n---\n\nbody\n", encoding="utf-8"
    )
    row = {"id": "finding-1", "file": PATH, "summary": FINDING, "out_of_ask": True}
    if status:
        row["status"] = status
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\n"
        f"Out-of-ask hunks.\n\n## Findings Ledger\n\n```json\n{json.dumps([row])}\n```\n",
        encoding="utf-8",
    )


def _halted_sibling_on_the_landing_plan(root) -> str:
    run_id = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - 60))}-aaaaaa"
    prod._write(root, SIZING, 'status: open\ndeliverable_id: d1\nplan: "docs/plans/b.md"\n')
    prod._write(
        root, "docs/plans/b.md",
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + PATH + "\n```\n",
    )
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}), encoding="utf-8"
    )
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "sizing_rel": SIZING, "plan": prod.PLAN}), encoding="utf-8"
    )
    return run_id


def _land_dirty(root) -> tuple:
    prod._author(root, PATH, "x = 1\n")
    prod._declare(root, PATH)
    prod._record_dir(root)
    head = prod._git(["rev-parse", "HEAD"], root).strip()
    return prod._land(root), head


def _commits_naming_the_finding(out, head, repo) -> None:
    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert prod._git(["rev-parse", "HEAD"], repo).strip() != head
    prov = out.get("provenance") or []
    assert any(e.get("reason") == peer_hunk_gate.OUT_OF_ASK_HUNKS and "rev-1" in str(e) for e in prov), out


def test_unresolved_marked_row_on_the_landing_path_lands_with_out_of_ask_provenance(repo):
    _review(repo)

    out, head = _land_dirty(repo)

    _commits_naming_the_finding(out, head, repo)


def test_halted_hold_release_still_lands_with_the_unresolved_finding_in_provenance(repo):
    _review(repo)
    _halted_sibling_on_the_landing_plan(repo)

    out, head = _land_dirty(repo)

    _commits_naming_the_finding(out, head, repo)


def test_the_same_row_marked_applied_lands(repo):
    _review(repo, status="applied")

    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
