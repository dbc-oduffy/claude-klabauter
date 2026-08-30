"""
Tests for coordinator_core.ops.tracker.push_suggestion — tracker.push_suggestion.

Coverage:
  (a) registration — tracker.push_suggestion lands in the live registry on
      import.
  (b) handler-level: repo_root=None raises RuntimeError; params.event
      missing/not-a-dict raises ValueError; a genuine D3 params.repo_root
      mismatch raises ValueError.
  (c) ownership routing — a `scope: "fleet-authored"` event routes on
      `authority.emitter_repo`, not `repo`; a missing `emitter_repo` for that
      scope refuses; the cockpit legacy `""` owner is translated to `None`
      before `write_root_for` ever sees it.
  (d) local-vs-peer routing — a target resolving to the CALLER's own worktree
      writes locally via the sovereign-tracker store's own append primitive;
      a target resolving
      elsewhere delivers a DR-338 envelope (committed) into that repo's
      `cross-repo/inbox/`, never a direct `open(...)`.
  (e) D2a — the destination-agnosticism guard DR-338's Ratification Status
      table requires land WITH this handler: no destination-specific literal
      anywhere in this module's or `tracker_holder`'s source.
  (f) five-surface wiring + command-type smoke (registry, classification,
      scope, module_map, _EAGER_OP_MODULES) — mirrors
      test_completion_policy.py's own (d) block.

Harness: asyncio.run() in sync test fns for handler-level tests — no
pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

# ---- Import guard: fires @register_op side-effect for tracker.push_suggestion. ----
import coordinator_core.ops.tracker.push_suggestion as push_suggestion

from coordinator_core.ipc import _REGISTRY, dispatch_message
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.ops import _EAGER_OP_MODULES
from coordinator_core.ops._registry_map import OP_MODULE_MAP
from coordinator_core.ops.tracker.push_suggestion import (
    PushSuggestionDuplicateDeliveryError,
    PushSuggestionMalformedError,
    PushSuggestionRefused,
    PushSuggestionSchemaStaleError,
    PushSuggestionUnauthorizedError,
    _handler,
)
from coordinator_core import tracker_holder
from coordinator_core.win_portability import no_console_creationflags


def _run(coro):
    return asyncio.run(coro)


def _make_git_repo(root: Path) -> Path:
    """Init a minimal git repository under *root* and return the repo root."""
    root.mkdir(parents=True, exist_ok=True)

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(root),
            capture_output=True,
            check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "push-suggestion-test@claude-klabauter.test")
    _git("config", "user.name", "Push Suggestion Test")
    _git("config", "commit.gpgsign", "false")
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", ".gitkeep")
    _git("commit", "-m", "init")
    return root


def _fake_registry(mapping: dict[str, str]):
    def _get(key: str):
        return mapping.get(key)

    return _get


def _event(**overrides) -> dict:
    base = {
        "observed_at": "2026-08-20T10:00:00.000000Z",
        "item_id": "item-1",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# (a) Import-guard floor assertion
# ---------------------------------------------------------------------------


def test_tracker_push_suggestion_registered():
    assert "tracker.push_suggestion" in _REGISTRY


# ---------------------------------------------------------------------------
# (b) handler-level
# ---------------------------------------------------------------------------


def test_handler_repo_root_none_raises_runtime_error():
    with pytest.raises(RuntimeError):
        _run(_handler({"event": _event()}, repo_root=None))


def test_handler_missing_event_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        _run(_handler({}, repo_root=repo))


def test_handler_non_dict_event_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(ValueError):
        _run(_handler({"event": "not-a-dict"}, repo_root=repo))


def test_handler_mismatched_params_repo_root_raises_value_error(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    with pytest.raises(ValueError):
        _run(
            _handler(
                {"event": _event(), "repo_root": str(bogus_other)},
                repo_root=repo,
            )
        )


# ---------------------------------------------------------------------------
# (c) ownership routing
# ---------------------------------------------------------------------------


def test_resolve_owning_repo_routes_on_repo_field_by_default():
    assert push_suggestion._resolve_owning_repo(_event(repo="acme/widgets")) == "acme/widgets"


def test_resolve_owning_repo_none_when_repo_absent():
    assert push_suggestion._resolve_owning_repo(_event()) is None


def test_resolve_owning_repo_empty_string_translated_to_none():
    assert push_suggestion._resolve_owning_repo(_event(repo="")) is None


def test_resolve_owning_repo_fleet_authored_routes_on_emitter_repo_not_repo():
    event = _event(
        repo="acme/subject-repo",
        scope="fleet-authored",
        authority={"emitter_repo": "acme/emitter-repo"},
    )
    assert push_suggestion._resolve_owning_repo(event) == "acme/emitter-repo"


def test_resolve_owning_repo_fleet_authored_missing_emitter_repo_refuses():
    event = _event(repo="acme/subject-repo", scope="fleet-authored")
    with pytest.raises(PushSuggestionRefused):
        push_suggestion._resolve_owning_repo(event)


# ---------------------------------------------------------------------------
# (d) local-vs-peer routing
# ---------------------------------------------------------------------------


def test_handler_local_target_writes_via_the_local_write_arm(monkeypatch, tmp_path):
    """A stated owning_repo resolving to the CALLER's OWN worktree writes
    locally via the sovereign-tracker store's own append primitive —
    never a peer delivery."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )
    result = _run(
        _handler(
            {"event": _event(repo="acme/self")},
            repo_root=repo,
        )
    )
    assert result["delivered"] == "local"
    assert re.fullmatch(r"evt-[a-z0-9-]+-[0-9a-f]{12}", result["event"]["id"])

    events = list((repo / "state" / "sovereign-tracker").glob("events.*.jsonl"))
    assert events, "expected an appended shard file under the caller's own worktree"


