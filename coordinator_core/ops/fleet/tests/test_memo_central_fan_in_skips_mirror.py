"""A publish mirror registered as the content root must not capture the central receiver, nor be suggested back."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.fleet import _memo_resolver as mr

CENTRAL = {"coordinator-content-repo-em", "content-root-em"}


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mirror = tmp_path / "coordinator-claude"
    doe = tmp_path / "coordinator-content-repo"
    mirror.mkdir()
    doe.mkdir()
    repos = {"repos.content_root": str(mirror), "repos.doe_claude": str(doe)}
    monkeypatch.setattr(mr, "read_central_receiver_ids", lambda: set(CENTRAL))
    monkeypatch.setattr(mr, "read_receiver_aliases", lambda: {})
    monkeypatch.setattr(mr, "read_doe_identity", lambda: {"centralReceiverIds": ["coordinator-content-repo-em", "content-root-em"]})
    monkeypatch.setattr(mr, "read_registry_repos", lambda: dict(repos))
    return repos, mirror, doe


def test_mirror_registered_as_content_root_is_not_a_central_match(box) -> None:
    repos, _, _ = box
    assert list(mr._central_fan_in_matches(set(CENTRAL), repos)) == ["repos.doe_claude"]


def test_refusal_suggests_an_id_that_resolves_to_the_working_tree(box) -> None:
    _, mirror, _ = box
    text = mr.never_inbox_mirror_refusal("claude-klabauter-em", mirror)
    assert text is not None
    assert "Send coordinator/doctrine topics to coordinator-content-repo-em;" in text
    assert "content-root-em" not in text


def test_refusal_names_no_bouncing_id_when_only_the_mirror_is_registered(box, monkeypatch) -> None:
    repos, mirror, _ = box
    repos.pop("repos.doe_claude")
    text = mr.never_inbox_mirror_refusal("content-root-em", mirror)
    assert text is not None
    assert "content-root-em;" not in text and "Send coordinator/doctrine topics" not in text
    assert "register repos.doe_claude" in text
