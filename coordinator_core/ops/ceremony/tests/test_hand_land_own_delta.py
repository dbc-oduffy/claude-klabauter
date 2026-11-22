"""`ceremony.hand_land` takes a halted run's own-delta blob, never its raw kept image, and refuses a landing that
imports a name existing only uncommitted; both refuse with nothing moved. Real git fixture, default op entry."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.ceremony.tests.test_hand_land import _call, _git, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit import kept_work
from coordinator_core.ops.dispatch_emit.halt_refire import WITHHELD_OWN_DELTA_FILE

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "ask-20261122T000000-0wnd31"
PATH = "a.py"
HEAD_TEXT = b"one\ntwo\nthree\n"
RAW = b"one\nPEER\nthree\nOWN\n"
OWN = b"one\ntwo\nthree\nOWN\n"


def _blob(repo, data: bytes) -> str:
    (repo / "scratch.bin").write_bytes(data)
    sha = _git(repo, "hash-object", "-w", "--no-filters", "scratch.bin")
    (repo / "scratch.bin").unlink()
    return sha


def _halted_run(repo, *, own_blob: str | None, unlandable: bool = False) -> None:
    (repo / PATH).write_bytes(HEAD_TEXT)
    _git(repo, "add", "--", PATH)
    _git(repo, "commit", "-q", "-m", "head text")
    raw = _blob(repo, RAW)
    rec = repo / "state" / "warp" / RUN
    kept_work.write(rec, {"C1": {"paths": {PATH: raw}}}, None)
    doc = {"paths": {PATH: {"blob": own_blob}} if own_blob else {}, **({"unlandable": [PATH]} if unlandable else {})}
    (rec / WITHHELD_OWN_DELTA_FILE).write_text(json.dumps(doc), encoding="utf-8")


def test_a_raw_kept_image_is_refused_with_nothing_moved_and_the_own_delta_blob_lands_without_the_peer_hunk(repo):
    _halted_run(repo, own_blob=_blob(repo, OWN))
    (repo / PATH).write_bytes(RAW)
    head = _git(repo, "rev-parse", "HEAD")

    refused = _call({"repo_root": str(repo), "paths": [PATH], "message": "land raw"})

    assert refused["ok"] is False
    assert refused["refused"] == "raw_kept_blob"
    assert RUN in refused["error"]
    assert _git(repo, "rev-parse", "HEAD") == head
    assert (repo / PATH).read_bytes() == RAW

    (repo / PATH).write_bytes(OWN)
    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "land own delta"})

    assert landed["ok"] is True, landed
    assert _git(repo, "show", f"HEAD:{PATH}") == OWN.decode().strip()
    assert "PEER" not in _git(repo, "show", f"HEAD:{PATH}")


def test_a_raw_kept_image_equal_to_its_own_delta_lands(repo):
    _halted_run(repo, own_blob=_blob(repo, RAW))
    (repo / PATH).write_bytes(RAW)

    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "no peer dirt"})

    assert landed["ok"] is True, landed


def test_a_raw_kept_image_of_an_unlandable_path_is_refused(repo):
    _halted_run(repo, own_blob=None, unlandable=True)
    (repo / PATH).write_bytes(RAW)

    refused = _call({"repo_root": str(repo), "paths": [PATH], "message": "land raw"})

    assert refused["refused"] == "raw_kept_blob"
    assert "unlandable" in refused["error"]


def test_a_path_the_worktree_edited_past_the_kept_image_is_not_refused(repo):
    _halted_run(repo, own_blob=_blob(repo, OWN))
    (repo / PATH).write_bytes(RAW + b"later\n")

    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "edited past kept"})

    assert landed["ok"] is True, landed


def _commit_package(repo) -> None:
    (repo / "coordinator_core" / "pkg").mkdir(parents=True)
    (repo / "coordinator_core" / "__init__.py").write_bytes(b"")
    (repo / "coordinator_core" / "pkg" / "__init__.py").write_bytes(b"")
    _git(repo, "add", "--", "coordinator_core/__init__.py", "coordinator_core/pkg/__init__.py")
    _git(repo, "commit", "-q", "-m", "package")


@pytest.mark.parametrize("source", [b"import coordinator_core.pkg.newmod\n", b"from coordinator_core.pkg import newmod\n"])
def test_an_import_of_an_uncommitted_only_module_is_refused_with_nothing_moved(repo, source):
    _commit_package(repo)
    (repo / "coordinator_core" / "pkg" / "newmod.py").write_bytes(b"VALUE = 1\n")
    (repo / PATH).write_bytes(source)
    head = _git(repo, "rev-parse", "HEAD")

    refused = _call({"repo_root": str(repo), "paths": [PATH], "message": "land importer"})

    assert refused["ok"] is False
    assert refused["refused"] == "uncommitted_import"
    assert "coordinator_core" in refused["error"] and "newmod" in refused["error"]
    assert _git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("source", [b"import coordinator_core.pkg.newmod\n", b"from coordinator_core.pkg import newmod\n"])
def test_landing_the_module_with_its_importer_lands(repo, source):
    _commit_package(repo)
    (repo / "coordinator_core" / "pkg" / "newmod.py").write_bytes(b"VALUE = 1\n")
    (repo / PATH).write_bytes(source)

    landed = _call({"repo_root": str(repo), "paths": [PATH, "coordinator_core/pkg/newmod.py"], "message": "land both"})

    assert landed["ok"] is True, landed


def test_an_import_of_a_committed_module_or_a_guarded_import_lands(repo):
    _commit_package(repo)
    (repo / "coordinator_core" / "pkg" / "newmod.py").write_bytes(b"VALUE = 1\n")
    _git(repo, "add", "--", "coordinator_core/pkg/newmod.py")
    _git(repo, "commit", "-q", "-m", "module")
    (repo / PATH).write_bytes(b"from coordinator_core.pkg import newmod\nimport coordinator_core.pkg.newmod\n")

    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "committed import"})

    assert landed["ok"] is True, landed
    (repo / "coordinator_core" / "pkg" / "other.py").write_bytes(b"VALUE = 2\n")
    (repo / "b.py").write_bytes(b"try:\n    import coordinator_core.pkg.other\nexcept ImportError:\n    pass\n")

    guarded = _call({"repo_root": str(repo), "paths": ["b.py"], "message": "guarded import"})

    assert guarded["ok"] is True, guarded


def test_a_gate_that_cannot_run_allows_the_landing_and_names_itself_in_the_reply(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import stranded_sibling

    def _boom(*_a, **_k):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(stranded_sibling, "recent_run_dirs", _boom)
    _commit_package(repo)
    (repo / "coordinator_core" / "pkg" / "newmod.py").write_bytes(b"VALUE = 1\n")
    (repo / PATH).write_bytes(b"from coordinator_core.pkg import newmod\n")
    monkeypatch.setattr("coordinator_core.percolate.guards._module_level_import_targets", _boom)

    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "gates cannot run"})

    assert landed["ok"] is True, landed
    assert landed["gate_unverified"] == ["raw_kept_blob", "uncommitted_import"]


def test_a_gate_that_runs_leaves_no_gate_unverified_in_the_reply(repo):
    (repo / PATH).write_bytes(b"x = 1\n")

    landed = _call({"repo_root": str(repo), "paths": [PATH], "message": "gates run"})

    assert landed["ok"] is True, landed
    assert "gate_unverified" not in landed
