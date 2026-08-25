"""coordinator_core.warm.front_door_routing -- clone-identity seam, resolution to
an engine root, and forwarding to that clone's own listener.

Spec backlink: docs/plans/2026-08-25-the-bash-guard-stops-paying-for-a-process.md
§ C4 (AC5, AC9). `docs/reference/hook-seam-warm-reach-contract.md` § "The front
door -- a fixed-port routing multiplexer, not a clone-bound listener" is the
contract this module builds to; do not restate its reasoning here, cite it.

WHAT THIS MODULE OWNS -- the routing decision for one fire against the shared,
fixed-port front door (`front_door.py`): given the request's headers, learn
WHICH clone this fire belongs to, resolve that identity to a real engine root,
and reach that root's already-running per-clone listener
(`supervisor.read_discovery`). Nothing here binds a socket, elects anything, or
spawns a process -- that is `front_door.py`'s job (C2/C3); this module is pure
routing logic plus the one network hop to the resolved listener.

THE IDENTITY SEAM (AC5), NOT A HARD-CODED KEY. DoE's own C1 has not chosen the
header/env-var name that carries clone identity, and the one candidate this
plan measured concretely (a header-carried path placeholder,
`${CLAUDE_PLUGIN_ROOT}`) is already measured DEAD -- it does not expand over
this transport (`docs/research/spike-verdicts/
2026-08-25-http-hook-headers-expand-env-vars-but-not-path-placeholders.md`).
A key threaded through the routing path is a rewrite the moment DoE's C1
lands; a seam is a one-site change. `resolve_route` therefore takes an
`extractor` parameter (`CloneIdentityExtractor`) rather than reading a fixed
header name inline, and every call site (including `main`'s eventual wiring
in `front_door.py`) is expected to pass the module-level default unless a
test is substituting a double -- swapping the identity source is passing a
different callable, never editing this module's resolution body.

THE NAMED DEFAULT (F7). `COORDINATOR_CLONE_ROOT` -- a non-`CLAUDE_`-prefixed
env var, measured to expand over the header channel (the spike verdict cited
above, finding 2) -- is shipped as the ONE default implementation,
`clone_identity_from_headers`, explicitly labelled provisional-pending-DoE-C1
both here and in the contract doc. This is a concrete default the
substitution test (AC5) and any real fire exercise, not merely a decision:
"provisional" describes its STATUS, not its presence.

RESOLUTION PATH, AND WHY EACH STEP IS DISTINCT (feeds C5's reporting, not
built here). `COORDINATOR_CLONE_ROOT`'s value is an engine clone ROOT PATH,
not an opaque token -- exactly as `${CLAUDE_PLUGIN_ROOT}` would have named a
directory before it was found dead. Four outcomes, each a materially
different fact:
  - no identity on the request at all (`key-absent`, AC6/AC17's own class);
  - the path names nothing this box can resolve (deleted clone, typo, a path
    that never existed here);
  - the path exists but carries no valid engine stamp
    (`engine_root.is_engine_root`, DR-315 discipline -- present but not a
    real build);
  - the path is a stamped, resolvable engine root, but nothing currently
    vouches for a live listener there (`supervisor.read_discovery` /
    `discovery_is_live`) -- the ordinary post-idle-demotion state, not an
    error.
`resolve_route` returns a `RouteResolution` whose `state` distinguishes all
four PLUS the success case. `unroutable_response` (C5, `docs/plans/...md`
§ C5, "the two unroutable states, reported distinctly") composes each
unroutable outcome with `hook_http.unreachable_response` into the wire-shape
response -- `resolve_route` produces the discriminated fact, `unroutable_
response` is what turns it into what `front_door.py` actually answers with.

FORWARDING NEVER MINTS OR RECOMPUTES A TOKEN (partial AC17 scope -- full
authentication-before-forward is a later chunk's job, not reproduced here).
`forward_request` proxies the caller's own body and its own engine-token
header verbatim to the resolved clone's listener; it does not compute
`skew.compute_client_token` on the caller's behalf, which would make the
fixed, public door a way to mint a token for any local process.

THE ROUTING TABLE (AC9). A resolved `identity -> engine root` mapping is
recorded to a small, best-effort, per-machine cache
(`ROUTING_TABLE_FILENAME`, sitting in the SAME per-clone/per-user `svc_dir()`
`front_door.py` and `supervisor.py` already publish discovery records into)
on every SUCCESSFUL resolution -- a multiplexer that forgets every route the
moment its process restarts fails every fire until the next boot revalidates
each one from scratch, which is strictly worse than the microsecond-scale
torn-read window `write_discovery`'s own docstring accepts as a tradeoff
elsewhere in this package. The write reuses `locked_write.replace_with_retry`
(`10c465a14`) with the SAME fall-back-on-exhaustion policy `front_door.
write_discovery` and `supervisor.write_discovery` already use -- bound to the
identical object (`_replace_with_retry = locked_write.replace_with_retry`),
never a third hand-rolled copy of the retry-then-fall-back-in-place shape.
The table is a CACHE, not a source of truth: `resolve_route` always
re-validates the stamp and the live discovery record on every call, so a
stale or corrupt table entry can only ever cost a wasted lookup, never route
a fire to a clone that is no longer valid.

NEGATIVE SPEC:
  - No socket binding, no election, no process spawn -- `front_door.py`'s job.
  - No third reporting vocabulary for the unroutable states -- `unroutable_
    response` composes with `hook_http.unreachable_response`'s EXISTING loud
    did-not-run shape rather than inventing one (AC15).
  - No inferring an op from the event for forwarding -- routing stays
    per-registration via the URL/path a caller passes to `forward_request`
    (AC14).
  - No token minting on the caller's behalf -- see FORWARDING section above.
  - No second `replace_with_retry`-shaped retry loop -- reuse the one object.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from coordinator_core import locked_write
from coordinator_core.warm import breadcrumb, hook_http, supervisor
from coordinator_core.warm.engine_root import resolve_engine_root
from coordinator_core.warm.http_listener import ENGINE_TOKEN_HEADER

__all__ = [
    "CLONE_IDENTITY_HEADER",
    "CloneIdentityExtractor",
    "clone_identity_from_headers",
    "KEY_ABSENT",
    "ROOT_UNRESOLVABLE",
    "ROOT_UNSTAMPED",
    "NO_LISTENER",
    "ROUTED",
    "RouteResolution",
    "resolve_route",
    "unroutable_response",
    "ROUTING_TABLE_FILENAME",
    "routing_table_path",
    "PROXY_TIMEOUT_SECS",
    "forward_request",
]

#: The default header a fire's clone identity travels on (F7, provisional
#: pending DoE's own C1 -- see module docstring). Named after the env var it
#: carries (`COORDINATOR_CLONE_ROOT`), matching this package's existing
#: `X-Coordinator-*` header-naming convention (`http_listener.
#: ENGINE_TOKEN_HEADER`).
CLONE_IDENTITY_HEADER = "X-Coordinator-Clone-Root"

#: A callable that reads clone identity off a fire's headers, or returns
#: `None` when absent -- the whole of AC5's "single implementation point"
#: seam. `resolve_route` takes one of these rather than a fixed header name.
CloneIdentityExtractor = Callable[[Mapping[str, str]], Optional[str]]


def clone_identity_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    """The one named default extractor (F7): `CLONE_IDENTITY_HEADER`, read
    case-insensitively (HTTP header names are case-insensitive on the wire;
    `http.client.HTTPMessage` already folds this, but a plain `dict` -- what
    every test here constructs -- does not, so the fold is done explicitly
    rather than assumed from the caller's container type).

    Returns `None` for a missing header AND for a present-but-empty value --
    an empty string is indistinguishable from "not exported" on this
    transport (the spike verdict's own finding on path placeholders), and
    treating it as a real identity would route a fire nobody could name a
    real clone for.
    """
    if not headers:
        return None
    target = CLONE_IDENTITY_HEADER.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value if isinstance(value, str) and value else None
    return None


#: `RouteResolution.state` values -- see module docstring's "RESOLUTION
#: PATH" section for what each one means and who (this module vs. C5) owns
#: turning it into a response.
KEY_ABSENT = "key_absent"
ROOT_UNRESOLVABLE = "root_unresolvable"
ROOT_UNSTAMPED = "root_unstamped"
NO_LISTENER = "no_listener"
ROUTED = "routed"


@dataclass(frozen=True)
class RouteResolution:
    """The discriminated outcome of resolving one fire's clone identity.

    `state` is always one of the five module-level constants above.
    `identity` is the raw string the extractor returned, if any -- present
    on every state except `KEY_ABSENT`. `engine_root` is populated from
    `ROOT_UNSTAMPED` onward (a resolvable path was found, whether or not it
    turned out to be a valid engine build). `record`/`url` are populated
    only on `ROUTED`.
    """

    state: str
    identity: Optional[str] = None
    engine_root: Optional[Path] = None
    record: Optional[dict] = None
    url: Optional[str] = None


def resolve_route(
    headers: Mapping[str, str],
    *,
    extractor: CloneIdentityExtractor = clone_identity_from_headers,
    front_door_root: Optional[Path] = None,
) -> RouteResolution:
    """Resolve one fire's headers to a `RouteResolution` (AC5).

    Never raises: every read this function performs
    (`engine_root.is_engine_root`, `supervisor.read_discovery`) already has
    a never-raises contract, and this function adds no unguarded call of its
    own -- mirrors `supervisor.ensure_listener`'s and `front_door.
    ensure_front_door`'s identical fail-open framing (AC10), even though
    this function is a pure resolver rather than an entry point that spawns.

    `front_door_root` is the multiplexer's OWN engine root (default:
    `engine_root.current_engine_clone()`), used only to locate the routing-
    table cache (AC9) -- it is never the TARGET of routing, which is always
    `engine_root` resolved from `identity`.
    """
    identity = extractor(headers)
    if not identity:
        return RouteResolution(state=KEY_ABSENT)

    candidate = Path(identity)
    if not candidate.is_dir():
        return RouteResolution(state=ROOT_UNRESOLVABLE, identity=identity)

    root = resolve_engine_root(candidate)
    if root is None:
        return RouteResolution(state=ROOT_UNSTAMPED, identity=identity, engine_root=candidate)

    record = supervisor.read_discovery(root)
    if record is None or not supervisor.discovery_is_live(record):
        return RouteResolution(state=NO_LISTENER, identity=identity, engine_root=root)

    url = supervisor.listener_url(record)
    if url is None:
        return RouteResolution(state=NO_LISTENER, identity=identity, engine_root=root, record=record)

    _record_route(identity, root, front_door_root=front_door_root)
    return RouteResolution(state=ROUTED, identity=identity, engine_root=root, record=record, url=url)


# ---------------------------------------------------------------------------
# C5 -- turning a resolved (but unroutable) fact into the wire-shape response.
# See module docstring's "RESOLUTION PATH" section: `resolve_route` above
# produces the discriminated fact; this is where it becomes the response
# `front_door.py` actually answers with.
# ---------------------------------------------------------------------------

#: One distinct `detail` builder per unroutable `RouteResolution.state` (AC6),
#: each naming the fact that state carries so the operator sees a different
#: string per cause rather than one generic "could not route" message. Never
#: called for `ROUTED` -- see `unroutable_response` below.
_UNROUTABLE_DETAILS: dict = {
    KEY_ABSENT: lambda res: "no clone identity on this request",
    ROOT_UNRESOLVABLE: lambda res: "clone identity %s names no engine root this box can resolve" % res.identity,
    ROOT_UNSTAMPED: lambda res: "clone root %s carries no valid engine stamp" % res.engine_root,
    NO_LISTENER: lambda res: "clone root %s has no live listener" % res.engine_root,
}


def unroutable_response(resolution: RouteResolution, event_name: str) -> Optional[Dict[str, Any]]:
    """Turn an unroutable `RouteResolution` into `hook_http.unreachable_response`'s
    EXISTING loud did-not-run shape (AC15) -- never a third reporting vocabulary
    invented here (`unserved_response` is single-argument and structurally
    cannot carry these `detail` values, per eng-director review F2).

    Carries AC6's FOUR distinct facts as four distinct `detail` strings, one
    per unroutable `state` (`_UNROUTABLE_DETAILS` above) -- `key_absent`,
    `root_unresolvable`, `root_unstamped` (the `skew.UnstampedEngineRootError`
    case), and `no_listener` (the ordinary post-idle-demotion case) are
    DIFFERENT facts with different owners and different remediations, and
    collapsing them into one message is the exact conflation AC6 forbids.

    Returns `None` for `ROUTED`: composing a "did not run" response for a
    resolution that DID reach a listener is a caller bug this function does
    not paper over -- a routed fire is answered by `forward_request`'s own
    response, not this one.

    Never raises: `resolution.state` is always one of the five module-level
    constants (`RouteResolution`'s own docstring), so the lookup below cannot
    miss.

    NEGATIVE SPEC (AC14, `399a72b4b`): this function decides nothing about
    WHERE a fire is forwarded -- routing is per-registration via the URL
    (`forward_request`'s own `path` argument, passed through verbatim by the
    caller), never inferred from `event_name` here. It also decides nothing
    about DoE's deny-on-unreachable policy and does not reroute to cold
    dispatch (DR-347 Ruling 3, module docstring's NEGATIVE SPEC section) --
    it only turns a resolved fact into the wire-shape response.
    """
    if resolution.state == ROUTED:
        return None
    detail = _UNROUTABLE_DETAILS[resolution.state](resolution)
    return hook_http.unreachable_response(event_name, detail)


# ---------------------------------------------------------------------------
# AC9 -- the routing-table cache. See module docstring's "THE ROUTING TABLE"
# section for why this exists and what it is (and is not): a best-effort
# accelerant, never a source of truth -- `resolve_route` above never reads
# it, only writes it, so a stale or corrupt entry can only cost a wasted
# lookup on the next boot, never misroute a live fire.
# ---------------------------------------------------------------------------

#: Distinct filename in the SAME per-clone, per-user `svc_dir()` `front_door.
#: DISCOVERY_FILENAME` / `supervisor.DISCOVERY_FILENAME` already publish
#: into -- never a second shape inside either of those files.
ROUTING_TABLE_FILENAME = "front-door-routing-table.json"

# Same atomic-replace primitive `front_door.write_discovery` and `supervisor.
# write_discovery` both use -- bound to the SAME object (AC9), never a third
# hand-rolled copy.
_replace_with_retry = locked_write.replace_with_retry


def routing_table_path(front_door_root: Optional[Path] = None) -> Path:
    """`<svc dir>/front-door-routing-table.json` for `front_door_root` (the
    multiplexer's own clone, default `current_engine_clone()`) -- `breadcrumb.
    svc_dir` reused as a pure per-clone/per-user directory resolver, never
    mutated, exactly as `front_door.discovery_path` / `supervisor.
    discovery_path` reuse it."""
    return breadcrumb.svc_dir(front_door_root) / ROUTING_TABLE_FILENAME


def _read_routing_table(front_door_root: Optional[Path] = None) -> dict:
    """Best-effort read of the cache -- absent, unreadable, or malformed all
    degrade to `{}` rather than raising, mirroring every other HINT reader in
    this package (`supervisor.read_discovery`'s own docstring)."""
    path = routing_table_path(front_door_root)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        table = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return table if isinstance(table, dict) else {}


def _write_routing_table(table: dict, front_door_root: Optional[Path] = None) -> None:
    """Write the cache under `locked_write.held_lock`, replacing any prior
    content -- mirrors `front_door.write_discovery`'s own atomic-replace-
    with-in-place-fallback shape line for line, including the fallback's own
    rationale: a cache that goes briefly torn is recoverable on the very next
    successful resolution, but raising here would take down a fire this
    module has already successfully routed, over a write that is purely
    advisory. Never raises."""
    path = routing_table_path(front_door_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(table, ensure_ascii=False)
    try:
        with locked_write.held_lock(path, holder_label="warm.front_door_routing"):
            fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=".routing-table-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                if not _replace_with_retry(tmp_path, str(path)):
                    path.write_text(payload, encoding="utf-8", newline="\n")
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                else:
                    tmp_path = None
            finally:
                if tmp_path is not None:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001 -- the cache is advisory; a write failure must never
        # propagate into a caller that has already successfully resolved a route.
        return


def _record_route(identity: str, engine_root: Path, *, front_door_root: Optional[Path] = None) -> None:
    """Best-effort update of the routing-table cache with a freshly-resolved
    `identity -> engine_root` mapping (AC9). Called only from `resolve_route`'s
    own `ROUTED` branch, never from an unroutable one -- the cache never
    remembers a failure, only a route that actually worked THIS call."""
    table = _read_routing_table(front_door_root)
    table[identity] = {
        "engine_root": str(engine_root),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write_routing_table(table, front_door_root)


# ---------------------------------------------------------------------------
# Forwarding -- the one network hop from the front door to the resolved
# clone's own listener. See module docstring's "FORWARDING NEVER MINTS OR
# RECOMPUTES A TOKEN" section: this is a proxy, not a second authoriser.
# ---------------------------------------------------------------------------

#: A liveness-probe-scale budget, not a work budget -- mirrors `supervisor.
#: HEALTH_CHECK_TIMEOUT_SECS` / `front_door.PROBE_TIMEOUT_SECS`'s own framing:
#: the resolved clone's listener is a sibling transport on the SAME machine.
PROXY_TIMEOUT_SECS = 2.0


def forward_request(
    resolution: RouteResolution,
    path: str,
    body: bytes,
    *,
    engine_token: Optional[str] = None,
    timeout: float = PROXY_TIMEOUT_SECS,
    opener: Any = None,
) -> Optional[bytes]:
    """POST `body` to `resolution.url + path` on the resolved clone's own
    listener, returning the raw response bytes, or `None` on ANY failure --
    not `ROUTED`, connection refused, timeout, or a non-2xx status. Never
    raises: mirrors `front_door.probe_existing_holder`'s and `supervisor.
    check_health`'s identical liveness-probe contract, so a caller (C5's
    response-shaping layer) reads `None` as "could not reach the resolved
    listener THIS call" and answers unroutable, exactly as it would for a
    resolution failure -- never a hang, never a raised exception (AC10).

    `engine_token`, if given, is forwarded VERBATIM on `http_listener.
    ENGINE_TOKEN_HEADER` -- this function never computes one itself (module
    docstring's negative spec). The resolved clone's own `_serve_line` is
    what judges it, identically to a direct caller.

    `opener` is an injectable `urllib.request.urlopen`-shaped callable for
    tests, mirroring `front_door.probe_existing_holder`'s own seam.
    """
    if resolution.state != ROUTED or not resolution.url:
        return None

    import urllib.request

    headers = {"Content-Type": "application/json"}
    if engine_token is not None:
        headers[ENGINE_TOKEN_HEADER] = engine_token

    url = resolution.url.rstrip("/") + path
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    open_url = opener if opener is not None else urllib.request.urlopen
    try:
        with open_url(req, timeout=timeout) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            if not (200 <= int(status) < 300):
                return None
            return resp.read()
    except Exception:  # noqa: BLE001 -- a proxy hop must never raise, see docstring
        return None
