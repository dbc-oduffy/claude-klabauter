"""Stall probe: block every hot-path IO primitive under a sandbox and time each best-effort site.

Run ``python coordinator_core/tests/hot_path_stall_probe.py``. Each site is called
while the sandbox's open/write/mkdir/iterdir/glob/replace block on an event that a
timer releases after ``--stall`` seconds. Prints per site the caller's process-time
ms, caller-blocked wall ms, and subprocess spawn count; exits 1 if any site blocks
past ``--limit-ms``. Only paths under the sandbox stall, so the probe itself is
unaffected.
"""

from __future__ import annotations

import argparse
import builtins
import glob
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _install_stall(sandbox: str, gate: "dict[str, threading.Event]"):
    """Patch IO primitives to wait on gate['ev'] for paths under ``sandbox``; return an undo."""
    real = {}

    def under(p) -> bool:
        try:
            return str(os.fspath(p)).startswith(sandbox)
        except TypeError:
            return False

    def wrap_path(name):
        orig = getattr(Path, name)
        real[("Path", name)] = orig

        def patched(self, *a, **k):
            if under(self):
                gate["ev"].wait()
            return orig(self, *a, **k)

        setattr(Path, name, patched)

    def wrap_fn(mod, name):
        orig = getattr(mod, name)
        real[(mod, name)] = orig

        def patched(p, *a, **k):
            if under(p):
                gate["ev"].wait()
            return orig(p, *a, **k)

        setattr(mod, name, patched)

    for n in ("open", "write_text", "mkdir", "iterdir", "glob"):
        wrap_path(n)
    for mod, n in ((builtins, "open"), (os, "open"), (os, "replace"), (os, "scandir"), (glob, "glob")):
        wrap_fn(mod, n)

    def undo():
        for (owner, name), orig in real.items():
            setattr(Path if owner == "Path" else owner, name, orig)

    return undo


ARCHIVE_RECORDS = 1757
SIBLING_RUN_ID = "probe-run"


def build_archive(sandbox: Path, records: int = ARCHIVE_RECORDS) -> None:
    """Fill ``archive/handoffs`` with ``records`` frontmatter-headed handoffs, none matching ``hid``."""
    archive = sandbox / "archive" / "handoffs"
    nested = archive / "2026-10"
    nested.mkdir(parents=True, exist_ok=True)
    body = "# handoff\n" + "filler line\n" * 40
    for i in range(records):
        folder = archive if i % 2 else nested
        (folder / f"h{i}.md").write_text(
            f"---\nhandoff_id: other-{i}\nstatus: archived\n---\n{body}", encoding="utf-8"
        )


def _build_sites(sandbox: Path, setattr_=setattr):
    """Return the seven ``(name, call)`` sites rooted at ``sandbox``.

    ``setattr_`` patches the counters' root resolver; a pytest caller passes ``monkeypatch.setattr``
    so the patch is undone.
    """
    from coordinator_core import guard_advisory_counter as gac
    from coordinator_core import handoff_creation_guard as hcg
    from coordinator_core import registry_fallback_counter as rfc
    from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
    from coordinator_core.ops.dispatch_emit import run_close
    from coordinator_core.roadmap import prep_gate
    from coordinator_core.session import em_turn_count

    build_archive(sandbox)
    (sandbox / "docs" / "decisions").mkdir(parents=True)
    (sandbox / "docs" / "decisions" / "DR-001-x.md").write_text("x", encoding="utf-8")
    status = sandbox / contract.status_rel(SIBLING_RUN_ID)
    status.parent.mkdir(parents=True)
    status.write_text('{"status": "running"}\n', encoding="utf-8")
    root = str(sandbox)
    for mod in (gac, rfc):
        setattr_(mod, "resolve_git_root_cheap", lambda cwd=None, _r=root: _r)

    return [
        ("em_turn_count.save_state", lambda: em_turn_count.save_state(sandbox / "s" / "state.json", {"n": 1})),
        ("registry_fallback_counter.record_registry_fallback", lambda: rfc.record_registry_fallback("op", "stage", "sid", True, root)),
        ("guard_advisory_counter.record_advisory_fire", lambda: gac.record_advisory_fire("g", "sid", root)),
        ("guard_advisory_counter.record_deny_fire", lambda: gac.record_deny_fire("g", "sid", False, root)),
        ("handoff_creation_guard.find_archived_twin_by_handoff_id", lambda: hcg.find_archived_twin_by_handoff_id("hid", root)),
        ("prep_gate._new_dr_number_pinned", lambda: prep_gate._new_dr_number_pinned([], sandbox)),
        ("run_close.sibling_release", lambda: run_close.sibling_release(sandbox, SIBLING_RUN_ID, None)),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stall", type=float, default=5.0, help="seconds each primitive stays blocked")
    ap.add_argument("--limit-ms", type=float, default=250.0, help="caller-blocked ceiling per site")
    args = ap.parse_args()

    spawns = [0]
    orig_init = subprocess.Popen.__init__

    def counting_init(self, *a, **k):
        spawns[0] += 1
        return orig_init(self, *a, **k)

    subprocess.Popen.__init__ = counting_init
    failed = 0
    with tempfile.TemporaryDirectory(prefix="stall-probe-") as tmp:
        sandbox = Path(os.path.realpath(tmp))
        sites = _build_sites(sandbox)
        gate: "dict[str, threading.Event]" = {}
        undo = _install_stall(str(sandbox), gate)
        try:
            for name, call in sites:
                gate["ev"] = ev = threading.Event()
                timer = threading.Timer(args.stall, ev.set)
                timer.daemon = True
                timer.start()
                spawns[0] = 0
                cpu0, t0 = time.process_time(), time.perf_counter()
                try:
                    call()
                except Exception as exc:
                    print(f"  ({name} raised {type(exc).__name__}: {exc})", file=sys.stderr)
                blocked = (time.perf_counter() - t0) * 1000
                cpu = (time.process_time() - cpu0) * 1000
                timer.cancel()
                ev.set()
                bad = blocked > args.limit_ms or spawns[0] != 0
                failed += bad
                print(f"{'FAIL' if bad else 'ok  '} {name}: process_ms={cpu:.1f} blocked_ms={blocked:.0f} spawns={spawns[0]}")
                time.sleep(0.05)
        finally:
            undo()
            subprocess.Popen.__init__ = orig_init
    print(f"sites over {args.limit_ms:.0f}ms or spawning: {failed}/{len(sites)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
