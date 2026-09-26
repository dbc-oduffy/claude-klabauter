"""
coordinator_core.ops.backfill_initiative_fk

Port of: backfill-initiative-fk.sh (DoE 432e3285, 2026-07-22). BIG_PORT wave,
direct-import trampoline variant #1 — no `register_op`, no IPC; the DoE-side polyglot
trampoline imports and calls `main()` in-process, exactly like
coordinator_core.hooks.auto_push / coordinator_core.ops.handoff_gate_aging.

Purpose: idempotent batch-attach tool. Reads (artifact-path, initiative-id) TSV pairs
from a file or stdin and attaches the `initiative:` FK to each artifact's YAML
frontmatter via the sibling `coordinator-initiative attach` CLI (a python3 script as of
DoE-claude commit 6fb5fb37; invoked via `sys.executable`, not shelled out through bash).
One-shot backfill tool; safe to re-run on a partially-processed mapping.

Spec backlink: docs/plans/2026-07-06-ceremony-as-pipeline-2-doe-land-d-slice.md § F4 (AC8)

Public API:
    main(argv, script_dir=None) -> int
        CLI entry point. `argv` is the trampoline's own `sys.argv[1:]` (an optional
        single positional TSV path, or "-"/absent for stdin), plus an optional
        `--script-dir VALUE` (or `--script-dir=VALUE`) flag that this function
        parses out before treating the remaining argv as the positional path.
        `script_dir` (the explicit kwarg, or the argv flag) is the DoE
        trampoline's own bin/ directory — used to resolve the sibling
        `coordinator-initiative` executable via a plain same-directory join, exactly
        as the bash oracle's `SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"`
        did. This is a SAME-DIRECTORY SIBLING lookup, not a cross-repo DOE_ROOT
        resolution — this module is always invoked in-process by a trampoline that
        already knows its own location, so reusing that location is both simpler and
        more byte-faithful to the oracle than re-deriving DOE_ROOT via
        `coordinator_core.ops.coordinator_doe_root` (which is the right tool for a
        genuinely cross-repo caller, not this one). If neither is supplied, this
        module's own directory is used as a fallback (not a faithful mirror of the
        oracle's behavior when invoked from a repo layout other than co-located
        `coordinator/bin/`, but there is no other sensible default for a bare import).

Concurrency safety (unchanged from the oracle):
    A lockfile at `tempfile.gettempdir()/backfill-initiative-fk.lock` structurally
    prevents two concurrent invocations. Per concurrent-em-hazards §H1, concurrent
    frontmatter writers corrupt the tree. Uses an atomic `os.O_CREAT | os.O_EXCL`
    initial create (Python equivalent of the oracle's `set -C` noclobber fix, review
    finding F3) to close the TOCTOU window between existence-check and file-write on a
    non-existent lockfile. A stale-lockfile overwrite (crashed prior holder) remains a
    plain non-atomic write, faithfully matching the oracle (a second TOCTOU window
    exists there too, but it is bounded to the already-rare crash-recovery path and the
    oracle never closed it either).

Exit-code contract (byte-parity with the bash oracle):
    0  success — all pairs attached or skipped, zero errors.
    1  business failure — coordinator-initiative missing/not-executable, mapping file
       not found, lock held by a live concurrent instance, or errors > 0 after
       processing all pairs (fail-loud, matches oracle's `exit 1` on `$errors -gt 0`).
    (transport/import failure — engine-root resolution or `coordinator_core` import
    failing before this module is even reached — is NOT a code this function can
    return; it is handled by the DoE trampoline itself, which uses a DEDICATED exit
    code 2 for that case per the porter addendum §3b fail-loud-gate-script rule,
    since this tool is a mutating fail-loud CLI, not a best-effort/never-block one.)

Known limitation — PID-reuse hazard (RAW-PID-LIVENESS tripwire, faithfully carried
over from the oracle, NOT fixed by this port):
    The concurrency guard probes lock-holder liveness via `os.kill(pid, 0)` (Python
    equivalent of the oracle's `kill -0 <pid>`). PID values are reused by the OS;
    after a crash the stored PID may belong to an unrelated process, causing a live
    lock to appear stale (or vice-versa). There is no portable fix for PID reuse in
    either the bash or Python transport. If the lock holder belongs to a different
    user, `os.kill(pid, 0)` raises `PermissionError` — this module treats that
    identically to "process not running" (faithful bug repro of the oracle's own
    `kill -0 ... 2>/dev/null` short-circuit, which does the same thing), so a live
    foreign-owned lock can be incorrectly treated as stale and overwritten. If the
    lock appears stuck, remove it manually:
    `rm -f "$(python3 -c 'import tempfile;print(tempfile.gettempdir())')/backfill-initiative-fk.lock"`.

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
    - Does NOT auto-create initiatives; run `coordinator-initiative create` first.
    - Does NOT modify the mapping file in-place.
    - Does NOT read from multiple mapping files in one invocation.
    - Does NOT implement concurrency via a genuine OS file lock (`fcntl.flock` etc.)
      — uses the same PID-in-lockfile pattern as the oracle, RAW-PID-LIVENESS warts
      and all. A `flock`-based rewrite would be a genuine correctness improvement
      but is explicitly OUT of scope for a faithful port.
    - Does NOT implement `--help`/usage text (the oracle has none either — its header
      comment IS the usage doc, reproduced above).
    - The idempotency check builds a regex directly from the caller-supplied
      `initiative_id` WITHOUT escaping it (faithful repro of the oracle's
      `grep -qE "^initiative: ${initiative_id}[[:space:]]*$"`, which is likewise an
      unescaped extended-regex interpolation) — an `initiative_id` containing regex
      metacharacters is interpreted as a pattern, not a literal string, in BOTH the
      oracle and this port. Not fixed; faithfully reproduced.
    - Field-trim semantics mirror the oracle's narrow `${var#"${var%%[! ]*}"}` /
      `${var%"${var##*[! ]}"}` pattern exactly: only literal ASCII space (0x20) runs
      are stripped from each field's ends — tabs, CR, and other whitespace are left
      untouched (the oracle's grep pattern uses `[[:space:]]` in a different place,
      the idempotency check, but the field-trim itself is space-only).

Departure from the oracle (additive robustness, not a behavior change to any tested
path):
    - Invokes `coordinator-initiative` via `[sys.executable, coordinator_initiative_path,
      ...]` rather than shebang-exec of a bare path. `coordinator-initiative` was ported
      from bash to python3 (DoE-claude commit 6fb5fb37); invoking the running
      interpreter directly needs NO shebang interpretation at all — strictly stronger
      than the earlier bash-resolution approach's Windows-portability workaround (no
      shebang interpretation on Windows was the old bash-resolution rationale; this
      approach sidesteps the shebang question entirely on every platform).
    - Batches ALL pairs surviving the per-line `_already_attached` idempotency
      filter into ONE `coordinator-initiative attach --pairs-file` subprocess call
      (`_attach_batch`) instead of spawning one subprocess per pair (the oracle's
      shape, faithfully carried by this port until now). Per-pair success/failure
      is still individually attributed via the batch call's JSON-lines stdout,
      matched back to each pair positionally — see `_attach_batch`'s docstring for
      the one traded-off property (a batch-wide timeout now fails every pair still
      in flight, not just the one that hung).
"""

