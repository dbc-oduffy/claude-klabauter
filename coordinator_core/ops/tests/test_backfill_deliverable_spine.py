"""
coordinator_core.ops.tests.test_backfill_deliverable_spine

Unit tests for coordinator_core.ops.backfill_deliverable_spine._stamp_yaml_document
(C5b whole-document sizing-YAML deliverable_id stamp).

Coverage — the four hazards `_stamp_yaml_document`'s own docstring (Review:
staff-eng — Finding 5(a-d)) names as closed by its rewrite, none of which had
a regression test before this module existed:
  (a) lock ordering       — the read-modify-write routes through
                             `locked_write.locked_rmw`'s cross-process flock;
                             a `LockTimeout` from that primitive surfaces as a
                             named skip, not a crash, and the target file is
                             left unchanged.
  (b) decode-failure       — an undecodable (non-UTF-8) file surfaces as a
      propagation            named skip via `UnicodeDecodeError`, not a
                              silently-persisted `errors="replace"` corruption.
  (c) insertion-point       — `deliverable_id:` is inserted after every
      robustness              leading blank/`#`-comment line, never
                               unconditionally at line index 1.
  (d) post-mutation          — the mutated document is parsed and
      schema validation        schema-validated before the write is allowed
                                to land; a value the vendored
                                `sizing-object.schema.json` rejects aborts the
                                stamp via `MutateAbort` rather than persisting
                                a broken document.

Reviewer finding backlink: state/subagent-share/06a8a386-7b86-47f8-b163-
8ad6593830a4/coordinatorcode-reviewer-fc183a78.md, Finding [P2] "zero test
coverage".

Per standing PM rule, these tests are WRITTEN but NOT RUN by this dispatch —
see the review-integrator's completion report for the invocation to run them.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path

from coordinator_core.locked_write import LockTimeout
from coordinator_core.ops.backfill_deliverable_spine import (
    classify_artifact,
    enumerate_corpus,
    extract_deliverable_id,
    extract_plan_id,
    main as backfill_main,
    _stamp_yaml_document,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with a state/sizings/ directory.

    Returns repo_root (the main worktree root) — the caller passes this
    directly as `_stamp_yaml_document`'s `repo_root` (it forwards to
    `locked_rmw`, which resolves the git common dir from any path inside the
    repo, worktree root included).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "backfill-test@claude-klabauter.test")
    _git("config", "user.name", "Backfill Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / "state" / "sizings" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _sizing_body(
    *,
    status: str = "routed",
    deliverable_id: str | None = None,
    leading: str = "",
) -> str:
    """A schema-valid whole-document sizing-object YAML body (1.8.0).

    `leading`, if given, is prepended verbatim (comment/blank lines) before
    the `schema:` line — exercises the leading-comment-aware insertion point.
    """
    lines = [
        "schema: sizing-object",
        "intent: Test intent, verbatim.",
        "estimate:",
        "  tshirt: M",
        "  provisional: true",
        "route: plan",
        "detents: []",
        "fork: null",
        "xl_exit: null",
        f"status: {status}",
        "premise:",
        "  provenance: read",
        "  evidence: test fixture, no real premise verified",
    ]
    if deliverable_id is not None:
        lines.append(f"deliverable_id: {deliverable_id}")
    body = "\n".join(lines) + "\n"
    return leading + body


def _seed_sizing(repo: Path, name: str, text: str) -> Path:
    path = repo / "state" / "sizings" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) lock ordering — LockTimeout surfaces as a named skip, file untouched
# ---------------------------------------------------------------------------


def test_lock_timeout_is_named_skip_and_leaves_file_unchanged(tmp_path, monkeypatch, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-a.yaml", _sizing_body())
    original = sizing.read_text(encoding="utf-8")

    def _raise_lock_timeout(*_args, **_kwargs):
        raise LockTimeout("Could not acquire coordinator lock within 10s")

    # `_stamp_yaml_document` imports `locked_rmw` function-locally from
    # `coordinator_core.locked_write` on every call — patching the module
    # attribute here is visible to that fresh import.
    monkeypatch.setattr("coordinator_core.locked_write.locked_rmw", _raise_lock_timeout)

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "timed out waiting for file lock" in captured.err

    assert sizing.read_text(encoding="utf-8") == original, (
        "LockTimeout must leave the target file byte-identical — no read, "
        "no mutate, no write occurred"
    )


# ---------------------------------------------------------------------------
# (b) decode-failure propagation — undecodable file is a named skip
# ---------------------------------------------------------------------------


def test_undecodable_file_is_named_skip_and_leaves_file_unchanged(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = repo / "state" / "sizings" / "20260101-b.yaml"
    sizing.parent.mkdir(parents=True, exist_ok=True)
    # Invalid UTF-8 byte sequence — real bytes, not mocked, so this exercises
    # locked_rmw's own `read_text(encoding="utf-8")` read leg genuinely
    # raising UnicodeDecodeError, propagating through `_stamp_yaml_document`
    # rather than being silently persisted as U+FFFD replacement characters.
    bad_bytes = b"schema: sizing-object\n\xff\xfe not valid utf-8\n"
    sizing.write_bytes(bad_bytes)

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "undecodable file" in captured.err

    assert sizing.read_bytes() == bad_bytes, (
        "an undecodable file must be left byte-identical — no write attempted"
    )


# ---------------------------------------------------------------------------
# (c) insertion-point robustness — leading comment/blank lines are respected
# ---------------------------------------------------------------------------


def test_insertion_point_skips_leading_comment_and_blank_lines(tmp_path):
    repo = _make_git_repo(tmp_path)
    leading = "# a hand-authored comment\n# a second comment line\n\n"
    sizing = _seed_sizing(repo, "20260101-c.yaml", _sizing_body(leading=leading))

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    new_lines = sizing.read_text(encoding="utf-8").splitlines()
    assert new_lines[0] == "# a hand-authored comment"
    assert new_lines[1] == "# a second comment line"
    assert new_lines[2] == ""
    assert new_lines[3] == "deliverable_id: dlv-test-000000", (
        "deliverable_id: must land immediately after the leading "
        f"blank/comment block, not at a fixed line index; got {new_lines[:5]!r}"
    )
    assert new_lines[4] == "schema: sizing-object"


def test_insertion_point_at_start_when_no_leading_comments(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-d.yaml", _sizing_body())

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    new_lines = sizing.read_text(encoding="utf-8").splitlines()
    assert new_lines[0] == "deliverable_id: dlv-test-000000"
    assert new_lines[1] == "schema: sizing-object"


def test_pre_existing_deliverable_id_is_dropped_before_reinsertion(tmp_path):
    """Idempotency-guard parity (should be unreachable given the caller's own
    exclusion, kept here for defense — see the function's own docstring)."""
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(
        repo, "20260101-e.yaml", _sizing_body(deliverable_id="dlv-old-000000")
    )

    _stamp_yaml_document(str(sizing), "dlv-new-000000", str(repo))

    text = sizing.read_text(encoding="utf-8")
    assert text.count("deliverable_id:") == 1
    assert "dlv-new-000000" in text
    assert "dlv-old-000000" not in text


# ---------------------------------------------------------------------------
# (d) post-mutation schema validation — an invalid document aborts the write
# ---------------------------------------------------------------------------


def test_schema_invalid_deliverable_id_aborts_and_leaves_file_unchanged(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-f.yaml", _sizing_body())
    original = sizing.read_text(encoding="utf-8")

    # Does not match the schema's `^dlv-(?!placeholder-replace-with)...`
    # pattern for `deliverable_id` — the post-mutation narrow, op-local
    # `_validate_stamped_deliverable_id` check (not full-document schema
    # validation — see that function's docstring) must reject this and
    # MutateAbort before the write lands.
    _stamp_yaml_document(str(sizing), "not-a-valid-deliverable-id", str(repo))

    captured = capsys.readouterr()
    assert "validation failed" in captured.err

    assert sizing.read_text(encoding="utf-8") == original, (
        "a schema-invalid mutation must never be written — MutateAbort keeps "
        "the file byte-identical"
    )


def test_valid_stamp_passes_schema_validation_and_writes(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "20260101-g.yaml", _sizing_body())

    _stamp_yaml_document(str(sizing), "dlv-test-000000", str(repo))

    text = sizing.read_text(encoding="utf-8")
    assert "deliverable_id: dlv-test-000000" in text


# ---------------------------------------------------------------------------
# File-not-found — pre-existing, unaffected by the rewrite, kept for parity
# ---------------------------------------------------------------------------


def test_file_not_found_is_named_skip(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    missing = repo / "state" / "sizings" / "does-not-exist.yaml"

    _stamp_yaml_document(str(missing), "dlv-test-000000", str(repo))

    captured = capsys.readouterr()
    assert "file not found" in captured.err


# ---------------------------------------------------------------------------
# C2 — archive/specs/** deliverable_id leg (new gap this chunk closes)
# ---------------------------------------------------------------------------


def test_classify_artifact_recognizes_archived_spec():
    assert classify_artifact("/repo/archive/specs/2026-08/foo.md") == "archived-spec"


def test_enumerate_corpus_includes_archive_specs(tmp_path):
    specs_dir = tmp_path / "archive" / "specs" / "2026-08"
    specs_dir.mkdir(parents=True)
    spec_file = specs_dir / "2026-08-01-fixture.md"
    spec_file.write_text("---\ntitle: x\n---\n", encoding="utf-8")

    corpus = enumerate_corpus(str(tmp_path))
    assert str(spec_file) in corpus


def _write_frontmatter(
    path: Path,
    *,
    workstream: str | None = None,
    deliverable_id_line: str | None = None,
    plan_id_line: str | None = None,
    slug: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---", 'title: "fixture"']
    if slug is not None:
        lines.append(f"slug: {slug}")
    if workstream is not None:
        lines.append(f"workstream: {workstream}")
    if deliverable_id_line is not None:
        lines.append(deliverable_id_line)
    if plan_id_line is not None:
        lines.append(plan_id_line)
    lines += ["---", "", "# fixture", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def test_write_mints_deliverable_id_onto_archived_spec_and_never_remints_on_rerun(tmp_path):
    root = tmp_path
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-spec.md"
    _write_frontmatter(spec, workstream="fixture-ws", slug="fixture-spec")

    rc1 = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc1 == 0

    text = spec.read_text(encoding="utf-8")
    assert "deliverable_id: dlv-" in text
    minted = extract_deliverable_id(str(spec), "archived-spec")
    assert minted.startswith("dlv-")

    # Re-run must mint nothing further (carry rule D1).
    rc2 = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc2 == 0
    assert spec.read_text(encoding="utf-8") == text


def test_archived_spec_already_carrying_deliverable_id_is_untouched(tmp_path):
    root = tmp_path
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-spec-full.md"
    _write_frontmatter(
        spec,
        workstream="fixture-ws",
        deliverable_id_line="deliverable_id: dlv-fixture-preexisting-000000",
        plan_id_line="plan_id: pln-fixture-spec-full-preexisting",
        slug="fixture-spec-full",
    )
    original = spec.read_text(encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0
    assert spec.read_text(encoding="utf-8") == original


def test_literal_null_deliverable_id_is_treated_as_absent_and_minted(tmp_path):
    root = tmp_path
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-spec-null.md"
    _write_frontmatter(
        spec,
        workstream="fixture-ws-null",
        deliverable_id_line="deliverable_id: null",
        slug="fixture-spec-null",
    )

    backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())

    minted = extract_deliverable_id(str(spec), "archived-spec")
    assert minted.startswith("dlv-")


# ---------------------------------------------------------------------------
# C2 — plan_id leg (fourth leg, entirely new)
# ---------------------------------------------------------------------------


def test_write_mints_plan_id_onto_plan_and_archived_spec_lacking_one(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan.md"
    _write_frontmatter(plan, slug="fixture-plan")
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-spec.md"
    _write_frontmatter(spec, slug="fixture-spec")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    plan_id_1 = extract_plan_id(str(plan), "plan")
    plan_id_2 = extract_plan_id(str(spec), "archived-spec")
    assert plan_id_1.startswith("pln-")
    assert plan_id_2.startswith("pln-")


def test_plan_id_already_present_is_not_re_minted(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan-full.md"
    _write_frontmatter(
        plan,
        # Pre-existing deliverable_id so the C2-leg4 standalone-mint arm
        # (an ungrouped plan/archived-spec) also carries D1 and does not
        # touch this fixture — this test's own concern is the plan_id
        # carry rule specifically, not leg4's deliverable_id behaviour.
        deliverable_id_line="deliverable_id: dlv-fixture-plan-full-preexisting",
        plan_id_line="plan_id: pln-fixture-plan-full-aaaaaa",
        slug="fixture-plan-full",
    )
    original = plan.read_text(encoding="utf-8")

    backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())

    assert plan.read_text(encoding="utf-8") == original


def test_plan_id_rerun_is_a_true_no_op(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan-rerun.md"
    _write_frontmatter(plan, slug="fixture-plan-rerun")

    out1 = io.StringIO()
    backfill_main(["--write", "--root", str(root)], out=out1, err=io.StringIO())
    stamped_text = plan.read_text(encoding="utf-8")

    out2 = io.StringIO()
    backfill_main(["--write", "--root", str(root)], out=out2, err=io.StringIO())
    assert plan.read_text(encoding="utf-8") == stamped_text
    assert "Lacking plan_id:    0" in out2.getvalue()


def test_dry_run_reports_plan_id_population_without_writing(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan-dry.md"
    _write_frontmatter(plan, slug="fixture-plan-dry")
    original = plan.read_text(encoding="utf-8")

    out = io.StringIO()
    rc = backfill_main(["--dry-run", "--root", str(root)], out=out, err=io.StringIO())
    assert rc == 0
    assert plan.read_text(encoding="utf-8") == original
    assert "Lacking plan_id:    1" in out.getvalue()


def test_plan_id_leg_skips_sidecar(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan-sidecar.md"
    _write_frontmatter(plan, slug="fixture-plan-sidecar")
    sidecar = root / "docs" / "plans" / "2026-08-13-fixture-plan-sidecar.sonnet-review.md"
    _write_frontmatter(sidecar, slug="fixture-plan-sidecar-review")
    original_sidecar = sidecar.read_text(encoding="utf-8")

    out = io.StringIO()
    rc = backfill_main(["--write", "--root", str(root)], out=out, err=io.StringIO())
    assert rc == 0

    assert extract_plan_id(str(plan), "plan").startswith("pln-")
    assert sidecar.read_text(encoding="utf-8") == original_sidecar
    assert "Lacking plan_id:    1" in out.getvalue()


def test_plan_id_leg_skips_undated_non_plan_document(tmp_path):
    root = tmp_path
    readme = root / "docs" / "plans" / "README.md"
    _write_frontmatter(readme, slug="plans-readme")
    index = root / "docs" / "plans" / "INDEX.md"
    _write_frontmatter(index, slug="plans-index")
    original_readme = readme.read_text(encoding="utf-8")
    original_index = index.read_text(encoding="utf-8")

    out = io.StringIO()
    rc = backfill_main(["--write", "--root", str(root)], out=out, err=io.StringIO())
    assert rc == 0

    assert readme.read_text(encoding="utf-8") == original_readme
    assert index.read_text(encoding="utf-8") == original_index
    assert "Lacking plan_id:    0" in out.getvalue()


def test_plan_id_leg_still_mints_onto_dated_plan_record(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-plan-dated.md"
    _write_frontmatter(plan, slug="fixture-plan-dated")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    assert extract_plan_id(str(plan), "plan").startswith("pln-")


# ---------------------------------------------------------------------------
# C2-leg3 — unkeyed sizings (no citing plan) mint a STANDALONE deliverable_id
# ---------------------------------------------------------------------------


def test_write_mints_standalone_id_onto_unkeyed_sizing_and_never_remints_on_rerun(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "2026-08-13-fixture-unkeyed.yaml", _sizing_body())

    out1 = io.StringIO()
    rc1 = backfill_main(["--write", "--root", str(repo)], out=out1, err=io.StringIO())
    assert rc1 == 0

    minted = extract_deliverable_id(str(sizing), "sizing")
    assert minted.startswith("dlv-"), f"expected a minted dlv- id, got {minted!r}"
    assert "fixture-unkeyed" in minted, (
        "an unkeyed sizing's standalone id must derive from the sizing's OWN "
        f"slug (filename minus date prefix), got {minted!r}"
    )
    assert "Stamped (unkeyed sizings): 1" in out1.getvalue()

    text_after_first_write = sizing.read_text(encoding="utf-8")

    # Re-run: carry rule D1 — a sizing already carrying a real id is never
    # re-minted. Must be a true no-op (byte-identical file).
    out2 = io.StringIO()
    rc2 = backfill_main(["--write", "--root", str(repo)], out=out2, err=io.StringIO())
    assert rc2 == 0
    assert sizing.read_text(encoding="utf-8") == text_after_first_write
    assert "Stamped (unkeyed sizings): 0" in out2.getvalue()
    assert extract_deliverable_id(str(sizing), "sizing") == minted


def test_sizing_already_carrying_real_id_is_untouched(tmp_path):
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(
        repo,
        "2026-08-13-fixture-preexisting.yaml",
        _sizing_body(deliverable_id="dlv-preexisting-000000"),
    )
    original = sizing.read_text(encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(repo)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0
    assert sizing.read_text(encoding="utf-8") == original
    assert extract_deliverable_id(str(sizing), "sizing") == "dlv-preexisting-000000"


def test_write_mints_standalone_id_onto_ungrouped_plan(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-ungrouped-plan.md"
    _write_frontmatter(plan, slug="fixture-ungrouped-plan")

    out1 = io.StringIO()
    rc1 = backfill_main(["--write", "--root", str(root)], out=out1, err=io.StringIO())
    assert rc1 == 0

    minted = extract_deliverable_id(str(plan), "plan")
    assert minted.startswith("dlv-"), f"expected a minted dlv- id, got {minted!r}"
    assert "fixture-ungrouped-plan" in minted
    assert "Stamped (standalone plan/spec): 1" in out1.getvalue()

    text_after_first_write = plan.read_text(encoding="utf-8")

    # Re-run: carry rule D1 — never re-minted.
    out2 = io.StringIO()
    rc2 = backfill_main(["--write", "--root", str(root)], out=out2, err=io.StringIO())
    assert rc2 == 0
    assert plan.read_text(encoding="utf-8") == text_after_first_write
    assert "Stamped (standalone plan/spec): 0" in out2.getvalue()
    assert extract_deliverable_id(str(plan), "plan") == minted


def test_write_mints_standalone_id_onto_ungrouped_archived_spec(tmp_path):
    root = tmp_path
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-ungrouped-spec.md"
    _write_frontmatter(spec, slug="fixture-ungrouped-spec")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    minted = extract_deliverable_id(str(spec), "archived-spec")
    assert minted.startswith("dlv-")
    assert "fixture-ungrouped-spec" in minted


def test_grouped_plan_still_gets_group_derived_deliverable_id_unchanged(tmp_path):
    """A plan WITH a workstream group must keep its group-derived id — only
    the ungrouped arm changes (C2-leg4 constraint)."""
    root = tmp_path
    plan_a = root / "docs" / "plans" / "2026-08-13-fixture-grouped-plan-a.md"
    _write_frontmatter(plan_a, workstream="fixture-grouped-ws", slug="fixture-grouped-plan-a")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    minted = extract_deliverable_id(str(plan_a), "plan")
    assert minted.startswith("dlv-")
    assert "fixture-grouped-ws" in minted, (
        f"a grouped plan must derive its id from the workstream key, got {minted!r}"
    )


def test_rerun_after_write_is_byte_identical_no_op(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-full-rerun.md"
    _write_frontmatter(plan, slug="fixture-full-rerun")
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-full-rerun-spec.md"
    _write_frontmatter(spec, slug="fixture-full-rerun-spec")

    rc1 = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc1 == 0
    plan_text_1 = plan.read_text(encoding="utf-8")
    spec_text_1 = spec.read_text(encoding="utf-8")

    rc2 = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc2 == 0
    assert plan.read_text(encoding="utf-8") == plan_text_1
    assert spec.read_text(encoding="utf-8") == spec_text_1


def test_ungrouped_review_sidecar_never_mints_standalone_deliverable_id(tmp_path):
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-sidecar-host.md"
    _write_frontmatter(plan, slug="fixture-sidecar-host")
    sidecar = root / "docs" / "plans" / "2026-08-13-fixture-sidecar-host.sonnet-review.md"
    _write_frontmatter(sidecar, slug="fixture-sidecar-host-review")
    original_sidecar = sidecar.read_text(encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    assert sidecar.read_text(encoding="utf-8") == original_sidecar
    assert extract_deliverable_id(str(sidecar), "plan") == ""


def test_undated_non_plan_document_never_mints_standalone_deliverable_id(tmp_path):
    root = tmp_path
    readme = root / "docs" / "plans" / "README.md"
    _write_frontmatter(readme, slug="plans-readme-standalone")
    index = root / "docs" / "plans" / "INDEX.md"
    _write_frontmatter(index, slug="plans-index-standalone")
    original_readme = readme.read_text(encoding="utf-8")
    original_index = index.read_text(encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    assert readme.read_text(encoding="utf-8") == original_readme
    assert index.read_text(encoding="utf-8") == original_index
    assert extract_deliverable_id(str(readme), "plan") == ""
    assert extract_deliverable_id(str(index), "plan") == ""


def test_stamp_file_creates_frontmatter_fence_when_absent(tmp_path):
    """_stamp_file on a plan-shaped document with NO `---` frontmatter fence
    at all must actually inject the field (prepending a new fence) rather
    than silently reporting success with no mutation."""
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-fenceless.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# A narrative doc with no frontmatter at all\n\nBody text.\n", encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    minted = extract_plan_id(str(plan), "plan")
    assert minted.startswith("pln-"), f"expected a minted pln- id, got {minted!r}"
    text = plan.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "A narrative doc with no frontmatter at all" in text


def test_keyed_sizing_still_gets_group_derived_id_from_citing_plan(tmp_path):
    """A sizing WITH a citing plan must keep getting the group-derived id it
    gets today — only the unkeyed arm changes (C2-leg3 constraint)."""
    repo = _make_git_repo(tmp_path)
    sizing = _seed_sizing(repo, "2026-08-13-fixture-keyed.yaml", _sizing_body())

    plan = repo / "docs" / "plans" / "2026-08-13-fixture-keyed-plan.md"
    _write_frontmatter(
        plan,
        slug="fixture-keyed-plan",
        deliverable_id_line="deliverable_id: dlv-fixture-keyed-plan-aaaaaa",
    )
    # sizing_object: is the plan -> sizing forward pointer build_plan_sizing_index
    # reads to build the reverse edge — see that function's docstring.
    text = plan.read_text(encoding="utf-8")
    text = text.replace(
        "deliverable_id: dlv-fixture-keyed-plan-aaaaaa",
        "deliverable_id: dlv-fixture-keyed-plan-aaaaaa\n"
        "sizing_object: state/sizings/2026-08-13-fixture-keyed.yaml",
    )
    plan.write_text(text, encoding="utf-8")

    rc = backfill_main(["--write", "--root", str(repo)], out=io.StringIO(), err=io.StringIO())
    assert rc == 0

    minted = extract_deliverable_id(str(sizing), "sizing")
    assert minted == "dlv-fixture-keyed-plan-aaaaaa", (
        "a keyed sizing must inherit its citing plan's deliverable_id "
        f"unchanged, got {minted!r}"
    )


# ---------------------------------------------------------------------------
# Reviewer findings (state/subagent-share/264c40f4-364d-4e65-a790-40140b1eb3e2/
# coordinatorcode-reviewer-c67ef990.md, [P1] x2) — both control-flow defects
# fixed together in main()/group_corpus.
# ---------------------------------------------------------------------------


def test_plan_id_leg_still_writes_while_deliverable_id_group_is_ambiguous(tmp_path):
    """P1: `run_plan_id_leg` must stay reachable in --write mode even when an
    UNRELATED deliverable_id workstream group is ambiguous — the plan_id leg
    is per-file with no grouping/ambiguity concept of its own (its own
    docstring). Before the fix, `main()`'s `if total_ambiguous: return 2`
    fired before the loop ever reached `run_plan_id_leg`, corpus-wide."""
    root = tmp_path
    # Ambiguous deliverable_id group: two plans sharing a workstream key but
    # carrying distinct slugs.
    plan_a = root / "docs" / "plans" / "2026-08-13-fixture-ambig-a.md"
    _write_frontmatter(plan_a, workstream="fixture-ambig-ws", slug="fixture-ambig-a")
    plan_b = root / "docs" / "plans" / "2026-08-13-fixture-ambig-b.md"
    _write_frontmatter(plan_b, workstream="fixture-ambig-ws", slug="fixture-ambig-b")

    # An unrelated, ambiguity-free plan lacking a plan_id.
    plan_c = root / "docs" / "plans" / "2026-08-13-fixture-unrelated-c.md"
    _write_frontmatter(plan_c, slug="fixture-unrelated-c")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())
    assert rc == 2, "an ambiguous deliverable_id group must still fail loud (exit 2)"

    # The deliverable_id write pass must be withheld for the ambiguous group...
    assert extract_deliverable_id(str(plan_a), "plan") == ""
    assert extract_deliverable_id(str(plan_b), "plan") == ""
    # ...but the plan_id leg (per-file, no grouping/ambiguity concept) must
    # still have run and stamped all three plans, unrelated group included.
    assert extract_plan_id(str(plan_a), "plan").startswith("pln-")
    assert extract_plan_id(str(plan_b), "plan").startswith("pln-")
    assert extract_plan_id(str(plan_c), "plan").startswith("pln-")


def test_archived_spec_sharing_workstream_with_differently_slugged_plan_is_ambiguous(tmp_path):
    """P1: an archived-spec sharing a workstream key with a differently-
    slugged plan must trip the ambiguity guard, not be silently collapsed
    onto one deliverable_id via `_find_group_id`'s single-mint-per-group
    logic. Before the fix, `group_corpus` only fed `group_plan_slugs` from
    `artifact_class == "plan"`, so an archived-spec never contributed a slug
    and this pairing sailed through undetected."""
    root = tmp_path
    plan = root / "docs" / "plans" / "2026-08-13-fixture-shared-ws-plan.md"
    _write_frontmatter(plan, workstream="fixture-shared-ws", slug="fixture-shared-ws-plan")
    spec = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-shared-ws-spec.md"
    _write_frontmatter(spec, workstream="fixture-shared-ws", slug="fixture-shared-ws-spec")

    out = io.StringIO()
    rc = backfill_main(["--write", "--root", str(root)], out=out, err=io.StringIO())

    assert rc == 2, "archived-spec + differently-slugged plan sharing a workstream must be ambiguous"
    assert "fixture-shared-ws" in out.getvalue()
    # Neither file may have been silently stamped with a shared id.
    assert extract_deliverable_id(str(plan), "plan") == ""
    assert extract_deliverable_id(str(spec), "archived-spec") == ""


def test_two_archived_specs_sharing_workstream_with_distinct_slugs_is_ambiguous(tmp_path):
    """P1 sibling case: TWO archived specs (no plan involved at all) sharing
    a workstream key with distinct slugs must also trip the ambiguity
    guard."""
    root = tmp_path
    spec_a = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-two-spec-a.md"
    _write_frontmatter(spec_a, workstream="fixture-two-spec-ws", slug="fixture-two-spec-a")
    spec_b = root / "archive" / "specs" / "2026-08" / "2026-08-01-fixture-two-spec-b.md"
    _write_frontmatter(spec_b, workstream="fixture-two-spec-ws", slug="fixture-two-spec-b")

    rc = backfill_main(["--write", "--root", str(root)], out=io.StringIO(), err=io.StringIO())

    assert rc == 2, "two archived specs sharing a workstream with distinct slugs must be ambiguous"
    assert extract_deliverable_id(str(spec_a), "archived-spec") == ""
    assert extract_deliverable_id(str(spec_b), "archived-spec") == ""
