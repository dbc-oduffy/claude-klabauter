"""The f8207b shape: a PARTIAL report marked ``post-commit: bug_close <id>`` lands, and the landing closes the
bug row at the product sha and discharges ``bug_close``; without a marker the act stays owed."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import bug_close, requirements_ledger as rl, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import ASK_MANIFEST_MARKER, RUN_DIR_ROOT, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.tests._review_fixture import reviewed
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_register_ship_in_run import (  # noqa: F401
    _PLAN, _SID, _SIZING, _git, _row, _sizing_text, repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
BUG = "0123456789ab"
BUG_REL = f"state/bug-backlog/2026-10-26-x-{BUG}.yaml"
OPEN_ROW = "created: 2026-10-26\ntitle: t\nbody: b\nstatus: open\nsurface: s\nseverity: P1\n"
CLOSED_ELSEWHERE = (
    "created: 2026-10-26\ntitle: t\nbody: b\nstatus: closed\nclosed_at: 2026-10-25\n"
    'resolution: "fixed at 1111111111"\nsurface: s\nseverity: P1\n'
)
VERDICT = {"items": [{"id": "R1", "status": "post_commit", "wired": True, "post_commit_acts": ["bug_close"]}]}
PARTIAL_MARKED = f"PARTIAL: X1\n<exit-status>PARTIAL</exit-status>\n{bug_close.marker_line(BUG)}\n"
PARTIAL_PROSE = f"PARTIAL: X1\nNot done: close bug {BUG} with the landing sha\n<exit-status>PARTIAL</exit-status>\n"


@pytest.fixture(autouse=True)
def _no_review_write_source(monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edits

    monkeypatch.setattr(review_edits, "review_writes", lambda *a, **k: {})


def _fire(repo, report: str, bug_text: str, *, incomplete=("X1",), ask: str = "", rationale: str = "") -> dict:
    row = {**_row("R1"), **({"source_text": ask} if ask else {})}
    sizing = _sizing_text([row])
    if rationale:
        sizing += f"em_analysis:\n  em_rationale: {json.dumps(rationale)}\n"
    (repo / _SIZING).write_text(sizing, encoding="utf-8")
    (repo / BUG_REL).parent.mkdir(parents=True, exist_ok=True)
    (repo / BUG_REL).write_text(bug_text, encoding="utf-8")
    (repo / "r.md").write_text(report, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "shape"], repo)
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(ChunkCommit(id="X1", title="t", paths=("a.py",), report="r.md"),), plan_path=_PLAN
    )
    run_dir = repo / RUN_DIR_ROOT / RUN
    run_dir.mkdir(parents=True)
    (run_dir / "commit-request.txt").write_text(render_marker(request) + "\n", encoding="utf-8")
    manifest = StageManifest(
        run_dir=f"{RUN_DIR_ROOT}/{RUN}", rows=(), review_declared_paths=(),
        marker_path=f"{RUN_DIR_ROOT}/{RUN}/commit-request.txt",
    )
    (run_dir / "manifest.json").write_text(json.dumps(manifest.to_json()), encoding="utf-8")
    (repo / "run.mjs").write_text(
        f"// emitted\n{ASK_MANIFEST_MARKER}{RUN_DIR_ROOT}/{RUN}/manifest.json\n", encoding="utf-8"
    )
    rl.start(repo, RUN, all_of_pm_ask=False)
    rl.bind(repo, RUN, _SIZING)
    review = {
        "integration_stem": "pln-x-123456.review-wave-bookkeeping", "slices": 1, "fixes": 0,
        "plan_id": "pln-x-123456", "prep_sidecar": None, "wave_sidecar_paths": [],
        "wave_reviews": reviewed()["wave_reviews"],
        "prep": {"run_base_sha": "a" * 40, "product_files": 1, "foreign_claims": [], "slice_files": ["a.py"]},
        "delivery": {"verdict": "PASS", "product_files": 1, "claims_unbacked": 0},
        "tests": {"status": "not_run", "run": 0, "failed": 0, "sidecar": None},
        "criterion": {"status": "indeterminate", "observation": "o", "sidecar": "j.md", "register_rows": []},
    }
    return terminal_commit._handler(
        {"inline_review": review, "script_path": "run.mjs", "incomplete_chunks": list(incomplete),
         "session_id": _SID, "ledger_verdict": VERDICT},
        repo_root=repo / ".git",
    )


def test_a_marked_partial_lands_and_the_row_closes_at_the_product_sha(repo):
    out = _fire(repo, PARTIAL_MARKED, OPEN_ROW)

    assert out["committed"] is True and "coded_stamp_error" not in out, out
    assert "a.py" in set(_git(["show", "--name-only", "--format=", out["sha"]], repo).split())
    assert out["bugs_closed"] == {BUG: BUG_REL} and "bug_close_refused" not in out
    text = (repo / BUG_REL).read_text(encoding="utf-8")
    assert "status: closed" in text and f"fixed at {out['sha'][:10]}" in text
    assert BUG_REL in set(_git(["show", "--name-only", "--format=", "HEAD"], repo).split())
    assert _git(["status", "--porcelain", "--", BUG_REL], repo) == ""
    assert "post_commit_owed" not in out


def test_without_a_marker_the_row_stays_open_and_bug_close_is_owed(repo):
    out = _fire(repo, PARTIAL_PROSE, OPEN_ROW, incomplete=())

    assert out["committed"] is True, out
    assert "bugs_closed" not in out
    assert "status: open" in (repo / BUG_REL).read_text(encoding="utf-8")
    assert [o["acts"] for o in out["post_commit_owed"]] == [["bug_close"]]


UNMARKED_DONE = "DONE: X1\n<exit-status>DONE</exit-status>\n"


def test_a_bug_id_named_only_in_the_ask_closes_at_the_landing_sha(repo):
    out = _fire(repo, UNMARKED_DONE, OPEN_ROW, incomplete=(), ask=f"Close bug row {BUG} at its fix")

    assert out["committed"] is True, out
    assert out["bugs_closed"] == {BUG: BUG_REL} and "bug_close_refused" not in out
    text = (repo / BUG_REL).read_text(encoding="utf-8")
    assert "status: closed" in text and f"fixed at {out['sha'][:10]}" in text
    assert "post_commit_owed" not in out


def test_a_bug_id_named_only_in_em_rationale_closes_at_the_landing_sha(repo):
    out = _fire(repo, UNMARKED_DONE, OPEN_ROW, incomplete=(), rationale=f"this run closes {BUG}")

    assert out["bugs_closed"] == {BUG: BUG_REL}
    assert "status: closed" in (repo / BUG_REL).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "ask,rationale",
    [
        (f"unrelated to {BUG}", ""),
        (f"distinct from bug row {BUG}", ""),
        (f"bug {BUG} is still open", ""),
        ("", f"follow-up on row {BUG}"),
        (f"do not close bug {BUG}", ""),
    ],
)
def test_a_mention_without_closure_intent_leaves_the_row_open(repo, ask, rationale):
    out = _fire(repo, UNMARKED_DONE, OPEN_ROW, incomplete=(), ask=ask, rationale=rationale)

    assert out["committed"] is True, out
    assert "bugs_closed" not in out
    assert "status: open" in (repo / BUG_REL).read_text(encoding="utf-8")


def test_an_id_naming_no_bug_row_closes_nothing(repo):
    out = _fire(repo, UNMARKED_DONE, OPEN_ROW, incomplete=(), ask="Close bug row ffffffffffff")

    assert "bugs_closed" not in out
    assert "status: open" in (repo / BUG_REL).read_text(encoding="utf-8")


def test_a_row_already_closed_elsewhere_is_left_byte_identical_and_discharges(repo):
    out = _fire(repo, PARTIAL_MARKED, CLOSED_ELSEWHERE)

    assert out["committed"] is True, out
    assert out["bugs_closed"] == {BUG: BUG_REL}
    assert (repo / BUG_REL).read_text(encoding="utf-8") == CLOSED_ELSEWHERE
    assert "post_commit_owed" not in out


def test_sole_run_review_writes_keeps_in_worktree_paths_and_drops_escapes(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edit_source

    monkeypatch.setattr(terminal_commit, "_review_since", lambda *a, **k: 1.0)
    monkeypatch.setattr(
        review_edit_source, "sole_run_attested_writes",
        lambda *a, **k: frozenset({".\\docs\\a.md", "../out.md", "C:/x.md"}),  # abs-path-ok: drive-qualified escape input
    )

    got = terminal_commit._sole_run_review_writes(tmp_path, _SID, RUN, None)

    assert got == frozenset({"docs/a.md"})


def test_sole_run_review_writes_attributes_nothing_when_the_store_is_unreadable(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import review_edit_source

    def unreadable(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(terminal_commit, "_review_since", lambda *a, **k: 1.0)
    monkeypatch.setattr(review_edit_source, "sole_run_attested_writes", unreadable)

    assert terminal_commit._sole_run_review_writes(tmp_path, _SID, RUN, None) == frozenset()
