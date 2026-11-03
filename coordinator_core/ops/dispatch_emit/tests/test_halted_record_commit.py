"""The halt-record commit never stages a review literal another halted run withholds, and fails closed
when the hold cannot be computed."""

from __future__ import annotations

import pytest

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops.dispatch_emit import halted_run, land_run, review_edits, run_artifacts, sibling_claims
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _call,
    _git,
    _landed,
    _write_record,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LITERAL = "pkg/twin.py"


def _reviewed(repo, monkeypatch) -> None:
    _write_record(repo, RUN)
    (repo / "pkg").mkdir()
    (repo / LITERAL).write_text("# reviewed\n", encoding="utf-8")
    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {LITERAL: "agent-1"})


def _spend(repo, sha: str) -> str:
    from coordinator_core.ops.dispatch_emit import run_token

    token = run_token.mint(repo, RUN, "run.mjs")
    assert run_token.redeem(repo, RUN, token, "run.mjs")
    run_token.consume(repo, RUN, landed={"sha": sha, "paths": ["product.py"]})
    return token


def test_a_spent_token_whose_landing_is_on_head_answers_already_landed(repo):
    from coordinator_core.ops.dispatch_emit import run_token

    _write_record(repo, RUN)
    head = _git(["rev-parse", "HEAD"], repo).strip()
    token = _spend(repo, head)
    assert not run_token.halt_admits(repo, RUN, token, "run.mjs")

    out = _call(repo, run_token=token)

    assert not out.get("error") and not out.get("refused")
    assert out["committed"] is True and out["sha"] == head
    assert out["already_landed"] is True and out["paths"] == ["product.py"]
    assert _git(["rev-parse", "HEAD"], repo).strip() == head


def test_a_spent_token_still_refuses_a_wrong_token_and_an_off_head_sha(repo):
    _write_record(repo, RUN)
    token = _spend(repo, "f" * 40)

    assert _call(repo, run_token=token)["refused"] == "run-token"
    assert _call(repo, run_token="0" * 32)["refused"] == "run-token"


def test_a_foreign_held_review_literal_is_not_staged(repo, monkeypatch):
    _reviewed(repo, monkeypatch)
    monkeypatch.setattr(
        land_run, "foreign_halted_withheld",
        lambda _root, _run, _plan, paths: {p: "other-halted" for p in paths if p == LITERAL},
    )

    out = _call(repo)

    assert out["sha"] is None or LITERAL not in _landed(repo, out, "--name-only")
    assert out["review_edits_unlanded"] == {LITERAL: "halted-run"}
    assert out["withheld"][LITERAL] == "foreign-halted-withheld"


def test_an_oserror_reading_the_halt_record_excludes_the_review_literal(repo, monkeypatch):
    _reviewed(repo, monkeypatch)

    def unreadable(*_a, **_k):
        raise OSError("halt record unreadable")

    # held_paths falls back to the raw record reader when the normalizer fails, so the unreadable
    # record is simulated at that reader too.
    monkeypatch.setattr(warp_run_deliverable, "halted_withheld_paths", unreadable)
    monkeypatch.setattr(halted_run, "halted_withheld_strict", unreadable)
    monkeypatch.setattr(sibling_claims, "_unlanded_run_ids", lambda *_a, **_k: ["ask-x"])
    monkeypatch.setattr(warp_run_deliverable, "_halted", lambda *_a, **_k: True)

    out = _call(repo)

    assert out["sha"] is None or LITERAL not in _landed(repo, out, "--name-only")
    assert out["withheld"][LITERAL] == "foreign-halted-withheld"


def _ignore_run_state(repo) -> None:
    exclude = repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text(encoding="utf-8") + "state/warp/\n", encoding="utf-8")


def test_an_ignored_run_record_is_named_in_the_reply_and_the_halt_marker_and_never_committed(repo):
    import json

    _ignore_run_state(repo)
    rec = _write_record(repo, RUN)
    prompt = f"state/warp/{RUN}/prompts/review-1.md"
    (rec / "prompts").mkdir()
    (rec / "prompts" / "review-1.md").write_text("p\n", encoding="utf-8")
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert out["committed"] is True
    assert _landed(repo, out, "--name-only") == [run_artifacts.outcome_rel(RUN)]
    named = out["run_artifacts"]["ignored_excluded"]
    assert prompt in named and f"state/warp/{RUN}/run.json" in named
    marker = json.loads((rec / halted_run.HALTED_FILE).read_text(encoding="utf-8"))
    assert prompt in marker[halted_run.HALT_RUN_ARTIFACTS_FIELD]["ignored_excluded"]
    assert _git(["rev-parse", "HEAD"], repo).strip() != head
    assert not [p for p in _git(["ls-files"], repo).splitlines() if p.startswith("state/warp")]


def test_a_tracked_legacy_run_record_edit_is_named_not_committed(repo):
    _ignore_run_state(repo)
    legacy = f"state/warp/{RUN}/ask.md"
    _write_record(repo, RUN, ("ask.md",))
    _git(["add", "-f", legacy], repo)
    _git(["commit", "-q", "-m", "legacy tracked record"], repo)
    _write_record(repo, RUN)
    (repo / legacy).write_text("edited after the tracked commit, longer\n", encoding="utf-8")

    out = _call(repo)

    assert not out.get("error") and not out.get("refused"), out
    assert out["committed"] is True
    assert _landed(repo, out, "--name-only") == [run_artifacts.outcome_rel(RUN)]
    assert legacy in out["run_artifacts"]["ignored_excluded"]


def test_a_record_file_written_during_the_op_is_never_relanded(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc

    _ignore_run_state(repo)
    _write_record(repo, RUN)
    rec = repo / "state" / "warp" / RUN
    real = hrc._baton_cascade

    def late_write(*a, **k):
        (rec / "withheld-prints.json").write_text("{}\n", encoding="utf-8")
        return real(*a, **k)

    monkeypatch.setattr(hrc, "_baton_cascade", late_write)
    head = _git(["rev-parse", "HEAD"], repo).strip()

    out = _call(repo)

    assert out["committed"] is True
    assert _landed(repo, out, "--name-only") == [run_artifacts.outcome_rel(RUN)]
    assert out["relanded"] == []
    assert _git(["rev-parse", "HEAD"], repo).strip() != head
    assert "run_artifact_residue" not in out
