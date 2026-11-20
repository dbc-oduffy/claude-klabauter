"""A completion entry whose dispatch actuals yield a null `loe.tshirt` takes the
tshirt of the sizing whose `resolution.run_id` names its run, marked
`loe.tshirt_source: sizing`; a measured tshirt is never overwritten. Driven
through `coordinator_complete_entry.main` at default arguments plus `--run-id`.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops import coordinator_complete_entry as m

_RUN = "ask-20261118T114303-f7e559"


def _sizing(
    repo: Path, name: str, run_id: str, estimate: str, accepted: str | None = None, month: str | None = None
) -> None:
    month_dir = repo / "state" / "sizings" / (month or date.today().strftime("%Y-%m"))
    month_dir.mkdir(parents=True, exist_ok=True)
    doc: dict = {"estimate": {"tshirt": estimate}, "resolution": {"run_id": run_id}}
    if accepted:
        doc["exit_criterion"] = {"accepted": {"tshirt": accepted}}
    (month_dir / f"{name}.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(m, "git_root", lambda: root.as_posix())
    monkeypatch.setattr(m, "_resolve_session_commits", lambda *a, **k: [])
    monkeypatch.setattr(m._session_core, "resolve_session_id", lambda *a, **k: "")
    return root


def _write_entry(*extra: str) -> dict:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = m.main(["--sid", "session-abcdef", "--disposition", "single-session", *extra])
    assert rc == 0, err.getvalue()
    text = Path(out.getvalue().strip().splitlines()[-1]).read_text(encoding="utf-8")
    return parse_frontmatter(text)["frontmatter"]["loe"]


def test_null_actuals_take_the_matching_sizings_estimate(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="S")
    _sizing(repo, "other", "ask-other", estimate="XL")

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] == "S"
    assert loe["tshirt_source"] == "sizing"
    assert loe["agent_dispatches"] is None


def test_accepted_tshirt_wins_over_estimate(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="S", accepted="M")

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] == "M"
    assert loe["tshirt_source"] == "sizing"


def test_no_matching_sizing_leaves_null(repo: Path) -> None:
    _sizing(repo, "other", "ask-other", estimate="XL")

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] is None
    assert "tshirt_source" not in loe


def test_no_run_id_leaves_null(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="S")

    loe = _write_entry()

    assert loe["tshirt"] is None
    assert "tshirt_source" not in loe


def test_workflow_suffixed_run_id_matches_the_bare_run_id_sizing(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="S")

    loe = _write_entry("--run-id", _RUN + ".workflow")

    assert loe["tshirt"] == "S"
    assert loe["tshirt_source"] == "sizing"


def test_a_sizing_older_than_the_search_window_is_not_found(repo: Path) -> None:
    old = date.today().replace(day=1)
    for _ in range(3):
        old = (old - timedelta(days=1)).replace(day=1)
    _sizing(repo, "old", _RUN, estimate="S", month=old.strftime("%Y-%m"))

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] is None
    assert "tshirt_source" not in loe


def test_measured_tshirt_is_never_overwritten(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sizing(repo, "match", _RUN, estimate="XL")
    sessions = repo / "sessions"
    (sessions / "sid-1").mkdir(parents=True)
    (sessions / "sid-1" / "dispatched-agents.txt").write_text("a\tsonnet\n", encoding="utf-8")
    monkeypatch.setattr(m._session_core, "resolve_session_id", lambda *a, **k: "sid-1")
    monkeypatch.setattr(m._session_core, "sessions_dir", lambda *a, **k: str(sessions))

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] == "XS"
    assert loe["agent_dispatches"] == 1
    assert "tshirt_source" not in loe


def test_a_sizing_tshirt_outside_the_ladder_is_ignored(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="HUGE")

    loe = _write_entry("--run-id", _RUN)

    assert loe["tshirt"] is None


def _leg_entry(repo: Path, run_id: str) -> dict:
    from coordinator_core.ops.dispatch_emit import run_close_completion as rcc

    plan = "docs/plans/p.md"
    (repo / ".git").mkdir(exist_ok=True)
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / "p.md").write_text('---\ntitle: "Shipped it"\n---\n\nbody\n', encoding="utf-8")
    result, paths = rcc.completion_entry_leg(
        repo, run_id, plan, {"status": "met", "completion_summary": "Shipped."}
    )
    assert result.status == "ran", result.detail
    text = (repo / paths[0]).read_text(encoding="utf-8")
    return parse_frontmatter(text)["frontmatter"]["loe"]


def test_run_close_leg_takes_the_matching_sizing(repo: Path) -> None:
    _sizing(repo, "match", _RUN, estimate="S")

    loe = _leg_entry(repo, _RUN)

    assert loe["tshirt"] == "S"
    assert loe["tshirt_source"] == "sizing"


def test_run_close_leg_without_matching_sizing_stays_null(repo: Path) -> None:
    _sizing(repo, "other", "ask-other", estimate="XL")

    loe = _leg_entry(repo, _RUN)

    assert loe["tshirt"] is None
    assert "tshirt_source" not in loe
