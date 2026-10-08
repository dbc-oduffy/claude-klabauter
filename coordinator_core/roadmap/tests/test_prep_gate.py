"""
coordinator_core/roadmap/tests/test_prep_gate.py — the mise-prep authoring bar.

Subject: `coordinator_core.roadmap.prep_gate`, which answers "is every input a
fire-time driver would otherwise have to ask a human about DECLARED?" over one
plan.

The claim under test, stated once so every case reads against it: the bar is ONE
rule reported in FOUR classes, and "nothing to declare" is itself a declaration —
so `census: []`, `writes: []` and `mise_prepped_findings: []` all pass where the
absent key fails. Every test either pins one class's predicate, pins the
three-way external-dependency split, or pins the cost.

Zero spawns; every functional case builds its plan in `tmp_path`. The two budget
cases read this repo's own `docs/plans/` corpus — a cost claim needs the real
corpus, and a hand-built fixture would only prove the scan is fast over the
shapes its author thought of.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import prep_gate as pg

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_plan(
    root: Path,
    slug: str = "2026-09-07-fixture.md",
    *,
    frontmatter: str = "",
    spine: str | None = None,
) -> Path:
    plans = root / "docs" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    # `author` is not decoration: the SCHEMA class runs plan.schema.json over
    # this frontmatter, and a fixture missing a required field would make every
    # positive case in this module assert against a plan no real corpus holds.
    fm = [
        "title: fixture",
        "status: draft",
        "created: 2026-09-07",
        "author: fixture-session",
    ]
    if frontmatter.strip():
        fm.append(frontmatter.strip())
    body = ["", "# Fixture", "", "Prose that names nothing.", ""]
    if spine is not None:
        body += ["## Tasks", "", "```yaml plan-tasks", spine.strip(), "```", ""]
    path = plans / slug
    path.write_text(
        "---\n" + "\n".join(fm) + "\n---\n" + "\n".join(body), encoding="utf-8"
    )
    return path


_CLEAN_FM = """census: []
prime_exit_criterion:
  statement: the four fields land and validate
  derived_from: state/sizings/2026-09-07-fixture.yaml
"""

_CLEAN_SPINE = """- id: C1
  title: Ship the thing
  body: Add the SPINE predicate and pin it with a failing-first test.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: [coordinator_core/roadmap/prep_gate.py]
  queue_scope: project
  disposition: open
"""


def _gate(root: Path, path: Path) -> dict:
    return pg.gate_plan(root, path)


def prepped_plan(root: Path, **kw) -> Path:
    """A plan that clears all four classes — the baseline every negative case
    perturbs by exactly one field."""
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    return _write_plan(root, frontmatter=_CLEAN_FM, spine=_CLEAN_SPINE, **kw)


# ---------------------------------------------------------------------------
# The baseline
# ---------------------------------------------------------------------------


def test_a_fully_declared_plan_is_prepped(tmp_path):
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert report["verdict"] == pg.PREPPED
    assert report["withheld_rows"] == []
    assert all(c["status"] == "PASS" for c in report["classes"].values())
    assert report["message"].startswith("mise-prep: PREPPED —")


@pytest.mark.parametrize(
    "body_line",
    ["", "  body: \"\"\n", "  body: Ship the thing.\n"],
    ids=["no-body", "empty-body", "title-restatement"],
)
def test_a_row_with_nothing_to_execute_is_withheld(tmp_path, body_line):
    """claude-klabauter#20: a row the emitter would hand an executor with no work
    in it gated PREPPED and blocked at dispatch. SPINE reads the same row body
    the emitter does, withholds the row, and the plan is NOT-PREPPED."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = _CLEAN_SPINE.replace(
        "  body: Add the SPINE predicate and pin it with a failing-first test.\n", body_line
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "body-absent"
    assert report["withheld_rows"] == ["C1"]


def test_a_row_the_emitter_would_refuse_to_route_is_withheld(tmp_path):
    """coordinator-content-repo#75: a row writing both a plan body and code certified PREPPED
    and was then refused whole by dispatch. The gate now asks the emitter's own
    routing predicate and refuses with its message."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = _CLEAN_SPINE.replace(
        "  writes: [coordinator_core/roadmap/prep_gate.py]\n",
        "  writes: [coordinator_core/roadmap/prep_gate.py, docs/plans/2026-09-07-fixture.md]\n",
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "MixedAgentTypeRowError"
    assert report["withheld_rows"] == ["C1"]


def test_every_class_is_reported_even_when_it_passes(tmp_path):
    """The per-class breakdown IS the product. A caller handed only a verdict has
    to re-derive where the fix lands, and derives it differently each time."""
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert tuple(report["classes"]) == pg.CLASS_ORDER


# ---------------------------------------------------------------------------
# SPINE
# ---------------------------------------------------------------------------


def test_absent_spine_block_is_its_own_defect_kind(tmp_path):
    """Reported separately from a parse error: the two are fixed differently and
    differ by an order of magnitude in the corpus. Collapsing them would report
    the dominant defect under the name of the rarest."""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM))
    assert report["classes"]["SPINE"]["kind"] == "spine-absent"
    assert report["verdict"] == pg.NOT_PREPPED


def test_a_row_with_no_writes_key_fails_and_is_named(tmp_path):
    spine = """- id: C1
  title: Undeclared
  change_kind: code-edit
  surface: coordinator_core/x.py
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    spine_class = report["classes"]["SPINE"]
    assert spine_class["kind"] == "writes-undeclared"
    assert "C1" in spine_class["detail"]


def test_declared_empty_writes_passes(tmp_path):
    """`writes: []` is a declared-empty and is fine for a verification row — the
    same distinction the whole bar is built on."""
    spine = """- id: C1
  title: Verify
  body: Run the prep-gate suite and confirm it is green.
  change_kind: verification
  surface: coordinator_core/roadmap/prep_gate.py
  writes: []
  queue_scope: project
  disposition: open
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


def test_a_glob_shaped_write_is_refused(tmp_path):
    """`dispatch.emit`'s mint leg (`inventory_mint._refuse_if_glob`) raises on
    a glob pathspec after this bar would otherwise certify it — refuse here,
    before the stamp."""
    spine = """- id: C1
  title: Sweep
  body: Touch every module under the package.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: ["coordinator_core/roadmap/*.py"]
  queue_scope: project
  disposition: open
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    spine_class = report["classes"]["SPINE"]
    assert spine_class["kind"] == "writes-unreadable-at-emit"
    assert "C1" in spine_class["detail"]
    assert "glob" in spine_class["detail"]


