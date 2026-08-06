#!/usr/bin/env python3
from __future__ import annotations
"""
test_plan_tasks_spine_and_harvest.py — coordinator-side proving tests for the
machine-parseable ## Tasks task-spine contract (C1) and the PM-gated deferral
harvest (C4a/C4b/C6).

Spec backlink: docs/plans/2026-07-09-plan-full-coverage-and-deferred-harvest.md § C7

This suite is coordinator-side only (no claude-klabauter-side test — M1 is a
collaborative consultation memo, not an engine commission per the plan's
Architecture section). It exercises the pinned interfaces:

  - coordinator/schemas/plan-tasks.schema.json (C1 — per-row schema, incl. the
    pm_approved-required-when-deferred allOf/if-then cross-field rule that is
    the mechanical proxy for plan-coverage-checker's Lens 2b deferral-
    ratification check — the checker itself is an agent-prompt, not code, so
    this schema conditional is the closest coordinator-side-testable surface
    for "a deferred-without-pm_approved row is flagged").
  - coordinator/bin/coordinator-harvest-deferrals (C4a — the harvest CLI).

Fixtures live under coordinator/bin/tests/fixtures/plan-tasks-spine/ —
throwaway plan markdown files exercising: a well-formed spine with mixed
deferred/non-deferred/queue_scope rows (valid-spine-with-deferrals.md), the
two parser-locate error states (zero-fenced-blocks.md,
multiple-fenced-blocks.md), and a malformed row missing required fields
(malformed-row.md).

This is now the SOLE canonical plan-tasks-spine fixture corpus — it was
consolidated from two separate, partly-overlapping fixture dirs (this one
and coordinator_core/frontmatter/tests/fixtures/plan-tasks-spine/, now
retired) precisely because separate corpora meant no test could ever fail
on a divergence between example-doctrine-repo's `_locate_tasks_block` (below) and its ported
successor `coordinator_core.frontmatter.body_blocks.locate_fenced_block`.
The fixture-name -> expected-locate-outcome table both suites parametrize
over lives in this same dir: fixtures/plan-tasks-spine/fixture_expectations.py
— see test_locator_parity_against_shared_expectation_table below, and that
module's docstring for the full rationale.

Test isolation: every harvest invocation below runs inside a freshly
`git init`-ed temp directory (see _run_harvest_in_isolated_repo) with cwd set
to that directory AND both QUEUE_APPEND_OUTPUT_ROOT and
LESSON_PROMOTE_OUTBOX_ROOT pointed at it, so no test run ever touches this
repo's real state/improvement-queue/ or state/lessons-outbox/.

FIXED — see test_harvest_idempotency_env_override.py for the regression test
proving the fix. (Prior state, for history: this docstring previously
described a KNOWN UPSTREAM DEFECT in coordinator-harvest-deferrals'
_candidate_search_dirs()/_repo_root() env-override precedence, confirmed via
direct reproduction — two harvest runs against the same fixture plan with
only QUEUE_APPEND_OUTPUT_ROOT set and a mismatched cwd produced duplicate
queue/lesson entries for the same (plan_id, row id) key. That precedence gap
is now closed: _candidate_search_dirs() checks each write seam's env override
first, matching coordinator-queue-append/_output_path() and
coordinator-lesson-promote's _outbox_root(). A follow-up review (2026-07-09,
slice2 Finding 1) further found the fix's central/lessons legs mirrored only
the DOE_ROOT-env leg of coordinator_registry.doe_root()'s three-step
resolution chain, missing the machine-local-registry leg — that gap is also
now closed by calling doe_root() directly rather than re-deriving it; see
test_harvest_doe_root_machine_local_leg.py for that regression's own test.)
This suite's own harvest invocations still cwd into a `git init`-ed fixture
dir per test (harmless with the fix in place — it exercises the git-root
fallback leg used only when neither an env override nor doe_root() resolve).

Run with: python3 -m pytest test_plan_tasks_spine_and_harvest.py
"""

import importlib.machinery
import importlib.util
import json
import pytest
import os
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml as _yaml  # PyYAML — available on most coordinator installs
    _YAML_AVAILABLE = True
except ImportError:
    _yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False

try:
    import jsonschema as _jsonschema
    _JSONSCHEMA_AVAILABLE = True
except ImportError:
    _jsonschema = None  # type: ignore[assignment]
    _JSONSCHEMA_AVAILABLE = False

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)  # coordinator/bin
_COORDINATOR_DIR = os.path.dirname(_BIN_DIR)  # coordinator/

