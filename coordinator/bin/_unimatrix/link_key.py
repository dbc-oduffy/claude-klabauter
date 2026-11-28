"""Canonical identity of a link target, shared by autolink, roster and mirror-all singleton keys. Stdlib-only."""
from __future__ import annotations


def host_key(name) -> str:
    """Lower-cased host with trailing dots and a `.local` suffix removed."""
    # invariant: same normalisation as unimatrix-pair.py and unimatrix-doctor.py :: _host_key; change all together.
    low = str(name).strip().lower().rstrip(".")
    return low[:-len(".local")] if low.endswith(".local") else low


def link_key(name) -> str:
    """`<user lower>@<host_key(host)>`, or the bare host key without a user."""
    user, at, host = str(name).strip().rpartition("@")
    host = host_key(host)
    return f"{user.strip().lower()}@{host}" if at and user.strip() else host
