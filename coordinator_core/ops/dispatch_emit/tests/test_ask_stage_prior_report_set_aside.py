"""ask_stage sets a prior run's row report aside so a refired row re-executes."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN_REL, repo  # noqa: F401


def _report(repo, row):  # noqa: F811
    path = repo / _dispatch_report_path(PLAN_REL, row)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_a_prior_report_moves_to_prior_md_and_the_brief_names_it(repo):  # noqa: F811
    c1 = _report(repo, "C1")
    c1.write_text("PARTIAL old", encoding="utf-8")
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    prior = c1.with_name("C1.prior.md")
    assert not c1.exists()
    assert prior.read_text(encoding="utf-8") == "PARTIAL old"
    brief = (repo / next(r for r in m.rows if r.id == "C1").brief_path).read_text(encoding="utf-8")
    assert prior.relative_to(repo).as_posix() in brief
    assert "never this run's answer" in brief


def test_an_older_prior_is_replaced(repo):  # noqa: F811
    c1 = _report(repo, "C1")
    c1.with_name("C1.prior.md").write_text("ancient", encoding="utf-8")
    c1.write_text("newer", encoding="utf-8")
    stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert c1.with_name("C1.prior.md").read_text(encoding="utf-8") == "newer"


def test_no_prior_report_changes_nothing(repo):  # noqa: F811
    m = stage(repo, run_id="r1", plan_rel=PLAN_REL)
    for r in m.rows:
        assert not (repo / _dispatch_report_path(PLAN_REL, r.id)).with_name(f"{r.id}.prior.md").exists()
        assert "set aside" not in (repo / r.brief_path).read_text(encoding="utf-8")


def test_a_non_staged_rows_report_is_untouched(repo):  # noqa: F811
    other = _report(repo, "ZZ9")
    other.write_text("keep", encoding="utf-8")
    stage(repo, run_id="r1", plan_rel=PLAN_REL)
    assert other.read_text(encoding="utf-8") == "keep"
    assert not other.with_name("ZZ9.prior.md").exists()
