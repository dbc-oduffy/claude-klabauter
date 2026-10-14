"""Parity harness: live ``list_stale_claim_handoffs`` against a frozen oracle, cold then warm.

Each case builds a repo, calls the oracle and the live function (cold), applies a mutation, and
calls both again (warm). Equality is on the ordered ``(path, claimer_sid)`` list.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import List, NamedTuple, Optional
from unittest import mock

import pytest

from coordinator_core import claim_state
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ops.fleet import _common as fleet_common
from coordinator_core.session import core, stale_claims
from coordinator_core.session.liveness import session_live
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

DEAD = {"pid": "1", "last_activity": "2000-01-01T00:00:00Z"}


# --- Frozen oracle: list_stale_claim_handoffs as of 385afa747b (stale_claims.py) ---------------
class _OracleEntry(NamedTuple):
    path: str
    claimer_sid: str


def _oracle_claimer_sid(handoff_path: str, repo_root: Optional[str] = None) -> str:
    state = resolve_claim_state(handoff_path, repo_root=repo_root)
    return state.holder or ""


def _oracle_list_stale(repo_root: Optional[str] = None) -> List[_OracleEntry]:
    root = Path(repo_root).resolve() if repo_root else Path.cwd()
    cwd_str = str(root)

    stale: List[_OracleEntry] = []
    try:
        handoff_paths = fleet_common.collect_live_handoff_paths(root)
    except OSError:
        return stale

    for p in handoff_paths:
        sid = _oracle_claimer_sid(str(p), repo_root=cwd_str)
        if not sid:
            continue
        if not session_live(sid, cwd_str):
            stale.append(_OracleEntry(path=str(p), claimer_sid=sid))
    return stale


# --- Fixtures ----------------------------------------------------------------------------------
def _make_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    return tmp_path


def _session(repo: Path, sid: str, meta: dict) -> None:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def _live_meta() -> dict:
    return {"pid": "2", "last_activity": core.now_iso()}


def _handoff(repo: Path, name: str, extra: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        'title: "Test Handoff"\n'
        "status: active\n"
        f"{extra}\n"
        "---\n\n# Handoff\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def _ledger(repo: Path, handoff_name: str, sid: str) -> None:
    d = repo / ".git" / "coordinator-sessions" / "handoff-claims" / handoff_name
    d.mkdir(parents=True, exist_ok=True)
    (d / "session_id").write_text(sid, encoding="utf-8")
    (d / "claimed_at").write_text("2026-08-07T10:00:00Z", encoding="utf-8")


def _both(repo: Path):
    """Oracle then live, with ledger-holder liveness forced so ledger cases are deterministic."""
    with mock.patch.object(claim_state, "cs_claim_holder_live", return_value=True):
        oracle = [tuple(e) for e in _oracle_list_stale(str(repo))]
        live = [tuple(e) for e in stale_claims.list_stale_claim_handoffs(str(repo))]
    return oracle, live


def _assert_parity(repo: Path) -> List[tuple]:
    oracle, live = _both(repo)
    assert live == oracle
    return live


# --- Setups (each returns a mutation applied between the cold and warm call) -------------------
def _setup_ledger_claim(repo):
    _session(repo, "s-dead-ledger", DEAD)
    h = _handoff(repo, "2026-10-01-ledger.md", "status: open")
    _ledger(repo, h.name, "s-dead-ledger")
    return lambda: _ledger(repo, h.name, "s-dead-ledger2") or _session(repo, "s-dead-ledger2", DEAD)


def _setup_mirror_claim(repo):
    _session(repo, "s-dead-mirror", DEAD)
    h = _handoff(repo, "2026-10-01-mirror.md", "claimed_by: s-dead-mirror")
    return lambda: h.write_text(h.read_text(encoding="utf-8").replace("claimed_by: s-dead-mirror\n", ""), encoding="utf-8")


def _setup_both(repo):
    _session(repo, "s-dead-ledger", DEAD)
    _session(repo, "s-live-mirror", _live_meta())
    h = _handoff(repo, "2026-10-01-both.md", "claimed_by: s-live-mirror")
    _ledger(repo, h.name, "s-dead-ledger")
    return lambda: (repo / ".git" / "coordinator-sessions" / "handoff-claims" / h.name / "session_id").write_text(
        "s-live-mirror", encoding="utf-8"
    )


def _setup_neither(repo):
    _handoff(repo, "2026-10-01-open.md", "status: open")
    _session(repo, "s-dead-late", DEAD)
    return lambda: _handoff(repo, "2026-10-02-late.md", "claimed_by: s-dead-late")


def _setup_consumed_by_legacy(repo):
    _session(repo, "s-dead-legacy", DEAD)
    _handoff(repo, "2026-10-01-legacy.md", "consumed_by: s-dead-legacy")
    _handoff(repo, "2026-10-01-both-keys.md", "claimed_by: s-dead-legacy\nconsumed_by: s-live-old")
    _session(repo, "s-live-old", _live_meta())
    return lambda: None


def _setup_live_and_dead(repo):
    _session(repo, "s-dead-a", DEAD)
    _session(repo, "s-live-a", _live_meta())
    _handoff(repo, "2026-10-01-a-dead.md", "claimed_by: s-dead-a")
    _handoff(repo, "2026-10-01-b-live.md", "claimed_by: s-live-a")
    _handoff(repo, "2026-10-01-c-dead.md", "claimed_by: s-dead-a")
    return lambda: _session(repo, "s-live-a", DEAD)


def _setup_in_place_same_size(repo):
    _session(repo, "s-dead-x1", DEAD)
    _session(repo, "s-live-x2", _live_meta())
    h = _handoff(repo, "2026-10-01-edit.md", "claimed_by: s-dead-x1")
    st = h.stat()

    def mutate():
        h.write_text(h.read_text(encoding="utf-8").replace("s-dead-x1", "s-live-x2"), encoding="utf-8")
        assert h.stat().st_size == st.st_size
        os.utime(h, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))

    return mutate


def _setup_added(repo):
    _session(repo, "s-dead-add", DEAD)
    _handoff(repo, "2026-10-01-one.md", "claimed_by: s-dead-add")
    return lambda: _handoff(repo, "2026-10-01-two.md", "claimed_by: s-dead-add")


def _setup_removed(repo):
    _session(repo, "s-dead-rm", DEAD)
    _handoff(repo, "2026-10-01-one.md", "claimed_by: s-dead-rm")
    gone = _handoff(repo, "2026-10-01-two.md", "claimed_by: s-dead-rm")
    return lambda: gone.unlink()


SETUPS = {
    "ledger-claim": _setup_ledger_claim,
    "mirror-claim": _setup_mirror_claim,
    "both-ledger-wins": _setup_both,
    "neither": _setup_neither,
    "consumed-by-legacy": _setup_consumed_by_legacy,
    "live-and-dead-claimers": _setup_live_and_dead,
    "edited-in-place-same-size": _setup_in_place_same_size,
    "file-added": _setup_added,
    "file-removed": _setup_removed,
}


@pytest.mark.parametrize("case", list(SETUPS))
def test_cold_then_warm_parity(tmp_path, case):
    repo = _make_repo(tmp_path)
    mutate = SETUPS[case](repo)

    _assert_parity(repo)
    mutate()
    warm = _assert_parity(repo)
    again = _assert_parity(repo)

    assert again == warm


def test_mutation_changes_answer_for_state_dependent_cases(tmp_path):
    repo = _make_repo(tmp_path)
    mutate = _setup_in_place_same_size(repo)
    cold = _assert_parity(repo)
    mutate()
    warm = _assert_parity(repo)
    assert len(cold) == 1 and warm == []


def test_file_added_and_removed_change_the_answer(tmp_path):
    repo = _make_repo(tmp_path)
    mutate = _setup_added(repo)
    assert len(_assert_parity(repo)) == 1
    mutate()
    assert len(_assert_parity(repo)) == 2

    (tmp_path / "second").mkdir()
    repo2 = _make_repo(tmp_path / "second")
    mutate2 = _setup_removed(repo2)
    assert len(_assert_parity(repo2)) == 2
    mutate2()
    assert len(_assert_parity(repo2)) == 1


def test_order_is_sorted_by_path(tmp_path):
    repo = _make_repo(tmp_path)
    _setup_live_and_dead(repo)
    live = _assert_parity(repo)
    paths = [p for p, _ in live]
    assert paths == sorted(paths) and len(paths) == 2


def test_permission_denied_handoffs_dir_degrades_to_empty(tmp_path):
    repo = _make_repo(tmp_path)
    mutate = _setup_mirror_claim(repo)

    def denied(_root):
        raise PermissionError("denied")

    with mock.patch.object(fleet_common, "collect_live_handoff_paths", denied), mock.patch.object(
        stale_claims, "collect_live_handoff_paths", denied
    ):
        assert _both(repo) == ([], [])
        mutate()
        assert _both(repo) == ([], [])
    assert len(_assert_parity(repo)) == 0  # mutation removed the claim; visible again, still equal


def test_permission_denied_then_restored_recovers(tmp_path):
    repo = _make_repo(tmp_path)
    _setup_mirror_claim(repo)

    def denied(_root):
        raise PermissionError("denied")

    with mock.patch.object(fleet_common, "collect_live_handoff_paths", denied), mock.patch.object(
        stale_claims, "collect_live_handoff_paths", denied
    ):
        assert _both(repo) == ([], [])
    assert len(_assert_parity(repo)) == 1
