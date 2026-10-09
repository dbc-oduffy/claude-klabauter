"""
coordinator_core.ops.seam_baton_mint — JSON-RPC "seam.mint_batons".

Purpose: residual seam flags (out-of-set gaps the in-workflow pass could not handle) become
staged batons instead of EM questions. Mints through the scaffolder that ``emit-wave-fire
--from-sizing`` uses (``_scaffold_spinoff`` in coordinator-doc-new.py, loaded by
``baton_assemble.apply._load_doc_new_module``); never ``/spinoff``, never a second scaffolder.

Wire params:
    flags (list[{key, plan, class, path?, detail}], required) — ``key`` is the caller's
        stable dedup key; recommended ``<class>|<plan>|<path or "">``.
    run_id (str, required) — names the run in each baton and keys the rollup's filename.
    dry_run (bool, default False) — compute the whole reply, write nothing.

Reply: {"minted": [repo-relative paths], "rollup": path|None, "deduped": [keys], "dry_run": bool}.

Staged = ``deployment_state: awaiting_gate`` + ``pickup_ready: false`` + non-empty ``gate_notes``
+ ``plan_blitz_hold_reason``: the DR-173 parked shape (awaiting_gate with an EMPTY ``blocked_by``).
``handoff.schema.json`` admits only status open|claimed, so there is no ``status: staged`` to use;
promotion to open is ordinary triage (edit deployment_state/pickup_ready, drop the hold).

Dedup: ``state/seam-baton-index.json`` maps key -> baton path. A key is live while its baton
file still exists under ``state/handoffs/`` with a deployment_state outside shipped/continued/
closed; an archived or terminal baton lets the key mint again. One small file read per key, no
corpus parse. Every baton also carries its keys in ``seam_keys:`` so the index is rebuildable.
A rolled-up key maps to the rollup baton, so it dedupes like any other.

Negative-spec: never commits; accepts no caller-supplied root; writes only under
``state/handoffs/`` (exclusive create) and ``state/seam-baton-index.json`` (atomic).
"""

from __future__ import annotations

from coordinator_core.session.declared_writes import declare_write
import hashlib
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.atomic_replace import atomic_write_bytes
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.git.git_state import head_branch
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle import main_worktree_root
from coordinator_core.ops import mint_deliverable_id

GENERATES: list = []
MUTATES = ["state/handoffs/*.md", "state/seam-baton-index.json"]

CAP = 5
INDEX_REL = "state/seam-baton-index.json"
_TERMINAL = ("shipped", "continued", "closed")
_HOLD = "staged by seam.mint_batons: promote through triage before any session fires it"
_NOTES = "Minted for a residual seam flag; staged, not fireable until promoted through triage."
_DEPLOYMENT_RE = re.compile(r"^deployment_state:\s*(\S+)", re.MULTILINE)


def _parse_params(params: dict) -> dict:
    flags = params.get("flags")
    if not isinstance(flags, list):
        raise ValueError("flags must be a list of {key, plan, class, path?, detail}")
    seen: set = set()
    clean: List[dict] = []
    for f in flags:
        if not isinstance(f, dict):
            raise ValueError("each flag must be a mapping")
        for k in ("key", "plan", "class"):
            if not isinstance(f.get(k), str) or not f[k].strip():
                raise ValueError(f"flag {k} must be a non-empty string")
        for k in ("path", "detail"):
            if f.get(k) is not None and not isinstance(f[k], str):
                raise ValueError(f"flag {k} must be a string")
        if f["key"] in seen:
            continue
        seen.add(f["key"])
        clean.append({"key": f["key"], "plan": f["plan"].strip(), "class": f["class"].strip(),
                      "path": (f.get("path") or "").strip(), "detail": (f.get("detail") or "").strip()})
    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")
    dry_run = params.get("dry_run", False)
    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be a bool")
    return {"flags": clean, "run_id": run_id.strip(), "dry_run": dry_run}