def test_an_app_router_dynamic_segment_write_is_not_a_glob(tmp_path):
    """`[id]` is a concrete Next.js segment the commit preflight accepts;
    refusing it here forced authors onto `writes_under`."""
    spine = """- id: C1
  title: Route
  body: Add the route.
  change_kind: code-edit
  surface: src/app/api/rules/[id]/route.ts
  writes: ["src/app/api/rules/[id]/route.ts", "src/app/(main)/[[...slug]]/page.tsx"]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    spine_class = report["classes"]["SPINE"]
    assert spine_class.get("kind") != "writes-unreadable-at-emit", spine_class


def test_a_trailing_slash_write_is_refused(tmp_path):
    """`pathspec.py`'s `DirectoryShapedWriteError` (and `inventory_mint.py`'s
    own directory rung) both refuse a trailing-separator `writes:` entry at
    emit time — refused here first."""
    spine = """- id: C1
  title: Land the outbox
  body: Write the outbox directory.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: ["state/memo-outbox/sent/"]
  queue_scope: project
  disposition: open
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    spine_class = report["classes"]["SPINE"]
    assert spine_class["kind"] == "writes-directory-shaped"
    assert "C1" in spine_class["detail"]
    assert "directory-shaped" in spine_class["detail"]


def test_an_existing_directory_write_is_refused(tmp_path):
    """No trailing separator, but the path names a real directory on disk at
    gate time — `inventory_mint._refuse_if_directory_shaped`'s smaller rung
    (coordinator-klabauter#45 class B)."""
    (tmp_path / "coordinator_core" / "ops" / "tests").mkdir(parents=True, exist_ok=True)
    spine = """- id: C1
  title: Land tests
  body: Add coverage under the existing tests directory.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: ["coordinator_core/ops/tests"]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    spine_class = report["classes"]["SPINE"]
    assert spine_class["kind"] == "writes-unreadable-at-emit"
    assert "C1" in spine_class["detail"]
    assert "directory-shaped" in spine_class["detail"]


def test_a_concrete_file_write_passes_the_shape_check(tmp_path):
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert report["classes"]["SPINE"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


def test_a_closed_row_with_a_glob_write_is_not_refused(tmp_path):
    """`read_spine` already excludes a closed-disposition row from scheduling
    — its declared writes are never resolved by a driver, so a glob there is
    moot, not defective, matching `_row_is_unschedulable`'s own reasoning for
    EXTERNAL_DEPS."""
    spine = _CLEAN_SPINE + """- id: C2
  title: Retired sweep
  body: Already handled elsewhere.
  change_kind: code-edit
  surface: coordinator_core/roadmap/prep_gate.py
  writes: ["coordinator_core/roadmap/*.py"]
  queue_scope: project
  disposition: coded
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


_CONSUMES_ROW = """- id: {id}
  title: Row {id}
  body: Does the {id} work.
  change_kind: code-edit
  surface: coordinator_core/{id}.py
  writes: {writes}
  consumes: {consumes}
  queue_scope: project
  disposition: open
{extra}"""


def _consumes_spine(*rows: str) -> str:
    return "".join(rows)


def test_a_consumed_path_written_by_an_ordered_producer_passes(tmp_path):
    """No hand-declared edge: the derived read-after-write edge orders it."""
    spine = _consumes_spine(
        _CONSUMES_ROW.format(id="P1", writes="[coordinator_core/lib.py]", consumes="[]", extra=""),
        _CONSUMES_ROW.format(
            id="K1", writes="[coordinator_core/k1.py]", consumes="[coordinator_core/lib.py]", extra=""
        ),
    )
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS"


def test_a_consumed_path_no_row_writes_is_a_preexisting_file(tmp_path):
    spine = _consumes_spine(
        _CONSUMES_ROW.format(
            id="K1", writes="[coordinator_core/k1.py]", consumes="[coordinator_core/old.py]", extra=""
        ),
    )
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS"


def test_a_consumer_that_is_the_producers_ancestor_is_refused(tmp_path):
    """K1 consumes lib.py, P1 writes it, and P1 depends_on K1: the derived edge is
    dropped (declared outranks derived) so the producer lands AFTER the consumer."""
    spine = _consumes_spine(
        _CONSUMES_ROW.format(
            id="K1", writes="[coordinator_core/k1.py]", consumes="[coordinator_core/lib.py]", extra=""
        ),
        _CONSUMES_ROW.format(
            id="P1",
            writes="[coordinator_core/lib.py]",
            consumes="[]",
            extra="  depends_on:\n    - chunk: K1\n      gate_kind: output-consumption-runtime\n",
        ),
    )
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    spine_class = report["classes"]["SPINE"]
    assert spine_class["kind"] == "consumes-unordered"
    assert "K1 consumes coordinator_core/lib.py written by P1" in spine_class["detail"]
    assert report["verdict"] != pg.PREPPED


def test_a_path_scaffolded_early_and_finalized_late_is_ordered(tmp_path):
    """W0 creates lib.py, K1 consumes it, then F1 (after K1) rewrites it: the
    earliest writer satisfies the read, so the late finalizer is not a defect."""
    spine = _consumes_spine(
        _CONSUMES_ROW.format(id="W0", writes="[coordinator_core/lib.py]", consumes="[]", extra=""),
        _CONSUMES_ROW.format(
            id="K1",
            writes="[coordinator_core/k1.py]",
            consumes="[coordinator_core/lib.py]",
            extra="  depends_on:\n    - chunk: W0\n      gate_kind: output-consumption-runtime\n",
        ),
        _CONSUMES_ROW.format(
            id="F1",
            writes="[coordinator_core/lib.py]",
            consumes="[]",
            extra="  depends_on:\n    - chunk: K1\n      gate_kind: output-consumption-runtime\n",
        ),
    )
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS", report["classes"]["SPINE"]


def test_a_consumed_writes_under_prefix_is_ordered_or_refused_like_a_file(tmp_path):
    spine = _consumes_spine(
        _CONSUMES_ROW.format(id="P1", writes="[]", consumes="[]", extra="  writes_under: [coordinator_core/gen/]\n"),
        _CONSUMES_ROW.format(
            id="K1", writes="[coordinator_core/k1.py]", consumes="[coordinator_core/gen/a.py]", extra=""
        ),
    )
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["status"] == "PASS"


def test_an_unreadable_spine_reports_the_reader_s_own_error_class(tmp_path):
    spine = """- id: C1
  title: Dangling
  change_kind: code-edit
  surface: coordinator_core/x.py
  writes: []
  queue_scope: project
  disposition: open
  depends_on:
    - chunk: C9
      gate_kind: output-consumption-runtime
      note: no such row
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["kind"] == "DanglingDependencyError"


# ---------------------------------------------------------------------------
# CENSUS
# ---------------------------------------------------------------------------


def test_missing_census_key_fails(tmp_path):
    fm = """prime_exit_criterion:
  statement: s
  derived_from: d
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path,
        _write_plan(tmp_path, frontmatter=fm, spine=_CLEAN_SPINE),
    )
    assert report["classes"]["CENSUS"]["kind"] == "census-undeclared"


