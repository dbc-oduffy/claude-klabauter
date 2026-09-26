
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from coordinator_core.machine_resolver import registry_get
from coordinator_core.resolve_validation_cmd import cs_read_local_md_key
from coordinator_core.session.core import read_meta_field

_MARKER_KEY = "vcs_mirror"
_MARKER_VALUE = "p4"

_IDENTITY_FIELDS = ("port", "user", "client")


class P4WorkspaceUnregistered(Exception):

    def __init__(self, repo_key: str):
        self.repo_key = repo_key
        super().__init__(f"p4 workspace unregistered for repo_key={repo_key!r}")


@dataclass(frozen=True)
class P4Identity:
    port: str
    user: str
    client: str
    client_root: Optional[str] = None


def is_p4_repo(repo_root: str) -> bool:
    return cs_read_local_md_key(repo_root, _MARKER_KEY).strip() == _MARKER_VALUE


def identity(repo_key: str) -> P4Identity:
    values = {}
    for name in _IDENTITY_FIELDS:
        value = registry_get(f"p4.{repo_key}.{name}")
        if not value:
            raise P4WorkspaceUnregistered(repo_key)
        values[name] = value
    values["client_root"] = registry_get(f"p4.{repo_key}.client_root") or None
    return P4Identity(**values)


def session_change(sdir: str) -> dict:
    raw_change = read_meta_field(sdir, "p4_change")
    p4_change: Optional[int]
    try:
        p4_change = int(raw_change) if raw_change else None
    except ValueError:
        p4_change = None
    return {
        "p4_change": p4_change,
        "p4_base_sha": read_meta_field(sdir, "p4_base_sha") or None,
        "p4_shelved_at": read_meta_field(sdir, "p4_shelved_at") or None,
        "p4_shelved_sha": read_meta_field(sdir, "p4_shelved_sha") or None,
    }
