"""wiring_contract: one part per status, built-path, evidence classifier, evidence sha, absent key."""

from __future__ import annotations

import subprocess

import pytest
from pathlib import Path

pytestmark = pytest.mark.complete

from coordinator_core.ops import wiring_contract as wc
from coordinator_core.ops.dispatch_emit.run_truth_contract import STATUS_UNWIRED
from coordinator_core.win_portability import no_console_creationflags

H = "a" * 40
_REAL = "state/warp/run/receipts/r.json"


def _part(tmp_path, **over):
    (tmp_path / "x.py").write_text("")
    part = {
        "id": "p",
        "status": "wired",
        "built_path": "x.py",
        "runs_evidence": [wc.evidence_entry(_REAL, H, False)],
    }
    part.update(over)
    return part


def _one(tmp_path, **over):
    return wc.part_verdicts({"wiring": [_part(tmp_path, **over)]}, tmp_path, H)[0]


def test_wired_part_passes(tmp_path: Path) -> None:
    fm = {"wiring": [_part(tmp_path)]}
    assert wc.part_verdicts(fm, tmp_path, H)[0]["status"] == "wired"
    assert wc.refusal(fm, tmp_path, H) is None


def test_not_built_and_blocked_verbatim(tmp_path: Path) -> None:
    assert _one(tmp_path, status="not_built")["status"] == "not_built"
    v = _one(tmp_path, status="blocked", blocked_on="DoE schema")
    assert (v["status"], v["reason"]) == ("blocked", "DoE schema")


def test_missing_built_path_is_unwired(tmp_path: Path) -> None:
    v = _one(tmp_path, built_path="nope.py")
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "built-path-missing")
    assert _one(tmp_path, built_path="")["reason"] == "built-path-missing"


def test_empty_runs_evidence_is_stub_only(tmp_path: Path) -> None:
    v = _one(tmp_path, runs_evidence=[])
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "stub-only-evidence")


@pytest.mark.parametrize(
    "path",
    [
        "a/fixtures/r.json", "a/fixture/r.json", "a/stubs/r.json", "a/stub/r.json",
        "a/fakes/r.json", "a/fake/r.json", "a/mocks/r.json", "a/mock/r.json",
        "a/conftest.py", "a/stub_r.json", "a/fake_r.json", "a/mock_r.json",
        "a\\fixtures\\r.json",
    ],
)
def test_each_stub_pattern_is_stub_only(tmp_path: Path, path) -> None:
    assert wc.is_stub_evidence(path)
    ev = [wc.evidence_entry(path, H, False)]
    assert _one(tmp_path, runs_evidence=ev)["reason"] == "stub-only-evidence"


def test_real_evidence_among_stubs_is_wired(tmp_path: Path) -> None:
    ev = [
        wc.evidence_entry("a/fixtures/r.json", H, False),
        wc.evidence_entry("state/r.json", H, False),
    ]
    assert _one(tmp_path, runs_evidence=ev)["status"] == "wired"


@pytest.mark.parametrize("path", ["state/r.json", "a/fixturesx/r.json", "a/mocking.json"])
def test_non_stub_paths(path) -> None:
    assert not wc.is_stub_evidence(path)


def test_absent_wiring_key_is_not_refused(tmp_path: Path) -> None:
    assert wc.refusal({}, tmp_path, H) is None
    assert wc.part_verdicts({}, tmp_path, H) == []


def test_non_list_wiring_is_malformed_refusal(tmp_path: Path) -> None:
    assert wc.refusal({"wiring": "oops"}, tmp_path, H) == "wiring: malformed"
    assert wc.refusal({"wiring": {"id": "a"}}, tmp_path, H) == "wiring: malformed"


def test_refusal_sentence_names_each_bad_part(tmp_path: Path) -> None:
    fm = {"wiring": [_part(tmp_path), _part(tmp_path, id="q", status="not_built")]}
    assert wc.refusal(fm, tmp_path, H) == "wiring: q not_built (declared)"


