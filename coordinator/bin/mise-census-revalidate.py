"""mise-census-revalidate — the census leg of fire-time revalidation. Writes nothing.

WHY THIS EXISTS. `coordinator-content-repo coordinator/docs/wiki/lesson-triage/mise-prepped-attest.md` § "Fire-time revalidation is ONE step, not
two" specifies two ordered legs: recompute the plan body sha, and — only if that passes — re-run
each `census[].command` and diff against `result`. The first leg is `mise-prep-gate.py` and the
engine's `prep_gate`. The second was specified, assigned to "the RUNNER", and never built, so
every hands-off run so far has fired on a sha check alone. That is the weaker half: the sha says
the DOCUMENT has not moved, and the census says the WORLD has not. A plan is stale in the second
sense far more often, because planning runs waves ahead of execution by construction.

THE TWO LEGS ARE ORDERED, AND THIS ONE IS SECOND. The sha leg is spawn-free and sub-millisecond;
this one spawns a process per declared command. Running it against a plan whose body already
failed the cheap leg spends the box's budget re-measuring premises of a document that is no
longer the certified one. This module therefore REFUSES a plan that is not CERTIFIED, rather
than reporting on it. The one caller that opts out is mise-prep-run (`require_stamp=False`): it
runs this leg on a plan it is ABOUT to stamp, so the sha leg is the gate that plan just passed.

WHAT IT WILL NOT DO, and the reason is the whole design:

  IT NEVER GUESSES A VERDICT. `census[].result` is free text — the bar requires the key to be
  present and never constrains its shape — so "diff against result" is not mechanically total.
  Measured over example-retrieval-repo's 25 certified plans: 189 entries, 17% a bare integer, 55% leading
  with an integer then prose, 28% prose throughout. So this reports MATCH only on POSITIVE
  evidence, DRIFT only on positive counter-evidence, and names everything else UNDECIDABLE with
  both texts printed. An UNDECIDABLE is not a pass and must never be read as one; it is the
  honest shape of a question this leg cannot close by itself.

  IT NEVER PASSES A PLAN ON ZERO MEASUREMENTS. A `census:` key declaring no entries revalidates
  nothing, so it is REFUSED rather than rolled up. The roll-up decides on set difference and an
  empty entry set differs from nothing, so an empty census would otherwise report MATCH -- this
  leg's strongest verdict, earned by measuring nothing, and indistinguishable on the wire from a
  plan whose every counted premise was re-checked and held.

  A TYPED ENTRY (`kind:` instead of `command:`) IS EVALUATED IN-PROCESS by
  `census.evaluate_typed`: one directory listing, no shell, no spawn.

  IT NEVER RUNS A COMMAND THAT COULD MUTATE. A census command is a QUESTION, and these are
  strings lifted out of plan frontmatter that agents wrote. A leg that shells out to whatever it
  finds there would make "certify this plan" an arbitrary-execution surface, on a tree many
  sessions share. Every command is screened against a read-only allowlist first; one that does
  not clear it is REFUSED by name and never executed. The screen is
  `coordinator_core.roadmap.census.screen`, the same function the prep gate refuses against at
  stamp time, so a REFUSED here means a plan stamped before that gate existed.

  A `count:` BESIDE `result:` IS DECIDABLE. An entry declaring `count: <int>` is compared on the
  number alone -- MATCH or DRIFT, never UNDECIDABLE -- and `result` is carried as the note. This
  is what closes "12 (prose)" against an observed 13.

Exit status is a verdict, not a diagnostic, because the caller of this leg is a gate:
  0  every entry MATCHed — the counted premises still hold
  1  at least one DRIFT — the premise moved; that plan re-plans rather than fires
  2  no DRIFT, but at least one UNDECIDABLE, REFUSED or UNRUNNABLE — the leg did not close
  3  usage
Precedence is DRIFT over the unclosed classes: a premise known to have moved outranks one whose
status is unknown, because the two route differently and the first is actionable now.

PATH RESOLUTION — SESSION REPO, EXPLICIT. `--repo-root` is required and every path below it is
caller-supplied; this module derives no path from its own `__file__` and needs no engine or
plugin-root resolution (`docs/plans/2026-09-18-doe-holds-no-scripts.md` § Path resolution).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md, chunk W3-C2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat as _stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, NamedTuple, Optional, Union

try:
    import resource
except ImportError:
    resource = None

def _load_yaml():
    """Import PyYAML lazily -- a module-scope `try: import yaml / except ImportError: print(...);
    raise SystemExit(3)` is not the permitted-try shape (`coordinator_core.warm.serve_classifier`
    exempts only a body of imports/assignments; the `print`+`raise` here is neither), so the
    dependency guard moves here, called once by `revalidate()`."""
    try:
        import yaml
    except ImportError:  # pragma: no cover - dependency guard
        print("mise-census-revalidate: PyYAML unavailable", file=sys.stderr)
        raise SystemExit(3)
    return yaml


MATCH = "MATCH"
DRIFT = "DRIFT"
UNDECIDABLE = "UNDECIDABLE"
REFUSED = "REFUSED"
UNRUNNABLE = "UNRUNNABLE"

EXIT_MATCH = 0
EXIT_DRIFT = 1
EXIT_UNCLOSED = 2
EXIT_USAGE = 3

_CENSUS_MODULE: Any = None


def _census():
    """`coordinator_core.roadmap.census` -- the screen the prep gate stamps against, so a stamped
    premise is one this leg will run. Resolved on first call, never at import, so the module body
    stays inert for `serve_classifier`. Cached: the set pass calls it per entry, and the uncached
    path (`require_engine_on_path`, machine profile) cost ~2s of process time over a 153-plan set."""
    global _CENSUS_MODULE
    if _CENSUS_MODULE is not None:
        return _CENSUS_MODULE
    own_dir = str(Path(__file__).resolve().parent)
    if own_dir not in sys.path:
        sys.path.insert(0, own_dir)
    import lib  # noqa: F401 -- bootstraps coordinator/bin/lib onto sys.path
    import cc_invoke

    cc_invoke.require_engine_on_path(__file__)
    from coordinator_core.roadmap import census

    _CENSUS_MODULE = census
    return census


def _operands():
    """`coordinator_core.roadmap.census_operands`, resolved after `_census()` has put the engine
    on the path."""
    _census()
    from coordinator_core.roadmap import census_operands

    return census_operands


def _inproc():
    """`coordinator_core.roadmap.census_inproc`, resolved after `_census()` has put the engine on
    the path; `evaluate` is read off the module at call time."""
    _census()
    from coordinator_core.roadmap import census_inproc

    return census_inproc


def screen(command: str, repo_root: Optional[Path] = None) -> Optional[dict]:
    """`None` when the command is read-only; a REFUSED row otherwise."""
    reason = _census().screen(command, repo_root)
    return None if reason is None else {"state": REFUSED, "detail": reason}


#: One `grep -c` line over several files: any path (a Windows drive letter's colon included, since
#: the match anchors on the TRAILING `:N`), then the count.
_PER_FILE_COUNT = re.compile(r"^.+:(\d+)$")


def compare(recorded: str, stdout: str) -> dict:
    """MATCH on positive evidence, DRIFT on positive counter-evidence, UNDECIDABLE otherwise.

    Three decidable shapes, in order of strength. Each is a claim about the RECORDED text, which
    is the only thing whose shape this leg controls:

      EXACT     the recorded result IS the output, modulo surrounding whitespace.
      CONTAINS  the output appears verbatim inside the recorded result — the shape an author
                writes when they paste the answer and then explain it.
      COUNT     the recorded result leads with an integer N and the output has N non-empty
                lines. Covers the dominant `grep`/`ls`/`find` idiom.

    A recorded result leading with an integer whose line count DISAGREES is the one shape that
    earns DRIFT, because the author's own leading number is a claim about how many things there
    were and there are now a different number. Everything else is UNDECIDABLE: a prose result
    can be right, wrong, or about something the output does not mention, and this leg has no way
    to tell those apart. Saying so is the point.
    """
    rec = recorded.strip()
    out = stdout.strip()
    if rec == out:
        return {"state": MATCH, "basis": "exact"}
    if out and out in rec:
        return {"state": MATCH, "basis": "contains"}
    lead = re.match(r"^(-?\d+)\b", rec)
    if lead:
        want = int(lead.group(1))
        # A leading integer is a claim about a QUANTITY, and there are two idioms for where the
        # quantity lives in the output. `grep -n …` puts it in the line COUNT; `wc -l`, `grep -c`
        # and `git rev-list --count` put it in the output's own leading integer. Reading only the
        # first made every `wc -l` census a DRIFT — recorded "142 lines", observed "142 file.py",
        # one output line, 142 != 1. The number agreed; the reading did not.
        out_lead = re.match(r"^\s*(-?\d+)\b", out)
        if out_lead and int(out_lead.group(1)) == want:
            return {"state": MATCH, "basis": f"leading count={want}"}
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if len(lines) == want:
            return {"state": MATCH, "basis": f"line count={want}"}
        # Multi-file `grep -c` prints `path:N` per file, so the recorded figure is the SUM.
        per_file = [_PER_FILE_COUNT.match(ln.strip()) for ln in lines]
        if per_file and all(per_file) and sum(int(m.group(1)) for m in per_file) == want:
            return {"state": MATCH, "basis": f"per-file count sum={want}"}
        # A bare recorded value is a VALUE (`grep -n` of a limit), not a count: present as a
        # whole token in the output means it still holds. Absence falls through to the bare-number
        # DRIFT rule below, so prose rows gain no new DRIFT source. The `path:LINE:` prefix of
        # `grep -n` output is stripped first: a line number equal to a recorded COUNT is not
        # evidence the count holds.
        body = "\n".join(re.sub(r"^.*?:\d+:", "", ln) for ln in out.splitlines())
        if re.fullmatch(r"-?\d+", rec) and re.search(rf"(?<![\w.]){re.escape(rec)}(?![\w.])", body):
            return {"state": MATCH, "basis": f"recorded value {want} present in output"}
        # DRIFT ONLY WHERE THE RECORDED TEXT IS NOTHING BUT THE NUMBER. A bare `3799` can only
        # be the answer, so a different answer is counter-evidence. A number followed by prose
        # is not reliably a count at all: measured across this corpus it is as often a LINE
        # NUMBER (`7` in "7 — the CONSUMED_FIELDS_VERSION line"), an entry tally against a
        # multi-line print, or a figure quoted from the body. Calling those DRIFT sent seven
        # plans to re-plan over a comparator's guess about what the author's first token meant.
        # Unknown is the honest verdict, and the pair is printed so a reader can close it.
        if re.fullmatch(r"-?\d+", rec):
            observed = out_lead.group(1) if out_lead else f"{len(lines)} line(s)"
            return {"state": DRIFT, "basis": f"recorded {want}, observed {observed}"}
        return {
            "state": UNDECIDABLE,
            "basis": (
                f"recorded leads with {want} then prose; output leads with "
                f"{out_lead.group(1) if out_lead else 'no integer'} over {len(lines)} line(s) "
                "— cannot tell whether the recorded integer is a count, a line number or a quote"
            ),
        }
    return {"state": UNDECIDABLE, "basis": "recorded result is prose; no decidable comparison"}


#: A census command string is POSIX shell. `screen()` parses it with `shlex` in POSIX mode, and
#: the corpus's dominant idioms are POSIX: `grep -rln 'a\|b'`, `;` as a separator, `2>/dev/null`.
#: `shell=True` inherits the platform's interpreter, which is `cmd.exe` on Windows -- it splits
#: `'a\|b'` mid-quote and treats `;` as a literal, so the command fails to parse rather than
#: failing to match. That reads out as UNRUNNABLE and is indistinguishable from a premise that
#: moved. It is the same mid-quote cut `screen()`'s own docstring records fixing one layer down:
#: the screen learned POSIX, the executor had not. So the interpreter is named, never inherited.
_SHELL_ENV_OVERRIDE = "COORDINATOR_CENSUS_SHELL"

#: `bash.exe` under Windows' System32 is the WSL launcher, not a shell on this filesystem: it
#: cannot accept a Windows `cwd`, so every entry would fail on the directory rather than the
#: command. Skipped by directory, because its basename is identical to a real one's.
_WSL_SHELL_MARKER = "system32"


def resolve_posix_shell() -> Optional[str]:
    """Absolute path to a POSIX shell, or `None` when the host has none.

    Ladder, most authoritative first: an explicit `COORDINATOR_CENSUS_SHELL` override used AS-IS,
    then `bash` on PATH, then `sh`. `None` is a real answer on a bare Windows host and routes to
    an UNRUNNABLE row naming the missing interpreter -- never to a silent fallback that would put
    `cmd.exe` back in the path this function exists to take it out of.
    """
    override = os.environ.get(_SHELL_ENV_OVERRIDE)
    if override:
        return override
    for name in ("bash", "sh"):
        found = shutil.which(name)
        if found and _WSL_SHELL_MARKER not in found.replace("\\", "/").lower():
            return found
    return None


def _no_console_kw() -> dict:
    """CREATE_NO_WINDOW kwargs for a captured child on Windows; empty elsewhere."""
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def plan_rel_path(plan: Path, repo_root: Path) -> Optional[str]:
    """The plan's repo-relative POSIX path, or `None` when it lies outside `repo_root`."""
    try:
        return plan.resolve().relative_to(repo_root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def drop_own_path(out: str, plan_rel: Optional[str]) -> str:
    """Remove output lines that are exactly the revalidated plan's own path.

    A plan-scoped `git grep -l` census lists the plan itself once committed, so the count drifts
    by one by construction. Only a whole line equal to the plan path is dropped; any other path,
    and any line that merely contains it, is left to the comparison.
    """
    if not plan_rel or plan_rel not in out:
        return out
    own = {plan_rel, "./" + plan_rel}
    return "".join(ln for ln in out.splitlines(keepends=True) if ln.strip() not in own)


def run_entry(entry: dict, repo_root: Path, timeout: int, plan_rel: Optional[str] = None) -> dict:
    """One census entry's row. Four-argument signature is fixed: callers fake it."""
    command = str(entry.get("command") or "").strip()
    row: dict[str, Any] = {"question": str(entry.get("question") or ""), "command": command}
    census = _census()
    if census.is_typed(entry):
        row["command"] = census.describe_typed(entry)
        row.update(census.evaluate_typed(entry, repo_root))
        row["recorded"] = f"claims {entry.get('claims')}"
        return row
    if not command:
        row.update({"state": REFUSED, "detail": "entry declares no command"})
        return row
    refusal = screen(command, repo_root)
    if refusal:
        row.update(refusal)
        return row
    ran = _spawn(command, repo_root, timeout)
    if isinstance(ran, dict):
        row.update(ran)
        return row
    return _judge(entry, row, ran, plan_rel)


class _LegCost:
    """Process cost of one leg from one start: this process's CPU plus the CPU of the children it
    reaped, and the number of shells spawned.

    POSIX reads `getrusage(RUSAGE_CHILDREN)`, which is process-wide: under a multi-threaded warm
    engine another thread's reaped child lands in the delta, so the figure can over-count. That is
    the conservative direction for a ceiling. Windows reads each batch shell's Job object after it
    exits. `last_run_ms` holds the per-command wall ms of the latest `_spawn_many` call.
    """

    def __init__(self) -> None:
        self._parent0 = time.process_time()
        self._job_s = 0.0
        self.spawns = 0
        self.last_run_ms: list[int] = []
        self._children0 = self._children_s()

    def _children_s(self) -> float:
        if os.name == "nt":
            return self._job_s
        if resource is None:
            return 0.0
        ru = resource.getrusage(resource.RUSAGE_CHILDREN)
        return ru.ru_utime + ru.ru_stime

    @property
    def children_ms(self) -> int:
        return int(round((self._children_s() - self._children0) * 1000))

    def total_s(self) -> float:
        return (time.process_time() - self._parent0) + (self._children_s() - self._children0)


def _unrunnable(detail: str) -> dict:
    return {"state": UNRUNNABLE, "detail": detail}


def _drain(stream, sink: list) -> None:
    """Reader thread body: appends `(arrival, chunk)` until EOF, so a marker's arrival time stamps
    the end of its command."""
    try:
        while True:
            chunk = stream.read1(65536)
            if not chunk:
                return
            sink.append((time.perf_counter(), chunk))
    except (OSError, ValueError):
        return


def _batch_script(nonce: str, commands: list[str]) -> str:
    """One shell program running each command in its own subshell, bracketed on stdout and stderr
    by nonce markers carrying the index and `$?`. The newline before an end marker is the
    separator the parser strips, which is how output with no trailing newline stays exact."""
    lines: list[str] = []
    for i, command in enumerate(commands):
        begin = f"@@{nonce}:B:{i}@@"
        lines.append(f"printf '%s\\n' '{begin}'; printf '%s\\n' '{begin}' >&2")
        lines.append("(")
        lines.append(command)
        lines.append(")")
        lines.append("rc=$?")
        end = f"@@{nonce}:E:{i}:%s@@"
        lines.append(f"printf '\\n{end}\\n' \"$rc\"; printf '\\n{end}\\n' \"$rc\" >&2")
    return "\n".join(lines) + "\n"


def _parse_batch(nonce: str, n: int, out: bytes, err: bytes) -> list[Optional[tuple]]:
    """Per command `(rc, stdout, stderr, end_offset_in_out)`, or None for a command whose end
    marker was not seen on both streams."""

    def spans(buf: bytes) -> list[Optional[tuple]]:
        found: list[Optional[tuple]] = []
        pos = 0
        for i in range(n):
            begin = f"@@{nonce}:B:{i}@@\n".encode()
            if not buf.startswith(begin, pos):
                break
            start = pos + len(begin)
            end = re.compile(rb"\n@@" + nonce.encode() + rb":E:" + str(i).encode() + rb":(\d+)@@\n")
            m = end.search(buf, start)
            if m is None:
                break
            found.append((int(m.group(1)), buf[start:m.start()], m.end()))
            pos = m.end()
        return found + [None] * (n - len(found))

    so, se = spans(out), spans(err)
    return [
        None if a is None or b is None else (a[0], a[1], b[1], a[2])
        for a, b in zip(so, se)
    ]


def _reap_into_cost(cost: _LegCost, job: Any) -> None:
    """Windows: fold the batch Job's user+kernel time (100ns ticks) into `cost`."""
    pt = _process_time_module()
    info = pt._windows_query_job_accounting(job)
    cost._job_s += (info.TotalUserTime + info.TotalKernelTime) / 1e7
    pt._k32.CloseHandle(job)


def _process_time_module():
    """`coordinator_core.benchmarks.process_time`, imported on first use and only on Windows,
    for the Job-object helpers it keeps module-private."""
    _census()
    import importlib

    return importlib.import_module("coordinator_core.benchmarks.process_time")


def _start_shell(shell: str, script: str, repo_root: Path):
    """Popen the batch shell; returns `(proc, job)` with `job` None off Windows. On Windows the
    shell starts suspended and joins a Job before it can run, so its descendants are accounted."""
    argv = [shell, "-c", script]
    common = dict(
        shell=False, cwd=repo_root, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **common), None
    from ctypes import wintypes

    pt = _process_time_module()
    job = pt._k32.CreateJobObjectW(None, None)
    proc = subprocess.Popen(
        argv, creationflags=pt._CREATE_SUSPENDED | pt._CREATE_NO_WINDOW, **common
    )
    pt._k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    pt._resume_all_threads(proc.pid)
    return proc, job


def _kill_tree(proc, job: Any) -> None:
    try:
        if os.name == "nt":
            from ctypes import wintypes

            _process_time_module()._k32.TerminateJobObject(wintypes.HANDLE(job), 1)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, AttributeError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _spawn_batch(
    commands: list[str], repo_root: Path, timeout: int, cost: _LegCost
) -> list[Union[tuple, dict]]:
    shell = resolve_posix_shell()
    if not shell:
        return [_unrunnable(
            "no POSIX shell on PATH (tried bash, sh); set "
            f"{_SHELL_ENV_OVERRIDE} to one"
        )] * len(commands)
    nonce = os.urandom(8).hex()
    n = len(commands)
    script = _batch_script(nonce, commands)
    started = time.perf_counter()
    try:
        proc, job = _start_shell(shell, script, repo_root)
    except OSError as exc:
        return [_unrunnable(str(exc))] * n
    cost.spawns += 1
    out_sink: list = []
    err_sink: list = []
    readers = [
        threading.Thread(target=_drain, args=(proc.stdout, out_sink), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, err_sink), daemon=True),
    ]
    for t in readers:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc, job)
        proc.wait()
    for t in readers:
        t.join(timeout=2)
    if job is not None:
        _reap_into_cost(cost, job)
    out = b"".join(c for _t, c in out_sink)
    err = b"".join(c for _t, c in err_sink)
    parsed = _parse_batch(nonce, n, out, err)
    ends, total = [], 0
    for t, chunk in out_sink:
        total += len(chunk)
        ends.append((total, t))
    results: list[Union[tuple, dict]] = []
    run_ms: list[int] = []
    prev = started
    for item in parsed:
        if item is None:
            break
        rc, so, se, end_at = item
        stamp = next((t for upto, t in ends if upto >= end_at), time.perf_counter())
        run_ms.append(max(0, int(round((stamp - prev) * 1000))))
        prev = stamp
        results.append((rc, so.decode("utf-8", "replace"), se.decode("utf-8", "replace")))
    done = len(results)
    if done < n:
        if timed_out:
            results.append(_unrunnable(f"timed out after {timeout}s"))
            results.extend({"state": UNRUNNABLE, "detail": _DEADLINE_SPENT} for _ in range(n - done - 1))
        else:
            results.extend(
                _unrunnable(f"batch shell ended early (exit {proc.returncode})")
                for _ in range(n - done)
            )
        run_ms.extend(0 for _ in range(n - done))
    cost.last_run_ms = run_ms
    return results


