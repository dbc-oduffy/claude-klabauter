"""One seam for reaching a peer node: ``select(box)`` returns the transport for that box record.

A box record (``box_registry`` row) with no ``transport`` (or ``kind: fs``) is reached through the
filesystem: its own published roster snapshot for listing, its inbox file for sending. A record
carrying ``transport: {kind: ssh, host, invoke}`` is reached through ``box_transport`` (the host may
be any address ssh resolves, a WireGuard address included). Any other kind selects a transport
whose calls reply ``ok=False``.

Every call on every transport returns ``{"ok": True, ...}`` or ``{"ok": False, "reason": ...}`` and
never raises:
    list_sessions(box, run=None) -> {ok, box_id, sessions}
    send(box, to_session, text, from_session=None) -> {ok, reply}
    deliver_grant(box, envelope) -> {ok, reply}   # far box's suite_grant.receive_relayed_grant

Negative-spec:
    - ``from_session`` is accepted for the receiver-namespace rewrite but neither underlying
      send path carries it yet; it is not written into the inbox line here.
    - The ssh transport is a thin wrapper: ``box_transport`` stays the sole ssh spawn site.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional


def _fail(reason: str) -> dict:
    return {"ok": False, "reason": reason}


class FsTransport:
    """Loopback or shared-mount box: roster from its published snapshot, send into its inbox."""

    kind = "fs"

    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = directory

    def list_sessions(self, box: dict, run: Optional[str] = None) -> dict:
        try:
            from coordinator_core.group_em import remote_roster

            box_id = box["box_id"]
            root = box.get("root")
            if not isinstance(root, str) or not root:
                return _fail("no_root")
            loaded = remote_roster._load(
                remote_roster.snapshot_path(Path(root) / "state" / "group-em", box_id),
                time.time(),
                remote_roster.SNAPSHOT_TTL_S,
            )
            if not loaded["reachable"]:
                return _fail("snapshot_unavailable")
            sessions = []
            for row in remote_roster._trim(loaded["sessions"]):
                if run is not None and run not in str(row.get("name", "")) + str(
                    row.get("session_id", "")
                ):
                    continue
                sessions.append({**row, "box_id": box_id})
            return {"ok": True, "box_id": box_id, "sessions": sessions}
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"fs_list_failed:{type(exc).__name__}:{exc}")

    def send(
        self, box: dict, to_session: str, text: str, from_session: Optional[str] = None
    ) -> dict:
        try:
            from coordinator_core.group_em import box_send

            reply = box_send.send(box["box_id"], to_session, text, self.directory)
            if reply.get("sent") is True:
                return {"ok": True, "reply": reply}
            return _fail(str(reply.get("reason", "send_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"fs_send_failed:{type(exc).__name__}:{exc}")

    def deliver_grant(self, box: dict, envelope: dict) -> dict:
        try:
            from coordinator_core.group_em import suite_grant

            root = box.get("root")
            if not isinstance(root, str) or not root:
                return _fail("no_root")
            res = suite_grant.receive_relayed_grant(envelope, home=Path(root))
            return {"ok": True, "reply": res} if res.get("ok") else _fail(str(res.get("reason")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"fs_deliver_failed:{type(exc).__name__}:{exc}")


class SshTransport:
    """ssh box: delegates to ``box_transport``; ``runner`` is injectable so tests spawn nothing."""

    kind = "ssh"

    def __init__(self, runner: Any = None) -> None:
        self.runner = runner

    def list_sessions(self, box: dict, run: Optional[str] = None) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.list_remote_sessions(box, run=run, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_list_failed:{type(exc).__name__}:{exc}")

    def send(
        self, box: dict, to_session: str, text: str, from_session: Optional[str] = None
    ) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.send(box, to_session, text, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_send_failed:{type(exc).__name__}:{exc}")

    def deliver_grant(self, box: dict, envelope: dict) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.deliver_grant(box, envelope, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_deliver_failed:{type(exc).__name__}:{exc}")


class UnknownTransport:
    """A box whose ``transport.kind`` is unrecognised: every call replies ``ok=False``."""

    kind = "unknown"

    def __init__(self, detail: str) -> None:
        self.detail = detail

    def list_sessions(self, box: dict, run: Optional[str] = None) -> dict:
        return _fail(f"unknown_transport:{self.detail}")

    def send(
        self, box: dict, to_session: str, text: str, from_session: Optional[str] = None
    ) -> dict:
        return _fail(f"unknown_transport:{self.detail}")

    def deliver_grant(self, box: dict, envelope: dict) -> dict:
        return _fail(f"unknown_transport:{self.detail}")


def select(box: Any, directory: Optional[Path] = None, runner: Any = None):
    """The transport for ``box``: fs when it has no transport or ``kind: fs``, ssh for ``kind: ssh``.

    ``directory`` (the primary's ``state/group-em`` dir holding the registry) is used by the fs
    send; ``runner`` is the injectable ssh spawn for tests. Any other kind, or a non-dict box,
    yields an ``UnknownTransport``. Never raises.
    """
    if not isinstance(box, dict):
        return UnknownTransport("bad_box")
    t = box.get("transport")
    if t is None:
        return FsTransport(directory)
    if not isinstance(t, dict):
        return UnknownTransport("bad_transport")
    kind = t.get("kind", "fs")
    if kind == "fs":
        return FsTransport(directory)
    if kind == "ssh":
        return SshTransport(runner)
    return UnknownTransport(str(kind))
