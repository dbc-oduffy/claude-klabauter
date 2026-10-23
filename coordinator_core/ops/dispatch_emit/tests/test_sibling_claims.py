"""sibling_claims: unlanded-claimed paths of other runs, and the own-declared filter."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, sibling_claims
from coordinator_core.session import core as session_core

SELF = "5e1f0000-0000-4000-8000-0000000000b1"
OWN = "ask-own"


def _stamped(tag: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{tag}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SELF)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    session_core.init(SELF, cwd=str(root))
    return root


def _declare(repo: Path, run_id: str, *writes: str, **extra) -> Path:
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "manifest.json").write_text(
        json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": [], **extra}),
        encoding="utf-8",
    )
    return rdir


def _halt(rdir: Path) -> None:
    (rdir / "halted.json").write_text(json.dumps({"status": "halted"}), encoding="utf-8")


def test_live_token_sibling_claims_its_declared_path(repo):
    _declare(repo, "run-b", "other/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    claims = sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py", "src/a.py"])
    assert claims == {"other/b.py": "run-b"}


def test_claim_lookup_lists_the_token_store_once_per_call(repo, monkeypatch):
    _declare(repo, "run-b", "other/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    listings: list = []
    real = run_token.held_records
    monkeypatch.setattr(run_token, "held_records", lambda r: listings.append(1) or real(r))
    sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py"])
    assert len(listings) == 1


def test_halted_sibling_claims_its_declared_path(repo):
    run_id = _stamped("aaaaaa")
    _halt(_declare(repo, run_id, "other/b.py"))
    claims = sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py", "src/a.py"])
    assert claims == {"other/b.py": run_id}


def test_stale_token_without_halt_is_not_a_claim(repo):
    _declare(repo, "run-b", "other/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    path = run_token._record_path(repo, "run-b")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = 1.0
    path.write_text(json.dumps(record), encoding="utf-8")
    assert sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py"]) == {}


def test_own_run_dir_is_excluded(repo):
    run_id = _stamped("bbbbbb")
    _halt(_declare(repo, run_id, "other/b.py"))
    assert sibling_claims.sibling_run_claims(repo, run_id, ["other/b.py"]) == {}


def test_clean_record_dir_is_not_read(repo):
    rdir = _declare(repo, _stamped("cccccc"), "other/b.py", review_declared_paths=["other/r.py"])
    (rdir / "run-outcome.json").write_text(
        json.dumps({"inline_review": {"prep": {"slice_files": ["other/s.py"]}}}), encoding="utf-8"
    )
    assert sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py", "other/r.py", "other/s.py"]) == {}


def test_halted_sibling_extra_sources_are_claimed(repo):
    run_id = _stamped("dddddd")
    rdir = _declare(repo, run_id, "other/b.py", review_declared_paths=["other/r.py"])
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/b.yaml"}), encoding="utf-8")
    (rdir / "run-outcome.json").write_text(
        json.dumps({"inline_review": {"prep": {"slice_files": ["other/s.py"]}}}), encoding="utf-8"
    )
    _halt(rdir)
    paths = ["other/r.py", "other/s.py", "state/sizings/b.yaml", "src/a.py"]
    claims = sibling_claims.sibling_run_claims(repo, OWN, paths)
    assert claims == {"other/r.py": run_id, "other/s.py": run_id, "state/sizings/b.yaml": run_id}


def test_unreadable_manifest_contributes_nothing_and_never_raises(repo):
    run_id = _stamped("eeeeee")
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "manifest.json").write_text("{not json", encoding="utf-8")
    _halt(rdir)
    assert sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py"]) == {}


def test_shared_dir_holds_live_sibling_dirt_in_a_shared_directory(repo):
    _declare(repo, "run-b", "shared/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    held = sibling_claims.shared_dir_holds(
        repo, OWN, ["shared/a.py"], ["shared/dirt.py", "elsewhere/x.py"]
    )
    assert held == ["shared/dirt.py"]


def test_shared_dir_holds_halted_sibling_dirt_in_a_shared_directory(repo):
    _halt(_declare(repo, _stamped("ffffff"), "shared/b.py"))
    held = sibling_claims.shared_dir_holds(repo, OWN, ["shared/a.py"], ["shared/dirt.py"])
    assert held == ["shared/dirt.py"]


def test_shared_dir_holds_nothing_for_a_dead_sibling(repo):
    _declare(repo, "run-b", "shared/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    path = run_token._record_path(repo, "run-b")
    record = json.loads(path.read_text(encoding="utf-8"))
    record["minted_at"] = 1.0
    path.write_text(json.dumps(record), encoding="utf-8")
    assert sibling_claims.shared_dir_holds(repo, OWN, ["shared/a.py"], ["shared/dirt.py"]) == []


def test_shared_dir_holds_nothing_for_a_sibling_in_a_foreign_directory(repo):
    _declare(repo, "run-b", "foreign/b.py")
    run_token.mint(repo, "run-b", "b.mjs", session_id=SELF)
    held = sibling_claims.shared_dir_holds(
        repo, OWN, ["shared/a.py"], ["shared/dirt.py", "foreign/dirt.py"]
    )
    assert held == []


def test_shared_dir_holds_excludes_the_run_itself(repo):
    run_id = _stamped("a1b2c3")
    _halt(_declare(repo, run_id, "shared/b.py"))
    assert sibling_claims.shared_dir_holds(repo, run_id, ["shared/a.py"], ["shared/dirt.py"]) == []


def test_filter_own_keeps_a_path_the_run_declares_and_withholds_the_rest():
    claims = {"other/b.py": "run-b", "src/a.py": "run-b"}
    kept, withheld = sibling_claims.filter_own(
        ["src/a.py", "other/b.py", "x.py"], claims, ["src/a.py"]
    )
    assert kept == ["src/a.py", "x.py"]
    assert withheld == {"other/b.py": "run-b"}


def test_reland_remedy_names_the_refire_workflow_line():
    from coordinator_core import warp_fire

    line = sibling_claims.reland_remedy("docs\\sizings/x.yaml")
    assert warp_fire.fire_command("docs/sizings/x.yaml") in line
    assert "dispatch.ask_gate" not in line
    assert "\n" not in line


def test_reland_remedy_args_are_only_the_sizing():
    """The gate derives an XS run's writes from its manifest, so the line must not carry them."""
    import json
    import re

    line = sibling_claims.reland_remedy("state/sizings/x.yaml")
    args = json.loads(re.search(r"args: (\{.*?\})\}\)", line).group(1))
    assert args == {"sizing": "state/sizings/x.yaml"}
