"""
Message-text regression for chunk C5a of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

`example_retrieval_repo_detect.detect_banner` is the highest reader-facing volume site in
that plan: its UNINITIALIZED / STALE session-start banners are injected into
agent context on every session. This pins that none of those rendered banner
strings name a private repo codename (or its OSS-scrub placeholder) — a
banner should tell the reader what command to run, never send them to a repo
an OSS reader cannot reach.

Spec backlink: docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md, chunk C5a.
"""

from __future__ import annotations

from unittest import mock

from coordinator_core.hooks import example_retrieval_repo_detect

_FORBIDDEN_SUBSTRINGS = (
    "example-doctrine-repo",
    "example-doctrine-repo",
    "cockpit",
    "example-fleet",
    "machine-b",
    "example-game-repo-repo",
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
    example_retrieval_repo_dir = tmp_path / ".example-retrieval-repo"
    example_retrieval_repo_dir.mkdir()
    (example_retrieval_repo_dir / "manifest.json").write_text("{}")

    banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert banner.startswith("example-retrieval-repo: UNINITIALIZED")
    assert "run the example-retrieval-repo indexer" in banner
    _assert_clean(banner)


def _make_marker(tmp_path):
    example_retrieval_repo_dir = tmp_path / ".example-retrieval-repo"
    example_retrieval_repo_dir.mkdir()
    (example_retrieval_repo_dir / "manifest.json").write_text("{}")
    (example_retrieval_repo_dir / "graph.db").write_text("db")
    return example_retrieval_repo_dir


def test_stale_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert banner.startswith("example-retrieval-repo: STALE")
    _assert_clean(banner)


def test_stale_escalated_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "80"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert "<system-reminder>" in banner
    assert "Run the example-retrieval-repo indexer to rebuild" in banner
    _assert_clean(banner)


def test_all_rendered_banner_strings_in_module_are_repo_free():
    import inspect

    source = inspect.getsource(example_retrieval_repo_detect.detect_banner)
    _assert_clean(source)
