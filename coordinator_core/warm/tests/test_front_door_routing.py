"""coordinator_core.warm.tests.test_front_door_routing

Spec backlink: docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md
§ C4 (AC5, AC9).

AC5 -- the identity seam resolves through a single implementation point, and
the routing decision is exercised with TWO live discovery records present at
once (a router that has only ever seen one clone has not been tested). AC9 --
the routing-table cache is written through the SAME `locked_write.
replace_with_retry` object every other atomic-swap site in this package uses.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core import locked_write
from coordinator_core.warm import front_door_routing as routing
from coordinator_core.warm import supervisor
from coordinator_core.warm.skew import write_engine_stamp


def _stamped_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    (root / "coordinator_core").mkdir(parents=True)
    write_engine_stamp(root, "sha-%s" % name)
    return root


def _publish_live_discovery(root: Path, *, port: int) -> None:
    supervisor.write_discovery(
        port=port,
        pid=os.getpid(),
        stable_pid_start_epoch=supervisor._self_stable_pid_start_epoch() or 0,
        engine_sha="sha-%d" % port,
        engine_root=root,
    )


class TestDefaultExtractor:
    def test_reads_the_named_header(self):
        headers = {routing.CLONE_IDENTITY_HEADER: "/some/clone"}
        assert routing.clone_identity_from_headers(headers) == "/some/clone"

    def test_is_case_insensitive(self):
        headers = {routing.CLONE_IDENTITY_HEADER.lower(): "/some/clone"}
        assert routing.clone_identity_from_headers(headers) == "/some/clone"

    def test_absent_header_is_none(self):
        assert routing.clone_identity_from_headers({}) is None
        assert routing.clone_identity_from_headers(None) is None

    def test_empty_value_is_none(self):
        headers = {routing.CLONE_IDENTITY_HEADER: ""}
        assert routing.clone_identity_from_headers(headers) is None


class TestResolveRouteUnroutableStates:
    def test_key_absent_when_no_identity_header(self):
        res = routing.resolve_route({})
        assert res.state == routing.KEY_ABSENT
        assert res.identity is None

    def test_root_unresolvable_for_a_path_that_does_not_exist(self, tmp_path: Path):
        missing = str(tmp_path / "never-existed")
        headers = {routing.CLONE_IDENTITY_HEADER: missing}
        res = routing.resolve_route(headers)
        assert res.state == routing.ROOT_UNRESOLVABLE
        assert res.identity == missing

    def test_root_unstamped_for_an_existing_but_unstamped_directory(self, tmp_path: Path):
        bare = tmp_path / "bare-clone"
        (bare / "coordinator_core").mkdir(parents=True)
        headers = {routing.CLONE_IDENTITY_HEADER: str(bare)}
        res = routing.resolve_route(headers)
        assert res.state == routing.ROOT_UNSTAMPED
        assert res.identity == str(bare)
        assert res.engine_root == bare

    def test_no_listener_when_stamped_but_no_discovery_record(self, tmp_path: Path):
        root = _stamped_root(tmp_path, "clone-no-listener")
        headers = {routing.CLONE_IDENTITY_HEADER: str(root)}
        res = routing.resolve_route(headers)
        assert res.state == routing.NO_LISTENER
        assert res.engine_root == root

    def test_no_listener_when_the_discovery_record_names_a_dead_pid(self, tmp_path: Path):
        root = _stamped_root(tmp_path, "clone-dead-record")
        supervisor.write_discovery(
            port=9999,
            pid=1,  # essentially never our test process, and no stored epoch match
            stable_pid_start_epoch=1,
            engine_sha="stale",
            engine_root=root,
        )
        headers = {routing.CLONE_IDENTITY_HEADER: str(root)}
        res = routing.resolve_route(headers)
        assert res.state == routing.NO_LISTENER


class TestResolveRouteTwoLiveClones:
    """AC5: a router that has only ever seen one clone has not been tested."""

    def test_each_identity_routes_to_its_own_clones_listener(self, tmp_path: Path):
        clone_a = _stamped_root(tmp_path, "clone-a")
        clone_b = _stamped_root(tmp_path, "clone-b")
        _publish_live_discovery(clone_a, port=41001)
        _publish_live_discovery(clone_b, port=41002)

        res_a = routing.resolve_route({routing.CLONE_IDENTITY_HEADER: str(clone_a)})
        res_b = routing.resolve_route({routing.CLONE_IDENTITY_HEADER: str(clone_b)})

        assert res_a.state == routing.ROUTED
        assert res_b.state == routing.ROUTED
        assert res_a.url == "http://127.0.0.1:41001"
        assert res_b.url == "http://127.0.0.1:41002"
        assert res_a.url != res_b.url, "clone A's fire must never resolve to clone B's listener"

    def test_a_fire_carrying_clone_as_identity_never_reaches_clone_bs_engine(self, tmp_path: Path):
        clone_a = _stamped_root(tmp_path, "clone-a2")
        clone_b = _stamped_root(tmp_path, "clone-b2")
        _publish_live_discovery(clone_a, port=41011)
        _publish_live_discovery(clone_b, port=41012)

        res = routing.resolve_route({routing.CLONE_IDENTITY_HEADER: str(clone_a)})
        assert res.url == "http://127.0.0.1:41011"
        assert res.engine_root == clone_a
        assert res.engine_root != clone_b


class TestExtractorSubstitution:
    """AC5: swapping the identity source is a one-site change -- passing a
    different callable, never touching resolve_route's own body."""

    def test_a_substituted_extractor_drives_the_identical_routing_path(self, tmp_path: Path):
        clone = _stamped_root(tmp_path, "clone-substituted")
        _publish_live_discovery(clone, port=41021)

        def fixed_identity(_headers):
            return str(clone)

        res = routing.resolve_route({}, extractor=fixed_identity)
        assert res.state == routing.ROUTED
        assert res.url == "http://127.0.0.1:41021"

    def test_default_extractor_is_the_module_level_default_argument(self):
        import inspect

        sig = inspect.signature(routing.resolve_route)
        assert sig.parameters["extractor"].default is routing.clone_identity_from_headers


