"""coordinator_core.ops.test_red_triage -- the deterministic triage beside
``state/test-red/<machine>.yaml``.

Public contract (C2 tests and C3 wiring build against these):
    build_triage(output, failing, repo_root, *, ran_at, tier) -> dict
    render_triage(doc) -> str
    write_triage(*, repo_root, output, failing, tier, machine=None, ran_at=None) -> Path
    triage_path(repo_root, machine, ran_at) -> Path
    list_triage_files(repo_root, machine) -> list[Path]   # oldest first

File shape: ``state/test-red/<machine>.triage.<YYYYMMDDTHHMMSSZ>.yaml``; after every
write exactly the newest two such files for the machine remain.

NEGATIVE SPEC
    - Pure function of captured output: no pytest run, no git spawn, no LLM
      classification, no severity or root-cause labels.
    - Never touches ``<machine>.yaml`` (frozen DoE-consumed record).
    - No absolute path is written; file keys are repo-relative with ``/``.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.machine_resolver import compute_machine

KEEP = 2
UNGROUPED = "(ungrouped)"

_SECTION_RE = re.compile(r"^_{3,} (.+?) _{3,}\s*$")
_E_LINE_RE = re.compile(r"^E\s+(.*\S)\s*$")
_SUMMARY_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+)(?: - (.*))?$")
_PATH_RE = re.compile(r"[\w.\-]+(?:[\\/][\w.\-]+)*\.[A-Za-z][A-Za-z0-9]{0,5}")
_STAMP_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _stamp(ran_at: str) -> str:
    return re.sub(r"[-:]", "", ran_at)


def triage_path(repo_root: Path, machine: str, ran_at: str) -> Path:
    return Path(repo_root) / "state" / "test-red" / f"{machine}.triage.{_stamp(ran_at)}.yaml"


def list_triage_files(repo_root: Path, machine: str) -> list[Path]:
    d = Path(repo_root) / "state" / "test-red"
    if not d.is_dir():
        return []
    prefix = f"{machine}.triage."
    found = []
    for p in d.iterdir():
        n = p.name
        if n.startswith(prefix) and n.endswith(".yaml") and _STAMP_RE.match(n[len(prefix):-5]):
            found.append(p)
    return sorted(found, key=lambda p: p.name)


def _sections(output: str) -> dict[str, list[str]]:
    """Map pytest failure-section header name -> its ``E`` lines, in order."""
    out: dict[str, list[str]] = {}
    cur: Optional[list[str]] = None
    for line in output.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            cur = out.setdefault(m.group(1), [])
            continue
        if line.startswith("=") and cur is not None:
            cur = None
            continue
        if cur is not None:
            e = _E_LINE_RE.match(line)
            if e:
                cur.append(e.group(1))
    return out


def _summary_messages(output: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in output.splitlines():
        m = _SUMMARY_RE.match(line.rstrip())
        if m and m.group(2) and m.group(1) not in out:
            out[m.group(1)] = m.group(2).strip()
    return out


def _header_name(nodeid: str) -> str:
    return nodeid.split("::", 1)[-1].replace("::", ".")


def _repo_file(text: str, repo_root: Path, cache: dict[str, bool]) -> Optional[str]:
    root = str(repo_root).replace("\\", "/").rstrip("/")
    for m in _PATH_RE.finditer(text):
        cand = m.group(0).replace("\\", "/")
        if cand.startswith(root + "/"):
            cand = cand[len(root) + 1:]
        cand = cand.removeprefix("./")
        if cand not in cache:
            try:
                cache[cand] = (Path(repo_root) / cand).is_file()
            except OSError:
                cache[cand] = False
        if cache[cand]:
            return cand
    return None


def build_triage(
    output: str,
    failing: list[str],
    repo_root: Path,
    *,
    ran_at: str,
    tier: str,
) -> dict:
    """Group ``failing`` node ids by the repo file their assertion text names.

    The first assertion line is the first ``E`` line of the test's failure
    section, else the ``FAILED <id> - <msg>`` summary message, else "".
    The file is the first repo-existing path in the section's ``E`` lines, then
    in the summary message; none -> the ``(ungrouped)`` bucket.
    """
    sections = _sections(output)
    summaries = _summary_messages(output)
    cache: dict[str, bool] = {}
    groups: dict[str, list[dict]] = {}
    for nodeid in sorted(set(failing)):
        e_lines = sections.get(_header_name(nodeid), [])
        summary = summaries.get(nodeid, "")
        first = e_lines[0] if e_lines else summary
        target = None
        for text in [*e_lines, summary]:
            target = _repo_file(text, Path(repo_root), cache)
            if target:
                break
        groups.setdefault(target or UNGROUPED, []).append(
            {"id": nodeid, "assertion": first}
        )
    ordered = sorted(groups, key=lambda k: (k == UNGROUPED, k))
    return {
        "tier": tier,
        "ran_at": ran_at,
        "failing_count": sum(len(v) for v in groups.values()),
        "groups": [
            {"file": k, "count": len(groups[k]), "tests": groups[k]} for k in ordered
        ],
    }


def render_triage(doc: dict) -> str:
    return yaml.safe_dump(
        doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=10**6
    )


def write_triage(
    *,
    repo_root: Path,
    output: str,
    failing: list[str],
    tier: str,
    machine: Optional[str] = None,
    ran_at: Optional[str] = None,
) -> Path:
    """Write the triage and prune to the newest two for ``machine``. Returns the path."""
    machine = machine or compute_machine()
    ran_at = ran_at or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo_root = Path(repo_root)
    target = triage_path(repo_root, machine, ran_at)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = render_triage(build_triage(output, failing, repo_root, ran_at=ran_at, tier=tier))
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    for old in list_triage_files(repo_root, machine)[:-KEEP]:
        try:
            old.unlink()
        except FileNotFoundError:
            pass
    return target