def _spawn_many(
    commands: list[str], repo_root: Path, timeout: int, cost: _LegCost
) -> list[Union[tuple, dict]]:
    """Run screened commands in ONE shell, each in its own subshell, and return one result per
    command in input order: `(returncode, stdout, stderr)`, or an UNRUNNABLE `{state, detail}`.

    `timeout` bounds the whole batch. A timeout kills the batch: the command in flight reads
    "timed out" and the commands not yet started read `_DEADLINE_SPENT`; neither is storable.
    A substituted module-level `_spawn` is honoured command by command, which is the seam the
    spawn-counting tests wrap. `_spawn` itself stays one `subprocess.run` per command: the
    single-plan `revalidate` path and its tests pin that.
    """
    if not commands:
        cost.last_run_ms = []
        return []
    if _spawn is not _SINGLE_SPAWN:
        results, run_ms = [], []
        for command in commands:
            started = time.perf_counter()
            results.append(_spawn(command, repo_root, timeout))
            run_ms.append(_ms_since(started))
            cost.spawns += 1
        cost.last_run_ms = run_ms
        return results
    return _spawn_batch(commands, repo_root, timeout, cost)


def _spawn(command: str, repo_root: Path, timeout: int):
    """Run one screened command. A completed run is `(returncode, stdout, stderr)`; a run that
    could not complete (no shell, timeout, OSError) is an UNRUNNABLE `{state, detail}` dict."""
    shell = resolve_posix_shell()
    if not shell:
        return _unrunnable(
            "no POSIX shell on PATH (tried bash, sh); set "
            f"{_SHELL_ENV_OVERRIDE} to one"
        )
    try:
        proc = subprocess.run(
            [shell, "-c", command],
            shell=False,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
            **_no_console_kw(),
        )
    except subprocess.TimeoutExpired:
        return _unrunnable(f"timed out after {timeout}s")
    except OSError as exc:
        return _unrunnable(str(exc))
    return proc.returncode, proc.stdout, proc.stderr


