"""
coordinator_core.plan_assemble.predicates.test_substrate_scans — coverage
for `substrate_scans.compute(...)`: one populated case plus one
absent-input `undetermined` case per row group this module owns.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C4
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates import substrate_scans as ss

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _git_repo_with_one_commit(tmp_path: Path) -> tuple[Path, str]:
    """A real `git init`ed repo under `tmp_path` with one commit, for
    exercising `_peer_sha_lint`'s batched `git cat-file --batch-check`
    resolution against a genuine local SHA rather than a mocked call."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()
    return tmp_path, sha


def _ctx(
    *,
    repo_root: Path,
    plan_body=None,
    plan_frontmatter=None,
    sizing_frontmatter=None,
    resolved_route="plan",
    caller_flags=None,
) -> PredicateContext:
    return PredicateContext(
        repo_root=repo_root,
        plan_path=Path("docs/plans/fixture.md") if plan_body is not None else None,
        plan_frontmatter=plan_frontmatter,
        plan_body=plan_body,
        sizing_object_path=None,
        sizing_frontmatter=sizing_frontmatter,
        resolved_route=resolved_route,
        caller_flags=caller_flags or {},
    )


def _assert_undetermined(value) -> None:
    assert isinstance(value, dict)
    assert value.get("undetermined") is True
    assert isinstance(value.get("reason"), str) and value["reason"]


# ---------------------------------------------------------------------------
# :89 peer_sha_lint
# ---------------------------------------------------------------------------


def test_peer_sha_lint_flags_unresolvable_bare_sha(tmp_path: Path):
    repo_root, local_sha = _git_repo_with_one_commit(tmp_path)
    body = (
        f"See coordinator-project@{local_sha} for the good form, "
        "but deadbeefcafe is bare and unresolvable."
    )
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert result["fires"] is True
    assert "deadbeefcafe" in result["bad_citations"]
    assert local_sha not in result["bad_citations"]
    assert isinstance(result.get("known_limit"), str) and result["known_limit"]


def test_peer_sha_lint_does_not_flag_locally_resolving_sha(tmp_path: Path):
    # Class 1 — a bare SHA that resolves as a commit in THIS repo's own
    # history is correctly cited bare (house style) and must not be flagged.
    repo_root, local_sha = _git_repo_with_one_commit(tmp_path)
    body = f"Fixed in {local_sha} as part of this change."
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert local_sha not in result["bad_citations"]
    assert result["fires"] is False


def test_peer_sha_lint_never_flags_well_formed_repo_at_sha(tmp_path: Path):
    repo_root, local_sha = _git_repo_with_one_commit(tmp_path)
    body = f"See peer-repo@{local_sha} for the peer citation."
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert result["bad_citations"] == []
    assert result["fires"] is False


def test_peer_sha_lint_excludes_session_id_by_context(tmp_path: Path):
    # Class 3 — an 8-hex-char session-id fragment adjacent to a
    # session-id-bearing marker is not a SHA candidate.
    repo_root, _ = _git_repo_with_one_commit(tmp_path)
    body = "sent_by: bca38888 picked this up."
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert "bca38888" not in result["bad_citations"]
    assert result["fires"] is False


def test_peer_sha_lint_flags_bare_sha_near_unrelated_session_word(tmp_path: Path):
    # Regression for the false-negative the symmetric ±40-char window
    # produced: "session" appearing nearby in ordinary prose must NOT
    # suppress a genuine bare peer SHA that isn't a session id at all.
    repo_root, _ = _git_repo_with_one_commit(tmp_path)
    body = "session 958cf906 landed a59f8d79c"
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert "a59f8d79c" in result["bad_citations"]
    assert result["fires"] is True


def test_peer_sha_lint_still_excludes_field_marker_immediately_before_token(tmp_path: Path):
    # The tightened leading-only window must still suppress a token that
    # directly follows a field-shaped marker.
    repo_root, _ = _git_repo_with_one_commit(tmp_path)
    body = "sent_by: bca38888 picked this up."
    ctx = _ctx(repo_root=repo_root, plan_body=body)
    result = ss._peer_sha_lint(ctx)
    assert "bca38888" not in result["bad_citations"]
    assert result["fires"] is False


