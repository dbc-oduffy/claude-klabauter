"""
Message-text regression for chunk C5a of
docs/plans/2026-08-12-message-text-stops-naming-an-unreachable-repo.md.

`example_retrieval_repo_detect.detect_banner` is the highest reader-facing volume site in
that plan: its UNINITIALIZED / STALE session-start banners are injected into
agent context on every session. This pins that none of those rendered banner
strings name a private repo codename (or its OSS-scrub placeholder) — a
banner should tell the reader what command to run, never send them to a repo
an OSS reader cannot reach.

Spec backlink: pln-message-text-stops-naming-a-re-5c92dd, chunk C5a.
"""

from __future__ import annotations

from unittest import mock

from coordinator_core.hooks import example_retrieval_repo_detect
from coordinator_core.ops.rag_freshness import FreshnessVerdict

_FORBIDDEN_SUBSTRINGS = (
    "coordinator-content-repo",
    "coordinator-content-repo",
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
    example_retrieval_repo_dir = tmp_path / ".project-rag"
    example_retrieval_repo_dir.mkdir()
    (example_retrieval_repo_dir / "state.json").write_text("{}")

    banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert banner.startswith("project-rag: UNINITIALIZED")
    assert "run the project-RAG indexer" not in banner
    assert "refresh not queued" in banner
    _assert_clean(banner)


def _make_marker(tmp_path):
    example_retrieval_repo_dir = tmp_path / ".project-rag"
    example_retrieval_repo_dir.mkdir()
    (example_retrieval_repo_dir / "state.json").write_text("{}")
    (example_retrieval_repo_dir / "graph.db").write_text("db")
    return example_retrieval_repo_dir


def test_stale_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert banner.startswith("project-rag: STALE")
    _assert_clean(banner)


def test_stale_escalated_banner_names_no_repo(tmp_path):
    _make_marker(tmp_path)

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "80"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(str(tmp_path))

    assert "<system-reminder>" in banner
    assert "Run the project-RAG indexer" not in banner
    assert "80 commits" in banner
    _assert_clean(banner)


def _stale_verdict(_root):
    return FreshnessVerdict(state="stale", remedy=("example-retrieval-repo-refresh",), source="stub")


def _absent_verdict(_root):
    return FreshnessVerdict(state="absent", remedy=("example-retrieval-repo-refresh",), source="stub")


def test_stale_banner_states_queued_refresh_and_spawns_once(tmp_path):
    _make_marker(tmp_path)
    spawn = mock.Mock()

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(
            str(tmp_path), verdict_fn=_stale_verdict, spawn_fn=spawn, marker_root=tmp_path / "m"
        )

    assert "refresh queued" in banner
    spawn.assert_called_once()
    _assert_clean(banner)


def test_absent_graph_db_states_queued_refresh_and_spawns_once(tmp_path):
    example_retrieval_repo_dir = tmp_path / ".project-rag"
    example_retrieval_repo_dir.mkdir()
    (example_retrieval_repo_dir / "state.json").write_text("{}")
    spawn = mock.Mock()

    banner = example_retrieval_repo_detect.detect_banner(
        str(tmp_path), verdict_fn=_absent_verdict, spawn_fn=spawn, marker_root=tmp_path / "m"
    )

    assert banner.startswith("project-rag: UNINITIALIZED")
    assert "refresh queued" in banner
    spawn.assert_called_once()


def test_live_marker_states_already_queued_and_does_not_respawn(tmp_path):
    _make_marker(tmp_path)
    spawn = mock.Mock()
    kwargs = dict(verdict_fn=_stale_verdict, spawn_fn=spawn, marker_root=tmp_path / "m")

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "5", "deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        example_retrieval_repo_detect.detect_banner(str(tmp_path), **kwargs)
        banner = example_retrieval_repo_detect.detect_banner(str(tmp_path), **kwargs)

    assert "refresh already queued" in banner
    spawn.assert_called_once()


def test_fresh_index_queues_nothing(tmp_path):
    _make_marker(tmp_path)
    spawn = mock.Mock()

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "0"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(
            str(tmp_path), verdict_fn=_stale_verdict, spawn_fn=spawn, marker_root=tmp_path / "m"
        )

    assert banner == "example-retrieval-repo: fresh (HEAD aligned)"
    spawn.assert_not_called()


def test_settings_home_indexed_repo_is_not_silent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = tmp_path / "my-repo"
    (repo / ".git").mkdir(parents=True)
    db = home / "project-rag" / "indexes" / "my_repo" / "root" / "graph.db"
    db.parent.mkdir(parents=True)
    db.write_text("db")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    spawn = mock.Mock()

    with mock.patch.object(example_retrieval_repo_detect, "_git", side_effect=["deadbeef", "5"]), mock.patch(
        "shutil.which", return_value="/usr/bin/git"
    ):
        banner = example_retrieval_repo_detect.detect_banner(
            str(repo), verdict_fn=_stale_verdict, spawn_fn=spawn, marker_root=tmp_path / "m"
        )

    assert banner.startswith("project-rag: STALE")
    assert "refresh queued" in banner
    spawn.assert_called_once()


def test_unindexed_repo_with_absent_verdict_queues_and_with_unknown_stays_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "home"))
    repo = tmp_path / "bare-repo"
    (repo / ".git").mkdir(parents=True)
    spawn = mock.Mock()

    assert example_retrieval_repo_detect.detect_banner(str(repo), spawn_fn=spawn, marker_root=tmp_path / "m") == ""
    spawn.assert_not_called()

    banner = example_retrieval_repo_detect.detect_banner(
        str(repo), verdict_fn=_absent_verdict, spawn_fn=spawn, marker_root=tmp_path / "m"
    )
    assert "refresh queued" in banner
    spawn.assert_called_once()


def test_all_rendered_banner_strings_in_module_are_repo_free():
    import inspect

    source = inspect.getsource(example_retrieval_repo_detect.detect_banner)
    _assert_clean(source)