def test_declared_empty_census_passes(tmp_path):
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert "declared-empty" in report["classes"]["CENSUS"]["detail"]


def test_a_census_entry_missing_its_command_is_incomplete(tmp_path):
    fm = """census:
  - question: how many plans carry a spine?
    result: "56"
prime_exit_criterion:
  statement: s
  derived_from: d
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=fm, spine=_CLEAN_SPINE))
    assert report["classes"]["CENSUS"]["kind"] == "census-incomplete"
    assert "command" in report["classes"]["CENSUS"]["detail"]


def test_a_census_command_is_never_run(tmp_path, monkeypatch):
    """The bar checks the question is ASKABLE, never re-asks it. Re-running is
    fire-time work for the runner, which is why the command is recorded rather
    than the answer alone."""
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError("the bar must never run a census command")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    fm = """census:
  - question: how many?
    command: "grep -c never-there README.md"
    result: "0"
prime_exit_criterion:
  statement: s
  derived_from: d
"""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=fm, spine=_CLEAN_SPINE))
    assert report["classes"]["CENSUS"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# PRIME_EXIT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "criterion, kind",
    [
        ("", "prime-exit-absent"),
        ("prime_exit_criterion:\n  derived_from: d\n", "prime-exit-empty"),
        ("prime_exit_criterion:\n  statement: s\n", "prime-exit-underived"),
    ],
)
def test_prime_exit_requires_a_statement_and_a_link(tmp_path, criterion, kind):
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path,
        _write_plan(tmp_path, frontmatter="census: []\n" + criterion, spine=_CLEAN_SPINE),
    )
    assert report["classes"]["PRIME_EXIT"]["kind"] == kind


def test_prime_exit_does_not_require_a_falsifier(tmp_path):
    """The mandate widened the CRITERION, not its instrument — the falsifier's
    own M/L/XL proportionality is not this bar's business."""
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------


def test_prose_where_the_schema_wants_a_sizings_path_is_a_defect(tmp_path):
    """example-retrieval-repo, 2026-09-11: the reported case, which reached approved AND
    certified.

    `derived_from` was a paragraph of prose. PRIME_EXIT passed it — the field is
    PRESENT and is not a scaffold placeholder, which is all that class asks —
    and nothing else looked at its SHAPE. Only the frontmatter-schema hook
    caught it, and only because the author happened to edit the file for an
    unrelated reason.
    """
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path,
        _write_plan(
            tmp_path,
            frontmatter=(
                "census: []\n"
                "prime_exit_criterion:\n"
                "  statement: the four fields land and validate\n"
                "  derived_from: >-\n"
                "    The replan brief names this work, and the sizing it came"
                " from was archived.\n"
            ),
            spine=_CLEAN_SPINE,
        ),
    )
    assert report["verdict"] == pg.NOT_PREPPED
    schema = report["classes"]["SCHEMA"]
    assert schema["kind"] == "schema-invalid"
    assert "derived_from" in schema["detail"]
    # PRIME_EXIT still PASSES: the two classes ask different questions of the
    # same field, and collapsing them would lose the one that found this.
    assert report["classes"]["PRIME_EXIT"]["status"] == "PASS"


def test_a_defect_prime_exit_already_names_is_not_reported_twice(tmp_path):
    """Register: one fact, once.

    A plan with no `prime_exit_criterion` fails PRIME_EXIT by design. The schema
    walk finds the identical absence, and printing it on both lines makes an
    author read two findings to learn one thing.
    """
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path, _write_plan(tmp_path, frontmatter="census: []\n", spine=_CLEAN_SPINE)
    )
    assert report["classes"]["PRIME_EXIT"]["kind"] == "prime-exit-absent"
    assert report["classes"]["SCHEMA"]["status"] == "PASS"
    assert report["message"].count("prime_exit_criterion") == 1


def test_an_unreadable_schema_passes_rather_than_refusing_every_plan(
    tmp_path, monkeypatch
):
    """The instrument's failure is advisory about itself, never about the plan.

    Failing closed here would refuse every plan in a tree whose vendored schemas
    have not been re-published yet — a defect in this gate, not in the corpus.
    """
    monkeypatch.setattr(pg, "_PLAN_SCHEMA", tmp_path / "no-such-schema.json")
    report = _gate(tmp_path, prepped_plan(tmp_path))
    assert report["verdict"] == pg.PREPPED
    assert report["classes"]["SCHEMA"]["status"] == "PASS"
    assert "unreadable" in report["classes"]["SCHEMA"]["detail"]


# ---------------------------------------------------------------------------
# EXTERNAL_DEPS — the three-way split
# ---------------------------------------------------------------------------


def _external_plan(tmp_path, gate_block: str = "") -> Path:
    spine = f"""- id: C1
  title: Reaches out
  body: Call the sibling repo's gate once it clears.
  change_kind: code-edit
  surface: coordinator-content-repo/coordinator/bin/mise-prep-gate.py
  writes: []
  queue_scope: project
  disposition: open
{gate_block}"""
    return _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine)


def test_a_row_naming_a_sibling_repo_without_a_gate_is_not_prepped(tmp_path):
    report = _gate(tmp_path, _external_plan(tmp_path))
    deps = report["classes"]["EXTERNAL_DEPS"]
    assert deps["kind"] == "external-dep-undeclared"
    assert "coordinator-content-repo" in deps["detail"]
    assert report["verdict"] == pg.NOT_PREPPED


def test_a_gate_with_no_requires_is_not_prepped(tmp_path):
    block = """  external_gate:
    - owner_repo: coordinator-content-repo
      condition: the schema lands
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    assert "requires" in report["classes"]["EXTERNAL_DEPS"]["detail"]


@pytest.mark.parametrize(
    "closure", ["disposition: coded", "disposition: wont_do", "disposition: open\n  deferred: true"]
)
def test_a_row_no_wave_schedules_is_skipped_not_withheld(tmp_path, closure):
    """`withheld` becomes `mise_prepped_findings`: rows held by an uncleared
    external_gate, work waiting on somebody else. Example-game-repo, 2026-09-11: a plan with
    59 of 70 rows coded stamped "62 row(s) withheld", which its aggregate reader
    reports as a PARTIAL-FIRE excluding work that is finished."""
    spine = f"""- id: C1
  title: Already done
  change_kind: code-edit
  surface: coordinator_core/example.py
  writes: [<resolved-in-chunk>]
  queue_scope: project
  {closure}
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.PREPPED
    assert report["withheld_rows"] == []


