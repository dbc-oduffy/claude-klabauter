"""ask_stage stages a commit-in-owner-repo row owned by a provisioned, remote-free local sibling."""

from __future__ import annotations

import pytest

import coordinator_core.machine_resolver as mr
import coordinator_core.ops.fleet._memo_resolver as memo
import coordinator_core.ops.sibling_repo_provision_contract as contract
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.cross_repo_write_refusal import (
    CrossRepoWriteError,
    check_cross_repo_writes,
    grant_file,
    provisioned_sibling_roots,
)
from coordinator_core.ops.review_mint.execute_review import owner_repo_clause

PLAN_REL = "docs/plans/2026-10-20-provisioned.md"
OWNER = "vaultguide"


def _plan(requires: str = "commit-in-owner-repo", target: str = "pkg/manifest.json") -> str:
    return f"""---
title: fixture
---
# Fixture

## Tasks

```yaml plan-tasks
- id: U1
  title: ungated
  change_kind: code-edit
  surface: pkg/a.py
  writes:
    - pkg/a.py
- id: G1
  title: owned by the sibling
  change_kind: code-edit
  surface: {target}
  writes:
    - {target}
  external_gate:
    - owner_repo: {OWNER}
      requires: {requires}
```
"""


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    home = tmp_path / "home"
    sibling = tmp_path / OWNER
    (home / ".git").mkdir(parents=True)
    (home / "docs" / "plans").mkdir(parents=True)
    (sibling / ".git").mkdir(parents=True)
    (sibling / ".git" / contract.MARKER_NAME).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(memo, "read_receiver_aliases", lambda: {})
    key = memo.receiver_em_to_repo_key(OWNER)
    table = {key: str(sibling)}
    monkeypatch.setattr(mr, "registry_get", lambda k: table.get(k))
    monkeypatch.setattr(contract, "registry_get", lambda k: table.get(k))
    return home, sibling


def _stage(home, plan, **params):
    (home / PLAN_REL).write_text(plan, encoding="utf-8", newline="\n")
    return _handler({"run_id": "r1", "plan_path": PLAN_REL, **params}, repo_root=home)


def test_unapproved_run_stages_the_row_with_no_approval_touchpoint_and_no_grant(world):
    home, sibling = world
    reply = _stage(home, _plan(), session_id="s1")
    assert "error" not in reply
    assert "G1" in [r["id"] for r in reply["rows"]]
    assert reply["gated"] == []
    assert reply["engine_rows"] == [{"id": "G1", "owner_repo": OWNER, "owner_root": sibling.as_posix()}]
    assert not grant_file(home, "s1").exists()
    row = reply["engine_rows"][0]
    clause = owner_repo_clause([row], {row["owner_repo"].lower(): row["owner_root"]})
    assert f"git -C {sibling.as_posix()}" in clause


def test_row_writing_inside_the_target_is_refused_with_the_route_remedy_even_when_approved(world):
    home, _ = world
    reply = _stage(home, _plan(target=f"../{OWNER}/README.md"), cross_repo_approved=True)
    assert reply["error"].endswith(contract.ROUTE_REMEDY)


def test_commit_request_keeps_the_staged_row_writes_under_this_repo(world):
    home, _ = world
    reply = _stage(home, _plan())
    row = next(r for r in reply["rows"] if r["id"] == "G1")
    assert row["writes"] == ["pkg/manifest.json"]


def test_sibling_with_a_remote_is_gated_as_before(world):
    home, sibling = world
    (sibling / ".git" / "config").write_text('[remote "origin"]\n\turl = x\n', encoding="utf-8")
    reply = _stage(home, _plan())
    assert [g["id"] for g in reply["gated"]] == ["G1"]
    assert reply["engine_rows"] == []


def test_landed_work_gate_on_the_same_owner_is_still_gated(world):
    home, _ = world
    reply = _stage(home, _plan("landed-work"))
    assert [g["id"] for g in reply["gated"]] == ["G1"]


def test_unmarked_sibling_is_gated(world):
    home, sibling = world
    (sibling / ".git" / contract.MARKER_NAME).unlink()
    reply = _stage(home, _plan())
    assert [g["id"] for g in reply["gated"]] == ["G1"]


def test_provisioned_sibling_roots_names_only_commit_gates_on_siblings(world):
    home, _ = world
    rows = [{"id": "G1", "external_gate": [{"owner_repo": OWNER, "requires": "commit-in-owner-repo"}]}]
    assert set(provisioned_sibling_roots(rows, home)) == {OWNER}
    mixed = rows + [{"id": "G2", "external_gate": [{"owner_repo": OWNER, "requires": "landed-work"}]}]
    assert set(provisioned_sibling_roots(mixed, home)) == set()
    assert set(provisioned_sibling_roots([], home)) == set()


class _Row:
    id = "C1"
    writes = ["../newrepo/README.md"]
    writes_under = ()


def test_unreachable_write_to_an_absent_sibling_names_the_provision_op(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(CrossRepoWriteError) as exc:
        check_cross_repo_writes([_Row()], home)
    assert "repo.provision_local" in str(exc.value) and "newrepo" in str(exc.value)


def test_unreachable_absolute_write_to_an_absent_sibling_names_the_provision_op(tmp_path):
    home = tmp_path / "home"
    home.mkdir()

    class _Abs:
        id = "C1"
        writes = [(tmp_path / "newrepo" / "README.md").as_posix()]
        writes_under = ()

    with pytest.raises(CrossRepoWriteError) as exc:
        check_cross_repo_writes([_Abs()], home)
    assert "repo.provision_local" in str(exc.value)


def test_unreachable_write_to_an_existing_plain_dir_gets_no_alternative(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "newrepo").mkdir()
    with pytest.raises(CrossRepoWriteError) as exc:
        check_cross_repo_writes([_Row()], home)
    assert "repo.provision_local" not in str(exc.value)
