"""
coordinator_core.group_em.suite_grant -- the live Group EM's test-run grant: record, issue,
revoke, and the fail-closed reader the suite gate consults.

Record: ``<settings-home>/state/group-em/suite-grants/<issuer_sid>/<grantee_sid>.json`` -- under the
ISSUER's key, beside the nomination records, never in a grantee's ``.coordinator-local/coordinator-sessions/``
dir (grant.py's "a writer writes only its own session" invariant holds). Claude-Klabauter-owned; DoE's
``tier-u-grant.schema.json`` and ``grant.py``'s ``_VALID_GRANTED_BY`` are untouched.

A grant relayed from another box lands at ``suite-grants/relayed/<grantee_sid>.json`` only through
``receive_relayed_grant`` (HMAC tag verified under the issuer box's key pinned here) and is
re-verified on every read; the relayed leg is independent of the local holder.
A relayed grant is revoked by a signed ``suite-grant-revoke`` envelope delivered to the grantee box
(``revoke_suite_grant`` with ``box:session``); it also lapses at ``expires_at``.
``receive_relayed_grant`` checks grantee liveness against this box's session registry
even when ``home`` names another box's root.

NEGATIVE SPEC:

  - The issuer is the CALLING session (``core.resolve_session_id``), never an argument. Issue
    refuses unless that sid is the verified, live box holder (``nomination.box_holder``); a
    pending or rejected entry verdict is no standing.
  - The reader names ONE path under the CURRENT verified live holder's key. No glob, no scan, no
    spawn. A displaced or departed holder's grants stop authorizing with no revocation sweep, and a
    file planted under any other key is never read.
  - Fail closed: any error, malformed record, or unresolvable sid reads UNGRANTED. ``check`` never
    raises.
  - ``expires_at - issued_at`` above ``TTL_CEILING_MINUTES`` reads UNGRANTED, so a hand-edited
    record cannot outlive the ceiling.
  - The record authorizes the Tier-U/F suite gate only.

Spoofing stance: the caller sid comes from env, as it does for ``tier-u-grant-cli grant pm``;
this raises the cost of error, not of intent.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple, Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.group_em import (
    box_key,
    box_registry,
    member_box,
    node_transport,
    nomination,
    remote_identity,
    session_registry,
)
from coordinator_core.session import core
from coordinator_core.session.liveness import session_live

SCHEMA_VERSION = 1
KIND = "group-em-suite-grant"
TTL_DEFAULT_MINUTES = 120
TTL_CEILING_MINUTES = 720
RELAYED_DIR = "relayed"
CLOCK_SKEW_S = 300
AUDIT_FILE = "suite-grant-relay-audit.jsonl"

NOT_GROUP_EM = "NOT_GROUP_EM"
GROUP_EM_NOT_LIVE = "GROUP_EM_NOT_LIVE"
ISSUER_UNRESOLVED = "ISSUER_UNRESOLVED"
RELAY_ISSUER_ENV = "COORDINATOR_GROUP_EM_RELAY_ISSUER_SID"
GRANTEE_UNRESOLVED = "GRANTEE_UNRESOLVED"
#: Detail on a refused bare grantee: a name resolves only against this box's own session registry, so a session on
#: another box must be addressed by box.
ACCEPTED_GRANTEE_FORMS = (
    "accepted: <session_id> | <registry name of a live session on this box> | "
    "<box_id>:<session_id> | <member_host>:<session_id>"
)
GRANTEE_NOT_LIVE = "GRANTEE_NOT_LIVE"
TTL_OUT_OF_RANGE = "TTL_OUT_OF_RANGE"
NOTE_EMPTY = "NOTE_EMPTY"
BOX_UNKNOWN = "BOX_UNKNOWN"
RELAY_FAILED = "RELAY_FAILED"
SCOPE_ROOT_MSYS_MANGLED = "SCOPE_ROOT_MSYS_MANGLED"
REVOKE_KIND = "suite-grant-revoke"
NO_PIN_REASON = "bad_signature:no_pin"
SCOPE_ROOT_NOT_ABSOLUTE = "scope_root_not_absolute"

_MSYS_ROOT = re.compile(
    r"^[A-Za-z]:[\\/](?:Program Files(?: \(x86\))?[\\/]Git|msys64|msys32|cygwin64|cygwin)[\\/]",
    re.IGNORECASE,
)

#: Generator-provenance declaration (coordinator_core/ops/generator_census). Records live under
#: `settings_home() / "state" / "group-em" / "suite-grants"`, the operator home, never a path
#: inside this repo's tracked tree -- same disposition as nomination.py.
GENERATES = []


class IssueResult(NamedTuple):
    """``ok`` with the written ``record``, or a refusal ``reason`` (one of the constants above)."""

    ok: bool
    reason: str
    record: Optional[dict]


def _grants_dir(directory: Optional[Path]) -> Path:
    base = directory if directory is not None else settings_home() / "state" / "group-em"
    return base / "suite-grants"


def _safe_sid(sid: object) -> bool:
    return isinstance(sid, str) and bool(sid) and all(c.isalnum() or c in "-_" for c in sid)


def _grant_path(issuer_sid: str, grantee_sid: str, directory: Optional[Path]) -> Path:
    return _grants_dir(directory) / issuer_sid / f"{grantee_sid}.json"


def _relayed_path(grantee_sid: str, directory: Optional[Path], home: Optional[Path]) -> Path:
    base = directory if directory is not None else (box_registry._group_em_dir(home) if home else None)
    return _grants_dir(base) / RELAYED_DIR / f"{grantee_sid}.json"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def names_this_box(stamped: object, home: Optional[Path] = None) -> bool:
    """True when ``stamped`` is this box's id, or the same id differing only in case or a ``.local`` suffix
    (the same fold member_box applies). An unconfigured box (id "local") matches only itself; any other box id
    never matches."""
    local = box_registry.local_box_id(home)
    if not isinstance(stamped, str):
        return False
    if stamped == local:
        return True

    def fold(name: str) -> str:
        low = name.lower()
        return low[: -len(".local")] if low.endswith(".local") else low

    return local != box_registry.LOCAL_BOX_ID and fold(stamped) == fold(local)


def _box_ok(record: dict) -> bool:
    """A record with no ``box_id``, or one stamped "local", is valid anywhere; any other
    ``box_id`` must equal this box's identity."""
    stamped = record.get("box_id")
    if stamped is None or stamped == box_registry.LOCAL_BOX_ID:
        return True
    return stamped == box_registry.local_box_id()