_LIB_DIR = os.path.join(_BIN_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from coordinator_data_root import data_root  # noqa: E402

_HARVEST_CLI = os.path.join(_BIN_DIR, "coordinator-harvest-deferrals")
# schemas/ is example-doctrine-repo-resident post-2026-07-22 executable-surface migration (this
# script moved to claude-klabauter; schemas/ did not) — resolve via the shared
# two-rung helper rather than a bare _COORDINATOR_DIR-relative path.
_PLAN_TASKS_SCHEMA = os.path.join(str(data_root("schemas")), "plan-tasks.schema.json")
_FIXTURES_DIR = os.path.join(_THIS_DIR, "fixtures", "plan-tasks-spine")

_FIXTURE_VALID = os.path.join(_FIXTURES_DIR, "valid-spine-with-deferrals.md")
_FIXTURE_ZERO_BLOCKS = os.path.join(_FIXTURES_DIR, "zero-fenced-blocks.md")
_FIXTURE_MULTI_BLOCKS = os.path.join(_FIXTURES_DIR, "multiple-fenced-blocks.md")
_FIXTURE_MALFORMED = os.path.join(_FIXTURES_DIR, "malformed-row.md")
_FIXTURE_TEMPLATE_COMMENT = os.path.join(_FIXTURES_DIR, "template-comment-with-deferral.md")
_FIXTURE_ZERO_BLOCKS_WITH_DEFERRED = os.path.join(_FIXTURES_DIR, "zero-blocks-with-deferred-marker.md")
_FIXTURE_PROSE_BETWEEN = os.path.join(_FIXTURES_DIR, "prose-between-heading-and-fence.md")

_SUBPROCESS_TIMEOUT_SECS = 30

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _run_harvest_in_isolated_repo(
    plan_source_path: str,
    extra_env: dict[str, str] | None = None,
    dry_run: bool = False,
) -> tuple[subprocess.CompletedProcess, str]:
    """Copy a fixture plan into a fresh git-init'ed temp dir and invoke
    coordinator-harvest-deferrals against it, with cwd AND the two write-seam
    output-root env overrides all pointed at the same isolated temp dir — see
    the module docstring's FIXED note (formerly KNOWN UPSTREAM DEFECT) for the
    env-override/cwd precedence history this helper's isolation shape guards
    against.

    Returns (CompletedProcess, tmpdir_path). Caller is responsible for
    tmpdir cleanup — this helper does NOT use a context manager because
    several tests need to run the harvest twice (idempotency) against the
    SAME tmpdir before it goes out of scope.
    """
    tmpdir = tempfile.mkdtemp(prefix="harvest-test-")
    subprocess.run(["git", "init", "-q"], cwd=tmpdir, capture_output=True, text=True, timeout=10)

    plan_path = os.path.join(tmpdir, "plan.md")
    with open(plan_path, "w", encoding="utf-8") as fh:
        fh.write(_read(plan_source_path))

    env = dict(os.environ)
    env["QUEUE_APPEND_OUTPUT_ROOT"] = tmpdir
    env["LESSON_PROMOTE_OUTBOX_ROOT"] = os.path.join(tmpdir, "state", "lessons-outbox")
    # Avoid any ambient DOE_ROOT/CLAUDE_KLABAUTER_ROOT bleeding central-scope writes
    # out of the isolated tmpdir.
    env.pop("DOE_ROOT", None)
    env.pop("CLAUDE_KLABAUTER_ROOT", None)
    if extra_env:
        env.update(extra_env)

    cmd = ["python3", os.path.abspath(_HARVEST_CLI), "--plan", plan_path]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(
        cmd, cwd=tmpdir, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS
    )
    return result, tmpdir


def _yaml_files_in(directory: str) -> list[str]:
    if not os.path.isdir(directory):
        return []
    return sorted(f for f in os.listdir(directory) if f.endswith(".yaml"))


# ===========================================================================
# (a) Spine parses, including the two parser-locate error states
# ===========================================================================


def test_valid_spine_parses_via_harvest_dry_run() -> None:
    name = "test_valid_spine_parses_via_harvest_dry_run"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_VALID, dry_run=True)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        if "D1" not in result.stdout or "D2" not in result.stdout:
            raise AssertionError(
                name + ": " +
                f"expected dry-run output to name harvest candidates D1 and D2, got: {result.stdout!r}",
            )
        if "D3" in result.stdout:
            raise AssertionError(
                name + ": " +
                "D3 (deferred:true, pm_approved:false) must NOT be a harvest candidate, "
                f"but appeared in dry-run output: {result.stdout!r}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zero_fenced_blocks_is_warn_and_skip() -> None:
    name = "test_zero_fenced_blocks_is_warn_and_skip"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_ZERO_BLOCKS, dry_run=True)
    try:
        # Parser-locate rule: zero fenced blocks -> WARN-AND-SKIP (exit 0) for
        # the harvest, per the pinned contract (fail-loud is the
        # coverage-checker's posture, not this CLI's).
        if result.returncode != 0:
            raise AssertionError(
                name + ": " +
                f"expected exit 0 (warn-and-skip), got {result.returncode}. stderr={result.stderr}",
            )
        if "Queued 0 deferred items" not in result.stdout and "skipping harvest" not in result.stderr.lower():
            raise AssertionError(
                name + ": " +
                f"expected a 'skipping harvest'/zero-queued signal. stdout={result.stdout!r} stderr={result.stderr!r}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_multiple_fenced_blocks_is_warn_and_skip() -> None:
    name = "test_multiple_fenced_blocks_is_warn_and_skip"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_MULTI_BLOCKS, dry_run=True)
    try:
        if result.returncode != 0:
            raise AssertionError(
                name + ": " +
                f"expected exit 0 (warn-and-skip on ambiguous >1-block spine), got {result.returncode}. "
                f"stderr={result.stderr}",
            )
        if "skipping harvest" not in result.stderr.lower():
            raise AssertionError(
                name + ": " +
                f"expected a 'skipping harvest' warning naming the ambiguous spine. stderr={result.stderr!r}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_template_comment_is_located_and_deferred_row_harvested() -> None:
    """Regression for the silent-data-loss bug: a plan that still carries
    the writing-plans.md template's unedited authoring HTML comment
    directly under '## Tasks' (which embeds a literal
    ```yaml plan-tasks``` string AND sits as non-blank content between the
    heading and the real fence — BOTH of _locate_tasks_block's former
    false-negative paths at once) must still be LOCATED, and its
    deferred:true/pm_approved:true row (D1) must still be harvested — not
    silently skipped.
    """
    name = "test_template_comment_is_located_and_deferred_row_harvested"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_TEMPLATE_COMMENT)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        if "Queued 1 deferred items" not in result.stdout:
            raise AssertionError(
                name + ": " +
                f"expected the D1 deferred row to be located and harvested despite the "
                f"template comment, got stdout={result.stdout!r} stderr={result.stderr!r}",
            )
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        files = _yaml_files_in(qdir)
        if len(files) != 1:
            raise AssertionError(name + ": " + f"expected exactly 1 improvement-queue yaml file, found {files} in {qdir}")
        content = _read(os.path.join(qdir, files[0]))
        if "harvest-key: pln-fixture-template-comment-000001:D1" not in content:
            raise AssertionError(name + ": " + f"expected the harvest-key idempotency marker, got:\n{content}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_prose_between_heading_and_fence_is_located_and_harvested() -> None:
    """Regression for the containment-vs-adjacency bug: a plan carrying
    load-bearing prose (a pinned-interface paragraph, a wave map) between
    the '## Tasks' heading and the real fence must still be LOCATED, and
    its deferred:true/pm_approved:true row (D1) harvested.

    The former guard permitted only blank lines between heading and fence,
    so every real reviewed plan with an interface/wave-map paragraph there
    warn-and-skipped, silently losing its ratified deferrals. Adjacency was
    never load-bearing for disambiguation — exactly-one-fence-in-the-whole
    -document is already enforced before position is examined — so the guard
    could only produce false negatives. Containment (fence lives inside the
    '## Tasks' section) is what actually needed enforcing, and the fixture
    carries a trailing '## Some Later Section' to prove that bound is real.

    Originating incident: docs/plans/2026-07-20-machine-blind-repo-identity.md
    parsed fine under plan-coverage-checker (8 rows) while this CLI reported
    "no locatable block" — two consumers of one pinned contract disagreeing
    on what parses, when the contract says they differ only on severity.

    Uses --dry-run deliberately: the write path subprocesses the extensionless
    `coordinator-queue-append`, which is not executable on Windows (WinError 193),
    so the harvest-and-write tests are red on this platform for reasons unrelated
    to the locator. Dry-run exercises the fixed locator end-to-end and stays
    platform-clean.
    """
    name = "test_prose_between_heading_and_fence_is_located_and_harvested"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_PROSE_BETWEEN, dry_run=True)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        if "no locatable" in result.stderr:
            raise AssertionError(
                name + ": " +
                "locator warn-and-skipped a well-formed spine because load-bearing prose "
                f"sits between the '## Tasks' heading and the fence: stderr={result.stderr!r}",
            )
        if "D1" not in result.stdout:
            raise AssertionError(
                name + ": " +
                f"expected the D1 deferred row to be located as a harvest candidate despite "
                f"load-bearing prose between heading and fence, got "
                f"stdout={result.stdout!r} stderr={result.stderr!r}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_zero_blocks_with_deferred_marker_is_loud_nonzero_exit() -> None:
    """Belt-and-suspenders silent-data-loss guard: when _locate_tasks_block
    genuinely fails (no real fence anywhere) BUT the '## Tasks' region
    visibly contains a 'deferred: true' line, the harvest must escalate to
    a LOUD, non-zero exit — not the default soft exit-0 skip — since that
    combination is the exact silent-loss shape this fix targets.
    """
    name = "test_zero_blocks_with_deferred_marker_is_loud_nonzero_exit"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_ZERO_BLOCKS_WITH_DEFERRED, dry_run=True)
    try:
        if result.returncode == 0:
            raise AssertionError(
                name + ": " +
                f"expected a NON-ZERO exit (loud failure) when a deferred:true line is present "
                f"but no fenced block could be located, got exit 0. stdout={result.stdout!r} "
                f"stderr={result.stderr!r}",
            )
        if "SILENTLY LOST" not in result.stderr and "silently lost" not in result.stderr.lower():
            raise AssertionError(
                name + ": " +
                f"expected a loud stderr warning naming the silent-loss risk, got: {result.stderr!r}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_multiple_fenced_blocks_with_deferred_marker_still_soft_skips_when_no_marker() -> None:
    """Confirms the genuinely-ambiguous two-REAL-fence case (no comment
    involved) is unaffected by the comment-blanking fix: it must still
    return None / warn-and-skip at exit 0, since the existing
    multiple-fenced-blocks.md fixture carries no deferred:true line.
    """
    name = "test_multiple_fenced_blocks_with_deferred_marker_still_soft_skips_when_no_marker"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_MULTI_BLOCKS, dry_run=True)
    try:
        if result.returncode != 0:
            raise AssertionError(
                name + ": " +
                f"expected exit 0 (soft skip — fixture has no deferred:true line), got "
                f"{result.returncode}. stderr={result.stderr}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# (a.1) Locate-rule PARITY against the shared expectation table.
#
# This is the test that closes the gap 08cbf4bd's fix exposed: example-doctrine-repo's
# `_locate_tasks_block` and coordinator_core's `locate_fenced_block` are
# exercised over the SAME fixture corpus via the SAME expectation table
# (fixtures/plan-tasks-spine/fixture_expectations.py), so a future
# divergence fails HERE instead of surviving in a docstring claim of parity.
# `_locate_tasks_block` is loaded directly (not subprocessed) so this test
# calls it as a plain function, mirroring test_lesson_promote.py's pattern
# for importing a hyphenated, extensionless CLI file as a Python module.
# ===========================================================================


_harvest_loader = importlib.machinery.SourceFileLoader("coordinator_harvest_deferrals", _HARVEST_CLI)
_harvest_spec = importlib.util.spec_from_loader("coordinator_harvest_deferrals", _harvest_loader)
_harvest_mod = importlib.util.module_from_spec(_harvest_spec)  # type: ignore[arg-type]
_harvest_loader.exec_module(_harvest_mod)
_locate_tasks_block = _harvest_mod._locate_tasks_block

_exp_loader = importlib.machinery.SourceFileLoader(
    "plan_tasks_spine_fixture_expectations", os.path.join(_FIXTURES_DIR, "fixture_expectations.py")
)
_exp_spec = importlib.util.spec_from_loader("plan_tasks_spine_fixture_expectations", _exp_loader)
_exp_mod = importlib.util.module_from_spec(_exp_spec)  # type: ignore[arg-type]
_exp_loader.exec_module(_exp_mod)

FIXTURE_EXPECTATIONS = _exp_mod.FIXTURE_EXPECTATIONS
LocateOutcome = _exp_mod.LocateOutcome


@pytest.mark.parametrize("fixture_name", sorted(FIXTURE_EXPECTATIONS))
def test_locator_parity_against_shared_expectation_table(fixture_name: str) -> None:
    """example-doctrine-repo's `_locate_tasks_block` collapses ABSENT and MALFORMED into a
    single `None` return (see its own docstring) — the translation at this
    assertion site, not a second table, is how that collapsed shape is
    reconciled against the shared table's three-way `LocateOutcome`.
    """
    source = _read(os.path.join(_FIXTURES_DIR, fixture_name))
    expected_outcome = FIXTURE_EXPECTATIONS[fixture_name].outcome
    result = _locate_tasks_block(source)
    if expected_outcome is LocateOutcome.LOCATED:
        if result is None:
            raise AssertionError(
                f"test_locator_parity_against_shared_expectation_table[{fixture_name}]: "
                f"expected a LOCATED body (non-None), got None"
            )
    else:
        if result is not None:
            raise AssertionError(
                f"test_locator_parity_against_shared_expectation_table[{fixture_name}]: "
                f"expected None ({expected_outcome.value}), got a located body: {result!r}"
            )


# ===========================================================================
# (b) Ledger derivation excludes deferred rows AND still applies expansion.
#
# execute-plan Phase 1.6's derivation logic is prose in a SKILL.md, not a
# standalone script — there is no CLI to invoke. The mechanically-testable
# proxy is the spine ITSELF: assert that (1) a non-deferred row set (the
# ledger's derivation floor) excludes every deferred:true row by construction
# via a schema-shaped filter mirroring the documented rule, and (2) a single
# authoring-time row (C2 in the plan's actual spine, and the C2a/C2b split in
# our fixture) legitimately expands into >1 write-target when the expansion
# rule applies — i.e. row-count >= task-count is achievable and is NOT
# malformed. This test operates purely on the fixture's parsed rows (no
# subprocess) since there is no execute-plan CLI surface to invoke directly.
# ===========================================================================


def _load_valid_fixture_rows() -> list[dict]:
    text = _read(_FIXTURE_VALID)
    marker = "```yaml plan-tasks\n"
    start = text.index(marker) + len(marker)
    end = text.index("```", start)
    body = text[start:end]
    if _YAML_AVAILABLE:
        return _yaml.safe_load(body)  # type: ignore[no-any-return]
    raise AssertionError("_load_valid_fixture_rows" + ": " + "PyYAML not available — cannot parse fixture body")
    return []


def test_ledger_derivation_excludes_deferred_rows() -> None:
    name = "test_ledger_derivation_excludes_deferred_rows"
    if not _YAML_AVAILABLE:
        pytest.skip("PyYAML not installed")
        return
    rows = _load_valid_fixture_rows()
    if not rows:
        raise AssertionError(name + ": " + "fixture produced zero rows — fixture or parser broken")

    derived_chunk_set = [r for r in rows if r.get("deferred") is not True]
    derived_ids = {r["id"] for r in derived_chunk_set}
    deferred_ids = {r["id"] for r in rows if r.get("deferred") is True}

    if derived_ids & deferred_ids:
        raise AssertionError(
            name + ": " +
            f"derived chunk-set must exclude all deferred:true rows; overlap found: {derived_ids & deferred_ids}",
        )
    if derived_ids != {"C1", "C2a", "C2b"}:
        raise AssertionError(name + ": " + f"expected derived set {{'C1','C2a','C2b'}}, got {derived_ids}")
    if deferred_ids != {"D1", "D2", "D3"}:
        raise AssertionError(name + ": " + f"expected deferred set {{'D1','D2','D3'}}, got {deferred_ids}")


def test_ledger_expansion_row_count_exceeds_task_count() -> None:
    name = "test_ledger_expansion_row_count_exceeds_task_count"
    if not _YAML_AVAILABLE:
        pytest.skip("PyYAML not installed")
        return
    rows = _load_valid_fixture_rows()
    non_deferred = [r for r in rows if r.get("deferred") is not True]

    # The fixture's C2a/C2b pair models the disjoint-write-target expansion
    # rule: what a hand-authored plan would enumerate as a SINGLE "C2" chunk
    # with two disjoint write-targets (docs/wiki/widget-a.md,
    # docs/wiki/widget-b.md) is represented here as two already-split spine
    # rows — i.e. the spine-derived "task count" for the C2 unit of work is
    # 2, which is >= 1 and demonstrates the row-count-never-== invariant
    # holds at the row-authoring level (Phase 1.5/1.6 still runs its own
    # expansion on TOP of whatever the spine derives, per
    # execute-plan/SKILL.md line ~186 — this fixture proves the floor
    # relationship, not the Phase 1.6 mechanism itself, since that mechanism
    # has no standalone CLI to invoke).
    c2_surfaces = {r["surface"] for r in non_deferred if r["id"] in ("C2a", "C2b")}
    if len(c2_surfaces) < 2:
        raise AssertionError(
            name + ": " +
            f"expected >=2 disjoint write-target surfaces for the C2 expansion fixture, got {c2_surfaces}",
        )
    if len(non_deferred) < 3:
        raise AssertionError(name + ": " + f"expected derived non-deferred row-count >= 3 (C1, C2a, C2b), got {len(non_deferred)}")


# ===========================================================================
# (c) plan-coverage-checker FLAGS a deferred-without-pm_approved fixture row.
#
# plan-coverage-checker is an agent-prompt (agents/plan-coverage-checker.md),
# not executable code — there is no CLI to invoke against a fixture. The
# mechanically-testable proxy is the pinned schema's OWN cross-field rule:
# plan-tasks.schema.json's allOf/if-then conditional requires pm_approved
# whenever deferred is true. D3 in the valid fixture (deferred:true,
# pm_approved:false) legitimately VALIDATES against the base per-property
# schema (pm_approved:false is a valid boolean) but the malformed-row.md
# fixture's D1 (deferred:true, pm_approved:true, but MISSING change_kind and
# surface) fails validation outright — that is the more directly testable
# assertion this suite can make: an incomplete deferred row does not
# silently validate. For the "unratified but otherwise well-formed" shape
# (this suite's D3), we assert against the schema's documented conditional
# directly via jsonschema, which is the same conditional-shape enforcement
# that backs the checker's Lens 2b prose rule (agents/plan-coverage-checker.md
# Phase 3.5 Step 3).
# ===========================================================================


def test_schema_conditional_requires_pm_approved_when_deferred() -> None:
    name = "test_schema_conditional_requires_pm_approved_when_deferred"
    if not _JSONSCHEMA_AVAILABLE:
        pytest.skip("jsonschema module not installed")
        return
    with open(_PLAN_TASKS_SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)

    # A row that is deferred:true but omits pm_approved entirely must fail
    # the schema's cross-field allOf/if-then rule (pm_approved is
    # required-when-deferred) — this is the mechanical proxy for the
    # coverage-checker's "deferral pending PM ratification" finding.
    unratified_row = {
        "id": "D-unratified",
        "title": "Deferred without any pm_approved field at all",
        "change_kind": "skill-edit",
        "surface": "coordinator/skills/some-skill/SKILL.md",
        "deferred": True,
        # pm_approved deliberately omitted
    }
    try:
        _jsonschema.validate(instance=unratified_row, schema=schema)  # type: ignore[union-attr]
    except _jsonschema.exceptions.ValidationError:  # type: ignore[union-attr]
        return
    raise AssertionError(
        name + ": " +
        "expected jsonschema.validate to raise ValidationError for a deferred:true row "
        "missing pm_approved (the schema's pm_approved-required-when-deferred allOf/if-then "
        "conditional), but validation succeeded",
    )


def test_schema_conditional_allows_ratified_deferral() -> None:
    name = "test_schema_conditional_allows_ratified_deferral"
    if not _JSONSCHEMA_AVAILABLE:
        pytest.skip("jsonschema module not installed")
        return
    with open(_PLAN_TASKS_SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)

    ratified_row = {
        "id": "D1",
        "title": "Retro-migrate old widgets",
        "change_kind": "doc-edit",
        "surface": "docs/plans/",
        "deferred": True,
        "pm_approved": True,
    }
    try:
        _jsonschema.validate(instance=ratified_row, schema=schema)  # type: ignore[union-attr]
    except _jsonschema.exceptions.ValidationError as exc:  # type: ignore[union-attr]
        raise AssertionError(name + ": " + f"expected a fully-ratified deferred row to validate cleanly, got: {exc}")


def test_coverage_checker_prompt_documents_the_exact_flag_text() -> None:
    """Ground-truth check: the coverage-checker's agent-prompt (the only
    artifact this deliverable can inspect, since the checker itself is not
    executable) must still carry the documented finding text verbatim. This
    guards against silent drift between C1's schema conditional (tested
    above) and C3's prose enforcement of the SAME rule.
    """
    name = "test_coverage_checker_prompt_documents_the_exact_flag_text"
    # agents/ is example-doctrine-repo-resident post-2026-07-22 executable-surface migration —
    # resolve via the shared two-rung helper rather than a bare
    # _COORDINATOR_DIR-relative path.
    checker_path = os.path.join(str(data_root("agents")), "plan-coverage-checker.md")
    if not os.path.isfile(checker_path):
        raise AssertionError(name + ": " + f"expected file not found: {checker_path}")
    text = _read(checker_path)
    needle = "deferral pending PM ratification"
    if needle not in text:
        raise AssertionError(name + ": " + f"expected agents/plan-coverage-checker.md to contain {needle!r}")


# ===========================================================================
# (d) Harvest call-site fires: project scope, central scope, doctrine-edit
#     routing, and idempotency on a second run.
# ===========================================================================


def test_harvest_call_site_project_scope_queue_append() -> None:
    name = "test_harvest_call_site_project_scope_queue_append"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_VALID)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        files = _yaml_files_in(qdir)
        if len(files) != 1:
            raise AssertionError(name + ": " + f"expected exactly 1 improvement-queue yaml file, found {files} in {qdir}")
        content = _read(os.path.join(qdir, files[0]))
        if "queue_scope: project" not in content:
            raise AssertionError(name + ": " + f"expected queue_scope: project in written entry, got:\n{content}")
        if "harvest-key: pln-fixture-valid-spine-000001:D1" not in content:
            raise AssertionError(name + ": " + f"expected the harvest-key idempotency marker in 'evidence:', got:\n{content}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_harvest_call_site_doctrine_edit_routes_to_lesson_promote() -> None:
    name = "test_harvest_call_site_doctrine_edit_routes_to_lesson_promote"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_VALID)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        ldir = os.path.join(tmpdir, "state", "lessons-outbox")
        files = _yaml_files_in(ldir)
        if len(files) != 1:
            raise AssertionError(name + ": " + f"expected exactly 1 lessons-outbox yaml file, found {files} in {ldir}")
        content = _read(os.path.join(ldir, files[0]))
        if "change_kind: doctrine-edit" not in content:
            raise AssertionError(name + ": " + f"expected change_kind: doctrine-edit in written lesson entry, got:\n{content}")
        if "harvest-key: pln-fixture-valid-spine-000001:D2" not in content:
            raise AssertionError(name + ": " + f"expected the harvest-key idempotency marker, got:\n{content}")
        # D2's queue_scope is 'central' in the fixture — confirm the CLI did
        # NOT route it through coordinator-queue-append (which would have
        # required queue_scope: central handling); coordinator-lesson-promote
        # has no queue_scope concept at all, confirming the doctrine-class
        # routing bypassed queue-append entirely for this row.
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        q_files = _yaml_files_in(qdir)
        for qf in q_files:
            q_content = _read(os.path.join(qdir, qf))
            if "Central-scope deferred doctrine note" in q_content:
                raise AssertionError(
                    name + ": " +
                    "D2 (change_kind: doctrine-edit) must NOT be written to the improvement-queue "
                    f"directory, but found it in {qf}",
                )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_harvest_call_site_second_run_is_idempotent() -> None:
    name = "test_harvest_call_site_second_run_is_idempotent"
    tmpdir = tempfile.mkdtemp(prefix="harvest-idem-test-")
    try:
        subprocess.run(["git", "init", "-q"], cwd=tmpdir, capture_output=True, text=True, timeout=10)
        plan_path = os.path.join(tmpdir, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as fh:
            fh.write(_read(_FIXTURE_VALID))

        env = dict(os.environ)
        env["QUEUE_APPEND_OUTPUT_ROOT"] = tmpdir
        env["LESSON_PROMOTE_OUTBOX_ROOT"] = os.path.join(tmpdir, "state", "lessons-outbox")
        env.pop("DOE_ROOT", None)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)

        cmd = ["python3", os.path.abspath(_HARVEST_CLI), "--plan", plan_path]

        r1 = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS)
        if r1.returncode != 0:
            raise AssertionError(name + ": " + f"first run: expected exit 0, got {r1.returncode}. stderr={r1.stderr}")
        if "Queued 2 deferred items" not in r1.stdout:
            raise AssertionError(name + ": " + f"first run: expected 'Queued 2 deferred items', got: {r1.stdout!r}")

        r2 = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True, env=env, timeout=_SUBPROCESS_TIMEOUT_SECS)
        if r2.returncode != 0:
            raise AssertionError(name + ": " + f"second run: expected exit 0, got {r2.returncode}. stderr={r2.stderr}")
        if "Queued 0 deferred items" not in r2.stdout:
            raise AssertionError(
                name + ": " +
                f"second run: expected 'Queued 0 deferred items' (fully deduped), got: {r2.stdout!r}",
            )
        if "already-harvested" not in r2.stdout:
            raise AssertionError(name + ": " + f"second run: expected an 'already-harvested' dedup note, got: {r2.stdout!r}")

        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        ldir = os.path.join(tmpdir, "state", "lessons-outbox")
        q_files = _yaml_files_in(qdir)
        l_files = _yaml_files_in(ldir)
        if len(q_files) != 1:
            raise AssertionError(
                name + ": " +
                f"expected exactly 1 improvement-queue file after two runs (idempotent), found {q_files}",
            )
        if len(l_files) != 1:
            raise AssertionError(
                name + ": " +
                f"expected exactly 1 lessons-outbox file after two runs (idempotent), found {l_files}",
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# (d.1) case_against carry-through (example-doctrine-repo cross-repo memo, leg 3): a
#     `backlogged`/deferred row's `case_against` must survive onto the
#     harvested improvement-queue entry — and a row with none must harvest
#     cleanly with the key simply omitted, never an empty string or a
#     placeholder.
# ===========================================================================

_FIXTURE_CASE_AGAINST = os.path.join(_FIXTURES_DIR, "valid-spine-with-case-against.md")
_IMPROVEMENT_QUEUE_SCHEMA = os.path.join(str(data_root("schemas")), "improvement-queue.schema.json")


def test_harvest_carries_case_against_through_to_queue_entry() -> None:
    name = "test_harvest_carries_case_against_through_to_queue_entry"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_CASE_AGAINST)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        files = _yaml_files_in(qdir)
        if len(files) != 2:
            raise AssertionError(name + ": " + f"expected exactly 2 improvement-queue yaml files, found {files} in {qdir}")

        d1_content = next(
            (
                _read(os.path.join(qdir, f))
                for f in files
                if "pln-fixture-case-against-000001:D1" in _read(os.path.join(qdir, f))
            ),
            None,
        )
        if d1_content is None:
            raise AssertionError(name + ": " + f"could not locate the D1 harvested entry among {files}")
        if "case_against: The counter-argument that lost" not in d1_content:
            raise AssertionError(
                name + ": " +
                f"expected D1's case_against to carry through to the queue entry, got:\n{d1_content}",
            )

        if not _JSONSCHEMA_AVAILABLE or not _YAML_AVAILABLE:
            pytest.skip("jsonschema and/or PyYAML not installed — skipping schema-validation leg")
            return
        with open(_IMPROVEMENT_QUEUE_SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        entry = _yaml.safe_load(d1_content)  # type: ignore[union-attr]
        entry = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in entry.items()}
        try:
            _jsonschema.validate(instance=entry, schema=schema)  # type: ignore[union-attr]
        except _jsonschema.exceptions.ValidationError as exc:  # type: ignore[union-attr]
            raise AssertionError(name + ": " + f"D1 entry (with case_against) failed schema validation: {exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_harvest_omits_case_against_when_row_carries_none() -> None:
    name = "test_harvest_omits_case_against_when_row_carries_none"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_CASE_AGAINST)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0, got {result.returncode}. stderr={result.stderr}")
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        files = _yaml_files_in(qdir)
        if len(files) != 2:
            raise AssertionError(name + ": " + f"expected exactly 2 improvement-queue yaml files, found {files} in {qdir}")

        d2_content = next(
            (
                _read(os.path.join(qdir, f))
                for f in files
                if "pln-fixture-case-against-000001:D2" in _read(os.path.join(qdir, f))
            ),
            None,
        )
        if d2_content is None:
            raise AssertionError(name + ": " + f"could not locate the D2 harvested entry among {files}")
        if any(line.strip().startswith("case_against:") for line in d2_content.splitlines()):
            raise AssertionError(
                name + ": " +
                f"D2 carries no case_against on the row — the key must be OMITTED from the "
                f"queue entry (never an empty string or placeholder), but found it in:\n{d2_content}",
            )

        if not _JSONSCHEMA_AVAILABLE or not _YAML_AVAILABLE:
            pytest.skip("jsonschema and/or PyYAML not installed — skipping schema-validation leg")
            return
        with open(_IMPROVEMENT_QUEUE_SCHEMA, encoding="utf-8") as fh:
            schema = json.load(fh)
        entry = _yaml.safe_load(d2_content)  # type: ignore[union-attr]
        entry = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in entry.items()}
        try:
            _jsonschema.validate(instance=entry, schema=schema)  # type: ignore[union-attr]
        except _jsonschema.exceptions.ValidationError as exc:  # type: ignore[union-attr]
            raise AssertionError(name + ": " + f"D2 entry (no case_against) failed schema validation: {exc}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ===========================================================================
# (e) Malformed row skipped-with-warning by the harvest AND flagged by the
#     coverage-checker's schema conditional (mechanical proxy, per (c) above).
# ===========================================================================


def test_malformed_row_skipped_with_warning_by_harvest() -> None:
    name = "test_malformed_row_skipped_with_warning_by_harvest"
    result, tmpdir = _run_harvest_in_isolated_repo(_FIXTURE_MALFORMED)
    try:
        if result.returncode != 0:
            raise AssertionError(name + ": " + f"expected exit 0 (skip-with-warning, not a hard failure), got {result.returncode}")
        if "malformed" not in result.stderr.lower() and "missing required field" not in result.stderr.lower():
            raise AssertionError(
                name + ": " +
                f"expected a malformed/missing-required-field warning on stderr, got: {result.stderr!r}",
            )
        if "Queued 0 deferred items" not in result.stdout:
            raise AssertionError(
                name + ": " +
                f"expected 'Queued 0 deferred items' (the only deferred row, D1, is malformed and "
                f"must be skipped, not harvested), got: {result.stdout!r}",
            )
        qdir = os.path.join(tmpdir, "state", "improvement-queue")
        if _yaml_files_in(qdir):
            raise AssertionError(name + ": " + f"malformed row must not produce a queue entry, but found: {_yaml_files_in(qdir)}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_malformed_row_fails_schema_validation() -> None:
    """Mechanical proxy for 'flagged by the coverage-checker' — the
    malformed fixture's D1 row (deferred:true, pm_approved:true, but missing
    change_kind and surface) must fail plan-tasks.schema.json validation,
    which is the schema-level enforcement backing the checker's malformed-row
    lens (agents/plan-coverage-checker.md Phase 3.5 Step 2/2b).
    """
    name = "test_malformed_row_fails_schema_validation"
    if not _JSONSCHEMA_AVAILABLE or not _YAML_AVAILABLE:
        pytest.skip("jsonschema and/or PyYAML not installed")
        return
    text = _read(_FIXTURE_MALFORMED)
    marker = "```yaml plan-tasks\n"
    start = text.index(marker) + len(marker)
    end = text.index("```", start)
    body = text[start:end]
    rows = _yaml.safe_load(body)  # type: ignore[union-attr]
    malformed = next((r for r in rows if r.get("id") == "D1"), None)
    if malformed is None:
        raise AssertionError(name + ": " + "fixture does not contain the expected D1 malformed row")

    with open(_PLAN_TASKS_SCHEMA, encoding="utf-8") as fh:
        schema = json.load(fh)

    try:
        _jsonschema.validate(instance=malformed, schema=schema)  # type: ignore[union-attr]
    except _jsonschema.exceptions.ValidationError:  # type: ignore[union-attr]
        return
    raise AssertionError(
        name + ": " +
        "expected the malformed row (missing change_kind, surface) to fail schema "
        "validation (required-field check), but validation succeeded",
    )


# ===========================================================================
# (f) Governed-plan selection axis for _select_harvest_candidates — mirrors
#     plan_tasks_mutate.py's test_resolve_governed_* trio, plus a fourth case
#     specific to the harvest CLI (Review: code-reviewer Finding 2 — this
#     branch previously had zero test coverage on the more externally-visible
#     of the two write paths, since a governed harvest drives a real
#     queue-append).
# ===========================================================================

_select_harvest_candidates = _harvest_mod._select_harvest_candidates
_compute_grouping_digest = _harvest_mod.compute_grouping_digest


def _harvest_row(row_id: str = "H1", **overrides) -> dict:
    row = {
        "id": row_id,
        "title": "Harvest candidate row",
        "change_kind": "script-edit",
        "surface": "some/path.py",
        "queue_scope": "project",
        "body": "Do the harvested thing.",
        "disposition": "backlogged",
    }
    row.update(overrides)
    return row


def _governed_plan_fm(*, status: str = "approved", digest: str | None = None) -> dict:
    if digest is None:
        digest = _compute_grouping_digest([{"id": "H1", "disposition": "backlogged"}], "defer")
    return {
        "title": "Test Plan",
        "status": "draft",
        "schema_version": "1.2.0",
        "grouping_approvals": {
            "defer": {
                "status": status,
                "approver": "pm",
                "approved_at": "2026-07-29",
                "pm_utterance": "yes — harvest this cut-set",
                "digest": digest,
            },
        },
    }


def test_select_harvest_candidates_governed_admits_on_approved_grouping() -> None:
    name = "test_select_harvest_candidates_governed_admits_on_approved_grouping"
    rows = [_harvest_row()]
    plan_fm = _governed_plan_fm()

    candidates, warnings, malformed = _select_harvest_candidates(rows, plan_fm=plan_fm)

    assert malformed == 0, name
    assert warnings == [], name
    assert [r["id"] for r in candidates] == ["H1"], (
        name + ": expected the approved, fresh-digest row to be selected, got " + repr(candidates)
    )


def test_select_harvest_candidates_governed_refuses_pending_grouping() -> None:
    name = "test_select_harvest_candidates_governed_refuses_pending_grouping"
    rows = [_harvest_row()]
    plan_fm = _governed_plan_fm(status="pending")

    candidates, _warnings, malformed = _select_harvest_candidates(rows, plan_fm=plan_fm)

    assert malformed == 0, name
    assert candidates == [], (
        name + ": a pending (not yet approved) defer grouping must select nothing, got " + repr(candidates)
    )


def test_select_harvest_candidates_governed_refuses_stale_digest() -> None:
    name = "test_select_harvest_candidates_governed_refuses_stale_digest"
    stale = "sha256:" + "0" * 64
    rows = [_harvest_row()]
    plan_fm = _governed_plan_fm(digest=stale)

    candidates, _warnings, malformed = _select_harvest_candidates(rows, plan_fm=plan_fm)

    assert malformed == 0, name
    assert candidates == [], (
        name + ": an approval over a stale (non-matching) cut-set digest must select nothing, got "
        + repr(candidates)
    )


def test_select_harvest_candidates_governed_never_falls_through_to_legacy_deferred_arm() -> None:
    """A governed plan carrying a disposition-absent `deferred: true` row must
    NOT select it — the legacy arm must stay unreachable on a governed plan,
    even when the row also carries pm_approved: true. Selecting it would be
    exactly the hole example-doctrine-repo's memo warned about: silently opening the legacy
    corpus to ungated harvest via the axis that governed plans are supposed
    to have replaced.
    """
    name = "test_select_harvest_candidates_governed_never_falls_through_to_legacy_deferred_arm"
    row = _harvest_row(row_id="H2", deferred=True, pm_approved=True)
    del row["disposition"]
    rows = [row]
    plan_fm = _governed_plan_fm()

    candidates, _warnings, malformed = _select_harvest_candidates(rows, plan_fm=plan_fm)

    assert malformed == 0, name
    assert candidates == [], (
        name + ": the legacy deferred/pm_approved arm must be unreachable on a governed "
        "plan, got " + repr(candidates)
    )


def test_select_harvest_candidates_legacy_plan_still_selects_on_pm_approved_bool() -> None:
    """A LEGACY plan (no `grouping_approvals` key at all — `plan_fm=None`, or
    a dict lacking the key) is untouched by the governed re-point: a
    `disposition: backlogged` row with `pm_approved: true` is still a
    candidate exactly as before. Pins the read-tolerance half of the
    legacy-vs-governed axis this plan's contract requires — the governed
    tests above cover the new branch, this one covers the old branch stays
    reachable."""
    name = "test_select_harvest_candidates_legacy_plan_still_selects_on_pm_approved_bool"
    rows = [_harvest_row(pm_approved=True)]

    candidates_no_fm, warnings_no_fm, malformed_no_fm = _select_harvest_candidates(rows, plan_fm=None)
    assert malformed_no_fm == 0, name
    assert warnings_no_fm == [], name
    assert [r["id"] for r in candidates_no_fm] == ["H1"], (
        name + ": plan_fm=None, got " + repr(candidates_no_fm)
    )

    legacy_fm = {"title": "Test Plan", "status": "draft"}
    candidates_legacy_fm, _warnings, malformed_legacy_fm = _select_harvest_candidates(rows, plan_fm=legacy_fm)
    assert malformed_legacy_fm == 0, name
    assert [r["id"] for r in candidates_legacy_fm] == ["H1"], (
        name + ": plan_fm without grouping_approvals key, got " + repr(candidates_legacy_fm)
    )


# ===========================================================================
# Entry point
# ===========================================================================