class TestRoutingTableCache:
    """AC9."""

    def test_reuses_the_shared_atomic_replace_primitive(self):
        assert routing._replace_with_retry is locked_write.replace_with_retry

    def test_a_successful_resolution_records_the_route(self, tmp_path: Path):
        clone = _stamped_root(tmp_path, "clone-cache")
        _publish_live_discovery(clone, port=41031)
        front_door_root = tmp_path / "front-door-own-clone"

        headers = {routing.CLONE_IDENTITY_HEADER: str(clone)}
        res = routing.resolve_route(headers, front_door_root=front_door_root)
        assert res.state == routing.ROUTED

        table = routing._read_routing_table(front_door_root)
        assert str(clone) in table
        assert table[str(clone)]["engine_root"] == str(clone)

    def test_an_unroutable_fire_is_never_recorded(self, tmp_path: Path):
        front_door_root = tmp_path / "front-door-own-clone-2"
        missing = str(tmp_path / "never-existed-2")
        routing.resolve_route(
            {routing.CLONE_IDENTITY_HEADER: missing}, front_door_root=front_door_root
        )
        table = routing._read_routing_table(front_door_root)
        assert missing not in table

    def test_write_falls_back_in_place_when_the_replace_is_exhausted(
        self, tmp_path: Path, monkeypatch
    ):
        front_door_root = tmp_path / "front-door-own-clone-3"
        monkeypatch.setattr(routing, "_replace_with_retry", lambda *_a, **_kw: False)
        routing._write_routing_table({"x": {"engine_root": "y"}}, front_door_root)

        path = routing.routing_table_path(front_door_root)
        assert json.loads(path.read_text(encoding="utf-8")) == {"x": {"engine_root": "y"}}

    def test_no_temp_file_is_left_behind(self, tmp_path: Path):
        front_door_root = tmp_path / "front-door-own-clone-4"
        routing._write_routing_table({"a": {"engine_root": "b"}}, front_door_root)
        parent = routing.routing_table_path(front_door_root).parent
        leftovers = [p.name for p in parent.iterdir() if p.name.startswith(".routing-table-")]
        assert leftovers == []

    def test_a_cache_write_failure_never_raises_into_the_caller(
        self, tmp_path: Path, monkeypatch
    ):
        front_door_root = tmp_path / "front-door-own-clone-5"

        def boom(*_a, **_kw):
            raise OSError("disk full")

        monkeypatch.setattr(routing.os, "fdopen", boom)
        # Must not raise -- the cache is advisory (module docstring, AC9 section).
        routing._write_routing_table({"a": {"engine_root": "b"}}, front_door_root)