def test_landed_work_withholds_its_own_row_and_the_plan_still_certifies(tmp_path):
    """Row granularity, deliberately: refusing the plan would discard every
    schedulable row alongside the blocked one."""
    block = f"""  external_gate:
    - owner_repo: coordinator-content-repo
      condition: the schema lands
      requires: {pg.REQUIRES_LANDED}
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["verdict"] == pg.PREPPED
    assert report["withheld_rows"] == ["C1"]
    assert "withheld" in report["message"]


def test_commit_in_owner_repo_withholds_its_row_and_the_plan_still_certifies(tmp_path):
    """PM ruling: a plan is not rejected because part of it needs code in another repo.

    This used to refuse the whole plan, reasoning that a hands-off run has no session in which
    to obtain the per-session assent DR-127 requires for a cross-repo commit. That is sound
    about the ROW and wrong about the PLAN — withholding the row already stops the run writing
    into a sibling's tree unassented, and refusing on top of it discarded every schedulable row
    that had nothing to do with the sibling. The same argument the LANDED-WORK case above always
    made, finally applied to the other value.
    """
    block = f"""  external_gate:
    - owner_repo: coordinator-content-repo
      condition: someone commits there
      requires: {pg.REQUIRES_COMMIT}
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["verdict"] == pg.PREPPED
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"
    assert report["withheld_rows"] == ["C1"]


def test_a_commit_gated_row_says_so_rather_than_only_being_withheld(tmp_path):
    """Both `requires:` values withhold the same row id, so the detail is the only place the
    difference can live — and they route differently: one waits for a peer's landing, the other
    needs a cross-repo commit dispatched under per-session assent."""
    commit_block = f"""  external_gate:
    - owner_repo: coordinator-content-repo
      condition: someone commits there
      requires: {pg.REQUIRES_COMMIT}
"""
    landed_block = f"""  external_gate:
    - owner_repo: coordinator-content-repo
      condition: they land the op
      requires: {pg.REQUIRES_LANDED}
"""
    commit = _gate(tmp_path / "c", _external_plan(tmp_path / "c", commit_block))
    landed = _gate(tmp_path / "l", _external_plan(tmp_path / "l", landed_block))
    assert "cross-repo commit" in commit["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "cross-repo commit" not in landed["classes"]["EXTERNAL_DEPS"]["detail"]


def test_no_predicate_produces_the_retired_refused_verdict():
    """REFUSED is retired and its absence is the rule, not an oversight.

    The constant stays so no consumer's string comparison shifts, and `_refuse` stays as the one
    shape that reaches the verdict — so reintroducing a whole-plan refusal means calling it, and
    deleting this test to do so.
    """
    source = Path(pg.__file__).read_text(encoding="utf-8")
    calls = [
        line.strip()
        for line in source.splitlines()
        if "_refuse(" in line and not line.lstrip().startswith("def _refuse(")
    ]
    assert calls == [], f"a predicate reintroduced the whole-plan refusal: {calls}"


def test_a_cleared_gate_is_not_a_finding(tmp_path):
    block = """  external_gate:
    - owner_repo: coordinator-content-repo
      condition: the schema lands
      cleared: true
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"
    assert report["verdict"] == pg.PREPPED


def test_closure_evidence_alone_does_not_clear_a_gate(tmp_path):
    """`spine_read._has_uncleared_execution_gate` reads `cleared` and nothing
    else; the two readers must agree or a gate's visibility depends on which one
    saw it first."""
    block = """  external_gate:
    - owner_repo: coordinator-content-repo
      condition: the schema lands
      closure_evidence: a memo I have not sent
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "DEFECT"


def test_surface_prose_is_never_read_as_a_path(tmp_path):
    """The ROOT-EXISTENCE leg does not run against `surface:`, whose own schema
    description admits "a single path OR SUBSYSTEM"."""
    spine = """- id: C1
  title: Prose surface
  change_kind: doc-edit
  surface: the whole ceremony, wherever it is spelled
  writes: []
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"


def test_a_write_whose_first_segment_is_not_in_this_repo_leaves_it(tmp_path):
    spine = """- id: C1
  title: Nameless cross-repo write
  change_kind: code-edit
  surface: somewhere
  writes: [not_a_directory_here/x.py]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert "does not exist in this repo" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_a_root_the_plan_itself_creates_is_not_somebody_elses_tree(tmp_path):
    """Ported from DoE c36c45dd0a, which owns the twin. Without this the leg fires
    hardest on the plans whose whole job is to bring a new top-level directory into
    existence, and the only way through the bar is an `external_gate` that would be
    a lie — no external party, nothing to wait for. Measured by
    example-game-workbench-repo-b8: one workspace-skeleton plan refused 35 times on
    `ide/`, the directory it exists to create."""
    spine = """- id: C1
  title: Create the workspace root
  change_kind: code-edit
  surface: ide
  writes: [ide/shell/main.py, ide/shell/boundary.py]
  writes_under: [ide/]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"


def test_writing_deeper_does_not_buy_the_created_root_exemption(tmp_path):
    """The converse, and the reason the exemption reads only the two spellings that
    name a DIRECTORY. Taken off any `writes:` path, a first segment is always its
    own row's first segment — so `coordinator_core/ops/x.py` would exempt itself and
    the leg would be dead."""
    spine = """- id: C1
  title: Writes deep into a tree that is not here
  change_kind: code-edit
  surface: somewhere
  writes: [not_a_directory_here/deep/x.py, not_a_directory_here/deep/y.py]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    detail = report["classes"]["EXTERNAL_DEPS"]["detail"]

    assert "does not exist in this repo" in detail
    assert "(x2)" in detail, "one distinct fact, reported once with a count"
    assert "writes_under:" in detail, "the repair names the spelling, not just the rule"


def test_a_settings_home_write_is_not_an_undeclared_cross_repo_dependency(tmp_path):
    """Ported alongside `_is_settings_home_path` (coordinator-content-repo `ff446da1b`, "the
    settings home is not another team's tree"). A row writing under the
    machine-local settings home is not a nameless path into a sibling repo's
    tree, so it must pass EXTERNAL_DEPS with no `external_gate` -- matching
    `mise-prep-gate.py`'s bar exactly, since `plan.stamp_prepped` is meant to
    enforce it ahead of, not a looser one."""
    spine = """- id: C1
  title: Settings-home write
  change_kind: config-edit
  surface: settings home
  writes: [~/.coordinator-claude-settings/machine-local/example_retrieval_repo.toml]
  queue_scope: project
  disposition: open
"""
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS"


def test_the_repo_s_own_name_is_never_a_sibling(tmp_path):
    """An intra-repo blocker is a `depends_on` edge, never a gate. A list that
    kept the running repo's own name would fire the DR-127 leg on every plan that
    cites its own tree by name."""
    own = tmp_path / "claude-klabauter"
    own.mkdir()
    assert "claude-klabauter" not in pg.fleet_siblings(own)
    assert "coordinator-content-repo" in pg.fleet_siblings(own)


def _plan_with(root: Path, spine: str, slug: str) -> Path:
    """A plan clearing CENSUS/PRIME_EXIT whose spine is the case under test.

    `prepped_plan` fixes the spine; these cases perturb exactly that, so they
    build the plan directly and create the `coordinator_core/` root entry the
    baseline relies on.
    """
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(parents=True, exist_ok=True)
    return _write_plan(root, slug, frontmatter=_CLEAN_FM, spine=spine)


def test_the_fleet_list_carries_every_name_including_the_doctrine_repo(tmp_path):
    """Defect 1, the write side's half of it. Neither half hard-omits a name: the
    scanned repo's own is subtracted at CALL time, so standing in the doctrine
    repo drops it and standing anywhere else keeps it. The read-side twin now
    carries the same eight, which is what makes the two halves return the same
    verdict on a corpus that is neither of them."""
    assert "coordinator-content-repo" in pg.FLEET_REPOS
    doctrine = tmp_path / "coordinator-content-repo"
    doctrine.mkdir()
    assert "coordinator-content-repo" not in pg.fleet_siblings(doctrine)
    assert "claude-klabauter" in pg.fleet_siblings(doctrine)


def test_the_repo_s_own_name_is_subtracted_case_insensitively(tmp_path):
    """A clone at `coordinator-content-repo/` and one at `coordinator-content-repo/` are the same repo. A
    case-sensitive subtraction would report every self-naming row in the
    lower-case clone as a cross-repo dependency."""
    own = tmp_path / "coordinator-content-repo"
    own.mkdir()
    assert "coordinator-content-repo" not in pg.fleet_siblings(own)


def test_a_sibling_named_in_a_different_case_is_still_caught(tmp_path):
    """Defect 2. The corpus does not agree with itself on case — example-retrieval-repo's own
    plans and cross-repo archive spell the doctrine repo `coordinator-content-repo` 1207 times
    against `coordinator-content-repo` 1052 — so a case-sensitive `==`/`startswith` left a row
    declaring a genuine cross-repo surface in the corpus's own spelling unseen by
    the SIBLING-NAME leg entirely."""
    for value in (
        "coordinator-content-repo/coordinator/bin/thing.py",
        "COORDINATOR-CONTENT-REPO@coordinator/bin/thing.py",
        "coordinator-content-repo",
    ):
        spine = (
            "- id: C1\n  title: t\n  change_kind: code-edit\n"
            f"  surface: {value}\n  writes: [coordinator_core/x.py]\n"
            "  queue_scope: project\n  disposition: open\n"
        )
        report = _gate(tmp_path, _plan_with(tmp_path, spine, "case.md"))
        assert (
            report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
        ), value


def test_case_folding_does_not_weaken_the_separator_rule(tmp_path):
    """Defect 2's boundary. Folding case widens which spellings are SEEN; it must
    not widen what counts as a separator, or `claude_klabauter2/` — a different
    name — starts reading as a match."""
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: claude_klabauter2/coordinator_core/x.py\n"
        "  writes: [coordinator_core/x.py]\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _plan_with(tmp_path, spine, "sep.md"))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", report["message"]


def test_a_new_root_level_entry_is_not_read_as_a_cross_repo_write(tmp_path):
    """Defect 3. ROOT-EXISTENCE's named false-positive shape, no longer
    hypothetical: example-retrieval-repo `docs/plans/2026-09-06-inbox-blitz-xs-s-bundle.md`
    row T8 declares a new root-level `ADOPTERS` file, and the only way past the
    bar was to delete that write from `writes:` — the leg forced an
    UNDER-declaration, inverting the one rule the bar enforces.

    The discriminant is the declared value's own shape, never an author's claim: a
    path INTO another tree carries a separator by construction, so a
    single-segment value names an entry at THIS repo's root."""
    for value in ("ADOPTERS", "ADOPTERS.md", "brand-new-dir/"):
        spine = (
            "- id: C1\n  title: t\n  body: Add the root-level entry.\n  change_kind: doc-edit\n"
            f"  surface: docs/x.md\n  writes: [{value}]\n"
            "  queue_scope: project\n  disposition: open\n"
        )
        report = _gate(tmp_path, _plan_with(tmp_path, spine, "root.md"))
        assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", value
        if value.endswith("/"):
            # `brand-new-dir/` clears EXTERNAL_DEPS's created-roots exemption but
            # is trailing-slash directory-shaped — SPINE's own emit-shape check
            # (this module's writes-directory-shaped) now refuses it before the
            # stamp, the same shape `dispatch_emit.pathspec` refuses at emit time
            # regardless of that exemption. Not a regression in this leg: the
            # exemption's own PASS above is unaffected.
            assert report["verdict"] == pg.NOT_PREPPED, value
            assert report["classes"]["SPINE"]["kind"] == "writes-directory-shaped"
        else:
            assert report["verdict"] == pg.PREPPED, value


def test_the_exemption_does_not_weaken_the_nameless_cross_repo_catch(tmp_path):
    """Defect 3's constraint. What the leg genuinely catches is a nameless path
    into a sibling's tree, and every one of those is multi-segment, so the
    single-segment exemption cannot reach them."""
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: somewhere\n  writes: [not_a_directory_here/x.py]\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _plan_with(tmp_path, spine, "nameless.md"))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