def _load_index(root: Path) -> Dict[str, str]:
    try:
        data = json.loads((root / INDEX_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(data, dict) else {}


def _is_live(root: Path, rel: str) -> bool:
    """True when ``rel`` is a baton still on the live shelf and not in a terminal state."""
    if not rel.startswith("state/handoffs/") or ".." in rel.split("/"):
        return False
    try:
        with open(root / rel, encoding="utf-8") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    m = _DEPLOYMENT_RE.search(head)
    return not (m and m.group(1).strip("'\"") in _TERMINAL)


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


def _flag_slug(flag: dict) -> str:
    stem = Path(flag["plan"]).stem
    return re.sub(r"[^a-z0-9]+", "-", f"{flag['class']}-{stem}".lower()).strip("-")[:48]


def _branch(root: Path) -> str:
    name = head_branch(root)
    return name if name and name != "HEAD" else "work/MACHINE/YYYY-MM-DD"


def _describe(flag: dict) -> str:
    where = f" at {flag['path']}" if flag["path"] else ""
    return f"{flag['class']} in {flag['plan']}{where}: {flag['detail'] or 'no detail recorded'}"


def _baton_content(doc_new, *, branch: str, origin, title: str, flags: List[dict], run_id: str,
                   slug: str) -> str:
    lines = [_describe(f) for f in flags]
    covers = (
        f"Residual seam gap(s) from seam run {run_id} that the in-workflow pass could not handle:\n\n"
        + "\n".join(f"- {line}" for line in lines)
    )
    spec = "Decide each gap: close it in the owning plan, or record it as wont_do with the reason.\n\n" + "\n".join(
        f"- key `{f['key']}`" for f in flags
    )
    content = doc_new._scaffold_spinoff(
        title=title,
        branch=branch,
        deliverable_id=mint_deliverable_id.mint(slug=mint_deliverable_id.slug_from_title(title))[0],
        handoff_id=doc_new._mint_artifact_id("hnd", slug),
        summary=doc_new._fit_summary(title),
        what_this_covers=covers,
        specification=spec,
        acceptance=[f"Resolved: {line}" for line in lines],
        kind="spinoff",
        origin=origin,
        next_steps=["1. Re-run the seam check over the owning plan(s) and confirm the flag is gone."],
    )
    return _stage(doc_new, content, flags, run_id)


def _stage(doc_new, content: str, flags: List[dict], run_id: str) -> str:
    """Rewrites the scaffold's readiness lines to the staged shape and adds the mint's own fields.

    Trap: the scaffolder derives readiness from ``blocked_by`` alone, so staged is patched in
    after it; ``baton_assemble`` rewrites scaffold lines the same way.
    """
    q = doc_new._yaml_quote
    out: List[str] = []
    fences = 0
    for line in content.split("\n"):
        if line == "---":
            fences += 1
        if fences == 1:
            if line.startswith("deployment_state:"):
                line = "deployment_state: awaiting_gate"
            elif line.startswith("pickup_ready:"):
                out += ["pickup_ready: false", f"gate_notes: {q(_NOTES)}",
                        f"plan_blitz_hold_reason: {q(_HOLD)}",
                        f"seam_run_id: {q(run_id)}", "seam_keys:",
                        *(f"  - {q(f['key'])}" for f in flags),
                        "producer:", "  typed_command: null", "  op_identity: machine-minted"]
                continue
        out.append(line)
    return "\n".join(out)


def _validate(content: str, rel: str) -> None:
    from coordinator_core.frontmatter.schema_validate import (
        _SCHEMAS_DIR, load_schemas, match_schema, parse_frontmatter, validate_frontmatter_obj,
    )

    fm = parse_frontmatter(content).get("frontmatter")
    match = match_schema(rel, fm, load_schemas(_SCHEMAS_DIR)) if fm is not None else None
    if not match:
        raise ValueError(f"engine failure: no handoff schema resolves for {rel}")
    result = validate_frontmatter_obj(fm, match["schema"])
    if not result.get("ok"):
        raise ValueError(f"minted baton for {rel} fails its schema: {result.get('errors')}")


def _free_path(root: Path, today: str, stem: str) -> str:
    rel = f"state/handoffs/{today}-{stem}.md"
    n = 1
    while (root / rel).exists():
        n += 1
        rel = f"state/handoffs/{today}-{stem}-{n}.md"
    return rel


def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    if repo_root is None:
        raise ValueError("seam.mint_batons requires a resolved repo_root")
    root = Path(main_worktree_root(repo_root))
    ctx = _parse_params(params)
    index = _load_index(root)

    fresh: List[dict] = []
    deduped: List[str] = []
    for f in ctx["flags"]:
        if f["key"] in index and _is_live(root, index[f["key"]]):
            deduped.append(f["key"])
        else:
            fresh.append(f)

    reply = {"minted": [], "rollup": None, "deduped": deduped, "dry_run": ctx["dry_run"]}
    if not fresh:
        return reply

    from coordinator_core.baton_assemble.apply import _load_doc_new_module

    doc_new = _load_doc_new_module()
    today = doc_new._today()
    branch = _branch(root)
    origin = doc_new._resolve_spinoff_origin()
    batches = [([f], f"seam-{_flag_slug(f)}-{_digest(f['key'])}", False) for f in fresh[:CAP]]
    if fresh[CAP:]:
        batches.append((fresh[CAP:], f"seam-rollup-{_digest(ctx['run_id'])}", True))

    planned = []
    for flags, stem, is_rollup in batches:
        rel = _free_path(root, today, stem)
        title = (f"Seam gaps rolled up from run {ctx['run_id']} ({len(flags)})" if is_rollup
                 else f"Seam gap: {flags[0]['class']} in {flags[0]['plan']}")
        try:
            content = _baton_content(doc_new, branch=branch, origin=origin, title=title, flags=flags,
                                     run_id=ctx["run_id"], slug=stem)
        except SystemExit as exc:
            raise ValueError("engine failure: authoring session id does not resolve") from exc
        _validate(content, rel)
        planned.append((rel, content, flags))
        if is_rollup:
            reply["rollup"] = rel
        else:
            reply["minted"].append(rel)
    if ctx["dry_run"]:
        return reply

    for rel, content, flags in planned:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "x", encoding="utf-8", newline="\n") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
        declare_write(target)
        for f in flags:
            index[f["key"]] = rel
    atomic_write_bytes(root / INDEX_REL, (json.dumps(index, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    declare_write(root / INDEX_REL)
    return reply


@register_op("seam.mint_batons")
def _mint_handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "seam.mint_batons" handler. See module docstring."""
    return _handler(params, repo_root)
