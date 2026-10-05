"""
coordinator_core.ops.tests.test_cascade_terminal_op — the v2 `deliverable.cascade_terminal`.

Purpose: pins the rebuilt op's contract (both kinds advanced in one commit, refusal legs,
required params, the `ship_sha` requirement) and its measurement: the handler at one handoff
plus one sizing advanced over a 305-handoff corpus costs under 200ms process time and 0 spawns
on each of 5 runs. Also pins leg (b)'s raw-text prefilter against the unprefiltered
`has_live_children_from_metas`.

Run (from repo root):
    python3 -m pytest coordinator_core/ops/tests/test_cascade_terminal_op.py -q
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest import mock

import pytest
import yaml

import coordinator_core.claim_state as claim_state_mod
import coordinator_core.ipc as ipc_mod
import coordinator_core.ops as ops_pkg
import coordinator_core.ops.cascade_terminal_op as op_mod
import coordinator_core.ops.handoff_children as hc_mod
from coordinator_core.benchmarks.spawn_counter import _count_spawns_attributed
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.tests.test_deliverable_cascade_kinds import (
    _git,
    _init_repo,
    _seed_handoff,
    _seed_sizing,
)

# The fixture repo is built and reset with real git; the op under test spawns nothing.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SESSION_ID = "11111111-1111-1111-1111-111111111111"
_DID = "dlv-v2-000000"
_PROCESS_BAR_S = 0.200


@pytest.fixture(autouse=True)
def _caller_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _run(params: dict, repo: Path) -> dict:
    return asyncio.run(op_mod._handler(params, repo_root=repo / ".git"))


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"{message}\n\nSession-Id: {_SESSION_ID}")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _fm(path: Path, key: str):
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    assert split is not None
    return read_fm_field_unquoted(split.fm_text, key)


def _pad_handoffs(repo: Path, n: int) -> None:
    for i in range(n):
        _seed_handoff(repo, f"20260101-pad-{i:04d}.md", deliverable_id=f"dlv-pad-{i:04d}")


def _seed_run(tmp_path: Path, name: str = "repo", pad: int = 304) -> tuple[Path, Path, Path, str]:
    """Repo with `pad` unrelated handoffs, one candidate handoff and one candidate sizing
    joined by `_DID`, all committed. Returns (repo, handoff, sizing, base_sha)."""
    repo = tmp_path / name
    _init_repo(repo)
    _pad_handoffs(repo, pad)
    handoff = _seed_handoff(repo, "20260102-target.md", deliverable_id=_DID)
    sizing = _seed_sizing(repo, "20260102-target.yaml", status="routed", deliverable_id=_DID)
    base = _commit_all(repo, "seed corpus")
    return repo, handoff, sizing, base


def _params(base: str, **overrides) -> dict:
    params = {
        "deliverable_id": _DID,
        "source_kind": "plan",
        "source_path": "docs/plans/dummy.md",
        "ship_sha": base,
    }
    params.update(overrides)
    return params


def test_op_registers_from_this_module_not_the_library():
    ops_pkg._eager_import_all()
    handler = ipc_mod._REGISTRY["deliverable.cascade_terminal"]
    assert handler.__module__ == "coordinator_core.ops.cascade_terminal_op"


def test_handler_one_handoff_one_sizing_under_200ms_zero_spawns(tmp_path, monkeypatch):
    repo, handoff, sizing, base = _seed_run(tmp_path)
    assert len(list((repo / "state" / "handoffs").glob("*.md"))) == 305

    warm_repo, *_ = _seed_run(tmp_path, name="warm", pad=3)
    warm = _run(_params(base), warm_repo)
    assert warm["exit_code"] == 0

    samples = []
    for _ in range(5):
        _git(repo, "reset", "--hard", base)
        with _count_spawns_attributed(monkeypatch) as spawns:
            start = time.process_time()
            result = _run(_params(base), repo)
            elapsed = time.process_time() - start
        assert result["exit_code"] == 0, result
        assert len(result["by_kind"]["handoff"]["advanced"]) == 1
        assert len(result["by_kind"]["sizing"]["advanced"]) == 1
        assert result["commit_sha"] and "commit_error" not in result
        assert spawns == [], [s.argv for s in spawns]
        samples.append(elapsed)

    assert max(samples) < _PROCESS_BAR_S, [round(s * 1000, 1) for s in samples]


def test_both_kinds_land_in_one_commit_and_index_equals_head(tmp_path):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=3)
    result = _run(_params(base, source_kind="plan", source_path="docs/plans/dummy.md"), repo)

    assert result["exit_code"] == 0
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert result["commit_sha"] == head
    assert _git(repo, "rev-list", "--count", f"{base}..HEAD").stdout.strip() == "1"
    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").stdout.split())
    assert committed == {
        "state/handoffs/20260102-target.md",
        "state/sizings/20260102-target.yaml",
    }
    assert _git(repo, "diff", "--cached", "--quiet").returncode == 0

    assert _fm(handoff, "deployment_state") == "shipped"
    assert _fm(handoff, "pickup_ready") == "false"
    assert _fm(handoff, "shipped_in") == base[:8]
    assert _fm(handoff, "shipped_in_kind") == "ship-commit"
    assert _fm(handoff, "advanced_by") == _DID
    assert yaml.safe_load(sizing.read_text(encoding="utf-8"))["status"] == "shipped"
    assert set(result["by_kind"]["handoff"]) >= {
        "candidates_matched", "advanced", "refused", "already_advanced",
        "scan_incomplete", "unreadable",
    }


def test_handoff_without_ship_sha_is_refused_by_name_and_sizing_still_advances(tmp_path):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=3)
    before = handoff.read_text(encoding="utf-8")
    params = _params(base)
    del params["ship_sha"]

    result = _run(params, repo)

    refusal = result["by_kind"]["handoff"]["refused"]
    assert len(refusal) == 1 and "ship_sha" in refusal[0]["reason"]
    assert handoff.read_text(encoding="utf-8") == before
    assert len(result["by_kind"]["sizing"]["advanced"]) == 1
    assert result["exit_code"] == 0


def test_spinoff_candidate_is_refused_by_leg_d(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    handoff = _seed_handoff(repo, "20260102-spin.md", deliverable_id=_DID)
    text = handoff.read_text(encoding="utf-8").replace("status: open\n", "status: open\nkind: spinoff\n", 1)
    handoff.write_text(text, encoding="utf-8")
    base = _commit_all(repo, "seed spinoff")

    result = _run(_params(base), repo)

    refused = result["by_kind"]["handoff"]["refused"]
    assert len(refused) == 1 and "spinoff" in refused[0]["reason"]
    assert result["exit_code"] == 1
    assert result["commit_sha"] is None
    assert handoff.read_text(encoding="utf-8") == text


def test_claimed_candidate_is_refused_by_leg_a(tmp_path):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=2)
    claim_dir = claim_state_mod.handoff_claim_dir(repo / ".git", Path("state/handoffs/20260102-target.md"))
    claim_dir.mkdir(parents=True, exist_ok=True)
    holder = "22222222-2222-2222-2222-222222222222"
    (claim_dir / "session_id").write_text(holder, encoding="utf-8")
    (claim_dir / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")

    with mock.patch.object(claim_state_mod, "cs_claim_holder_live", return_value=True), \
            mock.patch("coordinator_core.ops.deliverable_cascade.resolve_live_session_ids", return_value={holder}):
        result = _run(_params(base), repo)

    refused = result["by_kind"]["handoff"]["refused"]
    assert len(refused) == 1 and "claimed by live session" in refused[0]["reason"]
    assert _fm(handoff, "deployment_state") == "ready_to_fire"
    assert len(result["by_kind"]["sizing"]["advanced"]) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"deliverable_id": ""},
        {"source_kind": "bogus"},
        {"source_path": ""},
        {"ship_sha": "not-a-sha"},
    ],
)
def test_missing_or_malformed_required_param_raises(tmp_path, overrides):
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(ValueError):
        _run(_params("abcdef1", **overrides), repo)


def test_second_call_is_a_noop(tmp_path):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=2)
    assert _run(_params(base), repo)["exit_code"] == 0
    after_first = (handoff.read_text(encoding="utf-8"), sizing.read_text(encoding="utf-8"))
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    second = _run(_params(base), repo)

    assert second["exit_code"] == 1
    assert second["commit_sha"] is None
    assert (handoff.read_text(encoding="utf-8"), sizing.read_text(encoding="utf-8")) == after_first
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head


def _seed_referencer(repo: Path, name: str, **edge: str) -> Path:
    path = repo / "state" / "handoffs" / name
    lines = [
        f'title: "Successor {name}"',
        "created: 2026-01-01",
        "branch: work/test/2026-01-01",
        "status: open",
        "deployment_state: ready_to_fire",
        "deliverable_id: dlv-successor-0",
    ]
    lines += [f'{key}: "{value}"' for key, value in edge.items()]
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n# Handoff\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "edge",
    [
        {"predecessor": "state/handoffs/20260102-target.md"},
        {"predecessor": "20260102-target.md"},
        {"predecessor": "none", "predecessor_id": "hnd-target-a1b2c3"},
    ],
)
def test_live_successor_refuses_the_candidate_by_leg_b(tmp_path, edge):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=2)
    text = handoff.read_text(encoding="utf-8").replace(
        "status: open\n", "status: open\nhandoff_id: hnd-target-a1b2c3\n", 1
    )
    handoff.write_text(text, encoding="utf-8")
    _seed_referencer(repo, "20260103-successor.md", **edge)
    base = _commit_all(repo, "seed successor")

    result = _run(_params(base), repo)

    refused = result["by_kind"]["handoff"]["refused"]
    assert len(refused) == 1 and "live successor" in refused[0]["reason"]
    assert _fm(handoff, "deployment_state") == "ready_to_fire"


def _full_leg_b_children(repo: Path, candidate: Path) -> list:
    result = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(candidate), repo / ".git", edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas=None
        )
    )
    return result["children"]


def _prefiltered_leg_b_children(repo: Path, candidate: Path) -> list:
    texts, _ = op_mod._read_corpus_texts(repo / "state" / "handoffs", ".md")
    fm = op_mod._read_meta(str(candidate))
    metas = op_mod._leg_b_metas(candidate, fm, texts)
    result = asyncio.run(
        hc_mod.has_live_children_from_metas(
            str(candidate), repo / ".git", edge_kinds=hc_mod.CONCLUSION_EDGE_KINDS, metas=metas
        )
    )
    return result["children"]


def test_leg_b_prefilter_agrees_with_the_unprefiltered_scan_on_a_seeded_corpus(tmp_path):
    repo, handoff, sizing, base = _seed_run(tmp_path, pad=20)
    text = handoff.read_text(encoding="utf-8").replace(
        "status: open\n", "status: open\nhandoff_id: hnd-target-a1b2c3\n", 1
    )
    handoff.write_text(text, encoding="utf-8")
    _seed_referencer(repo, "20260103-by-path.md", predecessor="state/handoffs/20260102-target.md")
    _seed_referencer(repo, "20260103-by-name.md", predecessor="20260102-target.md")
    _seed_referencer(repo, "20260103-by-id.md", predecessor="none", predecessor_id="hnd-target-a1b2c3")
    _seed_referencer(repo, "20260103-unrelated.md", predecessor="state/handoffs/20260101-pad-0001.md")

    full = _full_leg_b_children(repo, handoff)
    assert len(full) == 3
    assert _prefiltered_leg_b_children(repo, handoff) == full


def test_leg_b_prefilter_agrees_with_the_unprefiltered_scan_on_the_real_corpus():
    repo = Path(__file__).resolve().parents[3]
    handoff_dir = repo / "state" / "handoffs"
    if not (repo / ".git").exists() or not handoff_dir.is_dir():
        pytest.skip("no real corpus alongside this checkout")
    candidates = sorted(handoff_dir.glob("*.md"))[::12][:30]
    assert candidates
    for candidate in candidates:
        assert _prefiltered_leg_b_children(repo, candidate) == _full_leg_b_children(repo, candidate), candidate


def test_plan_source_advances_a_sizing_joined_only_by_its_plan_fk(tmp_path):
    """A sizing routed before its plan minted a deliverable id carries
    `deliverable_id: null`; the plan source still reaches it through `plan`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    sizing = _seed_sizing(
        repo, "20260102-fk.yaml", status="routed", deliverable_id="null",
        plan="docs/plans/dummy.md",
    )
    stranger = _seed_sizing(
        repo, "20260102-other.yaml", status="routed", deliverable_id="null",
        plan="docs/plans/other.md",
    )
    base = _commit_all(repo, "seed corpus")

    result = _run(_params(base), repo)

    assert result["exit_code"] == 0, result
    assert [Path(a["path"]).name for a in result["by_kind"]["sizing"]["advanced"]] == [sizing.name]
    assert yaml.safe_load(stranger.read_text(encoding="utf-8"))["status"] == "routed"


@pytest.mark.parametrize(
    "value, plan_fk",
    [
        ("docs/plans/p.md", "archive/specs/2026-10/p.md"),
        ("archive/specs/2026-10/p.md", "docs/plans/p.md"),
        ("claude-klabauter:docs/plans/p.md", "archive/specs/2026-10/p.md"),
    ],
)
def test_plan_fk_join_resolves_an_archived_plan_path(value, plan_fk):
    from coordinator_core.ops.deliverable_cascade import _plan_fk_matches

    assert _plan_fk_matches(value, plan_fk)
    assert not _plan_fk_matches(value, "archive/specs/2026-10/q.md")