def test_peer_sha_lint_git_failure_degrades_to_undetermined(tmp_path: Path):
    # A candidate token exists, but repo_root is not a git repository at
    # all — the batched git call fails, and the row must degrade to
    # `undetermined` rather than mass-flagging every candidate as bad.
    body = "See abc1234def as a bare candidate."
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    _assert_undetermined(ss._peer_sha_lint(ctx))


def test_peer_sha_lint_undetermined_without_plan_body(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body=None)
    _assert_undetermined(ss._peer_sha_lint(ctx))


# ---------------------------------------------------------------------------
# :106/:107/:108 collapse
# ---------------------------------------------------------------------------


def test_collapse_invoked_true_consumes_probe_signal(tmp_path: Path):
    ctx = _ctx(
        repo_root=tmp_path,
        sizing_frontmatter={"probe": {"signal": "collapse"}},
        resolved_route="roadmap",
        caller_flags={"collapse_fired_this_pass": True},
    )
    result = ss._collapse(ctx)
    assert result["invoked"] is True
    assert result["route_reachability_violation"] is True
    assert result["fired_this_pass"] is True


def test_collapse_invoked_false_when_probe_signal_is_raise(tmp_path: Path):
    ctx = _ctx(
        repo_root=tmp_path,
        sizing_frontmatter={"probe": {"signal": "raise"}},
        resolved_route="roadmap",
        caller_flags={"collapse_fired_this_pass": False},
    )
    result = ss._collapse(ctx)
    assert result["invoked"] is False
    assert result["route_reachability_violation"] is False
    assert result["fired_this_pass"] is False


def test_collapse_undetermined_without_sizing_object(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, sizing_frontmatter=None)
    result = ss._collapse(ctx)
    _assert_undetermined(result["invoked"])
    _assert_undetermined(result["route_reachability_violation"])
    _assert_undetermined(result["fired_this_pass"])


def test_collapse_fired_this_pass_undetermined_when_flag_absent(tmp_path: Path):
    ctx = _ctx(
        repo_root=tmp_path,
        sizing_frontmatter={"probe": {"signal": "collapse"}},
        resolved_route="plan",
        caller_flags={},
    )
    result = ss._collapse(ctx)
    assert result["invoked"] is True
    _assert_undetermined(result["fired_this_pass"])


# ---------------------------------------------------------------------------
# :111/:112 fix_locus
# ---------------------------------------------------------------------------


def test_fix_locus_citation_and_gate_type_present(tmp_path: Path):
    (tmp_path / "coordinator_core" / "ops").mkdir(parents=True)
    registry = tmp_path / "coordinator_core" / "ops" / "_registry_map.py"
    registry.write_text("REGISTRY = {'my_gate_type': 'x'}\n", encoding="utf-8")

    body = (
        "## Fix Locus\n"
        "See coordinator_core/foo.py:42 for the site.\n"
        "Gate type: `my_gate_type`\n"
    )
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._fix_locus(ctx)
    assert result["citation_present"] is True
    assert result["citation"] == "coordinator_core/foo.py:42"
    assert result["registry_has_gate_type"] is True


def test_fix_locus_undetermined_without_section(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="No relevant section here.")
    _assert_undetermined(ss._fix_locus(ctx))


def test_fix_locus_gate_type_undetermined_without_line(tmp_path: Path):
    body = "## Fix Locus\nSee coordinator_core/foo.py:42 for the site.\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._fix_locus(ctx)
    assert result["citation_present"] is True
    _assert_undetermined(result["registry_has_gate_type"])


# ---------------------------------------------------------------------------
# :114 citations_verified
# ---------------------------------------------------------------------------


def test_citations_verified_true_when_all_citations_resolve(tmp_path: Path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "real_file.py").write_text("x = 1\n", encoding="utf-8")
    body = "See `real_file.py` for the implementation.\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    assert ss._citations_verified(ctx) is True


def test_citations_verified_false_when_a_citation_is_absent(tmp_path: Path):
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    body = "See `coordinator_core/does_not_exist_at_all.py` for the implementation.\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    assert ss._citations_verified(ctx) is False


def test_citations_verified_undetermined_without_plan_body(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body=None)
    _assert_undetermined(ss._citations_verified(ctx))


