"""
Tests for coordinator_core.ops.plan_capture_persist.

Spec backlink: state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md
    § Part 2 -- the claude-klabauter-side seam a plan-persistence hook should call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.plan_capture_persist import (
    PersistError,
    _handler,
    build_problem_body,
    derive_hook_slug,
    extract_h1_title,
    find_frontmatterless_duplicate,
    invoke_coordinator_doc_new,
    local_day,
    main,
    parse_chunk_rows,
    persist_captured_plan,
    predict_doc_new_plan_path,
    predict_hook_capture_path,
    readme_row,
    splice_problem_body,
    splice_tasks,
    split_top_sections,
    validate_rows,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE_INPUT = (
    Path.home() / ".claude" / "plans"
    / "state-sizings-2026-08-13-em-exercisable-expressive-teapot.md"
)
_FIXTURE_SIZING = (
    _REPO_ROOT / "state" / "sizings"
    / "2026-08-13-em-exercisable-in-band-grant-route-for-t.yaml"
)


# ---------------------------------------------------------------------------
# Slug / date derivation — byte-parity with the hook
# ---------------------------------------------------------------------------


def test_derive_hook_slug_from_h1():
    text = "# An EM-Exercisable Thing\n\nbody\n"
    assert derive_hook_slug(text) == "an-em-exercisable-thing"


def test_derive_hook_slug_truncates_at_60_chars():
    long_title = "# " + ("word " * 30)
    slug = derive_hook_slug(long_title)
    assert len(slug) <= 60
    assert not slug.endswith("-")


def test_derive_hook_slug_timestamp_fallback_when_no_h1():
    slug = derive_hook_slug("no heading here\n")
    assert slug.startswith("plan-")


def test_local_day_shape():
    day = local_day()
    assert len(day) == 10
    assert day[4] == "-" and day[7] == "-"


def test_predict_doc_new_plan_path_uses_40_char_truncation(tmp_path):
    title = "word " * 30
    path = predict_doc_new_plan_path(tmp_path, title)
    slug_part = path.stem[len(local_day()) + 1:]
    assert len(slug_part) <= 40
    assert path.parent == tmp_path / "docs" / "plans"


def test_predict_hook_capture_path_matches_derive_hook_slug(tmp_path):
    text = "# A Title\n\nbody\n"
    path = predict_hook_capture_path(tmp_path, text)
    assert path.name == f"{local_day()}-a-title.md"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_extract_h1_title():
    assert extract_h1_title("# My Title\n\nbody\n") == "My Title"


def test_extract_h1_title_absent():
    assert extract_h1_title("no heading\n") is None


def test_split_top_sections_basic():
    text = "# T\n\nintro\n\n## A\n\nbody a\n\n## B\n\nbody b\n"
    assert split_top_sections(text) == [("A", "body a"), ("B", "body b")]


def test_parse_chunk_rows_and_validate():
    body = "### C0 — First\nsome body\n\n### C1 — `path/to/file.py`: second\nmore body\n"
    rows = parse_chunk_rows(body)
    assert len(rows) == 2
    assert rows[0]["id"] == "C0"
    assert rows[1]["surface"] == "path/to/file.py"
    validate_rows(rows)


def test_validate_rows_rejects_bad_row():
    with pytest.raises(PersistError):
        validate_rows([{"id": "C0"}])


def test_build_problem_body_excludes_chunks_and_demotes():
    text = "# T\n\n## Context\n\n### Sub\nprose\n\n## Chunks\n\n### C0 -- x\nbody\n"
    sections = split_top_sections(text)
    problem = build_problem_body(sections)
    assert "### Context" in problem
    assert "#### Sub" in problem
    assert "Chunks" not in problem


def test_splice_problem_body_replaces_placeholder():
    scaffold = "## Problem\n\n<!-- State the problem this plan solves. -->\n\n## Acceptance Criteria\n"
    out = splice_problem_body(scaffold, "carried content")
    assert "carried content" in out
    assert "<!-- State the problem" not in out


def test_splice_problem_body_requires_placeholder():
    with pytest.raises(PersistError):
        splice_problem_body("no placeholder", "x")


def test_splice_tasks_replaces_fence():
    scaffold = "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: sample\n```\n"
    rows = parse_chunk_rows("### C9 — Real chunk\nreal body\n")
    out = splice_tasks(scaffold, rows)
    assert "id: C9" in out
    assert "sample" not in out


def test_splice_tasks_noop_on_empty_rows():
    scaffold = "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: sample\n```\n"
    assert splice_tasks(scaffold, []) == scaffold


def test_readme_row_uses_plans_prefix():
    row = readme_row(Path("/repo/docs/plans/2026-08-13-foo.md"))
    assert row == "- [`2026-08-13-foo.md`](plans/2026-08-13-foo.md)"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_find_frontmatterless_duplicate_matches_by_title(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    dup = plans_dir / "2026-08-13-raw-dump.md"
    _write(dup, "# A distinctive matching title\n\nbody\n")
    found = find_frontmatterless_duplicate(plans_dir, "A distinctive matching title")
    assert found == dup


def test_find_frontmatterless_duplicate_ignores_frontmatter(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    _write(plans_dir / "2026-08-13-compliant.md", '---\ntitle: "Same title"\n---\n\n# Same title\n')
    assert find_frontmatterless_duplicate(plans_dir, "Same title") is None


def test_find_frontmatterless_duplicate_refuses_ambiguous(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    title = "A distinctive matching title"
    _write(plans_dir / "2026-08-13-dup-a.md", f"# {title}\n\na\n")
    _write(plans_dir / "2026-08-13-dup-b.md", f"# {title}\n\nb\n")
    with pytest.raises(PersistError):
        find_frontmatterless_duplicate(plans_dir, title)


# ---------------------------------------------------------------------------
# persist_captured_plan orchestration (invoke_coordinator_doc_new monkeypatched)
# ---------------------------------------------------------------------------


def _fake_scaffold(scaffold_path: Path, *, title: str, sizing_object_relpath):
    scaffold_path.parent.mkdir(parents=True, exist_ok=True)
    scaffold_path.write_text(
        "---\n"
        f'title: "{title}"\n'
        'plan_id: "pln-fake-000000"\n'
        'deliverable_id: "dlv-fake-000000"\n'
        f'sizing_object: {"null" if not sizing_object_relpath else json.dumps(sizing_object_relpath)}\n'
        "status: draft\n"
        "---\n\n"
        "## Problem\n\n<!-- State the problem this plan solves. -->\n\n"
        "## Acceptance Criteria\n\n## Anti-scope\n\n## Out of scope\n\n"
        "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: sample\n```\n",
        encoding="utf-8",
    )


def test_persist_captured_plan_ok(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)

    title = "A brand new captured plan"
    scaffold_path = predict_doc_new_plan_path(repo_root, title)

    def _fake_invoke(repo_root_arg, *, title, sizing_object_relpath, branch):
        _fake_scaffold(scaffold_path, title=title, sizing_object_relpath=sizing_object_relpath)
        return scaffold_path

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fake_invoke
    )

    plan_content = f"# {title}\n\n## Context\n\nsome prose\n"
    result = persist_captured_plan(plan_content, repo_root=repo_root)
    assert result["status"] == "ok"
    assert result["path"] == f"docs/plans/{scaffold_path.name}"
    assert result["readme_row"] == readme_row(scaffold_path)
    assert scaffold_path.is_file()
    assert "some prose" in scaffold_path.read_text(encoding="utf-8")


def test_persist_captured_plan_idempotent_when_compliant_already_exists(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "Already persisted plan"
    existing_path = predict_doc_new_plan_path(repo_root, title)
    _fake_scaffold(existing_path, title=title, sizing_object_relpath=None)

    def _fail_invoke(*a, **kw):
        raise AssertionError("must not re-scaffold an idempotent capture")

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fail_invoke
    )

    result = persist_captured_plan(f"# {title}\n\nbody\n", repo_root=repo_root)
    assert result["status"] == "idempotent"
    assert result["path"] == f"docs/plans/{existing_path.name}"


def test_persist_captured_plan_collision_on_different_raw_dump(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "A colliding capture"
    plan_content = f"# {title}\n\nnew body text\n"
    hook_path = predict_hook_capture_path(repo_root, plan_content)
    _write(hook_path, f"# {title}\n\nDIFFERENT prior body text\n")

    def _fail_invoke(*a, **kw):
        raise AssertionError("must not scaffold over an unreconciled collision")

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fail_invoke
    )

    result = persist_captured_plan(plan_content, repo_root=repo_root)
    assert result["status"] == "collision"
    assert result["path"] is None
    assert hook_path.read_text(encoding="utf-8") == f"# {title}\n\nDIFFERENT prior body text\n"


def test_persist_captured_plan_reconciles_byte_identical_raw_dump(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "A reconcilable capture"
    plan_content = f"# {title}\n\nidentical body\n"
    hook_path = predict_hook_capture_path(repo_root, plan_content)
    _write(hook_path, plan_content)

    scaffold_path = predict_doc_new_plan_path(repo_root, title)

    def _fake_invoke(repo_root_arg, *, title, sizing_object_relpath, branch):
        _fake_scaffold(scaffold_path, title=title, sizing_object_relpath=sizing_object_relpath)
        return scaffold_path

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fake_invoke
    )

    result = persist_captured_plan(plan_content, repo_root=repo_root)
    assert result["status"] == "ok"
    if hook_path.resolve() != scaffold_path.resolve():
        assert not hook_path.exists()


def test_persist_captured_plan_empty_body_is_error(tmp_path):
    result = persist_captured_plan("   \n\n", repo_root=tmp_path)
    assert result["status"] == "error"


def test_persist_captured_plan_no_sizing_object_calls_no_sizing_object_flag(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "No sizing object here"
    scaffold_path = predict_doc_new_plan_path(repo_root, title)
    seen = {}

    def _fake_invoke(repo_root_arg, *, title, sizing_object_relpath, branch):
        seen["sizing_object_relpath"] = sizing_object_relpath
        _fake_scaffold(scaffold_path, title=title, sizing_object_relpath=sizing_object_relpath)
        return scaffold_path

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fake_invoke
    )
    persist_captured_plan(f"# {title}\n\nbody\n", repo_root=repo_root)
    assert seen["sizing_object_relpath"] is None


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


def test_handler_requires_repo_root():
    result = _handler({"plan_content": "# T\n\nbody\n"}, repo_root=None)
    assert result.get("exit_code") == 1


def test_handler_requires_plan_content(tmp_path):
    result = _handler({}, repo_root=tmp_path)
    assert result.get("exit_code") == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_main_reads_stdin_and_prints_json(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "CLI stdin capture"
    scaffold_path = predict_doc_new_plan_path(repo_root, title)

    def _fake_invoke(repo_root_arg, *, title, sizing_object_relpath, branch):
        _fake_scaffold(scaffold_path, title=title, sizing_object_relpath=sizing_object_relpath)
        return scaffold_path

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fake_invoke
    )
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(f"# {title}\n\nbody\n"))

    rc = main(["--repo-root", str(repo_root)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["status"] == "ok"


def test_main_plan_file_flag(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "plans").mkdir(parents=True)
    title = "CLI plan-file capture"
    scaffold_path = predict_doc_new_plan_path(repo_root, title)
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(f"# {title}\n\nbody\n", encoding="utf-8")

    def _fake_invoke(repo_root_arg, *, title, sizing_object_relpath, branch):
        _fake_scaffold(scaffold_path, title=title, sizing_object_relpath=sizing_object_relpath)
        return scaffold_path

    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist.invoke_coordinator_doc_new", _fake_invoke
    )
    rc = main(["--repo-root", str(repo_root), "--plan-file", str(plan_file)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["path"] == f"docs/plans/{scaffold_path.name}"


# ---------------------------------------------------------------------------
# invoke_coordinator_doc_new — engine-root vs consumer-repo resolution
# (regression coverage for the repo_root/engine_root conflation: the
# scaffolder script lives in the ENGINE checkout, not the caller's
# --repo-root, which for any real consumer repo is a DIFFERENT tree with no
# coordinator/bin/ at all).
# ---------------------------------------------------------------------------


def test_invoke_coordinator_doc_new_succeeds_against_a_consumer_repo_root(tmp_path):
    """Regression: with the old ``repo_root / "coordinator" / "bin" /
    "coordinator-doc-new"`` resolution this raised PersistError for any
    --repo-root that is not the engine checkout itself (verified directly
    against the pre-fix line: a consumer repo has no coordinator/bin/
    subtree). The scaffolder must now be found via the engine root
    regardless of where --repo-root points, and the write target must still
    land under --repo-root, not the engine checkout.
    """
    consumer_repo = tmp_path / "consumer-repo"
    (consumer_repo / "docs" / "plans").mkdir(parents=True)
    (consumer_repo / "docs" / "README.md").write_text("# Plans\n", encoding="utf-8")

    title = "A regression-covering consumer repo capture"
    scaffold_path = invoke_coordinator_doc_new(
        consumer_repo, title=title, sizing_object_relpath=None, branch=None,
    )
    try:
        assert scaffold_path.is_file()
        assert consumer_repo in scaffold_path.parents
        assert _REPO_ROOT not in scaffold_path.parents or _REPO_ROOT == consumer_repo
        assert scaffold_path == predict_doc_new_plan_path(consumer_repo, title)
    finally:
        scaffold_path.unlink(missing_ok=True)


def test_persist_captured_plan_ok_against_a_consumer_repo_root_real_scaffolder(tmp_path):
    """End-to-end (no monkeypatch of invoke_coordinator_doc_new): the write
    target is the consumer repo passed as --repo-root, not the engine
    checkout the scaffolder script itself lives in.
    """
    consumer_repo = tmp_path / "consumer-repo"
    (consumer_repo / "docs" / "plans").mkdir(parents=True)
    (consumer_repo / "docs" / "README.md").write_text("# Plans\n", encoding="utf-8")

    title = "A second regression-covering consumer capture"
    plan_content = f"# {title}\n\n## Context\n\nsome carried prose\n"
    result = persist_captured_plan(plan_content, repo_root=consumer_repo)
    try:
        assert result["status"] == "ok", result
        scaffold_path = consumer_repo / result["path"]
        assert scaffold_path.is_file()
        assert consumer_repo in scaffold_path.parents
        assert "some carried prose" in scaffold_path.read_text(encoding="utf-8")
    finally:
        (consumer_repo / result["path"]).unlink(missing_ok=True) if result.get("path") else None


def test_invoke_coordinator_doc_new_missing_scaffolder_names_engine_path(tmp_path, monkeypatch):
    consumer_repo = tmp_path / "consumer-repo"
    (consumer_repo / "docs" / "plans").mkdir(parents=True)

    bogus_engine_path = tmp_path / "nonexistent-engine-root" / "coordinator" / "bin" / "coordinator-doc-new"
    monkeypatch.setattr(
        "coordinator_core.ops.plan_capture_persist._DOC_NEW_PATH", bogus_engine_path,
    )

    with pytest.raises(PersistError) as excinfo:
        invoke_coordinator_doc_new(
            consumer_repo, title="Missing scaffolder case", sizing_object_relpath=None, branch=None,
        )
    assert str(bogus_engine_path) in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end against the real coordinator-doc-new binary and the 2026-08-13
# fixture body (scratch sizing/out paths -- see docstring for why the real
# ones can't be reused; same reasoning the earlier port prototype hit).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _FIXTURE_INPUT.is_file(), reason="fixture capture file not present on this machine")
@pytest.mark.skipif(not _FIXTURE_SIZING.is_file(), reason="fixture sizing object not present in this checkout")
def test_persist_captured_plan_end_to_end_real_coordinator_doc_new():
    """Exercises the real coordinator-doc-new subprocess against the
    2026-08-13 fixture plan body. The real sizing object already carries a
    `plan:` reverse FK (the committed known-good artifact was scaffolded for
    real earlier) -- coordinator-doc-new correctly refuses to re-route it,
    so this test uses a scratch copy (plan: null, throwaway deliverable_id)
    written under the real repo's state/sizings/ (must be repo-relative) and
    a scratch title (-> a scratch docs/plans/ path, never the committed
    filename), both removed in the finally block.
    """
    plan_content = _FIXTURE_INPUT.read_text(encoding="utf-8")
    scratch_title = "PORT TEST SCRATCH -- em exercisable in band grant route"
    # Swap the H1 so coordinator-doc-new's own title-derived slug lands on a
    # scratch filename distinct from the committed known-good plan.
    lines = plan_content.splitlines()
    assert lines[0].startswith("# ")
    lines[0] = f"# {scratch_title}"
    plan_content = "\n".join(lines)

    sizing_text = _FIXTURE_SIZING.read_text(encoding="utf-8")
    scratch_sizing_rel = "state/sizings/2026-08-13-capture-persist-test-scratch.yaml"
    scratch_sizing_path = _REPO_ROOT / scratch_sizing_rel
    scratch_sizing_text = sizing_text.replace(
        'plan: "docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md"', "plan: null",
    ).replace(
        'deliverable_id: "dlv-em-exercisable-in-band-grant-route-for-t-03ace3"',
        'deliverable_id: "dlv-capture-persist-test-scratch-000000"',
    )
    assert "plan: null" in scratch_sizing_text
    scratch_sizing_path.write_text(scratch_sizing_text, encoding="utf-8")

    scaffold_path = predict_doc_new_plan_path(_REPO_ROOT, scratch_title)
    assert not scaffold_path.exists(), "scratch title collided with an existing file -- pick a different scratch title"

    try:
        result = persist_captured_plan(
            plan_content, repo_root=_REPO_ROOT, sizing_object=scratch_sizing_rel,
        )
        assert result["status"] == "ok", result
        assert scaffold_path.is_file()
        text = scaffold_path.read_text(encoding="utf-8")

        from coordinator_core.frontmatter.primitives import (
            read_fm_field_unquoted,
            split_frontmatter,
        )

        split = split_frontmatter(text)
        assert split is not None
        assert read_fm_field_unquoted(split.fm_text, "plan_id")
        assert read_fm_field_unquoted(split.fm_text, "deliverable_id")
        assert read_fm_field_unquoted(split.fm_text, "sizing_object") == scratch_sizing_rel
        for heading in ("## Problem", "## Anti-scope", "## Out of scope", "## Tasks"):
            assert heading in text
        # Falsifier for the retired AC row family (DoE-claude memo
        # 2026-08-27-doe-claude-em-ac-table-collapse.md, ask 1): a freshly
        # scaffolded plan must carry no `## Acceptance Criteria` heading --
        # the heading is what kept authors filling a table nothing gates.
        assert "## Acceptance Criteria" not in text
        assert "```yaml plan-tasks" in text
        assert "id: C0" in text or "id: C1" in text
    finally:
        scratch_sizing_path.unlink(missing_ok=True)
        scaffold_path.unlink(missing_ok=True)
