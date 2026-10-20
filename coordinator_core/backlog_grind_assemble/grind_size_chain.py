"""grind-row size-chain: the queue-grind size stage's one engine command.

Skips when a superseded predecessor of the same slug already carries route-to-plan;
otherwise runs sizing-assemble, then coordinator-doc-new, then the ledger mark. A failing step
records its exit code and stderr tail in `<item>.size.md` and a `size`/`fail` ledger line.

Command line:
  size-chain --profile P --row-id R --digest D --sizing S --run-stamp T --repo-root ROOT
             -- <sizing-assemble scout flags>

Negative-spec:
  - No shell, no quoting helper, no `chdir`: the two tools spawn as argv lists with `cwd=ROOT`
    (sizing-assemble falls back to the cwd for its repo root).
  - No git and no subprocess in the lookup.
  - Tools resolve under the settings home's `bin` only, never PATH.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from coordinator_core import _settings_home
from coordinator_core.backlog_grind_assemble import grind_rows
from coordinator_core.ops.dispatch_emit.ask_contract import NO_PLAN_HAND_OFF_FLAG
from coordinator_core.win_portability import no_console_passthrough_kwargs

SKIP_MESSAGE = "size skipped: a superseded predecessor already carries route-to-plan"

_REMINT_ID = re.compile(r"^\d{4}-\d{2}-\d{2}-(.+)-[0-9a-f]{12}$")
_ROUTE_TO_PLAN_BYTES = '"outcome": "route-to-plan"'
_CMD_UNSAFE = "&|<>%^\""

_REQUIRED = ("profile", "row-id", "digest", "sizing", "run-stamp", "repo-root")
_FAIL_REQUIRED = ("profile", "row-id", "digest", "run-stamp", "repo-root", "reason")
_FAIL_USAGE = (
    "usage: grind-row size-fail --profile P --row-id R --digest D --run-stamp T --repo-root D --reason TEXT"
)
_USAGE = (
    "usage: grind-row size-chain --profile P --row-id R --digest D --sizing S "
    "--run-stamp T --repo-root D -- <sizing-assemble flags>"
)


def superseded_predecessor(repo_root: Path, profile: str, row_id: str) -> Optional[Path]:
    """A sibling ledger of a re-minted row that already carries route-to-plan, else None.

    A row id shaped `<date>-<slug>-<12 hex>` is re-minted under a fresh date and hash with
    the same slug. The row's own ledger is excluded. A slug truncated identically for two
    surfaces matches both: the guard errs toward skipping. A missing directory or an
    unreadable ledger is not a match."""
    m = _REMINT_ID.match(row_id)
    if m is None:
        return None
    ledger_dir = grind_rows._ledger_path(repo_root, profile, row_id).parent
    pattern = f"????-??-??-{m.group(1)}-????????????.jsonl"
    own = f"{row_id}.jsonl"
    try:
        names = sorted(os.listdir(ledger_dir))
    except OSError:
        return None
    for name in names:
        if name == own or not fnmatch.fnmatchcase(name, pattern):
            continue
        path = ledger_dir / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _ROUTE_TO_PLAN_BYTES in text:
            return path
    return None


def _resolve_tool(name: str) -> Optional[Path]:
    """`<name>.exe`, `<name>`, then `<name>.cmd` under the settings home's `bin`."""
    bin_dir = _settings_home.settings_home() / "bin"
    for candidate in (f"{name}.exe", name, f"{name}.cmd"):
        path = bin_dir / candidate
        if path.is_file():
            return path
    return None


_STDERR_TAIL_CHARS = 600
_last_stderr: list[str] = [""]


def _run_tool(argv: list[str], cwd: Path) -> int:
    """The one spawn seam; stdout passes through, stderr is echoed and its tail kept in `_last_stderr`."""
    sys.stdout.flush()
    kwargs = no_console_passthrough_kwargs()
    kwargs.pop("stderr", None)
    done = subprocess.run(argv, cwd=str(cwd), stderr=subprocess.PIPE, **kwargs)
    if done.stdout:
        sys.stdout.write(done.stdout.decode("utf-8", errors="replace"))
        sys.stdout.flush()
    text = (done.stderr or b"").decode("utf-8", errors="replace")
    if text:
        sys.stderr.write(text)
        sys.stderr.flush()
    _last_stderr[0] = text[-_STDERR_TAIL_CHARS:]
    return done.returncode


