"""checkpoint_hunks: a checkpoint's blob carries the run's own hunks only; unknown withholds."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git import checkpoint_hunks as ch
from coordinator_core.ops.dispatch_emit import run_token
import pytest

_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
PLAN = "docs/plans/p.md"
MSG = f"checkpoint(wave 1): 1 row — C1\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234\n"


def test_credited_writes_reads_declared_writes_of_credited_rows(tmp_path: Path) -> None:
    import json

    from coordinator_core.ops.dispatch_emit import run_credit
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    plan = "docs/plans/p.md"
    (tmp_path / "docs/plans").mkdir(parents=True)
    (tmp_path / plan).write_text(
        "# p\n\n## Tasks\n\n```yaml plan-tasks\n- id: C4\n  title: t\n  writes: [a/x.py]\n  disposition: open\n"
        "- id: C5\n  title: u\n  writes: [a/y.py]\n  disposition: open\n```\n",
        encoding="utf-8",
    )
    base = record_dir(tmp_path, "run1")
    base.mkdir(parents=True)
    (base / "manifest.json").write_text(json.dumps({"plan_rel": plan}), encoding="utf-8")
    run_credit.record(base, [{"chunk": "C4", "evidence": "earlier pass wrote it"}], "2026-11-11T00:00:00Z")
    assert run_credit.credited_writes(tmp_path, "run1") == frozenset({"a/x.py"})
    assert run_credit.credited_writes(tmp_path, "no-such-run") == frozenset()


def _runs(monkeypatch, tmp_path, records, plans) -> None:
    monkeypatch.setattr(run_token, "live_records", lambda root: iter(records))
    monkeypatch.setattr(ch, "_sizing_plan", lambda root, rel: plans.get(rel, ""))


def test_run_resolves_by_session_and_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s2", "sizing_rel": "sb"},
        ],
        {"sa": PLAN, "sb": PLAN},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") == "a"
    assert ch.checkpoint_run_id(tmp_path, MSG, "s9") is None
    assert ch.checkpoint_run_id(tmp_path, MSG, None) is None


def test_run_unresolved_when_plan_differs_or_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s1", "sizing_rel": "sb"},
        ],
        {"sa": "docs/plans/other.md", "sb": "docs/plans/other.md"},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") is None
    _runs(
        monkeypatch, tmp_path,
        [
            {"run_id": "a", "session_id": "s1", "sizing_rel": "sa"},
            {"run_id": "b", "session_id": "s1", "sizing_rel": "sb"},
        ],
        {"sa": PLAN, "sb": PLAN},
    )
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") is None
    assert ch.checkpoint_run_id(tmp_path, "no plan line", "s1") is None


def test_run_trailer_selects_only_live_run_with_matching_owner_and_plan(tmp_path, monkeypatch):
    records = [
        {"run_id": "ask-launcher", "session_id": "owner", "sizing_rel": "sizing"},
        {"run_id": "warp-stage", "session_id": "owner", "sizing_rel": "sizing"},
        {"run_id": "warp-other", "session_id": "peer", "sizing_rel": "sizing"},
        {"run_id": "warp-wrong-plan", "session_id": "owner", "sizing_rel": "other"},
    ]
    _runs(monkeypatch, tmp_path, records, {"sizing": PLAN, "other": "docs/plans/other.md"})
    assert ch.checkpoint_run_id(tmp_path, MSG, "owner") is None
    for run_id, expected in [
        ("warp-stage", "warp-stage"), ("warp-other", None), ("warp-wrong-plan", None),
        ("absent", None), ("", None),
    ]:
        assert ch.checkpoint_run_id(tmp_path, f"{MSG}\nCheckpoint-Run-Id: {run_id}", "owner") == expected
    assert ch.checkpoint_run_id(tmp_path, MSG + "\nCheckpoint-Run-Id: warp-stage\nCheckpoint-Run-Id: ask-launcher", "owner") is None


def _live_run(root: Path, run_id: str, sizing_rel: str, plan: str, *, session: str = "s1", in_token: bool) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    (root / sizing_rel).parent.mkdir(parents=True, exist_ok=True)
    (root / sizing_rel).write_text(f'plan: "{plan}"\n', encoding="utf-8")
    run_token.mint(root, run_id, "s.mjs", session_id=session, sizing_rel=sizing_rel if in_token else None)
    (record_dir(root, run_id)).mkdir(parents=True, exist_ok=True)
    (record_dir(root, run_id) / "run.json").write_text(f'{{"sizing_rel": "{sizing_rel}"}}', encoding="utf-8")


def test_run_resolves_when_the_token_record_omits_its_sizing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _live_run(tmp_path, "ask-1", "state/sizings/a.yaml", PLAN, in_token=False)
    _live_run(tmp_path, "ask-2", "state/sizings/b.yaml", "docs/plans/other.md", in_token=False)
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") == "ask-1"
    assert ch.checkpoint_run_id(tmp_path, MSG, "s9") is None


def _reemit_run(root: Path, run_id: str, plan: str, *, session: str = "s1") -> None:
    import json

    script = f"scratch/{run_id}.workflow.mjs"
    (root / "scratch").mkdir(exist_ok=True)
    (root / f"{script}.emitted.json").write_text(json.dumps({"plan_relpath": plan}), encoding="utf-8")
    run_token.mint(root, run_id, script, session_id=session)


def test_plan_reemit_run_without_a_sizing_resolves_through_its_emission_receipt(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _reemit_run(tmp_path, "warp-reemit", PLAN)
    _reemit_run(tmp_path, "warp-other-plan", "docs/plans/other.md")
    _reemit_run(tmp_path, "warp-peer", PLAN, session="s2")
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") == "warp-reemit"
    assert ch.checkpoint_run_id(tmp_path, MSG, "s2") == "warp-peer"
    assert ch.checkpoint_run_id(tmp_path, MSG, "s9") is None
    assert ch.checkpoint_run_id(tmp_path, MSG.replace(PLAN, "docs/plans/none.md"), "s1") is None


def test_run_unresolved_when_two_token_less_sizing_runs_match(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _live_run(tmp_path, "ask-1", "state/sizings/a.yaml", PLAN, in_token=False)
    _live_run(tmp_path, "ask-2", "state/sizings/b.yaml", PLAN, in_token=True)
    assert ch.checkpoint_run_id(tmp_path, MSG, "s1") is None
