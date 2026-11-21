"""co_tenant_kept: identical-kept holder detection and the antisymmetric seniority order."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_contract, co_tenant_kept, halted_hold_release, land_run

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str, data: bytes | None = None) -> str:
    done = subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True, check=True)
    return done.stdout.decode().strip()


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def _blob(root: Path, text: str) -> str:
    return _git(root, "hash-object", "-w", "--stdin", data=text.encode())


@pytest.fixture()
def kept(monkeypatch):
    """Per-run `(paths, kept)` plans served through the real `_kept_plan` seam; a missing run reads None."""
    plans: dict = {}
    monkeypatch.setattr(ask_contract, "record_dir", lambda root, run_id: Path(run_id))
    monkeypatch.setattr(halted_hold_release, "_kept_plan", lambda record: plans.get(str(record)))

    def put(run_id: str, blobs: dict) -> None:
        plans[run_id] = (sorted(blobs), dict(blobs))

    return put


@pytest.fixture()
def spawns(monkeypatch):
    import coordinator_core.git.run as run_mod

    calls: list = []
    real = run_mod.run_git

    def counting(argv, *a, **k):
        calls.append(list(argv))
        return real(argv, *a, **k)

    monkeypatch.setattr(run_mod, "run_git", counting)
    return calls


def test_identical_blob_maps_to_holder(repo, kept, spawns):
    x = _blob(repo, "same\n")
    kept("run-a", {"p.py": x})
    kept("run-b", {"p.py": x})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b"}) == {"p.py": "run-b"}
    assert len(spawns) == 1 and "--batch-check" in spawns[0]


def test_differing_blob_is_omitted(repo, kept):
    kept("run-a", {"p.py": _blob(repo, "one\n")})
    kept("run-b", {"p.py": _blob(repo, "two\n")})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b"}) == {}


def test_unreadable_holder_record_is_omitted(repo, kept):
    kept("run-a", {"p.py": _blob(repo, "one\n")})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-live"}) == {}


def test_missing_blob_is_omitted(repo, kept):
    x = _blob(repo, "one\n")
    kept("run-a", {"p.py": x})
    kept("run-b", {"p.py": "0" * 40})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b"}) == {}


def test_empty_foreign_spawns_nothing(repo, kept, spawns):
    kept("run-a", {"p.py": _blob(repo, "one\n")})
    spawns.clear()
    assert co_tenant_kept.identical_holders(repo, "run-a", {}) == {}
    assert spawns == []


def test_multi_holder_value_splits_and_needs_one_identical(repo, kept):
    x = _blob(repo, "same\n")
    kept("run-a", {"p.py": x, "q.py": x})
    kept("run-b", {"p.py": x, "q.py": x})
    kept("run-c", {"p.py": _blob(repo, "other\n"), "q.py": x})
    out = co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b, run-c", "q.py": "run-b, run-c"})
    assert out == {"p.py": "run-b"}


def test_live_holder_beside_identical_holder_still_maps_the_identical_one(repo, kept):
    x = _blob(repo, "same\n")
    kept("run-a", {"p.py": x})
    kept("run-b", {"p.py": x})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b, run-live"}) == {"p.py": "run-b"}


def test_own_kept_unreadable_maps_nothing(repo, kept):
    kept("run-b", {"p.py": _blob(repo, "same\n")})
    assert co_tenant_kept.identical_holders(repo, "run-a", {"p.py": "run-b"}) == {}


@pytest.mark.parametrize("seniority", [{}, {"run-a": (1.0, "run-a"), "run-b": (2.0, "run-b")}, {"run-a": (1.0, "run-a")}])
def test_senior_is_antisymmetric(monkeypatch, tmp_path, seniority):
    monkeypatch.setattr(land_run, "_own_run_seniority", lambda root, run, script: seniority.get(run))
    a = co_tenant_kept.senior(tmp_path, "run-a", seniority.get("run-a"), "run-b")
    b = co_tenant_kept.senior(tmp_path, "run-b", seniority.get("run-b"), "run-a")
    assert a is True and b is False


def test_senior_prefers_minted_order_over_lexical(monkeypatch, tmp_path):
    table = {"run-a": (9.0, "run-a"), "run-b": (1.0, "run-b")}
    monkeypatch.setattr(land_run, "_own_run_seniority", lambda root, run, script: table.get(run))
    assert co_tenant_kept.senior(tmp_path, "run-b", table["run-b"], "run-a") is True
    assert co_tenant_kept.senior(tmp_path, "run-a", table["run-a"], "run-b") is False