def test_an_unreplaced_placeholder_path_is_its_own_defect_no_gate_clears(tmp_path):
    """Defect 3's other half. The single-segment exemption would otherwise drop
    the one real catch ROOT-EXISTENCE had at that depth — an unreplaced `<...>`
    stand-in. It is now reported by name, and an `external_gate` does not silence
    it: a gate says who owns a path, not what the path is."""
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n"
        "  writes: ['<surface-resolved-in-chunk>']\n"
        "  external_gate:\n"
        "    - owner_repo: example-retrieval-repo\n"
        "      condition: they land the op\n"
        "      requires: landed-work\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _plan_with(tmp_path, spine, "ph.md"))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "path-placeholder", report["message"]
    assert report["verdict"] == pg.NOT_PREPPED


# ---------------------------------------------------------------------------
# EXTERNAL_DEPS — external_reads_ungated (mirrors coordinator-content-repo
# coordinator/bin/mise-prep-gate.py's "Declaration 3b" cases)
# ---------------------------------------------------------------------------


#: A fleet sibling name read off the gate's own constant, so the fixtures stay consistent with
#: whatever names the gate recognises.
_SIBLING = pg.FLEET_REPOS[-1]


def _reads_spine(entries: str, *, owner: str = _SIBLING) -> str:
    """A one-row spine reading a sibling path, with `entries` (already-indented
    YAML for `external_reads_ungated:`, or empty) spliced onto the row."""
    return (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n  writes: [docs/x.md]\n"
        f"  reads: [{owner}/coordinator_core/x.py]\n"
        f"{entries}"
        "  queue_scope: project\n  disposition: open\n"
    )