_SINGLE_SPAWN = _spawn


def _judge(entry: dict, row: dict, ran: tuple, plan_rel: Optional[str]) -> dict:
    """Fold a completed run's output into `row` against the entry's own recorded result."""
    returncode, stdout, stderr = ran
    recorded = str(entry.get("result") or "")
    out = drop_own_path(stdout, plan_rel)
    # A non-zero exit with NO output is a command that could not answer; a non-zero exit WITH
    # output is ordinary (`grep` exits 1 on no match, which is itself an answer of "zero").
    if returncode != 0 and not out.strip():
        row.update({
            "state": UNRUNNABLE,
            "detail": f"exit {returncode}: {(stderr or '').strip()[:200]}",
        })
        return row
    count = entry.get("count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else None
    verdict = _census().same_value(count, recorded, out)
    if verdict is None:
        rec_n, rec_v = _census().split_count(recorded)
        count = count if count is not None else rec_n
        if count is not None:
            verdict = _census().compare_count(count, out)
        else:
            verdict = compare(recorded, out)
    row.update(verdict)
    shown_n, shown_v = _census().split_count(recorded)
    shown_n = entry["count"] if "count" in entry else shown_n
    row["recorded"] = (f"count {shown_n}; " if shown_n is not None else "") + shown_v[:400]
    row["observed"] = out.strip()[:400]
    return row


_DEADLINE_SPENT = "not run: the op's census deadline was spent before this entry"
_CANNOT_DRIFT = "not run: recorded result cannot drift"


def can_drift(entry: dict) -> bool:
    """True iff some observed output could make this entry DRIFT.

    Derived from the DRIFT branches of `compare`, `census.same_value` and `census.compare_count`;
    a fourth DRIFT branch added to any of them must update this. `compare` drifts only on a
    recorded result that is a bare integer, `same_value` only when the recorded result carries
    a `count N;` prefix, and `compare_count` only for a declared `count` (int, not bool). A typed
    entry is evaluated in-process and is never skipped.
    """
    census = _census()
    if census.is_typed(entry):
        return True
    count = entry.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        return True
    recorded = str(entry.get("result") or "")
    if census.split_count(recorded)[0] is not None:
        return True
    return re.fullmatch(r"-?\d+", recorded.strip()) is not None


class MemoEntry(NamedTuple):
    """One memoised census command. `key` is its `input_key` at the run (None when un-keyable or
    run directly), `ran` the completed `(returncode, stdout, stderr)`, `run_ms` the spawn's wall
    and `key_ms` the key's wall (None when never keyed)."""

    key: Optional[str]
    ran: tuple
    run_ms: int
    key_ms: Optional[int]


_DIRECT_BASIS = "direct: key cost exceeds spawn"
#: A stored `MemoEntry.key` for a command whose key at an unchanged tree (stat walk, no hash)
#: costs no less than its spawn: it runs directly. Never an `input_key` digest, so it never hits.
_DIRECT_KEY = "direct"
#: Largest stdout+stderr a keyed `MemoEntry` retains; a bigger run is judged and not stored, so the
#: engine-lifetime memo cannot grow by one unbounded census output per plan.
_MEMO_MAX_OUTPUT = 256 * 1024

#: git subcommands that read objects reachable from HEAD and nothing else mutable.
_GIT_HISTORY = frozenset("log show rev-list ls-tree cat-file rev-parse".split())
_GIT_WIDE_REFS = frozenset({"--all", "--branches", "--tags", "--remotes"})
#: Options whose output depends on the wall clock, so HEAD alone does not key it.
_GIT_CLOCK_OPTS = frozenset({"--since", "--until", "--after", "--before", "--max-age", "--min-age"})
_REV = r"(?:HEAD|[0-9a-f]{7,40})(?:[~^]\d*)*"
_REVSPEC = re.compile(rf"^{_REV}(?:\.{{2,3}}{_REV})?(?::.*)?$")
_REV_PARSE_OPTS = frozenset({"--short", "--verify", "-q", "--quiet"})
_UNBOUNDED_SHAPE = re.compile(r"[;&`\n]|\|\|")


class _TreeView:
    """One `revalidate` call's read-only view of the index and the files under an operand.

    The index is parsed at most once (`parse_index_identity`, never written, never refreshed) and
    each path's identity is computed at most once, so N entries over one tree pay for it once.
    `deadline` (a `time.monotonic()` value) makes a long directory walk give up as un-keyable."""

    def __init__(
        self, root: Path, deadline: Optional[float] = None, store: Any = None,
        deadline_gates_hashing: bool = False, hash_budget_s: Optional[float] = None,
    ):
        self.root = Path(root)
        self.deadline = deadline
        self.store = store
        self.deadline_gates_hashing = deadline_gates_hashing
        #: Seconds of hashing this view may spend; once `hash_s` reaches it a file without a
        #: stored or index identity has none this call, so its key is None and the entry runs.
        #: Identities already hashed stay in the store, so the next call resumes from them.
        self.hash_budget_s = hash_budget_s
        #: Seconds spent hashing so far; the share of a key a later call at an unchanged tree
        #: does not pay.
        self.hash_s = 0.0
        self._subtrees: dict[str, Optional[str]] = {}
        self._loaded = False
        self._dash: Optional[bool] = None
        self._entries: Optional[dict] = None
        self._index_mtime_ns = 0
        self._files: dict[tuple, Optional[str]] = {}
        self._dirs: dict[str, Optional[str]] = {}
        self._kids: dict[str, Optional[list]] = {}

    def has_dash_names(self) -> bool:
        """True when the worktree root holds an entry whose name begins with `-`, the only thing
        an unquoted `--opt=*.x` word could glob to."""
        if self._dash is None:
            try:
                self._dash = any(n.startswith("-") for n in os.listdir(self.root))
            except OSError:
                self._dash = True
        return self._dash

    def entries(self) -> Optional[dict]:
        if not self._loaded:
            self._loaded = True
            _census()
            from coordinator_core.git import git_index
            from coordinator_core.git.git_dir import resolve_git_dir

            try:
                self._index_mtime_ns = (resolve_git_dir(self.root) / "index").stat().st_mtime_ns
            except OSError:
                self._index_mtime_ns = 0
            try:
                self._entries = git_index.parse_index_identity(self.root)
            except (OSError, ValueError):
                self._entries = None
        return self._entries

    def _stat_clean(self, e: Any, st: os.stat_result) -> bool:
        """scoped_status's stat compare plus git's racy-clean rule: an entry whose mtime is not
        older than the index file's own mtime cannot be trusted by stat alone."""
        if e.mode & 0o170000 != 0o100000:
            return False
        mtime_ns = st.st_mtime_ns
        same = e.size == (st.st_size & 0xFFFFFFFF) and e.mtime == mtime_ns // 1_000_000_000
        if same and e.mtime_nsec:
            same = e.mtime_nsec == mtime_ns % 1_000_000_000
        return same and e.mtime * 1_000_000_000 + e.mtime_nsec < self._index_mtime_ns

    def _raw_id(self, rel: str, size: int) -> Optional[str]:
        """`raw:<sha1(b"blob <len>\\0" + file bytes)>:<size>`, an identity for a file git does not
        track. It is never written, never compared with a git object and makes no claim about the
        blob git would store, so it does not reopen git_state.py's raw-bytes prohibition."""
        digest = hashlib.sha1(f"blob {size}\0".encode())
        try:
            with open(self.root / rel, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return f"raw:{digest.hexdigest()}:{size}"

    def file_id(self, rel: str, st: os.stat_result, in_dir: bool = False) -> Optional[str]:
        """`<blob sha>:<size>` of a regular file. The sha is the index's for a stat-clean tracked
        file and `checkin_blob_sha` otherwise; the size keeps a CRLF/LF rewrite that normalises to
        one blob from keying equal, since the command reads the bytes on disk. Under a directory
        walk (`in_dir`) an untracked or ignored file takes `_raw_id` instead, so a file
        `checkin_blob_sha` refuses cannot make the whole directory un-keyable."""
        slot = (rel, in_dir)
        if slot in self._files:
            return self._files[slot]
        entries = self.entries()
        ident: Optional[str] = None
        if entries is not None:
            cached = entries.get(rel)
            sha = cached.sha if cached is not None and self._stat_clean(cached, st) else None
            if sha is not None:
                ident = f"{sha}:{st.st_size}"
            else:
                tag = "d" if in_dir else "f"
                stored = self.store.ident(rel, st) if self.store is not None else None
                if stored is not None and stored.startswith(tag + "|"):
                    ident = stored[2:]
                elif (
                    self.deadline_gates_hashing
                    and self.deadline is not None
                    and time.monotonic() > self.deadline
                ):
                    ident = None
                elif self.hash_budget_s is not None and self.hash_s >= self.hash_budget_s:
                    ident = None
                else:
                    computed_at_ns = time.time_ns()
                    hashing_from = time.perf_counter()
                    if cached is None and in_dir:
                        ident = self._raw_id(rel, st.st_size)
                    else:
                        from coordinator_core.git.commit import checkin_blob_sha

                        sha = checkin_blob_sha(self.root, rel)
                        if sha is not None:
                            ident = f"{sha}:{st.st_size}"
                    self.hash_s += time.perf_counter() - hashing_from
                    if ident is not None and self.store is not None:
                        self.store.put_ident(rel, st, f"{tag}|{ident}", computed_at_ns)
        self._files[slot] = ident
        return ident

    def _subtree(self, rel: str) -> Optional[str]:
        """The digest text of everything under `rel`, memoised per directory so a directory
        nested under another operand is scanned once; None when any part cannot be bounded."""
        if rel not in self._subtrees:
            self._subtrees[rel] = self._scan(rel)
        return self._subtrees[rel]

    def _children(self, rel: str) -> Optional[list]:
        """`rel`'s entries as sorted `(name, kind, stat_or_link)`, scandir'd once per call and
        shared by the key walk and `walk`. kind: "l" symlink (link target), "d" directory, "f"
        regular file (lstat), "o" anything else; None when the directory cannot be listed."""
        if rel not in self._kids:
            kids: Optional[list] = []
            try:
                with os.scandir(self.root / rel) as it:
                    for kid in sorted(it, key=lambda k: k.name):
                        if kid.name == ".git":
                            continue
                        if kid.is_symlink():
                            kids.append((kid.name, "l", os.readlink(kid.path)))
                        elif kid.is_dir(follow_symlinks=False):
                            kids.append((kid.name, "d", None))
                        elif kid.is_file(follow_symlinks=False):
                            kids.append((kid.name, "f", kid.stat(follow_symlinks=False)))
                        else:
                            kids.append((kid.name, "o", None))
            except OSError:
                kids = None
            self._kids[rel] = kids
        return self._kids[rel]

    def _scan(self, rel: str) -> Optional[str]:
        if (
            self.deadline is not None
            and not self.deadline_gates_hashing
            and time.monotonic() > self.deadline
        ):
            return None
        kids = self._children(rel)
        if kids is None:
            return None
        lines: list[str] = []
        for name, kind, info in kids:
            child = f"{rel}/{name}"
            if kind == "l":
                lines.append(f"l\0{child}\0{info}\n")
            elif kind == "d":
                inner = self._subtree(child)
                if inner is None:
                    return None
                lines.append(f"d\0{child}\n")
                lines.append(inner)
            elif kind == "f":
                ident = self.file_id(child, info, True)
                if ident is None:
                    return None
                lines.append(f"f\0{child}\0{ident}\n")
            else:
                return None
        return "".join(lines)

    def walk(self, rel: str) -> Optional[list]:
        """FileSource: every regular file under `rel` (`rel` itself when a file) as
        `(path, lstat)`, skipping `.git` and symlinks; None when a part cannot be bounded."""
        try:
            st = os.lstat(self.root / rel)
        except OSError:
            return None
        if _stat.S_ISREG(st.st_mode):
            return [(rel, st)]
        if not _stat.S_ISDIR(st.st_mode):
            return None
        out: list = []
        stack = [rel]
        while stack:
            here = stack.pop()
            kids = self._children(here)
            if kids is None:
                return None
            for name, kind, info in kids:
                child = f"{here}/{name}"
                if kind == "d":
                    stack.append(child)
                elif kind == "f":
                    out.append((child, info))
                elif kind == "o":
                    return None
        return out

    def read(self, rel: str) -> Optional[bytes]:
        """FileSource: the file's bytes, None when unreadable."""
        try:
            return (self.root / rel).read_bytes()
        except OSError:
            return None

    def dir_id(self, rel: str) -> Optional[str]:
        if rel not in self._dirs:
            text = self._subtree(rel)
            self._dirs[rel] = (
                hashlib.sha1(text.encode("utf-8", "surrogateescape")).hexdigest()
                if text is not None else None
            )
        return self._dirs[rel]

    def path_id(self, rel: str) -> Optional[str]:
        """Content identity of one operand: "absent", a file id, or a directory digest; None when
        it cannot be bounded (a top-level symlink or special file, an unreadable path)."""
        try:
            st = os.lstat(self.root / rel)
        except (FileNotFoundError, NotADirectoryError):
            return "absent"
        except (OSError, ValueError):
            return None
        if _stat.S_ISREG(st.st_mode):
            return self.file_id(rel, st)
        if _stat.S_ISDIR(st.st_mode):
            return self.dir_id(rel)
        return None


_DOLLAR_EXPANDS = frozenset("{(!@*#?-$_") | frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)


def _shell_expands(command: str, dash_globs_literal: bool = False) -> bool:
    """True when the shell would expand something in `command` before the program runs: an
    unquoted glob character or brace, an unquoted word-leading `~`, a backtick, or a `$` that
    begins a parameter, command or arithmetic expansion. A quoted `*` and a `$` followed by a
    space, quote or end are literal text. With `dash_globs_literal`, a glob character inside a
    word that begins with `-` (`--include=*.py`) is literal: it could expand only to a file
    whose name begins with `-`, and the caller has checked that none exists."""
    quote = ""
    word_start = True
    dash_word = False
    i = 0
    while i < len(command):
        ch = command[i]
        if word_start and not quote:
            dash_word = ch == "-"
        nxt = command[i + 1] if i + 1 < len(command) else ""
        if ch == "`" and quote != "'":
            return True
        if quote == "'":
            if ch == "'":
                quote = ""
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = ""
            elif ch == "$" and nxt in _DOLLAR_EXPANDS:
                return True
        elif ch == "\\":
            i += 1
        elif ch in "'\"":
            quote = ch
        elif ch == "$":
            if nxt not in _DOLLAR_EXPANDS:
                if nxt in ("", " ", "\t"):
                    i += 1
                    word_start = False
                    continue
            return True
        elif ch in "*?[{":
            if not (dash_globs_literal and dash_word):
                return True
        elif ch == "~" and word_start:
            return True
        word_start = not quote and (ch.isspace() or ch in "|<>();&")
        i += 1
    return False


def _git_history_only(args: list[str], head: Optional[str]) -> bool:
    """True when a git stage reads only objects reachable from HEAD, so `head` keys it."""
    if head is None:
        return False
    i = 2 if args[:2] == ["-C", "."] else 0
    while i < len(args) and args[i] == "--no-pager":
        i += 1
    if i >= len(args) or args[i] not in _GIT_HISTORY:
        return False
    sub = args[i]
    for tok in args[i + 1:]:
        if tok == "--":
            break
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if (
                name in _GIT_WIDE_REFS
                or name.startswith(("--glob", "--exclude", "--batch"))
                or name in ("-g", "--walk-reflogs", "--reflog", "--source", "--notes")
                or name in _GIT_CLOCK_OPTS
                or "relative" in tok
                or "%ar" in tok
                or "%cr" in tok
                or "decorate" in tok
                or "%d" in tok
                or "%D" in tok
            ):
                return False
            if sub == "rev-parse" and name not in _REV_PARSE_OPTS:
                return False
        elif not (tok.isdigit() or _REVSPEC.match(tok)):
            return False
    return True


def input_key(
    entry: dict,
    repo_root: Path,
    *,
    head: Optional[str],
    view: Optional[_TreeView] = None,
    stage_cache: Optional[dict] = None,
) -> Optional[str]:
    """A digest over everything the entry's command can read, or None when that set cannot be
    bounded. INVARIANT: equal keys for one command imply byte-identical stdout, stderr and
    returncode. Every rule fails toward None, which means "run it". Spawns nothing, writes
    nothing, never refreshes the index and never calls scoped_changed.

    None when: the entry is typed or has no command; the command carries `;`, `&&`, `||`, `&`,
    a backtick or a newline, or does not tokenise (`census._split_stages`); the shell would
    expand something (an unquoted glob character, brace, `$`, backtick or word-leading `~`, or a
    `$` inside double quotes; a quoted pattern like 'a.*b' is literal and is kept); a redirect other than to /dev/null; a program outside
    grep, wc, find, ls, cat, jq, head, tail, sort, uniq, cut, tr and git (rg reads ignore files and
    sed/awk programs can read files argv does not name, so none of the three is keyed); a grep
    `--file`/`--exclude-from` option; an
    absolute path, a `..` or `.git` segment, or a "." operand (the whole worktree); a stage
    that reads the tree by default with no operand; a `find` predicate, `ls` flag or grep
    option outside the bounded sets; a git stage that is not `[-C .] log|show|rev-list|ls-tree|
    cat-file|rev-parse` over HEAD-reachable revisions with no wide-ref option (and any git stage
    with `head` None); an operand whose files cannot be hashed in process.

    Operand identity: "absent" for a missing path; for a file, the index sha when the entry is
    stat-clean (racily-clean entries count as stale) and `commit.checkin_blob_sha` when it is
    stat-stale or untracked, each joined to the on-disk size; for a directory, every file under
    it by os.scandir (untracked and ignored included, `.git` skipped, symlinks by readlink
    text, never followed). Inside a directory an untracked or ignored file takes an identity hash
    of its raw bytes (`raw:` prefix; never a claim about a git blob), so a CR-bearing untracked
    file keys; a tracked file under a directory, and a file operand, follow the rule above. The
    index is read once per `view`.
    """
    census = _census()
    ops = _operands()
    if census.is_typed(entry):
        return None
    command = str(entry.get("command") or "").strip()
    tree =view if view is not None else _TreeView(Path(repo_root))
    if (
        not command
        or _UNBOUNDED_SHAPE.search(command)
        or _shell_expands(command, not tree.has_dash_names())
    ):
        return None
    stages = _stages(command, stage_cache)
    if not stages or any(not st for st in stages):
        return None
    digest = hashlib.sha1()
    uses_head = False
    for index, raw in enumerate(stages):
        parts = ops.scrub_redirects(raw)
        if not parts:
            return None
        prog, args = parts[0], parts[1:]
        digest.update(json.dumps(parts).encode("utf-8", "surrogateescape") + b"\n")
        if prog == "git":
            if not _git_history_only(args, head):
                return None
            uses_head = True
            continue
        if prog not in ops.READERS:
            return None
        operands = ops.stage_operands(prog, args, index == 0)
        if operands is None:
            return None
        for token in operands:
            rel = ops.operand_rel(token)
            ident = tree.path_id(rel) if rel is not None else None
            if ident is None:
                return None
            digest.update(f"{token}\0{ident}\n".encode("utf-8", "surrogateescape"))
    if uses_head:
        digest.update(f"head\0{head}\n".encode())
    return digest.hexdigest()


def _stages(command: str, cache: Optional[dict]) -> Optional[list]:
    """`census._split_stages(command)`, split once per distinct command when a per-call `cache`
    is given."""
    if cache is None:
        return _census()._split_stages(command)
    if command not in cache:
        cache[command] = _census()._split_stages(command)
    return cache[command]


def _git_stage_refused(entry: dict, head: Optional[str], stage_cache: Optional[dict] = None) -> bool:
    """True when `input_key` is certain to refuse `entry` on a git stage (one that reads beyond
    HEAD, or any git stage with `head` None). `revalidate` runs those after every other entry."""
    census = _census()
    if census.is_typed(entry):
        return False
    command = str(entry.get("command") or "").strip()
    if not command or _UNBOUNDED_SHAPE.search(command):
        return False
    stages = _stages(command, stage_cache)
    for raw in stages or ():
        parts = _operands().scrub_redirects(raw) if raw else None
        if parts and parts[0] == "git" and not _git_history_only(parts[1:], head):
            return True
    return False


def _ms_since(started: float) -> int:
    return int(round((time.perf_counter() - started) * 1000))


def _plan_frontmatter(plan: Path) -> dict:
    """The plan's parsed frontmatter, `{}` when unreadable or malformed."""
    yaml = _load_yaml()
    try:
        parts = plan.read_text(encoding="utf-8", errors="replace").split("---", 2)
        fm = yaml.safe_load(parts[1]) if len(parts) >= 3 else {}
    except (OSError, yaml.YAMLError):
        return {}
    return fm if isinstance(fm, dict) else {}


def spawning_commands(plan: Path, repo_root: Path, doc: Optional[dict] = None) -> tuple[str, ...]:
    """The distinct commands `revalidate(decidable_only=True)` would spawn: untyped, screen-clear
    and `can_drift`. Spawns nothing, so a python target is screened without the tracked-file
    check (the estimate may include a command the live screen later refuses). `doc` is a
    frontmatter dict the caller already parsed."""
    census = _census()
    fm = doc if doc is not None else _plan_frontmatter(plan)
    entries = fm.get("census") if isinstance(fm, dict) else None
    if not isinstance(entries, list):
        return ()
    out: dict[str, None] = {}
    for e in entries:
        if not isinstance(e, dict) or census.is_typed(e):
            continue
        command = str(e.get("command") or "").strip()
        if command and can_drift(e) and census.screen(command, None) is None:
            out[command] = None
    return tuple(out)


def _remaining_timeout(timeout: int, deadline: Optional[float]) -> Optional[int]:
    """The spawn timeout under an optional deadline; `None` when the deadline is spent."""
    if deadline is None:
        return timeout
    remaining = deadline - time.monotonic()
    if remaining < 1:
        return None
    return min(timeout, int(remaining))


def _deadline_row(e: dict) -> dict:
    return {
        "question": str(e.get("question") or ""),
        "command": str(e.get("command") or "").strip(),
        "state": UNRUNNABLE,
        "detail": _DEADLINE_SPENT,
    }


def _deadline_bounded(
    e: dict, repo_root: Path, timeout: int, plan_rel: Optional[str], deadline: Optional[float]
) -> dict:
    """`run_entry` under an optional `time.monotonic()` deadline.

    `None` is the unbounded CLI path. Under a deadline an entry gets `min(timeout, remaining)`
    seconds, and one reached with under a second left is UNRUNNABLE without a spawn.
    """
    bounded = _remaining_timeout(timeout, deadline)
    if bounded is None:
        return _deadline_row(e)
    return run_entry(e, repo_root, bounded, plan_rel)


#: `_entry_row`'s answer under `stats["hold"]` for an entry that needs a spawn; never a row.
_HELD: dict = {"state": "HELD"}


def _entry_row(
    e: dict,
    repo_root: Path,
    timeout: int,
    plan_rel: Optional[str],
    deadline: Optional[float],
    decidable_only: bool,
    memo: Optional[dict],
    head: Optional[str] = None,
    view: Optional[_TreeView] = None,
    stats: Optional[dict] = None,
) -> dict:
    """One entry's row under the skip and memo. Order: typed, empty and refused entries go
    through `_deadline_bounded` as before; then the cannot-drift skip; then the cost gate (a
    prior run no dearer than its key spawns directly, keyless); then `input_key` against the
    prior key (an equal non-None key judges the prior run, no spawn); then a deadline-bounded
    spawn whose completed run is stored as a `MemoEntry`. Neither a skip nor a memo hit spends a
    spawn, so neither consults the deadline.

    `stats["shared"]` holds the per-call screen, key and stage-split results by command text.
    Under `stats["hold"]` an entry with no prior run is held before it is keyed. The cost gate
    reads `key_ms` as the key's cost at an unchanged tree (its hashing excluded); a prior whose
    key is `_DIRECT_KEY` or any non-None key at that cost spawns directly, and a prior with a
    None key is keyed again. `stats["ran"]`, command -> `(ran, run_ms)`, supplies the batch's
    results in place of a spawn; a command absent from it was never reached."""
    census = _census()
    command = str(e.get("command") or "").strip()
    if census.is_typed(e) or not command:
        return _deadline_bounded(e, repo_root, timeout, plan_rel, deadline)
    shared = stats.setdefault("shared", {}) if stats is not None else {}
    screens = shared.setdefault("screen", {})
    if command not in screens:
        screens[command] = screen(command, repo_root)
    refusal = screens[command]
    if refusal:
        return _deadline_bounded(e, repo_root, timeout, plan_rel, deadline)
    row: dict[str, Any] = {"question": str(e.get("question") or ""), "command": command}
    if decidable_only and not can_drift(e):
        recorded = str(e.get("result") or "")
        row.update({
            "state": UNDECIDABLE,
            "basis": _CANNOT_DRIFT,
            "recorded": recorded[:400],
            "observed": "",
        })
        return row
    if view is not None and stats is not None and stats.get("inproc") and not stats.get("inproc_off"):
        answers = shared.setdefault("inproc", {})
        if command not in answers:
            answers[command] = _inproc().evaluate(command, view, view.store)
        if answers[command] is not None:
            stats["inproc_runs"] += 1
            return _judge(e, row, answers[command], plan_rel)
    prior = memo.get(command) if memo is not None else None
    prior = prior if isinstance(prior, MemoEntry) else None
    direct = (
        prior is not None and prior.key is not None
        and prior.key_ms is not None and prior.run_ms <= prior.key_ms
    )
    key: Optional[str] = None
    key_ms: Optional[int] = None
    if memo is not None and not direct:
        if prior is None and stats is not None and stats.get("hold"):
            return _HELD
        keys = shared.setdefault("key", {})
        if command not in keys:
            hashed_from = view.hash_s if view is not None else 0.0
            started = time.perf_counter()
            computed = input_key(
                e, repo_root, head=head, view=view, stage_cache=shared.setdefault("stages", {})
            )
            spent_ms = _ms_since(started)
            if stats is not None:
                stats["key_ms"] = stats.get("key_ms", 0) + spent_ms
            hashed_ms = int(round(((view.hash_s if view is not None else 0.0) - hashed_from) * 1000))
            keys[command] = (computed, max(0, spent_ms - hashed_ms))
        key, key_ms = keys[command]
        if key is not None and prior is not None and prior.key == key:
            memo[command] = prior._replace(key_ms=key_ms)
            if stats is not None:
                stats["memo_hits"] += 1
            return _judge(e, row, prior.ran, plan_rel)
    if stats is not None and stats.get("hold"):
        return _HELD
    batched = stats.get("ran") if stats is not None else None
    if batched is not None:
        if command not in batched:
            return _deadline_row(e)
        ran, run_ms = batched[command]
    else:
        bounded = _remaining_timeout(timeout, deadline)
        if bounded is None:
            return _deadline_row(e)
        started = time.perf_counter()
        ran = _spawn(command, repo_root, bounded)
        run_ms = _ms_since(started)
    if isinstance(ran, dict):
        row.update(ran)
        return row
    if memo is None:
        return _judge(e, row, ran, plan_rel)
    if direct:
        if len(ran[1]) + len(ran[2]) > _MEMO_MAX_OUTPUT:
            memo.pop(command, None)
            return _judge(e, row, ran, plan_rel)
        memo[command] = MemoEntry(
            prior.key if prior.key == _DIRECT_KEY else None, ran, run_ms, prior.key_ms
        )
        if stats is not None:
            stats["direct_runs"] += 1
        row = _judge(e, row, ran, plan_rel)
        row["basis"] = (f"{row['basis']}; " if row.get("basis") else "") + _DIRECT_BASIS
        return row
    if len(ran[1]) + len(ran[2]) > _MEMO_MAX_OUTPUT:
        memo.pop(command, None)
        return _judge(e, row, ran, plan_rel)
    if key is not None and key_ms is not None and run_ms <= key_ms:
        key = _DIRECT_KEY
    memo[command] = MemoEntry(key, ran, run_ms, key_ms)
    return _judge(e, row, ran, plan_rel)


def revalidate(
    plan: Path,
    repo_root: Path,
    timeout: int,
    *,
    require_stamp: bool = True,
    deadline: Optional[float] = None,
    decidable_only: bool = False,
    memo: Optional[dict] = None,
    head: Optional[str] = None,
) -> dict:
    """Revalidate one plan's census. `decidable_only` skips (no spawn) every command entry whose
    recorded result `can_drift` rules out DRIFT. `memo` is a caller-owned `{command: MemoEntry}`:
    an entry whose `input_key` equals the stored key is judged from the stored run with no spawn,
    and one whose stored spawn cost no more than its key runs directly with no key. `head` is the
    HEAD sha that keys history-reading git stages (None leaves them un-keyed). `memo=None` runs
    no key at all. The reply carries `spent_ms`, the entry loop's monotonic wall, and the
    `memo_hits` and `direct_runs` counts."""
    early, census = _plan_census(plan, require_stamp)
    if early is not None:
        return early
    plan_rel = plan_rel_path(plan, repo_root)
    started = time.monotonic()
    stats = {"memo_hits": 0, "direct_runs": 0}
    if decidable_only or memo is not None:
        view = _TreeView(repo_root, deadline) if memo is not None else None
        dicts = [e for e in census if isinstance(e, dict)]
        order = list(range(len(dicts)))
        if memo is not None:
            order.sort(key=lambda i: _git_stage_refused(dicts[i], head))
        rows: dict[int, dict] = {}
        for i in order:
            rows[i] = _entry_row(
                dicts[i], repo_root, timeout, plan_rel, deadline, decidable_only, memo, head,
                view, stats,
            )
        entries = [rows[i] for i in range(len(dicts))]
    else:
        entries = [
            _deadline_bounded(e, repo_root, timeout, plan_rel, deadline)
            for e in census
            if isinstance(e, dict)
        ]
    spent_ms = int(round((time.monotonic() - started) * 1000))
    return _plan_reply(plan, entries, spent_ms, stats)


_CENSUS_HEAD = re.compile(r"^census:[ \t]*(?:#[^\n]*)?\n", re.M)
_NEXT_TOP_KEY = re.compile(r"^(?![ \t\r\n#-])", re.M)


def _census_block(fm_text: str, yaml: Any) -> Optional[list]:
    """The `census:` list parsed from just that top-level block of the frontmatter, so a plan's
    other keys are never parsed. None (the caller parses the whole frontmatter) when the block
    is not one plain block list: absent or repeated key, a flow or scalar value, a parse error."""
    heads = _CENSUS_HEAD.findall(fm_text)
    if len(heads) != 1 or len(re.findall(r"^census:", fm_text, re.M)) != 1:
        return None
    head = _CENSUS_HEAD.search(fm_text)
    rest = fm_text[head.end():]
    stop = _NEXT_TOP_KEY.search(rest)
    block = fm_text[head.start():head.end() + (stop.start() if stop else len(rest))]
    try:
        doc = yaml.safe_load(block)
    except yaml.YAMLError:
        return None
    census = doc.get("census") if isinstance(doc, dict) else None
    return census if isinstance(census, list) else None


def _parsed_key(plan_rel: Optional[str]) -> Optional[str]:
    """The store key under which a plan's parsed `census:` list is held: the plan's own path
    spelled through its parent (`docs/plans/../plans/a.md`). It must name an existing path or
    `CensusStore.save` drops it as absent, and must differ from `plan_rel` itself, which
    `_TreeView.file_id` owns when a directory operand covers the plan."""
    if not plan_rel or "/" not in plan_rel:
        return None
    parent, name = plan_rel.rsplit("/", 1)
    return f"{parent}/../{parent.rsplit('/', 1)[-1]}/{name}"


def _plan_census(
    plan: Path, require_stamp: bool, store: Any = None, plan_rel: Optional[str] = None
) -> tuple[Optional[dict], list]:
    """`(early_reply, census_entries)`: an early reply when the plan cannot be revalidated at all
    (bad frontmatter, not CERTIFIED under `require_stamp`, no `census:` list). With a `store`
    and no stamp requirement, a plan whose stat is unchanged since its list was last parsed
    takes the stored list and is neither read nor parsed; the stat check is the store's own
    (size, mtime, inode, racy-clean)."""
    pkey = _parsed_key(plan_rel) if store is not None and not require_stamp else None
    computed_at_ns = time.time_ns()
    if pkey is not None:
        try:
            st = os.stat(plan)
        except OSError:
            pkey = None
        else:
            stored = store.ident(pkey, st)
            if stored is not None and stored.startswith("c|"):
                try:
                    return None, json.loads(stored[2:])
                except ValueError:
                    pass
    yaml = _load_yaml()
    text = plan.read_text(encoding="utf-8", errors="replace")
    parts = text.split("---", 2)
    if not require_stamp and len(parts) >= 3:
        sliced = _census_block(parts[1], yaml)
        if sliced is not None:
            if pkey is not None:
                try:
                    blob = json.dumps(sliced)
                    if json.loads(blob) == sliced:
                        store.put_ident(pkey, st, "c|" + blob, computed_at_ns)
                except (TypeError, ValueError):
                    pass
            return None, sliced
    fm = {}
    if len(parts) >= 3:
        try:
            fm = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            return ({"plan": str(plan), "state": UNRUNNABLE, "detail": f"frontmatter: {exc}",
                     "entries": []}, [])
    if require_stamp and not fm.get("mise_prepped_sha"):
        return ({"plan": str(plan), "state": REFUSED,
                 "detail": "not CERTIFIED — the sha leg is ordered first and has not passed",
                 "entries": []}, [])
    census = fm.get("census")
    if not isinstance(census, list):
        return ({"plan": str(plan), "state": REFUSED, "detail": "no census: key",
                 "entries": []}, [])
    return None, census


def _plan_reply(plan: Path, entries: list, spent_ms: int, stats: dict) -> dict:
    """The per-plan reply: the roll-up of `entries` plus the spend counters."""
    if not entries:
        # An empty census revalidates nothing, so it cannot be a pass. The roll-up below
        # decides on set difference, and `set() - {MATCH}` is empty, so a census carrying no
        # runnable entries would fall through to MATCH -- reporting the strongest verdict this
        # leg can give on the strength of zero measurements. That reads identically to a plan
        # whose every counted premise was re-checked and held.
        return {"plan": str(plan), "state": REFUSED,
                "detail": "census: declares no entries -- nothing was revalidated",
                "entries": []}
    states = {e["state"] for e in entries}
    if DRIFT in states:
        state = DRIFT
    elif states - {MATCH}:
        state = UNDECIDABLE
    else:
        state = MATCH
    return {"plan": str(plan), "state": state, "entries": entries, "spent_ms": spent_ms,
            "memo_hits": stats["memo_hits"], "direct_runs": stats["direct_runs"]}


_BUDGET_SPENT = "not run: set census budget spent"
#: Seconds a `revalidate_set` call may spend hashing files it holds no identity for. Past it the
#: file's identity is None, the entry runs, and the next call resumes from the store.
_HASH_BUDGET_S = 0.06
#: The default process-time brightline for one call: parent plus reaped children (DR-344).
_PROCESS_BAR_S = 0.5
#: What a call spends after its last batch: judging the batch's rows, the roll-up and one
#: `store.save()`. It is the ceiling's previous margin (0.5 - 0.32) carried over, not re-measured
#: here; the children's CPU it once also covered is now counted in `_LegCost`, so it is an
#: over-estimate until the scoped census-set measurement replaces it.
_TAIL_RESERVE_S = 0.18
#: A command with no stored cost is assumed to take this share of the budget left, so a batch of
#: strangers cannot spend more than that remainder.
_UNMEASURED_SHARE = 4


def _next_batch(
    commands: list[str], pos: int, left_ms: float, estimate: Any
) -> list[str]:
    """The commands from `pos` that fit one batch: their estimated wall ms (stored run_ms, else
    the share above) sum to at most `left_ms`. Wall ms bounds CPU ms from above. The first
    command is always taken, so a dear command still runs alone."""
    batch: list[str] = []
    spent = 0.0
    for command in commands[pos:]:
        ms, known = estimate(command)
        cost = ms if known else max(ms, left_ms / _UNMEASURED_SHARE)
        if batch and spent + cost > left_ms:
            break
        batch.append(command)
        spent += cost
    return batch


def revalidate_set(
    plans: list[Path],
    repo_root: Path,
    timeout: int,
    *,
    deadline: float,
    head: Optional[str],
    store: Any = None,
    process_budget_ms: int = 500,
) -> dict:
    """Revalidate a set of plans' census under one `time.monotonic()` `deadline`.

    `process_budget_ms` is the call's process-time bar (a value <= 0 reads as 500); no new batch
    starts once `_LegCost.total_s()` passes that bar less `_TAIL_RESERVE_S`.

    Phase 1 spawns nothing: every entry goes through `_entry_row` with the store's runs as the
    memo, a hit is judged from the stored run and an entry needing a spawn is held. Phase 2 runs
    the held entries plan by plan, cheapest estimated remaining cost first (stored run_ms, else
    the store's median, else 1; ties to more never-run commands, then input order), keyable
    entries before git-stage ones within a plan. An entry reached past `deadline` is UNRUNNABLE
    and never stored. One `_TreeView` serves the call and `store.save()` runs once, at the end.
    The deadline gates hashing in the key walk, not the walk: a stat-clean or stored identity is
    free, so a pre-expired deadline still judges every hit, while a first-run hash stops at it.
    """
    started = time.monotonic()
    leg = _LegCost()
    bar_s = process_budget_ms / 1000 if process_budget_ms > 0 else _PROCESS_BAR_S
    spawn_gate_s = bar_s - _TAIL_RESERVE_S
    view = _TreeView(
        repo_root, deadline, store, deadline_gates_hashing=True,
        hash_budget_s=_HASH_BUDGET_S,
    )
    memo: dict[str, MemoEntry] = {}
    initial: dict[str, MemoEntry] = {}
    shared: dict[str, dict] = {}

    def no_spawn() -> bool:
        return leg.total_s() > spawn_gate_s

    p1 = {
        "memo_hits": 0, "direct_runs": 0, "key_ms": 0, "hold": True, "shared": shared,
        "inproc": True, "inproc_runs": 0,
    }
    census = _census()

    def seed(command: str) -> None:
        if command in memo or store is None:
            return
        rec = store.run(command)
        if rec is not None:
            memo[command] = initial[command] = MemoEntry(*rec)

    slots: list[dict] = []
    for plan in plans:
        if no_spawn():
            p1["inproc_off"] = True
        t0 = time.monotonic()
        plan_rel = plan_rel_path(plan, repo_root)
        early, entries = _plan_census(plan, False, store, plan_rel)
        slot: dict[str, Any] = {
            "plan": plan, "rel": plan_rel or str(plan), "early": early, "dicts": [],
            "rows": {}, "held": [], "ms": 0, "hits": 0, "direct": 0,
        }
        slots.append(slot)
        if early is not None:
            continue
        slot["dicts"] = dicts = [e for e in entries if isinstance(e, dict)]
        hits0, direct0 = p1["memo_hits"], p1["direct_runs"]
        for i, e in enumerate(dicts):
            command = str(e.get("command") or "").strip()
            if command and not census.is_typed(e):
                seed(command)
            row = _entry_row(e, repo_root, timeout, plan_rel, None, True, memo, head, view, p1)
            if row is _HELD:
                slot["held"].append(i)
            else:
                slot["rows"][i] = row
        slot["hits"], slot["direct"] = p1["memo_hits"] - hits0, p1["direct_runs"] - direct0
        slot["ms"] += int(round((time.monotonic() - t0) * 1000))

    median = store.median_run_ms() if store is not None else None
    measured = False

    def estimate(command: str) -> tuple[int, bool]:
        rec = store.run(command) if store is not None else None
        if rec is not None and isinstance(rec[2], (int, float)):
            return int(rec[2]), True
        return (median, False) if median is not None else (1, False)

    ranked: list[tuple[int, int, int, dict]] = []
    for pos, slot in enumerate(slots):
        if not slot["held"]:
            continue
        cmds = {str(slot["dicts"][i].get("command") or "").strip() for i in slot["held"]}
        cost, never = 0, 0
        for command in cmds:
            ms, from_store = estimate(command)
            cost += ms
            measured = measured or from_store
            never += 0 if store is not None and store.run(command) is not None else 1
        ranked.append((cost, -never, pos, slot))
    ranked.sort(key=lambda r: r[:3])

    stage_cache = shared.setdefault("stages", {})
    distinct: list[str] = []
    seen_cmds: set[str] = set()
    for _cost, _unrun, _pos, slot in ranked:
        dicts = slot["dicts"]
        slot["order"] = sorted(
            slot["held"], key=lambda i: _git_stage_refused(dicts[i], head, stage_cache)
        )
        for i in slot["order"]:
            command = str(dicts[i].get("command") or "").strip()
            if command not in seen_cmds:
                seen_cmds.add(command)
                distinct.append(command)
    ran_cache: dict[str, tuple] = {}
    at = 0
    while at < len(distinct) and not no_spawn():
        bounded = _remaining_timeout(timeout, deadline)
        if bounded is None:
            break
        left_ms = max(0.0, (spawn_gate_s - leg.total_s()) * 1000)
        batch = _next_batch(distinct, at, left_ms, estimate)
        whole = max(1, int(min(timeout * len(batch), deadline - time.monotonic())))
        results = _spawn_many(batch, repo_root, whole, leg)
        for command, ran, run_ms in zip(batch, results, leg.last_run_ms):
            ran_cache[command] = (ran, run_ms)
        at += len(batch)

    p2 = {"memo_hits": 0, "direct_runs": 0, "key_ms": 0, "shared": shared, "ran": ran_cache}
    pending: list[str] = []
    for _cost, _unrun, _pos, slot in ranked:
        t0 = time.monotonic()
        dicts = slot["dicts"]
        held = slot["order"]
        unreached = False
        hits0, direct0 = p2["memo_hits"], p2["direct_runs"]
        for i in held:
            row = _entry_row(
                dicts[i], repo_root, timeout, plan_rel_path(slot["plan"], repo_root), deadline,
                True, memo, head, view, p2,
            )
            if row.get("state") == UNRUNNABLE and row.get("detail") == _DEADLINE_SPENT:
                row = {**row, "detail": _BUDGET_SPENT, "basis": _BUDGET_SPENT}
                unreached = True
            slot["rows"][i] = row
        slot["hits"] += p2["memo_hits"] - hits0
        slot["direct"] += p2["direct_runs"] - direct0
        if unreached:
            pending.append(slot["rel"])
        slot["ms"] += int(round((time.monotonic() - t0) * 1000))
    for command, now in memo.items():
        was = initial.get(command)
        if store is not None and (
            was is None
            or (now.key, now.ran, now.run_ms, now.key_ms)
            != (was.key, was.ran, was.run_ms, was.key_ms)
        ):
            store.put_run(command, now.key, now.ran, now.run_ms, now.key_ms)
    if store is not None:
        store.save()

    out: dict[str, dict] = {}
    covered = census_plans = 0
    for slot in slots:
        if slot["early"] is not None:
            out[slot["rel"]] = slot["early"]
            continue
        entries = [slot["rows"][i] for i in range(len(slot["dicts"]))]
        stats = {"memo_hits": slot["hits"], "direct_runs": slot["direct"]}
        reply = _plan_reply(slot["plan"], entries, slot["ms"], stats)
        out[slot["rel"]] = reply
        if entries:
            census_plans += 1
            if all(r.get("state") != UNRUNNABLE for r in entries):
                covered += 1
    return {
        "plans": out,
        "spent_ms": int(round((time.monotonic() - started) * 1000)),
        "key_ms": p1["key_ms"] + p2["key_ms"],
        "memo_hits": p1["memo_hits"] + p2["memo_hits"],
        "direct_runs": p1["direct_runs"] + p2["direct_runs"],
        "covered": covered,
        "census_plans": census_plans,
        "pending": pending,
        "order": "measured" if measured else "input",
        "children_process_ms": leg.children_ms,
        "spawns": leg.spawns,
        "inproc_runs": p1["inproc_runs"],
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="mise-census-revalidate")
    ap.add_argument("plans", nargs="+", help="plan paths, repo-relative or absolute")
    ap.add_argument("--repo-root", required=True)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="print only non-MATCH rows")
    args = ap.parse_args(argv)

    root = Path(args.repo_root).resolve()
    reports = [revalidate(root / p if not Path(p).is_absolute() else Path(p), root, args.timeout)
               for p in args.plans]

    if args.json:
        reports = [
            {k: v for k, v in r.items() if k not in ("spent_ms", "memo_hits", "direct_runs")}
            for r in reports
        ]
        json.dump(reports, sys.stdout, indent=1)
        print()
    else:
        tally: dict[str, int] = {}
        for rep in reports:
            for e in rep["entries"]:
                tally[e["state"]] = tally.get(e["state"], 0) + 1
            if args.quiet and rep["state"] == MATCH:
                continue
            print(f"census: {rep['state']} — {Path(rep['plan']).name}")
            if rep.get("detail"):
                print(f"  {rep['detail']}")
            for e in rep["entries"]:
                if e["state"] == MATCH and args.quiet:
                    continue
                print(f"  {e['state']:12s} {e['command'][:110]}")
                if e["state"] in (DRIFT, UNDECIDABLE):
                    print(f"    basis    {e.get('basis','')}")
                    print(f"    recorded {e.get('recorded','')[:160]}")
                    print(f"    observed {e.get('observed','')[:160]}")
                elif e.get("detail"):
                    print(f"    {e['detail']}")
        print()
        print(f"{len(reports)} plan(s); entries: "
              + ", ".join(f"{k} {v}" for k, v in sorted(tally.items())))

    states = {r["state"] for r in reports}
    if DRIFT in states:
        return EXIT_DRIFT
    if states - {MATCH}:
        return EXIT_UNCLOSED
    return EXIT_MATCH


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
