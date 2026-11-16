"""
coordinator_core.ops.tests.test_cascade_redrive — pins `redrive_terminal_divergences` and `landed_parked`.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_cascade_redrive.py -q
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.cascade_redrive import landed_parked, redrive_terminal_divergences
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import _git, _init_repo, _seed_handoff

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_DID = "dlv-redrive-000000"


@pytest.fixture(autouse=True)
def _caller_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _write_plan(repo: Path, rel: str, status: str, did: str = _DID, shipped_in: str = "", **extra) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"status: {status}", f"deliverable_id: {did}"]
    if shipped_in:
        lines.append(f"shipped_in: {shipped_in}")
    lines += [f"{k}: {v}" for k, v in extra.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n# Plan\n", encoding="utf-8")
    return path


def _commit_all(repo: Path) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"seed\n\nSession-Id: {_SESSION_ID}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _state(path: Path) -> str:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, "deployment_state")


def test_hand_written_implemented_plan_advances_its_baton(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    baton = _seed_handoff(repo, "20260102-baton.md", deliverable_id=_DID)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_plan(repo, "docs/plans/2026-11/hand-written.md", "implemented", shipped_in=head)
    _commit_all(repo)

    result = redrive_terminal_divergences(repo / ".git")

    assert result["errors"] == []
    assert [Path(a["path"]).name for a in result["advanced"]] == ["20260102-baton.md"]
    assert result["advanced"][0]["source"] == "docs/plans/2026-11/hand-written.md"
    assert _state(baton) == "shipped"


def test_deliverable_ids_restriction_leaves_other_batons_open(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    other = _seed_handoff(repo, "20260102-other.md", deliverable_id="dlv-other-000000")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_plan(repo, "docs/plans/other.md", "implemented", did="dlv-other-000000", shipped_in=head)
    _commit_all(repo)

    result = redrive_terminal_divergences(repo / ".git", deliverable_ids={_DID})

    assert result["advanced"] == []
    assert _state(other) == "ready_to_fire"


def test_archived_handoff_source_with_handoff_metas_cites_its_own_shipped_in(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    baton = _seed_handoff(repo, "20260102-baton.md", deliverable_id=_DID)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    archived = repo / "archive" / "handoffs" / "2026-11" / "20260101-source.md"
    archived.parent.mkdir(parents=True)
    archived.write_text(
        "---\ntitle: src\ndeployment_state: shipped\n"
        f"deliverable_id: {_DID}\nshipped_in: {head[:8]}\n---\n\n# src\n",
        encoding="utf-8",
    )
    _commit_all(repo)
    rel = archived.relative_to(repo).as_posix()
    metas = {rel: {"deployment_state": "shipped", "deliverable_id": _DID, "shipped_in": head[:8]}}

    result = redrive_terminal_divergences(repo / ".git", deliverable_ids={_DID}, handoff_metas=metas)

    assert result["errors"] == []
    assert len(result["advanced"]) == 1
    assert result["advanced"][0]["source"] == rel
    split = split_frontmatter(baton.read_text(encoding="utf-8"))
    assert read_fm_field_unquoted(split.fm_text, "shipped_in") == head[:8]


def test_handoff_metas_given_skips_the_archive_wide_handoff_scan(tmp_path, monkeypatch):
    import coordinator_core.ops.cascade_redrive as mod

    repo = tmp_path / "repo"
    _init_repo(repo)

    def _boom(_root):
        raise AssertionError("archive-wide handoff scan must not run when handoff_metas is supplied")

    monkeypatch.setattr(mod, "_terminal_handoff_deliverable_ids", _boom)
    assert redrive_terminal_divergences(repo / ".git", handoff_metas={})["errors"] == []


def test_refused_candidate_is_reported_not_advanced(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    baton = _seed_handoff(repo, "20260102-baton.md", deliverable_id=_DID)
    _write_plan(repo, "docs/plans/no-sha.md", "implemented")
    _commit_all(repo)

    result = redrive_terminal_divergences(repo / ".git")

    assert result["advanced"] == []
    assert _state(baton) == "ready_to_fire"
    assert [Path(r["path"]).name for r in result["refused"]] == ["20260102-baton.md"]
    assert result["commit_shas"] == []


def test_no_divergence_spawns_no_process(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _seed_handoff(repo, "20260102-unrelated.md", deliverable_id="dlv-unrelated-000000")
    _write_plan(repo, "docs/plans/2026-11/done.md", "implemented")
    _commit_all(repo)

    with _count_spawns_attributed(monkeypatch) as spawns:
        result = redrive_terminal_divergences(repo / ".git")

    assert result == {"advanced": [], "refused": [], "commit_shas": [], "scan_incomplete": False, "errors": []}
    assert spawns == []


def _count_collector(monkeypatch) -> list[str]:
    import coordinator_core.ops.cascade_redrive as mod

    calls: list[str] = []
    real = mod._collect_live_candidates_for_kind

    def _counting(worktree_root, did, *args, **kwargs):
        calls.append(did)
        return real(worktree_root, did, *args, **kwargs)

    monkeypatch.setattr(mod, "_collect_live_candidates_for_kind", _counting)
    return calls


def test_collector_runs_once_over_many_ids_and_each_baton_advances(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    dids = [f"dlv-many-{i:06d}" for i in range(4)]
    batons = [_seed_handoff(repo, f"2026010{i}-baton.md", deliverable_id=d) for i, d in enumerate(dids)]
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    for i, d in enumerate(dids):
        _write_plan(repo, f"docs/plans/many-{i}.md", "implemented", did=d, shipped_in=head)
    _commit_all(repo)
    calls = _count_collector(monkeypatch)

    result = redrive_terminal_divergences(repo / ".git")

    assert len(calls) == 1
    assert result["errors"] == []
    assert sorted(Path(a["path"]).name for a in result["advanced"]) == sorted(b.name for b in batons)
    assert all(_state(b) == "shipped" for b in batons)
    assert result["scan_incomplete"] is False


def test_collector_runs_once_over_many_refused_ids(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    dids = [f"dlv-refused-{i:06d}" for i in range(3)]
    batons = [_seed_handoff(repo, f"2026010{i}-baton.md", deliverable_id=d) for i, d in enumerate(dids)]
    for i, d in enumerate(dids):
        _write_plan(repo, f"docs/plans/refused-{i}.md", "implemented", did=d)
    _commit_all(repo)
    calls = _count_collector(monkeypatch)

    result = redrive_terminal_divergences(repo / ".git")

    assert len(calls) == 1
    assert result["advanced"] == []
    assert sorted(Path(r["path"]).name for r in result["refused"]) == sorted(b.name for b in batons)
    assert all(_state(b) == "ready_to_fire" for b in batons)


def test_scan_incomplete_from_the_single_scan_is_reported(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _write_plan(repo, "docs/plans/orphan.md", "implemented")
    _commit_all(repo)

    result = redrive_terminal_divergences(repo / ".git")

    assert result["scan_incomplete"] is True
    assert result["advanced"] == []


def test_a_child_advanced_for_one_id_is_seen_terminal_when_the_next_parent_is_judged(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent = _seed_handoff(repo, "20260101-parent.md", deliverable_id="dlv-b-parent-000000")
    child = _seed_handoff(repo, "20260102-child.md", deliverable_id="dlv-a-child-000000")
    child.write_text(
        child.read_text(encoding="utf-8").replace('predecessor: "none"', 'predecessor: "state/handoffs/20260101-parent.md"'),
        encoding="utf-8",
    )
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_plan(repo, "docs/plans/a.md", "implemented", did="dlv-a-child-000000", shipped_in=head)
    _write_plan(repo, "docs/plans/b.md", "implemented", did="dlv-b-parent-000000", shipped_in=head)
    _commit_all(repo)
    calls = _count_collector(monkeypatch)
    import coordinator_core.ops.cascade_redrive as mod

    seen: list[tuple[str, str]] = []
    real_predicate = mod._predicate_refusal_sync

    def _spy(candidate_path, fm, common_dir, **kwargs):
        child_meta = next(m for k, m in kwargs["corpus_metas"].items() if k.endswith("20260102-child.md"))
        seen.append((Path(candidate_path).name, child_meta["deployment_state"]))
        return real_predicate(candidate_path, fm, common_dir, **kwargs)

    monkeypatch.setattr(mod, "_predicate_refusal_sync", _spy)

    result = redrive_terminal_divergences(repo / ".git")

    assert len(calls) == 1
    assert result["errors"] == []
    assert seen == [("20260102-child.md", "ready_to_fire"), ("20260101-parent.md", "shipped")]
    assert [Path(a["path"]).name for a in result["advanced"]] == ["20260102-child.md", "20260101-parent.md"]
    assert _state(child) == "shipped"
    assert _state(parent) == "shipped"


def test_a_live_child_of_an_unrelated_id_still_refuses_its_parent(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent = _seed_handoff(repo, "20260101-parent.md", deliverable_id="dlv-b-parent-000000")
    child = _seed_handoff(repo, "20260102-child.md", deliverable_id="dlv-a-child-000000")
    child.write_text(
        child.read_text(encoding="utf-8").replace('predecessor: "none"', 'predecessor: "state/handoffs/20260101-parent.md"'),
        encoding="utf-8",
    )
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _write_plan(repo, "docs/plans/b.md", "implemented", did="dlv-b-parent-000000", shipped_in=head)
    _commit_all(repo)

    result = redrive_terminal_divergences(repo / ".git")

    assert result["advanced"] == []
    assert _state(parent) == "ready_to_fire"
    assert _state(child) == "ready_to_fire"


def test_landed_parked_lists_plans_in_month_subdirectories(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plan(repo, "docs/plans/2026-10/old.md", "landed", created="2026-10-01")
    _write_plan(repo, "docs/plans/2026-11/new.md", "landed", did="dlv-new-000000", created="2026-11-09")

    parked = landed_parked(repo, days=14, today=date(2026, 11, 10))

    assert parked == [
        {"plan": "docs/plans/2026-10/old.md", "deliverable_id": _DID, "age_days": 40, "age_basis": "created"}
    ]
