"""
Message-text regression for chunk C5a of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

`project_rag_detect.detect_banner` is the highest reader-facing volume site in
that plan: its UNINITIALIZED / STALE session-start banners are injected into
agent context on every session. This pins that none of those rendered banner
strings name a private repo codename (or its OSS-scrub placeholder) — a
banner should tell the reader what command to run, never send them to a repo
an OSS reader cannot reach.

Spec backlink: pln-message-text-stops-naming-a-re-5c92dd, chunk C5a.
"""

from __future__ import annotations

from unittest import mock

from coordinator_core.hooks import project_rag_detect

_FORBIDDEN_SUBSTRINGS = (
    "DoE-claude",
    "doe-claude",
    "opticon",
    "delphi",
    "delphipro",
    "holodeck-repo",
    "example-doctrine-repo",
    "example-retrieval-repo",
    "example-fleet",
    "example-game-repo",
    "machine-b",
    "cockpit",
)


def _assert_clean(banner: str) -> None:
    lowered = banner.lower()
    for hit in _FORBIDDEN_SUBSTRINGS:
        assert hit.lower() not in lowered, f"banner names a repo codename/placeholder: {hit!r} in {banner!r}"


def test_uninitialized_banner_names_no_repo(tmp_path):
    project_rag_dir = tmp_path / ".project-rag"
    project_rag_dir.mkdir()
    (project_rag_dir / "manifest.json").write_text("{}")

    banner = project_rag_detect.detect_banner(str(tmp_path))

    assert banner.startswith("project-rag: UNINITIALIZED")
    assert "run the project-RAG indexer" in banner
    _assert_clean(banner)


def _make_marker(tmp_path):
    project_rag_dir = tmp_path / ".project-rag"
    project_rag_dir.mkdir()
    (project_rag_dir / "manifest.json").write_text("{}")
    (project_rag_dir / "graph.db").write_text("db")
    return project_rag_dir


def test_stale_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(project_rag_detect, "_git", side_effect=["deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = project_rag_detect.detect_banner(str(tmp_path))

    assert banner.startswith("project-rag: STALE")
    _assert_clean(banner)


def test_stale_escalated_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(project_rag_detect, "_git", side_effect=["deadbeef", "80"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = project_rag_detect.detect_banner(str(tmp_path))

    assert "<system-reminder>" in banner
    assert "Run the project-RAG indexer to rebuild" in banner
    _assert_clean(banner)


def test_all_rendered_banner_strings_in_module_are_repo_free():
    import inspect

    source = inspect.getsource(project_rag_detect.detect_banner)
    _assert_clean(source)
