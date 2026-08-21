"""
coordinator_core.ops.emission_publish — JSON-RPC "emission.publish" operation.

Purpose: wire identity (C1, `ops/emit/publish_identity.py`), envelope-splice (C2,
`ops/emit/publish_envelope.py`) and transport (C3, `ops/emit/publish_transport.py`)
together into the claude-klabauter-side half of the per-repo emission publish producer. Reads the
already-written `cockpit-emission.json` artifact off disk and POSTs a spliced copy to
Cockpit's `fleet-emissions` sink. It does NOT produce that artifact — see the negative-spec
below and AC9.

Self-registration: importing this module calls register_op("emission.publish", ...) as a
side-effect (same pattern as ops/artifact_emit.py). coordinator_core.ops.__init__ imports
it so registration fires at start_server() time.

AC9 negative-spec, load-bearing: this module never imports `coordinator_core.ops.
artifact_emit` and never calls its entry point. A pull (cockpit triggering this op) must
never re-emit — it transports the artifact already on disk, and cannot trigger the
expensive 21-section production path. `coordinator_core.ops.emit.envelope` IS imported —
that module is the shared home of `DEFAULT_OUTPUT_NAME` and other constants both
`artifact_emit` and this op need — but its `emit()` writer function is never called here.

AC5 negative-spec, load-bearing: exactly one document is written per invocation, keyed
`fleet-emissions/{repoKey}` on cockpit's side. No fan-out, no multi-repo loop, no
consolidated/aggregated write path exists anywhere below — the handler resolves exactly one
`derived_root`, reads exactly one on-disk artifact, and calls `publish_transport.
publish_document` exactly once.

Identity derivation deliberately does NOT parse the emission body (DR-344 § Performance
plan — a `json.loads` costs ~172ms of the ~281ms naive-design cost this plan's whole byte-
splice design exists to avoid). Instead it reuses the same cheap `git remote get-url origin`
resolution `ops/emit/context.py::resolve_repo_name` already performs for `artifact.emit`'s
own attribution — a single git subprocess call, not a body parse.

Single-flight concurrency guard: at most one in-flight publish per repo (keyed on the
derived main-worktree root). NOT for sink-corruption reasons — Firestore per-document
writes are atomic and this op is idempotent by construction (C6) — but for the memory
floor: each invocation holds a multi-ten-MB document in memory on a box sized against
50-70 concurrent LLMs, not a server's concurrency budget. A second concurrent caller for
the same repo is refused fast (`PublishInFlightError`) rather than allocating a second
buffer. No rate limit and no other idempotency machinery is added — both would be
over-engineering against a genuinely free property (see C6).

Spec backlink: pln-publish-per-repo-emission-to-o-b27b5e § C4
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from coordinator_core.ipc import register_op
from coordinator_core.machine_resolver import registry_get
from coordinator_core.ops.emit import context as _context
from coordinator_core.ops.emit import envelope as _envelope
from coordinator_core.ops.emit import publish_envelope as _publish_envelope
from coordinator_core.ops.emit import publish_identity as _publish_identity
from coordinator_core.ops.emit import publish_transport as _publish_transport
from coordinator_core.ops.fleet._common import main_worktree_root

# Base-URL resolution mirrors C3's own token convention exactly (environment primary,
# machine-local registry fallback, fail-loud on absence) -- no cadence, no doc, and no
# AC in this plan names where cockpit's endpoint URL comes from, so this reuses the
# already-established pattern rather than inventing a second one. Named distinctly from
# TOKEN_ENV_VAR/TOKEN_REGISTRY_KEY (publish_transport.py) -- destination and credential
# are two different configuration facts.
BASE_URL_ENV_VAR = "COCKPIT_EMISSION_PUBLISH_URL"
BASE_URL_REGISTRY_KEY = "cockpit_emission_publish_url"

_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT_REPOS: set[str] = set()


class PublishInFlightError(RuntimeError):
    """Raised when a publish is already in-flight for this repo (single-flight guard).

    Not a correctness guard (see module docstring -- the sink is idempotent by
    construction) -- a memory-floor guard: refuses fast rather than allocating a second
    multi-ten-MB in-memory buffer for the same repo concurrently.
    """


def _resolve_base_url() -> str:
    """Environment is primary (`COCKPIT_EMISSION_PUBLISH_URL`); fallback is the
    machine-local registry, key `cockpit_emission_publish_url`, read through
    `registry_get`. Absent configuration is a refusal, not a skip -- mirrors
    `publish_transport._resolve_token`'s own fail-loud posture.
    """
    url = os.environ.get(BASE_URL_ENV_VAR)
    if url:
        return url
    url = registry_get(BASE_URL_REGISTRY_KEY)
    if url:
        return url
    raise RuntimeError(
        f"emission.publish: no cockpit publish endpoint configured: set {BASE_URL_ENV_VAR} "
        f"or the machine-local registry key {BASE_URL_REGISTRY_KEY!r}"
    )


@register_op("emission.publish")
def _emission_publish(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'emission.publish' handler — transport the on-disk cockpit-emission
    artifact to cockpit's fleet-emissions sink.

    Params (all optional):
        emission_path (str) — override the on-disk artifact path; mirrors
            `artifact.emit`'s `out` param. Default: `<derived_root>/state/
            cockpit-emission.json` (the canonical per-repo emission path).

    repo_root (injected by ipc.dispatch_message): git_common_dir of the originating
    worktree. The handler derives the main-worktree root via
    main_worktree_root(repo_root) exactly as `artifact_emit._artifact_emit` does.
    Fails loud when repo_root is None -- no silent fallback to meta-repo (matches
    artifact.emit's own AC5 posture).

    Takes NO caller-supplied destination or body -- `base_url` and the document bytes
    are both resolved/derived here, never accepted as params. AC11's narrow-trampoline
    contract rests on this: there is nothing here for a foreign caller to steer beyond
    an optional local-path override this op's own params dict already carries.
    """
    if repo_root is None:
        raise ValueError(
            "emission.publish requires a per-repo dispatch key (_origin_worktree); "
            "repo_root is None — op scope must be 'common_dir' and _origin_worktree must be "
            "present in the JSON-RPC envelope. No silent fallback to meta-repo."
        )

    derived_root = main_worktree_root(repo_root)
    lock_key = str(derived_root)

    with _INFLIGHT_LOCK:
        if lock_key in _INFLIGHT_REPOS:
            raise PublishInFlightError(
                f"emission.publish: a publish is already in-flight for {lock_key!r}; "
                "refused fast rather than allocating a second in-memory buffer"
            )
        _INFLIGHT_REPOS.add(lock_key)

    try:
        emission_path = params.get("emission_path")
        if emission_path is None:
            artifact_path = Path(derived_root) / "state" / _envelope.DEFAULT_OUTPUT_NAME
        else:
            artifact_path = Path(emission_path)

        raw = artifact_path.read_bytes()

        # Identity: git-remote-derived, never body-parsed (see module docstring).
        slug = _context.resolve_repo_name(derived_root)
        owner, _sep, repo = slug.partition("/")

        spliced = _publish_envelope.splice_publish_envelope(raw, owner=owner, repo=repo)

        base_url = _resolve_base_url()
        _publish_transport.publish_document(base_url, spliced)

        doc_id = _publish_identity.publish_doc_id(owner, repo)
        repo_slug = _publish_identity.repo_slug(owner, repo)

        return {
            "ok": True,
            "repo_slug": repo_slug,
            "doc_id": doc_id,
            "bytes_published": len(spliced),
            "emission_path": str(artifact_path),
        }
    finally:
        with _INFLIGHT_LOCK:
            _INFLIGHT_REPOS.discard(lock_key)