def test_citations_verified_true_for_doc_relative_citation(tmp_path: Path):
    # Review: code-reviewer — Finding (P2). `verify_doc` must be called with
    # `doc_relative_checker`, not `repo_exists` alone — a citation that only
    # resolves relative to the CITING plan's own directory (not repo-root)
    # must not read as `absent`/uncited. `sibling.py` exists ONLY under
    # `docs/plans/`, never at the repo root.
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "sibling.py").write_text("x = 1\n", encoding="utf-8")
    body = "See `sibling.py` for the implementation.\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    assert ss._citations_verified(ctx) is True


# ---------------------------------------------------------------------------
# _git_grep_count / _grep_repo_count — fast-path/fallback parity
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    import subprocess

    no_console = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, creationflags=no_console)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"],
        cwd=root,
        check=True,
        creationflags=no_console,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=root, check=True, creationflags=no_console
    )


def test_git_grep_count_finds_untracked_file(tmp_path: Path):
    # Review: code-reviewer — Finding (P2). `git grep` alone only searches
    # tracked/staged content; a token that exists only in a freshly-created,
    # not-yet-`git add`ed file must still be found by the fast path — it
    # must agree with the pure-Python fallback, not silently under-report.
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.py").write_text("needle_token = 1\n", encoding="utf-8")
    fast = ss._git_grep_count(tmp_path, "needle_token")
    assert fast == 1


def test_git_grep_count_finds_gitignored_file(tmp_path: Path):
    # The pure-Python fallback walk does not honor `.gitignore` (it only
    # skips `_GREP_SKIP_DIRS`), so the fast path must widen to match —
    # `--no-exclude-standard` alongside `--untracked`.
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("needle_token = 1\n", encoding="utf-8")
    fast = ss._git_grep_count(tmp_path, "needle_token")
    assert fast == 1