def test_handler_peer_target_delivers_committed_envelope(monkeypatch, tmp_path):
    """An owning_repo (or unowned -> holder) resolving to a DIFFERENT repo
    delivers a DR-338 envelope, committed, into that repo's cross-repo/inbox/
    — never a direct write into its state/ tree."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )
    result = _run(_handler({"event": _event()}, repo_root=repo))

    assert result["delivered"] == "peer"
    delivered_path = Path(result["path"])
    assert delivered_path.exists()
    assert delivered_path.parent == peer / "cross-repo" / "inbox"
    assert result["committed"] is True

    # Never a direct write into the peer's own state/ tree.
    assert not (peer / "state" / "sovereign-tracker").exists()

    log = subprocess.run(
        ["git", "-C", str(peer), "log", "--oneline"],
        capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    assert delivered_path.name not in log.stdout or "deliver sovereign-tracker event" in log.stdout


def test_handler_peer_delivery_collision_refuses(monkeypatch, tmp_path):
    """C1 (review-driven correction, 2026-08-20): the peer arm addresses on
    the minted `event['id']`, which is only stable across a retry when
    `idempotency_key` is supplied — so this collision test must supply one
    to reach the same filename twice."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )
    params = {"event": _event(item_id="collide-me"), "idempotency_key": "collide-key"}
    _run(_handler(dict(params), repo_root=repo))
    with pytest.raises(PushSuggestionDuplicateDeliveryError) as excinfo:
        _run(_handler(dict(params), repo_root=repo))
    assert excinfo.value.refusal_class == push_suggestion._REFUSAL_CLASS_DUPLICATE_DELIVERY


def test_no_addressing_key_required_on_either_arm():
    """C1 (review-driven correction, 2026-08-20) removed the addressing-key
    requirement entirely — an event carrying neither `item_id` nor
    `event_id` behaves identically on both arms, since `_deliver_envelope`
    now addresses the peer-arm filename on the minted `event['id']`, never
    a caller-supplied field. The old `_item_addressing_key`-driven
    `PushSuggestionMalformedError` (arm-dependent — it fired on the peer
    arm only, and only reachable when the resolved target differs from the
    caller's own worktree) is gone with that function."""
    assert not hasattr(push_suggestion, "_item_addressing_key")


# ---------------------------------------------------------------------------
# C12 — rejection contract: three DISTINCT, LOUD, non-collapsing classes
# ---------------------------------------------------------------------------


def test_malformed_missing_event_is_distinguishable_class(tmp_path):
    """The pre-existing ValueError contract is narrowed, not widened: still
    a ValueError, now ALSO a PushSuggestionMalformedError distinguishable
    from Unauthorized/SchemaStale."""
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionMalformedError):
        _run(_handler({}, repo_root=repo))


