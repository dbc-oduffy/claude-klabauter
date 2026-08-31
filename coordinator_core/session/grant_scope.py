"""Scoped Tier-U grants — a NARROWING sidecar, deliberately not a field.

The baton criterion this closes: *"A scoped Tier-U grant form exists."*
Today's grant is all-or-nothing by construction, so the only way past a
correct denial is a wholesale disarm — which is exactly the shape that gets
a guard disarmed once and left disarmed.

WHY A SIDECAR AND NOT A ``scope`` FIELD
=======================================
The grant record's schema is DoE-owned
(``coordinator/schemas/tier-u-grant.schema.json``) and declares BOTH
``"additionalProperties": false`` AND ``"schema_version": {"const": 1}``. A
``scope`` key on that record is therefore not a compatible extension — it is
an invalid record — and bumping their ``const`` is a change to a contract
this repo does not own and cannot unilaterally version
(``CLAUDE.md`` § Architecture: two hard external deps whose schemas we must
not break). So the narrowing lives in a claude-klabauter-owned artifact beside it:
``.git/coordinator-sessions/<sid>/tier-u-grant-scope.json``. Their record
stays byte-identical and schema-valid; nothing they validate changes.

THE SEMANTICS ARE NARROW-ONLY, AND THAT IS THE SAFETY PROPERTY
==============================================================
- **No scope file** → the grant means what it has always meant: unbounded.
  Every existing grant and every existing caller is unaffected, which is
  what makes this additive rather than a migration.
- **Scope file present** → the grant authorizes ONLY invocations every one
  of whose positionals sits at or under a declared prefix. A whole-suite run
  (no positionals at all) is DENIED even though a live grant exists. That is
  the entire point of the form: a scoped grant that still authorized the
  unscoped run would be decoration.
- **Scope file, no grant** → UNGRANTED. Narrowing nothing is not granting,
  so this can never manufacture authority it was not handed.

FAIL CLOSED ON MALFORMATION
===========================
Matching ``check_tier_u_grant``'s own deliberate inversion of the house
fail-open convention: an unreadable or malformed scope file reads as a scope
admitting NOTHING, never as an absent scope admitting everything. A
corrupted narrowing must not silently widen back to unbounded — that failure
mode is indistinguishable from an attack and from a bug, and it fails in the
direction that grants authority.

Negative-spec — this module NEVER writes, widens, or repairs the grant
record itself. It has no import of ``write_tier_u_grant`` and must not gain
one: the two artifacts are owned by different repos, and the only direction
authority moves here is down.

Spec backlink: DoE-claude DR-088 § Decision, layer 5.
Ruling backlink: ``docs/decisions/DR-396-a-grant-narrows-without-touching-a-schema-it-does-not-own.md``
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from coordinator_core.session import core
from coordinator_core.session.grant import check_tier_u_grant

# Generator-provenance declaration (generator_provenance.py). This module
# writes/unlinks only `.git/coordinator-sessions/<sid>/tier-u-grant-scope.json`
# -- git-internal session-hub state, never a tracked repo artifact.
GENERATES: List[str] = []

_GRANT_SCOPE_FILENAME = "tier-u-grant-scope.json"

#: Returned by `check_tier_u_grant_scoped` as its third element. Machine-stable
#: so a caller can branch or put it in a denial without re-deriving why.
REASON_GRANTED = ""
REASON_NO_GRANT = "no-grant"
REASON_SCOPE_UNREADABLE = "scope-unreadable"
REASON_UNSCOPED_INVOCATION = "unscoped-invocation"
REASON_OUT_OF_SCOPE = "out-of-scope"


def _grant_scope_file(sid: str, cwd: Optional[str]) -> Optional[Path]:
    """Resolve ``<session_dir>/tier-u-grant-scope.json`` — the single
    location-naming seam, mirroring ``grant._grant_file``'s discipline."""
    sdir = core.session_dir(sid, cwd)
    if not sdir:
        return None
    return Path(sdir) / _GRANT_SCOPE_FILENAME


def _norm_prefix(raw: str) -> str:
    """Repo-relative, forward-slashed, no leading/trailing separator.

    Windows is first-class in this repo, so a scope written
    ``coordinator\\tests`` must match a positional spelled
    ``coordinator/tests``."""
    return raw.replace("\\", "/").strip().strip("/")


