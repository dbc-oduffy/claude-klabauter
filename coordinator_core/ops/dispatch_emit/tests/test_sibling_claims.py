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


def _halt(rdir: Path, *withheld: str) -> None:
    marker: dict = {"status": "halted"}
    if withheld:
        marker["run_artifacts"] = {"withheld": {p: "left in tree" for p in withheld}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")


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


def test_halted_sibling_claims_its_withheld_path(repo):
    run_id = _stamped("aaaaaa")
    _halt(_declare(repo, run_id, "other/b.py"), "other/b.py")
    claims = sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py", "src/a.py"])
    assert claims == {"other/b.py": run_id}


def test_halted_sibling_withholding_nothing_claims_nothing(repo):
    run_id = _stamped("aaaaab")
    _halt(_declare(repo, run_id, "other/b.py"))
    assert sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py"]) == {}


def test_halted_sibling_withholding_another_path_does_not_claim_this_one(repo):
    _halt(_declare(repo, _stamped("aaaaac"), "other/b.py", "other/c.py"), "other/c.py")
    assert sibling_claims.sibling_run_claims(repo, OWN, ["other/b.py"]) == {}


def test_missing_sibling_sizing_file_yields_no_claim_and_never_raises(repo):
    run_id = _stamped("aaaaad")
    rdir = _declare(repo, run_id, "other/b.py")
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/gone.yaml"}), encoding="utf-8")
    run_token.mint(repo, run_id, "b.mjs", session_id=SELF)
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    assert peer_hunk_gate.co_declared_peer_holds(repo, OWN, ["src/a.py"], ["src/a.py"]) == []


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
    _halt(rdir, "other/r.py", "other/s.py", "state/sizings/b.yaml")
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
    _halt(_declare(repo, _stamped("ffffff"), "shared/b.py"), "shared/b.py")
    held = sibling_claims.shared_dir_holds(repo, OWN, ["shared/a.py"], ["shared/dirt.py"])
    assert held == ["shared/dirt.py"]


def test_shared_dir_holds_nothing_for_a_halted_sibling_that_withheld_nothing(repo):
    _halt(_declare(repo, _stamped("ffffee"), "shared/b.py"))
    assert sibling_claims.shared_dir_holds(repo, OWN, ["shared/a.py"], ["shared/dirt.py"]) == []


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


SIZING = "state/sizings/s.yaml"


def _aged(age: int, tail: str) -> str:
    return f"ask-{time.strftime('%Y%m%dT%H%M%S', time.gmtime(time.time() - age))}-{tail}"


def _bind(repo: Path, run_id: str, marker: str | None, *, sizing: str = SIZING, plan: str | None = None, withheld: tuple = ()) -> None:
    rec = repo / "state" / "warp" / run_id
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if marker:
        body = {"status": marker, "sizing_rel": sizing, **({"plan": plan} if plan else {})}
        if withheld:
            body["run_artifacts"] = {"withheld": {p: "left in tree" for p in withheld}}
        (rec / "halted.json").write_text(json.dumps(body), encoding="utf-8")


def _sizing(repo: Path, rel: str, status: str = "open") -> None:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(f"status: {status}\n", encoding="utf-8")


def test_halted_run_with_superseded_sizing_or_plan_landing_is_not_unlanded(repo):
    _sizing(repo, SIZING, "superseded")
    halted = _aged(300, "aaaaaa")
    _declare(repo, halted, "p/a.py")
    _bind(repo, halted, "halted", plan="docs/plans/p.md", withheld=("p/a.py",))
    assert sibling_claims._unlanded_run_ids(repo, OWN) == []
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}
    _sizing(repo, SIZING)
    assert sibling_claims._unlanded_run_ids(repo, OWN) == [halted]
    assert sibling_claims._unlanded_run_ids(repo, OWN, "docs/plans/p.md") == []
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"], own_plan="docs/plans/p.md") == {}
    assert sibling_claims.shared_dir_holds(repo, OWN, ["p/own.py"], ["p/x.py"]) == ["p/x.py"]
    assert sibling_claims.shared_dir_holds(repo, OWN, ["p/own.py"], ["p/x.py"], own_plan="docs/plans/p.md") == []