def _parts(path: str) -> tuple[str, ...]:
    return Path(os.path.normcase(os.path.normpath(os.path.abspath(path)))).parts


def _at_or_under(root: str, scope_root: str) -> bool:
    """Component-wise, so ``/r/a`` is not under ``/r/ab``; case-folds only where the OS does."""
    r, s = _parts(root), _parts(scope_root)
    return len(r) >= len(s) and r[: len(s)] == s


def _msys_mangled(path: str) -> bool:
    """True when ``path`` carries a Git-for-Windows/MSYS2/Cygwin install-root prefix, the signature of
    Git Bash re-rooting a POSIX argv path. Detect only; never repaired."""
    return bool(_MSYS_ROOT.match(path))


def _path_flavour():
    return PureWindowsPath if os.name == "nt" else PurePosixPath


def _absolute_here(path: str) -> bool:
    """True when ``path`` is absolute in this OS's path flavour."""
    return _path_flavour()(path).is_absolute()


def _resolve_grantee(grantee: str) -> tuple[Optional[str], Optional[session_registry.RegistryRow]]:
    """A session id, or a registry name matching exactly one session; ``(None, None)`` otherwise."""
    rows = session_registry.read_rows()
    for row in rows:
        if row.session_id == grantee:
            return row.session_id, row
    named = [row for row in rows if row.name and row.name == grantee]
    if len(named) == 1:
        return named[0].session_id, named[0]
    return None, None


def _member_host_key(grantee: str, home: Optional[Path]) -> str:
    """``grantee`` with a verified-member head (``mac-mini.local:<sid>``, ``user@host:<sid>``, a box id in any case)
    rewritten to ``<membership box id>:<sid>``; a head the membership does not name is returned unchanged."""
    head, sep, tail = grantee.partition(remote_identity.SEP)
    if not sep or not tail:
        return grantee
    box_id = member_box.member_box_id(head, home=home)
    return f"{box_id}{remote_identity.SEP}{tail}" if box_id else grantee


def _home_of(directory: Optional[Path]) -> Optional[Path]:
    """The settings home owning the ``state/group-em`` dir ``directory`` (None: this box's home)."""
    return Path(directory).parent.parent if directory is not None else None