def write_tier_u_grant_scope(
    paths: Sequence[str],
    note: str,
    *,
    session_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> bool:
    """Narrow the CALLING session's existing grant to ``paths``.

    Writes the sidecar only. It never mints, widens, or touches the grant
    record, so calling this without a grant produces a narrowing that
    authorizes nothing.

    ``paths`` must be non-empty: an empty scope would mean "admits nothing",
    which is a revoke, and ``revoke_tier_u_grant_scope`` is that. An
    absolute path or an upward traversal raises ``ValueError`` — a scope is
    a statement about this repo, and admitting either would let a narrowing
    name a target outside the tree it narrows within.

    Returns True on success, False on infra failure — same contract as
    ``write_tier_u_grant``.
    """
    if not paths:
        raise ValueError(
            "paths must be non-empty; an empty scope admits nothing -- use "
            "revoke_tier_u_grant_scope() to remove a narrowing"
        )
    if not note:
        raise ValueError("note is required (why this grant is narrowed)")

    normalized: List[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("scope path must be a non-empty string, got %r" % (raw,))
        # NOT `os.path.isabs`: it is platform-dependent, and Python 3.13
        # changed `ntpath.isabs` so a single-slash-rooted path like
        # `/abs/path` reads FALSE on Windows and TRUE on POSIX. A validator
        # that admits a rooted path on one host and refuses it on another is
        # worse than either answer -- measured by this module's own test,
        # which failed on exactly that row. Decide it from the string.
        if raw[:1] in ("/", "\\") or (len(raw) > 1 and raw[1] == ":"):
            raise ValueError("scope path must be repo-relative, got %r" % (raw,))
        norm = _norm_prefix(raw)
        if not norm:
            raise ValueError("scope path normalizes to empty: %r" % (raw,))
        if norm == ".." or norm.startswith("../") or "/../" in norm:
            raise ValueError("scope path must not traverse upward, got %r" % (raw,))
        normalized.append(norm)

    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False
    sdir = core.ensure_session(sid, cwd)
    if not sdir or not os.path.isdir(sdir):
        return False

    record = {
        "schema_version": 1,
        "session_id": sid,
        "scoped_at": core.now_iso(),
        "paths": sorted(set(normalized)),
        "note": note,
    }

    target = Path(sdir) / _GRANT_SCOPE_FILENAME
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=_GRANT_SCOPE_FILENAME + ".", dir=str(sdir))
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(record, fh)
            fh.write("\n")
        os.replace(tmp_name, target)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            # Best-effort tmp cleanup; the caller already gets False.
            pass
        return False
    return True


def read_tier_u_grant_scope(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> Optional[dict]:
    """Raw scope-sidecar reader.

    ``None`` means no narrowing was declared. A present-but-unparseable file
    returns the sentinel ``{}`` instead, so a caller can tell "nothing was
    declared" from "something was declared and I could not read it" — the
    two must not collapse, because they authorize opposite things.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return None
    path = _grant_scope_file(sid, cwd)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return record


def revoke_tier_u_grant_scope(
    cwd: Optional[str] = None, *, session_id: Optional[str] = None
) -> bool:
    """Remove the narrowing, restoring the grant's unbounded meaning.

    Idempotent: revoking an absent scope is success. This WIDENS authority,
    which is why it is a separate named act rather than something a scope
    write can do implicitly.
    """
    sid = session_id or core.resolve_session_id(cwd)
    if not sid:
        return False
    path = _grant_scope_file(sid, cwd)
    if path is None:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _positional_in_scope(positional: str, prefixes: Sequence[str]) -> bool:
    """Is ``positional`` at or under one of ``prefixes``?

    Matching is path-COMPONENT-wise, never string-wise: a scope of
    ``coordinator`` must not admit ``coordinator_core``, which a bare
    ``startswith`` would. A pytest node id (``path::test_name``) is judged
    on its path half.
    """
    norm = _norm_prefix(positional.split("::", 1)[0])
    if not norm or norm == "." or norm == "..":
        return False
    if norm.startswith("../") or "/../" in norm:
        return False
    for prefix in prefixes:
        if norm == prefix or norm.startswith(prefix + "/"):
            return True
    return False


def check_tier_u_grant_scoped(
    positionals: Sequence[str],
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
) -> Tuple[bool, Optional[dict], str]:
    """The scope-aware authorization predicate.

    Returns ``(granted, grant_record, reason)``. ``reason`` is ``""`` when
    granted, else one of the ``REASON_*`` constants above.

    The grant leg runs FIRST and unchanged, so this can never grant anything
    ``check_tier_u_grant`` would not — it only ever takes authority away.

    ``positionals`` is the invocation's already-extracted positional
    operands. An EMPTY list means a whole-suite run, which a narrowed grant
    must refuse: a scoped grant that authorized the unscoped run would
    narrow nothing.

    Never raises.
    """
    granted, record = check_tier_u_grant(cwd, session_id=session_id)
    if not granted:
        return False, record, REASON_NO_GRANT

    scope = read_tier_u_grant_scope(cwd, session_id=session_id)
    if scope is None:
        return True, record, REASON_GRANTED

    raw_paths = scope.get("paths")
    if not isinstance(raw_paths, list):
        return False, record, REASON_SCOPE_UNREADABLE

    prefixes = [
        _norm_prefix(p) for p in raw_paths if isinstance(p, str) and _norm_prefix(p)
    ]
    if not prefixes:
        return False, record, REASON_SCOPE_UNREADABLE

    if not positionals:
        return False, record, REASON_UNSCOPED_INVOCATION

    if all(_positional_in_scope(p, prefixes) for p in positionals):
        return True, record, REASON_GRANTED
    return False, record, REASON_OUT_OF_SCOPE
