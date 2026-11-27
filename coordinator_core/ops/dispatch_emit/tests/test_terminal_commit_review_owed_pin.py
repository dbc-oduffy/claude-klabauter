"""dispatch.terminal_commit refuses a landing whose standing REQUIRES_CHANGES review still owes findings.

Drives the real handler at default arguments against a temp git repo; the review-owed refusal
(`terminal_commit_review.review_owed_refusal`) is reached through `_tests_not_run_refusal`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SIDECAR = "wave-reviewer.md"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _sidecar(verdict: str) -> str:
    ledger = (
        '[{"id": "F1", "status": "open", "severity": "ASK", "file": "a.py"},'
        ' {"id": "F2", "status": "open", "severity": "ASK", "file": "a.py"}]'
    )
    return f"---\nverdict: {verdict}\n---\n# review\n\n## Findings Ledger\n\n```json\n{ledger}\n```\n"


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    return root


def _fire(repo: Path, verdict: str, sidecar: str | None = None, claim: bool = True) -> dict:
    (repo / "a.py").write_text("a\n", encoding="utf-8")
    (repo / _SIDECAR).write_text(_sidecar(verdict) if sidecar is None else sidecar, encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C3", title="t3", paths=("a.py",)),))
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    review = {
        "integration_stem": "rev-stem", "slices": 1, "fixes": 0,
        "wave_sidecar_paths": [_SIDECAR],
        "wave_reviews": (
            [{"agent_type": "coordinator:reviewer", "verdict": verdict, "sidecar_path": _SIDECAR}] if claim else []
        ),
    }
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": review},
        repo_root=repo / ".git",
    )


def test_standing_requires_changes_with_owed_findings_refuses_the_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "REQUIRES_CHANGES")

    assert out.get("committed") is not True, out
    assert not out.get("sha"), out
    assert out.get("refused") == "review-owed", out
    assert "REQUIRES_CHANGES" in out["error"]
    assert f"{_SIDECAR}:F1" in out["error"] and f"{_SIDECAR}:F2" in out["error"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def _assert_review_owed(repo: Path, out: dict, head: str) -> None:
    assert out.get("committed") is not True, out
    assert not out.get("sha"), out
    assert out.get("refused") == "review-owed", out
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_the_same_ledger_under_a_passing_verdict_commits(repo):
    out = _fire(repo, "PASS")

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_blocked_verdict_with_owed_findings_refuses_the_commit(repo):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, "BLOCKED")

    _assert_review_owed(repo, out, head)
    assert f"{_SIDECAR}:F1" in out["error"]


@pytest.mark.parametrize("verdict", ["REQUIRES_CHANGES", "BLOCKED", "Requires Changes"])
def test_standing_verdict_with_no_ledger_rows_refuses_the_commit(repo, verdict):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, verdict, sidecar=f"---\nverdict: {verdict}\n---\n# review\n\nFinding: route.py import is broken.\n")

    _assert_review_owed(repo, out, head)
    assert "no-ledger-rows" in out["error"]


@pytest.mark.parametrize("verdict", ["REQUIRES_CHANGES", "BLOCKED"])
def test_standing_verdict_stated_only_in_the_body_refuses_without_a_relayed_claim(repo, verdict):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire(repo, verdict, sidecar=f"# Staff review\n\n**Verdict:** {verdict}\n\nroute.py import is broken.\n", claim=False)

    _assert_review_owed(repo, out, head)


def test_a_quoted_standing_verdict_in_a_blockquote_or_fence_does_not_flip_a_passing_review(repo):
    body = (
        "# Review\n\n> verdict: BLOCKED\n\n```\nVerdict: REQUIRES_CHANGES\n```\n\n**Verdict:** PASS\n"
    )

    out = _fire(repo, "PASS", sidecar=body, claim=False)

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_standing_verdict_whose_findings_are_all_disposed_commits(repo):
    ledger = '[{"id": "F1", "status": "rejected", "severity": "ASK", "file": "a.py"}]'
    sidecar = f"---\nverdict: BLOCKED\n---\n# review\n\n## Findings Ledger\n\n```json\n{ledger}\n```\n"

    out = _fire(repo, "BLOCKED", sidecar=sidecar)

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


def test_a_superseding_passing_verdict_in_the_sidecar_commits(repo):
    out = _fire(repo, "REQUIRES_CHANGES", sidecar="---\nverdict: PASS\n---\n# review\n\nSuperseded.\n")

    assert out.get("refused") != "review-owed", out
    assert out.get("committed") is True, out


RUN = "warp-2026-11-27-hnd-fixture-20261127T010101-abc123"
SIZING_REL = "state/sizings/2026-11-27-hnd-fixture.yaml"
PLAN_REL = "docs/plans/2026-11-27-hnd-fixture.md"
RED_TEST = "tests/test_x.py::test_red"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
STALE_SENTINEL = "// kept script composed by another engine\n"
_SPINE = (
    "- id: C1\n  title: Ship it\n  body: Do the named work and pin it with a test.\n  change_kind: code-edit\n"
    "  surface: a.py\n  writes: [a.py]\n  queue_scope: project\n  disposition: open\n"
)


def _six_row_sidecar(*, out_of_ask: bool, file: str = "a.py") -> str:
    rows = [{"id": f"F{n}", "status": "open", "severity": "ASK", "file": file} for n in range(1, 7)]
    if out_of_ask:
        for row in rows:
            row["out_of_ask"] = True
    return (
        f"---\nverdict: REQUIRES_CHANGES\n---\n# review\n\n## Findings Ledger\n\n```json\n{json.dumps(rows)}\n```\n"
    )


def _halted_run(repo: Path, *, record: str, sidecar: str) -> Path:
    """A halted run whose first fire wrote a REQUIRES_CHANGES sidecar, whose revise ruled R1 out of scope,
    and whose halt resume record is ``missing`` or ``engine-stale``; returns its record dir."""
    from coordinator_core.ops.dispatch_emit import halt_resume_build, kept_work
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

    (repo / "state" / "sizings").mkdir(parents=True)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-hnd-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-11-27", "mode": "pm"}},
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "coordinator_core").mkdir()
    (repo / PLAN_REL).write_text(
        "---\ntitle: fixture\nauthor: fixture\nstatus: draft\ncreated: 2026-11-01\n"
        "census:\n  - question: q\n    command: echo a\n    result: '1'\n"
        "capabilities: []\nanchor_premises: []\n"
        f"sizing_object: {SIZING_REL}\n"
        f"prime_exit_criterion:\n  statement: done\n  derived_from: {SIZING_REL}\n"
        "---\n\n# Fixture\n\n## Tasks\n\n```yaml plan-tasks\n" + _SPINE + "```\n",
        encoding="utf-8",
    )
    (repo / _SIDECAR).write_text(sidecar, encoding="utf-8")
    rd = repo / RECORD_DIR_ROOT / RUN
    rd.mkdir(parents=True)
    (rd / "manifest.json").write_text(json.dumps({"rows": [{"id": "C1", "writes": ["a.py"]}]}), encoding="utf-8")
    (rd / "kept.json").write_text(json.dumps({
        "chunks": {"C1": {"paths": {"a.py": kept_work.fingerprint(repo, "a.py") if (repo / "a.py").exists() else None}}},
        "plan_gate": None, "post_checkpoint": {},
    }), encoding="utf-8")
    first_fire_review = {
        "wave_sidecar_paths": [_SIDECAR],
        "wave_reviews": [{"agent_type": "coordinator:reviewer", "verdict": "REQUIRES_CHANGES", "sidecar_path": _SIDECAR}],
    }
    (rd / "run-outcome.json").write_text(
        json.dumps({"incomplete_chunks": [], "plan_path": PLAN_REL, "inline_review": first_fire_review}), encoding="utf-8"
    )
    (rd / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "review_blocked", "sizing_rel": SIZING_REL}), encoding="utf-8"
    )
    (rd / "run-stage.json").write_text(json.dumps({"stage": "halted"}), encoding="utf-8")
    (rd / "notes.jsonl").write_text(
        json.dumps({"note": "out_of_scope:[R1] revise before the refire", "at": "2026-11-27T00:00:00Z", "run": RUN}) + "\n",
        encoding="utf-8",
    )
    if record == "engine-stale":
        kept = rd / halt_resume_build.COMPOSED_SCRIPT_FILE
        kept.write_text(STALE_SENTINEL, encoding="utf-8")
        hrr.write(rd, hrr.build_record(
            resume_plan_rel=None, composed_script_rel=kept.relative_to(repo).as_posix(), hoisted_fragments={},
            gate_verdict={}, landed_state_sha=None, fingerprint={"engine_sha": "stale-engine"},
            retry_not_before=None, stop_after=None,
        ))
    return rd


def _refire_digest(*, lists_sidecar: bool) -> dict:
    """The refire's digest, hand-built: the engine composes `_review_halt_commit_params` over it, but the
    digest itself is the output of the run's JS, which no engine composer produces."""
    from coordinator_core.ops.review_test_blocker import TEST_BLOCKER

    wave = [{"agent_type": "coordinator:reviewer", "verdict": "REQUIRES_CHANGES", "sidecar_path": _SIDECAR}] if lists_sidecar else []
    return {
        "run_id": RUN,
        "halted_by": {"halted": "review_blocked", "blocker": TEST_BLOCKER, "failing": [RED_TEST]},
        "incomplete": [],
        "ledger": {"items": []},
        "review": {
            "wave": wave, "prep": None, "delivery": None,
            "reverify": {"status_before": "fail", "status_after": "fail", "failing": [RED_TEST], "pre_existing": [RED_TEST]},
        },
        "next_action": {"kind": "terminal_commit", "params": {}},
    }