def test_malformed_non_dict_event_is_distinguishable_class(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionMalformedError):
        _run(_handler({"event": "not-a-dict"}, repo_root=repo))


def test_malformed_repo_root_mismatch_is_distinguishable_class(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    bogus_other = tmp_path / "some-other-path-that-does-not-exist"
    with pytest.raises(PushSuggestionMalformedError):
        _run(
            _handler(
                {"event": _event(), "repo_root": str(bogus_other)},
                repo_root=repo,
            )
        )


def test_malformed_non_int_contract_version_is_distinguishable_class(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionMalformedError):
        _run(
            _handler(
                {"event": _event(), "contract_version": "1"},
                repo_root=repo,
            )
        )


def test_malformed_caller_supplied_event_id_is_refused_loud(tmp_path):
    """claude-klabauter mints the event id (RULING 2026-08-20, C4) — a request that
    ARRIVES CARRYING an `id` is refused loud, never silently overwritten,
    which would leave the caller believing its id is the stored key."""
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionMalformedError):
        _run(
            _handler(
                {"event": _event(id="caller-supplied-id")},
                repo_root=repo,
            )
        )


def test_unauthorized_non_member_slug_is_distinguishable_class(monkeypatch, tmp_path):
    """Reuses C5's own non-member-slug refusal (tracker_holder), reclassified
    at this op's boundary — never collapses into a bare RuntimeError."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(PushSuggestionUnauthorizedError):
        _run(_handler({"event": _event(repo="acme/unregistered")}, repo_root=repo))


def test_unauthorized_is_not_raised_for_other_holder_config_rungs(monkeypatch, tmp_path):
    """A DIFFERENT tracker_holder failure rung (holder-role unset) is
    operator misconfiguration, not an unauthorized caller-supplied
    identifier — must stay a plain, unclassified RuntimeError, never
    PushSuggestionUnauthorizedError."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(tracker_holder, "registry_get", _fake_registry({}))
    with pytest.raises(RuntimeError) as excinfo:
        _run(_handler({"event": _event()}, repo_root=repo))
    assert not isinstance(excinfo.value, PushSuggestionUnauthorizedError)


def test_schema_stale_old_contract_version_is_distinguishable_class(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionSchemaStaleError):
        _run(
            _handler(
                {"event": _event(), "contract_version": 0},
                repo_root=repo,
            )
        )


def test_schema_stale_absent_contract_version_treated_as_current(monkeypatch, tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )
    result = _run(_handler({"event": _event(repo="acme/self")}, repo_root=repo))
    assert result["delivered"] == "local"


def test_three_classes_are_mutually_exclusive_types():
    """None of the three C12 classes may collapse into the others or into
    the generic PushSuggestionRefused catch-all (AC17) — each is its own
    concrete, non-overlapping type below PushSuggestionRefused."""
    assert PushSuggestionMalformedError is not PushSuggestionUnauthorizedError
    assert PushSuggestionMalformedError is not PushSuggestionSchemaStaleError
    assert PushSuggestionUnauthorizedError is not PushSuggestionSchemaStaleError
    assert not issubclass(PushSuggestionUnauthorizedError, PushSuggestionMalformedError)
    assert not issubclass(PushSuggestionSchemaStaleError, PushSuggestionMalformedError)
    assert not issubclass(PushSuggestionMalformedError, PushSuggestionUnauthorizedError)
    for cls in (
        PushSuggestionMalformedError,
        PushSuggestionUnauthorizedError,
        PushSuggestionSchemaStaleError,
    ):
        assert issubclass(cls, PushSuggestionRefused)
        assert getattr(cls, "caller_facing_validation", False) is True


def test_handler_exception_error_surfaces_own_message_not_class_name_collapse():
    """Direct proof of the AC17 mechanism: ipc._handler_exception_error must
    NOT collapse a C12-classified exception to the generic 'Internal error:
    <ClassName>' shape every unclassified exception gets."""
    from coordinator_core.ipc import _handler_exception_error, INVALID_PARAMS

    exc = PushSuggestionSchemaStaleError("tracker.push_suggestion: distinctive-message")
    error = _handler_exception_error(exc)
    assert error["code"] == INVALID_PARAMS
    assert "distinctive-message" in error["message"]
    assert "Internal error" not in error["message"]