def test_resumed_run_with_landed_successor_claims_nothing_but_unlanded_successor_still_does(repo):
    _sizing(repo, SIZING)
    first, second = _aged(400, "aaaaaa"), _aged(200, "bbbbbb")
    _declare(repo, first, "p/a.py")
    _bind(repo, first, "resumed")
    run_token.mint(repo, first, "a.mjs", session_id=SELF)
    _bind(repo, second, "halted")
    assert sibling_claims._unlanded_run_ids(repo, OWN) == [first, second]
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {"p/a.py": first}
    _bind(repo, second, "landed")
    assert sibling_claims._unlanded_run_ids(repo, OWN) == []
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_live_peer_with_superseded_sizing_still_claims(repo):
    _sizing(repo, SIZING, "superseded")
    live = _aged(100, "cccccc")
    _declare(repo, live, "p/a.py")
    _bind(repo, live, None)
    run_token.mint(repo, live, "c.mjs", session_id=SELF)
    assert sibling_claims._unlanded_run_ids(repo, OWN) == [live]
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {"p/a.py": live}


def test_fresh_token_run_minted_at_or_after_minted_before_is_junior_and_claims_nothing(repo):
    live = _aged(100, "cccccc")
    _declare(repo, live, "p/a.py")
    run_token.mint(repo, live, "c.mjs", session_id=SELF)
    minted_at = run_token._read(run_token._record_path(repo, live, claimed=False))["minted_at"]
    assert sibling_claims._unlanded_run_ids(repo, OWN, None, (minted_at, live)) == []
    assert sibling_claims._unlanded_run_ids(repo, OWN, None, (minted_at + 1, live)) == [live]


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


def _spine_only_halted(repo: Path, path: str, withheld: tuple = ()) -> str:
    run_id = _stamped("aaaa5e")
    rdir = repo / "state" / "warp" / run_id
    rdir.mkdir(parents=True)
    (rdir / "run.json").write_text(json.dumps({"sizing_rel": "state/sizings/sp.yaml"}), encoding="utf-8")
    marker: dict = {"status": "halted"}
    if withheld:
        marker["run_artifacts"] = {"withheld": {p: "left" for p in withheld}}
    (rdir / "halted.json").write_text(json.dumps(marker), encoding="utf-8")
    (repo / "state/sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state/sizings/sp.yaml").write_text('plan: "docs/plans/sp.md"\n', encoding="utf-8")
    (repo / "docs/plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs/plans/sp.md").write_text(
        "# P\n\n## Tasks\n\n```yaml plan-tasks\n- id: C0\n  writes:\n    - " + path + "\n```\n", encoding="utf-8",
    )
    return run_id


@pytest.mark.parametrize("dirty, withheld, warns", [
    ({"p/a.py"}, (), True),
    (set(), (), False),
    ({"p/a.py"}, ("p/a.py",), False),
])
def test_spine_only_halted_sibling_warns_once_when_it_frees_a_dirty_own_declared_path(
    repo, monkeypatch, caplog, dirty, withheld, warns,
):
    run_id = _spine_only_halted(repo, "p/a.py", withheld)
    monkeypatch.setattr(sibling_claims, "_dirty_reader", lambda root, wanted: (lambda: dirty))

    with caplog.at_level("WARNING", logger=sibling_claims.__name__):
        claims = sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"], own_declared=["p/a.py"])

    hits = [r.getMessage() for r in caplog.records if "freed for landing run" in r.getMessage()]
    if warns:
        assert hits == [f"spine-only halted sibling {run_id}: dirty declared path p/a.py freed for landing run {OWN}"]
        assert claims == {}
    else:
        assert hits == []
    assert bool(claims) is bool(withheld)
