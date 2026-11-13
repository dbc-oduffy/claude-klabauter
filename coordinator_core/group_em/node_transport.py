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
    deliver_revoke(box, envelope) -> {ok, reply}  # far box's relayed_grant_peer.receive_relayed_revoke
    deliver_key_pin(box, {box_id, key_b64}) -> {ok, reply}  # far box's relayed_grant_peer.receive_key_pin

Negative-spec:
    - ``from_session`` rides both send paths; over ssh it travels signed with the near box id, and
      the far box stamps it only when the signature verifies against its pinned key for that box.
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

            reply = box_send.send(box["box_id"], to_session, text, self.directory, from_session=from_session)
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

    def deliver_revoke(self, box: dict, envelope: dict) -> dict:
        try:
            from coordinator_core.group_em import relayed_grant_peer

            root = box.get("root")
            if not isinstance(root, str) or not root:
                return _fail("no_root")
            res = relayed_grant_peer.receive_relayed_revoke(envelope, Path(root))
            return {"ok": True, "reply": res} if res.get("ok") else _fail(str(res.get("reason")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"fs_revoke_failed:{type(exc).__name__}:{exc}")

    def deliver_key_pin(self, box: dict, payload: dict) -> dict:
        try:
            from coordinator_core.group_em import relayed_grant_peer

            root = box.get("root")
            if not isinstance(root, str) or not root:
                return _fail("no_root")
            res = relayed_grant_peer.receive_key_pin(
                payload["box_id"], payload["key_b64"], payload["box_id"], Path(root)
            )
            return {"ok": True, "reply": res} if res.get("ok") else _fail(str(res.get("reason")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"fs_keypin_failed:{type(exc).__name__}:{exc}")


class SshTransport:
    """ssh box: delegates to ``box_transport``; ``runner`` is injectable so tests spawn nothing."""

    kind = "ssh"

    def __init__(self, runner: Any = None, directory: Optional[Path] = None) -> None:
        self.runner = runner
        self.directory = directory

    def _mark_unreachable(self, box: dict) -> None:
        """Write an ``ok=false`` snapshot for ``box`` (prior sessions kept) so the watch holds it.

        Never raises: a marking failure must not mask the send failure being reported.
        """
        try:
            from coordinator_core.group_em import box_registry, remote_roster

            base = self.directory if self.directory is not None else box_registry._group_em_dir()
            box_id = box["box_id"]
            prior = remote_roster._raw(remote_roster.snapshot_path(base, box_id))
            kept = remote_roster._trim(prior.get("sessions")) if prior else []
            remote_roster._write_snapshot(base, box_id, False, kept, time.time())
        except Exception:  # noqa: BLE001
            pass

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
            from coordinator_core.group_em import box_registry, box_transport

            home = Path(self.directory).parent.parent if self.directory is not None else None
            res = box_transport.send(
                box,
                to_session,
                text,
                runner=self.runner,
                from_session=from_session,
                from_box=box_registry.local_box_id(home) if from_session else None,
                home=home,
            )
            if res.get("ok"):
                return res
            self._mark_unreachable(box)
            return _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_send_failed:{type(exc).__name__}:{exc}")

    def deliver_grant(self, box: dict, envelope: dict) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.deliver_grant(box, envelope, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_deliver_failed:{type(exc).__name__}:{exc}")

    def deliver_revoke(self, box: dict, envelope: dict) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.deliver_revoke(box, envelope, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_revoke_failed:{type(exc).__name__}:{exc}")

    def deliver_key_pin(self, box: dict, payload: dict) -> dict:
        try:
            from coordinator_core.group_em import box_transport

            res = box_transport.deliver_key_pin(box, payload, runner=self.runner)
            return res if res.get("ok") else _fail(str(res.get("reason", "ssh_failed")))
        except Exception as exc:  # noqa: BLE001 -- the seam contract is never-raise
            return _fail(f"ssh_keypin_failed:{type(exc).__name__}:{exc}")


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

    def deliver_revoke(self, box: dict, envelope: dict) -> dict:
        return _fail(f"unknown_transport:{self.detail}")

    def deliver_key_pin(self, box: dict, payload: dict) -> dict:
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
        return SshTransport(runner, directory)
    return UnknownTransport(str(kind))