def _dials_member_host(box: dict) -> bool:
    """True when the row is a relay row dialling a raw member host (``user@host`` or a dotted mDNS name) rather than a
    registered ssh alias; only such a row can be stale and is worth re-resolving."""
    transport = box.get("transport")
    if not isinstance(transport, dict) or "relay_key" not in transport:
        return False
    host = transport.get("host")
    return isinstance(host, str) and ("@" in host or "." in host)


def _far_box(
    box_id: str, directory: Optional[Path], home: Optional[Path]
) -> tuple[Optional[dict], str]:
    """The box.json row for ``box_id``, else the row ``member_box`` materialises from verified membership;
    ``(None, reason)`` with ``BOX_UNKNOWN`` (plus the membership failure detail, when not a plain miss)."""
    box = next((b for b in box_registry.read_boxes(directory) if b.get("box_id") == box_id), None)
    if box is not None and not _dials_member_host(box):
        return box, ""
    resolved = member_box.resolve_member_box(box_id, home=home)
    if resolved.get("ok") is True:
        return resolved["box"], ""
    if box is not None:
        return box, ""
    why = str(resolved.get("reason") or BOX_UNKNOWN)
    return None, BOX_UNKNOWN if why == BOX_UNKNOWN else f"{BOX_UNKNOWN}:{why}"


def _relay_grant(
    sid: str,
    holder: dict,
    box_id: str,
    grantee_sid: str,
    note: str,
    ttl_minutes: int,
    repo: Optional[str],
    directory: Optional[Path],
    home: Optional[Path],
    now: Optional[float],
) -> IssueResult:
    """Sign the grant under this box's key and deliver it to ``box_id``; the far box decides."""
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    if repo and _msys_mangled(repo):
        return IssueResult(False, SCOPE_ROOT_MSYS_MANGLED, None)
    box, unknown = _far_box(box_id, directory, home)
    if box is None:
        return IssueResult(False, unknown, None)
    issued = time.time() if now is None else now
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "issuer_session_id": sid,
        "issuer_name": nomination._holder_name(holder),
        "issuer_box_id": box_registry.local_box_id(home),
        "grantee_session_id": grantee_sid,
        "grantee_box_id": box_id,
        "grantee_name": "",
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + ttl_minutes * 60),
        "note": note.strip(),
        "issuer_key_fp": box_key.own_fingerprint(home),
    }
    if repo:
        record["scope_root"] = repo
    envelope = box_key.sign(record, home)
    transport = node_transport.select(box, directory)
    reply = transport.deliver_grant(box, envelope)
    if isinstance(reply, dict) and str(reply.get("reason", "")).startswith(NO_PIN_REASON):
        pinned = transport.deliver_key_pin(
            box,
            {
                "box_id": box_registry.local_box_id(home),
                "key_b64": base64.b64encode(box_key.ensure_key(home)).decode("ascii"),
            },
        )
        if isinstance(pinned, dict) and pinned.get("ok") is True:
            reply = transport.deliver_grant(box, envelope)
        else:
            far = pinned.get("reason") if isinstance(pinned, dict) else None
            return IssueResult(False, f"{RELAY_FAILED}:{NO_PIN_REASON}:keypin_refused:{far or 'no_reply'}", None)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        far = reply.get("reason") if isinstance(reply, dict) else None
        return IssueResult(False, f"{RELAY_FAILED}:{far or 'no_reply'}", None)
    return IssueResult(True, "issued", envelope)


AUDIT_FIELD_MAX = 128


def _audit_clip(value: object) -> object:
    """An envelope field as an audit value: refused arrivals carry attacker-chosen content, so strings are bounded
    and non-scalars are dropped."""
    if isinstance(value, str):
        return value[:AUDIT_FIELD_MAX]
    return value if value is None or isinstance(value, (bool, int, float)) else None


def _audit_relayed(
    envelope: object,
    reply: dict,
    directory: Optional[Path],
    home: Optional[Path],
    now: Optional[float],
) -> None:
    """Append one JSON line per arrival (accepted or refused) to ``<group-em dir>/suite-grant-relay-audit.jsonl``
    on this box; best-effort, never raises."""
    try:
        env = envelope if isinstance(envelope, dict) else {}
        if home is None and directory is not None:
            home = _home_of(directory)
        base = directory if directory is not None else (box_registry._group_em_dir(home) if home else None)
        accepted = reply.get("ok") is True
        line = {
            "at": _iso(time.time() if now is None else now),
            "outcome": "accepted" if accepted else "refused",
            "reason": _audit_clip(reply.get("reason")),
            "issuer_box_id": _audit_clip(env.get("issuer_box_id")),
            "issuer_session_id": _audit_clip(env.get("issuer_session_id")),
            "grantee_session_id": _audit_clip(env.get("grantee_session_id")),
            "expires_at": _audit_clip(env.get("expires_at")),
        }
        if accepted:
            line["basis"] = "box-key-hmac"
        path = _grants_dir(base).parent / AUDIT_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 -- audit is best-effort
        pass