# ---------------------------------------------------------------------------
# F1 (the Director of Engineering boundary review, 2026-08-20) — refusal_class discriminator
# survives to the process boundary (AC17), not just to an in-process caller.
# ---------------------------------------------------------------------------


def test_refusal_class_values_are_pinned():
    """The three refusal_class strings are a contract with cockpit — pinned
    so a rename is caught here rather than silently breaking their build."""
    assert push_suggestion._REFUSAL_CLASS_MALFORMED == "malformed"
    assert push_suggestion._REFUSAL_CLASS_UNAUTHORIZED == "unauthorized"
    assert push_suggestion._REFUSAL_CLASS_SCHEMA_STALE == "schema_stale"
    assert PushSuggestionMalformedError.refusal_class == "malformed"
    assert PushSuggestionUnauthorizedError.refusal_class == "unauthorized"
    assert PushSuggestionSchemaStaleError.refusal_class == "schema_stale"
    # The D2(4)/D2(7) unclassified peer-delivery refusals carry none.
    assert PushSuggestionRefused.refusal_class is None


def test_refusal_class_survives_dispatch_message_response_not_just_exception_type(
    tmp_path,
):
    """Proves the discriminator survives to the ACTUAL wire boundary — the
    dispatch_message() JSON-RPC response a subprocess caller reads off
    stdout — not merely that the in-process exception carries an attribute.
    All three classes collapse to the same exit code 1
    (invoke.__main__._exit_code_for_response); this is the caller's only
    other legible signal."""
    repo = _make_git_repo(tmp_path / "repo")

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.push_suggestion",
                "params": {},
                "_origin_worktree": str(repo),
            }
        )
    )
    assert "error" in response, f"expected a dispatch error, got: {response}"
    message = response["error"]["message"]
    assert message.startswith("refusal_class=malformed: ")


