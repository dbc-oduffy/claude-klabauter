"""
coordinator_core.ops.session.tests.test_in_process_writer_claim_path

Evidence test for the in-process claim-path hole: an engine module that
writes a state/ file WITHOUT returning through
``coordinator_core.ipc.dispatch_message`` records no
``session.scope.touch()`` claim, so the file never enters ``my_scope`` and
no wrap ceremony can ever commit it.

``dispatch_message`` is the ONLY place a handler's self-reported write set
becomes a claim (it strips ``_scope_touch_paths`` off the result dict and
replays it through ``session.scope.touch`` — see
``coordinator_core.ipc._record_self_reported_touches``).
``coordinator_core.dispatch.provision.provision_subagent_sidecar`` is called
in-process from the spawn path, returns a bare ``str`` path (not a result
dict), and never crosses that seam — so its ``open(doc_path, "x")`` write is
unattributed.

The file IS seen as dirty by ``compute_scope`` Step 2's mtime fallback, but
Step 4(c) deliberately routes an mtime-only candidate to ``orphans`` rather
than ``my_scope`` (DR-246 / docs/plans/2026-07-31-unclaimed-dirty-file-
adoption.md — "somebody dirtied this file" is not "this session wrote it").
Provenance, not detection, is what is missing: only a real claim can move it.

Was marked ``designed_red``; the failure output was the worklist. The
marker is removed now that ``coordinator_core.dispatch.provision`` records a
touch-claim after each successful write (see
``docs/plans/2026-08-05-in-process-writers-declare-their-writes.md`` chunk
C1) — the assertions below go green WITHOUT widening what any ceremony
commits.

Negative-spec: this test must NOT be "fixed" by adding a
``state/subagent-share`` bucket to ``compute_scope``, by widening the
pre-commit dirty-tree gate, or by asserting on ``orphans`` instead of
``safe_paths`` — every one of those declares a path on a writer's behalf
rather than making the writer claim what it actually wrote.

Spec backlink: coordinator_core/dispatch/provision.py::provision_subagent_sidecar
Seam under test: coordinator_core/ipc.py::_record_self_reported_touches
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.dispatch.provision import provision_subagent_sidecar
from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import core

SIDECAR_ELIGIBLE_TYPE = "coordinator:executor"


def _make_repo(tmp_path: Path) -> Path:
    """Mirrors test_safe_commit_offer._make_repo — check=True on every
    fixture-setup git call so a silent setup failure cannot masquerade as a
    passing test."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    """Mirrors dispatch/tests/test_provision.py::policy_path."""
    path = tmp_path / "subagent-sandbox-policy.yaml"
    path.write_text(
        yaml.safe_dump({"report_sidecar": [SIDECAR_ELIGIBLE_TYPE]}), encoding="utf-8"
    )
    return path


def test_provisioned_subagent_sidecar_is_committable_by_its_own_session(
    tmp_path: Path, policy_path: Path
) -> None:
    # Repo under tmp_path/repo so the policy fixture's own yaml (written to
    # tmp_path) is not an untracked file inside the repo under test.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "sess-in-process-writer"
    core.init(session_id, cwd=str(repo))
    sdir = Path(core.session_dir(session_id, cwd=str(repo)))
    (sdir / "started_at").write_text("2000-01-01T00:00:00Z")

    # The real spawn-time code path — no test double, no monkeypatched writer.
    sidecar_rel = provision_subagent_sidecar(
        {"agent_type": SIDECAR_ELIGIBLE_TYPE, "session_id": session_id},
        str(policy_path),
        str(repo),
    )
    assert sidecar_rel is not None, "fixture failure: sidecar was not provisioned"
    assert (repo / sidecar_rel).is_file(), "fixture failure: sidecar not on disk"

    offer = safe_commit_offer.compute_offer(session_id, cwd=str(repo))

    assert sidecar_rel in offer["safe_paths"], (
        "the session whose spawn wrote this sidecar cannot commit it: "
        f"safe_paths={offer['safe_paths']!r} orphans={offer['orphans']!r} "
        f"excluded={offer['excluded']!r}"
    )


def test_provision_does_not_materialize_a_phantom_session_dir(
    tmp_path: Path, policy_path: Path
) -> None:
    """AC6: no claim call may materialize a session dir that did not
    already exist. Here ``never-spawned-session`` is never ``core.init()``'d
    before the provision call, so ``provision_subagent_sidecar`` must skip
    the claim rather than let ``scope.touch``'s lazy ``core.init()`` create
    one -- the phantom-live-peer hazard ``coordinator_core/ipc.py``'s F1
    comment documents, reachable here via ``provision.main``'s ``--cwd``."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo = _make_repo(repo_root)
    session_id = "never-spawned-session"

    assert core.session_dir(session_id, cwd=str(repo)) == "" or not Path(
        core.session_dir(session_id, cwd=str(repo))
    ).is_dir(), "fixture failure: session dir already exists before provisioning"

    sidecar_rel = provision_subagent_sidecar(
        {"agent_type": SIDECAR_ELIGIBLE_TYPE, "session_id": session_id},
        str(policy_path),
        str(repo),
    )
    assert sidecar_rel is not None, "fixture failure: sidecar was not provisioned"
    assert (repo / sidecar_rel).is_file(), "fixture failure: sidecar not on disk"

    assert not Path(core.session_dir(session_id, cwd=str(repo))).is_dir(), (
        "provisioning a sidecar for a never-initialized session must not "
        "materialize a phantom session dir via scope.touch's lazy core.init()"
    )
