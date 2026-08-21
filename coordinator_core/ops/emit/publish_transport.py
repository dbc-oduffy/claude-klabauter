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
``opener`` is a ``urllib.request.urlopen``-shaped callable, defaulting to the
real one, so this module is testable with no network and no endpoint --
Cockpit's side of ``/api/emissions/publish`` does not exist yet.

Negative-spec: no retry, no exception-swallowing anywhere in this module, and
the bearer token never appears in a raised exception's message or in any log
output (AC7) -- error text names the failure mode (a status code,
"connection refused"-shaped text, "timed out", "no publish token
configured"), never the credential's value.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from typing import Any

from coordinator_core.machine_resolver import registry_get

PUBLISH_PATH = "/api/emissions/publish"
PUBLISH_TIMEOUT_SECS = 10.0

TOKEN_ENV_VAR = "COCKPIT_EMISSION_PUBLISH_TOKEN"
TOKEN_REGISTRY_KEY = "cockpit_emission_publish_token"


class PublishTransportError(RuntimeError):
    """Raised for any publish-transport failure: missing token, connection
    refused, timeout, or a non-2xx response.

    Never carries the token's value in its message (AC7) -- only the failure
    mode (status code, reason text, or a fixed "no token configured"
    sentence), regardless of which branch below raises it.
    """


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
    tests, defaulting to the real one -- mirrors
    `warm.supervisor._probe_health`'s isolated-transport-seam convention,
    inverted only in failure posture (see module docstring).
    """
    token = _resolve_token()

    open_url = opener if opener is not None else urllib.request.urlopen
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
