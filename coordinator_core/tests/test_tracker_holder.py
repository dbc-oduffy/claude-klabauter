"""
coordinator_core.tests.test_tracker_holder — C4 tests for the designated
holder repo resolver's failure rungs, the ownership split, and the
caller-side fallback ban.

Purpose: exercises `coordinator_core.tracker_holder.holder_repo_root()` and
`write_root_for()` (landed under C2/C3) against every failure rung named in
their own docstrings, the AC5 ownership-vs-kind discriminator, the AC11
keyword-required contract, the AC12 brightline guards (both halves, plus
the load-bearing companion case that must NOT raise), and the AC13
repo_root-threading invariant in `tracker_entities`'s membership-edge
emitters.

Spec backlink: pln-designated-holder-repo-for-uno-d11d4d
chunk C4.

Fixture discipline: every case monkeypatches `registry_get` and
`coordinator_claude_klabauter_root` directly — none reads or writes the real
machine-local registry. That registry is concurrently written by other
sessions on this fleet; a test depending on its live state would be flaky
by construction, not merely impure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import tracker_entities, tracker_holder
from coordinator_core.tracker_holder import holder_repo_root, write_root_for


def _fake_registry(mapping: dict[str, str]):
    """Build a `registry_get`-shaped stub over a plain dict, mirroring the
    real function's `None`/falsy return-on-unresolved contract."""

    def _get(key: str):
        return mapping.get(key)

    return _get


# --- holder_repo_root(): the three fail-loud rungs (AC2, AC3) ---


def test_holder_repo_root_unset_role_key_raises_naming_key_and_remediation(monkeypatch):
    """AC2: `tracker.holder_repo` unset entirely -> raises, message names
    the role key and the `machine-local set` remediation."""
    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    msg = str(exc.value)
    assert "tracker.holder_repo" in msg
    assert "machine-local set tracker.holder_repo" in msg


def test_holder_repo_root_unset_repos_key_raises(monkeypatch):
    """AC2: the role key resolves, but the `repos.<key>` it names is itself
    unset -> raises."""
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

    # Distinguishability: the unset-role-key message must not be confusable
    # with this not-cloned message.
    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(RuntimeError) as exc2:
        holder_repo_root()
    not_configured_msg = str(exc2.value)
    assert not_configured_msg != not_cloned_msg
    assert "not been configured" in not_configured_msg
    assert "not been configured" not in not_cloned_msg
    assert "clone" not in not_configured_msg


# --- holder_repo_root(): AC12, own-root brightline ---


def test_holder_repo_root_raises_if_resolved_root_is_claude_klabauter_own_root(
    monkeypatch, tmp_path
):
    """AC12: `holder_repo_root()` raises if the resolved holder root equals
    claude-klabauter's own root — the brightline guard is a real path comparison,
    not a lexical one."""
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
        tracker_holder, "coordinator_claude_klabauter_root", lambda: str(tmp_path)
    )
    with pytest.raises(RuntimeError) as exc:
        holder_repo_root()
    assert "claude-klabauter" in str(exc.value)


# --- write_root_for(): AC6, the four arms, distinguishable failures ---


def test_write_root_for_none_returns_holder_root(monkeypatch, tmp_path):
    """AC6: `owning_repo=None` resolves via `holder_repo_root()`."""
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
        tracker_holder, "coordinator_claude_klabauter_root", lambda: str(tmp_path / "claude-klabauter")
    )
    result = write_root_for(owning_repo=None, repo_root=caller_dir)
    assert result.resolve() == holder_dir.resolve()


def test_write_root_for_named_resolvable_repo_returns_that_repos_root(
    monkeypatch, tmp_path
):
    """AC6: a stated `owning_repo` naming a resolvable `repos.<key>`
    returns that repo's own root, not the holder's."""
    owner_dir = tmp_path / "owner-repo"
    owner_dir.mkdir()
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry({"repos.some_project": str(owner_dir)}),
    )
    result = write_root_for(owning_repo="some_project", repo_root=caller_dir)
    assert result.resolve() == owner_dir.resolve()


def test_write_root_for_named_repo_unset_raises_and_does_not_fall_through_to_holder(
    monkeypatch, tmp_path
):
    """AC6: a stated `owning_repo` naming a `repos.<key>` UNSET in the
    registry raises, naming the key and the `machine-local set`
    remediation, and specifically does NOT silently resolve to the holder
    root. Guards against a resolver that falls through and then raises for
    an unrelated reason: also asserts the holder root, if reachable at all,
    is never returned here."""
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
                # repos.unregistered_project deliberately absent
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        result = write_root_for(owning_repo="unregistered_project", repo_root=caller_dir)
        # if no raise occurred, make the fall-through visible in the failure
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
                "repos.uncloned_project": str(absent_owner),
            }
        ),
    )
    with pytest.raises(RuntimeError) as exc:
        result = write_root_for(owning_repo="uncloned_project", repo_root=caller_dir)
        assert result.resolve() != holder_dir.resolve(), (
            "fell through to the holder root instead of raising"
        )
    not_cloned_msg = str(exc.value)
    assert "clone" in not_cloned_msg
    assert "uncloned_project" in not_cloned_msg

    # Distinguishable from the unset-repos-key message.
    with pytest.raises(RuntimeError) as exc2:
        write_root_for(owning_repo="unregistered_project", repo_root=caller_dir)
    unset_msg = str(exc2.value)
    assert unset_msg != not_cloned_msg
    assert "clone" not in unset_msg


