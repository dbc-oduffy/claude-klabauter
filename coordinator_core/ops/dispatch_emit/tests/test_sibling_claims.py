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


def _halted_declaring_dirty(repo: Path, monkeypatch, path: str = "p/a.py", *, kept: str | None = None) -> str:
    """A halted sibling whose manifest declares `path`, dirty in the tree; `kept` is the content its
    kept.json fingerprints for the path (None: it recorded nothing)."""
    from coordinator_core.ops.dispatch_emit import kept_work

    run_id = _stamped("bbbb5e")
    rdir = _declare(repo, run_id, path, write_baseline={path: kept_work.blob_sha(b"baseline = 0\n")})
    _halt(rdir)
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_bytes(b"current = 1\n")
    if kept is not None:
        blob = kept_work.blob_sha(kept.encode("utf-8"))
        (rdir / "kept.json").write_text(
            json.dumps({"chunks": {"C1": {"paths": {path: blob}}}, "plan_gate": None}), encoding="utf-8",
        )
    monkeypatch.setattr(sibling_claims, "_dirty_reader", lambda root, wanted: (lambda: {path}))
    return run_id


def test_halted_sibling_claims_nothing_when_the_dirt_in_its_declared_path_is_another_runs(repo, monkeypatch):
    _halted_declaring_dirty(repo, monkeypatch, kept="sibling's own earlier content\n")
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_halted_sibling_that_recorded_nothing_claims_nothing_on_a_dirty_declared_path(repo, monkeypatch):
    _halted_declaring_dirty(repo, monkeypatch)
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_halted_sibling_whose_kept_blob_matches_the_current_file_still_claims_it(repo, monkeypatch):
    """The landing run shares the halted run's baseline and started after the halt, so only rule 2b
    (kept blob == current) keeps the path held."""
    from coordinator_core.ops.dispatch_emit import kept_work

    run_id = _halted_declaring_dirty(repo, monkeypatch, kept="current = 1\n")
    own_dir = _declare(repo, OWN, "p/a.py", write_baseline={"p/a.py": kept_work.blob_sha(b"baseline = 0\n")})
    (own_dir / "run-stage.json").write_text(
        json.dumps({"stage": "running", "started_at": "2999-01-01T00:00:00Z"}), encoding="utf-8",
    )
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {"p/a.py": run_id}


def test_halted_sibling_withheld_path_claims_whatever_the_dirt(repo, monkeypatch):
    run_id = _halted_declaring_dirty(repo, monkeypatch, kept="other\n")
    _halt(repo / "state" / "warp" / run_id, "p/a.py")
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {"p/a.py": run_id}


def test_halted_sibling_baseline_that_head_moved_past_is_not_own_dirt_but_a_live_one_is(repo, monkeypatch):
    run_id = _halted_declaring_dirty(repo, monkeypatch)
    manifest = repo / "state" / "warp" / run_id / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    monkeypatch.setattr(sibling_claims, "_head_blobs", lambda root, paths: {"p/a.py": "headblob"})
    for baseline, claimed in (("headblob", True), ("older-blob", False)):
        manifest.write_text(json.dumps({**data, "write_baseline": {"p/a.py": baseline}}), encoding="utf-8")
        got = sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"])
        assert got == ({"p/a.py": run_id} if claimed else {})


def test_own_dirt_crlf_edit_over_baseline_head_holds_in_lf_form_is_own(tmp_path):
    from coordinator_core.ops.dispatch_emit.tests._kept_fixture import crlf_repo

    root, rel, committed, raw = crlf_repo(tmp_path)
    run_id = _stamped("c3c3c3")
    _declare(root, run_id, rel, write_baseline={rel: raw})
    (root / rel).write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    assert sibling_claims._own_dirt(root, run_id, rel, {rel: committed}) is True
    assert sibling_claims._own_dirt(root, run_id, rel, {rel: "1" * 40}) is False