def test_malformed_part_does_not_crash(tmp_path: Path) -> None:
    assert wc.refusal({"wiring": ["junk"]}, tmp_path, H).startswith("wiring: ? malformed")


def test_evidence_entry_shape() -> None:
    assert wc.evidence_entry("p", H, True) == {"path": "p", "sha": H, "dirty": True}


def test_evidence_from_a_different_sha_does_not_count(tmp_path: Path) -> None:
    ev = [wc.evidence_entry(_REAL, "b" * 40, False)]
    v = _one(tmp_path, runs_evidence=ev)
    assert (v["status"], v["reason"]) == (STATUS_UNWIRED, "evidence-sha-mismatch")
    fm = {"wiring": [_part(tmp_path, runs_evidence=ev)]}
    assert "evidence-sha-mismatch" in wc.refusal(fm, tmp_path, H)


def test_dirty_evidence_does_not_count(tmp_path: Path) -> None:
    ev = [wc.evidence_entry(_REAL, H, True)]
    assert _one(tmp_path, runs_evidence=ev)["reason"] == "evidence-dirty-tree"
    missing_flag = [{"path": _REAL, "sha": H}]
    assert _one(tmp_path, runs_evidence=missing_flag)["reason"] == "evidence-dirty-tree"


def test_bare_string_or_blank_sha_is_sha_missing(tmp_path: Path) -> None:
    assert _one(tmp_path, runs_evidence=[_REAL])["reason"] == "evidence-sha-missing"
    blank = [wc.evidence_entry(_REAL, "", False)]
    assert _one(tmp_path, runs_evidence=blank)["reason"] == "evidence-sha-missing"


def test_off_route_with_reason_is_reported_never_refused(tmp_path: Path) -> None:
    fm = {"wiring": [_part(tmp_path), {"id": "inj", "status": "off_route", "off_route_reason": "example-game-repo"}]}
    assert wc.refusal(fm, tmp_path, H) is None
    v = wc.part_verdicts(fm, tmp_path, H)[1]
    assert (v["id"], v["status"], v["reason"]) == ("inj", "off_route", "example-game-repo")


@pytest.mark.parametrize("why", [None, "", "   ", 3])
def test_off_route_without_reason_is_malformed_and_refuses(tmp_path: Path, why) -> None:
    part = {"id": "inj", "status": "off_route"}
    if why is not None:
        part["off_route_reason"] = why
    fm = {"wiring": [part]}
    assert wc.part_verdicts(fm, tmp_path, H)[0]["status"] == "malformed"
    assert "wiring: inj malformed" in wc.refusal(fm, tmp_path, H)


def test_off_route_consumes_not_required_and_produces_do_not_feed(tmp_path: Path) -> None:
    off = {"id": "inj", "status": "off_route", "off_route_reason": "r", "consumes": ["x"], "produces": ["y"]}
    assert wc.refusal({"wiring": [off]}, tmp_path, H) is None
    fm = {"wiring": [off, _part(tmp_path, id="B", consumes=["y"])]}
    msg = wc.refusal(fm, tmp_path, H)
    assert "B" in msg and "unfed-consumes" in msg and "y" in msg


def _coverage(tmp_path, spine, ids=("R1", "R2", "R3"), exclude=()):
    return wc.register_coverage_refusal(spine, [{"id": i} for i in ids], tmp_path, exclude)


