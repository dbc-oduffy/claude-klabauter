"""Box registry: which boxes exist, what each can run, and the id a box stamps on its records.

A box is a settings-home root. Identity lives in ``<home>/state/group-em/box.json`` ({box_id});
the primary's registry lives in ``<primary home>/state/group-em/boxes/<box_id>.json``. Leaf
module like ``box_hold`` -- never import ``nomination``. ``local_box_id`` is one open+read and
reads "local" when the identity file is absent, so every single-box record keeps its meaning.
``directory`` (as in ``box_hold``) is the ``state/group-em`` dir; ``home`` is a settings-home.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core._settings_home import settings_home
from coordinator_core.atomic_replace import atomic_write_bytes

SCHEMA_VERSION = 1
KIND = "group-em-box"
LOCAL_BOX_ID = "local"


def admission_session_id(requested: str | None = None) -> str:
    """The actual invoking identity; a CLI selector can confirm it, never replace it."""
    from coordinator_core.session import core

    sid = core.attributable_session_id()
    if not isinstance(sid, str) or not sid:
        raise ValueError("resource admission requires the actual invoking session identity")
    if requested is not None and requested != sid:
        raise ValueError("--session-id must match the actual invoking session identity")
    return sid


def admission_parent(session_id: str) -> dict:
    """A selected parent must come from the actual child's birth-verified host binding."""
    from coordinator_core.session import external_registry

    child = external_registry.lookup(session_id)
    if not isinstance(child, dict) or child.get("session_id") != session_id:
        raise ValueError("parent pool requires the actual child's live native registry binding")
    parent_id = child.get("parent_session_id")
    if not isinstance(parent_id, str) or parent_id == session_id:
        raise ValueError("parent pool requires recorded native child-to-parent lineage")
    parent = external_registry.lookup(parent_id)
    if not isinstance(parent, dict) or parent.get("session_id") != parent_id:
        raise ValueError("parent pool requires a live birth-verified parent binding")
    return parent


def _group_em_dir(home: Optional[Path] = None) -> Path:
    base = Path(home) if home is not None else settings_home()
    return base / "state" / "group-em"


def _identity_path(home: Optional[Path] = None) -> Path:
    return _group_em_dir(home) / "box.json"


def _boxes_dir(directory: Optional[Path] = None) -> Path:
    base = Path(directory) if directory is not None else _group_em_dir()
    return base / "boxes"


def _valid_id(box_id: object) -> bool:
    return (
        isinstance(box_id, str)
        and bool(box_id)
        and all(c.isalnum() or c in "-_" for c in box_id)
    )


def local_box_id(home: Optional[Path] = None) -> str:
    """The box id stamped in ``<home>/state/group-em/box.json``; "local" when absent or malformed."""
    try:
        with open(_identity_path(home), "r", encoding="utf-8") as fh:
            data = json.loads(fh.read())
    except (OSError, ValueError):
        return LOCAL_BOX_ID
    box_id = data.get("box_id") if isinstance(data, dict) else None
    return box_id if _valid_id(box_id) else LOCAL_BOX_ID


def _write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, (json.dumps(record, indent=2) + "\n").encode("utf-8"))


def ensure_identity(home: Optional[Path] = None) -> str:
    """Return the box's id, minting ``box-<uuid8>`` into box.json once; never rewrites an extant id."""
    existing = local_box_id(home)
    if existing != LOCAL_BOX_ID:
        return existing
    box_id = "box-" + uuid.uuid4().hex[:8]
    _write(
        _identity_path(home),
        {"schema_version": SCHEMA_VERSION, "box_id": box_id, "minted_at": time.time()},
    )
    return box_id


REMOTE_SHELLS = ("posix", "cmd")
SSH_BINARY_NAMES = frozenset({"ssh", "ssh.exe"})


def lock_dir(home: Optional[Path] = None) -> Path:
    """Directory holding the per-box cross-process lock and rate-window files."""
    return _group_em_dir(home) / "box-locks"


def transport_problem(transport: dict) -> Optional[str]:
    """Why an ssh ``transport`` row's optional ``remote_shell`` / ``ssh`` fields are unusable; else None."""
    if transport.get("kind") != "ssh":
        return None
    shell = transport.get("remote_shell", "posix")
    if shell not in REMOTE_SHELLS:
        return f"remote_shell must be one of {REMOTE_SHELLS}"
    ssh = transport.get("ssh")
    if ssh is not None and (
        not isinstance(ssh, str) or ssh.replace("\\", "/").rsplit("/", 1)[-1] not in SSH_BINARY_NAMES
    ):
        return "ssh must be a path to an ssh or ssh.exe binary"
    return None


def register(
    root,
    capability: dict,
    directory: Optional[Path] = None,
    box_id: Optional[str] = None,
    transport: Optional[dict] = None,
) -> dict:
    """Register the peer box rooted at ``root`` in the registry under ``directory``; returns the record.

    Any node registers its peers; no node is privileged. Without ``box_id`` the id is
    ``ensure_identity(root)`` (a locally readable root), so the box's own records and its registry
    row agree. A peer whose root is not locally reachable passes its ``box_id`` (and ``transport``,
    e.g. ``{kind: ssh, host, invoke}`` plus optional ``remote_shell`` ("posix" default, or "cmd" for
    a Windows far box) and ``ssh`` (pinned absolute path of the ssh binary)): nothing is minted or
    written under ``root``. Raises ValueError for an unsafe ``box_id`` or transport. Re-registering overwrites the row (capability refresh).
    """
    root_path = Path(root)
    if box_id is None:
        box_id = ensure_identity(root_path)
    elif not _valid_id(box_id):
        raise ValueError(f"invalid box id: {box_id!r}")
    record = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "box_id": box_id,
        "root": os.fspath(root_path),
        "capability": dict(capability),
        "registered_at": time.time(),
    }
    if transport is not None:
        problem = transport_problem(transport)
        if problem:
            raise ValueError(f"invalid transport: {problem}")
        record["transport"] = dict(transport)
    _write(_boxes_dir(directory) / (box_id + ".json"), record)
    return record


def _parse(raw: str) -> Optional[dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("kind") != KIND:
        return None
    if not _valid_id(data.get("box_id")) or not isinstance(data.get("root"), str):
        return None
    if not isinstance(data.get("capability"), dict):
        return None
    return data


def read_boxes(directory: Optional[Path] = None) -> list[dict]:
    """Every well-formed registered box, sorted by box_id; malformed rows skipped. One listdir."""
    base = _boxes_dir(directory)
    try:
        names = sorted(n for n in os.listdir(base) if n.endswith(".json"))
    except OSError:
        return []
    out: list[dict] = []
    for name in names:
        try:
            with open(base / name, "r", encoding="utf-8") as fh:
                rec = _parse(fh.read())
        except OSError:
            continue
        if rec is not None:
            out.append(rec)
    return out


def box_root(box_id: str, directory: Optional[Path] = None) -> Optional[Path]:
    """The settings-home root registered for ``box_id``, or None (unknown or unsafe id). One open+read."""
    if not _valid_id(box_id):
        return None
    try:
        with open(_boxes_dir(directory) / (box_id + ".json"), "r", encoding="utf-8") as fh:
            rec = _parse(fh.read())
    except OSError:
        return None
    return Path(rec["root"]) if rec is not None else None
