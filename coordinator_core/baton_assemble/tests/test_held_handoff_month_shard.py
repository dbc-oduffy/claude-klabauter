"""A session-held predecessor that lives under its `state/handoffs/<YYYY-MM>/` shard
self-resolves to the sharded path; the claim ledger records basenames only.

Reproduces example-retrieval-repo-ue-addon's 2026-11-12 refusal: the resolver rebuilt
`state/handoffs/<basename>` and `brief` refused it as missing.
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.baton_assemble as ba
import coordinator_core.session.claims as claims
from coordinator_core.test_baton_assemble import _write_artifact

SESSION = "sess-current-0001"
NAME = "2026-11-11-machine-a-restart-deposit-fix-seal-merge-r.md"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _hold(monkeypatch, *basenames: str) -> None:
    monkeypatch.setattr(
        claims,
        "list_claims_by_session",
        lambda sid, cwd=None: [("handoff-claims", b) for b in basenames],
    )


def test_single_held_claim_resolves_to_its_month_shard(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write_artifact(repo / "state/handoffs/2026-11" / NAME, ["kind: session-handoff"])
    _hold(monkeypatch, NAME)

    primary, additional, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == f"state/handoffs/2026-11/{NAME}"
    assert (repo / f"state/handoffs/2026-11/{NAME}").is_file()
    assert additional == []


def test_held_claim_filed_in_another_month_folder_still_resolves(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write_artifact(repo / "state/handoffs/2026-12" / NAME, ["kind: session-handoff"])
    _hold(monkeypatch, NAME)

    primary, _, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == f"state/handoffs/2026-12/{NAME}"


def test_undated_handoff_in_a_month_folder_resolves(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write_artifact(repo / "state/handoffs/2026-11/machine-a-restart.md", ["kind: session-handoff"])
    _hold(monkeypatch, "machine-a-restart.md")

    primary, _, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == "state/handoffs/2026-11/machine-a-restart.md"


def test_continued_chain_inside_shards_reaches_the_sharded_leaf(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    leaf = "2026-11-12-next.md"
    _write_artifact(
        repo / "state/handoffs/2026-11" / NAME,
        [
            "kind: session-handoff",
            "claimed_at: 2026-11-12T10:00:00Z",
            "claimed_by: test-session",
            "deployment_state: continued",
            f"continued_into: state/handoffs/2026-11/{leaf}",
        ],
    )
    _write_artifact(repo / "state/handoffs/2026-11" / leaf, ["kind: session-handoff"])
    _hold(monkeypatch, NAME)

    primary, _, _ = ba._resolve_held_handoff_for_session(repo, session_id=SESSION)

    assert primary == f"state/handoffs/2026-11/{leaf}"


def test_flat_cite_of_a_sharded_handoff_resolves_instead_of_refusing(tmp_path):
    repo = _repo(tmp_path)
    sharded = _write_artifact(repo / "state/handoffs/2026-11" / NAME, ["kind: session-handoff"])

    resolved = ba._resolve_qualified_path_or_raise(f"state/handoffs/{NAME}", repo, kind="handoff")

    assert resolved.resolve() == sharded.resolve()
