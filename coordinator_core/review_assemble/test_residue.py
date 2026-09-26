from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.contract.decision_object.envelope import ENVELOPE_KEYS
from coordinator_core.resolve_coordinator_clone import ResolveCoordinatorCloneError
from coordinator_core.review_assemble import residue as residue_mod
from coordinator_core.review_assemble.residue import (
    ResidueAssembleError,
    ResidueUsageError,
    brief,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "work/test/2026-01-01")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _segment_text(segment_id: str, surface: str, cls: str, order: int, body: str) -> str:
    return (
        "---\n"
        f"segment_id: {segment_id}\n"
        f"surface: {surface}\n"
        f"class: {cls}\n"
        f"order: {order}\n"
        "---\n"
        f"{body}\n"
    )


def _make_residue_dir(
    tmp_path: Path,
    *,
    include_plan: bool = True,
    include_diff: bool = True,
    include_shared: bool = True,
) -> Path:
    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "review" / "residue"
    residue_dir.mkdir(parents=True)
    if include_shared:
        (residue_dir / "010-shared.md").write_text(
            _segment_text("shared-reminder", "shared", "protected", 0, "Shared body."),
            encoding="utf-8",
        )
    if include_plan:
        (residue_dir / "020-plan.md").write_text(
            _segment_text("plan-reminder", "plan", "droppable", 1, "Plan body."),
            encoding="utf-8",
        )
    if include_diff:
        (residue_dir / "030-diff.md").write_text(
            _segment_text("diff-reminder", "diff", "droppable", 2, "Diff body."),
            encoding="utf-8",
        )
    return content_root


def _patch_content_root(monkeypatch: pytest.MonkeyPatch, content_root: Path) -> None:
    monkeypatch.setattr(
        residue_mod, "resolve_content_root", lambda: str(content_root)
    )


def test_envelope_key_set_is_exactly_the_eight_canonical_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    plan_path = repo / "docs" / "plans" / "2026-07-26-example.md"
    result = brief(str(plan_path), repo_root=repo)

    envelope_only = {k: v for k, v in result.items() if k != "segments"}
    assert set(envelope_only.keys()) == set(ENVELOPE_KEYS)
    assert set(result.keys()) == set(ENVELOPE_KEYS) | {"segments"}


def test_segment_source_path_is_relative_not_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    plan_path = repo / "docs" / "plans" / "2026-07-26-example.md"
    result = brief(str(plan_path), repo_root=repo)

    content_root_str = str(content_root)
    for segment in result["segments"]:
        source_path = segment["source_path"]
        assert not Path(source_path).is_absolute(), source_path
        assert content_root_str not in source_path, source_path
        assert source_path.startswith("skills/review/residue/"), source_path

    assert not Path(result["decisions"]["residue_dir"]).is_absolute()
    assert content_root_str not in result["decisions"]["residue_dir"]


def test_unresolved_surface_raises_judgment_point_offering_plan_and_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(None, repo_root=repo)

    assert result["artifact"]["surface"] is None
    judgment_points = result["judgment_points"]
    assert len(judgment_points) == 1

    jp = judgment_points[0]
    dispositions = {d["value"] for d in jp["dispositions"]}
    assert dispositions == {"plan", "diff"}
    assert jp["recommendation"] is None


def test_unresolved_surface_selects_only_shared_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(None, repo_root=repo)

    segment_ids = {s["segment_id"] for s in result["segments"]}
    assert segment_ids == {"shared-reminder"}


def test_artifact_under_docs_plans_resolves_to_plan_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief("docs/plans/2026-07-26-example.md", repo_root=repo)

    assert result["artifact"]["surface"] == "plan"
    assert result["judgment_points"] == []


def test_artifact_ending_in_dot_md_resolves_to_plan_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief("notes/some-write-up.md", repo_root=repo)

    assert result["artifact"]["surface"] == "plan"
    assert result["judgment_points"] == []


def test_no_artifact_with_nonempty_diff_resolves_to_diff_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = brief(None, repo_root=repo)

    assert result["artifact"]["surface"] == "diff"
    assert result["judgment_points"] == []


def test_artifact_argument_that_is_neither_plans_path_nor_md_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief("src/some_module.py", repo_root=repo)

    assert result["artifact"]["surface"] is None
    assert len(result["judgment_points"]) == 1


def test_explicit_surface_plan_wins_over_diff_inferring_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE REGRESSION TEST for the actual bug: no artifact argument, dirty
    tracked tree (a context that would otherwise infer `diff` per rule 3) —
    an explicit `--surface plan` must still win. Against the pre-fix
    behaviour (no `--surface` plumbing at all) this is red: the CLI had no
    way to pass an explicit surface, so a bare `/coordinator:review --surface
    plan` on a dirty tree silently resolved `diff`."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    (repo / "README.md").write_text("changed\n", encoding="utf-8")

    result = brief(None, repo_root=repo, explicit_surface="plan")

    assert result["artifact"]["surface"] == "plan"
    assert result["judgment_points"] == []
    segment_ids = {s["segment_id"] for s in result["segments"]}
    assert "plan-reminder" in segment_ids
    assert "diff-reminder" not in segment_ids


def test_explicit_surface_diff_wins_over_plan_inferring_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(
        "docs/plans/2026-07-26-example.md",
        repo_root=repo,
        explicit_surface="diff",
    )

    assert result["artifact"]["surface"] == "diff"
    assert result["judgment_points"] == []
    segment_ids = {s["segment_id"] for s in result["segments"]}
    assert "diff-reminder" in segment_ids
    assert "plan-reminder" not in segment_ids


def test_explicit_surface_never_raises_judgment_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    result = brief(None, repo_root=repo, explicit_surface="plan")

    assert result["artifact"]["surface"] == "plan"
    assert result["judgment_points"] == []


def test_unrecognized_explicit_surface_value_is_usage_error_not_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(tmp_path)
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueUsageError) as excinfo:
        brief(None, repo_root=repo, explicit_surface="bogus")

    assert "bogus" in str(excinfo.value)


def test_unresolvable_content_root_exits_nonzero_with_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    def _raise() -> str:
        raise ResolveCoordinatorCloneError(
            "resolve-coordinator-clone --for-content: no readable content root found.\n"
            "  Run: coordinator:install"
        )

    monkeypatch.setattr(residue_mod, "resolve_content_root", _raise)

    with pytest.raises(ResolveCoordinatorCloneError) as excinfo:
        brief(None, repo_root=repo)

    assert "coordinator:install" in str(excinfo.value)


def test_empty_residue_directory_raises_fail_loud_not_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = tmp_path / "content-root"
    residue_dir = content_root / "skills" / "review" / "residue"
    residue_dir.mkdir(parents=True)
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief("docs/plans/2026-07-26-example.md", repo_root=repo)

    assert "no segment files" in str(excinfo.value)


def test_absent_residue_directory_raises_fail_loud_not_empty_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = tmp_path / "content-root"
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief("docs/plans/2026-07-26-example.md", repo_root=repo)

    assert "residue directory not found" in str(excinfo.value)
    assert "coordinator:install" in str(excinfo.value)


def test_resolved_surface_with_zero_applicable_segments_is_fail_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    content_root = _make_residue_dir(
        tmp_path, include_plan=False, include_diff=True, include_shared=False
    )
    _patch_content_root(monkeypatch, content_root)

    with pytest.raises(ResidueAssembleError) as excinfo:
        brief("docs/plans/2026-07-26-example.md", repo_root=repo)

    assert "zero applicable segments" in str(excinfo.value)
