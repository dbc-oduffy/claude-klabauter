"""
coordinator_core.roadmap_planning_assemble.scaffold_directive — the shared
`coordinator-doc-new` directive constructor.

Purpose: builds a `{id, cli, args, depends_on, already_satisfied}` directive
for `coordinator-doc-new` from a caller's own already-resolved ceremony
state, never from a caller-supplied free-text argument. Every required flag
for a doc type — predecessor fan-in, deliverable-id minting, `--blocks`,
`--sizing-object`, and the rest — is computed here from a declarative
`FlagSpec` sequence the caller supplies per type, so a scaffolder gaining a
required field is a call-site change, never a silent gap. This module is
the CANONICAL shape (docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md
§ "Which shape is canonical"): the first host, and the one every other
in-scope host (`plan_assemble`, `sizing_assemble`, `review_assemble`,
`goals`, `plugin_health`, `execute_plan_assemble`, `backlog_grind_assemble`)
imports rather than hand-assembling its own `coordinator-doc-new` arg list.
Deliberately not a new `ops/docgen` package — two donor shapes already exist
(`baton_assemble`'s flag-computation, `sprint_planning_assemble`'s
compute-half emission) and a third would retire neither.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-remembered.md,
chunk C1.

Negative spec: does NOT decide which doc types are emitted at all — that is
`coordinator_core/ops/doctype_hosts.py`'s (C0) table, read by every host and
by the C8 coverage pin, never re-derived here. Does NOT call
`coordinator-doc-new` or any other CLI — a compute-half constructor only,
same as every emitter in this plan's scope (§ Anti-scope: "Does NOT build an
apply half"). Does NOT wire any host's `brief()` onto this constructor —
that is C3 (this module) and C4-C6 (the other hosts), each importing and
calling what is built here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Union


class ScaffoldDirectiveError(ValueError):
    pass


@dataclass(frozen=True)
class Flag:

    name: str
    key: str
    required: bool = False


@dataclass(frozen=True)
class MutexFlagPair:
    """Exactly one of two flags may be resolved — the
    `--predecessor`/`--predecessor-id` shape `baton_assemble` already gates
    by hand at each call site. Living here instead makes passing both, or
    neither, unreachable BY CONSTRUCTION: every caller goes through this one
    function, so there is no second call site left to re-introduce the bug.
    """

    a: Flag
    b: Flag
    required: bool = True


FlagSpec = Union[Flag, MutexFlagPair]


def _resolve_out_path(out_value: str, root: Path) -> Path:
    """Resolve `out_value` (relative or absolute) against `root` and reject
    it outright if the result would resolve outside `root` — containment is
    the CONSTRUCTOR's job (AC4), never a caller obligation, so an escaping
    relative path (`../../etc/passwd`) or an absolute foreign path both fail
    the same way, here, before any directive is built."""
    resolved_root = root.resolve()
    candidate = Path(out_value)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    normalized = Path(os.path.normpath(str(candidate)))
    try:
        normalized.relative_to(resolved_root)
    except ValueError as exc:
        raise ScaffoldDirectiveError(
            f"--out {out_value!r} resolves outside repo root {resolved_root} — "
            "rejected by the constructor, not left to the caller"
        ) from exc
    return normalized


def _repo_relative_posix(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root.resolve())
    except ValueError:
        rel = path
    return str(rel).replace("\\", "/")


def build_args(doc_type: str, resolved: Mapping[str, Any], flag_spec: Sequence[FlagSpec]) -> list[str]:
    args = [f"--type={doc_type}"]
    for spec in flag_spec:
        if isinstance(spec, MutexFlagPair):
            a_val = resolved.get(spec.a.key)
            b_val = resolved.get(spec.b.key)
            present = [v for v in (a_val, b_val) if v is not None]
            if spec.required and len(present) != 1:
                raise ScaffoldDirectiveError(
                    f"doc type {doc_type!r}: exactly one of {spec.a.name!r}/{spec.b.name!r} "
                    f"must be resolved, got {len(present)}"
                )
            if a_val is not None:
                args.append(f"{spec.a.name}={a_val}")
            elif b_val is not None:
                args.append(f"{spec.b.name}={b_val}")
            continue
        value = resolved.get(spec.key)
        if value is None:
            if spec.required:
                raise ScaffoldDirectiveError(
                    f"doc type {doc_type!r}: required resolved field {spec.key!r} is missing"
                )
            continue
        if isinstance(value, (list, tuple)):
            for item in value:
                args.append(f"{spec.name}={item}")
        else:
            args.append(f"{spec.name}={value}")
    return args


def build_scaffold_directive(
    id_: str,
    doc_type: str,
    resolved: Mapping[str, Any],
    flag_spec: Sequence[FlagSpec],
    *,
    root: Path,
    out_key: str = "out",
    depends_on: Optional[Any] = None,
) -> dict[str, Any]:
    out_value = resolved.get(out_key)
    if not out_value:
        raise ScaffoldDirectiveError(
            f"doc type {doc_type!r}: resolved field {out_key!r} (the --out target) is required"
        )
    resolved_out = _resolve_out_path(str(out_value), Path(root))
    args = build_args(doc_type, resolved, flag_spec)
    args.append(f"--out={_repo_relative_posix(resolved_out, Path(root))}")

    already_satisfied = resolved_out.is_file()
    directive: dict[str, Any] = {
        "id": id_,
        "cli": "coordinator-doc-new",
        "args": args,
        "depends_on": depends_on,
        "already_satisfied": already_satisfied,
    }
    if already_satisfied:
        directive["already_satisfied_reason"] = (
            f"{_repo_relative_posix(resolved_out, Path(root))} already exists on disk — "
            "coordinator-doc-new --out is an unconditional overwrite, so re-firing this "
            "directive would destroy that content"
        )
    return directive