def record_size_fail(
    *, repo_root: Path, profile: str, row_id: str, digest: str, run_stamp: str, reason: str
) -> int:
    """Write `<item>.size.md` holding `reason` and append the row's `size`/`fail` ledger line."""
    evidence = grind_rows.evidence_rel(profile, run_stamp, row_id, "size")
    path = repo_root / evidence
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Size stage failed\n\n{reason}\n", encoding="utf-8", newline="\n")
    return grind_rows.cmd_append(
        [
            "--profile", profile,
            "--row-id", row_id,
            "--digest", digest,
            "--stage", "size",
            "--verdict", "fail",
            "--outcome", "fail",
            "--evidence-file", evidence,
            "--run-stamp", run_stamp,
            "--repo-root", str(repo_root),
        ]
    )


def cmd_size_fail(rest: list[str]) -> int:
    flags = grind_rows._parse_flags(rest, required=_FAIL_REQUIRED)
    if flags is None:
        return grind_rows._usage(_FAIL_USAGE)
    try:
        return record_size_fail(
            repo_root=Path(flags["repo-root"]), profile=flags["profile"], row_id=flags["row-id"],
            digest=flags["digest"], run_stamp=flags["run-stamp"], reason=flags["reason"],
        )
    except grind_rows.RowIdEscapeError as exc:
        return grind_rows._usage(str(exc))


def _spawn(name: str, args: list[str], cwd: Path) -> int:
    _last_stderr[0] = ""
    tool = _resolve_tool(name)
    if tool is None:
        message = f"grind-row size-chain: {name} not found under the settings home bin"
        print(message, file=sys.stderr)
        _last_stderr[0] = message
        return grind_rows.EXIT_REFUSAL
    if tool.suffix.lower() == ".cmd":
        for arg in args:
            bad = next((ch for ch in _CMD_UNSAFE if ch in arg), None)
            if bad is not None:
                message = (
                    f"grind-row size-chain: refusing to run {name}.cmd with an argument holding "
                    f"{bad!r}: cmd.exe re-parses it"
                )
                print(message, file=sys.stderr)
                _last_stderr[0] = message
                return grind_rows.EXIT_REFUSAL
    return _run_tool([str(tool), *args], cwd)


def cmd_size_chain(rest: list[str]) -> int:
    if "--" not in rest:
        return grind_rows._usage(_USAGE)
    split = rest.index("--")
    passthrough = rest[split + 1 :]
    flags = grind_rows._parse_flags(rest[:split], required=_REQUIRED)
    if flags is None:
        return grind_rows._usage(_USAGE)

    repo_root = Path(flags["repo-root"])
    profile, row_id, sizing = flags["profile"], flags["row-id"], flags["sizing"]
    try:
        skip = superseded_predecessor(repo_root, profile, row_id)
    except grind_rows.RowIdEscapeError as exc:
        return grind_rows._usage(str(exc))
    if skip is not None:
        print(SKIP_MESSAGE)
        return grind_rows.EXIT_OK

    assemble = [
        *passthrough,
        "--no-verbatims",
        "--intent-source",
        "em-elaborated",
        "--write",
        str(repo_root / sizing),
        NO_PLAN_HAND_OFF_FLAG,
    ]
    steps = (
        ("sizing-assemble", assemble),
        ("coordinator-doc-new", ["--type", "spinoff", "--from-sizing", sizing]),
    )
    for name, args in steps:
        code = _spawn(name, args, repo_root)
        if code != 0:
            reason = f"{name} exited {code}: {_last_stderr[0].strip() or '(no stderr)'}"
            record_size_fail(
                repo_root=repo_root, profile=profile, row_id=row_id, digest=flags["digest"],
                run_stamp=flags["run-stamp"], reason=reason,
            )
            return code
    return grind_rows.cmd_append(
        [
            "--profile", profile,
            "--row-id", row_id,
            "--digest", flags["digest"],
            "--stage", "size",
            "--verdict", "sized",
            "--outcome", "route-to-plan",
            "--evidence-file", sizing,
            "--run-stamp", flags["run-stamp"],
            "--repo-root", flags["repo-root"],
        ]
    )