def _fire_after_recompose(repo: Path, monkeypatch, *, record: str, rows_out_of_ask: bool, lists_sidecar: bool,
                          row_file: str = "a.py") -> dict:
    from coordinator_core.ops.dispatch_emit import ask_compose, halt_resume_build
    from coordinator_core.ops.dispatch_emit import halt_resume_record as hrr

    rd = _halted_run(repo, record=record, sidecar=_six_row_sidecar(out_of_ask=rows_out_of_ask, file=row_file))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    text, why = halt_resume_build.recompose_script(repo, RUN)
    assert why == "" and text and RUN in text and text != STALE_SENTINEL, (why, text)
    stored = hrr.read(rd, 1000)
    assert isinstance(stored, dict) and stored["composed_script_rel"], stored

    (repo / "a.py").write_text("a\n", encoding="utf-8")
    request = CommitRequest(chunks=(ChunkCommit(id="C1", title="t1", paths=("a.py",)),), plan_path=PLAN_REL)
    (repo / "run.mjs").write_text("// emitted\n" + render_marker(request) + "\n", encoding="utf-8")
    envelope = repo / "task-output.json"
    envelope.write_text(json.dumps({
        "parent_id": "p", "runs": [{"run_id": RUN, "status": "ok", "result": _refire_digest(lists_sidecar=lists_sidecar)}],
    }), encoding="utf-8")
    return terminal_commit._handler(
        {"task_output_path": str(envelope), "run_id": RUN, "script_path": "run.mjs"}, repo_root=repo / ".git",
    )