def test_a_matching_ungated_reads_entry_clears_the_reads_hit(tmp_path):
    entries = (
        "  external_reads_ungated:\n"
        f"    - path: {_SIBLING}/coordinator_core/x.py\n"
        f"      owner_repo: {_SIBLING}\n"
        "      reason: read-only, examined, nothing to land\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_reads_spine(entries)))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", report["message"]


@pytest.mark.parametrize("field", ["reads_at_head", "consumes"])
def test_reads_at_head_and_consumes_are_sibling_checked_and_ungatable(tmp_path, field):
    spine = _reads_spine("").replace("  reads:", f"  {field}:")
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    entries = (
        "  external_reads_ungated:\n"
        f"    - path: {_SIBLING}/coordinator_core/x.py\n"
        f"      owner_repo: {_SIBLING}\n"
        "      reason: read-only, examined\n"
    )
    spine = _reads_spine(entries).replace("  reads:", f"  {field}:")
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", report["message"]


def test_a_consumes_hit_with_no_ungated_entry_is_still_a_defect(tmp_path):
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n  writes: [docs/x.md]\n"
        "  consumes: [example-retrieval-repo/coordinator_core/x.py]\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


def test_a_reads_hit_with_no_ungated_entry_is_still_a_defect(tmp_path):
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_reads_spine("")))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


