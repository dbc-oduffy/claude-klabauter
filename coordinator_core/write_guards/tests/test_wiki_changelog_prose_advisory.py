
from __future__ import annotations

import pytest

from coordinator_core.write_guards import wiki_changelog_prose_advisory as guard


@pytest.fixture
def _repo(tmp_path, monkeypatch):
    repo_root = tmp_path / "repo"
    (repo_root / "docs" / "wiki").mkdir(parents=True)
    (repo_root / ".git").mkdir()
    monkeypatch.chdir(repo_root)
    return repo_root


def _write_payload(file_path: str, content: str, cwd: str) -> dict:
    return {
        "tool_name": "Write",
        "tool_input": {"file_path": file_path, "content": content},
        "cwd": cwd,
    }


def _edit_payload(file_path: str, new_string: str, cwd: str) -> dict:
    return {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": file_path,
            "old_string": "old",
            "new_string": new_string,
        },
        "cwd": cwd,
    }


def _multiedit_payload(file_path: str, new_strings, cwd: str) -> dict:
    return {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": file_path,
            "edits": [
                {"old_string": "old", "new_string": ns} for ns in new_strings
            ],
        },
        "cwd": cwd,
    }


def test_fires_on_ruling_date_inside_scope(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _write_payload(wiki_path, "Ruled 2026-08-01 that X.", str(_repo))
    result = guard.check(payload)
    assert result is not None
    assert (
        result["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    )
    assert "additionalContext" in result["hookSpecificOutput"]
    assert "ruling date" in result["hookSpecificOutput"]["additionalContext"]


def test_fires_on_dr_reference(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _write_payload(wiki_path, "Superseded by DR-344.", str(_repo))
    result = guard.check(payload)
    assert result is not None
    assert "DR-NNN" in result["hookSpecificOutput"]["additionalContext"]


def test_fires_on_was_now_phrasing(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _write_payload(
        wiki_path, "It was P, and it is now Q.", str(_repo)
    )
    result = guard.check(payload)
    assert result is not None
    assert "was ... now ..." in result["hookSpecificOutput"]["additionalContext"]


def test_allows_plain_doctrine_prose(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _write_payload(wiki_path, "State the rule as it stands.", str(_repo))
    assert guard.check(payload) is None


def test_never_fires_outside_docs_wiki(_repo):
    plan_path = str(_repo / "docs" / "plans" / "some-plan.md")
    (_repo / "docs" / "plans").mkdir(parents=True)
    payload = _write_payload(plan_path, "Ruled 2026-08-01 that X.", str(_repo))
    assert guard.check(payload) is None


def test_filename_changelog_exemption(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "2026-09-changelog.md")
    payload = _write_payload(wiki_path, "Ruled 2026-08-01 that X.", str(_repo))
    assert guard.check(payload) is None


def test_filename_exemption_is_case_insensitive(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "CHANGELOG.md")
    payload = _write_payload(wiki_path, "Ruled 2026-08-01 that X.", str(_repo))
    assert guard.check(payload) is None


def test_delta_scoped_edit_only_scans_new_string(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _edit_payload(wiki_path, "State the rule as it stands.", str(_repo))
    assert guard.check(payload) is None


def test_delta_scoped_edit_fires_on_new_string(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _edit_payload(wiki_path, "Superseded by DR-1.", str(_repo))
    assert guard.check(payload) is not None


def test_multiedit_scans_each_new_string(_repo):
    wiki_path = str(_repo / "docs" / "wiki" / "some-doctrine.md")
    payload = _multiedit_payload(
        wiki_path, ["plain prose", "Superseded by DR-9."], str(_repo)
    )
    assert guard.check(payload) is not None


def test_fails_open_when_repo_root_unresolvable(tmp_path):
    unresolvable = tmp_path / "no-repo" / "docs" / "wiki" / "some-doctrine.md"
    payload = _write_payload(
        str(unresolvable), "Ruled 2026-08-01 that X.", str(tmp_path / "no-repo")
    )
    assert guard.check(payload) is None


def test_scope_root_is_per_repo_not_bare_segment_match(tmp_path, monkeypatch):
    repo_a = tmp_path / "repo_a"
    (repo_a / ".git").mkdir(parents=True)
    (repo_a / "docs" / "wiki").mkdir(parents=True)

    other_tree_wiki_path = str(
        tmp_path / "unrelated" / "docs" / "wiki" / "some-doctrine.md"
    )
    payload = _write_payload(other_tree_wiki_path, "Ruled 2026-08-01.", str(repo_a))
    assert guard.check(payload) is None


def test_malformed_payload_fails_open():
    assert guard.check({}) is None
    assert guard.check({"tool_input": "not-a-dict"}) is None
    assert guard.check({"tool_name": "Write", "tool_input": {}}) is None


def test_never_raises_on_unexpected_shapes():
    assert guard.check({"tool_input": {"file_path": 5}}) is None
    assert guard.check(
        {"tool_name": "MultiEdit", "tool_input": {"file_path": "x", "edits": "bad"}}
    ) is None