@pytest.mark.parametrize("record", ["missing", "engine-stale"])
@pytest.mark.parametrize(
    "rows_out_of_ask, lists_sidecar",
    [(False, False), (True, True)],
    ids=["refire-review-names-no-prior-sidecar", "rows-marked-out_of_ask-by-the-revise"],
)
def test_a_commit_leg_recomposed_after_a_revise_refuses_the_standing_requires_changes(
    repo, monkeypatch, record, rows_out_of_ask, lists_sidecar
):
    head = _git(["rev-parse", "HEAD"], repo)

    out = _fire_after_recompose(
        repo, monkeypatch, record=record, rows_out_of_ask=rows_out_of_ask, lists_sidecar=lists_sidecar
    )

    _assert_review_owed(repo, out, head)
    assert "REQUIRES_CHANGES" in out["error"]
    assert f"{_SIDECAR}:F1" in out["error"] and "6 finding(s)" in out["error"]


def test_a_peer_hunk_out_of_ask_row_is_not_owed_by_the_recomposed_commit_leg(repo, monkeypatch):
    out = _fire_after_recompose(
        repo, monkeypatch, record="missing", rows_out_of_ask=True, lists_sidecar=True, row_file="peer/other.py"
    )

    assert out.get("refused") != "review-owed", out