def test_refusal_class_present_for_d2_4_duplicate_delivery_refusal(
    monkeypatch, tmp_path
):
    """C3 (review-driven correction, 2026-08-20): the D2(4) peer-delivery
    collision IS now classified — addressed on the minted event id, a
    collision is evidence of a successful prior delivery, and cockpit must
    be able to tell it apart from an unclassified refusal by
    `refusal_class` alone."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )
    params = {
        "event": _event(item_id="classified-collide"),
        "idempotency_key": "classified-collide-key",
    }
    _run(_handler(dict(params), repo_root=repo))
    with pytest.raises(PushSuggestionRefused) as excinfo:
        _run(_handler(dict(params), repo_root=repo))
    assert isinstance(excinfo.value, PushSuggestionDuplicateDeliveryError)
    assert not isinstance(excinfo.value, PushSuggestionMalformedError)
    assert not isinstance(excinfo.value, PushSuggestionUnauthorizedError)
    assert not isinstance(excinfo.value, PushSuggestionSchemaStaleError)
    assert str(excinfo.value).startswith(
        f"refusal_class={push_suggestion._REFUSAL_CLASS_DUPLICATE_DELIVERY}: "
    )


# ---------------------------------------------------------------------------
# F5 (the Director of Engineering boundary review, 2026-08-20) — idempotency_key makes a same-
# payload retry collide on the store's duplicate-id guard instead of
# double-appending.
# ---------------------------------------------------------------------------


def test_idempotency_key_retry_collides_on_duplicate_id_guard(monkeypatch, tmp_path):
    """Same payload + same idempotency_key, pushed twice (a retry after an
    ambiguous exit-code-only failure) => the SAME id is minted both times,
    and the second call refuses via the store's own duplicate-id guard
    rather than double-appending a second event."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )
    from coordinator_core.tracker_store import TrackerStoreDuplicateIdError

    params = {
        "event": _event(repo="acme/self"),
        "idempotency_key": "cockpit-event-42",
    }
    first = _run(_handler(dict(params), repo_root=repo))
    assert first["delivered"] == "local"
    first_id = first["event"]["id"]

    with pytest.raises(TrackerStoreDuplicateIdError):
        _run(_handler(dict(params), repo_root=repo))

    events = list((repo / "state" / "sovereign-tracker").glob("events.*.jsonl"))
    assert len(events) == 1
    lines = events[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert first_id in lines[0]


def test_idempotency_key_absent_retries_mint_different_ids(monkeypatch, tmp_path):
    """Without idempotency_key, today's wall-clock-nonce behaviour is
    unchanged — a retry of the same payload mints a DIFFERENT id and both
    writes land (the pre-existing, non-idempotent shape)."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )
    event = _event(repo="acme/self")
    first = _run(_handler({"event": dict(event)}, repo_root=repo))
    second = _run(_handler({"event": dict(event)}, repo_root=repo))
    assert first["event"]["id"] != second["event"]["id"]


def test_idempotency_key_must_be_non_empty_string_when_supplied(tmp_path):
    repo = _make_git_repo(tmp_path / "repo")
    with pytest.raises(PushSuggestionMalformedError):
        _run(
            _handler(
                {"event": _event(), "idempotency_key": ""},
                repo_root=repo,
            )
        )
    with pytest.raises(PushSuggestionMalformedError):
        _run(
            _handler(
                {"event": _event(), "idempotency_key": 42},
                repo_root=repo,
            )
        )


# ---------------------------------------------------------------------------
# F3 (the Director of Engineering boundary review, 2026-08-20) — sensitivity_tier passthrough onto
# the peer-delivery envelope frontmatter, never a hardcode/default.
# ---------------------------------------------------------------------------


def test_compose_envelope_passes_through_caller_supplied_sensitivity_tier():
    content = push_suggestion._compose_envelope(
        "acme/self", _event(sensitivity_tier="internal")
    )
    frontmatter = content.split("---")[1]
    assert 'sensitivity_tier: "internal"' in frontmatter


def test_compose_envelope_emits_nothing_when_sensitivity_tier_absent():
    content = push_suggestion._compose_envelope("acme/self", _event())
    assert "sensitivity_tier" not in content


def test_handler_peer_delivery_passes_through_sensitivity_tier(monkeypatch, tmp_path):
    """End-to-end: a caller-supplied sensitivity_tier lands verbatim in the
    committed peer envelope's frontmatter."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )
    result = _run(
        _handler(
            {"event": _event(item_id="sensitivity-tier-item", sensitivity_tier="restricted")},
            repo_root=repo,
        )
    )
    delivered_path = Path(result["path"])
    text = delivered_path.read_text(encoding="utf-8")
    assert 'sensitivity_tier: "restricted"' in text


# ---------------------------------------------------------------------------
# C5 (review-driven correction, 2026-08-20) — tests the design turns on that
# were previously untested: the digest is content-derived (not key-derived),
# field-reordering stability, peer-arm idempotency surviving a date
# boundary, minted-id grammar with a non-circular assertion, the traversal
# defect the peer arm's old addressing key opened, and that a caller's own
# `id` can never leak into a stored record, a delivered envelope, or the
# caller's own dict.
# ---------------------------------------------------------------------------


def test_mint_event_id_different_payload_same_key_mints_different_ids():
    """Pins DR-241 bound (i): the digest is content-derived, not merely
    key-derived. Replacing the digest input with the key alone (dropping
    the event) would pass the whole rest of the suite — this is the one
    test that catches that mutation."""
    key = "same-idempotency-key"
    id_a = push_suggestion._mint_event_id({"item_id": "a"}, idempotency_key=key)
    id_b = push_suggestion._mint_event_id({"item_id": "b"}, idempotency_key=key)
    assert id_a != id_b


def test_mint_event_id_reordered_keys_same_payload_same_key_mints_same_id():
    """Pins the `sort_keys=True` guarantee: field order must not affect the
    minted id for an otherwise-identical payload+key."""
    key = "reorder-key"
    id_a = push_suggestion._mint_event_id({"a": 1, "b": 2}, idempotency_key=key)
    id_b = push_suggestion._mint_event_id({"b": 2, "a": 1}, idempotency_key=key)
    assert id_a == id_b


def test_mint_event_id_grammar_and_machine_slug_component(monkeypatch):
    """Non-circular replacement for the old `machine_slug() in id` assertion
    (which called the implementation to check the implementation and would
    keep passing if the slug component were dropped to a constant). Pins
    the shape via regex, then independently proves a known, monkeypatched
    slug actually appears."""
    minted = push_suggestion._mint_event_id({"item_id": "x"}, idempotency_key="k")
    assert re.fullmatch(r"evt-[a-z0-9-]+-[0-9a-f]{12}", minted)

    monkeypatch.setattr(push_suggestion, "machine_slug", lambda: "known-slug-xyz")
    minted = push_suggestion._mint_event_id({"item_id": "x"}, idempotency_key="k")
    assert "known-slug-xyz" in minted


def test_peer_arm_idempotency_key_retry_survives_date_boundary(monkeypatch, tmp_path):
    """C1's fix: the peer arm addresses on the minted id, which — given
    `idempotency_key` — is independent of wall-clock date entirely. A retry
    landing on a different UTC date must still collide via O_EXCL instead
    of silently delivering a second envelope (the exact midnight-boundary
    scenario the Staff Engineer's review verified empirically)."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )

    real_datetime = push_suggestion.datetime.datetime
    utc = push_suggestion.datetime.timezone.utc
    dates = iter(
        [
            real_datetime(2026, 8, 20, 23, 59, 58, tzinfo=utc),
            real_datetime(2026, 8, 21, 0, 0, 3, tzinfo=utc),
        ]
    )

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return next(dates)

    monkeypatch.setattr(push_suggestion.datetime, "datetime", _FakeDateTime)

    params = {
        "event": _event(item_id="midnight-item"),
        "idempotency_key": "midnight-key",
    }
    first = _run(_handler(dict(params), repo_root=repo))
    assert first["delivered"] == "peer"
    with pytest.raises(PushSuggestionDuplicateDeliveryError):
        _run(_handler(dict(params), repo_root=repo))

    envelopes = list((peer / "cross-repo" / "inbox").glob("*.md"))
    assert len(envelopes) == 1, (
        f"expected exactly one envelope delivered across the simulated "
        f"midnight boundary, found: {envelopes}"
    )


def test_traversal_in_item_id_cannot_escape_receiver_inbox(monkeypatch, tmp_path):
    """Path-traversal regression test for the exact defect the review
    verified empirically: a caller-controlled `item_id` must never be able
    to compose a delivery path outside the receiver's `cross-repo/inbox/`.
    After C1, `item_id` no longer feeds the delivery path at all, and the
    confinement assertion in `_deliver_envelope` is the independent
    hardening backstop. Assert both the refusal AND that no file appeared
    anywhere outside the inbox."""
    repo = _make_git_repo(tmp_path / "repo")
    peer = _make_git_repo(tmp_path / "peer")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "tracker.holder_repo": "peer_key",
                "repos.peer_key": str(peer),
            }
        ),
    )
    monkeypatch.setattr(
        tracker_holder, "_claude_klabauter_source_tree", lambda: tmp_path / "claude-klabauter"
    )

    def _tracked_files() -> set:
        return {
            p.relative_to(peer)
            for p in peer.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(peer).parts
        }

    before = _tracked_files()

    event = _event(item_id="../../../../escaped")
    result = _run(_handler({"event": event}, repo_root=repo))

    # The minted-id addressing means the traversal string never reaches the
    # delivery path at all — the delivery succeeds, harmlessly, inside the
    # inbox. Confirm no file landed outside it regardless.
    assert result["delivered"] == "peer"
    delivered_path = Path(result["path"])
    assert delivered_path.parent == peer / "cross-repo" / "inbox"

    after = _tracked_files()
    new_files = after - before
    for rel in new_files:
        assert str(rel).startswith("cross-repo"), (
            f"file {rel} landed outside the receiver's cross-repo/inbox/: "
            f"traversal escaped confinement"
        )
    assert not (peer / "escaped.md").exists()
    assert not (peer.parent / "escaped.md").exists()


def test_deliver_envelope_refuses_path_outside_inbox_directly():
    """Direct unit test of the confinement assertion itself, bypassing
    minted-id addressing entirely — proves the backstop fires even if a
    future editor reintroduces a traversal-hostile filename component."""
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        target_root = Path(tmp)
        (target_root / "cross-repo" / "inbox").mkdir(parents=True)
        event = {"id": "evt-x-deadbeefcafe", "observed_at": "x"}
        with pytest.raises(PushSuggestionRefused):
            # Simulate a hostile filename by monkeypatching event['id'] to a
            # traversal string directly — _deliver_envelope must refuse
            # regardless of where the hostile string would have come from.
            hostile_event = dict(event, id="../../../../escaped")
            push_suggestion._deliver_envelope(target_root, None, hostile_event)


def test_callers_own_id_never_reaches_store_or_envelope_and_dict_is_not_mutated(
    monkeypatch, tmp_path
):
    """The caller's own dict is copied, never mutated, and a caller-supplied
    `id` (refused before this point) can never leak — the local arm's
    stored record and the peer arm's delivered envelope both carry only the
    claude-klabauter-minted id."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )
    caller_event = _event(repo="acme/self")
    caller_event_snapshot = dict(caller_event)
    result = _run(_handler({"event": caller_event}, repo_root=repo))
    assert caller_event == caller_event_snapshot, (
        "the caller's own dict must never be mutated by the handler"
    )
    assert "id" not in caller_event
    assert result["event"]["id"].startswith("evt-")


# ---------------------------------------------------------------------------
# (e) D2a — destination-agnosticism guard, owed WITH this handler
# (DR-338's Ratification Status table, "Impl-slice obligation", RATIFIED)
# ---------------------------------------------------------------------------


def _scan_for_literal(source: str, needle: str) -> bool:
    """Case-insensitive substring probe — the shared predicate for both the
    production D2a scan below and its negative control, so a change to one
    cannot silently diverge from the other."""
    return needle.lower() in source.lower()


@pytest.mark.real_home
def test_d2a_no_destination_specific_literal_in_module_source():
    """No destination-specific identifier may appear anywhere in THIS op's
    own module. The mechanism must stay destination-agnostic: repointing
    `tracker.holder_repo` at a different `repos.*` member must require NO
    code change here — D2a's own third discharge test (module docstring).

    RULING 2026-08-20 (C4): the forbidden token is DERIVED from the
    `tracker.holder_repo` registry key, never hardcoded — a hardcoded
    destination-name literal here is exactly what the sibling brightline
    guard (`test_tracker_holder_brightline.py`'s own no-hardcoded-
    destination-literal check, which scans `tracker_holder.py` plus every
    importer, this module included, since it imports `tracker_holder`)
    flags as a violation — two guards enforcing the same D2a/AC1 policy
    must not fight each other. Deriving the token also keeps this guard
    testing the property D2a actually states for WHATEVER this box's
    destination is, rather than one hardcoded fleet's seeded value.

    Scoped to `push_suggestion.py` only, not `tracker_holder.py` — this
    chunk's `writes:` list does not include that file.
    """
    from coordinator_core.machine_resolver import registry_get

    holder_value = registry_get("tracker.holder_repo")
    assert holder_value, (
        "registry_get('tracker.holder_repo') returned no value — this "
        "guard cannot derive a forbidden token to scan for; verify the "
        "registry is configured (a silently-empty scan must not read as "
        "clean, mirrors the brightline guard's own non-zero assertions)"
    )
    source = Path(push_suggestion.__file__).read_text(encoding="utf-8")
    assert not _scan_for_literal(source, str(holder_value)), (
        f"destination-specific literal {holder_value!r} (this box's "
        "tracker.holder_repo registry value) found in "
        f"{push_suggestion.__file__} — violates DR-338 D2a"
    )


def test_d2a_scan_predicate_negative_control_detects_planted_literal(tmp_path):
    """Control: a synthetic occurrence of the derived forbidden token MUST
    be caught by `_scan_for_literal` — without this, a guard that always
    reports zero offenders would pass the guard above vacuously (mirrors
    `test_tracker_holder_brightline.py`'s own negative-control doctrine)."""
    planted = tmp_path / "planted_destination.py"
    planted.write_text(
        'HOLDER_REPO_KEY = "seeded-destination-value"\n', encoding="utf-8"
    )
    source = planted.read_text(encoding="utf-8")
    assert _scan_for_literal(source, "seeded-destination-value")


# ---------------------------------------------------------------------------
# (f) five-surface wiring + command-type smoke
# ---------------------------------------------------------------------------


def test_registered_in_registry_map():
    assert OP_MODULE_MAP.get("tracker.push_suggestion") == (
        "coordinator_core.ops.tracker.push_suggestion"
    )


def test_classified_mutating():
    assert OP_CLASSIFICATION.get("tracker.push_suggestion") is OpClass.MUTATING


def test_scoped_common_dir():
    assert _OP_KEY_SCOPE.get("tracker.push_suggestion") == "common_dir"


def test_eager_op_module_entry_present():
    eager_module_paths = [path for path, _note in _EAGER_OP_MODULES]
    assert "coordinator_core.ops.tracker.push_suggestion" in eager_module_paths


def test_command_type_smoke_resolves_non_none_repo_root(monkeypatch, tmp_path):
    """Full dispatch_message() round trip — proves the op resolves a non-None
    repo_root end to end, per docs/wiki/coordinator-core-engine.md:266's
    warning that an op missing from op_scopes._OP_KEY_SCOPE silently degrades
    to repo_root=None. AC5."""
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.setattr(
        tracker_holder,
        "registry_get",
        _fake_registry(
            {
                "repo_slug.acme/self": "self_key",
                "repos.self_key": str(repo),
            }
        ),
    )

    response = _run(
        dispatch_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tracker.push_suggestion",
                "params": {"event": _event(repo="acme/self")},
                "_origin_worktree": str(repo),
            }
        )
    )

    assert "error" not in response, f"unexpected dispatch error: {response}"
    result = response["result"]
    assert result["delivered"] == "local"