def test_git_grep_count_and_fallback_agree_on_untracked_file(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "untracked.py").write_text("needle_token = 1\n", encoding="utf-8")
    fast = ss._git_grep_count(tmp_path, "needle_token")
    slow = 0
    for path in tmp_path.rglob("*"):
        if not path.is_file():
            continue
        if any(part in ss._GREP_SKIP_DIRS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "needle_token" in text:
            slow += 1
    assert fast == slow == 1


def test_grep_repo_count_zero_when_git_grep_finds_nothing(tmp_path: Path):
    _init_git_repo(tmp_path)
    (tmp_path / "other.py").write_text("unrelated = 1\n", encoding="utf-8")
    assert ss._grep_repo_count(tmp_path, "absent_token") == 0


def test_grep_repo_count_falls_back_for_non_git_dir(tmp_path: Path):
    # No `git init` here — `_git_grep_count` must return `None` (a real
    # exit-!=0 failure, not a repo), and `_grep_repo_count` must fall back
    # to the pure-Python walk rather than silently reporting 0.
    (tmp_path / "plain.py").write_text("needle_token = 1\n", encoding="utf-8")
    assert ss._git_grep_count(tmp_path, "needle_token") is None
    assert ss._grep_repo_count(tmp_path, "needle_token") == 1


# ---------------------------------------------------------------------------
# :116 symbol_liveness
# ---------------------------------------------------------------------------


def test_symbol_liveness_live_true_when_symbol_found(tmp_path: Path):
    (tmp_path / "src.py").write_text("def old_symbol():\n    pass\n", encoding="utf-8")
    body = "Symbol to replace: `old_symbol`\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._symbol_liveness(ctx)
    assert result["live"] is True


def test_symbol_liveness_undetermined_without_citation(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._symbol_liveness(ctx))


# ---------------------------------------------------------------------------
# :118 reverses_teardown — candidate evidence only
# ---------------------------------------------------------------------------


def test_reverses_teardown_candidate_false_when_artifact_present(tmp_path: Path):
    (tmp_path / "still_here.py").write_text("x = 1\n", encoding="utf-8")
    body = "Reintroduces: `still_here.py`\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._reverses_teardown(ctx)
    assert result == {"candidate": False}
    assert "verdict" not in result
    assert "recommended" not in result


def test_reverses_teardown_undetermined_without_citation(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._reverses_teardown(ctx))


# ---------------------------------------------------------------------------
# :120 native_code_plan
# ---------------------------------------------------------------------------


def test_native_code_plan_true_for_cpp_scope(tmp_path: Path):
    ctx = _ctx(
        repo_root=tmp_path,
        plan_frontmatter={"scope": ["Source/Game/MyActor.cpp", "Source/Game/MyActor.h"]},
    )
    assert ss._native_code_plan(ctx) is True


def test_native_code_plan_false_for_python_scope(tmp_path: Path):
    ctx = _ctx(
        repo_root=tmp_path,
        plan_frontmatter={"scope": ["coordinator_core/foo.py"]},
    )
    assert ss._native_code_plan(ctx) is False


def test_native_code_plan_undetermined_without_frontmatter(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_frontmatter=None)
    _assert_undetermined(ss._native_code_plan(ctx))


# ---------------------------------------------------------------------------
# :123 registered_dispatch_added
# ---------------------------------------------------------------------------


def test_registered_dispatch_added_true_when_pattern_in_registry(tmp_path: Path):
    (tmp_path / "coordinator_core" / "ops").mkdir(parents=True)
    registry = tmp_path / "coordinator_core" / "ops" / "_registry_map.py"
    registry.write_text("REGISTRY = {'my_new_op': 'x'}\n", encoding="utf-8")

    body = "Dispatch pattern: `my_new_op`\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    assert ss._registered_dispatch_added(ctx) is True


def test_registered_dispatch_added_undetermined_without_citation(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._registered_dispatch_added(ctx))


# ---------------------------------------------------------------------------
# :125 port_seam — candidate evidence only
# ---------------------------------------------------------------------------


def test_port_seam_exists_true_when_path_present(tmp_path: Path):
    (tmp_path / "coordinator_core" / "ports").mkdir(parents=True)
    (tmp_path / "coordinator_core" / "ports" / "my_seam.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    body = "Port seam: `coordinator_core/ports/my_seam.py`\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._port_seam(ctx)
    assert result == {"exists": True}
    assert "verdict" not in result
    assert "recommended" not in result


def test_port_seam_undetermined_without_citation(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._port_seam(ctx))


# ---------------------------------------------------------------------------
# :159 mutates_shared_symbol
# ---------------------------------------------------------------------------


def test_shared_symbol_mutation_true_when_multiple_consumers(tmp_path: Path):
    (tmp_path / "a.py").write_text("shared_symbol()\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("shared_symbol()\n", encoding="utf-8")
    body = "Mutates symbol: `shared_symbol`\n"
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._shared_symbol_mutation(ctx)
    assert result["consumer_count"] == 2
    assert result["mutates_shared_symbol"] is True


def test_shared_symbol_mutation_undetermined_without_citation(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._shared_symbol_mutation(ctx))


# ---------------------------------------------------------------------------
# :166 scaffold_checklist
# ---------------------------------------------------------------------------


def test_scaffold_checklist_full_six_items(tmp_path: Path):
    body = (
        "## Scaffold Checklist\n"
        "1. [x] one\n"
        "2. [x] two\n"
        "3. [ ] three\n"
        "4. [x] four\n"
        "5. [ ] five\n"
        "6. [ ] grep the pattern\n"
    )
    ctx = _ctx(repo_root=tmp_path, plan_body=body)
    result = ss._scaffold_checklist(ctx)
    assert result["items_1_5"] == [True, True, False, True, False]
    assert result["item_6_grep_present"] is True


def test_scaffold_checklist_undetermined_without_section(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path, plan_body="Nothing relevant here.")
    _assert_undetermined(ss._scaffold_checklist(ctx))


# ---------------------------------------------------------------------------
# compute() — full aggregation, and the withdrawn-row negative-spec
# ---------------------------------------------------------------------------


def test_compute_returns_every_row_group_key(tmp_path: Path):
    ctx = _ctx(repo_root=tmp_path)
    result = ss.compute(ctx)
    expected_keys = {
        "peer_sha_lint",
        "collapse",
        "fix_locus",
        "citations_verified",
        "symbol_liveness",
        "reverses_teardown",
        "native_code_plan",
        "registered_dispatch_added",
        "port_seam",
        "mutates_shared_symbol",
        "scaffold_checklist",
    }
    assert set(result.keys()) == expected_keys


def test_compute_never_emits_a_121_key(tmp_path: Path):
    # :121 (API-rekey) is withdrawn — no row group in this module's output
    # is named for it, under any spelling.
    ctx = _ctx(repo_root=tmp_path)
    result = ss.compute(ctx)
    assert not any("rekey" in key.lower() for key in result)
