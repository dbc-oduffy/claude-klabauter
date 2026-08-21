"""
coordinator_core.ops.emit.publish_identity — doc-id derivation and repo_slug identity.

Pure functions, no I/O, no network. This is the one piece of the emission-publish
producer surface that is fully verifiable without cockpit's endpoint existing.

`publish_doc_id(owner, repo)` derives a stable, collision-resistant identifier for an
(owner, repo) pair by hashing the pair joined with '/' — the same separator GitHub
itself uses for `<owner>/<repo>`, and one that is illegal inside either owner or repo
names. That illegality is load-bearing: a naive `f"{owner}__{repo}"` join collides for
owner="a_"/repo="_b" and owner="a"/repo="__b" (both stringify to "a___b"), because '_'
is GitHub-legal punctuation inside owner/repo names and so is not a safe separator.
'/' is not GitHub-legal inside either component, so joining on it cannot collide the
same way.

`repo_slug(owner, repo)` is the authoritative human-readable identity carried in the
envelope: `<owner>/<repo>`, casing preserved exactly as given (producer-authoritative
casing — see the emission body's own `repo` field, which the cockpit contract already
defines as owner-qualified `<owner>/<repo>`). Callers resolve (owner, repo) from that
field; this module never re-derives them from git remotes.

Negative-spec: `publish_doc_id` never calls builtin `hash()` — that seeds off
PYTHONHASHSEED and is not stable across processes. sha256 is deterministic across
processes and interpreters by construction.
"""

from __future__ import annotations

import hashlib

_DOC_ID_LEN = 16


def publish_doc_id(owner: str, repo: str) -> str:
    """Return the first 16 hex chars of sha256(f"{owner.lower()}/{repo.lower()}").

    Lowercase-folds both components before hashing so that casing differences in the
    source `repo` field never produce two doc ids for what is the same repository.
    """
    material = f"{owner.lower()}/{repo.lower()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DOC_ID_LEN]


def repo_slug(owner: str, repo: str) -> str:
    """Return the authoritative human-readable identity carried in the envelope.

    Casing is preserved exactly as given — the cockpit contract's `repo` field is
    producer-authoritative casing, and this is not a normalization point.
    """
    return f"{owner}/{repo}"