from __future__ import annotations

import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import IO, Iterable, List, Optional, Tuple
from coordinator_core.ipc import DISPATCH_TIMEOUT_SECS
from coordinator_core.win_portability import no_console_creationflags


# writes only a PID lockfile at tempfile.gettempdir()/_LOCK_BASENAME --
GENERATES = []

_CREATIONFLAGS = no_console_creationflags()

_PROG = "backfill-initiative-fk"
_LOCK_BASENAME = "backfill-initiative-fk.lock"
# NEGATIVE SPEC (DR-349 § "Dials that raise themselves", 2026-08-21): this bound is
_ATTACH_TIMEOUT_SECS = DISPATCH_TIMEOUT_SECS


def _lock_path() -> str:
    return os.path.join(tempfile.gettempdir(), _LOCK_BASENAME)


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        print(f"skip: _pid_is_live: os.kill(pid, 0) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    except (OSError, ValueError):
        print(f"skip: _pid_is_live: os.kill(pid, 0) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return True


def _acquire_lock(lock_path: str, stream: IO[str] = sys.stderr) -> Tuple[bool, int]:
    my_pid = os.getpid()

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        print(f"skip: _acquire_lock: fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644) failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass
    else:
        with os.fdopen(fd, "w", newline="\n") as fh:
            fh.write(f"{my_pid}\n")
        return True, 0

    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            held_pid_raw = fh.read().strip()
    except OSError:
        held_pid_raw = ""

    held_pid: Optional[int] = None
    if held_pid_raw:
        try:
            held_pid = int(held_pid_raw)
        except ValueError:
            held_pid = None

    if held_pid is not None and _pid_is_live(held_pid):
        print(
            f"{_PROG}: another instance is running (PID {held_pid}, lockfile {lock_path}).",
            file=stream,
        )
        print("  Wait for it to finish, or remove the lockfile if it crashed.", file=stream)
        return False, 1

    print(
        f"{_PROG}: removing stale lockfile (PID {held_pid_raw or 'unknown'} no longer running).",
        file=stream,
    )
    with open(lock_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{my_pid}\n")
    return True, 0


def _release_lock(lock_path: str) -> None:
    my_pid = str(os.getpid())
    try:
        with open(lock_path, "r", encoding="utf-8") as fh:
            lock_pid = fh.read().strip()
    except OSError:
        print(f"skip: _release_lock: with open(lock_path, \"r\", encoding=\"utf-8\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return
    if lock_pid == my_pid:
        try:
            os.remove(lock_path)
        except OSError:
            print(f"skip: _release_lock: os.remove(lock_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass


def _strip_spaces(value: str) -> str:
    return value.strip(" ")


def _already_attached(artifact_path: str, initiative_id: str) -> bool:
    pattern = re.compile(r"^initiative: " + initiative_id + r"\s*$")
    try:
        with open(artifact_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if pattern.match(line.rstrip("\n")):
                    return True
    except OSError:
        print(f"skip: _already_attached: with open(artifact_path, \"r\", encoding=\"utf-8\", errors=\"replace\") as f failed: {sys.exc_info()[1]}", file=sys.stderr)
        return False
    return False


def _attach_batch(
    pairs: List[Tuple[int, str, str]],
    coordinator_initiative_path: str,
    err: IO[str],
) -> Tuple[int, int]:
    """Attach every `(line_no, artifact_path, initiative_id)` in `pairs` via ONE
    `coordinator-initiative attach --pairs-file` subprocess call — N pairs, one
    spawn, not N (the amplification-gate fix this function exists for; see
    `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`'s exemption
    register entry for `_process_pairs::run`, now refuted and discharged).

    Writes `pairs` to a temp TSV pairs-file (same format `coordinator-initiative
    attach --pairs-file` documents), invokes the CLI once, then matches its
    JSON-lines stdout back to the ORIGINAL pairs POSITIONALLY — the CLI emits
    exactly one JSON line per non-blank/non-comment pairs-file line, in the same
    order it read them, and this function writes no blank/comment lines into the
    pairs-file, so a 1:1 zip is exact absent a subprocess crash mid-batch.

    Returns `(attached, errors)`. `skipped` is never produced here — the caller
    filters already-attached pairs out via `_already_attached` before batching, so
    every pair reaching this function is a genuine attach attempt.

    A batch-wide timeout (or a crash that exits before emitting a pair's JSON
    line) is attributed to EVERY pair still unmatched, not silently dropped —
    preserves the "collect all failures, name every failed pair" contract at the
    cost of the single-pair path's per-pair timeout isolation: one hung pair in a
    batch fails the whole batch, where the old per-pair-spawn loop only lost the
    one hung pair. Documented tradeoff of collapsing N spawns into one, not an
    oversight.

    The one spawn is bounded by the FLAT `_ATTACH_TIMEOUT_SECS` — see that
    constant's negative spec for why nothing here scales with `len(pairs)`.
    """
    tmp_fd, pairs_file = tempfile.mkstemp(suffix=".tsv", prefix="backfill-initiative-fk-pairs-")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8", newline="\n") as fh:
            for _, artifact_path, initiative_id in pairs:
                fh.write(f"{artifact_path}\t{initiative_id}\n")

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    coordinator_initiative_path,
                    "attach",
                    "--pairs-file",
                    pairs_file,
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=_ATTACH_TIMEOUT_SECS,
                **_CREATIONFLAGS,
            )
            stdout_text = result.stdout or ""
        except subprocess.TimeoutExpired:
            stdout_text = ""
    finally:
        try:
            os.remove(pairs_file)
        except OSError:
            pass

    result_lines = [ln for ln in stdout_text.splitlines() if ln.strip()]
    parsed_records: List[Optional[dict]] = []
    for result_line in result_lines:
        try:
            parsed_records.append(json.loads(result_line))
        except ValueError:
            parsed_records.append(None)

    attached = 0
    errors = 0
    for (line_no, artifact_path, initiative_id), record in itertools.zip_longest(
        pairs, parsed_records, fillvalue=None
    ):
        if line_no is None:
            break
        if record is None:
            print(
                f"{_PROG}: FAILED pair (no batch result reported): "
                f"{artifact_path} -> {initiative_id}",
                file=err,
            )
            errors += 1
            continue
        if record.get("ok"):
            attached += 1
            continue
        print(
            f"{_PROG}: FAILED pair: {artifact_path} -> {initiative_id} "
            f"({record.get('error', 'unknown error')})",
            file=err,
        )
        errors += 1

    return attached, errors


def _process_pairs(
    lines: Iterable[str],
    coordinator_initiative_path: str,
    out: Optional[IO[str]] = None,
    err: Optional[IO[str]] = None,
) -> Tuple[int, int, int]:
    """Process TSV `(artifact_path, initiative_id)` pairs. Returns
    `(attached, skipped, errors)`. Collects ALL failures rather than stopping at the
    first (matches oracle: "Continue processing remaining pairs; collect all
    failures before exiting.").

    Idempotency filtering (`_already_attached`) stays a per-line, in-process check
    — no subprocess involved — so it is not a batching concern. Every pair that
    survives that filter is queued and attached via ONE `_attach_batch` call at the
    end, rather than one `coordinator-initiative attach` subprocess per pair.

    `out`/`err` default to `None`, resolved to the CURRENT `sys.stdout`/`sys.stderr`
    at call time rather than bound as `sys.stdout`/`sys.stderr` at function-DEFINITION
    time (module import). A definition-time default captures whatever stream object
    was live at import — which, under a test harness that swaps `sys.stdout`/
    `sys.stderr` for capture (pytest's capsys/capfd), is a stale reference: writes
    through it land nowhere the test can observe, even though `print(x)` (no `file=`)
    resolves `sys.stdout` fresh on every call and is unaffected. Caught by the
    `--pairs-file` batch path, whose per-pair failure text now flows exclusively
    through this parameter (the pre-batching per-pair loop's failure text mostly came
    from the CHILD process's own inherited-fd stderr, which never touched this stale
    reference at all).
    """
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    attached = 0
    skipped = 0
    errors = 0
    line_no = 0
    to_attach: List[Tuple[int, str, str]] = []

    for raw_line in lines:
        line_no += 1
        line = raw_line.rstrip("\n")

        if "\t" in line:
            artifact_path, rest = line.split("\t", 1)
        else:
            artifact_path, rest = line, ""

        artifact_path = _strip_spaces(artifact_path)
        initiative_id = _strip_spaces(rest)
        initiative_id = initiative_id.split("\t", 1)[0]

        if not artifact_path:
            continue
        if artifact_path.startswith("#"):
            continue

        if not initiative_id:
            print(
                f"{_PROG}: line {line_no}: missing initiative-id for artifact: {artifact_path}",
                file=err,
            )
            errors += 1
            continue

        if _already_attached(artifact_path, initiative_id):
            print(
                f"skipped (already attached): {artifact_path} -> initiative: {initiative_id}",
                file=out,
            )
            skipped += 1
            continue

        to_attach.append((line_no, artifact_path, initiative_id))

    if to_attach:
        batch_attached, batch_errors = _attach_batch(to_attach, coordinator_initiative_path, err)
        attached += batch_attached
        errors += batch_errors

    return attached, skipped, errors


def main(argv: List[str], script_dir: Optional[str] = None) -> int:
    argv = list(argv)
    if script_dir is None:
        for i, arg in enumerate(argv):
            if arg == "--script-dir" and i + 1 < len(argv):
                script_dir = argv[i + 1]
                del argv[i : i + 2]
                break
            if arg.startswith("--script-dir="):
                script_dir = arg.split("=", 1)[1]
                del argv[i]
                break

    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))

    coordinator_initiative_path = os.path.join(script_dir, "coordinator-initiative.py")
    if not os.path.isfile(coordinator_initiative_path):
        coordinator_initiative_path = os.path.join(script_dir, "coordinator-initiative")
    if not os.path.isfile(coordinator_initiative_path):
        print(
            f"{_PROG}: coordinator-initiative not found at "
            f"{coordinator_initiative_path}",
            file=sys.stderr,
        )
        print("  Ensure the coordinator plugin is fully installed.", file=sys.stderr)
        return 1

    lock_path = _lock_path()
    acquired, lock_rc = _acquire_lock(lock_path)
    if not acquired:
        return lock_rc

    try:
        input_src = argv[0] if argv else "-"

        if input_src == "-":
            attached, skipped, errors = _process_pairs(
                sys.stdin, coordinator_initiative_path
            )
        else:
            if not os.path.isfile(input_src):
                print(f"{_PROG}: mapping file not found: {input_src}", file=sys.stderr)
                return 1
            with open(input_src, "r", encoding="utf-8", errors="replace") as fh:
                attached, skipped, errors = _process_pairs(
                    fh, coordinator_initiative_path
                )

        print(f"{_PROG}: done — attached={attached} skipped={skipped} errors={errors}")

        if errors > 0:
            return 1
        return 0
    finally:
        _release_lock(lock_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