def test_own_dirt_file_created_by_the_run_is_own_while_absent_at_head(tmp_path):
    run_id = _stamped("c4c4c4")
    _declare(tmp_path, run_id, "p/new.py", write_baseline={"p/new.py": None})
    (tmp_path / "p").mkdir(parents=True, exist_ok=True)
    (tmp_path / "p/new.py").write_bytes(b"created = 1\n")
    assert sibling_claims._own_dirt(tmp_path, run_id, "p/new.py", {"p/new.py": None}) is True
    assert sibling_claims._own_dirt(tmp_path, run_id, "p/new.py", {"p/new.py": "1" * 40}) is False
    assert sibling_claims._own_dirt(tmp_path, run_id, "p/new.py", None) is False
    assert sibling_claims._own_dirt(tmp_path, run_id, "p/other.py", {"p/other.py": None}) is False


def test_b9002c_replay_superseded_halted_run_is_not_unlanded_for_a_landing_on_another_sizing(repo):
    _sizing(repo, SIZING, "superseded")
    _sizing(repo, "state/sizings/land.yaml")
    old = _aged(300, "b9002c")
    _declare(repo, old, "p/a.py")
    _bind(repo, old, "halted", withheld=("p/a.py",))
    own = _aged(10, "996e12")
    _declare(repo, own, "p/a.py")
    _bind(repo, own, None, sizing="state/sizings/land.yaml")
    assert sibling_claims._unlanded_run_ids(repo, own) == []
    assert sibling_claims.sibling_run_claims(repo, own, ["p/a.py"]) == {}
    _sizing(repo, SIZING)
    assert sibling_claims._unlanded_run_ids(repo, own) == [old]


def _corrupt(repo: Path, run_id: str) -> None:
    """Replace the run's halt marker with non-JSON."""
    (repo / "state" / "warp" / run_id / "halted.json").write_text("{not json", encoding="utf-8")


def test_corrupt_marker_run_is_unlanded_and_claims_wanted_paths(repo):
    run = _stamped("c0rr01")
    _declare(repo, run, "p/a.py")
    _corrupt(repo, run)
    assert sibling_claims._unlanded_run_ids(repo, OWN) == [run]
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py", "q/z.py"]) == {"p/a.py": run, "q/z.py": run}


def test_corrupt_marker_holds_shared_dir_candidates(repo):
    run = _stamped("c0rr02")
    _declare(repo, run, "shared/b.py")
    _corrupt(repo, run)
    assert sibling_claims.shared_dir_holds(repo, OWN, ["shared/a.py"], ["shared/dirt.py"]) == ["shared/dirt.py"]


def test_absent_marker_run_without_token_is_not_unlanded(repo):
    run = _stamped("c0rr03")
    _declare(repo, run, "p/a.py")
    assert sibling_claims._unlanded_run_ids(repo, OWN) == []
    assert sibling_claims.sibling_run_claims(repo, OWN, ["p/a.py"]) == {}


def test_one_claim_pass_parses_each_run_manifest_once_over_many_paths(repo, monkeypatch):
    from coordinator_core.ops import warp_run_deliverable as wrd

    _sizing(repo, SIZING)
    runs = [_aged(300 - i * 10, tag) for i, tag in enumerate(("aaaaaa", "bbbbbb", "cccccc"))]
    for i, run_id in enumerate(runs):
        _declare(repo, run_id, f"p/d{i}.py")
        _bind(repo, run_id, "halted")
    calls = []
    real = wrd._manifest_declared_uncached
    monkeypatch.setattr(wrd, "_manifest_declared_uncached", lambda r, root: calls.append(r) or real(r, root))
    wanted = [f"p/d{i}.py" for i in range(3)] + [f"q/other{i}.py" for i in range(40)]
    sibling_claims.sibling_run_claims(repo, OWN, wanted)
    assert calls, "the spy must see the manifest reads"
    assert sorted(calls) == sorted(set(calls)) and set(calls) <= set(runs)
    calls.clear()
    with_scope = sibling_claims.shared_dir_holds(repo, OWN, ["p/own.py"], wanted)
    assert len(calls) == len(set(calls))
    assert with_scope == []
