"""E2E: a second distill / learn-lessons run after a committed run scopes only the delta.

Drives the registered ``distill.scope`` / ``distill.apply_disposal`` ops through
``ipc.dispatch_message`` at default scope params, and the learn-lessons ``brief`` + ``apply``
entry points with the real ``d-stamp-run-complete`` dispatch (the three external CLI leaves are
recorded stubs). The watermark advances only at the run's commit point.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
import coordinator_core.ops  # noqa: F401  (registers the ops)
from coordinator_core import run_watermark
from coordinator_core.distill import manifest_schema as schema
from coordinator_core.learn_lessons_pipeline import apply as llp_apply
from coordinator_core.learn_lessons_pipeline import brief

# Above one run's byte budget (it binds before the file budget), below two runs' worth.
TOTAL = 400
FIRST_DAY = _dt.date(2024, 1, 1)
DISTILL_RUN = "2026-11-13-01h00"


def _git(root: Path, *argv: str) -> str:
    done = subprocess.run(
        ["git", *argv], cwd=root, check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    return (done.stdout or "").strip()


def _rel(day: _dt.date) -> str:
    return f"archive/daily-summaries/{day.isoformat()}.md"


def _days() -> list[_dt.date]:
    return [FIRST_DAY + _dt.timedelta(days=i) for i in range(TOTAL)]


@pytest.fixture()
def distill_repo(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(run_watermark, "registry_get", lambda _key: None)
    root = tmp_path / "repo"
    (root / "state").mkdir(parents=True)
    (root / "state" / "distillation-log.md").write_text("## Run seed\n", encoding="utf-8")
    (root / "archive" / "daily-summaries").mkdir(parents=True)
    for day in _days():
        (root / _rel(day)).write_text("- a quiet day\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _dispatch(method: str, params: dict, root: Path) -> dict:
    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params, "_origin_worktree": str(root)}
    out = asyncio.run(ipc.dispatch_message(msg))
    assert "result" in out, out.get("error")
    return out["result"]


def _scope(root: Path, run_id: str, monkeypatch) -> tuple[set[str], int]:
    """Scoped (batched) paths of a default-params ``distill.scope`` call, plus the number of
    ``git diff`` spawns the watermark delta source made."""
    spawns: list[list[str]] = []
    real = run_watermark.git_out

    def counting(argv, *a, **kw):
        if "diff" in argv:
            spawns.append(list(argv))
        return real(argv, *a, **kw)

    monkeypatch.setattr(run_watermark, "git_out", counting)
    try:
        result = _dispatch("distill.scope", {"run_id": run_id}, root)
    finally:
        monkeypatch.setattr(run_watermark, "git_out", real)
    manifest = json.loads((root / result["input_json_path"]).read_text(encoding="utf-8"))
    return {p for batch in manifest["batches"] for p in batch}, len(spawns)


def _apply_disposal(root: Path, run_id: str) -> None:
    manifest = schema.make_disposal_manifest(
        run_id=run_id, rows=[], scan_stats=schema.make_scan_stats(0, 0, 0), mass_throttle=False
    )
    stamped = schema.apply_stamp(
        manifest, by="pm", at="2026-11-13T10:00:00Z", sha=schema.compute_manifest_sha(manifest), note="approved"
    )
    path = root / "state" / "scratch" / "artifact-distillation" / run_id / "disposal-manifest.json"
    path.write_text(json.dumps(stamped, indent=2), encoding="utf-8")
    result = _dispatch(
        "distill.apply_disposal", {"run_id": run_id, "harvest_committed_sha": _git(root, "rev-parse", "HEAD")}, root
    )
    assert result["failed"] == []


def _stored(root: Path) -> Path:
    return root / "state" / "run-watermarks" / "distill.json"


def test_distill_second_run_scopes_changed_files_plus_next_backlog_slice(distill_repo, monkeypatch):
    root, days = distill_repo, _days()

    first, first_spawns = _scope(root, DISTILL_RUN, monkeypatch)
    covered = len(first)
    assert 10 < covered < TOTAL
    assert first == {_rel(d) for d in days[:covered]}
    assert first_spawns == 0
    assert not _stored(root).exists()

    _apply_disposal(root, DISTILL_RUN)
    assert _stored(root).is_file()
    assert _git(root, "status", "--porcelain", "--", "state/run-watermarks") == ""
    assert "state/run-watermarks/distill.json" in _git(root, "show", "--name-only", "--format=", "HEAD").splitlines()

    edited = [days[3], days[covered - 7]]
    for day in edited:
        (root / _rel(day)).write_text("- edited after the first run\n", encoding="utf-8")

    second, second_spawns = _scope(root, "2026-11-13-02h00", monkeypatch)
    assert second == {_rel(d) for d in edited} | {_rel(d) for d in days[covered:]}
    assert second_spawns == 1


def test_distill_run_skipped_at_apply_leaves_the_watermark_byte_identical(distill_repo, monkeypatch):
    root, days = distill_repo, _days()
    _scope(root, DISTILL_RUN, monkeypatch)
    _apply_disposal(root, DISTILL_RUN)
    committed = _stored(root).read_bytes()

    (root / _rel(days[5])).write_text("- edited\n", encoding="utf-8")
    skipped, _ = _scope(root, "2026-11-13-02h00", monkeypatch)
    assert _stored(root).read_bytes() == committed

    rerun, _ = _scope(root, "2026-11-13-03h00", monkeypatch)
    assert rerun == skipped
    assert _stored(root).read_bytes() == committed


class _FixedDate(_dt.date):
    today_value = _dt.date(2026, 11, 13)

    @classmethod
    def today(cls):
        return cls.today_value


@pytest.fixture()
def lessons_env(tmp_path: Path, monkeypatch):
    import coordinator_core.learn_lessons_pipeline as pkg

    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(pkg, "date", _FixedDate)
    calls: list[tuple[str, list[str]]] = []
    state = {"fail": False}

    def stub(name):
        def leaf(args, repo_root):
            if state["fail"]:
                raise RuntimeError(f"{name} failed")
            calls.append((name, list(args)))
            return {"exit_code": 0}

        return leaf

    for name in ("extract-lessons", "lessons-outbox-drain", "age-sweep-lessons"):
        monkeypatch.setitem(llp_apply._DISPATCH_TABLE, name, stub(name))
    return tmp_path / "repo", calls, state


def _since(root: Path) -> str | None:
    args = next(d for d in brief(root, roots=[])["directives"] if d["id"] == "d-extract-lessons")["args"]
    return args[args.index("--since") + 1] if "--since" in args else None


def _lessons_watermark(root: Path) -> Path:
    return root / "state" / "run-watermarks" / "learn-lessons.json"


def test_lessons_second_run_scopes_by_the_committed_watermark_and_stamp_alone_advances_it(lessons_env, tmp_path):
    root, calls, state = lessons_env
    runs_dir = tmp_path / "home" / ".claude" / "tasks"

    assert _since(root) is None
    code, report = llp_apply.apply(root, roots=[])
    assert code == 0, report
    assert _lessons_watermark(root).is_file()
    committed = _lessons_watermark(root).read_bytes()
    assert json.loads(committed)["cursor"] == "2026-11-13"

    # The COMPLETE sentinel would give the same date; removing it proves the watermark drives --since.
    for sentinel in runs_dir.rglob("COMPLETE"):
        sentinel.unlink()
    assert _since(root) == "2026-11-13"
    extract = next(args for name, args in calls if name == "extract-lessons")
    assert "--since" not in extract

    _FixedDate.today_value = _dt.date(2026, 11, 14)
    try:
        state["fail"] = True
        code, _report = llp_apply.apply(root, roots=[])
        assert code != 0
        assert _lessons_watermark(root).read_bytes() == committed
        assert _since(root) == "2026-11-13"

        state["fail"] = False
        calls.clear()
        code, report = llp_apply.apply(root, roots=[])
        assert code == 0, report
        extract = next(args for name, args in calls if name == "extract-lessons")
        assert extract[extract.index("--since") + 1] == "2026-11-13"
        assert json.loads(_lessons_watermark(root).read_bytes())["cursor"] == "2026-11-14"
    finally:
        _FixedDate.today_value = _dt.date(2026, 11, 13)