class TestUnroutableResponse:
    """C5 -- the two unroutable states, reported distinctly (AC6/AC14/AC15)."""

    def test_routed_composes_no_response(self):
        res = routing.RouteResolution(state=routing.ROUTED, url="http://127.0.0.1:9")
        assert routing.unroutable_response(res, "PreToolUse") is None

    def test_key_absent_detail(self):
        res = routing.RouteResolution(state=routing.KEY_ABSENT)
        body = routing.unroutable_response(res, "PreToolUse")
        assert body["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert "no clone identity" in body["systemMessage"]

    def test_root_unresolvable_detail_names_the_identity(self):
        res = routing.RouteResolution(state=routing.ROOT_UNRESOLVABLE, identity="/nope")
        body = routing.unroutable_response(res, "PreToolUse")
        assert "/nope" in body["systemMessage"]

    def test_root_unstamped_detail_names_the_root(self, tmp_path: Path):
        res = routing.RouteResolution(state=routing.ROOT_UNSTAMPED, identity=str(tmp_path), engine_root=tmp_path)
        body = routing.unroutable_response(res, "PreToolUse")
        assert str(tmp_path) in body["systemMessage"]
        assert "no valid engine stamp" in body["systemMessage"]

    def test_no_listener_detail_names_the_root(self, tmp_path: Path):
        res = routing.RouteResolution(state=routing.NO_LISTENER, identity=str(tmp_path), engine_root=tmp_path)
        body = routing.unroutable_response(res, "PreToolUse")
        assert str(tmp_path) in body["systemMessage"]
        assert "no live listener" in body["systemMessage"]

    def test_the_four_unroutable_states_produce_four_distinct_details(self, tmp_path: Path):
        states = [
            routing.RouteResolution(state=routing.KEY_ABSENT),
            routing.RouteResolution(state=routing.ROOT_UNRESOLVABLE, identity="/x"),
            routing.RouteResolution(state=routing.ROOT_UNSTAMPED, identity="/x", engine_root=tmp_path),
            routing.RouteResolution(state=routing.NO_LISTENER, identity="/x", engine_root=tmp_path),
        ]
        messages = {routing.unroutable_response(res, "PreToolUse")["systemMessage"] for res in states}
        assert len(messages) == 4

    def test_never_a_third_vocabulary_it_is_unreachable_response_shaped(self):
        res = routing.RouteResolution(state=routing.NO_LISTENER, identity="/x")
        body = routing.unroutable_response(res, "PreToolUse")
        assert body == routing.hook_http.unreachable_response(
            "PreToolUse", "clone root None has no live listener"
        )


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self._status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self._status

    def read(self):
        return self._body


class TestForwardRequest:
    def test_a_non_routed_resolution_is_never_dialled(self):
        calls = {"n": 0}

        def opener(*_a, **_kw):
            calls["n"] += 1
            return _FakeResponse(200, b"{}")

        res = routing.RouteResolution(state=routing.NO_LISTENER)
        out = routing.forward_request(res, "/hook", b"{}", opener=opener)
        assert out is None
        assert calls["n"] == 0

    def test_a_successful_forward_returns_the_response_body(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.headers)
            return _FakeResponse(200, b'{"ok":true}')

        res = routing.RouteResolution(state=routing.ROUTED, url="http://127.0.0.1:41041")
        out = routing.forward_request(res, "/hook", b'{"a":1}', engine_token="tok", opener=opener)
        assert out == b'{"ok":true}'
        assert seen["url"] == "http://127.0.0.1:41041/hook"
        # urllib.Request title-cases header names it is given.
        assert seen["headers"].get(routing.ENGINE_TOKEN_HEADER.title()) == "tok" or any(
            v == "tok" for v in seen["headers"].values()
        )

    def test_a_non_2xx_status_is_reported_as_unreachable(self):
        def opener(*_a, **_kw):
            return _FakeResponse(500, b"boom")

        res = routing.RouteResolution(state=routing.ROUTED, url="http://127.0.0.1:41042")
        out = routing.forward_request(res, "/hook", b"{}", opener=opener)
        assert out is None

    def test_any_exception_from_the_opener_never_raises_out(self):
        def opener(*_a, **_kw):
            raise TimeoutError("no response")

        res = routing.RouteResolution(state=routing.ROUTED, url="http://127.0.0.1:41043")
        out = routing.forward_request(res, "/hook", b"{}", opener=opener)
        assert out is None
