"""co_declared_peer_holds releases a halted holder whose owner session is gone, hunks or not."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_hold_release, peer_hunk_gate
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha
from coordinator_core.ops.dispatch_emit.tests import test_land_run_halted_holder_attribution as attr
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PATH = "pkg/shared.py"
OWNER = "11111111-aaaa-bbbb-cccc-222222222222"


def _aged(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


H1 = _aged(120, "aaaaaa")
OWN = _aged(10, "cccccc")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _receipt(root: Path, run_id: str, session: str = OWNER) -> None:
    script = root / RUN_DIR_ROOT / f"{run_id}.workflow.mjs"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.with_name(script.name + ".emitted.json").write_text(json.dumps({"session_id": session}), encoding="utf-8")


def _holder(root: Path, run_id: str, *, baseline: str | None = "0" * 40) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (root / PATH).parent.mkdir(exist_ok=True)
    (root / PATH).write_text("dirty\n", encoding="utf-8")
    manifest = {"rows": [{"id": "C1", "writes": [PATH]}], "doc_writes": []}
    if baseline:
        manifest["write_baseline"] = {PATH: baseline}
    (rdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": {PATH: "peer-held"}}}), encoding="utf-8"
    )
    return rdir


def _liveness(monkeypatch, live: bool) -> None:
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda _sid, *a, **k: live)


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _holds(root: Path) -> list:
    return peer_hunk_gate.co_declared_peer_holds(root, OWN, [PATH], [PATH])


def _released(rdir: Path):
    return json.loads((rdir / "halted.json").read_text(encoding="utf-8")).get("released")


def test_phantom_holder_is_released_with_the_head_sha(root, monkeypatch):
    rdir = _holder(root, H1)
    _receipt(root, H1)
    _liveness(monkeypatch, False)

    assert _holds(root) == []
    assert _released(rdir) == {"reason": halted_hold_release.RELEASE_OWNER_NOT_LIVE, "sha": _git(root, "rev-parse", "HEAD")}
    assert json.loads((rdir / "halted.json").read_text(encoding="utf-8"))["status"] == "halted"


def test_live_owner_still_holds(root, monkeypatch):
    rdir = _holder(root, H1)
    _receipt(root, H1)
    _liveness(monkeypatch, True)
    before = (rdir / "halted.json").read_bytes()

    assert [s for _p, s in _holds(root)] == [H1]
    assert (rdir / "halted.json").read_bytes() == before


def test_unknown_owner_still_holds(root, monkeypatch):
    rdir = _holder(root, H1)
    _liveness(monkeypatch, False)

    assert [s for _p, s in _holds(root)] == [H1]
    assert _released(rdir) is None


def test_dead_owner_with_its_own_kept_hunk_is_released(root, monkeypatch):
    rdir = _holder(root, H1)
    _receipt(root, H1)
    _liveness(monkeypatch, False)
    (root / PATH).write_text("hunk\n", encoding="utf-8")
    kept = {"chunks": {"C1": {"paths": {PATH: blob_sha(b"hunk\n")}}}, "plan_gate": None}
    (rdir / "kept.json").write_text(json.dumps(kept), encoding="utf-8")

    assert _holds(root) == []
    assert _released(rdir)["reason"] == halted_hold_release.RELEASE_OWNER_NOT_LIVE


def _two_path_holder(root: Path, run_id: str, impl: str, tests: str) -> Path:
    rdir = root / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    for rel in (impl, tests):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("dirty\n", encoding="utf-8")
    manifest = {
        "rows": [{"id": "C1", "writes": [impl, tests]}],
        "doc_writes": [],
        "write_baseline": {impl: "0" * 40, tests: "0" * 40},
    }
    (rdir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    withheld = {impl: "peer-held", tests: "peer-held"}
    (rdir / "halted.json").write_text(
        json.dumps({"status": "halted", "run_artifacts": {"withheld": withheld}}), encoding="utf-8"
    )
    return rdir


def test_non_live_claimant_never_splits_an_implementation_file_from_its_tests(root, monkeypatch):
    impl, tests = "scripts/run_band_extraction.py", "scripts/tests/test_run_band_extraction.py"
    rdir = _two_path_holder(root, H1, impl, tests)
    _receipt(root, H1)
    _liveness(monkeypatch, False)

    assert peer_hunk_gate.co_declared_peer_holds(root, OWN, [impl, tests], [impl, tests]) == []
    assert _released(rdir)["reason"] == halted_hold_release.RELEASE_OWNER_NOT_LIVE


def test_live_claimant_still_holds_the_implementation_and_its_tests(root, monkeypatch):
    impl, tests = "scripts/run_band_extraction.py", "scripts/tests/test_run_band_extraction.py"
    rdir = _two_path_holder(root, H1, impl, tests)
    _receipt(root, H1)
    _liveness(monkeypatch, True)

    held = {p for p, _s in peer_hunk_gate.co_declared_peer_holds(root, OWN, [impl, tests], [impl, tests])}
    assert held == {impl, tests}
    assert _released(rdir) is None


def _phantom_landing(repo, monkeypatch, *, live: bool, receipt: bool, b_landing: str):
    holder = attr._landing(repo, b_landing=attr._sha(b_landing), holder="aaaaaa", b_holder=attr.B0)
    rdir = repo / "state" / "warp" / holder
    marker = json.loads((rdir / "halted.json").read_text(encoding="utf-8"))
    marker["run_artifacts"] = {"withheld": {attr.PATH: "peer-held"}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    if receipt:
        _receipt(repo, holder)
    _liveness(monkeypatch, live)
    return prod._land(repo), rdir


def test_land_run_commits_past_a_phantom_holder_and_records_the_release(repo, monkeypatch):
    out, rdir = _phantom_landing(repo, monkeypatch, live=False, receipt=True, b_landing=attr.B0)

    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert _released(rdir)["reason"] == halted_hold_release.RELEASE_OWNER_NOT_LIVE
    assert prod._git(["show", "HEAD:" + attr.PATH], repo) == attr.B2


def _names_peer_hunks_in_provenance(out) -> bool:
    prov = [e.get("reason") for e in out.get("provenance") or []]
    return peer_hunk_gate.PEER_HUNKS_CO_DECLARED in prov


def test_land_run_commits_past_a_live_owner_holder_naming_it_in_provenance(repo, monkeypatch):
    out, rdir = _phantom_landing(repo, monkeypatch, live=True, receipt=True, b_landing=attr.B0)

    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert _names_peer_hunks_in_provenance(out), out
    assert _released(rdir) is None


def test_land_run_commits_past_a_dead_owner_holder_with_real_hunks_and_records_the_release(repo, monkeypatch):
    out, rdir = _phantom_landing(repo, monkeypatch, live=False, receipt=True, b_landing=attr.B1)

    assert out.get("refused") is None, out
    assert out.get("committed") is True, out
    assert _released(rdir)["reason"] == halted_hold_release.RELEASE_OWNER_NOT_LIVE
    released = _released(rdir)
    assert len(released["sha"]) == 40 and released["sha"] != out["sha"], (released, out)