def test_register_coverage_names_unclaimed_and_writes_absent(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    spine = [
        {"id": "C1", "writes": ["a.py"], "register_rows": ["R1"]},
        {"id": "C2", "writes": ["gone.py"], "register_rows": ["R3"]},
    ]
    assert _coverage(tmp_path, spine) == (
        "register: R2 not_built (unclaimed); register: R3 not_built (writes-absent)"
    )


def test_register_coverage_passes_when_every_row_has_a_present_write(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    spine = [{"id": "C1", "writes": ["gone.py", "a.py"], "register_rows": ["R1", "R2"]}]
    assert _coverage(tmp_path, spine, ids=("R1", "R2")) is None


def test_register_coverage_row_with_empty_writes_claims_nothing_built(tmp_path: Path) -> None:
    spine = [{"id": "C1", "writes": [], "register_rows": ["R1"]}]
    assert _coverage(tmp_path, spine, ids=("R1",)) == "register: R1 not_built (writes-absent)"


def test_register_coverage_ruled_out_row_and_wont_do_claimant(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    spine = [{"id": "C1", "writes": ["a.py"], "register_rows": ["R2"], "disposition": "wont_do"}]
    assert _coverage(tmp_path, spine, ids=("R2",), exclude=("R2",)) is None
    assert _coverage(tmp_path, spine, ids=("R2",)) == "register: R2 not_built (unclaimed)"


def test_one_good_entry_outweighs_a_stale_one(tmp_path: Path) -> None:
    ev = [
        wc.evidence_entry(_REAL, "b" * 40, False),
        wc.evidence_entry(_REAL, H, False),
    ]
    assert _one(tmp_path, runs_evidence=ev)["status"] == "wired"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(root: Path, name: str, text: str) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"touch {name}")
    return _git(root, "rev-parse", "HEAD")


def _ancestry_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "test")
    _commit(root, "x.py", "v1")
    _commit(root, "inputs/in.json", "v1")
    return _git(root, "rev-parse", "HEAD")


def _anc_verdict(root: Path, evidence_sha: str, **over) -> dict:
    head = _git(root, "rev-parse", "HEAD")
    part = _part(
        root,
        consumes=["inputs/in.json"],
        consumes_external=["inputs/in.json"],
        runs_evidence=[wc.evidence_entry(_REAL, evidence_sha, False)],
        **over,
    )
    return wc.part_verdicts({"wiring": [part]}, root, head)[0]


def test_ancestor_sha_with_unrelated_later_commits_counts(tmp_path: Path) -> None:
    evidence_sha = _ancestry_repo(tmp_path)
    _commit(tmp_path, "unrelated/a.txt", "foreign")
    _commit(tmp_path, "unrelated/b.txt", "foreign")
    v = _anc_verdict(tmp_path, evidence_sha)
    assert (v["status"], v["reason"]) == ("wired", "")


def test_ancestor_sha_with_changed_built_path_or_input_is_refused_naming_paths(
    tmp_path: Path,
) -> None:
    evidence_sha = _ancestry_repo(tmp_path)
    _commit(tmp_path, "x.py", "v2")
    _commit(tmp_path, "inputs/in.json", "v2")
    _commit(tmp_path, "unrelated/a.txt", "foreign")
    v = _anc_verdict(tmp_path, evidence_sha)
    assert v["status"] == STATUS_UNWIRED
    assert v["reason"] == "covered-paths-changed: inputs/in.json, x.py"


def test_changed_consumes_directory_input_is_refused(tmp_path: Path) -> None:
    evidence_sha = _ancestry_repo(tmp_path)
    _commit(tmp_path, "inputs/deep/new.json", "v1")
    part = _part(
        tmp_path,
        consumes=["inputs"],
        consumes_external=["inputs"],
        runs_evidence=[wc.evidence_entry(_REAL, evidence_sha, False)],
    )
    head = _git(tmp_path, "rev-parse", "HEAD")
    v = wc.part_verdicts({"wiring": [part]}, tmp_path, head)[0]
    assert v["reason"] == "covered-paths-changed: inputs/deep/new.json"


def test_non_ancestor_sha_is_refused(tmp_path: Path) -> None:
    base = _ancestry_repo(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "side")
    side_sha = _commit(tmp_path, "unrelated/side.txt", "side")
    _git(tmp_path, "checkout", "-q", "-")
    _commit(tmp_path, "unrelated/main.txt", "main")
    v = _anc_verdict(tmp_path, side_sha)
    assert v["status"] == STATUS_UNWIRED
    assert v["reason"].startswith("evidence-sha-mismatch")
    assert base != side_sha


def test_dirty_ancestor_evidence_is_still_refused(tmp_path: Path) -> None:
    evidence_sha = _ancestry_repo(tmp_path)
    _commit(tmp_path, "unrelated/a.txt", "foreign")
    head = _git(tmp_path, "rev-parse", "HEAD")
    ev = [wc.evidence_entry(_REAL, evidence_sha, True)]
    v = wc.part_verdicts({"wiring": [_part(tmp_path, runs_evidence=ev)]}, tmp_path, head)[0]
    assert v["reason"] == "evidence-dirty-tree"


def test_sha_equal_to_head_spawns_no_git(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.ceremony import git_native

    def _boom(*a, **k):
        raise AssertionError("git spawned for sha == HEAD")

    monkeypatch.setattr(git_native, "_git", _boom)
    assert _one(tmp_path)["status"] == "wired"


def test_distinct_sha_costs_one_spawn_across_parts(tmp_path: Path, monkeypatch) -> None:
    from coordinator_core.ops.ceremony import git_native

    evidence_sha = _ancestry_repo(tmp_path)
    _commit(tmp_path, "unrelated/a.txt", "foreign")
    head = _git(tmp_path, "rev-parse", "HEAD")
    real, calls = git_native._git, []

    def _counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(git_native, "_git", _counting)
    ev = [wc.evidence_entry(_REAL, evidence_sha, False)]
    parts = [_part(tmp_path, id=f"p{i}", runs_evidence=ev) for i in range(3)]
    verdicts = wc.part_verdicts({"wiring": parts}, tmp_path, head)
    assert [v["status"] for v in verdicts] == ["wired"] * 3
    assert len(calls) == 1


def _seam(tmp_path, *parts):
    return {"wiring": [_part(tmp_path, id=pid, **keys) for pid, keys in parts]}


def test_unfed_consumes_is_refused_naming_part_and_entry(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}))
    msg = wc.refusal(fm, tmp_path, H)
    assert "unfed-consumes" in msg and "A" in msg and "x" in msg


