#!/usr/bin/env python3
"""Idle live-delivery doorbell for an agy conversation: agy runs it as a background command.

It blocks on `<state_dir()>/<conv>.doorbell/` (beside the leg's `<conv>.queue.jsonl`), claims the first
`<id>.msg` by os.replace to `<id>.rung`, prints the message text, the sender's reply command and the
re-arm line, then exits 0; agy's own task-finished wake turns that output into a model turn.
`armed.<pid>` marks the doorbell live for its lifetime. It exits 0 silent once its agy session is gone
(unimatrix-liveness agy_session_state "dead", the leg's rule), so agy exit leaks no process.

Usage: unimatrix-agy-doorbell.py --conversation <id>
"""
import argparse
import importlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
POLL_SECS = 0.5
LIVENESS_SECS = 5.0
_PRETOOL = None


def _pretool() -> "types.ModuleType":
    """The pretool hook module (state_dir, reply_hint, SENDER_RE, CONV_RE, _console_python), loaded once."""
    global _PRETOOL
    if _PRETOOL is None:
        spec = importlib.util.spec_from_file_location("unimatrix_agy_hook_pretool", HERE / "unimatrix-agy-hook-pretool.py")
        _PRETOOL = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_PRETOOL)
    return _PRETOOL


def doorbell_dir(conv) -> Path:
    """`<conv>.doorbell` beside the conversation's spool: $AGY_LEG_SPOOL's directory, else state_dir()."""
    spool = os.environ.get("AGY_LEG_SPOOL")
    base = Path(spool).parent if spool else _pretool().state_dir()
    return base / f"{conv}.doorbell"


def session_gone(conv, started) -> bool:
    """True when the conversation's agy session is provably dead (the leg's rule, same grace override)."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    lv = importlib.import_module("_unimatrix.proc")
    try:
        lv.AGY_SESSION_GRACE_SECONDS = float(os.environ["AGY_LEG_GRACE_SECS"])
    except (KeyError, ValueError):
        pass
    rec = {"ts": started}
    try:
        rec["agy_pid"] = int(os.environ["AGY_LEG_PARENT_PID"])
    except (KeyError, ValueError):
        pass
    return lv.agy_session_state(conv, rec) == "dead"


def claim_next(ddir) -> "Path | None":
    """Claim the oldest `<id>.msg` by os.replace to `<id>.rung`; the rung path, or None when nothing was won."""
    try:
        with os.scandir(ddir) as it:
            msgs = sorted((e.stat().st_mtime, e.name) for e in it if e.name.endswith(".msg"))
    except OSError:
        return None
    for _mtime, name in msgs:
        rung = ddir / (name[:-len(".msg")] + ".rung")
        if rung.exists():  # the backstop already injected this id; drop the late message
            try:
                os.unlink(ddir / name)
            except OSError:
                pass
            continue
        try:
            os.replace(ddir / name, rung)
        except OSError:  # a peer (the PreInvocation backstop) won the claim, or the file is mid-write
            continue
        try:  # the rung's mtime is the claim time: the backstop re-delivers a claim never printed
            os.utime(rung)
        except OSError:
            pass
        return rung
    return None


def mark_printed(rung) -> None:
    """Rewrite the claimed `.rung` as `{"printed": true}` once its text reached stdout; the backstop reads that as delivered."""
    tmp = rung.with_name(rung.name + ".tmp")
    try:
        tmp.write_text(json.dumps({"printed": True}), encoding="utf-8")
        os.replace(tmp, rung)
    except OSError:
        pass


def render(rung, conv) -> str:
    """The doorbell's output for one claimed message: text, reply command when a sender is named, re-arm line."""
    try:
        text = json.loads(rung.read_text(encoding="utf-8"))["text"]
    except (OSError, ValueError, KeyError, TypeError):
        text = ""
    pre = _pretool()
    out = [text if isinstance(text, str) else ""]
    m = pre.SENDER_RE.search(out[0])
    if m:
        out.append(pre.reply_hint(m.group(1)))
    prefix = [pre._console_python(), str(Path(__file__).resolve()), "--conversation", conv]
    quoted = subprocess.list2cmdline(prefix) if os.name == "nt" else " ".join(shlex.quote(a) for a in prefix)
    out.append(f"Re-arm: run `{quoted}` as a background command.")
    return "\n".join(out)


def _marker_key(ddir, name):
    try:
        return ((ddir / name).stat().st_mtime_ns, int(name[len("armed."):]))
    except (OSError, ValueError):
        return None


def live_peer_armed(ddir, mine=None) -> bool:
    """True when another `armed.<pid>` marker names a live pid; markers of dead pids are swept.

    With `mine` (this process's marker key), only a live marker ordered before it counts: of two
    simultaneous arms the earlier marker keeps the doorbell and the later one stands down."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    pid_alive = importlib.import_module("_unimatrix.proc").pid_alive
    found = False
    try:
        with os.scandir(ddir) as it:
            marks = [e.name for e in it if e.name.startswith("armed.")]
    except OSError:
        return False
    for name in marks:
        try:
            pid = int(name[len("armed."):])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if pid_alive(pid):
            key = _marker_key(ddir, name)
            if mine is None or (key is not None and key < mine):
                found = True
            continue
        try:
            os.unlink(ddir / name)
        except OSError:
            pass
    return found


def _marker_body() -> str:
    """The armed marker's JSON: pid plus process-start stamp, so a reused pid is not mistaken for this doorbell."""
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    try:
        start = importlib.import_module("_unimatrix.proc").proc_start(os.getpid())
    except OSError:
        start = None
    return json.dumps({"pid": os.getpid(), "procStart": start})


def ring_once(conv) -> int:
    """Block until one message is claimed and printed (exit 0) or the agy session is gone (exit 0, silent).

    Exits 0 at once, silent, when another live doorbell is armed for the conversation: one per session.
    """
    ddir = doorbell_dir(conv)
    ddir.mkdir(parents=True, exist_ok=True)
    armed = ddir / f"armed.{os.getpid()}"
    armed.write_text(_marker_body(), encoding="utf-8")
    try:
        if live_peer_armed(ddir, _marker_key(ddir, armed.name)):
            return 0
        started = time.time()
        next_check = time.monotonic() + LIVENESS_SECS
        while True:
            rung = claim_next(ddir)
            if rung is not None:
                print(render(rung, conv), flush=True)
                mark_printed(rung)
                return 0
            if time.monotonic() >= next_check:
                if session_gone(conv, started):
                    return 0
                next_check = time.monotonic() + LIVENESS_SECS
            time.sleep(POLL_SECS)
    finally:
        try:
            armed.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unimatrix-agy-doorbell", description=__doc__.splitlines()[0])
    ap.add_argument("--conversation", default=os.environ.get("ANTIGRAVITY_CONVERSATION_ID"),
                    help="agy conversation id (default $ANTIGRAVITY_CONVERSATION_ID)")
    args = ap.parse_args(argv)
    if not args.conversation or not _pretool().CONV_RE.match(args.conversation):
        ap.error("--conversation <id> is required (letters, digits, _ and -)")
    return ring_once(args.conversation)


if __name__ == "__main__":
    raise SystemExit(main())
