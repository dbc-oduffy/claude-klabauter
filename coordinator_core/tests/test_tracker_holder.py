
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_holder
from coordinator_core.tracker_holder import holder_repo_root, write_root_for


def _fake_registry(mapping: dict[str, str]):

    def _get(key: str):
        return mapping.get(key)

    return _get


def test_holder_repo_root_unset_role_key_raises_naming_key_and_remediation(monkeypatch):
    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    msg = str(exc.value)
    assert "tracker.holder_repo" in msg
    assert "machine-local set tracker.holder_repo" in msg


def test_holder_repo_root_unset_repos_key_raises(monkeypatch):
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry({"tracker.holder_repo": "example_store_repo"}),
    )
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    assert "repos.example_store_repo" in str(exc.value)


def test_holder_repo_root_not_cloned_raises_distinct_message(monkeypatch, tmp_path):
    """AC3: a resolved path absent on disk raises a message DISTINCT from
    the not-configured cases above — an operator must be able to tell "not
    configured" from "not cloned" from the exception text alone."""
    absent = tmp_path / "not-cloned-here"
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(absent),
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    not_cloned_msg = str(exc.value)
    assert "not been cloned" in not_cloned_msg or "clone" in not_cloned_msg

    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(RuntimeError) as exc2:
        holder_repo_root()
    not_configured_msg = str(exc2.value)
    assert not_configured_msg != not_cloned_msg
    assert "not been configured" in not_configured_msg
    assert "not been configured" not in not_cloned_msg
    assert "clone" not in not_configured_msg


def test_holder_repo_root_raises_if_resolved_root_is_claude_klabauter_own_root(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "claude_klabauter_itself",
                "repos.claude_klabauter_itself": str(tmp_path),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: Path(tmp_path)
    )
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    assert "claude-klabauter" in str(exc.value)


def test_write_root_for_none_returns_holder_root(monkeypatch, tmp_path):
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: Path(tmp_path / "claude-klabauter")
    )
    result = write_root_for(owning_repo=None, repo_root=caller_dir)
    assert result.resolve() == holder_dir.resolve()


def test_write_root_for_named_resolvable_repo_returns_that_repos_root(
    monkeypatch, tmp_path
):
    owner_dir = tmp_path / "owner-repo"
    owner_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/some_project": "some_project",
                "repos.some_project": str(owner_dir),
            }
        ),
    )
    result = write_root_for(owning_repo="acme/some_project", repo_root=caller_dir)
    assert result.resolve() == owner_dir.resolve()


def test_write_root_for_named_repo_unset_raises_and_does_not_fall_through_to_holder(
    monkeypatch, tmp_path
):
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
                "repo_slug.acme/unregistered_project": "unregistered_project",
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        result = write_root_for(
            owning_repo="acme/unregistered_project", repo_root=caller_dir
        )
        assert result.resolve() != holder_dir.resolve(), (
            "fell through to the holder root instead of raising"
        )
    unset_msg = str(exc.value)
    assert "unregistered_project" in unset_msg
    assert "machine-local set repos.unregistered_project" in unset_msg


def test_write_root_for_named_repo_not_cloned_raises_distinct_message_and_no_fallthrough(
    monkeypatch, tmp_path
):
    """AC6: a stated `owning_repo` resolving to a `repos.<key>` whose path
    is absent on disk raises a message DISTINCT from the unset-key case
    above, naming the clone step, and does NOT fall through to the holder
    root."""
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    absent_owner = tmp_path / "never-cloned"
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
                "repo_slug.acme/uncloned_project": "uncloned_project",
                "repos.uncloned_project": str(absent_owner),
                "repo_slug.acme/unregistered_project": "unregistered_project",
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        result = write_root_for(
            owning_repo="acme/uncloned_project", repo_root=caller_dir
        )
        assert result.resolve() != holder_dir.resolve(), (
            "fell through to the holder root instead of raising"
        )
    not_cloned_msg = str(exc.value)
    assert "clone" in not_cloned_msg
    assert "uncloned_project" in not_cloned_msg

    with pytest.raises(RuntimeError) as exc2:
        write_root_for(owning_repo="acme/unregistered_project", repo_root=caller_dir)
    unset_msg = str(exc2.value)
    assert unset_msg != not_cloned_msg
    assert "clone" not in unset_msg


def test_person_write_with_no_owning_repo_lands_in_holder_as_ordinary_none_case(
    monkeypatch, tmp_path
):
    """AC5: a person-entity write carrying no owning repo lands in the
    holder root — as an ORDINARY instance of the `owning_repo is None` arm,
    not via any kind-keyed branch. `write_root_for` takes no `kind`
    parameter at all, so this exercises the same call shape a project-item
    write with no owning repo would use — the discriminator is presence of
    an owning repo, never what kind of thing is being written (PM ruling
    2026-08-11 retired the identity-scoped reading)."""
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: Path(tmp_path / "claude-klabauter")
    )
    result = write_root_for(owning_repo=None, repo_root=caller_dir)
    assert result.resolve() == holder_dir.resolve()


def test_item_write_with_owning_repo_lands_in_that_repo_not_the_holder(
    monkeypatch, tmp_path
):
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    owner_dir = tmp_path / "item-owning-repo"
    owner_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
                "repo_slug.acme/item_owner": "item_owner",
                "repos.item_owner": str(owner_dir),
            }
        ),
    )
    result = write_root_for(owning_repo="acme/item_owner", repo_root=caller_dir)
    assert result.resolve() == owner_dir.resolve()
    assert result.resolve() != holder_dir.resolve()


