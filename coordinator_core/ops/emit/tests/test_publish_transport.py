"""Failure-matrix tests for `publish_transport.publish_document` -- an
opener-injected, no-network exercise of the fail-loud posture pinned in C3:
200 (success), 4xx, 5xx, timeout, connection-refused, and missing token all
covered, plus a token-non-leak assertion on every failure path that could
plausibly interpolate it.
"""

from __future__ import annotations

import io
import socket
import urllib.error

import pytest

from coordinator_core.ops.emit import publish_transport as pt

FAKE_TOKEN = "sekret-token-value-12345"
DOC = b'{"schema_version": "v2", "repo_slug": "x/y"}'


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


def _opener_returning(status: int):
    def _opener(request, timeout=None):
        return _FakeResponse(status)

    return _opener


def _opener_raising(exc: BaseException):
    def _opener(request, timeout=None):
        raise exc

    return _opener


@pytest.fixture(autouse=True)
def _env_token(monkeypatch):
    monkeypatch.setenv(pt.TOKEN_ENV_VAR, FAKE_TOKEN)
    yield


def test_200_succeeds(monkeypatch):
    pt.publish_document("https://cockpit.example", DOC, opener=_opener_returning(200))


def test_4xx_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="HTTP 403"):
        pt.publish_document(
            "https://cockpit.example",
            DOC,
            opener=_opener_raising(
                urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO())
            ),
        )


def test_5xx_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="HTTP 500"):
        pt.publish_document(
            "https://cockpit.example",
            DOC,
            opener=_opener_raising(
                urllib.error.HTTPError("url", 500, "Boom", {}, io.BytesIO())
            ),
        )


def test_non_2xx_status_object_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="HTTP 404"):
        pt.publish_document(
            "https://cockpit.example", DOC, opener=_opener_returning(404)
        )


def test_timeout_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="timed out"):
        pt.publish_document(
            "https://cockpit.example",
            DOC,
            opener=_opener_raising(TimeoutError("timed out")),
        )


def test_urlerror_timeout_reason_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="timed out"):
        pt.publish_document(
            "https://cockpit.example",
            DOC,
            opener=_opener_raising(
                urllib.error.URLError(socket.timeout("timed out"))
            ),
        )


def test_connection_refused_raises_loud():
    with pytest.raises(pt.PublishTransportError, match="connection error"):
        pt.publish_document(
            "https://cockpit.example",
            DOC,
            opener=_opener_raising(
                urllib.error.URLError(ConnectionRefusedError("refused"))
            ),
        )


def test_missing_token_is_a_refusal_not_a_skip(monkeypatch):
    monkeypatch.delenv(pt.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(pt, "registry_get", lambda key: None)
    with pytest.raises(pt.PublishTransportError, match="no publish token configured"):
        pt.publish_document(
            "https://cockpit.example", DOC, opener=_opener_returning(200)
        )


def test_missing_token_falls_back_to_registry(monkeypatch):
    monkeypatch.delenv(pt.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(pt, "registry_get", lambda key: FAKE_TOKEN if key == pt.TOKEN_REGISTRY_KEY else None)
    pt.publish_document("https://cockpit.example", DOC, opener=_opener_returning(200))


@pytest.mark.parametrize(
    "opener_factory",
    [
        lambda: _opener_raising(
            urllib.error.HTTPError("url", 403, "Forbidden", {}, io.BytesIO())
        ),
        lambda: _opener_raising(
            urllib.error.HTTPError("url", 500, "Boom", {}, io.BytesIO())
        ),
        lambda: _opener_raising(
            urllib.error.URLError(ConnectionRefusedError("refused"))
        ),
    ],
)
def test_token_never_appears_in_exception_text(opener_factory):
    with pytest.raises(pt.PublishTransportError) as excinfo:
        pt.publish_document("https://cockpit.example", DOC, opener=opener_factory())
    assert FAKE_TOKEN not in str(excinfo.value)


def test_token_never_appears_in_missing_token_exception_text(monkeypatch):
    monkeypatch.delenv(pt.TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(pt, "registry_get", lambda key: None)
    with pytest.raises(pt.PublishTransportError) as excinfo:
        pt.publish_document(
            "https://cockpit.example", DOC, opener=_opener_returning(200)
        )
    assert FAKE_TOKEN not in str(excinfo.value)


def test_url_and_authorization_header_shape(monkeypatch):
    captured = {}

    def _opener(request, timeout=None):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["method"] = request.get_method()
        return _FakeResponse(200)

    pt.publish_document("https://cockpit.example/", DOC, opener=_opener)
    assert captured["url"] == "https://cockpit.example" + pt.PUBLISH_PATH
    assert captured["auth"] == f"Bearer {FAKE_TOKEN}"
    assert captured["method"] == "POST"
