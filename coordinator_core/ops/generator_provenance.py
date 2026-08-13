"""
coordinator_core.ops.generator_provenance — discovers generator modules by
write behaviour and reads their declared `GENERATES` provenance.

Purpose: a generator's output going stale unnoticed is a coverage problem
before it is a staleness problem — you cannot ask "which generators failed
to declare their outputs?" by reading artifacts, because an undeclared
generator's artifact is exactly the one you never find. This module answers
it from the generator side: sweep for modules that actually write files,
read whatever provenance they declare, and report the ones that declare
nothing as `Verdict.UNDECLARED` BY NAME rather than silently skipping them.

AC1 — discovery is by write behaviour, parsed from the AST, never a
filename-family match (`emit|generat|regenerat`-style name matching hits
roughly sixty non-test modules against about five real declarations, and
misses real emitters like `write_surface_manifest.py`,
`render_template_tree.py`, `deliverable_ledger_write.py`; its survival
response is `GENERATES = []` pasted everywhere, the forbidden
hand-maintained registry respelled as boilerplate). A module is a generator
iff its source contains a write to a repo-relative path: `Path.write_text`,
`Path.open(..., "w")`, builtin `open(..., "w"/"a")`, or `json.dump` to a
non-tmp target.

AC6 — discovery reads source text only, via `ast.parse`; it never imports a
swept module. This is also what keeps
`coordinator_core/contract/cockpit_schema/emit_schema.py` — coordinator-claude's frozen
release path — from being *executed* by discovery.

AC1 (declaration half) — a discovered generator carrying no `GENERATES` is
reported as `Verdict.UNDECLARED` by name; silence is the failure mode being
designed out. `GENERATES = []` is DECLARED-EMPTY (a genuine non-emitter,
e.g. one that writes only to stdout), distinct from undeclared, and is the
explicit way to say so at the site rather than by absence.

AC10 — a declared pair whose `sources` is empty, is not a list, or names a
path absent from the repo resolves to `Verdict.UNDECLARED`, never to
something a caller would read as FRESH via a silently-empty since-range.
`check_import_budget_staleness.py` carries a review comment about this exact
failure mode on `measured_paths`; `sources` here is the same shape of
hand-written pathspec and gets the same loud treatment.

Negative-spec:
  - This module does not add or edit any generator's `GENERATES` list —
    that is C2's write set. Its own tests use synthetic fixture modules
    under `tmp_path`, never the real ones.
  - This module does not author `staleness_git.py` (C0's file) — it imports
    `Verdict` and `git_root` from it and does not reimplement or shadow
    either.
  - This module is not a central registry of generator/artifact pairs. The
    hand-maintained pair list is the failure class this detector exists to
    catch; discovery is always re-derived from the swept source, never
    cached into a file this module then trusts instead of re-sweeping.
  - This module does not compute freshness/staleness itself (no `git log`,
    no since-range walk) — that is C3/C6's `check_generator_output_staleness.py`.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

from coordinator_core.ops.staleness_git import Verdict

_SWEEP_DIRS = ("coordinator/bin", "bin", "coordinator_core")

_TMP_MARKERS = ("tmp", "temp", "tempfile", "/tmp/", "\\tmp\\")


@dataclass(frozen=True)
class Pair:
    generator: str
    artifact: str
    stamp_key: str
    sources: tuple[str, ...]


@dataclass(frozen=True)
class GeneratorRecord:
    generator: str
    pairs: tuple[Pair, ...]
    verdict: Verdict | None
    detail: str


def _looks_like_tmp(target: str) -> bool:
    lowered = target.lower()
    return any(marker in lowered for marker in _TMP_MARKERS)


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_target_str(call: ast.Call) -> str | None:
    for arg in call.args:
        value = _str_const(arg)
        if value is not None:
            return value
    return None


def _is_write_mode(node: ast.AST) -> bool:
    value = _str_const(node)
    return value is not None and ("w" in value or "a" in value)


def _call_is_write(call: ast.Call) -> bool:
    func = call.func

    if isinstance(func, ast.Attribute):
        attr = func.attr
        if attr == "write_text":
            return True
        if attr == "open":
            for arg in call.args[1:]:
                if _is_write_mode(arg):
                    return True
            for kw in call.keywords:
                if kw.arg == "mode" and _is_write_mode(kw.value):
                    return True
            return False
        if attr == "dump":
            value_expr = getattr(func, "value", None)
            if isinstance(value_expr, ast.Name) and value_expr.id == "json":
                return True
            return False
        return False

    if isinstance(func, ast.Name):
        if func.id == "open":
            for arg in call.args[1:]:
                if _is_write_mode(arg):
                    return True
            for kw in call.keywords:
                if kw.arg == "mode" and _is_write_mode(kw.value):
                    return True
            return False

    return False


def _module_is_generator(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_is_write(node):
            target = _call_target_str(node)
            if target is not None and _looks_like_tmp(target):
                continue
            return True
    return False


def _extract_generates(tree: ast.Module) -> object | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "GENERATES" in names:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and target.id == "GENERATES" and node.value is not None:
                try:
                    return ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    return "__MALFORMED__"
    return None


def _valid_sources(sources: object, repo_root: Path) -> bool:
    if not isinstance(sources, list) or len(sources) == 0:
        return False
    for source in sources:
        if not isinstance(source, str) or not source:
            return False
        if not (repo_root / source).exists():
            return False
    return True


def _build_record(rel_path: str, generates: object, repo_root: Path) -> GeneratorRecord:
    if generates is None:
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=Verdict.UNDECLARED,
            detail=f"{rel_path} writes a repo-relative path but declares no GENERATES",
        )

    if generates == "__MALFORMED__":
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=Verdict.UNDECLARED,
            detail=f"{rel_path} has a GENERATES assignment that is not a literal list",
        )

    if not isinstance(generates, list):
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=Verdict.UNDECLARED,
            detail=f"{rel_path} GENERATES is not a list",
        )

    if len(generates) == 0:
        return GeneratorRecord(
            generator=rel_path,
            pairs=(),
            verdict=None,
            detail=f"{rel_path} declares GENERATES = [] (declared-empty, no artifacts)",
        )

    pairs: list[Pair] = []
    for entry in generates:
        if not isinstance(entry, dict):
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry is not a mapping: {entry!r}",
            )

        artifact = entry.get("artifact")
        stamp_key = entry.get("stamp_key")
        sources = entry.get("sources")

        if not isinstance(artifact, str) or not artifact:
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry missing a valid artifact: {entry!r}",
            )

        if not isinstance(stamp_key, str) or not stamp_key:
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=f"{rel_path} GENERATES entry missing a valid stamp_key: {entry!r}",
            )

        if not _valid_sources(sources, repo_root):
            return GeneratorRecord(
                generator=rel_path,
                pairs=(),
                verdict=Verdict.UNDECLARED,
                detail=(
                    f"{rel_path} GENERATES entry for {artifact!r} has malformed "
                    f"sources (empty, not a list, or naming an absent path): {sources!r}"
                ),
            )

        pairs.append(
            Pair(
                generator=rel_path,
                artifact=artifact,
                stamp_key=stamp_key,
                sources=tuple(sources),
            )
        )

    return GeneratorRecord(
        generator=rel_path,
        pairs=tuple(pairs),
        verdict=None,
        detail=f"{rel_path} declares {len(pairs)} pair(s)",
    )


def discover_generators(repo_root: Path) -> list[GeneratorRecord]:
    records: list[GeneratorRecord] = []

    for sweep_dir in _SWEEP_DIRS:
        root = repo_root / sweep_dir
        if not root.exists():
            continue

        for path in sorted(root.rglob("*.py")):
            if not path.is_file():
                continue

            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError:
                continue

            if not _module_is_generator(tree):
                continue

            rel_path = path.relative_to(repo_root).as_posix()
            generates = _extract_generates(tree)
            records.append(_build_record(rel_path, generates, repo_root))

    return records