def test_write_root_for_empty_string_owning_repo_raises_and_never_reaches_slug_resolution(
    monkeypatch, tmp_path
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()

    def _boom(key: str):
        raise AssertionError(
            f"registry_get({key!r}) called — '' must never reach slug "
            "resolution at all"
        )

    monkeypatch.setattr(tracker_holder, "registry_get", _boom)
    with pytest.raises(RuntimeError) as exc:
        write_root_for(owning_repo="", repo_root=caller_dir)
    msg = str(exc.value)
    assert "empty string" in msg
    assert "repos." not in msg


def test_write_root_for_non_member_slug_refused_not_routed_to_holder(
    monkeypatch, tmp_path
):
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        result = write_root_for(owning_repo="acme/no_such_slug", repo_root=caller_dir)
        assert result.resolve() != holder_dir.resolve(), (
            "fell through to the holder root instead of raising"
        )
    msg = str(exc.value)
    assert "acme/no_such_slug" in msg
    assert "repo_slug.acme/no_such_slug" in msg
    assert "machine-local set repo_slug.acme/no_such_slug" in msg


def test_write_root_for_slug_index_unset_message_distinct_from_repos_key_unset_message(
    monkeypatch, tmp_path
):
    """AC9: the `repo_slug.<slug>` index-entry-absent message (an earlier,
    distinct failure inside `_resolve_repos_key_for_slug`) is a DISTINCT
    message from `write_root_for`'s own `repos.<key>`-unset message for the
    subsequent hop — neither ever falls through to the holder."""
    holder_dir = tmp_path / "holder"
    holder_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(holder_dir),
                "repo_slug.acme/repos_key_unset": "repos_key_unset",
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc_index:
        write_root_for(owning_repo="acme/no_index_entry", repo_root=caller_dir)
    index_unset_msg = str(exc_index.value)

    with pytest.raises(RuntimeError) as exc_repos_key:
        write_root_for(owning_repo="acme/repos_key_unset", repo_root=caller_dir)
    repos_key_unset_msg = str(exc_repos_key.value)

    assert index_unset_msg != repos_key_unset_msg
    assert "repo_slug.acme/no_index_entry" in index_unset_msg
    assert "repo_slug." not in repos_key_unset_msg
    assert "repos.repos_key_unset" in repos_key_unset_msg


def test_write_root_for_without_owning_repo_keyword_raises_type_error(tmp_path):
    with pytest.raises(TypeError):
        write_root_for(repo_root=tmp_path)  # type: ignore[call-arg]


def test_write_root_for_none_arm_raises_if_holder_root_equals_caller_repo_root(
    monkeypatch, tmp_path
):
    same_dir = tmp_path / "same"
    same_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "example_store_repo",
                "repos.example_store_repo": str(same_dir),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: Path(tmp_path / "claude-klabauter")
    )
    with pytest.raises(RuntimeError) as exc:
        write_root_for(owning_repo=None, repo_root=same_dir)
    assert "own repo_root" in str(exc.value) or "caller's own" in str(exc.value)


def test_write_root_for_stated_owner_matching_caller_root_does_not_raise(
    monkeypatch, tmp_path
):
    own_dir = tmp_path / "own-repo"
    own_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self_project": "self_project",
                "repos.self_project": str(own_dir),
            }
        ),
    )
    result = write_root_for(owning_repo="acme/self_project", repo_root=own_dir)
    assert result.resolve() == own_dir.resolve()


def test_NEGATIVE_CONTROL_stub_returning_empty_string_fails_the_fallback_ban(
    monkeypatch, tmp_path
):
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()

    def _stub_that_falls_through_to_empty_string(*, owning_repo, repo_root):
        return ""

    monkeypatch.setattr(
        tracker_holder, "write_root_for", _stub_that_falls_through_to_empty_string
    )

    with pytest.raises(AssertionError):
        result = tracker_holder.write_root_for(
            owning_repo="unregistered_project", repo_root=caller_dir
        )
        assert result, "resolver silently fell through to an empty/falsy root"


def test_emit_item_project_added_threads_same_repo_root_to_require_local_item_and_append_event(
    monkeypatch, tmp_path
):
    seen: dict[str, object] = {}

    def _fake_require_local_item(item_id, *, repo_root):
        seen["require_local_item_repo_root"] = repo_root

    def _fake_append_event(event, *, repo_root):
        seen["append_event_repo_root"] = repo_root
        return event

    monkeypatch.setattr(
        tracker_entities, "_require_local_item", _fake_require_local_item
    )
    monkeypatch.setattr(
        tracker_entities.tracker_store, "append_event", _fake_append_event
    )

    root = tmp_path / "repo"
    tracker_entities.emit_item_project_added("itm-x", "prj-y", repo_root=root)

    assert seen["require_local_item_repo_root"] == root
    assert seen["append_event_repo_root"] == root


def test_emit_item_project_retracted_threads_same_repo_root_to_require_local_item_and_append_event(
    monkeypatch, tmp_path
):
    seen: dict[str, object] = {}

    def _fake_require_local_item(item_id, *, repo_root):
        seen["require_local_item_repo_root"] = repo_root

    def _fake_append_event(event, *, repo_root):
        seen["append_event_repo_root"] = repo_root
        return event

    monkeypatch.setattr(
        tracker_entities, "_require_local_item", _fake_require_local_item
    )
    monkeypatch.setattr(
        tracker_entities.tracker_store, "append_event", _fake_append_event
    )

    root = tmp_path / "repo"
    tracker_entities.emit_item_project_retracted("itm-x", "prj-y", repo_root=root)

    assert seen["require_local_item_repo_root"] == root
    assert seen["append_event_repo_root"] == root
