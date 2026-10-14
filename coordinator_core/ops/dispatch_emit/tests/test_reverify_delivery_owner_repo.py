"""`--reverify-delivery` briefs the delivery verifier to read a plan's uncleared external_gate rows
at their owner repo's HEAD."""

from __future__ import annotations

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import reverify_delivery as rd
from coordinator_core.ops.tests.test_review_stamp_superseding_record import _git, _record, _repo

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_FAIL = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}
_SPINE = """## Tasks

```yaml plan-tasks
- id: C1
  title: t
  disposition: open
  external_gate:
    - owner_repo: coordinator-content-repo
      condition: delivered in DoE
      requires: commit-in-owner-repo
      closure_key: abc123
    - owner_repo: cleared-repo
      cleared: true
    - owner_repo: ac-repo
      blocks: ac-closure
- id: C2
  title: u
  disposition: open
```
"""


def _emit(tmp_path, monkeypatch, body):
    repo = _repo(tmp_path)
    record = _record(repo, _git(repo, "rev-parse", "HEAD"), delivery=_FAIL)
    plan = repo / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("---\nplan_id: pln-x\nstatus: executing\n---\n" + body, encoding="utf-8")
    monkeypatch.setattr(rd, "_head_sha", lambda root: "b" * 40)
    out = repo / "p.mjs"
    rd.emit_reverify(repo_root=repo, plan_path=str(plan), run_record=str(record), out_path=str(out))
    return out.read_text(encoding="utf-8")


def test_external_gate_row_names_owner_root_and_closure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        machine_resolver, "merged_flat_registry", lambda: {"repos.content_root": "/work/doe"}
    )
    text = _emit(tmp_path, monkeypatch, _SPINE)
    assert "C1: owned by coordinator-content-repo, root /work/doe" in text
    assert "closure_key abc123" in text
    assert "cleared-repo" not in text and "ac-repo" not in text


def test_unregistered_owner_is_not_on_this_machine(tmp_path, monkeypatch):
    monkeypatch.setattr(machine_resolver, "merged_flat_registry", lambda: {})
    text = _emit(tmp_path, monkeypatch, _SPINE)
    assert "C1: owner coordinator-content-repo is not on this machine" in text


def test_plan_without_gates_has_no_owner_clause(tmp_path, monkeypatch):
    monkeypatch.setattr(
        machine_resolver, "merged_flat_registry", lambda: {"repos.content_root": "/work/doe"}
    )
    body = "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n```\n"
    text = _emit(tmp_path, monkeypatch, body)
    assert "Owner-repo rows" not in text


def test_frontmatter_gate_resolved_onto_row(tmp_path):
    source = (
        "---\nexternal_gate:\n  - row: C1\n    owner_repo: coordinator-content-repo\n---\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: t\n  disposition: open\n```\n"
    )
    plan = tmp_path / "p.md"
    plan.write_text(source, encoding="utf-8")
    assert rd._owner_gate_rows(str(plan), None) == [{"id": "C1", "owner_repo": "coordinator-content-repo"}]
