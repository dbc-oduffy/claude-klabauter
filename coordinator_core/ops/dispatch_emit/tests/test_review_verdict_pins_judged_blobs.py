"""A review verdict pins the blobs it judged: land and terminal commit refuse a verdict whose pinned
``judged_sha256`` differs from the bytes being committed, naming only the changed files; a matching
verdict lands. Carve-out: bytes the reviewer itself wrote (its own touch-sink write, or a ledger row
it applied) and that a clean reverify judged are the reviewed product and keep the pin; any other
non-comment drift still refuses."""

from __future__ import annotations

import pytest

from coordinator_core.ops import review_prep
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.terminal_commit_review import (
    review_owed_refusal,
    verdict_pin_mismatches,
)
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_product import RUN, SELF, repo  # noqa: F401  (fixture)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_landing_integrity import (  # noqa: F401
    _fire,
    _git,
    repo as tc_repo,
)
from coordinator_core.ops.dispatch_emit.tests.test_land_run_landing_remedies import _land as _keeps_land
from coordinator_core.ops.tests.test_review_prep import _call as prep_call, _write as prep_write  # noqa: F401
from coordinator_core.ops.tests.test_review_prep import repo as prep_repo  # noqa: F401  (fixture)
from coordinator_core.session.touch_record import compute_content_hash
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIDECAR = "reviewer-sidecar.md"


def _sidecar(root, judged: dict) -> dict:
    lines = "".join(f"  {p}: '{sha}'\n" for p, sha in judged.items())
    (root / _SIDECAR).write_text(f"---\nverdict: PASS\njudged_sha256:\n{lines}---\nbody\n", encoding="utf-8", newline="\n")
    return {"wave_sidecar_paths": [_SIDECAR]}


def _sha(root, rel):
    return compute_content_hash(root / rel)


