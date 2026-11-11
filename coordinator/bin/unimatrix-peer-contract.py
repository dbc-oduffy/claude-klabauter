#!/usr/bin/env python3
"""Peer contract shared by the unimatrix peer router and adapters.

Defines the peers.json schema, the cross-session frame (build and parse), the reply
label, the inbound prefix a peer sees, and the provenance stamp every posted frame
carries. Stdlib only: the scripts are vendored into <settings home>/unimatrix with no venv.
"""
import ipaddress
import json
import re
import socket
import sys
import uuid
from pathlib import Path

VENDORS = ("codex", "agy", "grok")
VENDOR_FIELD = "unimatrixVendor"
BOX_FIELD = "unimatrixBox"
PROVENANCE_FIELDS = ("peer", "vendor", "target", "router_pid", "msg_id")


_RE_CACHE = {}


def _memo_re(key, pattern, flags=0):
    if key not in _RE_CACHE:
        _RE_CACHE[key] = re.compile(pattern, flags)
    return _RE_CACHE[key]


def wrapper_re():
    """The cross-session wrapper pattern, compiled on first use so import stays inert."""
    return _memo_re("wrapper", r"^<cross-session-message[^>]*>\n?(.*?)\n?</cross-session-message>$", re.S)


def _from_name_re():
    return _memo_re("from_name", r'from-name="([^"]*)"')


class PeerConfigError(ValueError):
    """peers.json is malformed, names an unknown vendor, or repeats a peer name."""


def safe_alias(name):
    return (isinstance(name, str) and 1 <= len(name) <= 64 and '..' not in name and
            re.fullmatch(r'[A-Za-z0-9_-](?:[A-Za-z0-9_.-]*[A-Za-z0-9_-])?', name) is not None)


def owned_codex_address(row, target, expected_box=None):
    """Validate/project the engine's address; never allocate or reconstruct it.

    A legacy row has no address_version. A claimed versioned row that cannot
    prove its exact native target fails rather than silently acquiring a name.
    expected_box is used only when reading this host's own engine bindings.
    """
    if not isinstance(row, dict) or 'address_version' not in row:
        return None
    try:
        canonical = isinstance(target, str) and str(uuid.UUID(target)) == target
    except ValueError:
        canonical = False
    name, box = row.get('name'), row.get('box')
    match = re.fullmatch(r'[a-z0-9][a-z0-9-]{0,19}-codex-[a-z0-9][a-z0-9-]{0,11}-([1-9][0-9]{0,18})', name) if isinstance(name, str) else None
    if not (type(row.get('schema_version')) is int and row['schema_version'] == 1 and
            row.get('provider') == 'codex' and canonical and row.get('session_id') == target and
            type(row.get('address_version')) is int and row['address_version'] == 1 and
            isinstance(box, str) and box.strip() and match and len(name) <= 64 and
            int(match[1]) <= (1 << 63) - 1 and
            (expected_box is None or box.casefold() == expected_box.casefold())):
        raise PeerConfigError(f'invalid engine-owned Codex address for {target!r}')
    return {key: row[key] for key in ('schema_version', 'provider', 'session_id', 'address_version', 'name', 'box')}