def test_consumes_fed_by_another_part_is_accepted(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}), ("B", {"produces": ["x"]}))
    assert wc.refusal(fm, tmp_path, H) is None


def test_part_cannot_feed_itself(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"consumes": ["x"], "produces": ["x"]}))
    assert "unfed-consumes" in wc.refusal(fm, tmp_path, H)


def test_consumes_external_is_exempt(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"consumes": ["x"], "consumes_external": ["x", "plan-9"]}))
    assert wc.refusal(fm, tmp_path, H) is None


def test_unconsumed_produces_is_reported_never_refused(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"produces": ["out"]}))
    assert wc.refusal(fm, tmp_path, H) is None
    assert wc.unconsumed_produces(fm) == [("A", "out")]


def test_legacy_parts_without_seam_keys_unchanged(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {}), ("B", {}))
    assert wc.refusal(fm, tmp_path, H) is None
    assert wc.unfed_consumes(fm) == [] and wc.unconsumed_produces(fm) == []


def test_unfed_consumes_joins_other_part_refusals(tmp_path: Path) -> None:
    fm = _seam(tmp_path, ("A", {"consumes": ["x"]}), ("B", {"status": "not_built"}))
    msg = wc.refusal(fm, tmp_path, H)
    assert "B not_built (declared)" in msg and "unfed-consumes" in msg


@pytest.mark.parametrize("key", ["consumes", "produces", "consumes_external"])
@pytest.mark.parametrize("value", ["x", {"a": 1}, [1]])
def test_non_list_seam_key_is_malformed(tmp_path: Path, key, value) -> None:
    v = _one(tmp_path, **{key: value})
    assert v["status"] == "malformed" and key in v["reason"]
    assert wc.refusal({"wiring": [_part(tmp_path, **{key: value})]}, tmp_path, H)
