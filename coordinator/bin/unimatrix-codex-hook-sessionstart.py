"""Codex SessionStart hook: bind this Codex thread to a stable peer name in peers.json.

Reads the hook payload on stdin ({"session_id": <thread id>, "cwd": ...}). Fires on every thread
start, resume, clear and fork, so each one rebinds the thread's peer name (kept while its checkout
is unchanged) to the current thread id; the router rescans peers.json, so no Codex restart is needed
and nothing depends on the id staying put.

Peer name: <checkout basename of the payload cwd>-<2 hex>, unique per thread, the same shape Claude
sessions use; the agent kind is the peers.json vendor field. No env var is read. Prints a
systemMessage so the registration shows in the TUI. Never fails the session: every path exits 0.

register_peer() is the shared registration helper: it takes the vendor and target, so another
vendor's SessionStart hook differs only in payload field names and vendor.
"""
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: replace stays atomic, only the read-modify-write lock is skipped
    fcntl = None

HERE = Path(__file__).resolve().parent
VENDOR = "codex"
NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")


def load_sibling(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def checkout_name(cwd):
    """Basename of the checkout holding cwd (nearest ancestor with .git), else of cwd itself."""
    path = Path(cwd)
    root = next((p for p in (path, *path.parents) if (p / ".git").exists()), path)
    return NAME_RE.sub("-", root.name).strip("-")


def peer_base(payload):
    """Repo part of the peer name from the session's own payload cwd; "codex" when there is none.

    UNIMATRIX_PEER_NAME is deliberately not read: it names the launching shell's own peer, and a
    Codex started from that shell inherits it.
    """
    cwd = payload.get("cwd")
    return (checkout_name(cwd) if isinstance(cwd, str) and cwd else "") or VENDOR


def session_name(rows, base, vendor, thread):
    """<base>-<2 hex>, unique per thread: this thread's own bound name is kept, else a free suffix.

    The suffix starts at a hash of the thread id so a name is stable across restarts and is only
    moved off when another thread already holds it.
    """
    own = re.compile(re.escape(base) + r"-[0-9a-f]{2}")
    taken = {}
    for r in rows:
        if isinstance(r, dict):
            if r.get("vendor") == vendor and r.get("target") == thread and own.fullmatch(str(r.get("name"))):
                return r["name"]
            taken[r.get("name")] = r.get("target")
    start = hashlib.sha1(thread.encode("utf-8")).digest()[0]
    for i in range(256):
        name = f"{base}-{(start + i) % 256:02x}"
        if name not in taken:
            return name
    raise ValueError(f"no free {base}-<hex> peer name")


def register_peer(peers_file, name, vendor, target):
    """Upsert {name, vendor, target} into peers_file atomically; return "added", "rebound" or "present".

    name may be a callable taking the current rows and returning the name, so it is chosen under the lock.

    The file's other rows are kept as written, except a row of the same vendor and target under
    another name (a stale binding of this thread); a missing file is created. An unreadable or
    non-list file raises ValueError and is left untouched.
    """
    peers_file = Path(peers_file)
    peers_file.parent.mkdir(parents=True, exist_ok=True)
    lock = open(f"{peers_file}.lock", "a", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data, rows = None, []
        if peers_file.exists():
            try:
                data = json.loads(peers_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"{peers_file} is not parseable JSON: {exc}") from exc
            rows = data.get("peers") if isinstance(data, dict) else data
            if not isinstance(rows, list):
                raise ValueError(f"{peers_file}: expected a list of peers or {{\"peers\": [...]}}")
        if callable(name):
            name = name(rows)
        row = {"name": name, "vendor": vendor, "target": target}
        stale = [r for r in rows if isinstance(r, dict) and r.get("name") != name
                 and r.get("vendor") == vendor and r.get("target") == target]
        rows = [r for r in rows if not any(r is s for s in stale)]
        status = "added"
        for i, existing in enumerate(rows):
            if isinstance(existing, dict) and existing.get("name") == name:
                if existing.get("vendor") == vendor and existing.get("target") == target:
                    if not stale:
                        return "present"
                    status = "rebound"
                    break
                rows[i] = row
                status = "rebound"
                break
        else:
            rows.append(row)
            status = "rebound" if stale else "added"
        out = {**data, "peers": rows} if isinstance(data, dict) else rows
        tmp = Path(f"{peers_file}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, peers_file)
        return status
    finally:
        lock.close()


def run(payload, peers_file=None):
    """Register the payload's thread; return the hook's stdout object, or None when nothing applies."""
    thread = payload.get("session_id")
    if not isinstance(thread, str) or not thread.strip():
        return None
    if peers_file is None:
        peers_file = load_sibling("unimatrix-peer-router").default_peers_file()
    base = peer_base(payload)
    chosen = []

    def pick(rows):
        chosen.append(session_name(rows, base, VENDOR, thread))
        return chosen[0]

    status = register_peer(peers_file, pick, VENDOR, thread)
    return {"systemMessage": f"unimatrix: peer {chosen[0]} bound to Codex thread {thread} ({status})"}


def main():
    try:
        payload = json.load(sys.stdin)
        out = run(payload) if isinstance(payload, dict) else None
        if out:
            print(json.dumps(out))
    except Exception as exc:  # a hook must never break the session
        print(json.dumps({"systemMessage": f"unimatrix: peer registration failed: {exc}"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
