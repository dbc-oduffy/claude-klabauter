"""landing_holds holds a path whose withheld print is typed co_tenant, or whose bytes carry hunks beyond the run's
own-hunk blob while another run owns the path: separable lands the own blob, a live owner parks the run, else the
path is withheld as provenance `co-tenant-hunks-unattributed` (never a refusal kind)."""
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import (
    co_tenant_owner, hunk_provenance, landing_refusal_kinds as kinds, land_refusal_registry, peer_hunk_gate,
)
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-x-20261125T000000-aaaaaa"
OWNER = "warp-y-20261125T000001-bbbbbb"
PATH = "pkg/shared.py"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    def git(*a) -> None:
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "a@b.c")
    git("config", "user.name", "n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / PATH).write_text("a = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "c")
    (tmp_path / PATH).write_text("peer = 3\na = 1\nown = 2\n", encoding="utf-8")
    return tmp_path


def _hold(repo: Path) -> dict:
    return peer_hunk_gate.landing_holds(repo, "sess", RUN, None, [PATH], [], [PATH], [PATH], None)


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch):
    state = {"owner": {"run_id": OWNER, "state": "live"}, "parked": [], "park_ok": True}
    monkeypatch.setattr(co_tenant_owner, "owner_of", lambda root, p, **k: dict(state["owner"]))

    def park(root, run_id, who, exc):
        state["parked"].append((run_id, who, exc.peer))
        return state["park_ok"]

    monkeypatch.setattr(co_tenant_owner, "_park_behind", park)
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda *a, **k: set())
    return state


def _write_print(repo: Path, entry) -> None:
    run_dir = repo / "state" / "warp" / RUN
    run_dir.mkdir(parents=True)
    (run_dir / WITHHELD_PRINTS_FILE).write_text(json.dumps({PATH: entry}), encoding="utf-8")


def test_a_live_owner_with_unseparable_hunks_parks_the_run_and_holds_the_path(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    assert _hold(repo) == {PATH: OWNER}
    assert owner["parked"] == [(RUN, OWNER, PATH)]


def test_a_refused_park_withholds_the_path_as_the_provenance_kind(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    owner["park_ok"] = False
    held = _hold(repo)
    assert held[PATH].startswith(peer_hunk_gate.CO_TENANT_HUNKS_UNATTRIBUTED) and OWNER in held[PATH]
    assert f"landed-tree-test-verify.py --repo {repo} --paths {PATH}" in held[PATH]


def test_a_halted_owner_is_never_parked_behind(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    owner["owner"] = {"run_id": OWNER, "state": "halted"}
    assert _hold(repo)[PATH].startswith(peer_hunk_gate.CO_TENANT_HUNKS_UNATTRIBUTED)
    assert owner["parked"] == []


def test_a_typed_co_tenant_print_holds_without_a_live_owner(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    owner["owner"] = {"run_id": None, "state": None}
    _write_print(repo, {"kind": "co_tenant", "owner": OWNER})
    held = _hold(repo)
    assert OWNER in held[PATH] and held[PATH].startswith(peer_hunk_gate.CO_TENANT_HUNKS_UNATTRIBUTED)


def test_no_owner_and_a_string_print_hold_nothing(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    owner["owner"] = {"run_id": None, "state": None}
    _write_print(repo, "0" * 40)
    assert _hold(repo) == {}


def test_bytes_equal_to_the_own_hunk_blob_hold_nothing(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: False)
    assert _hold(repo) == {}
    assert owner["parked"] == []


def test_a_separable_path_is_released_to_land_its_own_blob(repo, owner, monkeypatch) -> None:
    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    monkeypatch.setattr(hunk_provenance, "landable_under_hold", lambda root, run, plan, held, split_only=frozenset(): set(held))
    assert _hold(repo) == {}
    assert owner["parked"] == []


def test_a_halted_landing_run_holds_nothing_here(repo, owner, monkeypatch) -> None:
    from coordinator_core.ops import warp_run_deliverable

    monkeypatch.setattr(peer_hunk_gate, "_carries_other_hunks", lambda *a: True)
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda root, rid: rid == RUN)
    assert peer_hunk_gate._co_tenant_holds(repo, RUN, [PATH]) == {}


def test_carries_other_hunks_compares_the_worktree_to_the_own_hunk_blob(repo, monkeypatch) -> None:
    from coordinator_core.ops.dispatch_emit import run_hunk_claims

    real = run_hunk_claims.own_hunks

    def only_first(root, run_id, path, hunks, holders, **k):
        split = real(root, run_id, path, hunks, holders, **k)
        split.own[:] = list(hunks)[:1]
        return split

    monkeypatch.setattr(run_hunk_claims, "own_hunks", only_first)
    assert peer_hunk_gate._carries_other_hunks(repo, RUN, PATH, OWNER) is True
    (repo / PATH).write_text("a = 1\nown = 2\n", encoding="utf-8")
    monkeypatch.setattr(run_hunk_claims, "own_hunks", lambda root, run_id, path, hunks, holders, **k: type("S", (), {"own": list(hunks)})())
    assert peer_hunk_gate._carries_other_hunks(repo, RUN, PATH, OWNER) is False


def test_the_new_kind_is_provenance_and_registered_never_a_refusal() -> None:
    kind = peer_hunk_gate.CO_TENANT_HUNKS_UNATTRIBUTED
    assert kind in kinds.PROVENANCE_KINDS and kind not in kinds.LANDING_REFUSAL_KINDS
    assert not kinds.is_refusal(kind)
    entry = land_refusal_registry.classify(kind)
    assert entry is not None and entry.denies is False
