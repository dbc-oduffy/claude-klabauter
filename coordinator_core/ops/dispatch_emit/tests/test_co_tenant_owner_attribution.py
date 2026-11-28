"""co_tenant_owner.peer_hunk_runs names a halted run withholding or keeping a path, never one that only declares it; owner_of names none."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.dispatch_emit import co_tenant_kept, co_tenant_owner, halted_withheld_index

PATH = "coordinator_core/ops/dispatch_emit/ask_contract.py"
NEEDER = "ask-20261122T080904-740de8"
STALE = "ask-20261024T005815-667dbd"
HOLDER = "warp-x-20261122T070440-57463c"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _halt(repo: Path, run_id: str, *, withheld=(), declared=(), kept=(), indexed=None, at_ns: int) -> None:
    rd = repo / "state" / "warp" / run_id
    rd.mkdir(parents=True)
    marker = {"status": "halted", "run_artifacts": {"withheld": {p: "kept" for p in withheld}}}
    (rd / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    if kept:
        (rd / "kept.json").write_text(
            json.dumps({"chunks": {"C1": {"paths": {p: "HEAD:" + p for p in kept}}}}), encoding="utf-8"
        )
    idx = repo / halted_withheld_index.INDEX_DIR
    idx.mkdir(parents=True, exist_ok=True)
    (idx / f"{run_id}.json").write_text(
        json.dumps({"paths": list(withheld if indexed is None else indexed), "declared": list(declared), "at_ns": at_ns}), encoding="utf-8"
    )


@pytest.mark.parametrize("holder_first", [True, False])
def test_withholding_run_is_attributed_and_a_declared_only_run_is_not(repo, holder_first) -> None:
    holder_ns, stale_ns = (1_000, 2_000) if holder_first else (2_000, 1_000)
    _halt(repo, HOLDER, withheld=(PATH,), at_ns=holder_ns)
    _halt(repo, STALE, declared=(PATH,), at_ns=stale_ns)
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=NEEDER) == (HOLDER,)
    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=NEEDER) == {"run_id": None, "state": None}


@pytest.mark.parametrize("holder_first", [True, False])
def test_kept_return_is_attributed_and_a_declared_only_run_is_not(repo, holder_first) -> None:
    holder_ns, stale_ns = (1_000, 2_000) if holder_first else (2_000, 1_000)
    _halt(repo, HOLDER, withheld=(PATH,), kept=(PATH,), declared=(PATH,), indexed=(), at_ns=holder_ns)
    _halt(repo, STALE, declared=(PATH,), at_ns=stale_ns)
    assert co_tenant_kept.kept_paths(repo, HOLDER) == frozenset({PATH})
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=NEEDER) == (HOLDER,)


def test_declared_only_run_is_neither_attributed_nor_the_owner_when_nothing_withholds(repo) -> None:
    _halt(repo, STALE, declared=(PATH,), at_ns=1_000)
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=NEEDER) == ()
    assert co_tenant_owner.owner_of(repo, PATH, exclude_run_id=NEEDER) == {"run_id": None, "state": None}


def test_kept_paths_of_an_unreadable_or_unsafe_run_is_empty(repo) -> None:
    assert co_tenant_kept.kept_paths(repo, "../x") == frozenset()
    assert co_tenant_kept.kept_paths(repo, "ask-20261122T000000-aaaaaa") == frozenset()


def test_attribution_spawns_nothing(repo, monkeypatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    _halt(repo, HOLDER, withheld=(PATH,), at_ns=1_000)
    _halt(repo, STALE, declared=(PATH,), at_ns=2_000)
    assert co_tenant_owner.peer_hunk_runs(repo, PATH, exclude_run_id=NEEDER) == (HOLDER,)