def receive_relayed_grant(
    envelope: object,
    *,
    home: Optional[Path] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    """Arrival check for a grant another box issued; ``{"ok": True}`` after writing
    ``relayed/<grantee_sid>.json``, else ``{"ok": False, "reason"}`` having written nothing.

    The tag must verify under the issuer box's key pinned on this box. ``scope_root`` is the
    envelope's when the issuer named one, else the grantee's cwd in this box's registry. Every
    arrival, accepted or refused, appends one line to ``suite-grant-relay-audit.jsonl``. Never raises.
    """
    reply = _receive_relayed_grant(envelope, home=home, directory=directory, now=now)
    _audit_relayed(envelope, reply, directory, home, now)
    return reply


_FP_MAX = 16
_SIG_REMEDY = {
    "unsigned": "the envelope carries no signature: the issuer must re-issue",
    "no_pin": "pin the issuer key here (groupem.box_key_pin)",
    "pin_mismatch": "re-pin the issuer key here (groupem.box_key_pin)",
    "tag_mismatch": "same key, tag differs: tampering or engine version skew",
    "unverified_issuer_key": "no issuer fingerprint: re-pin or upgrade issuer",
}


def _bad_signature_reason(why: dict) -> str:
    """``bad_signature`` plus the failure kind, the fingerprint pinned here (``expected``) and the one the
    issuer stamped (``got``). Fingerprints only, never key material; ``got`` is attacker-chosen so it is clipped."""
    kind = why.get("kind")
    if kind not in _SIG_REMEDY:
        return "bad_signature"
    got = why.get("got")
    got_text = got[:_FP_MAX] if isinstance(got, str) and got else "none"
    return f"bad_signature:{kind}:expected={why.get('expected') or 'none'}:got={got_text}:{_SIG_REMEDY[kind]}"


def _receive_relayed_grant(
    envelope: object,
    *,
    home: Optional[Path] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict:
    try:
        if not isinstance(envelope, dict):
            return {"ok": False, "reason": "bad_envelope"}
        if home is None and directory is not None:
            home = _home_of(directory)
        issuer_box = envelope.get("issuer_box_id")
        if not box_registry._valid_id(issuer_box):
            return {"ok": False, "reason": "bad_issuer_box"}
        why = box_key.diagnose(envelope, issuer_box, home)
        if why is not None:
            return {"ok": False, "reason": _bad_signature_reason(why)}
        grantee_sid = envelope.get("grantee_session_id")
        if (
            envelope.get("schema_version") != SCHEMA_VERSION
            or envelope.get("kind") != KIND
            or not _safe_sid(grantee_sid)
            or not _safe_sid(envelope.get("issuer_session_id"))
        ):
            return {"ok": False, "reason": "bad_record"}
        if not names_this_box(envelope.get("grantee_box_id"), home):
            return {"ok": False, "reason": "wrong_grantee_box"}
        issued, expires = _parse_iso(envelope.get("issued_at")), _parse_iso(envelope.get("expires_at"))
        current = time.time() if now is None else now
        if issued is None or expires is None or expires - issued > TTL_CEILING_MINUTES * 60:
            return {"ok": False, "reason": "bad_ttl"}
        if not issued <= current + CLOCK_SKEW_S or not current < expires:
            return {"ok": False, "reason": "expired"}
        if not session_live(grantee_sid):
            return {"ok": False, "reason": GRANTEE_NOT_LIVE}
        scope_root = envelope.get("scope_root")
        if not (isinstance(scope_root, str) and scope_root):
            rows = session_registry.read_rows()
            scope_root = next((r.cwd for r in rows if r.session_id == grantee_sid), "")
        if not scope_root:
            return {"ok": False, "reason": GRANTEE_UNRESOLVED}
        if not _absolute_here(scope_root):
            return {"ok": False, "reason": SCOPE_ROOT_NOT_ABSOLUTE}
        target = _relayed_path(grantee_sid, directory, home).parent
        target.mkdir(parents=True, exist_ok=True)
        body = {"envelope": envelope, "scope_root": scope_root}
        atomic_write_bytes(
            target / f"{grantee_sid}.json", (json.dumps(body, indent=2) + "\n").encode("utf-8")
        )
        return {"ok": True, "reason": "received"}
    except Exception as exc:  # noqa: BLE001 -- the receive contract is never-raise
        return {"ok": False, "reason": f"receive_failed:{type(exc).__name__}"}


_HARNESS_WALK_DEPTH = 8


def harness_issuer_sid(
    start_pid: Optional[int] = None, config_dir: Optional[Path] = None
) -> str:
    """Session id of the nearest ``claude`` ancestor of ``start_pid`` (default: this process),
    read from that harness's own ``<config>/sessions/<pid>.json``; ``""`` when no ancestor
    resolves. It names the caller only; ``issue_suite_grant`` still demands it equal the
    live verified box holder."""
    try:
        from coordinator_core.session import core as _core

        ps = _core._psutil()
        pid = os.getpid() if start_pid is None else start_pid
        for _ in range(_HARNESS_WALK_DEPTH):
            proc = ps.Process(pid)
            if _core._is_harness_process(_core._harness_process_comm(proc)):
                base = config_dir or Path(
                    os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
                )
                data = json.loads((base / "sessions" / f"{proc.pid}.json").read_text("utf-8"))
                sid = data.get("sessionId") if isinstance(data, dict) else None
                return sid if _safe_sid(sid) else ""
            pid = proc.ppid()
            if not pid:
                break
    except Exception:  # noqa: BLE001 -- unresolved is an answer, never a raise
        pass
    return ""


def issue_suite_grant(
    grantee: str,
    note: str,
    *,
    ttl_minutes: int = TTL_DEFAULT_MINUTES,
    repo: Optional[str] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
    relay_issuer_sid: Optional[str] = None,
) -> IssueResult:
    """Write the calling live verified box holder's test-run grant to ``grantee`` (a session id,
    or a registry name resolving to exactly one session). Reissuing overwrites. The grantee may be
    the holder itself. ``scope_root`` is ``repo`` or the grantee's registry ``cwd``.

    ``relay_issuer_sid`` stands in for an unresolvable caller sid for a ``box_id:session`` grantee
    only (an env-less relay hop); it must still equal the live verified box holder's sid. Absent,
    it is read from ``RELAY_ISSUER_ENV``, so the CLI path reaches it without a new argument."""
    sid = core.resolve_session_id()
    remote = isinstance(grantee, str) and ":" in grantee
    if relay_issuer_sid is None:
        relay_issuer_sid = os.environ.get(RELAY_ISSUER_ENV)
    if not _safe_sid(sid) and remote and _safe_sid(relay_issuer_sid):
        sid = relay_issuer_sid
    if not _safe_sid(sid):
        return IssueResult(False, ISSUER_UNRESOLVED, None)
    holder = nomination.box_holder(directory, now)
    if holder is None or str(holder.get("session_id") or "") != sid:
        return IssueResult(False, NOT_GROUP_EM, None)
    if not holder.get("live"):
        return IssueResult(False, GROUP_EM_NOT_LIVE, None)
    if not isinstance(note, str) or not note.strip():
        return IssueResult(False, NOTE_EMPTY, None)
    if (
        isinstance(ttl_minutes, bool)
        or not isinstance(ttl_minutes, int)
        or not 1 <= ttl_minutes <= TTL_CEILING_MINUTES
    ):
        return IssueResult(False, TTL_OUT_OF_RANGE, None)
    home = _home_of(directory)
    if remote:
        grantee = _member_host_key(grantee, home)
    grantee_box, remote_sid = remote_identity.parse_key(grantee, box_registry.local_box_id(home))
    if grantee_box != box_registry.local_box_id(home):
        return _relay_grant(
            sid, holder, grantee_box, remote_sid, note, ttl_minutes, repo, directory, home, now
        )
    grantee_sid, row = _resolve_grantee(grantee)
    if not _safe_sid(grantee_sid):
        return IssueResult(False, f"{GRANTEE_UNRESOLVED}:{ACCEPTED_GRANTEE_FORMS}", None)
    scope_root = repo or (row.cwd if row is not None else "")
    if not scope_root:
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    if not session_live(grantee_sid):
        return IssueResult(False, GRANTEE_NOT_LIVE, None)
    issued = time.time() if now is None else now
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "issuer_session_id": sid,
        "issuer_name": nomination._holder_name(holder),
        "grantee_session_id": grantee_sid,
        "grantee_name": row.name if row is not None else "",
        "scope_root": scope_root,
        "box_id": box_registry.local_box_id(),
        "issued_at": _iso(issued),
        "expires_at": _iso(issued + ttl_minutes * 60),
        "note": note.strip(),
    }
    target = _grant_path(sid, grantee_sid, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(target, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return IssueResult(True, "issued", record)


def _revoke_remote(
    sid: str, grantee: str, directory: Optional[Path], home: Optional[Path], now: Optional[float]
) -> IssueResult:
    """Sign a revoke for ``box:session`` under this box's key and deliver it; the far box decides."""
    local = box_registry.local_box_id(home)
    box_id, grantee_sid = remote_identity.parse_key(grantee, local)
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    box, unknown = _far_box(box_id, directory, home)
    if box is None:
        return IssueResult(False, unknown, None)
    envelope = box_key.sign(
        {
            "kind": REVOKE_KIND,
            "issuer_box_id": local,
            "issuer_session_id": sid,
            "grantee_session_id": grantee_sid,
            "grantee_box_id": box_id,
            "revoked_at": _iso(time.time() if now is None else now),
        },
        home,
    )
    reply = node_transport.select(box, directory).deliver_revoke(box, envelope)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        far = reply.get("reason") if isinstance(reply, dict) else None
        return IssueResult(False, f"{RELAY_FAILED}:{far or 'no_reply'}", None)
    return IssueResult(True, "revoked", envelope)


def revoke_suite_grant(
    grantee: str, *, directory: Optional[Path] = None, now: Optional[float] = None
) -> IssueResult:
    """Remove the calling session's own grant to ``grantee``; idempotent. Touches only the
    caller's key, so no standing is needed to withdraw one's own grant."""
    sid = core.resolve_session_id()
    if not _safe_sid(sid):
        return IssueResult(False, ISSUER_UNRESOLVED, None)
    home = _home_of(directory)
    if isinstance(grantee, str) and ":" in grantee:
        grantee = _member_host_key(grantee, home)
        box_id, bare = remote_identity.parse_key(grantee, box_registry.local_box_id(home))
        if box_id != box_registry.local_box_id(home):
            return _revoke_remote(sid, grantee, directory, home, now)
        grantee = bare
    grantee_sid, _row = _resolve_grantee(grantee)
    if grantee_sid is None and _safe_sid(grantee):
        grantee_sid = grantee
    if not _safe_sid(grantee_sid):
        return IssueResult(False, GRANTEE_UNRESOLVED, None)
    try:
        _grant_path(sid, grantee_sid, directory).unlink()
    except FileNotFoundError:
        return IssueResult(True, "absent", None)
    return IssueResult(True, "revoked", None)


def _check_local(sid, cwd, repo_root, directory, now) -> tuple[bool, Optional[dict]]:
    holder = nomination.box_holder(directory, now)
    if holder is None or not holder.get("live"):
        return False, None
    holder_sid = str(holder.get("session_id") or "")
    if not _safe_sid(holder_sid):
        return False, None
    try:
        record = json.loads(_grant_path(holder_sid, sid, directory).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None
    if not isinstance(record, dict):
        return False, None
    current = time.time() if now is None else now
    issued, expires = _parse_iso(record.get("issued_at")), _parse_iso(record.get("expires_at"))
    scope_root = record.get("scope_root")
    ok = (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("kind") == KIND
        and record.get("issuer_session_id") == holder_sid
        and record.get("grantee_session_id") == sid
        and _box_ok(record)
        and issued is not None
        and expires is not None
        and issued <= current < expires
        and expires - issued <= TTL_CEILING_MINUTES * 60
        and isinstance(scope_root, str)
        and bool(scope_root)
        and _at_or_under(repo_root or cwd or os.getcwd(), scope_root)
        and session_live(sid, cwd)
    )
    return bool(ok), record


_DENIAL_KIND = {
    "unsigned": "unsigned",
    "no_pin": "no_pin",
    "pin_mismatch": "forged",
    "tag_mismatch": "forged",
    "unverified_issuer_key": "forged",
}

RELAYED_DENIAL_REMEDY = {
    "revoked": "the issuing Group EM withdrew this grant; ask for a new one",
    "expired": "the grant lapsed; ask the Group EM to re-issue",
    "unsigned": "the relayed grant carries no signature; ask the Group EM to re-issue",
    "forged": "the relayed grant fails verification under the pinned issuer key; do not trust it",
    "no_pin": "no issuer key is pinned for the issuing box; pin it (groupem.box_key_pin)",
}


def _check_relayed(sid, cwd, repo_root, directory, now) -> tuple[bool, Optional[dict], Optional[str]]:
    """The relayed leg: one path, re-verified against the pinned issuer key; a record that fails
    the tag is never returned, so a denial cannot quote forged content. The third element names why a
    present relayed grant is refused (a ``RELAYED_DENIAL_REMEDY`` key, or ``refused``); ``None`` when no
    relayed grant exists for ``sid`` or it is granted. Reads files only."""
    from coordinator_core.group_em.relayed_grant_peer import TOMBSTONE_SUFFIX

    home = _home_of(directory)
    path = _relayed_path(sid, directory, home)
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, None, "revoked" if path.with_name(f"{sid}{TOMBSTONE_SUFFIX}").exists() else None
    envelope = body.get("envelope") if isinstance(body, dict) else None
    local_scope = body.get("scope_root") if isinstance(body, dict) else None
    if not isinstance(envelope, dict):
        return False, None, "forged"
    why = box_key.diagnose(envelope, str(envelope.get("issuer_box_id")), home)
    if why is not None:
        return False, None, _DENIAL_KIND.get(why.get("kind"), "forged")
    record = {**envelope, "scope_root": local_scope}
    current = time.time() if now is None else now
    issued, expires = _parse_iso(envelope.get("issued_at")), _parse_iso(envelope.get("expires_at"))
    root = repo_root or cwd or os.getcwd()
    env_scope = envelope.get("scope_root")
    ok = (
        envelope.get("schema_version") == SCHEMA_VERSION
        and envelope.get("kind") == KIND
        and envelope.get("grantee_session_id") == sid
        and names_this_box(envelope.get("grantee_box_id"), home)
        and issued is not None
        and expires is not None
        and issued <= current + CLOCK_SKEW_S
        and current < expires
        and expires - issued <= TTL_CEILING_MINUTES * 60
        and isinstance(local_scope, str)
        and bool(local_scope)
        and _at_or_under(root, local_scope)
        and (env_scope is None or (isinstance(env_scope, str) and _at_or_under(root, env_scope)))
        and session_live(sid, cwd)
    )
    if ok:
        return True, record, None
    lapsed = issued is not None and expires is not None and not current < expires
    return False, record, "expired" if lapsed else "refused"


def relayed_denial_reason(
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[str] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> Optional[str]:
    """``reason: remedy`` for a relayed grant naming this session that is present but refused (or
    revoked), else ``None``. Spawn-free; never raises."""
    try:
        sid = session_id or core.resolve_session_id(cwd)
        if not _safe_sid(sid):
            return None
        ok, _record, reason = _check_relayed(sid, cwd, repo_root, directory, now)
        if ok or reason is None:
            return None
        return f"{reason}: {RELAYED_DENIAL_REMEDY.get(reason, 'refused')}"
    except Exception:
        return None


def check_group_em_suite_grant(
    cwd: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    repo_root: Optional[str] = None,
    directory: Optional[Path] = None,
    now: Optional[float] = None,
) -> tuple[bool, Optional[dict]]:
    """``(granted, record)``: does the current live verified box holder's grant, or a verified grant
    relayed from another box, name the calling session for the consumer root? ``record`` is the
    parsed grant whenever one was read, granted or not, so a denial can quote it. The two legs are
    independent: a box with no live holder still honours a relayed grant. Reads at most two paths;
    never raises."""
    try:
        sid = session_id or core.resolve_session_id(cwd)
        if not _safe_sid(sid):
            return False, None
        try:
            granted, record = _check_local(sid, cwd, repo_root, directory, now)
        except Exception:
            granted, record = False, None
        if granted:
            return True, record
        relayed_ok, relayed, _reason = _check_relayed(sid, cwd, repo_root, directory, now)
        if relayed_ok:
            return True, relayed
        return False, record if record is not None else relayed
    except Exception:
        return False, None
