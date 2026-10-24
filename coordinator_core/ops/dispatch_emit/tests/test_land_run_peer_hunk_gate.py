"""dispatch.land_run refuses a declared path a live peer run also declares, and a path with an unresolved out-of-ask finding."""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops.dispatch_emit import peer_hunk_gate
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/execute_review.py"
SIBLING = f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-7cc61e"


def _sibling_via_plan_spine(root) -> None:
    rdir = root / "state" / "warp" / SIBLING
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")
    prod._write(root, "state/sizings/b.yaml", 'plan: "docs/plans/b.md"\n')
    prod._write(
        root, "docs/plans/b.md",
        "# B\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + PATH + "\n```\n",
    )


def _land_dirty(root) -> tuple:
    prod._author(root, PATH, "x = 1\n")
    prod._declare(root, PATH)
    prod._record_dir(root)
    head = prod._git(["rev-parse", "HEAD"], root).strip()
    return prod._land(root), head


def test_path_a_halted_sibling_declares_only_in_its_plan_spine_refuses_naming_it(repo):
    _sibling_via_plan_spine(repo)

    out, head = _land_dirty(repo)

    assert out.get("refused") == peer_hunk_gate.PEER_HUNKS_CO_DECLARED, out
    assert SIBLING in str(out), out
    assert out.get("committed") is not True
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head
    assert (repo / PATH).read_text(encoding="utf-8") == "x = 1\n"


def test_same_path_with_the_sibling_gone_lands(repo):
    out, _head = _land_dirty(repo)

    assert out["committed"] is True, out
    assert PATH in prod._tree(repo)


def test_unresolved_out_of_ask_finding_on_the_path_refuses(repo):
    share = repo / ".coordinator-local" / "subagent-share" / prod.SELF
    share.mkdir(parents=True)
    (share / f"{prod.RUN}.review-wave-bookkeeping.md").write_text(
        "---\nintegrated_from: [\"rev-1\"]\n---\n\nbody\n", encoding="utf-8"
    )
    (share / "rev-1.md").write_text(
        "---\nagent_type: reviewer\n---\n\n## Findings\n\n### Finding 1\n\n"
        "Out-of-ask hunks: failing_detail changes in execute_review.py belong to another run.\n",
        encoding="utf-8",
    )

    out, head = _land_dirty(repo)

    assert out.get("refused") == peer_hunk_gate.OUT_OF_ASK_HUNKS, out
    assert "rev-1" in str(out), out
    assert out.get("committed") is not True
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head
