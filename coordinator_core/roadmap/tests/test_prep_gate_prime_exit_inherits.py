"""
coordinator_core/roadmap/tests/test_prep_gate_prime_exit_inherits.py — the
inheritance rung on PRIME_EXIT (C5).

Subject: `prep_gate._prime_exit`'s additive check that a plan's
`prime_exit_criterion.statement` matches an ACCEPTED sizing criterion it cites
through `derived_from`, so a plan inherits the PM's accepted words rather than
re-authoring them. The rung is a no-op on everything it cannot resolve to an
accepted criterion: an unresolvable path, a foreign-repo path, a sizing with no
`exit_criterion`, or one that is still merely proposed (`accepted: null`) all
keep today's verdict — never a DEFECT and never an exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import prep_gate as pg

_MATCHING_STATEMENT = "the four fields land and validate"

_ACCEPTED_SIZING = """
exit_criterion:
  statement: {statement}
  accepted:
    pm_quote: "yes, that's it"
    on: "2026-09-27"
    mode: pm
""".strip()

_PROPOSED_SIZING = """
exit_criterion:
  statement: the four fields land and validate
  accepted: null
""".strip()

_NO_CRITERION_SIZING = "title: fixture-sizing\n"

_CLEAN_SPINE = """- id: C1
  title: Ship the thing
  body: Add the SPINE predicate and pin it with a failing-first test.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: [coordinator_core/roadmap/prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _write_plan(root: Path, *, statement: str, derived_from: str) -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    fm = [
        "title: fixture",
        "status: draft",
        "created: 2026-09-07",
        "author: fixture-session",
        "census: []",
        "prime_exit_criterion:",
        f"  statement: {statement}",
        f"  derived_from: {derived_from}",
    ]
    body = [
        "",
        "# Fixture",
        "",
        "Prose that names nothing.",
        "",
        "## Tasks",
        "",
        "```yaml plan-tasks",
        _CLEAN_SPINE.strip(),
        "```",
        "",
    ]
    path = plans / "2026-09-07-fixture.md"
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body), encoding="utf-8"
    )
    return path


def _write_sizing(root: Path, rel: str, contents: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _gate(root: Path, path: Path) -> dict:
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    return pg.gate_plan(root, path)


# ---------------------------------------------------------------------------
# The rung fires
# ---------------------------------------------------------------------------


def test_matching_statement_passes(tmp_path):
    derived_from = "state/sizings/2026-09-07-fixture.yaml"
    _write_sizing(
        tmp_path, derived_from, _ACCEPTED_SIZING.format(statement=_MATCHING_STATEMENT)
    )
    plan = _write_plan(tmp_path, statement=_MATCHING_STATEMENT, derived_from=derived_from)
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


def test_matching_modulo_whitespace_passes(tmp_path):
    derived_from = "state/sizings/2026-09-07-fixture.yaml"
    _write_sizing(
        tmp_path, derived_from, _ACCEPTED_SIZING.format(statement=_MATCHING_STATEMENT)
    )
    plan = _write_plan(
        tmp_path,
        statement="the   four fields land and  validate",
        derived_from=derived_from,
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_diverging_statement_is_a_defect(tmp_path):
    derived_from = "state/sizings/2026-09-07-fixture.yaml"
    _write_sizing(
        tmp_path, derived_from, _ACCEPTED_SIZING.format(statement=_MATCHING_STATEMENT)
    )
    plan = _write_plan(
        tmp_path, statement="a completely different criterion", derived_from=derived_from
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "DEFECT"
    assert report["classes"]["PRIME_EXIT"]["kind"] == "prime-exit-diverges-from-sizing"
    assert report["verdict"] == pg.NOT_PREPPED


# ---------------------------------------------------------------------------
# The rung is a no-op everywhere else
# ---------------------------------------------------------------------------


def test_unresolvable_path_keeps_todays_verdict(tmp_path):
    plan = _write_plan(
        tmp_path,
        statement="a completely different criterion",
        derived_from="state/sizings/does-not-exist.yaml",
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_foreign_repo_path_keeps_todays_verdict(tmp_path):
    """The cross-repo no-op, pinned with THIS plan's own frontmatter as the
    fixture: `derived_from` is a DoE-tree path (AC: rung yields PRIME_EXIT PASS,
    no DEFECT and no exception)."""
    plan = _write_plan(
        tmp_path,
        statement="a completely different criterion",
        derived_from="state/sizings/2026-09-27-coordinator-claude-klabauter-restructure-to-beat-vanilla.yaml",
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_proposed_but_unaccepted_criterion_keeps_todays_verdict(tmp_path):
    derived_from = "state/sizings/2026-09-07-fixture.yaml"
    _write_sizing(tmp_path, derived_from, _PROPOSED_SIZING)
    plan = _write_plan(
        tmp_path, statement="a completely different criterion", derived_from=derived_from
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_sizing_with_no_exit_criterion_keeps_todays_verdict(tmp_path):
    derived_from = "state/sizings/2026-09-07-fixture.yaml"
    _write_sizing(tmp_path, derived_from, _NO_CRITERION_SIZING)
    plan = _write_plan(
        tmp_path, statement="a completely different criterion", derived_from=derived_from
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_non_sizing_derived_from_is_never_touched(tmp_path):
    """`derived_from` pointing at a goal KR or free prose is not this rung's
    business at all — it never even attempts a read."""
    plan = _write_plan(
        tmp_path,
        statement="a completely different criterion",
        derived_from="a hand-authored goal KR, not a sizing path",
    )
    report = _gate(tmp_path, plan)
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# Import closure
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_import_closure_gains_no_ops_module():
    """`coordinator_core.roadmap.prep_gate`'s import closure must not pull in
    `coordinator_core.ops.*` — Design § Inheritance, gate side, forbids importing
    `ops/deliverable_cascade` for its cold-import cost and layering inversion.
    A fresh-interpreter sys.modules diff is the only honest way to check this:
    a module already imported by an earlier test would hide in sys.modules
    regardless of what prep_gate itself imports.
    """
    import subprocess
    import sys

    probe = (
        "import sys; "
        "before = set(sys.modules); "
        "import coordinator_core.roadmap.prep_gate; "
        "after = set(sys.modules); "
        "gained = after - before; "
        "ops_gained = [m for m in gained if m.startswith('coordinator_core.ops')]; "
        "print(','.join(sorted(ops_gained)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(Path(__file__).resolve().parents[3]),
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", result.stdout