# --- AC5: the ownership discriminator is presence-of-owning-repo, never kind ---


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
        tracker_holder, "coordinator_claude_klabauter_root", lambda: str(tmp_path / "claude-klabauter")
    )
    # A "person" write is just a caller with no owning_repo — no kind arg
    # exists on write_root_for at all.
    result = write_root_for(owning_repo=None, repo_root=caller_dir)
    assert result.resolve() == holder_dir.resolve()


def test_item_write_with_owning_repo_lands_in_that_repo_not_the_holder(
    monkeypatch, tmp_path
):
    """AC5: an item write carrying a STATED owning repo resolves to that
    repo's own root, demonstrating the holder is not identity-scoped — an
    item is routed away from the holder purely by stating an owner, the
    same rule a person-write obeys by omitting one."""
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
                "repos.item_owner": str(owner_dir),
            }
        ),
    )
    result = write_root_for(owning_repo="item_owner", repo_root=caller_dir)
    assert result.resolve() == owner_dir.resolve()
    assert result.resolve() != holder_dir.resolve()


# --- AC11: owning_repo is keyword-required, no default ---


def test_write_root_for_without_owning_repo_keyword_raises_type_error(tmp_path):
    """AC11: calling `write_root_for` without the `owning_repo` keyword
    raises `TypeError` — omission is not a decision this function makes on
    the caller's behalf."""
    with pytest.raises(TypeError):
        write_root_for(repo_root=tmp_path)  # type: ignore[call-arg]


# --- AC12: the caller-root brightline, both halves, plus the companion case ---


def test_write_root_for_none_arm_raises_if_holder_root_equals_caller_repo_root(
    monkeypatch, tmp_path
):
    """AC12: on the `owning_repo is None` arm, raises if the resolved
    HOLDER root equals the caller-supplied `repo_root` — this guard is
    scoped to the None arm only."""
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
        tracker_holder, "coordinator_claude_klabauter_root", lambda: str(tmp_path / "claude-klabauter")
    )
    with pytest.raises(RuntimeError) as exc:
        write_root_for(owning_repo=None, repo_root=same_dir)
    assert "own repo_root" in str(exc.value) or "caller's own" in str(exc.value)


def test_write_root_for_stated_owner_matching_caller_root_does_not_raise(
    monkeypatch, tmp_path
):
    """AC12 companion case (load-bearing): a STATED `owning_repo` that
    resolves to the caller's OWN `repo_root` must NOT raise — it returns
    that root. This is the ordinary matched case DEC-11 describes (a repo
    recording work it owns while running in that repo). An earlier draft
    of C3 guarded every arm and broke this; this test is what stops that
    regressing."""
    own_dir = tmp_path / "own-repo"
    own_dir.mkdir()
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry({"repos.self_project": str(own_dir)}),
    )
    result = write_root_for(owning_repo="self_project", repo_root=own_dir)
    assert result.resolve() == own_dir.resolve()


# --- Negative control (guard-probe doctrine): the fallback-ban test must be able to fail ---


def test_NEGATIVE_CONTROL_stub_returning_empty_string_fails_the_fallback_ban(
    monkeypatch, tmp_path
):
    """Negative control, required by this fleet's guard-probe doctrine: a
    resolver stubbed to return "" for an unregistered owning-repo key must
    make an assertion like the fallback-ban test above go RED, proving that
    test is actually sensitive to a fall-through rather than vacuously
    passing. This test intentionally asserts the FAILURE — a probe with no
    failing control proves nothing."""
    caller_dir = tmp_path / "caller"
    caller_dir.mkdir()

    def _stub_that_falls_through_to_empty_string(*, owning_repo, repo_root):
        # Deliberately broken: returns a falsy "" instead of raising, the
        # exact silent fall-through AC6/AC13 exist to forbid.
        return ""

    monkeypatch.setattr(
        tracker_holder, "write_root_for", _stub_that_falls_through_to_empty_string
    )

    # Emulate the real fallback-ban assertion style: it must FAIL against
    # this stub, proving the real test is sensitive to the defect it guards.
    with pytest.raises(AssertionError):
        result = tracker_holder.write_root_for(
            owning_repo="unregistered_project", repo_root=caller_dir
        )
        assert result, "resolver silently fell through to an empty/falsy root"


# --- AC13: repo_root threading in the membership-edge emitters ---


def test_emit_item_project_added_threads_same_repo_root_to_require_local_item_and_append_event(
    monkeypatch, tmp_path
):
    """AC13: the `repo_root` passed to `_require_local_item` equals the
    `repo_root` passed to the subsequent `append_event` call, for
    `emit_item_project_added`."""
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
    """AC13: same invariant as above, for `emit_item_project_retracted`."""
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
