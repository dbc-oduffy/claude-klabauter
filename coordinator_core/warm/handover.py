"""Make-before-break warm pair handover: start the successor pair, wait until it answers, stop the old one.

Shared by the publish round (`percolate.round`) and `warm.pair_stop.restart_warm_pair`; it lives
under `warm` because it touches only warm modules, and the klabauter engine tree ships `warm`
but not the publish machinery."""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import IO, Optional


#: Longest the swap holds the publish marker waiting for the successor pair to answer a request.
#: Past it the OLD pair stays up and answering; nothing is stopped.
NEW_PAIR_READY_BUDGET_SECS = 10.0



#: Retry budget for a stamp write that loses the Windows sharing violation to a client reading it.
STAMP_REPLACE_RETRY_SECS = 1.0


def _replace_stamp_with_retry(root: Path, stamp_bytes: bytes) -> None:
    """`skew.replace_engine_stamp_bytes` retried on `PermissionError` for `STAMP_REPLACE_RETRY_SECS`:
    every hook reads the stamp, and a replace that loses to one aborts the handover with the old
    token still stamped and the old pair about to be left unstopped. Raises the last error."""
    from coordinator_core.warm import skew

    deadline = time.monotonic() + STAMP_REPLACE_RETRY_SECS
    while True:
        try:
            skew.replace_engine_stamp_bytes(root, stamp_bytes)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.002)


def start_successor_pair_in_hold(
    engine_root: Path,
    *,
    err: Optional[IO[str]] = None,
    ready_budget_secs: float = NEW_PAIR_READY_BUDGET_SECS,
    old_stamp: Optional[bytes] = None,
    new_stamp: Optional[bytes] = None,
) -> bool:
    """Make-before-break handover, under the caller's held publish marker, after the copy:
    start the successor pair from the freshly copied `engine_root`, wait until it answers a
    real request, only then stop the old pair (sparing the successor) and re-ensure the
    supervisor and the front door. True iff the successor answered and the swap completed.

    The old pair is never stopped before the successor answers. On timeout the old pair is left
    running and the remedy goes to `err`; the marker is never wedged on a slow boot and never
    released over a root with no answering pair. A root without the pair's entry scripts (not an
    engine tree) is a no-op. Never raises.

    `new_stamp`, when given, is the stamp the copy wrote that `hold_stamp_through_copy` already
    pinned back; the root then holds `old_stamp` and no pin write is needed.

    `old_stamp` is the engine stamp's bytes from before the copy. When given and the copy changed
    the stamp, it is put back for the successor's boot and the answer wait, so clients keep the old
    token (the old pair) until the successor answers; the successor boots under the new token via
    `skew.HANDOVER_TOKEN_ENV`. The new stamp is written back (the flip) only on a proven answer,
    before the old pair stops; on no answer or a raise the old stamp stays on disk.
    The pin is this function's first act, ahead of the breadcrumb read and the pair enumeration, so
    the window the copy's stamp is visible is two small reads wide.

    The spawn skips `should_spawn`: a live young breadcrumb is the OLD pair, which the debounce
    would read as a boot in flight; the election alone refuses a duplicate of the same generation.

    The spawn is skipped only when a LIVE old pair already serves the new token; a dead pair's
    stale breadcrumb never suppresses it.

    TRAP: with `old_stamp=None`, or a stamp that lands before this runs, clients hold the new token
    and reach no pair until the successor answers."""
    err = err if err is not None else sys.stderr
    unanswered_baseline: Optional[frozenset] = None
    try:
        from coordinator_core.warm import breadcrumb, client, front_door, pair_stop, skew, supervisor
        from coordinator_core.warm.telemetry import SPAWN_EPOCH_ENV

        root = Path(engine_root)
        if not (root / client.SERVER_ENTRY_SCRIPT).is_file():
            return True
        if new_stamp is None:
            new_stamp = skew.read_engine_stamp_bytes(root)
            token = skew.compute_client_token(root)
        else:
            token = skew.stamp_token(new_stamp)
        pinned = old_stamp is not None and new_stamp is not None and old_stamp != new_stamp
        flip_started = time.monotonic()
        if pinned and skew.read_engine_stamp_bytes(root) != old_stamp:
            _replace_stamp_with_retry(root, old_stamp)
        old = breadcrumb.read_breadcrumb(root) or {}
        old_pid = old.get("pid") if isinstance(old.get("pid"), int) else None
        old_pipe = old.get("pipe") if isinstance(old.get("pipe"), str) else None
        stored_epoch = old.get("stable_pid_start_epoch")
        old_alive = old_pid is not None and bool(
            breadcrumb.stable_pid_alive(
                old_pid, stored_start_epoch=str(stored_epoch) if stored_epoch is not None else ""
            )
        )
        old_pair_pids = frozenset(p.pid for p in pair_stop.find_pair_processes(root))
        unanswered_baseline = old_pair_pids
        env_extra = {SPAWN_EPOCH_ENV: repr(time.time())}
        if pinned:
            env_extra[skew.HANDOVER_TOKEN_ENV] = f"{token}:{skew.stamp_token(old_stamp)}"
        with breadcrumb.holder_successor_spawn():
            if not (old_alive and old_pipe and old_pipe.endswith(token)):
                client.spawn_detached(str(root), client.SERVER_ENTRY_SCRIPT, env_extra=env_extra)
        successor = breadcrumb.wait_for_successor_answer(
            root, token=token, superseded_pid=old_pid, max_secs=ready_budget_secs
        )
        if successor is not None:
            if pinned:
                _replace_stamp_with_retry(root, new_stamp)
            unanswered_baseline = None
            print(
                f"  [timing] successor token flip: {time.monotonic() - flip_started:.3f}s",
                file=err,
            )
        else:
            reaped = pair_stop.reap_pairs_outside(root, old_pair_pids)
            print(
                f"[handover] new warm pair under {root} not answering after {ready_budget_secs:g}s; "
                f"the old pair was not stopped; "
                f"{len(reaped)} unanswered successor process(es) ended. Remedy: check the new "
                f"server's log under the engine svc dir, then re-run the publish; the old pair "
                f"serves the previous build until then",
                file=err,
            )
            return False
        stop = pair_stop.stop_warm_pair(
            root, keep_pids=frozenset({successor["pid"]}), server_pipe=old_pipe
        )
        if not stop.ok():
            print(
                f"[handover] successor answers; the old pair under {root} survived its stop:\n"
                f"{stop.render_survivors()}",
                file=err,
            )
        with breadcrumb.holder_successor_spawn():
            supervisor.ensure_listener(root)
            if front_door.ensure_front_door(root) is None:
                print(
                    f"[handover] front door under {root} not answering after the swap; "
                    f"degraded until it boots (warm-front-door.json absent or unprobed)",
                    file=err,
                )
        return True
    except Exception as exc:  # noqa: BLE001 -- a slow successor never wedges or fails the swap
        print(f"[handover] successor pair start under {engine_root} failed: {exc!r}", file=err)
        if unanswered_baseline is not None:
            try:
                from coordinator_core.warm import pair_stop as _pair_stop

                _pair_stop.reap_pairs_outside(Path(engine_root), unanswered_baseline)
            except Exception:  # noqa: BLE001 -- best-effort reap of an unanswered successor
                pass
    return False
