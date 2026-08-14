"""Tests for coordinator_core.ops.fold_execution_record.

Port-parity coverage for coordinator/bin/coordinator-fold-execution-record.py
(DOE-PORT bin-entrypoint variant).
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from coordinator_core.ops import fold_execution_record as fer
from coordinator_core.testing import symlink_capability


def _git_init(repo_root) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo_root), check=True)


def _write(path, content: str) -> None:
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as fh:
        fh.write(content)


def _make_repo_with_plan(tmp_path, plan_filename: str, plan_body: str):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    plan_dir = repo / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / plan_filename
    _write(plan_path, plan_body)
    return repo, plan_path


_PLAN_BODY = textwrap.dedent(
    """\
    # My Plan

    ## Chunks

    ### C1 — Registry P0
    Some chunk 1 description.

    ### C2 — Docs Sweep
    Some chunk 2 description.

    ## Notes

    ### C1 — this is NOT in the Chunks section, must not be picked up
    """
)


def _sidecar_path(repo, session_id: str, plan_slug: str, chunk_id: str):
    return repo / "state" / "subagent-share" / session_id / f"{plan_slug}.{chunk_id}.md"


def _sidecar_content(chunk_id: str, obs_body: str) -> str:
    return textwrap.dedent(
        f"""\
        ---
        plan: docs/plans/2026-07-13-my-plan.md
        chunk: {chunk_id}
        status: complete
        ---

        ## Observations

        {obs_body}
        """
    )


# ---------------------------------------------------------------------------
# plan_slug derivation parity (shared idiom with fan-out-dispatch.py)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename, expected",
    [
        ("2026-07-13-my-plan.md", "my-plan"),
        ("notdated.md", "notdated"),
        ("2026-13-99-not-a-real-calendar-date.md", "not-a-real-calendar-date"),
        ("2026-07-13-plan.with.dots.md", "plan.with.dots"),
        ("2026-07-13-plan with spaces.md", "plan with spaces"),
        ("2026-07-13-.md", ""),
        ("md", "md"),
        (".md", ""),
    ],
)
def test_derive_plan_slug_parity_cases(filename, expected):
    assert fer._derive_plan_slug(f"/some/dir/{filename}") == expected


def test_derive_plan_slug_case_preserved_not_lowercased():
    assert fer._derive_plan_slug("2026-07-13-MixedCase-Slug.md") == "MixedCase-Slug"


def test_slug_valid_regex_rejects_uppercase_and_underscore():
    assert fer._SLUG_VALID_RE.match("plan-slug")
    assert not fer._SLUG_VALID_RE.match("Plan-Slug")
    assert not fer._SLUG_VALID_RE.match("plan_slug")
    assert not fer._SLUG_VALID_RE.match("-leading-dash")


# ---------------------------------------------------------------------------
# SKIP sentinel emission
# ---------------------------------------------------------------------------

def test_skip_plan_not_found(tmp_path, capsys):
    rc = fer.main(["--plan", str(tmp_path / "nope.md")])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP plan-not-found" in out.out
    assert "plan not found or unreadable" in out.err


def test_skip_slug_derivation_failed(tmp_path, capsys):
    plan_path = tmp_path / "2026-07-13-.md"
    _write(plan_path, "# stub\n")
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP slug-derivation-failed" in out.out


def test_skip_invalid_slug(tmp_path, capsys):
    plan_path = tmp_path / "2026-07-13-Bad_Slug.md"
    _write(plan_path, "# stub\n")
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP invalid-slug" in out.out
    assert "invalid plan-slug derived from filename" in out.err


def test_skip_repo_root_unresolvable(tmp_path, capsys, monkeypatch):
    plan_path = tmp_path / "2026-07-13-my-plan.md"
    _write(plan_path, "# stub\n")
    monkeypatch.setattr(fer, "_resolve_git_root", lambda _p: "")
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP repo-root-unresolvable" in out.out
    assert "cannot resolve repo root" in out.err


# ---------------------------------------------------------------------------
# _resolve_git_root: real (non-monkeypatched) fallback path
# ---------------------------------------------------------------------------

def test_resolve_git_root_fallback_grandparent_no_git(tmp_path):
    # Review: code-reviewer — fills a fidelity-critical coverage gap: every
    # other test drives the `git rev-parse` success rung; this exercises the
    # real logical grandparent-of-plan-dir fallback (no git repo anywhere in
    # the ancestor chain), not a monkeypatched stand-in.
    non_git_root = tmp_path / "not-a-repo"
    plan_dir = non_git_root / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-13-my-plan.md"
    _write(plan_path, "# stub\n")

    result = fer._resolve_git_root(str(plan_path))
    assert result == str(non_git_root)


@symlink_capability.requires_symlink_capability
def test_resolve_git_root_fallback_does_not_resolve_symlinks(tmp_path):
    # Review: code-reviewer — locks in the module docstring's explicit
    # negative-spec claim ("never resolves symlinks when falling back to the
    # grandparent-of-plan-dir heuristic"): the returned fallback path must
    # retain the symlinked directory component, not the realpath target.
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    symlinked_root = tmp_path / "symlinked-root"
    os.symlink(str(real_target), str(symlinked_root))

    plan_dir = symlinked_root / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "2026-07-13-my-plan.md"
    _write(plan_path, "# stub\n")

    result = fer._resolve_git_root(str(plan_path))
    assert result == str(symlinked_root)
    assert "real-target" not in result


def test_skip_no_subagent_share_dir(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP no-subagent-share-dir" in out.out
    assert out.err == ""


def test_skip_no_sidecars(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    (repo / "state" / "subagent-share").mkdir(parents=True)
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP no-sidecars-in" in out.out
    assert "my-plan.*.md" in out.out
    assert out.err == ""


def test_skip_all_observations_trivial(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    sidecar = _sidecar_path(repo, "sess1", "my-plan", "c1")
    _write(sidecar, _sidecar_content("c1", "N/A"))
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    out = capsys.readouterr()
    assert "SKIP all-observations-trivial" in out.out
    assert out.err == ""


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------

def test_missing_plan_flag_is_usage_error(capsys):
    rc = fer.main([])
    assert rc == 1
    assert "Usage:" in capsys.readouterr().err


def test_plan_requires_argument(capsys):
    rc = fer.main(["--plan"])
    assert rc == 1
    assert "--plan requires an argument" in capsys.readouterr().err


def test_desc_requires_argument(tmp_path, capsys):
    rc = fer.main(["--plan", str(tmp_path / "x.md"), "--desc"])
    assert rc == 1
    assert "--desc requires an argument" in capsys.readouterr().err


def test_unknown_flag_errors(capsys):
    rc = fer.main(["--bogus"])
    assert rc == 1
    assert "unknown flag: --bogus" in capsys.readouterr().err


def test_unexpected_positional_errors(capsys):
    rc = fer.main(["stray"])
    assert rc == 1
    assert "unexpected positional argument: stray" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Sidecar discovery + sort order determinism
# ---------------------------------------------------------------------------

def test_sidecar_discovery_sort_order_deterministic(tmp_path):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    _write(_sidecar_path(repo, "session-zzz", "my-plan", "c2"), _sidecar_content("c2", "obs2"))
    _write(_sidecar_path(repo, "session-aaa", "my-plan", "c1"), _sidecar_content("c1", "obs1"))
    subagent_share_dir = str(repo / "state" / "subagent-share")
    found = fer._collect_sidecar_files(subagent_share_dir, "my-plan")
    assert found == sorted(found)
    assert len(found) == 2
    assert found[0].endswith(os.path.join("session-aaa", "my-plan.c1.md"))
    assert found[1].endswith(os.path.join("session-zzz", "my-plan.c2.md"))


def test_sidecar_discovery_ignores_non_matching_names(tmp_path):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    _write(_sidecar_path(repo, "sess1", "my-plan", "c1"), _sidecar_content("c1", "obs"))
    _write(_sidecar_path(repo, "sess1", "other-plan", "c1"), _sidecar_content("c1", "obs"))
    subagent_share_dir = str(repo / "state" / "subagent-share")
    found = fer._collect_sidecar_files(subagent_share_dir, "my-plan")
    assert len(found) == 1
    assert "my-plan.c1.md" in found[0]


# ---------------------------------------------------------------------------
# Section extraction / blank-trim / title-case
# ---------------------------------------------------------------------------

def test_parse_chunk_ac_map_ignores_headings_outside_chunks_section():
    ac_map = fer._parse_chunk_ac_map(_PLAN_BODY)
    assert ac_map == {"c1": "Registry P0", "c2": "Docs Sweep"}


def test_parse_chunk_ac_map_em_dash_and_hyphen_separators():
    body = "## Chunks\n\n### C1 - Hyphen Separated\n### C2 — Em Dash Separated\n"
    ac_map = fer._parse_chunk_ac_map(body)
    assert ac_map["c1"] == "Hyphen Separated"
    assert ac_map["c2"] == "Em Dash Separated"


def test_extract_frontmatter_chunk_strips_quotes_independently():
    lines = ["---", "chunk: 'c1", "---"]
    assert fer._extract_frontmatter_chunk(lines) == "c1"
    lines2 = ["---", 'chunk: c1"', "---"]
    assert fer._extract_frontmatter_chunk(lines2) == "c1"
    lines3 = ["---", 'chunk: "c1"', "---"]
    assert fer._extract_frontmatter_chunk(lines3) == "c1"


def test_extract_frontmatter_chunk_missing_returns_empty():
    lines = ["---", "plan: foo", "---", "body"]
    assert fer._extract_frontmatter_chunk(lines) == ""


def test_extract_observations_body_prefix_match_heading_variant():
    lines = [
        "## Observations & Notes",
        "line one",
        "line two",
        "## Next Section",
        "should not appear",
    ]
    body = fer._extract_observations_body(lines)
    assert body == ["line one", "line two"]


def test_trim_blank_lines_removes_leading_and_trailing_only():
    lines = ["", "  ", "content 1", "", "content 2", "", ""]
    assert fer._trim_blank_lines(lines) == ["content 1", "", "content 2"]


def test_is_trivial_observation_variants():
    assert fer._is_trivial_observation("")
    assert fer._is_trivial_observation("   ")
    assert fer._is_trivial_observation("N/A")
    assert fer._is_trivial_observation("none")
    assert fer._is_trivial_observation("—")
    assert fer._is_trivial_observation("-")
    assert fer._is_trivial_observation("NA")
    assert not fer._is_trivial_observation("real content here")


def test_title_case_slug_collapses_whitespace_and_preserves_rest_case():
    assert fer._title_case_slug("my-cool-plan") == "My Cool Plan"
    assert fer._title_case_slug("aBc-def") == "ABc Def"


# ---------------------------------------------------------------------------
# Full success path
# ---------------------------------------------------------------------------

def test_full_success_emits_part_a_and_part_b(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    _write(
        _sidecar_path(repo, "sess1", "my-plan", "c1"),
        _sidecar_content("c1", "Registry P0 landed cleanly."),
    )
    _write(
        _sidecar_path(repo, "sess1", "my-plan", "c2"),
        _sidecar_content("c2", "Docs sweep completed."),
    )
    rc = fer.main(["--plan", str(plan_path), "--desc", "Landed the plan"])
    assert rc == 0
    out = capsys.readouterr()
    assert out.err == ""
    stdout = out.out

    assert "## Execution Observations" in stdout
    assert "### C1 — Registry P0" in stdout
    assert "Registry P0 landed cleanly." in stdout
    assert "### C2 — Docs Sweep" in stdout
    assert "Docs sweep completed." in stdout

    assert "## Completion Entry Prose" in stdout
    assert "**TITLE (past-tense one-liner):** Landed the plan" in stdout
    assert "[C1 — Registry P0]" in stdout
    assert "[Caller-supplied description]" in stdout
    assert "Landed the plan" in stdout
    assert stdout.rstrip("\n").endswith(
        "<!-- END coordinator-fold-execution-record output -->"
    )


def test_success_falls_back_to_filename_chunk_id_when_no_frontmatter(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    no_fm_content = "no frontmatter here\n\n## Observations\n\nfilename fallback obs.\n"
    _write(_sidecar_path(repo, "sess1", "my-plan", "c1"), no_fm_content)
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "### C1 — Registry P0" in stdout
    assert "filename fallback obs." in stdout


def test_unresolved_chunk_ac_mapping_annotated_manually(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    _write(
        _sidecar_path(repo, "sess1", "my-plan", "c9"),
        _sidecar_content("c9", "Unmapped chunk observation."),
    )
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "C9 (chunk-to-AC mapping not resolved — annotate manually)" in stdout


def test_no_desc_flag_emits_judgment_step_title(tmp_path, capsys):
    repo, plan_path = _make_repo_with_plan(tmp_path, "2026-07-13-my-plan.md", _PLAN_BODY)
    _write(
        _sidecar_path(repo, "sess1", "my-plan", "c1"),
        _sidecar_content("c1", "some observation"),
    )
    rc = fer.main(["--plan", str(plan_path)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "[JUDGMENT STEP — suggest title from: My Plan]" in stdout
    assert "[Caller-supplied description]" not in stdout
