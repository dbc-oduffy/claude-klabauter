"""Token-gated HTTP transport for POSTing the assembled publish document to
Cockpit's ``POST /api/emissions/publish`` endpoint.

Fails loud by design -- the deliberate INVERSE of
``warm.supervisor._probe_health``'s never-raise liveness-probe posture (that
function swallows every exception because a health check must never raise;
this module raises on every one of the same failure modes). A publish that
quietly no-ops is worse than one that never ran: the sink would go on serving
a stale document behind a fresh-looking ``published_at`` from the previous
run. Connection refusal, a timeout, a non-2xx status, and an absent token
each fail loud and non-zero.

Follows the same injectable-``opener`` convention ``_probe_health`` uses
(itself mirroring ``warm.client._open_pipe``'s isolated-transport seam):
``opener`` is a ``urllib.request.urlopen``-shaped callable, defaulting to a
no-redirect opener, so this module is testable with no network and no
endpoint -- cockpit's side of ``/api/emissions/publish`` does not exist yet.

Negative-spec: no retry, no exception-swallowing anywhere in this module, and
the bearer token never appears in a raised exception's message or in any log
output (AC7) -- error text names the failure mode (a status code,
"connection refused"-shaped text, "timed out", "no publish token
configured"), never the credential's value.

`base_url`'s scheme is validated (https required, plain http permitted only
to a loopback host) and 3xx responses are never followed -- both checked
before the token is read, closing off the two ways the bearer token could
otherwise leave this module unencrypted or land on an unintended host.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from coordinator_core.machine_resolver import registry_get

PUBLISH_PATH = "/api/emissions/publish"
PUBLISH_TIMEOUT_SECS = 10.0

TOKEN_ENV_VAR = "COCKPIT_EMISSION_PUBLISH_TOKEN"
TOKEN_REGISTRY_KEY = "cockpit_emission_publish_token"

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class PublishTransportError(RuntimeError):
    """Raised for any publish-transport failure: missing token, connection
    refused, timeout, or a non-2xx response.

    Never carries the token's value in its message (AC7) -- only the failure
    mode (status code, reason text, or a fixed "no token configured"
    sentence), regardless of which branch below raises it.
    """


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow any 3xx -- the bearer token must never be resent to
    a redirect target (a possibly cross-host leak). ``redirect_request``
    returning ``None`` signals "not handled", which surfaces the 3xx as an
    `urllib.error.HTTPError` through the normal error path below instead of
    silently chasing it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_DEFAULT_OPENER: Any = None


def _default_opener() -> Any:
    """Build the no-redirect opener on first use, never at import.

    ``build_opener`` costs 11.75ms of process time (measured amortised over
    2000 calls; a single sample reads as one 15.6ms clock tick and is noise).
    This module is imported eagerly via ``ops/__init__.py::_EAGER_OP_MODULES``,
    so at module scope that is ~24% of the <50ms warm-engine reach budget paid
    at every engine start, for a transport that fires twice a day at close
    cadence. Negative-spec: do not hoist this back to module scope. DR-344.
    """
    global _DEFAULT_OPENER
    if _DEFAULT_OPENER is None:
        _DEFAULT_OPENER = urllib.request.build_opener(_NoRedirectHandler).open
    return _DEFAULT_OPENER


def _validate_scheme(base_url: str) -> None:
    """Require `https://`; `http://` is permitted only to a loopback host
    (`localhost`, `127.0.0.1`, `::1`) so local-dev sinks still work. Anything
    else -- a non-loopback `http://`, or a non-HTTP scheme -- fails loud
    before the token is even read (AC7: the token is never at risk of an
    unencrypted send). Names the offending scheme, never the token.
    """
    parsed = urlsplit(base_url)
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS:
        return
    raise PublishTransportError(
        f"publish failed: refusing scheme {scheme!r} (require https, or http to loopback only)"
    )


def _resolve_token() -> str:
    """Environment is primary (`COCKPIT_EMISSION_PUBLISH_TOKEN`); fallback is
    the machine-local registry, key `cockpit_emission_publish_token`, read
    through `registry_get` -- the existing generic dotted-key registry
    reader, not a hand-rolled lookup. `registry_get` has no namespacing
    restriction on the keys it resolves, so no dedicated "secrets namespace"
    is needed to hold this key; absence of the key itself (not absence of
    some namespace concept) is what triggers the fail-loud branch below.

    Absent token is a refusal, not a skip: raises rather than returning
    `None` or an empty string.
    """
    token = os.environ.get(TOKEN_ENV_VAR)
    if token:
        return token
    token = registry_get(TOKEN_REGISTRY_KEY)
    if token:
        return token
    raise PublishTransportError(
        f"no publish token configured: set {TOKEN_ENV_VAR} or the "
        f"machine-local registry key {TOKEN_REGISTRY_KEY!r}"
    )


def publish_document(
    base_url: str,
    document: bytes,
    *,
    timeout: float = PUBLISH_TIMEOUT_SECS,
    opener: Any = None,
) -> None:
    """POST `document` (already-assembled JSON bytes -- see
    `publish_envelope.py`, C2) to `base_url.rstrip('/') + PUBLISH_PATH` with
    a bearer token.

    Fails loud (raises `PublishTransportError`) on: an absent token,
    connection refusal, a timeout, or a non-2xx response. Never returns a
    silent no-op on any of those -- see module docstring for why that matters
    more here than it does for a liveness probe. Returns `None` on any 2xx.

    `opener` is an injectable `urllib.request.urlopen`-shaped callable for
    tests, defaulting to a no-redirect opener -- mirrors
    `warm.supervisor._probe_health`'s isolated-transport-seam convention,
    inverted only in failure posture (see module docstring).

    `base_url`'s scheme is validated -- and any redirect response rejected --
    before the token is read, so a misconfigured plaintext or cross-host
    sink can never see the bearer token (see `_validate_scheme` and
    `_NoRedirectHandler`).
    """
    _validate_scheme(base_url)
    token = _resolve_token()

    open_url = opener if opener is not None else _default_opener()
    url = base_url.rstrip("/") + PUBLISH_PATH
    request = urllib.request.Request(
        url,
        data=document,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )

    try:
        with open_url(request, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            status = int(status)
    except urllib.error.HTTPError as exc:
        raise PublishTransportError(
            f"publish failed: cockpit returned HTTP {exc.code}"
        ) from None
    except TimeoutError:
        raise PublishTransportError("publish failed: request timed out") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise PublishTransportError(
                "publish failed: request timed out"
            ) from None
        raise PublishTransportError(
            "publish failed: could not reach cockpit (connection error)"
        ) from None
    except OSError:
        raise PublishTransportError(
            "publish failed: could not reach cockpit (connection error)"
        ) from None
    except PublishTransportError:
        raise
    except Exception as exc:  # noqa: BLE001 -- convert every other failure to a loud, token-free error
        raise PublishTransportError(
            f"publish failed: unexpected {type(exc).__name__}"
        ) from None

    if not (200 <= status < 300):
        raise PublishTransportError(f"publish failed: cockpit returned HTTP {status}")