def test_matching_pins_are_no_mismatch_and_a_stale_pin_names_only_its_file(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8", newline="\n")
    review = _sidecar(tmp_path, {"a.py": _sha(tmp_path, "a.py"), "b.py": _sha(tmp_path, "b.py")})
    assert verdict_pin_mismatches(tmp_path, review) == []
    assert review_owed_refusal(tmp_path, review) is None

    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8", newline="\n")

    assert verdict_pin_mismatches(tmp_path, review) == ["b.py"]
    reason = review_owed_refusal(tmp_path, review)
    assert reason is not None and "b.py" in reason and "a.py" not in reason


def test_a_backslash_commit_path_still_meets_its_pin(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    target = tmp_path / "d" / "a.py"
    target.write_bytes(b"a = 1"+bytes([10]))
    review = _sidecar(tmp_path, {"d/a.py": _sha(tmp_path, "d/a.py")})
    target.write_bytes(b"a = 2"+bytes([10]))

    assert verdict_pin_mismatches(tmp_path, review, ["d\\a.py"]) == ["d/a.py"]


def test_a_verdict_pinning_nothing_or_naming_an_absent_path_is_never_refused(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    (tmp_path / _SIDECAR).write_text("---\nverdict: PASS\n---\nbody\n", encoding="utf-8", newline="\n")
    assert review_owed_refusal(tmp_path, {"wave_sidecar_paths": [_SIDECAR]}) is None
    review = _sidecar(tmp_path, {"gone.py": "0" * 64})
    assert verdict_pin_mismatches(tmp_path, review) == []


def test_any_verdict_that_judged_the_current_bytes_stands(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 2\n", encoding="utf-8", newline="\n")
    (tmp_path / "old.md").write_text(f"---\njudged_sha256:\n  a.py: '{'1' * 64}'\n---\n", encoding="utf-8", newline="\n")
    (tmp_path / "new.md").write_text(
        f"---\njudged_sha256:\n  a.py: '{_sha(tmp_path, 'a.py')}'\n---\n", encoding="utf-8", newline="\n"
    )
    assert verdict_pin_mismatches(tmp_path, {"wave_sidecar_paths": ["old.md", "new.md"]}) == []
    assert verdict_pin_mismatches(tmp_path, {"wave_sidecar_paths": ["old.md"]}) == ["a.py"]


def test_judged_mismatches_ignores_unpinned_and_unreadable_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    assert review_prep.judged_mismatches(tmp_path, {"a.py": ["x" * 64, _sha(tmp_path, "a.py")]}, ["a.py"]) == []
    assert review_prep.judged_mismatches(tmp_path, {"a.py": 5}, ["a.py", "nope.py"]) == []
    assert review_prep.judged_mismatches(tmp_path, {"a.py": "x" * 64}, ["a.py"]) == ["a.py"]


def test_terminal_commit_refuses_a_stale_pin_then_lands_once_the_pin_matches(tc_repo) -> None:
    repo = tc_repo
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    head = _git(["rev-parse", "HEAD"], repo)
    stale = {"integration_stem": "rev-stem", "slices": 1, **_sidecar(repo, {"ok.py": "0" * 64})}

    out = _fire(repo, "ok.py", inline_review=stale)

    assert out["committed"] is False and "ok.py" in out["error"] and "judged other bytes" in out["error"], out
    assert _git(["rev-parse", "HEAD"], repo) == head

    fresh = {"integration_stem": "rev-stem", "slices": 1, **_sidecar(repo, {"ok.py": _sha(repo, "ok.py")})}
    assert _fire(repo, "ok.py", inline_review=fresh)["committed"] is True


def test_terminal_commit_ignores_a_stale_pin_on_a_file_outside_the_commit_set(tc_repo) -> None:
    repo = tc_repo
    (repo / "ok.py").write_text("x = 1\n", encoding="utf-8", newline="\n")
    (repo / "other.py").write_text("y = 1\n", encoding="utf-8", newline="\n")
    review = {
        "integration_stem": "rev-stem", "slices": 1,
        **_sidecar(repo, {"ok.py": _sha(repo, "ok.py"), "other.py": "0" * 64}),
    }

    out = _fire(repo, "ok.py", inline_review=review)

    assert out["committed"] is True, out


def _land_with(repo, judged_paths: dict):  # noqa: F811
    prod._author(repo, "src/a.py", "a = 1\n")
    prod._author(repo, "src/b.py", "b = 1\n")
    prod._declare(repo, "src/a.py", "src/b.py")
    prod._record_dir(repo)
    review = _sidecar(repo, {p: (sha if sha else _sha(repo, p)) for p, sha in judged_paths.items()})
    return prod._land(repo, inline_review={**prod.REVIEWED, **review})


def test_land_refuses_a_stale_blob_verdict_and_names_only_the_changed_file(repo) -> None:  # noqa: F811
    head = prod._git(["rev-parse", "HEAD"], repo).strip()

    out = _land_with(repo, {"src/a.py": None, "src/b.py": "0" * 64})

    assert out["committed"] is False and out["refused"] == land_run.VERDICT_PIN_STALE, out
    assert out["paths"] == ["src/b.py"] and "src/a.py" not in out["error"]
    assert prod._git(["rev-parse", "HEAD"], repo).strip() == head


def test_land_commits_a_verdict_whose_pins_match_the_blobs(repo) -> None:  # noqa: F811
    out = _land_with(repo, {"src/a.py": None, "src/b.py": None})

    assert out["committed"] is True, out


def _produced_review(prep_repo, *, applied=()):  # noqa: F811
    """A verdict over a REAL ``review.prep`` run: the pins come from the producer, never hand-written."""
    root = prep_repo
    prep_write(root, ["a.py", "b.py"], 3)
    reply = prep_call(root, ["a.py", "b.py"])
    assert reply["error"] is None, reply
    rows = ",".join(
        f'{{"id": "f{i}", "file": "{p}", "before": "x", "after": "b = 2", "status": "applied", "verified": true}}'
        for i, p in enumerate(applied)
    )
    ledger = "## Findings Ledger\n\n```json\n[" + rows + "]\n```\n"
    (root / _SIDECAR).write_text(f"---\nverdict: PASS\n---\n{ledger}", encoding="utf-8", newline="\n")
    return reply, {"wave_sidecar_paths": [_SIDECAR], "prep_sidecar": reply["sidecar_path"]}


def test_review_prep_records_the_sha_of_every_slice_file_it_froze(prep_repo) -> None:  # noqa: F811
    import yaml

    reply, _ = _produced_review(prep_repo)

    sliced = sorted(f for s in reply["slices"] for f in s["files"])
    assert sliced == ["a.py", "b.py"]
    assert reply[review_prep.VERDICT_JUDGED_KEY] == {p: _sha(prep_repo, p) for p in sliced}
    text = (prep_repo / reply["sidecar_path"]).read_text(encoding="utf-8")
    assert yaml.safe_load(text.split("---\n")[1])[review_prep.VERDICT_JUDGED_KEY] == reply[review_prep.VERDICT_JUDGED_KEY]


def test_slice_pins_skips_a_file_the_prep_could_not_read() -> None:
    assert review_prep.slice_pins({"a.py": "x" * 64}, ["a.py", "gone.py", "a.py"]) == {"a.py": "x" * 64}


def test_a_produced_verdict_is_pinned_and_refuses_a_post_review_edit_naming_only_that_file(prep_repo) -> None:  # noqa: F811
    _, review = _produced_review(prep_repo)
    assert verdict_pin_mismatches(prep_repo, review) == []
    assert review_owed_refusal(prep_repo, review) is None

    prep_write(prep_repo, ["a.py"], 5)

    assert verdict_pin_mismatches(prep_repo, review) == ["a.py"]
    reason = review_owed_refusal(prep_repo, review)
    assert reason is not None and "a.py" in reason and "b.py" not in reason


def test_a_file_the_reviewer_fixed_is_not_pinned_to_its_pre_fix_prep_bytes(prep_repo) -> None:  # noqa: F811
    _, review = _produced_review(prep_repo, applied=("b.py",))
    prep_write(prep_repo, ["b.py"], 5)

    assert verdict_pin_mismatches(prep_repo, review) == []


def test_review_owed_refusal_judges_only_the_paths_the_commit_carries(prep_repo) -> None:  # noqa: F811
    _, review = _produced_review(prep_repo)
    prep_write(prep_repo, ["a.py"], 5)

    assert review_owed_refusal(prep_repo, review, ["b.py"]) is None
    assert "a.py" in review_owed_refusal(prep_repo, review, ["a.py"])


def test_a_pinned_file_deleted_after_review_is_no_mismatch(prep_repo) -> None:  # noqa: F811
    _, review = _produced_review(prep_repo)
    (prep_repo / "a.py").unlink()

    assert verdict_pin_mismatches(prep_repo, review) == []


def test_a_prep_older_than_the_pins_pins_nothing(prep_repo) -> None:  # noqa: F811
    reply, review = _produced_review(prep_repo)
    prep = prep_repo / reply["sidecar_path"]
    older = prep.read_text(encoding="utf-8").replace(review_prep.VERDICT_JUDGED_KEY, "other_key")
    prep.write_text(older, encoding="utf-8", newline="\n")
    prep_write(prep_repo, ["a.py"], 5)

    assert verdict_pin_mismatches(prep_repo, review) == []


def test_land_does_not_pin_refuse_a_comment_only_edit_the_drift_gate_tolerates(repo) -> None:
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_post_inventory_keeps as keeps

    keeps._drifted(repo, {"a.py": keeps._PY_SOURCE}, {"a.py": "x = 1\n"})
    review = _sidecar(repo, {"a.py": keeps._sha(keeps._PY_SOURCE)})

    out = _keeps_land(repo, inline_review={**keeps._REVIEWED, **review})

    assert out["committed"] is True, out


def test_land_still_refuses_a_code_edit_after_review(repo) -> None:
    from coordinator_core.ops.dispatch_emit.tests import test_land_run_post_inventory_keeps as keeps

    keeps._drifted(repo, {"a.py": keeps._PY_SOURCE}, {"a.py": "# Reviewed-by: kira\nx = 2\n"})
    review = _sidecar(repo, {"a.py": keeps._sha(keeps._PY_SOURCE)})

    out = _keeps_land(repo, inline_review={**keeps._REVIEWED, **review})

    assert out["committed"] is False, out


def test_terminal_arm_skips_a_pinned_edit_in_an_incomplete_chunk_but_refuses_it_in_a_committed_one(prep_repo) -> None:  # noqa: F811
    from coordinator_core.ops.dispatch_emit import terminal_commit_refusals as refusals
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest

    _, review = _produced_review(prep_repo)
    prep_write(prep_repo, ["a.py"], 5)
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C1", title="t", paths=("a.py",)),
            ChunkCommit(id="C2", title="t", paths=("b.py",)),
        ),
        plan_path=None,
        version=1,
    )

    held = refusals._tests_not_run_refusal(prep_repo, {"incomplete_chunks": ["C1"]}, request, review)
    assert held is None

    both = refusals._tests_not_run_refusal(prep_repo, {"incomplete_chunks": []}, request, review)
    assert both["refused"] == refusals.REFUSED_REVIEW_OWED and "a.py" in both["error"]


def _prep_with_snapshot(root, rel: str, original: bytes) -> dict:
    """A wave sidecar pinning ``rel`` at ``original`` plus the pre-review snapshot the drift proof reads."""
    import json

    snap = root / "rv.pre-review"
    snap.mkdir()
    (snap / "blob0").write_bytes(original)
    (snap / "manifest.json").write_text(json.dumps({"files": {rel: "blob0"}}), encoding="utf-8")
    (root / "rv.review-prep.md").write_text("---\nkind: prep\n---\n", encoding="utf-8", newline="\n")
    (root / rel).write_bytes(original)
    review = _sidecar(root, {rel: _sha(root, rel)})
    review["prep_sidecar"] = "rv.review-prep.md"
    return review


def test_terminal_arm_tolerates_a_comment_only_edit_after_review_but_refuses_a_code_edit(tmp_path: Path) -> None:
    review = _prep_with_snapshot(tmp_path, "a.py", b"a = 1\n")

    (tmp_path / "a.py").write_bytes(b"# a note\na = 1\n")
    assert review_owed_refusal(tmp_path, review, ["a.py"]) is None

    (tmp_path / "a.py").write_bytes(b"# a note\na = 2\n")
    reason = review_owed_refusal(tmp_path, review, ["a.py"])
    assert reason is not None and "a.py" in reason


def test_terminal_arm_ignores_a_stale_pin_outside_the_commit_set(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "b.py").write_text("b = 1\n", encoding="utf-8", newline="\n")
    review = _sidecar(tmp_path, {"a.py": _sha(tmp_path, "a.py"), "b.py": _sha(tmp_path, "b.py")})
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8", newline="\n")

    assert review_owed_refusal(tmp_path, review, ["a.py"]) is None
    assert review_owed_refusal(tmp_path, review, ["b.py"]) is not None


_AGENT = "a0123456789abcdef"
_PASS = {"status_after": "pass", "failing": [], "pre_existing": [], "foreign": []}


def _reviewer_wrote(repo, rel: str, text: str) -> None:
    """The reviewer edits ``rel`` after prep and its own touch sink records the post-edit hash."""
    import hashlib
    import time

    from coordinator_core.session import touch_record
    from coordinator_core.session_hub_path import hub_for_common

    (repo / rel).write_text(text, encoding="utf-8", newline="\n")
    agent_dir = hub_for_common(repo / ".git") / ".agents" / _AGENT
    agent_dir.mkdir(parents=True, exist_ok=True)
    touch_record.append_event(
        touch_record.sink_path(agent_dir), session_id="s", agent_id=_AGENT, verb="T", path=rel,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(), kind="w", timestamp=time.time(),
    )


def _reviewed_with_agent(prep_repo, reverify):
    """A produced verdict whose reviewer sidecar carries its receipt and a ledger that records no applied row."""
    _, review = _produced_review(prep_repo)
    (prep_repo / _SIDECAR).write_text(
        f"---\nverdict: WARN\nreview_receipt:\n  agent_id: '{_AGENT}'\n---\n"
        "## Findings Ledger\n\n```json\n"
        '[{"id": "f1", "file": "b.py", "status": "handed-to-em", "verified": true}]\n```\n',
        encoding="utf-8", newline="\n",
    )
    return {**review, "reverify": reverify, "fixes": 1}


def test_a_reviewer_edit_that_passes_reverify_keeps_the_pin(prep_repo) -> None:  # noqa: F811
    review = _reviewed_with_agent(prep_repo, _PASS)
    _reviewer_wrote(prep_repo, "b.py", "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == []
    assert review_owed_refusal(prep_repo, review) is None


def test_a_reviewer_edit_whose_reverify_failed_still_refuses(prep_repo) -> None:  # noqa: F811
    bad = {"status_after": "fail", "failing": ["t.py::x"], "pre_existing": [], "foreign": []}
    review = _reviewed_with_agent(prep_repo, bad)
    _reviewer_wrote(prep_repo, "b.py", "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == ["b.py"]


def test_a_reviewer_edit_whose_only_failures_pre_existed_keeps_the_pin(prep_repo) -> None:  # noqa: F811
    known = {"status_after": "fail", "failing": ["t.py::x"], "pre_existing": ["t.py::x"], "foreign": []}
    review = _reviewed_with_agent(prep_repo, known)
    _reviewer_wrote(prep_repo, "b.py", "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == []


def test_a_non_reviewer_edit_after_the_reviewer_wrote_still_refuses(prep_repo) -> None:  # noqa: F811
    review = _reviewed_with_agent(prep_repo, _PASS)
    _reviewer_wrote(prep_repo, "b.py", "b = 2\n")
    (prep_repo / "b.py").write_text("b = 3\n", encoding="utf-8", newline="\n")

    assert verdict_pin_mismatches(prep_repo, review) == ["b.py"]


def test_a_non_reviewer_edit_to_an_unwritten_file_still_refuses_under_a_clean_reverify(prep_repo) -> None:  # noqa: F811
    review = _reviewed_with_agent(prep_repo, _PASS)
    _reviewer_wrote(prep_repo, "b.py", "b = 2\n")
    prep_write(prep_repo, ["a.py"], 5)

    assert verdict_pin_mismatches(prep_repo, review) == ["a.py"]


def test_a_ledger_row_naming_an_absolute_path_still_exempts_the_pre_fix_pin(prep_repo) -> None:  # noqa: F811
    _, review = _produced_review(prep_repo, applied=(str(prep_repo / "b.py").replace("\\", "/"),))
    prep_write(prep_repo, ["b.py"], 5)

    assert verdict_pin_mismatches(prep_repo, review) == []


def _reverified(prep_repo, reverify, now_text):  # noqa: F811
    """A produced verdict (pinned before the write) whose reviewer wrote nothing, then ``b.py`` becomes
    ``now_text`` and the re-verify record ``reverify`` is carried."""
    _, review = _produced_review(prep_repo)
    (prep_repo / "b.py").write_text(now_text, encoding="utf-8", newline="\n")
    return {**review, "reverify": reverify}


def _sha_of(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_reverify_edit_recorded_by_hash_under_a_clean_reverify_keeps_the_pin(prep_repo) -> None:  # noqa: F811
    review = _reverified(prep_repo, {**_PASS, "edited": ["b.py"], "edited_sha256": {"b.py": _sha_of("b = 2\n")}}, "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == []
    assert review_owed_refusal(prep_repo, review) is None


def test_a_foreign_edit_after_the_reverify_recorded_hash_still_refuses(prep_repo) -> None:  # noqa: F811
    review = _reverified(prep_repo, {**_PASS, "edited": ["b.py"], "edited_sha256": {"b.py": _sha_of("b = 2\n")}}, "b = 3\n")

    assert verdict_pin_mismatches(prep_repo, review) == ["b.py"]


def test_a_reverify_edit_whose_reverify_failed_still_refuses(prep_repo) -> None:  # noqa: F811
    bad = {"status_after": "fail", "failing": ["t.py::x"], "pre_existing": [], "foreign": []}
    review = _reverified(prep_repo, {**bad, "edited": ["b.py"], "edited_sha256": {"b.py": _sha_of("b = 2\n")}}, "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == ["b.py"]


def test_a_file_the_reverify_did_not_edit_still_refuses_under_a_clean_reverify(prep_repo) -> None:  # noqa: F811
    review = _reverified(prep_repo, {**_PASS, "edited": ["a.py"], "edited_sha256": {"a.py": _sha_of("b = 2\n")}}, "b = 2\n")

    assert verdict_pin_mismatches(prep_repo, review) == ["b.py"]
