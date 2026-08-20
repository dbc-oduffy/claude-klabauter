"""
Tests for `coordinator_core.ops._sizing_citation.resolve_sizing_citation` and
for the `assert_plan_sizing_citation` gate reading through it.

The defect under test: `fleet.archive_terminal_sizings` moves a terminal
sizing to `archive/sizings/<month>/` and rewrites no citation, so every plan
citing it went dangling — loudly at the gate, silently at
`dispatch_emit.derive_review_tier`. The FK is archive-agnostic by design, the
same posture `plan.schema.json`'s `predecessor_handoff` already states.

Neither leg spawns a process: `main` is always called with an explicit
`--root`, so `git.repo_root.show_toplevel` is never reached.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops._sizing_citation import resolve_sizing_citation
from coordinator_core.ops.assert_plan_sizing_citation import main

_CITED = "state/sizings/2026-08-01-example.yaml"
_SIZING_BODY = "schema: sizing-object\nestimate:\n  tshirt: M\n"


def _write(root: Path, rel: str, content: str) -> Path:
    full = root / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    return full


def _write_plan(root: Path, cited: str | None = _CITED) -> None:
    citation = "null" if cited is None else cited
    _write(
        root,
        "docs/plans/2026-08-01-example-plan.md",
        "---\n"
        'title: "Example plan"\n'
        "created: 2026-08-01\n"
        f"sizing_object: {citation}\n"
        "---\n\n# Example plan\n",
    )


# ---------------------------------------------------------------------------
# resolve_sizing_citation
# ---------------------------------------------------------------------------


def test_live_path_resolves(tmp_path: Path):
    live = _write(tmp_path, _CITED, _SIZING_BODY)
    assert resolve_sizing_citation(tmp_path, _CITED) == live.resolve()


def test_archived_namesake_resolves_when_live_path_is_absent(tmp_path: Path):
    archived = _write(
        tmp_path, "archive/sizings/2026-08/2026-08-01-example.yaml", _SIZING_BODY
    )
    assert resolve_sizing_citation(tmp_path, _CITED) == archived.resolve()


def test_live_wins_over_an_archived_namesake(tmp_path: Path):
    live = _write(tmp_path, _CITED, _SIZING_BODY)
    _write(tmp_path, "archive/sizings/2026-08/2026-08-01-example.yaml", _SIZING_BODY)
    assert resolve_sizing_citation(tmp_path, _CITED) == live.resolve()


def test_neither_path_resolves_returns_none(tmp_path: Path):
    (tmp_path / "archive" / "sizings" / "2026-08").mkdir(parents=True)
    assert resolve_sizing_citation(tmp_path, _CITED) is None


def test_ambiguous_archive_match_refuses(tmp_path: Path):
    """Two same-basename archived records: the resolver cannot say which one
    the plan meant, so it treats the match exactly like no match."""
    for month in ("2026-07", "2026-08"):
        _write(tmp_path, f"archive/sizings/{month}/2026-08-01-example.yaml", _SIZING_BODY)
    assert resolve_sizing_citation(tmp_path, _CITED) is None


def test_traversal_outside_the_root_refuses(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "escape.yaml").write_text(_SIZING_BODY, encoding="utf-8")
    assert resolve_sizing_citation(root, "../outside/escape.yaml") is None


def test_absolute_citation_refuses(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    absolute = outside / "escape.yaml"
    absolute.write_text(_SIZING_BODY, encoding="utf-8")
    assert resolve_sizing_citation(root, absolute.as_posix()) is None


# ---------------------------------------------------------------------------
# assert_plan_sizing_citation reading through the fallback
# ---------------------------------------------------------------------------


def test_gate_passes_on_a_citation_whose_sizing_was_archived(tmp_path: Path, capsys):
    _write(tmp_path, "archive/sizings/2026-08/2026-08-01-example.yaml", _SIZING_BODY)
    _write_plan(tmp_path)
    rc = main(["--root", str(tmp_path)])
    assert rc == 0
    assert "OK: no dangling" in capsys.readouterr().out


def test_gate_still_fails_when_the_record_resolves_nowhere(tmp_path: Path, capsys):
    _write_plan(tmp_path)
    rc = main(["--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "FAIL: 1 dangling" in captured.err
    assert "nor under archive/sizings/" in captured.err


def test_gate_still_fails_on_an_ambiguous_archive_match(tmp_path: Path, capsys):
    for month in ("2026-07", "2026-08"):
        _write(tmp_path, f"archive/sizings/{month}/2026-08-01-example.yaml", _SIZING_BODY)
    _write_plan(tmp_path)
    rc = main(["--root", str(tmp_path)])
    assert rc == 1
    assert "FAIL: 1 dangling" in capsys.readouterr().err
