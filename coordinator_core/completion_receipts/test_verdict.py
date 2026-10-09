"""Single writer of a terminal test-runner verdict into the run-report sidecar.

Import-light by contract: the review-stamp mint imports this module, so it must never pull in
`ops.dispatch_emit`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Mapping, Optional

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.session.declared_writes import declare_write

# The user-facing verb; the hand-edit guard and the mint hint re-state this literal, and a test pins them equal.
RECORD_VERB = "test-verdict record"


class TestVerdictRefused(ValueError):
    """Nothing was written; the message names why."""

    __test__ = False


_UNSET_VALUE_RE = re.compile(r"^(?:null|~|''|\"\")$")
_KEY_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*?)[ \t]*$")


def _bind_keys(fm_text: str, bind: Dict[str, str]) -> str:
    """`fm_text` (newline-terminated) with each `bind` key set when absent or null; a key that
    already carries a value is never overwritten."""
    lines = fm_text.split("\n")
    pending = dict(bind)
    for i, line in enumerate(lines):
        m = _KEY_LINE_RE.match(line)
        if m and m.group(1) in pending:
            value = pending.pop(m.group(1))
            if _UNSET_VALUE_RE.match(m.group(2)):
                lines[i] = f"{m.group(1)}: {value}"
    tail = [f"{k}: {v}" for k, v in pending.items()]
    return "\n".join(lines[:-1] + tail + lines[-1:])


def _count(result: Mapping, key: str) -> int:
    v = result.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise TestVerdictRefused(
            f"{key} is not an integer: {v!r}; the result must be a test_result "
            "(status, tests_run, tests_failed, sidecar_path), the shape the runner returns when "
            "its dispatch carries the wake-digest test_result schema"
        )
    if v < 0:
        raise TestVerdictRefused(f"{key} is negative: {v}")
    return v


def derive_verdict(result: Mapping) -> str:
    """Terminal verdict from the runner's `test_result` (status, tests_run, tests_failed).
    A contradictory result is refused, never resolved in either direction."""
    status = result.get("status")
    if status == "error":
        failed = result.get("tests_failed")
        if failed is not None and not isinstance(failed, bool) and isinstance(failed, int) and failed > 0:
            return "fail"
        return "errored"
    if status == "pass-with-skips":
        status = "pass"
    run = _count(result, "tests_run")
    failed = _count(result, "tests_failed")
    if failed > 0:
        if status not in ("fail", "pass"):
            raise TestVerdictRefused(f"unknown status {status!r} with tests_failed {failed}")
        if status == "pass":
            raise TestVerdictRefused(f"status pass contradicts tests_failed {failed}")
        return "fail"
    if status == "pass":
        if run < 1:
            raise TestVerdictRefused("status pass with tests_run 0")
        return "pass"
    if status == "fail":
        raise TestVerdictRefused("status fail with tests_failed 0")
    raise TestVerdictRefused(f"unknown status {status!r}")


def _resolve(repo_root: Path, sidecar_path: object) -> Path:
    if not isinstance(sidecar_path, str) or not sidecar_path:
        raise TestVerdictRefused("result carries no sidecar_path")
    p = Path(sidecar_path)
    return p if p.is_absolute() else Path(repo_root) / p


def _load(path: Path):
    try:
        raw = path.read_bytes().decode("utf-8")
    except OSError:
        raise TestVerdictRefused(f"sidecar absent: {path}") from None
    except UnicodeDecodeError:
        raise TestVerdictRefused(f"sidecar is not UTF-8: {path}") from None
    norm = raw.replace("\r\n", "\n")
    split = split_frontmatter(norm)
    if split is None:
        raise TestVerdictRefused(f"sidecar has no frontmatter: {path}")
    try:
        fm = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        raise TestVerdictRefused(f"sidecar frontmatter is not valid YAML: {path}") from None
    if not isinstance(fm, dict):
        raise TestVerdictRefused(f"sidecar frontmatter is not a mapping: {path}")
    base = split.fm_text if split.fm_text.endswith("\n") else split.fm_text + "\n"
    return norm, "\r\n" in raw, split, fm, base


def _store(path: Path, norm: str, crlf: bool, split, new_fm: str) -> None:
    out = norm.replace(split.fm_text, new_fm, 1)
    path.write_bytes((out.replace("\n", "\r\n") if crlf else out).encode("utf-8"))
    declare_write(str(path))


def _bind(plan_path: Optional[str], agent_type: Optional[str]) -> Dict[str, str]:
    return {k: v for k, v in (("agent_type", agent_type), ("target_plan", plan_path)) if v}


def bind_sidecar(
    repo_root: Path,
    sidecar_path: str,
    *,
    plan_path: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> bool:
    """Bind `target_plan` / `agent_type` into the sidecar (absent or null only, never overwritten),
    leaving `status` alone. Applies to an already-verdicted sidecar; binding is not a verdict.
    Returns whether the file was rewritten; an absent or frontmatter-less sidecar is left alone."""
    path = _resolve(repo_root, sidecar_path)
    try:
        norm, crlf, split, _fm, base = _load(path)
    except TestVerdictRefused:
        return False
    new_fm = _bind_keys(base, _bind(plan_path, agent_type))
    if new_fm == base:
        return False
    _store(path, norm, crlf, split, new_fm)
    return True


def record_test_verdict(
    repo_root: Path,
    result: Mapping,
    *,
    plan_path: Optional[str] = None,
    agent_type: Optional[str] = None,
) -> Path:
    """Derive the verdict from `result` and write it once into `result["sidecar_path"]`; returns
    the absolute sidecar path. Refuses a sidecar that is absent, frontmatter-less, already
    verdicted, or owned by a non-test-runner agent_type other than the passed one."""
    verdict = derive_verdict(result)
    path = _resolve(repo_root, result.get("sidecar_path"))
    norm, crlf, split, fm, base = _load(path)
    if "test_verdict" in fm:
        raise TestVerdictRefused(f"sidecar already carries test_verdict: {path}")
    owner = fm.get("agent_type")
    if owner and not str(owner).endswith("test-runner") and owner != agent_type:
        raise TestVerdictRefused(f"sidecar agent_type {owner!r} is not a test-runner: {path}")
    new_fm = _bind_keys(base, _bind(plan_path, agent_type))
    new_fm += f"test_verdict: {verdict}\n"
    for key, src in (("run", "tests_run"), ("failed", "tests_failed")):
        if key not in fm and result.get(src) is not None:
            new_fm += f"{key}: {result[src]}\n"
    _store(path, norm, crlf, split, new_fm)
    return path.resolve()
