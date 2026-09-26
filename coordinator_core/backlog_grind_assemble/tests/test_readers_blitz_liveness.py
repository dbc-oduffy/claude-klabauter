from __future__ import annotations

from pathlib import Path

from coordinator_core.backlog_grind_assemble import readers_blitz
from coordinator_core.ops.dispatch_emit import queue_select


def test_is_item_live_true_for_existing_file(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("def f(): pass\n", encoding="utf-8")
    assert readers_blitz.is_item_live(tmp_path, "x.py") is True


def test_is_item_live_true_for_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    assert readers_blitz.is_item_live(tmp_path, "pkg") is True


def test_is_item_live_false_for_missing_path(tmp_path: Path) -> None:
    assert readers_blitz.is_item_live(tmp_path, "does/not/exist.py") is False


def test_bare_cited_surface_accepts_a_single_clean_path() -> None:
    assert (
        readers_blitz.bare_cited_surface("coordinator_core/ops/handoff_transition.py")
        == "coordinator_core/ops/handoff_transition.py"
    )


def test_bare_cited_surface_rejects_compound_and_prose_surfaces() -> None:
    compound = [
        None,
        "",
        "shared-worktree commit discipline; state/ index hygiene",
        "coordinator_core/x.py::_evaluate",
        "coordinator_core/x.py, coordinator_core/y.py",
        "coordinator_core/ (repo-wide idiom)",
        "docs/architecture/systems/*.md",
        "no-slash-at-all",
    ]
    for surface in compound:
        assert readers_blitz.bare_cited_surface(surface) is None, surface


def test_read_backlog_readiness_evidence_reports_open_item_count(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(readers_blitz, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        readers_blitz,
        "load_family_records",
        lambda *a, **k: [{"frontmatter": {"surface": "live.py"}}],
    )
    result = readers_blitz._read_backlog_readiness()
    evidence = result.judgment_points[0]["evidence"]
    assert evidence.startswith("state/bug-backlog/ open-item count=1 |")


def _write_bug_row(path, **fields) -> None:
    import yaml

    path.write_text(yaml.safe_dump(fields, sort_keys=False), encoding="utf-8")


def _bug_row_fields(**overrides) -> dict:
    row = dict(
        created="2026-09-21",
        title="a bug row",
        body="a body",
        status="open",
        surface="pkg/live.py",
        severity="P2",
    )
    row.update(overrides)
    return row


def _select_bug(tmp_path, queue_dir, **kwargs):
    kwargs.setdefault("where", None)
    kwargs.setdefault("order", None)
    kwargs.setdefault("limit", None)
    kwargs.setdefault("batch_key", ["severity"])
    kwargs.setdefault("batch_sizes", {"@unkeyed": 4})
    kwargs.setdefault("row_id_key", "@stem")
    kwargs.setdefault("profile", "bug")
    kwargs.setdefault("repo_root", tmp_path)
    kwargs.setdefault("queue", [queue_dir])
    return queue_select.select_rows(**kwargs)


def test_select_rows_declines_a_bug_row_whose_bare_surface_is_gone(tmp_path: Path) -> None:
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_bug_row(queue_dir / "dead.yaml", **_bug_row_fields(surface="pkg/dead.py"))
    manifest = _select_bug(tmp_path, queue_dir)
    assert manifest.entries == ()
    assert len(manifest.declined) == 1
    assert manifest.declined[0].row_id == "dead"
    assert "pre-dispatch liveness" in manifest.declined[0].reason
    assert "pkg/dead.py" in manifest.declined[0].reason


def test_select_rows_keeps_a_bug_row_whose_bare_surface_still_exists(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "live.py").write_text("x = 1\n", encoding="utf-8")
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_bug_row(queue_dir / "live.yaml", **_bug_row_fields(surface="pkg/live.py"))
    manifest = _select_bug(tmp_path, queue_dir)
    assert [e.row_id for e in manifest.entries] == ["live"]
    assert manifest.declined == ()


def test_select_rows_never_disqualifies_a_compound_or_prose_surface(tmp_path: Path) -> None:
    queue_dir = tmp_path / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    _write_bug_row(
        queue_dir / "prose.yaml",
        **_bug_row_fields(surface="shared-worktree commit discipline; state/ index hygiene"),
    )
    manifest = _select_bug(tmp_path, queue_dir)
    assert [e.row_id for e in manifest.entries] == ["prose"]
    assert manifest.declined == ()


def test_select_rows_liveness_decline_is_scoped_to_the_bug_profile(tmp_path: Path) -> None:
    queue_dir = tmp_path / "state" / "some-other-queue"
    queue_dir.mkdir(parents=True)
    _write_bug_row(queue_dir / "dead.yaml", **_bug_row_fields(surface="pkg/dead.py"))
    manifest = _select_bug(tmp_path, queue_dir, profile="fixture")
    assert [e.row_id for e in manifest.entries] == ["dead"]
    assert manifest.declined == ()