def test_an_ungated_entry_naming_a_written_path_is_refused(tmp_path):
    """The field only acknowledges a READ; an entry naming a path the row WRITES
    must not launder a cross-repo write as an examined read."""
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n"
        "  writes: [example-retrieval-repo/coordinator_core/w.py]\n"
        "  reads: [example-retrieval-repo/coordinator_core/x.py]\n"
        "  external_reads_ungated:\n"
        "    - path: example-retrieval-repo/coordinator_core/w.py\n"
        "      owner_repo: example-retrieval-repo\n"
        "      reason: examined\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    assert "WRITES" in report["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "example-retrieval-repo/coordinator_core/w.py" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_an_ungated_entry_naming_a_path_not_in_reads_is_refused(tmp_path):
    entries = (
        "  external_reads_ungated:\n"
        "    - path: example-retrieval-repo/coordinator_core/other.py\n"
        "      owner_repo: example-retrieval-repo\n"
        "      reason: examined\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_reads_spine(entries)))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    assert "not in this row's reads" in report["classes"]["EXTERNAL_DEPS"]["detail"]
    assert "no external_gate" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_an_ungated_entry_with_an_empty_reason_is_refused(tmp_path):
    entries = (
        "  external_reads_ungated:\n"
        f"    - path: {_SIBLING}/coordinator_core/x.py\n"
        f"      owner_repo: {_SIBLING}\n"
        "      reason: ''\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_reads_spine(entries)))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    assert "empty reason" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_an_ungated_entry_never_clears_a_writes_hit(tmp_path):
    """Negative spec: the field is `reads:`-only. A row whose `writes:` leaves
    the repo must stay a defect even when `external_reads_ungated` names the
    exact same path and owner_repo."""
    spine = (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        "  surface: docs/x.md\n"
        "  writes: [example-retrieval-repo/coordinator_core/w.py]\n"
        "  external_reads_ungated:\n"
        "    - path: example-retrieval-repo/coordinator_core/w.py\n"
        "      owner_repo: example-retrieval-repo\n"
        "      reason: examined\n"
        "  queue_scope: project\n  disposition: open\n"
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"


# ---------------------------------------------------------------------------
# The attest, read back
# ---------------------------------------------------------------------------


def _stamped(tmp_path, **fields) -> str:
    lines = "\n".join(f"{k}: {v}" for k, v in fields.items())
    path = _write_plan(
        tmp_path, slug="2026-09-07-stamped.md", frontmatter=lines, spine=_CLEAN_SPINE
    )
    return path.read_text(encoding="utf-8")


def test_an_unstamped_plan_resolves_unstamped(tmp_path):
    assert pg.read_stamp(_stamped(tmp_path))["state"] == pg.UNSTAMPED


def test_a_partial_stamp_resolves_malformed(tmp_path):
    result = pg.read_stamp(_stamped(tmp_path, mise_prepped_by="s"))
    assert result["state"] == pg.MALFORMED
    assert "mise_prepped_sha" in result["missing"]


def test_a_matching_sha_resolves_certified_and_a_stale_one_does_not(tmp_path):
    text = _stamped(tmp_path)
    from coordinator_core.frontmatter.primitives import canonical_body_sha

    body_sha = canonical_body_sha(text)
    certified = pg.read_stamp(
        _stamped(
            tmp_path,
            mise_prepped_by="s",
            mise_prepped_at="2026-09-07T00:00:00Z",
            mise_prepped_sha=f'"{body_sha}"',
            mise_prepped_findings="[]",
        )
    )
    assert certified["state"] == pg.CERTIFIED
    stale = pg.read_stamp(
        _stamped(
            tmp_path,
            mise_prepped_by="s",
            mise_prepped_at="2026-09-07T00:00:00Z",
            mise_prepped_sha='"0000000"',
            mise_prepped_findings="[]",
        )
    )
    assert stale["state"] == pg.STALE


def test_declared_empty_findings_is_present_not_absent(tmp_path):
    """`mise_prepped_findings: []` is a declared-empty. Reading it as absent would
    report every clean certification MALFORMED — precisely inverted."""
    from coordinator_core.frontmatter.primitives import canonical_body_sha

    text = _stamped(tmp_path)
    result = pg.read_stamp(
        _stamped(
            tmp_path,
            mise_prepped_by="s",
            mise_prepped_at="2026-09-07T00:00:00Z",
            mise_prepped_sha=f'"{canonical_body_sha(text)}"',
            mise_prepped_findings="[]",
        )
    )
    assert result["state"] == pg.CERTIFIED
    assert result["findings"] == []


def test_the_sha_excludes_frontmatter_so_a_stamp_survives_its_own_write(tmp_path):
    """The load-bearing property of `canonical_body_sha`: only a material change
    to the plan BODY invalidates a stamp. Not `blitz_land :: _git_blob_sha`, which
    hashes the whole file and would report every plan stale the moment its own
    stamp landed."""
    from coordinator_core.frontmatter.primitives import canonical_body_sha

    before = _stamped(tmp_path)
    after = _stamped(tmp_path, mise_prepped_by="a-session-that-was-not-there-before")
    assert canonical_body_sha(before) == canonical_body_sha(after)


def test_a_schema_only_refusal_does_not_name_a_converter_that_cannot_fix_it(tmp_path):
    """example-retrieval-repo, 2026-09-11: they ran `mise-prep-upgrade --upgrade` across the
    whole refused set and got `0 would be written`.

    The converter DERIVES missing declarations from the plan's body. It cannot
    repair a value that is present and the wrong SHAPE, so naming it for a
    schema-only refusal costs the author the run it takes to find that out.
    """
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path,
        _write_plan(
            tmp_path,
            frontmatter=(
                "census: []\n"
                "prime_exit_criterion:\n"
                "  statement: the four fields land and validate\n"
                "  derived_from: this is prose, not a sizings path\n"
            ),
            spine=_CLEAN_SPINE,
        ),
    )
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SCHEMA"]["kind"] == "schema-invalid"
    assert "mise-prep-upgrade.py" not in report["message"]
    assert "by hand" in report["message"]


def test_a_derivable_defect_still_routes_to_the_converter(tmp_path):
    """The negative verdict, and why `_only_schema_defect` is scoped to SCHEMA
    ALONE: a plan also missing a census has derivable work the converter really
    can do, so a co-occurring shape error must not route it away."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    report = _gate(
        tmp_path,
        _write_plan(
            tmp_path,
            frontmatter=(
                "prime_exit_criterion:\n"
                "  statement: the four fields land and validate\n"
                "  derived_from: this is prose, not a sizings path\n"
            ),
            spine=_CLEAN_SPINE,
        ),
    )
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["CENSUS"]["status"] != "PASS"
    assert "mise-prep-upgrade.py" in report["message"]


def test_a_date_typed_created_is_not_a_schema_defect(tmp_path):
    """The false positive example-retrieval-repo reported fleet-wide on 2026-09-11, pinned
    against so it cannot become true later.

    `created: 2026-08-20` unquoted parses as a `datetime.date` while the schema
    says `type: string`. `validate_frontmatter` coerces it — that leniency is
    CONTRACT (claude-klabauter CLAUDE.md § Architecture: ~1350 records rely on
    it), and `blitz_land` writes `execution_authorized_at` the same way, so a
    gate rejecting these would reject the engine's own writer's output.

    Measured when the report came in: across example-retrieval-repo's 248 plans, 43 are
    schema-invalid and ZERO are invalid only on date-typed fields.
    """
    import datetime

    fm = {
        "title": "fixture",
        "author": "fixture-session",
        "status": "draft",
        "created": datetime.date(2026, 8, 20),
        "census": [],
        "prime_exit_criterion": {
            "statement": "the four fields land and validate",
            "derived_from": "state/sizings/2026-09-07-fixture.yaml",
        },
    }
    assert pg._schema(fm, pg._pass("declared"))["status"] == "PASS"


# ---------------------------------------------------------------------------
# Parity leg: writes-archive-refused-in-wave (coordinator-content-repo mise-prep-gate.py)
# ---------------------------------------------------------------------------


def test_a_row_writing_under_archive_is_refused_in_wave(tmp_path):
    """coordinator-content-repo ``mise-prep-gate.py``'s ``writes-archive-refused-in-wave``
    leg, restated here (2026-09-18-doe-holds-no-scripts, leg 1). A row writing
    under ``archive/`` outside the guard's own carve-outs is BLOCKED at
    dispatch time by `block_subagent_archive_write`; the bar must catch it at
    authoring time instead of certifying a plan whose row cannot land.
    """
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = _CLEAN_SPINE.replace(
        "  writes: [coordinator_core/roadmap/prep_gate.py]\n",
        "  writes: [archive/specs/fixture-spec.md]\n",
    ).replace(
        "  surface: coordinator_core/roadmap/prep_gate.py\n",
        "  surface: archive/specs/fixture-spec.md\n",
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "writes-archive-refused-in-wave"
    assert "C1 (archive/specs/fixture-spec.md)" in report["classes"]["SPINE"]["detail"]


def test_a_row_writing_a_carve_out_shaped_archive_path_passes(tmp_path):
    """The guard's carve-outs (daily summary / completed / week-changelog) are
    file-shaped and this leg calls them, never restates them — a row writing
    one of those exact shapes is not refused."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = _CLEAN_SPINE.replace(
        "  writes: [coordinator_core/roadmap/prep_gate.py]\n",
        "  writes: [archive/daily-summaries/2026-09-18.md]\n",
    ).replace(
        "  surface: coordinator_core/roadmap/prep_gate.py\n",
        "  surface: archive/daily-summaries/2026-09-18.md\n",
    )
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["SPINE"]["kind"] != "writes-archive-refused-in-wave"


def test_a_row_writing_under_archive_via_writes_under_is_refused_in_wave(tmp_path):
    """`writes_under:` names a PREFIX (names chosen at run time), and the
    guard's file-shaped carve-outs cannot be checked against a prefix — so
    ANY archive-shaped prefix refuses, concretize-into-writes being the fix."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    spine = _CLEAN_SPINE.rstrip("\n") + "\n  writes_under: [archive/memos/]\n"
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "writes-archive-refused-in-wave"
    assert "writes_under" in report["classes"]["SPINE"]["detail"]


def _dr_spine(path: str) -> str:
    return _CLEAN_SPINE.replace(
        "  writes: [coordinator_core/roadmap/prep_gate.py]\n", f"  writes: [{path}]\n"
    ).replace("  surface: coordinator_core/roadmap/prep_gate.py\n", f"  surface: {path}\n")


def test_a_row_pinning_a_new_dr_number_is_refused(tmp_path):
    """Plans that pick a new DR number collide with each other; the number is
    minted at write time, so the row declares the directory instead."""
    (tmp_path / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "decisions" / "DR-421-old.md").write_text("x", encoding="utf-8")
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_dr_spine("docs/decisions/DR-422-new.md")))
    assert report["verdict"] == pg.NOT_PREPPED
    assert report["classes"]["SPINE"]["kind"] == "dr-number-pinned"


def test_a_row_editing_an_existing_dr_is_not_refused(tmp_path):
    (tmp_path / "docs" / "decisions").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "decisions" / "DR-421-old.md").write_text("x", encoding="utf-8")
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_dr_spine("docs/decisions/DR-421-old.md")))
    assert report["classes"]["SPINE"]["kind"] != "dr-number-pinned"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("../example-retrieval-repo/x.py", "project-rag"),
        ("..\\example-retrieval-repo\\x.py", "project-rag"),
        ("./../example-retrieval-repo", "project-rag"),
        ("example-retrieval-repo-ue-addon/x.py", "example-retrieval-repo-ue-addon"),
        ("../example-retrieval-repo-ue-addon/x.py", "example-retrieval-repo-ue-addon"),
        ("example-retrieval-repo@x.py", "project-rag"),
        ("example-retrieval-repo-other/x.py", None),
        ("example_retrieval_repo_two/x.py", None),
    ],
)
def test_matched_sibling_normalises_paths_and_matches_names_exactly(value, expected):
    assert pg._matched_sibling(value, ("project-rag", "example-retrieval-repo-ue-addon")) == expected


def test_a_dotdot_sibling_read_is_cleared_by_its_ungated_entry(tmp_path):
    path = "../example-retrieval-repo-ue-addon/x.py"
    spine = _reads_spine("", owner="..").replace("../coordinator_core/x.py", path[3:]).replace(
        f"reads: [{path[3:]}]", f"reads: [{path}]"
    )
    entries = (
        "  external_reads_ungated:\n"
        f"    - path: {path}\n"
        "      owner_repo: example-retrieval-repo-ue-addon\n"
        "      reason: read-only, examined\n"
    )
    spine = spine.replace("  queue_scope", entries + "  queue_scope", 1)
    report = _gate(tmp_path, _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine))
    assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", report["message"]


def _workflow_spine(write: str, *, extra: str = "") -> str:
    return (
        "- id: C1\n  title: t\n  change_kind: code-edit\n"
        f"  surface: docs/x.md\n  writes: [docs/x.md, {write}]\n{extra}"
        "  queue_scope: project\n  disposition: open\n"
    )


@pytest.mark.parametrize(
    "write", [".github/workflows/ci.yml", "./.github/workflows/ci.yml", ".github\\workflows\\ci.yml"]
)
def test_a_workflow_write_is_refused_and_its_row_withheld(tmp_path, write):
    plan = _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=_workflow_spine(write))
    report = _gate(tmp_path, plan)
    cls = report["classes"]["CI_RETIRED"]
    assert cls["kind"] == "ci-retired-workflow-write"
    assert cls["withheld"] == ["C1"]
    assert "cross-platform-ci-discipline.md" in cls["detail"]
    assert report["verdict"] == pg.NOT_PREPPED
    assert "CI_RETIRED" in pg.CLASS_ORDER
    assert "CI_RETIRED" in report["message"]


def test_a_non_workflow_github_write_and_a_closed_workflow_row_pass(tmp_path):
    spine = _workflow_spine(".github/CODEOWNERS")
    closed = _workflow_spine(".github/workflows/ci.yml").replace("C1", "C2").replace(
        "disposition: open", "disposition: wont_do"
    )
    plan = _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine + closed)
    assert _gate(tmp_path, plan)["classes"]["CI_RETIRED"]["status"] == "PASS"


def test_a_done_disposition_reads_as_coded(tmp_path):
    spine = _workflow_spine(".github/workflows/ci.yml").replace("disposition: open", "disposition: done")
    plan = _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine)
    assert _gate(tmp_path, plan)["classes"]["CI_RETIRED"]["status"] == "PASS"
    rows = pg.raw_spine_rows(plan.read_text(encoding="utf-8"))
    assert [r["disposition"] for r in rows] == ["coded"]


def test_the_ue_addon_is_a_fleet_name_distinct_from_example_retrieval_repo():
    assert "example-retrieval-repo-ue-addon" in pg.FLEET_REPOS
    assert pg._matched_sibling("example-retrieval-repo-ue-addon/x.py", pg.FLEET_REPOS) == "example-retrieval-repo-ue-addon"


@pytest.mark.parametrize(
    "statement",
    [
        "tests covering touched files pass; does not need the fast tier",
        "tests pass, not the fast tier",
        "never run the full suite here",
        "verified without the broad suite",
    ],
)
def test_a_negated_suite_tier_mention_is_not_refused(statement):
    from coordinator_core.roadmap.post_stamp_clause import suite_tier_refusal

    assert suite_tier_refusal(statement) is None


@pytest.mark.parametrize(
    "statement",
    [
        "the fast tier passes",
        "not flaky; the full suite is green",
        "does not regress and the fast tier passes",
        "tier-U green",
    ],
)
def test_a_real_suite_tier_mention_still_refuses(statement):
    from coordinator_core.roadmap.post_stamp_clause import suite_tier_refusal

    assert suite_tier_refusal(statement)


# ---------------------------------------------------------------------------
# Repair fields: mechanical / repair on every finding
# ---------------------------------------------------------------------------


def test_external_dep_undeclared_is_mechanical_and_names_the_paths() -> None:
    finding = pg._defect("external-dep-undeclared", "reads ../sibling/x.py", withheld=["r1"])
    assert finding["mechanical"] is True
    assert "external_gate" in finding["repair"]
    assert "reads_at_head" in finding["repair"]
    assert "../sibling/x.py" in finding["repair"]


def test_prime_exit_suite_tier_is_mechanical_and_names_the_field() -> None:
    finding = pg._refuse("prime-exit-suite-tier", "names the fast tier")
    assert finding["mechanical"] is True
    assert "prime_exit_criterion" in finding["repair"]
    assert "names the fast tier" in finding["repair"]


@pytest.mark.parametrize("kind", ["census-incomplete", "wave-cycle", "ValueError", "nonesuch"])
def test_judgment_and_unlisted_kinds_are_not_mechanical(kind: str) -> None:
    finding = pg._defect(kind, "whatever")
    assert finding["mechanical"] is False
    assert finding["repair"] is None


def test_pass_carries_no_repair() -> None:
    finding = pg._pass("ok")
    assert finding["mechanical"] is False
    assert finding["repair"] is None


def _certified_text(sha_of) -> str:
    spine = (
        "- id: C1\n  title: one\n  surface: s\n  writes: [a.py]\n  disposition: open\n"
        "- id: C2\n  title: two\n  surface: s\n  writes: [b.py]\n  disposition: open\n"
    )
    fm = "title: t\nstatus: approved\n"
    text = f"---\n{fm}---\n\n# P\n\n## Tasks\n\n```yaml plan-tasks\n{spine}```\n"
    sha = sha_of(text)
    return text.replace(
        fm,
        fm + f'mise_prepped_by: s\nmise_prepped_at: "2026-09-07T00:00:00Z"\n'
        f'mise_prepped_sha: "{sha}"\nmise_prepped_findings: []\n',
        1,
    )


def test_a_coded_flip_keeps_the_stamp_certified():
    from coordinator_core.frontmatter.primitives import approval_body_sha

    text = _certified_text(approval_body_sha)
    assert pg.read_stamp(text)["state"] == pg.CERTIFIED

    coded = text.replace("writes: [a.py]\n  disposition: open", "writes: [a.py]\n  disposition: coded\n  disposition_ref: abc1234", 1)
    assert coded != text
    assert pg.read_stamp(coded)["state"] == pg.CERTIFIED


def test_a_real_body_edit_still_reads_stale():
    from coordinator_core.frontmatter.primitives import approval_body_sha

    text = _certified_text(approval_body_sha)

    assert pg.read_stamp(text.replace("title: two", "title: three", 1))["state"] == pg.STALE
    assert pg.read_stamp(text.replace("disposition: open", "disposition: wont_do", 1))["state"] == pg.STALE


def test_a_stamp_minted_over_the_raw_body_hash_still_certifies():
    from coordinator_core.frontmatter.primitives import canonical_body_sha

    assert pg.read_stamp(_certified_text(canonical_body_sha))["state"] == pg.CERTIFIED