def read_codex_address(directory, target, expected_box=None):
    """Read only the canonical UUID's engine binding; missing legacy data is absent."""
    try:
        if not isinstance(target, str) or str(uuid.UUID(target)) != target:
            return None
    except ValueError:
        return None
    try:
        row = json.loads((Path(directory) / f'{target}.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise PeerConfigError(f'cannot read engine address for {target}: {exc}') from exc
    if isinstance(row, dict) and 'ended_at' in row:
        raise PeerConfigError(f'engine Codex binding is ended: {target}')
    return owned_codex_address(row, target, expected_box)


def projected_addresses(row):
    """Extra pickable addresses only from a validated owned projection, including mirrors."""
    if not isinstance(row, dict) or row.get('vendor') != 'codex':
        return []
    try:
        owned = owned_codex_address({**row, 'name': row.get('unimatrixFarName') or row.get('name')}, row.get('target'))
    except PeerConfigError:
        return []
    aliases = row.get('aliases', [])
    if not owned or not isinstance(aliases, list) or not all(safe_alias(alias) for alias in aliases):
        return []
    return [row['target'], f"codex-{row['target']}", *aliases]


def load_peers(path):
    """Read peers.json into [{name, vendor, target}], keeping a valid codexPid/codexProcStart.

    Accepts a top-level list or {"peers": [...]}. Refuses a missing field, an unknown
    vendor, a blank name, or a duplicate name.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("peers") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise PeerConfigError(f"{path}: expected a list of peers or {{\"peers\": [...]}}")
    peers, seen, addresses = [], set(), {}
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PeerConfigError(f"{path}: peer #{i} is not an object")
        peer = {}
        for key in ("name", "vendor", "target"):
            val = row.get(key)
            if not isinstance(val, str) or not val.strip():
                raise PeerConfigError(f"{path}: peer #{i} needs a non-empty string {key!r}")
            peer[key] = val
        if peer["vendor"] not in VENDORS:
            raise PeerConfigError(f"{path}: peer {peer['name']!r} has unknown vendor {peer['vendor']!r}")
        if 'address_version' in row:
            if peer['vendor'] != 'codex':
                raise PeerConfigError('engine-owned Codex address requires vendor codex')
            peer.update(owned_codex_address(row, peer['target']))
        if 'aliases' in row:
            aliases = row['aliases']
            if not isinstance(aliases, list) or not all(safe_alias(alias) for alias in aliases):
                raise PeerConfigError(f'{path}: invalid peer aliases')
            peer['aliases'] = list(dict.fromkeys(aliases))
        pid, started = row.get('codexPid'), row.get('codexProcStart')
        if type(pid) is int and pid > 0:
            peer['codexPid'] = pid
            if isinstance(started, str) and started:
                peer['codexProcStart'] = started
        if peer["name"] in seen:
            raise PeerConfigError(f"{path}: duplicate peer name {peer['name']!r}")
        seen.add(peer["name"])
        for address in [peer['name'], *peer.get('aliases', [])]:
            owner = (peer['vendor'], peer['target'])
            if address in addresses and addresses[address] != owner:
                raise PeerConfigError(f'{path}: alias conflict for {address!r}')
            addresses[address] = owner
        peers.append(peer)
    return peers


def parse_inbound(frame):
    """Return (sender_endpoint, sender_name, body) from a received frame dict.

    sender_endpoint is the frame's `from` without its `uds:` prefix; sender_name is the
    wrapper's from-name attribute, or the endpoint when absent; body is the unwrapped text.
    """
    sender = str(frame.get("from", "")).removeprefix("uds:")
    content = frame.get("message", {}).get("content", "")
    m = wrapper_re().match(content)
    body = m.group(1) if m else content
    name = _from_name_re().search(content)
    return sender, (name.group(1) if name else sender), body


def label(peer, text):
    """Prefix a reply with the peer that produced it."""
    return f"[{peer}] {text}"


def build_frame(from_endpoint, from_name, text, msg_id=None):
    """Cross-session user frame carrying `text` from `from_endpoint` (a socket path or pipe)."""
    from_name = from_name.replace('"', "'")
    content = (f'<cross-session-message from="uds:{from_endpoint}" from-name="{from_name}">\n'
               f"{text}\n</cross-session-message>")
    return {"msgV": 1, "msg_id": msg_id or str(uuid.uuid4()), "type": "user",
            "message": {"role": "user", "content": content}, "priority": "next",
            "from": f"uds:{from_endpoint}"}


def stamp(frame, peer, vendor, target, router_pid):
    """Return a copy of `frame` with a `provenance` object and `from_name` set to the peer name."""
    out = dict(frame)
    out["provenance"] = {"peer": peer, "vendor": vendor, "target": target,
                         "router_pid": router_pid, "msg_id": frame.get("msg_id")}
    out["from_name"] = peer
    return out


def is_ip_literal(host):
    """True for an IPv4 or IPv6 literal (bracketed or zone-suffixed forms included)."""
    h = str(host).strip()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    try:
        ipaddress.ip_address(h.split("%", 1)[0])
        return True
    except ValueError:
        return False


def box_id(raw, ip_hosts=None):
    """The one box normaliser: drop any `user@`, keep the first dot label, drop a trailing `-lan`, lowercase.

    An IP literal has no host label: it maps through `ip_hosts` (ip -> member host name) to that
    host's label, and stays whole, never its first octet, when unmapped."""
    host = str(raw).rpartition("@")[2]
    if is_ip_literal(host):
        host = (ip_hosts or {}).get(host.strip("[]").lower(), host)
        if is_ip_literal(host):
            return host.strip("[]").lower()
    label = host.partition(".")[0].lower()
    return label[:-4] if label.endswith("-lan") else label


def local_box():
    """This box's id: box_id of the hostname."""
    return box_id(socket.gethostname())


def agent_row(entry, local_box):
    """Roster row {name, box, vendor} from one session entry.

    vendor: the entry's stamp, else "peer" for an unstamped `peer-` face (a router older than the
    stamp), else "claude". box: the entry's stamp, else `local_box`."""
    sid = str(entry.get("sessionId") or "")
    vendor = entry.get(VENDOR_FIELD) or ("peer" if sid.startswith("peer-") else "claude")
    return {"name": entry.get("name"), "box": entry.get(BOX_FIELD) or local_box, "vendor": vendor}


def main(argv=None):
    """Validate a peers.json: `unimatrix-peer-contract.py <peers.json>` prints each peer or the error."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) != 1:
        print("usage: unimatrix-peer-contract.py <peers.json>", file=sys.stderr)
        return 2
    try:
        peers = load_peers(argv[0])
    except (OSError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    for p in peers:
        print(f"{p['name']}\t{p['vendor']}\t{p['target']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