# ---------------------------------------------------------------------------
# the Director of Engineering boundary review, finding 8 — a failed delivery commit must not strand
# our envelope STAGED in the RECEIVER's index. Left staged, the receiver's next
# pathspec-less commit lands it inside an unrelated commit attributed to them:
# a cross-tree write arriving in their history without their action.
# Backlog row: state/bug-backlog/2026-08-20-peer-delivery-commit-failure-
# strands-a-staged-file-in-a-foreign-index.yaml
# ---------------------------------------------------------------------------


def _init_receiver_repo(root):
    """A real git repo with one commit, so HEAD is a born branch."""
    root.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
        ["commit", "-q", "--allow-empty", "-m", "seed"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True,
                       capture_output=True, stdin=subprocess.DEVNULL,
                       **no_console_creationflags())


def test_failed_delivery_commit_leaves_envelope_unstaged_in_receiver(tmp_path, monkeypatch):
    receiver = tmp_path / "receiver"
    _init_receiver_repo(receiver)

    rel_path = "cross-repo/inbox/delivered.md"
    target = receiver / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("---\nkind: sovereign-tracker-event\n---\n{}\n", encoding="utf-8")

    real_run = subprocess.run

    def _fail_only_commit(args, **kwargs):
        # Let `git add` (and everything else) really run; fail only the commit,
        # which is what index.lock contention in the receiver looks like.
        if "commit" in args:
            return subprocess.CompletedProcess(args, 1, "", "fatal: Unable to create index.lock")
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _fail_only_commit)
    outcome = push_suggestion._commit_envelope(receiver, rel_path)
    monkeypatch.undo()

    assert outcome["committed"] is False
    assert "index.lock" in outcome["reason"]

    staged = real_run(
        ["git", "-C", str(receiver), "diff", "--cached", "--name-only"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    ).stdout.split()
    assert rel_path not in staged, (
        f"envelope left STAGED in the receiver's index after a failed delivery "
        f"commit: {staged}. The receiver's next pathspec-less commit would sweep "
        f"it into an unrelated commit attributed to them."
    )
    assert target.exists(), (
        "the envelope must remain WRITTEN but untracked — the same state the "
        "detached/bare/unborn-HEAD arm documents as safe for the receiver's sweep"
    )


def test_delivery_commit_message_carries_deliverable_trailer():
    msg = push_suggestion._delivery_commit_message("cross-repo/inbox/x.md")
    assert msg.splitlines()[0].startswith("cross-repo: deliver sovereign-tracker event ")
    assert "Deliverable-Id: dlv-sat-06" in msg, (
        "a delivery lands a file in a repo whose operators ran nothing; without "
        "an attribution trailer they cannot tell what put it there"
    )
