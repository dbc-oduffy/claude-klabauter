"""
coordinator_core.ops.research_reground — JSON-RPC "research.reground" op.

Purpose: re-ground a scouts run whose ``grounding.md`` is stale. Moves the report aside to
``grounding.stale-<n>.md``, writes ``reground-<n>.md`` (the original brief plus a generation
line, so the grounding prompt differs per generation and no prompt-keyed cache replays the old
agent), and emits ``scratch/warp/<run_id>.reground-<n>.workflow.mjs``, a script holding only the
scouts ``grounding`` stage over the same scratch dir, with its emission receipt. The EM fires that
script fresh (never resumes the original) and re-runs ``research.close``.

Params: ``run_id`` (required, safe single path segment), ``scratch_dir`` (optional, absolute;
default ``<worktree>/scratch/warp/<run_id>``), ``brief`` (optional, absolute or worktree-relative
file; default ``<scratch>/brief.md`` when present, else ``<scratch>/ask.md``).

Incremental carry: each reground writes ``grounding.basis.json`` (line hashes per digest). A later
reground re-grades only digests changed since that basis or lacking a section in the prior
effective report, writes ``grounding.carry.json`` holding the other digests' prior sections, and
scopes the brief to the changed digests. With no changed digest it is a no-op. Carried rulings are
keyed to the content sha of each digest they judged (fresh ones to the prior basis); only rulings
whose digests are all unchanged survive, and the digests a retired ruling judged are regraded. A
pre-keying string ruling never survives. A ruling is also keyed to the grader version of the brief
that preceded it (the carry's ``grader``); a ruling of an older or unrecorded version is retired
and regraded. Generation 1 writes a carry too (no carried digest), recording the version of the
brief it writes. The brief always states the ruling words and asks for the
judged digest names on each ruling line.

Returns (exit_code 0): ``{run_id, generation, stale, carried, fresh, regraded, problems, brief,
script, next_action, then}``; ``stale`` is the moved report's scratch-relative name or null,
``fresh`` is null until the grader runs. The no-op returns ``{run_id, generation: null, carried,
fresh: 0, regraded: [], problems, next_action}`` and writes nothing. Returns (exit_code 1):
``{error}`` with nothing written, including an unreadable basis or carry file.

Negative-spec:
  - Validates everything before the first write; a refusal leaves the scratch dir untouched.
  - Never deletes the stale report, never commits, never re-runs a scout, never grades a claim.
  - Spawns nothing.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops import _research_grounding as rg
from coordinator_core.ops._path_guard import contained_path, safe_id
from coordinator_core.ops.fleet._common import main_worktree_root

_STAGE_ID = "grounding"
_PIPELINE = "scouts"
_RUN_DIR_ROOT = "scratch/warp"


def _err(error: str) -> dict:
    return {"exit_code": 1, "error": error}


def _resolve_scratch(worktree: Path, run_id: str, given: object) -> tuple[Optional[Path], Optional[str]]:
    if given is None or given == "":
        path = worktree / _RUN_DIR_ROOT / run_id
    elif isinstance(given, str) and Path(given).is_absolute():
        path = Path(given)
    else:
        return None, "scratch_dir must be an absolute path"
    if not path.is_dir():
        return None, f"scratch dir {path.as_posix()!r} does not exist"
    return path.resolve(), None


def _scratch_rel(worktree: Path, scratch: Path) -> tuple[Optional[str], Optional[str]]:
    """Worktree-relative POSIX scratch path; an absolute one only under the local-only research root."""
    guarded = contained_path(scratch, [worktree])
    if guarded is not None:
        return guarded.relative_to(worktree.resolve()).as_posix(), None
    from coordinator_core.ops import _research_local

    try:
        _research_local.under_local_root(scratch, _research_local.local_root(), "scratch_dir")
    except _research_local.LocalRootError as exc:
        return None, f"scratch_dir is under neither the repo nor the local research root: {exc}"
    return scratch.as_posix(), None


def _resolve_brief(worktree: Path, scratch: Path, given: object) -> tuple[Optional[Path], Optional[str]]:
    if given:
        candidate = Path(str(given))
        path = candidate if candidate.is_absolute() else worktree / candidate
        return (path, None) if path.is_file() else (None, f"brief {str(given)!r} is not a file")
    for name in ("brief.md", "ask.md"):
        if (scratch / name).is_file():
            return scratch / name, None
    return None, f"no brief: neither brief.md nor ask.md under {scratch.as_posix()!r}"


def _load_json(path: Path, kind: type) -> tuple[object, Optional[str]]:
    """``(None, None)`` when ``path`` is absent; ``(value, None)`` when it parses to ``kind``; else an error."""
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return None, f"{path.name} unreadable: {exc.__class__.__name__}"
    if not isinstance(value, kind):
        return None, f"{path.name} is not a JSON {kind.__name__}"
    return value, None


def _dump_json(value: object) -> str:
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _scoped_note(
    n: int, stale_name: str, has_report: bool, changed: dict[str, list[int]], flagged: dict[str, list[str]],
    carried: list[str],
) -> str:
    note = f"\nRe-ground generation {n}: the earlier report is {stale_name if has_report else 'absent'}.\n"
    if carried:
        note += "Grade only these digests; the engine keeps the grades of the others and discards any section you write for them.\n"
        for name in sorted(changed):
            lines = ", ".join(str(i) for i in changed[name]) or "none"
            note += f"- {name}: changed lines {lines}\n"
            for ln in flagged.get(name, []):
                note += f"  previously flagged: {ln.strip()}\n"
        note += "Carried, not to be graded: " + ", ".join(carried) + "\n"
    return note + rg.RULING_NOTE


def _reground_sync(worktree: Path, params: dict) -> dict:
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not safe_id(run_id):
        return _err(f"run_id {run_id!r} is not a safe single path segment")
    scratch, problem = _resolve_scratch(worktree, run_id, params.get("scratch_dir"))
    if scratch is None:
        return _err(problem)
    if not any(p.is_file() for p in scratch.glob("digest-*.md")):
        return _err(f"no digest-*.md under {scratch.as_posix()!r}")
    brief_src, problem = _resolve_brief(worktree, scratch, params.get("brief"))
    if brief_src is None:
        return _err(problem)
    scratch_rel, problem = _scratch_rel(worktree, scratch)
    if scratch_rel is None:
        return _err(problem)

    names = sorted(p.name for p in scratch.glob("digest-*.md") if p.is_file())
    try:
        current = {nm: (scratch / nm).read_text(encoding="utf-8") for nm in names}
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"digest unreadable: {exc.__class__.__name__}")
    basis, problem = _load_json(scratch / rg.BASIS_FILE, dict)
    if problem:
        return _err(problem)
    carry, problem = _load_json(scratch / rg.CARRY_FILE, dict)
    if problem:
        return _err(problem)
    report = scratch / rg.GROUNDING_REPORT
    has_report = report.is_file()
    try:
        report_text = report.read_text(encoding="utf-8") if has_report else ""
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"{rg.GROUNDING_REPORT} unreadable: {exc.__class__.__name__}")

    fresh_grader = (carry or {}).get("grader")
    prior = ""
    changed: dict[str, list[int]] = {nm: [] for nm in names}
    if basis is not None:
        since_basis = rg.changed_digests(basis, current)
        current_sha = {nm: rg.digest_sha(t) for nm, t in current.items()}
        judged = {nm: rg.basis_sha(h) for nm, h in basis.items() if isinstance(h, list)}
        prior = rg.effective_report(report_text, carry, set(since_basis), current_sha)
        carried_keys = (carry or {}).get("carried", {})
        carried_rulings = (carry or {}).get("rulings", [])
        pool = list(carried_rulings if isinstance(carried_rulings, list) else [])
        for ln in rg.report_sections(report_text)["rulings"]:
            named = rg._ruling_digests(ln)
            if not named or not named <= carried_keys.keys():
                pool.append(rg.key_ruling(ln, judged, grader=fresh_grader))
        survivors = [r for r in pool if rg.ruling_survives(r, current_sha)]
        graded = rg.report_sections(prior)["digests"]
        changed = dict(since_basis)
        for nm in names:
            if nm not in graded:
                changed.setdefault(nm, [])
        for nm in rg.dropped_ruling_digests(pool, current_sha):
            changed.setdefault(nm, [])
        if not changed:
            return {
                "exit_code": 0, "run_id": run_id, "generation": None,
                "carried": sum(rg.grounding_counts(prior).values()), "fresh": 0, "regraded": [],
                "problems": rg.grounding_problems(prior, names),
                "next_action": {"op": "research.close"},
            }

    from coordinator_core.ops.dispatch_emit.emission_receipt import _write_emission_receipt
    from coordinator_core.ops.dispatch_emit.emit import resolve_agent_type_host
    from coordinator_core.ops.dispatch_emit.op import _pipeline_content_root
    from coordinator_core.ops.dispatch_emit.pipeline_compose import compose_pipeline_script
    from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused, PipelineInputs
    from coordinator_core.ops.dispatch_emit.pipeline_manifest import load_manifest, validate

    n = rg.next_stale_index(p.name for p in scratch.iterdir())
    brief_name = rg.reground_brief_name(n)
    brief_rel = f"{scratch_rel}/{brief_name}"
    stale_name = rg.stale_report_name(n)

    try:
        content_root = _pipeline_content_root()
        manifest = load_manifest(content_root, _PIPELINE)
        stage = next((s for s in manifest.stages if s.id == _STAGE_ID), None)
        if stage is None:
            return _err(f"scouts manifest under {content_root.as_posix()!r} has no {_STAGE_ID!r} stage")
        manifest = dataclasses.replace(manifest, stages=(dataclasses.replace(stage, depends_on=()),))
        inputs = PipelineInputs(brief=brief_rel, subjects=(), scratch_dir=scratch_rel, flags={})
        schedule = validate(manifest, inputs)
        agent_type_host = resolve_agent_type_host(
            coordinator_agent_type_host=os.environ.get("COORDINATOR_AGENT_TYPE_HOST"),
            claude_plugin_root=os.environ.get("CLAUDE_PLUGIN_ROOT"),
        )
        script = compose_pipeline_script(
            manifest, inputs, schedule, run_id=f"{run_id}-reground-{n}", agent_type_host=agent_type_host
        )
    except PipelineEmitRefused as exc:
        return _err("; ".join(exc.reasons))

    try:
        brief_text = brief_src.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _err(f"brief {brief_src.as_posix()!r} unreadable: {exc.__class__.__name__}")
    script_path = worktree / _RUN_DIR_ROOT / f"{run_id}.reground-{n}.workflow.mjs"
    flagged: dict[str, list[str]] = {}
    if basis is None:
        carry_doc = rg.carry_forward("", set(names), names, generation=n, rulings=[], grader=rg.GRADER_VERSION)
    else:
        carry_doc = rg.carry_forward(
            prior, set(changed), names, generation=n, rulings=survivors, grader=rg.GRADER_VERSION
        )
        old = {**(carry or {}).get("carried", {}), **rg.report_sections(report_text)["digests"]}
        flagged = {
            nm: [ln for ln in old.get(nm, []) if rg._verdict(ln.lstrip()) in rg.BLOCKING] for nm in changed
        }
    carried_names = sorted(carry_doc["carried"])
    note = _scoped_note(n, stale_name, has_report, changed, flagged, carried_names)
    try:
        script_path.parent.mkdir(parents=True, exist_ok=True)
        (scratch / brief_name).write_text(brief_text.rstrip("\n") + "\n" + note, encoding="utf-8", newline="")
        script_path.write_text(script, encoding="utf-8", newline="")
        (scratch / rg.CARRY_FILE).write_text(_dump_json(carry_doc), encoding="utf-8", newline="")
        (scratch / rg.BASIS_FILE).write_text(
            _dump_json({nm: rg.line_hashes(t) for nm, t in current.items()}), encoding="utf-8", newline=""
        )
        if has_report:
            os.replace(report, scratch / stale_name)
    except OSError as exc:
        return _err(f"write failed: {exc.__class__.__name__}: {exc}")
    _write_emission_receipt(
        script_path, None, params,
        extras={"pipeline": _PIPELINE, "run_id": f"{run_id}-reground-{n}", "brief": brief_rel,
                "manifest_sha256": manifest.sha256, "subjects": []},
    )
    script_posix = script_path.as_posix()
    return {
        "exit_code": 0,
        "run_id": run_id,
        "generation": n,
        "stale": stale_name if has_report else None,
        "carried": sum(rg.grounding_counts("\n".join(ln for s in carry_doc["carried"].values() for ln in s)).values()),
        "fresh": None,
        "regraded": sorted(changed),
        "problems": rg.grounding_problems(prior, names) if basis is not None else [],
        "brief": brief_rel,
        "script": script_posix,
        "next_action": {"tool": "Workflow", "scriptPath": script_posix},
        "then": {"op": "research.close", "note": "re-run with the params this run's emit returned"},
    }


@register_op("research.reground")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "research.reground" handler.

    Params and reply: see the module docstring.
    """
    if repo_root is None:
        return _err("repo_root (engine common_dir) is required for research.reground")
    worktree = main_worktree_root(repo_root)
    return await asyncio.to_thread(_reground_sync, worktree, params)
