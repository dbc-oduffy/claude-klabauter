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

import time
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
    fm = ["title: fixture", "status: draft", "created: 2026-09-07"]
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
    command: "false"
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
# EXTERNAL_DEPS — the three-way split
# ---------------------------------------------------------------------------


def _external_plan(tmp_path, gate_block: str = "") -> Path:
    spine = f"""- id: C1
  title: Reaches out
  change_kind: code-edit
  surface: DoE-claude/coordinator/bin/mise-prep-gate.py
  writes: []
  queue_scope: project
  disposition: open
{gate_block}"""
    return _write_plan(tmp_path, frontmatter=_CLEAN_FM, spine=spine)


def test_a_row_naming_a_sibling_repo_without_a_gate_is_not_prepped(tmp_path):
    report = _gate(tmp_path, _external_plan(tmp_path))
    deps = report["classes"]["EXTERNAL_DEPS"]
    assert deps["kind"] == "external-dep-undeclared"
    assert "DoE-claude" in deps["detail"]
    assert report["verdict"] == pg.NOT_PREPPED


def test_a_gate_with_no_requires_is_not_prepped(tmp_path):
    block = """  external_gate:
    - owner_repo: DoE-claude
      condition: the schema lands
"""
    report = _gate(tmp_path, _external_plan(tmp_path, block))
    assert report["classes"]["EXTERNAL_DEPS"]["kind"] == "external-dep-undeclared"
    assert "requires" in report["classes"]["EXTERNAL_DEPS"]["detail"]


def test_landed_work_withholds_its_own_row_and_the_plan_still_certifies(tmp_path):
    """Row granularity, deliberately: refusing the plan would discard every
    schedulable row alongside the blocked one."""
    block = f"""  external_gate:
    - owner_repo: DoE-claude
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
    - owner_repo: DoE-claude
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
    - owner_repo: DoE-claude
      condition: someone commits there
      requires: {pg.REQUIRES_COMMIT}
"""
    landed_block = f"""  external_gate:
    - owner_repo: DoE-claude
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
    - owner_repo: DoE-claude
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
    - owner_repo: DoE-claude
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


def test_the_repo_s_own_name_is_never_a_sibling(tmp_path):
    """An intra-repo blocker is a `depends_on` edge, never a gate. A list that
    kept the running repo's own name would fire the DR-127 leg on every plan that
    cites its own tree by name."""
    own = tmp_path / "claude-klabauter"
    own.mkdir()
    assert "claude-klabauter" not in pg.fleet_siblings(own)
    assert "DoE-claude" in pg.fleet_siblings(own)


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
    assert "DoE-claude" in pg.FLEET_REPOS
    doctrine = tmp_path / "DoE-claude"
    doctrine.mkdir()
    assert "DoE-claude" not in pg.fleet_siblings(doctrine)
    assert "claude-klabauter" in pg.fleet_siblings(doctrine)


def test_the_repo_s_own_name_is_subtracted_case_insensitively(tmp_path):
    """A clone at `doe-claude/` and one at `DoE-claude/` are the same repo. A
    case-sensitive subtraction would report every self-naming row in the
    lower-case clone as a cross-repo dependency."""
    own = tmp_path / "doe-claude"
    own.mkdir()
    assert "DoE-claude" not in pg.fleet_siblings(own)


def test_a_sibling_named_in_a_different_case_is_still_caught(tmp_path):
    """Defect 2. The corpus does not agree with itself on case — example-retrieval-repo's own
    plans and cross-repo archive spell the doctrine repo `doe-claude` 1207 times
    against `DoE-claude` 1052 — so a case-sensitive `==`/`startswith` left a row
    declaring a genuine cross-repo surface in the corpus's own spelling unseen by
    the SIBLING-NAME leg entirely."""
    for value in (
        "doe-claude/coordinator/bin/thing.py",
        "DOE-CLAUDE@coordinator/bin/thing.py",
        "Doe-Claude",
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
            "- id: C1\n  title: t\n  change_kind: doc-edit\n"
            f"  surface: docs/x.md\n  writes: [{value}]\n"
            "  queue_scope: project\n  disposition: open\n"
        )
        report = _gate(tmp_path, _plan_with(tmp_path, spine, "root.md"))
        assert report["classes"]["EXTERNAL_DEPS"]["status"] == "PASS", value
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


# ---------------------------------------------------------------------------
# Budget — DR-344
# ---------------------------------------------------------------------------


def test_every_real_plan_holds_the_brightline():
    """500ms end-to-end, PROCESS TIME, per call, over this repo's real corpus —
    the brightline (DR-344), not a suspension bar.

    Per call, not per corpus, because one plan per call is the op's contract and
    the bound is a measurement: see `prep_gate.gate_plan` for the numbers and for
    the census surface a corpus-wide question routes to.

    Process time rather than wall clock: wall clock measures peer load on a box
    running ~50 sessions, and calibrating to it would let a busy box excuse a slow
    op or a quiet one hide a fast-growing one.
    """
    plans = sorted((REPO_ROOT / "docs" / "plans").glob("*.md"))
    assert plans, "empty corpus proves nothing about cost"

    root_names = pg.repo_root_names(REPO_ROOT)
    siblings = pg.fleet_siblings(REPO_ROOT)
    # Warm the deferred spine/primitive imports so the first plan is not charged
    # for every later plan's import cost.
    pg.evaluate_plan(plans[0], root_names=root_names, siblings=siblings)

    worst_ms = 0.0
    worst_plan = plans[0]
    for path in plans:
        start = time.process_time()
        pg.evaluate_plan(path, root_names=root_names, siblings=siblings)
        elapsed_ms = (time.process_time() - start) * 1000
        if elapsed_ms > worst_ms:
            worst_ms, worst_plan = elapsed_ms, path

    assert worst_ms < 500, (
        f"gating {worst_plan.name} took {worst_ms:.0f}ms process time — over the "
        f"500ms brightline, measured over {len(plans)} real plans. Cut the real "
        f"cost; do not raise this number."
    )


def test_the_gate_never_spawns_a_subprocess(monkeypatch):
    """The read half is pure. `git --version` alone costs 25.3ms, so a shell-out
    for the body sha would spend a process creation to compute what sha1 already
    answers — and this gate runs once per plan on the fire path."""
    import subprocess

    def _boom(*args, **kwargs):
        raise AssertionError("plan.prep_gate's read path must not create a process")

    plans = sorted((REPO_ROOT / "docs" / "plans").glob("*.md"))
    assert plans, "empty corpus proves nothing about spawns"
    root_names = pg.repo_root_names(REPO_ROOT)
    siblings = pg.fleet_siblings(REPO_ROOT)
    # Warm the deferred imports first: an import that itself spawns would be a
    # real finding, but it is a different one, and charging it here would report
    # the gate as spawning when the import did.
    pg.evaluate_plan(plans[0], root_names=root_names, siblings=siblings)

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    for path in plans[:40]:
        report = pg.evaluate_plan(path, root_names=root_names, siblings=siblings)
        pg.read_stamp(path.read_text(encoding="utf-8", errors="replace"))
        assert report["verdict"] in (pg.PREPPED, pg.NOT_PREPPED, pg.REFUSED)
