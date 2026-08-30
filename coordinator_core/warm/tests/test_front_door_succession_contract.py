"""
coordinator_core/warm/tests/test_front_door_succession_contract.py

Pins the fixed-port SUCCESSION contract: what a non-door holder of `FIXED_PORT`
must serve for `probe_existing_holder` to classify it as an ordinary
`ElectionLost` rather than a `ForeignHolderError`.

WHY THIS FILE EXISTS. `ensure_front_door` had no production caller for a month
and that was read on both sides as a wiring omission. It was not. DoE's
`http_hook_forwarder` holds `FIXED_PORT` and always has -- the port's value was
taken FROM it -- and it served `POST /hook` only, answering `501` to the
`GET /health` probe. So a spawned door probed it as FOREIGN, and registering the
caller would have spawned one doomed door per session on a ~30-session box.

The resolution is that the marker asserts CONFORMANCE, not identity (see
`front_door`'s module docstring). That makes the exact bytes a holder must serve
load-bearing across a repo boundary: DoE builds the endpoint, claude-klabauter owns the
predicate, and neither can see the other's tests. This file is the shared
artifact -- the requirements table in
`docs/reference/hook-seam-warm-reach-contract.md` § The fixed-port succession,
executed rather than described.

NEGATIVE SPEC. This does NOT test any live socket, any real holder, or DoE's
forwarder -- it exercises `probe_existing_holder`'s injectable `opener` seam
only, so it stays a unit test with no port binding on a box where the real
`47623` is held. It does NOT assert the door's own server answers the probe
(that is C3's endpoint, tested with the server); it asserts what the PROBER
requires, which is the half a foreign implementer has to satisfy.
"""
from __future__ import annotations

import io
import json

import pytest

from coordinator_core.warm.front_door import (
    DOOR_PROTOCOL_VERSION,
    DOOR_PROTOCOL_VERSION_KEY,
    FIXED_PORT,
    door_health_payload,
    is_own_door_health_payload,
    probe_existing_holder,
)


class _Resp(io.BytesIO):
    """Minimal `urlopen`-shaped response: context manager, `.status`, `.read()`."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        super().__init__(body)
        self.status = status

    def __enter__(self):  # noqa: D105
        return self

    def __exit__(self, *exc) -> None:  # noqa: D105
        return None


def _opener(body, status: int = 200, *, raises: Exception | None = None):
    """Build an opener returning *body* (bytes, or an object JSON-encoded)."""
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")

    def open_url(url, timeout=None):  # noqa: ANN001, ARG001
        if raises is not None:
            raise raises
        return _Resp(body, status)

    return open_url


def test_the_conforming_holder_body_is_recognized():
    """The exact shape the contract table names: 2xx, UTF-8 JSON, the marker."""
    got = probe_existing_holder(
        FIXED_PORT, opener=_opener({DOOR_PROTOCOL_VERSION_KEY: DOOR_PROTOCOL_VERSION})
    )
    assert got is not None, (
        "a holder serving GET /health with the door_protocol_version marker must probe "
        "as ours -- an ordinary ElectionLost, never a ForeignHolderError"
    )
    assert got[DOOR_PROTOCOL_VERSION_KEY] == DOOR_PROTOCOL_VERSION


def test_a_holder_may_identify_itself_alongside_the_marker():
    """Extra keys are ignored, so a non-door holder can say what it is. Pinned
    because the contract invites it ("a holder may identify itself alongside the
    marker") and a future strict-schema check here would break DoE silently."""
    body = {
        DOOR_PROTOCOL_VERSION_KEY: DOOR_PROTOCOL_VERSION,
        "holder": "doe-http-hook-forwarder",
        "pid": 62944,
    }
    assert probe_existing_holder(FIXED_PORT, opener=_opener(body)) is not None


@pytest.mark.parametrize("version", [1, 2, 99])
def test_any_integer_version_is_recognized(version: int):
    """A bumped successor must still be recognized, never misread as foreign.
    This is why the contract says do NOT bump the version to force a
    re-election -- the bump is not a fleet restart lever."""
    got = probe_existing_holder(
        FIXED_PORT, opener=_opener({DOOR_PROTOCOL_VERSION_KEY: version})
    )
    assert got is not None, f"version {version} must be recognized"


def test_the_shipped_payload_helper_satisfies_the_contract():
    """`door_health_payload()` is what our own endpoint publishes; if it ever
    stopped satisfying the predicate a foreign implementer would be held to a
    shape our own door does not meet."""
    assert is_own_door_health_payload(door_health_payload())


@pytest.mark.parametrize(
    "name,kwargs",
    [
        # The exact failure DoE's forwarder produced before this contract was
        # named: POST-only, no /health, 501 to GET.
        ("501_unsupported_method", {"body": b"Unsupported method ('GET')", "status": 501}),
        ("404_no_health_route", {"body": b"", "status": 404}),
        ("2xx_but_no_marker", {"body": {"ok": True}}),
        ("2xx_but_marker_not_an_int", {"body": {DOOR_PROTOCOL_VERSION_KEY: "1"}}),
        ("2xx_but_body_not_an_object", {"body": [1]}),
        ("2xx_but_malformed_json", {"body": b"{not json"}),
        ("2xx_but_not_utf8", {"body": b"\xff\xfe\x00"}),
    ],
)
def test_a_non_conforming_holder_is_not_recognized(name: str, kwargs: dict):
    """Every one of these is a ForeignHolderError upstream, and each must stay
    that way: a defer to a process that is not serving the transport is the
    silent misroute the whole discrimination exists to prevent."""
    assert probe_existing_holder(FIXED_PORT, opener=_opener(**kwargs)) is None, (
        f"{name} must NOT be recognized as a conforming holder"
    )


def test_an_unreachable_or_hung_holder_is_not_recognized():
    """Connection refused, timeout, or any other raise -- the probe never raises
    and never recognizes."""
    assert (
        probe_existing_holder(FIXED_PORT, opener=_opener(b"", raises=OSError("refused")))
        is None
    )


def test_the_probe_targets_the_health_path_on_the_given_port():
    """The URL is half the contract DoE builds against -- pin it, so a change
    here cannot silently strand a conforming holder on the old route."""
    from coordinator_core.warm.front_door import bind_host
    from coordinator_core.warm.supervisor import HEALTH_PATH

    seen = {}

    def open_url(url, timeout=None):  # noqa: ANN001
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp(json.dumps(door_health_payload()).encode("utf-8"))

    probe_existing_holder(FIXED_PORT, opener=open_url)
    assert seen["url"] == f"http://{bind_host()}:{FIXED_PORT}{HEALTH_PATH}"
    assert seen["timeout"] is not None, "the probe must bound its wait"
